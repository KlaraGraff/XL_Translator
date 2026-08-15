"""补译模式下「这一对真的是原文＋译文吗」的复核。

补译模式靠启发式判断某段中文的下一段是不是它的译文：像目标语言就跳过，不像就补译。
判错的代价不对称——判成"已有译文"而其实没有，那段中文就永远留在文档里，报告也不会
提（体检用的是同一套启发式），用户翻到才发现；反过来判成"未译"顶多多插一条译文，
看得见、删得掉。所以这里只做一件事：把启发式判定为"已覆盖"的段落对再审一遍，
不可信的打回去重新翻译。

免费规则只留"结构上/字面上就能答完"的两条，语义上的猜测一律交给模型：

1. 原文很短、且译文侧是外文简称（N°、CCTEB、BTR-ANODE-032）→ 直接信任。这类"译文"
   和原文写的就是同一串字母，送模型判等义只会得到没有意义的答案。原文长度这个前提
   不能省：整整一段中文后面跟着一个孤零零的 II 或 PV，那是排版残留，不是它的译文，
   正是本模块要抓的漏网之鱼。
2. 记忆库里已有这段原文的译名，且与文档里这一段完全一致 → 直接信任。译名是特定的，
   比对字符串就够了，不需要模型再判一次。
3. 剩下的全部送模型仲裁。不等义、拿不准，一律打回重新翻译——见开头那条不对称。

曾经还有第三条免费规则「译文/原文长度比正常就直接信任」，9.3.3 删掉了：它挡的是
"只翻了半句"，挡不住"配错了对"——一段语义完全不相符、但长度正常的译文，在那条规则
下根本到不了模型跟前，而配错对恰恰是本模块要抓的主要漏网之鱼。代价是送判对数上升，
由下面的批量协议消化。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

from core.translation_coverage import (
    COVERAGE_COVERED,
    COVERAGE_SOURCE_ONLY,
    CoverageUnit,
    clean_coverage_text,
    looks_like_short_target_token,
)

TRUST_SHORT_TOKEN = "short_token"
TRUST_KNOWN_TRANSLATION = "known_translation"
# 没有模型可用（本地引擎、没配 key）时的保底理由。名字要如实说明"为什么信任"——
# 这里信任的依据不是判过了，而是压根判不了，只能保持原判。
TRUST_NO_MODEL = "no_model"
# 送判对数超过单文件上限时的保底理由，同上：没判，不是判过了。
TRUST_OVER_CAP = "over_cap"
TRUST_MODEL = "model_equivalent"
RETRANSLATE_MODEL = "model_not_equivalent"
RETRANSLATE_UNCERTAIN = "model_uncertain"

VERDICT_EQUIVALENT = "equivalent"
VERDICT_NOT_EQUIVALENT = "not_equivalent"
VERDICT_UNCERTAIN = "uncertain"

# 「译文侧是外文简称就直接信任」只在原文也短的时候成立——表头、编号、单位名称。
# 一整段中文后面跟个 PV，那是排版残留，必须送判。
_SHORT_PAIR_MAX_CHARS = 12
# 每批送多少对。取消长度比预筛后送判对数会翻好几倍，一对一次请求会把请求数打爆，
# 所以按批走。25 是两头夹出来的：再大，一批的原文＋译文容易撑到模型开始漏项或截断
# JSON（漏项按 uncertain 处理，等于白白重译一批）；再小，请求数降不下来，批量就白做了。
_MODEL_BATCH_PAIR_COUNT = 25
# 每个文件最多送多少对进模型。超出的一律信任启发式并记一条日志——批量化之后这个闸
# 可以放宽，但不能拆：它防的是"一份满是双语内容的文档静悄悄打出成百上千次额外请求"。
_MAX_MODEL_CHECKS_PER_FILE = 2000


@dataclass(frozen=True)
class ArbitrationPair:
    """送进模型的一对，带 id。

    id 是对齐用的：模型可能少返一项、多返一项、把顺序打乱，靠位置对齐会把 A 的裁定
    安到 B 头上——那是最难查的一类错。带 id 之后，回来的结果按 id 认领，认不到就是
    uncertain，绝不会张冠李戴。
    """

    id: str
    source: str
    candidate: str


@dataclass
class PairReview:
    """One adjacent-pair verdict."""

    unit: CoverageUnit
    trusted: bool
    reason: str

    @property
    def used_model(self) -> bool:
        return self.reason in {TRUST_MODEL, RETRANSLATE_MODEL, RETRANSLATE_UNCERTAIN}


@dataclass
class ArbitrationOutcome:
    reviews: list[PairReview]
    model_check_count: int = 0
    model_batch_count: int = 0
    skipped_over_cap: int = 0

    @property
    def retranslated(self) -> list[PairReview]:
        return [review for review in self.reviews if not review.trusted]


def collect_arbitration_candidates(units: Iterable[CoverageUnit]) -> list[CoverageUnit]:
    """相邻段落对——单元格不在范围内，见模块说明。

    表格单元格的"已覆盖"是原文和译文挤在同一个格里，打回重译需要写入器支持
    "往已有译文的格里再追加一条"，那是另一件事。这里只处理段落对。
    """
    candidates: list[CoverageUnit] = []
    for unit in units:
        if unit.status != COVERAGE_COVERED or unit.kind != "paragraph":
            continue
        if not clean_coverage_text(unit.source_text):
            continue
        if not clean_coverage_text(unit.target_text):
            continue
        candidates.append(unit)
    return candidates


def review_coverage_pairs(
    units: Iterable[CoverageUnit],
    *,
    known_translations: dict[str, str] | None = None,
    arbitrate: Callable[[Sequence[ArbitrationPair]], Mapping[str, str]] | None = None,
    max_workers: int = 4,
    max_model_checks: int = _MAX_MODEL_CHECKS_PER_FILE,
    batch_size: int = _MODEL_BATCH_PAIR_COUNT,
    notify_model_checks: Callable[[int, int], None] | None = None,
) -> ArbitrationOutcome:
    """Re-check heuristic "already covered" paragraph pairs; flip the untrustworthy ones.

    ``arbitrate(pairs)`` 一次收一批 ``ArbitrationPair``，返回 ``{id: verdict}``，
    verdict 取 "equivalent" / "not_equivalent" / "uncertain"。返回里缺的 id、认不出的
    值、不属于这批的 id，一律按 uncertain 处理——宁可多翻一条，不能让某一对悄悄消失。
    传 None 表示模型不可用（本地引擎、没配 key），此时只跑两条免费规则、其余保持原判。

    ``notify_model_checks(pair_count, batch_count)`` 在开打之前调一次。
    """
    candidates = collect_arbitration_candidates(units)
    if not candidates:
        return ArbitrationOutcome(reviews=[])

    known = {
        clean_coverage_text(source): clean_coverage_text(translation)
        for source, translation in (known_translations or {}).items()
    }

    reviews: list[PairReview] = []
    needs_model: list[CoverageUnit] = []
    for unit in candidates:
        reason = _cheap_verdict(unit, known)
        if reason is not None:
            reviews.append(PairReview(unit=unit, trusted=True, reason=reason))
        else:
            needs_model.append(unit)

    skipped_over_cap = 0
    if arbitrate is None:
        # 没有模型可用时保持原判：启发式说已覆盖就已覆盖，不能因为"没法确认"就
        # 把整份已翻好的文档重翻一遍。
        reviews.extend(
            PairReview(unit=unit, trusted=True, reason=TRUST_NO_MODEL)
            for unit in needs_model
        )
        return ArbitrationOutcome(reviews=reviews)

    if len(needs_model) > max_model_checks:
        skipped_over_cap = len(needs_model) - max_model_checks
        reviews.extend(
            PairReview(unit=unit, trusted=True, reason=TRUST_OVER_CAP)
            for unit in needs_model[max_model_checks:]
        )
        needs_model = needs_model[:max_model_checks]

    batch_count = 0
    if needs_model:
        pairs = [
            ArbitrationPair(
                id=str(index),
                source=clean_coverage_text(unit.source_text),
                candidate=clean_coverage_text(unit.target_text),
            )
            for index, unit in enumerate(needs_model)
        ]
        batches = _split_batches(pairs, batch_size)
        batch_count = len(batches)
        # 这一步是文件预处理阶段唯一会打网络请求的地方，量大时要几分钟；不吭声的话
        # 界面就是一条不动的"正在预处理"。
        if notify_model_checks is not None:
            notify_model_checks(len(needs_model), batch_count)
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
            batch_results = list(executor.map(arbitrate, batches))

        verdicts: dict[str, str] = {}
        for batch, result in zip(batches, batch_results):
            if not isinstance(result, Mapping):
                # 回调没按协议给东西，整批当拿不准——不能因为解析失败就把这一批
                # 当成"都没问题"放过去。
                continue
            allowed = {pair.id for pair in batch}
            for key, value in result.items():
                key = str(key)
                if key not in allowed:
                    # 越界 id（模型自己编的、上一批串过来的）直接丢，不能让它顶替
                    # 本批某一对的裁定。
                    continue
                verdicts[key] = _normalize_verdict(value)

        for pair, unit in zip(pairs, needs_model):
            verdict = verdicts.get(pair.id, VERDICT_UNCERTAIN)
            if verdict == VERDICT_EQUIVALENT:
                reviews.append(PairReview(unit=unit, trusted=True, reason=TRUST_MODEL))
            elif verdict == VERDICT_NOT_EQUIVALENT:
                reviews.append(
                    PairReview(unit=unit, trusted=False, reason=RETRANSLATE_MODEL)
                )
            else:
                reviews.append(
                    PairReview(unit=unit, trusted=False, reason=RETRANSLATE_UNCERTAIN)
                )

    return ArbitrationOutcome(
        reviews=reviews,
        model_check_count=len(needs_model),
        model_batch_count=batch_count,
        skipped_over_cap=skipped_over_cap,
    )


def apply_arbitration(outcome: ArbitrationOutcome) -> list[CoverageUnit]:
    """Flip untrusted pairs back to source-only so they enter the translation pool."""
    flipped: list[CoverageUnit] = []
    for review in outcome.retranslated:
        review.unit.status = COVERAGE_SOURCE_ONLY
        review.unit.reason = (
            "原判为已有译文，复核认定下一段不是这一段的译文，已改为补译。"
        )
        review.unit.data["arbitration"] = review.reason
        flipped.append(review.unit)
    return flipped


def _split_batches(
    pairs: list[ArbitrationPair], batch_size: int
) -> list[list[ArbitrationPair]]:
    size = max(1, int(batch_size or _MODEL_BATCH_PAIR_COUNT))
    return [pairs[start : start + size] for start in range(0, len(pairs), size)]


def _normalize_verdict(value: object) -> str:
    verdict = str(value or "").strip().lower()
    if verdict in {VERDICT_EQUIVALENT, VERDICT_NOT_EQUIVALENT}:
        return verdict
    return VERDICT_UNCERTAIN


def _cheap_verdict(unit: CoverageUnit, known: dict[str, str]) -> str | None:
    """免费规则：命中就返回信任理由，全不命中返回 None（该送模型了）。

    只留结构匹配和精确字符串匹配两条。凡是要"猜语义"的（长度比、词频、字符集比例）
    都不放在这里——猜错的方向恰好是本模块最怕的那个：把配错的对当成好的放过去。
    """
    source = clean_coverage_text(unit.source_text)
    candidate = clean_coverage_text(unit.target_text)

    if looks_like_short_target_token(candidate) and len(source) < _SHORT_PAIR_MAX_CHARS:
        return TRUST_SHORT_TOKEN

    expected = known.get(source)
    if expected and _same_translation(expected, candidate):
        return TRUST_KNOWN_TRANSLATION

    return None


def _same_translation(left: str, right: str) -> bool:
    return _normalize_for_compare(left) == _normalize_for_compare(right)


def _normalize_for_compare(text: str) -> str:
    return "".join(str(text or "").casefold().split())
