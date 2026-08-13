"""补译模式下「这一对真的是原文＋译文吗」的复核。

补译模式靠启发式判断某段中文的下一段是不是它的译文：像目标语言就跳过，不像就补译。
判错的代价不对称——判成"已有译文"而其实没有，那段中文就永远留在文档里，报告也不会
提（体检用的是同一套启发式），用户翻到才发现；反过来判成"未译"顶多多插一条译文，
看得见、删得掉。所以这里只做一件事：把启发式判定为"已覆盖"的段落对再审一遍，
不可信的打回去重新翻译。

按代价从低到高排：能用正则/字符串答完的绝不占用一次模型调用。

1. 原文很短、且译文侧是外文简称（N°、CCTEB、BTR-ANODE-032）→ 直接信任。这类"译文"
   和原文写的就是同一串字母，送模型判等义只会得到没有意义的答案。原文长度这个前提
   不能省：整整一段中文后面跟着一个孤零零的 II 或 PV，那是排版残留，不是它的译文，
   正是本模块要抓的漏网之鱼。
2. 记忆库里已有这段原文的译名，且与文档里这一段完全一致 → 直接信任。译名是特定的，
   比对字符串就够了，不需要模型再判一次。
3. 长度比落在正常区间 → 直接信任。中译法/英，一个汉字通常摊成 1.5~3 个字母，
   译文明显短于原文才是可疑信号（只翻了半句、或者根本配错了对）。
4. 剩下的才送模型仲裁。不等义、拿不准，一律打回重新翻译——见开头那条不对称。
"""

from __future__ import annotations

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
TRUST_LENGTH_RATIO = "length_ratio"
TRUST_MODEL = "model_equivalent"
RETRANSLATE_MODEL = "model_not_equivalent"
RETRANSLATE_UNCERTAIN = "model_uncertain"

# 译文长度 ÷ 原文长度。低于这个值就认为译文短得可疑——中文摊成拉丁字母一般会变长，
# 比原文还短的"译文"多半只翻了一部分，或者压根是被配错对的另一段内容。
_SUSPICIOUS_LENGTH_RATIO = 0.8
# 原文太短时长度比噪声很大（"工期" vs "Délai" 比值 2.5，"是" vs "Oui" 比值 3），
# 这个长度以下不靠比值判断，交给上面几条规则和模型。
_LENGTH_RATIO_MIN_SOURCE_CHARS = 12
# 「译文侧是外文简称就直接信任」只在原文也短的时候成立——表头、编号、单位名称。
_SHORT_PAIR_MAX_CHARS = _LENGTH_RATIO_MIN_SOURCE_CHARS
# 每个文件最多送多少对进模型。超出的一律信任启发式并记一条日志——宁可少判几对，
# 也不能让一份满是双语内容的文档静悄悄打出几百次额外请求。
_MAX_MODEL_CHECKS_PER_FILE = 200


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
    arbitrate: Callable[[str, str], str] | None = None,
    max_workers: int = 4,
    max_model_checks: int = _MAX_MODEL_CHECKS_PER_FILE,
    notify_model_checks: Callable[[int], None] | None = None,
) -> ArbitrationOutcome:
    """Re-check heuristic "already covered" paragraph pairs; flip the untrustworthy ones.

    ``arbitrate(source, candidate)`` 返回 "equivalent" / "not_equivalent" / "uncertain"；
    传 None 表示不可用（本地引擎、没配 key），此时只跑前三条免费规则。
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
            PairReview(unit=unit, trusted=True, reason=TRUST_LENGTH_RATIO)
            for unit in needs_model
        )
        return ArbitrationOutcome(reviews=reviews)

    if len(needs_model) > max_model_checks:
        skipped_over_cap = len(needs_model) - max_model_checks
        reviews.extend(
            PairReview(unit=unit, trusted=True, reason=TRUST_LENGTH_RATIO)
            for unit in needs_model[max_model_checks:]
        )
        needs_model = needs_model[:max_model_checks]

    if needs_model:
        # 这一步是文件预处理阶段唯一会打网络请求的地方，量大时要几分钟；不吭声的话
        # 界面就是一条不动的"正在预处理"。
        if notify_model_checks is not None:
            notify_model_checks(len(needs_model))
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
            verdicts = list(
                executor.map(
                    lambda unit: arbitrate(
                        clean_coverage_text(unit.source_text),
                        clean_coverage_text(unit.target_text),
                    ),
                    needs_model,
                )
            )
        for unit, verdict in zip(needs_model, verdicts):
            value = str(verdict or "").strip().lower()
            if value == "equivalent":
                reviews.append(PairReview(unit=unit, trusted=True, reason=TRUST_MODEL))
            elif value == "not_equivalent":
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


def _cheap_verdict(unit: CoverageUnit, known: dict[str, str]) -> str | None:
    """免费规则：命中就返回信任理由，全不命中返回 None（该送模型了）。"""
    source = clean_coverage_text(unit.source_text)
    candidate = clean_coverage_text(unit.target_text)

    if looks_like_short_target_token(candidate) and len(source) < _SHORT_PAIR_MAX_CHARS:
        return TRUST_SHORT_TOKEN

    expected = known.get(source)
    if expected and _same_translation(expected, candidate):
        return TRUST_KNOWN_TRANSLATION

    if len(source) < _LENGTH_RATIO_MIN_SOURCE_CHARS:
        return TRUST_LENGTH_RATIO
    if len(candidate) / len(source) >= _SUSPICIOUS_LENGTH_RATIO:
        return TRUST_LENGTH_RATIO
    return None


def _same_translation(left: str, right: str) -> bool:
    return _normalize_for_compare(left) == _normalize_for_compare(right)


def _normalize_for_compare(text: str) -> str:
    return "".join(str(text or "").casefold().split())
