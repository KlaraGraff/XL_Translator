"""规则没通过的译文交给模型复查（所有模式共用一份）。

规则（数字、和原文一样、没译成目标语言……）只负责「放行」：通过的直接用，没通过的
不再直接丢掉，而是攒起来请文本翻译模型看一遍：

* 意思一致 → 采用（不涂复核底色，报告里列一行「复查后采用」，不进记忆库）；
* 不一致   → 带着模型指出的问题重译，重译结果过了规则就用，没过再复查一次，
             最多重译「单段重试次数」轮；
* 拿不准、请求出错、超时、模型漏答 → 一律当作拿不准，按各模式原来的办法处理。
  **绝不会因为复查出错就放行一条译文。**

分批：每批最多 10 条、合计约 3000 字，很长的一段单独一批；按编号对答案，漏答的
单独再问一次，还没答就算拿不准。请求走 ``API_REQUEST_CATEGORY_RECOVERY``、同一个
调度器和限流降速；按了停止就不再发请求。``ApiKeyTemporarilyUnavailableError``
照旧往上抛（那是「这个 key 暂时不能用」的调度信号）。

本模块不依赖 ``core.engine_dispatcher``（那边反过来调用这里）。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from loguru import logger

from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.api_scheduler import (
    API_REQUEST_CATEGORY_RECOVERY,
    ApiSchedulerAcquireCancelled,
)
from engines.base_engine import engine_supports_chat, strip_markdown_json

VERDICT_EQUIVALENT = "equivalent"
VERDICT_NOT_EQUIVALENT = "not_equivalent"
VERDICT_UNCERTAIN = "uncertain"
_VERDICTS = frozenset({VERDICT_EQUIVALENT, VERDICT_NOT_EQUIVALENT, VERDICT_UNCERTAIN})

#: 每批最多几条、合计多少字（原文 + 译文）。内部数值，不是设置（maple 2026-10-31）。
REVIEW_BATCH_MAX_ITEMS = 10
REVIEW_BATCH_MAX_CHARS = 3000
#: 限流降速后同一批最多重发几次，防止在一直限流的服务上空转。
_MAX_LIMIT_RETRIES = 6

#: 复查的结果：采用复查意见（意思一致）、重译后通过了规则、仍不能用。
OUTCOME_ACCEPTED = "accepted"
OUTCOME_FIXED = "fixed"
OUTCOME_REJECTED = "rejected"

_REPLACEMENT_CHARS = frozenset({"�", "□"})
_REASON_LANGUAGES = {"zh": "简体中文", "en": "English", "fr": "français"}


@dataclass(frozen=True)
class ReviewItem:
    """一条要复查的译文。``problem`` 是规则发现的问题（一句话，给模型看）。"""

    id: str
    source: str
    translation: str
    problem: str = ""


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str = VERDICT_UNCERTAIN
    reason: str = ""

    @property
    def equivalent(self) -> bool:
        return self.verdict == VERDICT_EQUIVALENT

    @property
    def not_equivalent(self) -> bool:
        return self.verdict == VERDICT_NOT_EQUIVALENT


@dataclass(frozen=True)
class RuleFailure:
    """规则没通过的一条：原文、当前译文、规则说的问题。"""

    source: str
    translation: str
    problem: str


@dataclass(frozen=True)
class RepairOutcome:
    """``status``：``accepted``（复查判一致，采用 ``translation``）、``fixed``（重译后的
    ``translation`` 过了规则）、``rejected``（仍不能用，``translation`` 是最后一稿）。
    ``reason`` 是模型的理由或规则的问题；``rounds`` 是重译了几轮。"""

    status: str
    translation: str
    reason: str = ""
    rounds: int = 0

    @property
    def usable(self) -> bool:
        return self.status in {OUTCOME_ACCEPTED, OUTCOME_FIXED}


# ── 规则这一侧 ────────────────────────────────────────────────────────────


def hard_failure(translation: str) -> str:
    """不值得问模型的硬伤：译文为空、有乱码。返回原因，没有硬伤时为空串。"""
    value = str(translation or "").strip()
    if not value:
        return "译文为空"
    if any(char in value for char in _REPLACEMENT_CHARS):
        return "译文中有乱码"
    return ""


def describe_validation(result) -> str:
    """``TranslationValidationResult`` → 给模型看的一句话（含规则找到的片段）。"""
    parts: list[str] = []
    for issue in getattr(result, "issues", ()) or ():
        message = str(getattr(issue, "message", "") or "").strip().rstrip("。")
        fragments = [str(f) for f in getattr(issue, "fragments", ()) or () if str(f).strip()]
        if fragments:
            message += "（" + "、".join(fragments[:6]) + "）"
        if message:
            parts.append(message)
    return "；".join(parts)


def number_problem(source: str, translation: str) -> str:
    """公共数字规则（不分语种）：原文里的每个数在译文里都要找得到。

    按「数值」比：2.5 与 2,5、1500 与 1 500 是同一个数，换了位置也算；月份写成月份名
    也算。CAD、PDF 原位替换用它代替以前各自按顺序逐字比的写法。
    """
    from core.translation_filter import _missing_number_tokens

    missing = _missing_number_tokens(str(source or ""), str(translation or ""))
    if not missing:
        return ""
    shown: list[str] = []
    for token in missing:
        value = token.token.strip()
        if value and value not in shown:
            shown.append(value)
    return "原文中的数字在译文里没找到：" + "、".join(shown[:6])


# ── 复查请求 ──────────────────────────────────────────────────────────────


def build_review_prompt(reason_lang: str = "zh") -> str:
    reason_language = _REASON_LANGUAGES.get(str(reason_lang or "zh").split("-", 1)[0], "简体中文")
    return (
        "你是严谨的翻译质量复查员。下面每一条译文都没有通过程序的自动检查，"
        "rule_finding 写了程序发现的问题。程序的检查很死板，经常误判，请你逐条判断"
        "译文是否完整、准确地传达了原文的全部实质信息。\n"
        "判定为 equivalent（意思一致）的情况，以下都算一致：\n"
        "- 数字按目标语言习惯书写：小数点写成逗号（2.5 → 2,5）、千分位用空格或点"
        "（1500 → 1 500）、写成单词或汉字（3 → three、50% → 百分之五十）、日期换成目标语言写法、"
        "数字的先后顺序随语序调整；\n"
        "- 专有名词、品牌、公司名、代号、型号、单位按惯例原样保留（这时译文可以和原文一样）。\n"
        "判定为 not_equivalent（不一致）的情况：\n"
        "- 数字、日期、金额、比例或事实被改动或遗漏；\n"
        "- 整句或其中一部分没有翻译、只翻了一部分、只是摘要；\n"
        "- 译文夹杂既不属于原文也不属于目标语言的其他语言文字；\n"
        "- 遗漏主体、义务、条件、范围或关键限制。\n"
        "无法确定时判定为 uncertain。\n"
        f"reason 用{reason_language}写一句话：一致时说明为什么程序的提示是误判，"
        "不一致时说清楚具体错在哪里（会原样交给翻译模型重译）。\n"
        "按 id 逐条作答，每个 id 都要回答。只输出一个 JSON 对象，不要输出 markdown 或其他文字。格式："
        '{"results":[{"id":"1","verdict":"equivalent|not_equivalent|uncertain","reason":"一句话"}]}'
    )


def build_review_batches(
    items: Sequence[ReviewItem],
    *,
    max_items: int = REVIEW_BATCH_MAX_ITEMS,
    max_chars: int = REVIEW_BATCH_MAX_CHARS,
) -> list[list[ReviewItem]]:
    """按条数和字数分批；单条就超过字数上限的（合同里一整段）单独一批。"""
    batches: list[list[ReviewItem]] = []
    current: list[ReviewItem] = []
    current_chars = 0
    max_items = max(1, int(max_items))
    for item in items:
        size = len(item.source) + len(item.translation)
        if size >= max_chars:
            batches.append([item])
            continue
        if current and (len(current) >= max_items or current_chars + size > max_chars):
            batches.append(current)
            current, current_chars = [], 0
        current.append(item)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def _request_weight(texts: Sequence[str], system_prompt: str) -> int:
    # 与 engine_dispatcher._estimate_api_request_weight 同一个尺度：输入 + 提示词 +
    # 很短的输出，约每 4000 字一个调度槽位。
    chars = sum(len(t) for t in texts) + min(len(system_prompt), 4000) + 80 * max(1, len(texts) // 2)
    return max(1, int(math.ceil(chars / 4000)))


def _is_local(engine) -> bool:
    return str(getattr(engine, "engine_name", "") or "").startswith(("ollama/", "local_openai/"))


def _parse_verdicts(raw: str, ids: set[str]) -> dict[str, ReviewVerdict]:
    payload = json.loads(strip_markdown_json(raw))
    if isinstance(payload, dict):
        rows = payload.get("results")
        if rows is None and "verdict" in payload:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        return {}
    verdicts: dict[str, ReviewVerdict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id", "")).strip()
        if not item_id and len(ids) == 1:
            item_id = next(iter(ids))
        if item_id not in ids or item_id in verdicts:
            continue
        verdict = str(row.get("verdict") or "").strip().lower()
        if verdict not in _VERDICTS:
            verdict = VERDICT_UNCERTAIN
        verdicts[item_id] = ReviewVerdict(verdict, str(row.get("reason") or "").strip())
    return verdicts


def _review_batch(
    engine,
    batch: Sequence[ReviewItem],
    *,
    source_lang: str,
    target_lang: str,
    system_prompt: str,
    api_scheduler,
    should_stop,
    error_callback,
    context_label: str,
) -> dict[str, ReviewVerdict]:
    """一次请求复查一批；只返回模型答了的条目。出错返回空（调用方按拿不准处理）。"""
    payload = json.dumps(
        {
            "source_language": source_lang,
            "target_language": target_lang,
            "items": [
                {
                    "id": item.id,
                    "source": item.source,
                    "translation": item.translation,
                    "rule_finding": item.problem,
                }
                for item in batch
            ],
        },
        ensure_ascii=False,
    )
    weight = _request_weight([payload], system_prompt)
    ids = {item.id for item in batch}
    for _attempt in range(_MAX_LIMIT_RETRIES + 1):
        if should_stop is not None and should_stop():
            return {}
        generation: int | None = None
        try:
            if api_scheduler is None:
                raw = engine.chat(system_prompt, payload)
            else:
                with api_scheduler.slot(
                    weight, category=API_REQUEST_CATEGORY_RECOVERY, should_stop=should_stop
                ) as lease:
                    generation = lease.generation
                    raw = engine.chat(system_prompt, payload)
            return _parse_verdicts(raw, ids)
        except ApiKeyTemporarilyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - a review that fails means "unsure", never "accept"
            if isinstance(exc, ApiSchedulerAcquireCancelled) or (should_stop is not None and should_stop()):
                return {}
            if api_scheduler is not None and not _is_local(engine):
                decision = handle_api_concurrency_limit(
                    exc,
                    scheduler=api_scheduler,
                    request_generation=generation,
                    context_label=context_label,
                    error_callback=error_callback,
                    should_stop=should_stop,
                )
                if decision is not None:
                    continue
            logger.debug(f"{context_label}：复查请求失败原始错误：{exc!r}")
            if error_callback is not None:
                error_callback(
                    f"  → {context_label}：有 {len(batch)} 条未通过自动检查的译文复查没有完成，按原来的办法处理。"
                )
            return {}
    return {}


def review_translations(
    engine,
    items: Sequence[ReviewItem],
    *,
    source_lang: str,
    target_lang: str,
    api_scheduler=None,
    should_stop: Callable[[], bool] | None = None,
    error_callback: Callable[[str], None] | None = None,
    context_label: str = "复查",
    reason_lang: str = "zh",
    concurrency: int = 1,
    max_items: int = REVIEW_BATCH_MAX_ITEMS,
    max_chars: int = REVIEW_BATCH_MAX_CHARS,
    system_prompt: str | None = None,
) -> dict[str, ReviewVerdict]:
    """复查 ``items``，返回 ``{id: ReviewVerdict}``；每个 id 都有结果（拿不准也算一个结果）。

    ``system_prompt`` 给了就用它代替默认的复查提示词（返回格式须相同）。
    """
    items = [item for item in items if item.id]
    if not items:
        return {}
    unsure = {item.id: ReviewVerdict(VERDICT_UNCERTAIN, "") for item in items}
    if not engine_supports_chat(engine) or (should_stop is not None and should_stop()):
        return unsure
    prompt = system_prompt or build_review_prompt(reason_lang)

    def run(batch: Sequence[ReviewItem]) -> dict[str, ReviewVerdict]:
        return _review_batch(
            engine,
            batch,
            source_lang=source_lang,
            target_lang=target_lang,
            system_prompt=prompt,
            api_scheduler=api_scheduler,
            should_stop=should_stop,
            error_callback=error_callback,
            context_label=context_label,
        )

    def run_all(batches: list[list[ReviewItem]]) -> dict[str, ReviewVerdict]:
        found: dict[str, ReviewVerdict] = {}
        workers = 1 if _is_local(engine) else max(1, min(int(concurrency or 1), len(batches)))
        if workers == 1:
            for batch in batches:
                found.update(run(batch))
            return found
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(run, batches):
                found.update(part)
        return found

    answered = run_all(build_review_batches(items, max_items=max_items, max_chars=max_chars))
    # 模型漏答的那几条单独再问一次；请求失败的那一批不重问（失败已经是「拿不准」）。
    missing = [item for item in items if item.id not in answered]
    if missing and len(missing) < len(items) and not (should_stop is not None and should_stop()):
        answered.update(run_all([[item] for item in missing]))
    unsure.update(answered)
    return unsure


# ── 复查 + 重译 ───────────────────────────────────────────────────────────


def retranslation_hint(previous: str, reason: str) -> str:
    """接在翻译提示词后面：上一稿和复查指出的问题。"""
    lines = ["", "", "【重译要求】这一段上一次的译文复查没有通过，请重新完整翻译，改正下面指出的问题。"]
    if previous:
        lines.append(f"上一次的译文：{previous}")
    if reason:
        lines.append(f"问题：{reason}")
    return "\n".join(lines)


def review_and_repair(
    failures: Sequence[RuleFailure],
    *,
    engine,
    rule_problem: Callable[[str, str], str],
    retranslate: Callable[[str, str, str], str | None] | None,
    max_rounds: int,
    source_lang: str,
    target_lang: str,
    api_scheduler=None,
    should_stop: Callable[[], bool] | None = None,
    error_callback: Callable[[str], None] | None = None,
    context_label: str = "复查",
    reason_lang: str = "zh",
    concurrency: int = 1,
) -> dict[str, RepairOutcome]:
    """规则没过的条目：复查 → 采用 / 带着问题重译 / 按老办法。返回 ``{原文: RepairOutcome}``。

    ``rule_problem(原文, 译文)`` 是这一处用的规则（空串 = 通过）；``retranslate(原文,
    上一稿, 问题)`` 返回新译文，失败时为 ``None``。重译最多 ``max_rounds`` 轮；重译稿
    没过规则时再复查一次，最后一稿复查仍不一致就按「仍不能用」交回。
    """
    outcomes: dict[str, RepairOutcome] = {}
    pending: dict[str, tuple[str, str]] = {}
    for failure in failures:
        if failure.source and failure.source not in pending:
            pending[failure.source] = (failure.translation, failure.problem)
    rounds = 0
    max_rounds = max(0, int(max_rounds or 0))
    while pending:
        sources = list(pending)
        ids = {source: str(index + 1) for index, source in enumerate(sources)}
        verdicts = review_translations(
            engine,
            [ReviewItem(ids[s], s, pending[s][0], pending[s][1]) for s in sources],
            source_lang=source_lang,
            target_lang=target_lang,
            api_scheduler=api_scheduler,
            should_stop=should_stop,
            error_callback=error_callback,
            context_label=context_label,
            reason_lang=reason_lang,
            concurrency=concurrency,
        )
        stopped = should_stop is not None and should_stop()
        redo: dict[str, tuple[str, str]] = {}
        for source in sources:
            translation, problem = pending[source]
            verdict = verdicts.get(ids[source], ReviewVerdict())
            if verdict.equivalent:
                outcomes[source] = RepairOutcome(OUTCOME_ACCEPTED, translation, verdict.reason, rounds)
            elif verdict.not_equivalent and retranslate is not None and rounds < max_rounds and not stopped:
                redo[source] = (translation, verdict.reason or problem)
            else:
                outcomes[source] = RepairOutcome(
                    OUTCOME_REJECTED, translation, _rejection_reason(problem, verdict.reason), rounds
                )
        if not redo:
            break
        rounds += 1

        def again(source: str) -> str | None:
            previous, reason = redo[source]
            if should_stop is not None and should_stop():
                return None
            return retranslate(source, previous, reason)

        redo_sources = list(redo)
        workers = 1 if _is_local(engine) else max(1, min(int(concurrency or 1), len(redo_sources)))
        if workers == 1:
            new_values = [again(source) for source in redo_sources]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                new_values = list(pool.map(again, redo_sources))
        pending = {}
        for source, value in zip(redo_sources, new_values):
            previous, reason = redo[source]
            candidate = str(value or "").strip()
            if not candidate or hard_failure(candidate):
                outcomes[source] = RepairOutcome(OUTCOME_REJECTED, previous, reason, rounds)
                continue
            problem = rule_problem(source, candidate)
            if not problem:
                outcomes[source] = RepairOutcome(OUTCOME_FIXED, candidate, reason, rounds)
            else:
                pending[source] = (candidate, problem)
    return outcomes


def _rejection_reason(problem: str, review_reason: str) -> str:
    """退回时报告里写什么：规则发现的问题，模型说了理由的再附上。"""
    problem, review_reason = str(problem or "").strip(), str(review_reason or "").strip()
    if problem and review_reason and review_reason != problem:
        return f"{problem}（复查：{review_reason}）"
    return problem or review_reason


def summarize_outcomes(outcomes: Mapping[str, RepairOutcome]) -> tuple[int, int, int]:
    """``(复查后采用, 重译后通过, 仍不能用)`` 的条数。"""
    accepted = sum(1 for o in outcomes.values() if o.status == OUTCOME_ACCEPTED)
    fixed = sum(1 for o in outcomes.values() if o.status == OUTCOME_FIXED)
    return accepted, fixed, len(outcomes) - accepted - fixed


_WS_RE = re.compile(r"\s+")


def short_excerpt(text: str, limit: int = 80) -> str:
    value = _WS_RE.sub(" ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"
