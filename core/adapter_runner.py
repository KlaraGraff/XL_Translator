"""格式适配器的通用执行器（扩展性阶段 D）。

所有格式适配器共用这一个执行器：适配器只负责「取字」和「写回」（``core/format_adapter.py``
的 ``AdapterHost``），其余与 CAD、PDF 原位替换同一套零件——

1. ``host.extract`` 取出单元；``locked`` 的、没有文字的原样写回，其余登记进台账
   （``core/unit_ledger.py``）；
2. 源语言为「自动识别」时，每个文件一次语言预检（``core/language_preflight.py``），定下
   记忆库按哪些语言对查；
3. 「自动识别场景」时先通读写一段文档说明；记忆库会话（``DocumentTermMemory``）整段复用，
   其余经 ``TextTranslationService`` 分批翻译（速率与重试、提示词、逐批日志、句中词条核对）；
4. 残留体检（``run_residual_pass``）；
5. ``host.write`` 按「双语对照」开关写出；
6. 需复核的单元汇总成一份「需复核清单.txt」放在输出目录里。

复核口径与 CAD、PDF 原位替换一致：没拿到译文的单元保留原文、记「未获得译文」、计入需复核；
有译文但体检发现仍有原文、或未采用设定译法的，保留译文并计入需复核。模型原样还回原文（或质量
校验把译文退回原文）也算没拿到译文——原文确实是要翻的源语言文字时（与 Excel、Word 的「未译源文」
同一个判据），不算已翻译、不进记忆库。

``TextUnit.protected`` 里的片段（行内代码、链接地址）翻译前换成 ``⟦1⟧`` 这样的标记，
翻译后换回；标记没有原样回来的单元保留原文并计入需复核，保证这些片段一个字符不变。

段内格式（``TextUnit.marked``，``<g1>…</g1>``）与 Word 共用 ``core/word_inline_marks.py``：
``InlineMarkEngine`` 包住引擎，把带标记的原文发出去、收回带标记的译文；记忆库命中的、
仅译文模式下另走一路翻的、标记没完整回来的、体检改过的，定稿后用 ``place_marks`` 让模型只补
标记。补不上的 ``marked_translation`` 留空，适配器按整段写法写回。校验、记忆库和复核只看纯文本。

仅译文模式下，原文是中文时，已经夹着外文的内容（「土石方工程合计 Total of Earthworks」）
与 Excel、Word 同一套规则单独翻（``core/mixed_language.py``）：整体翻成目标语言、不用记忆库；
模型认为原文已有双语的再整体翻一遍，仍翻不出的保留原文、计入需复核；译文里仍夹着没翻的外文的
保留译文、计入需复核。双语对照模式不分流，照常翻译。

执行器不认识任何具体格式；暂停在文件之间和两批请求之间生效，停止时已拿到的译文照常写出。
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from core.bilingual_writer import bilingual_output_name
from core.document_profile import append_profile
from core.file_progress import FileProgressReporter, file_progress_id
from core.format_adapter import TARGET_LANG_OPTION_KEY, AdapterError, AdapterHost
from core.language_preflight import AUTO_SOURCE_LANG, preflight_file_source_languages
from core.language_registry import get_source_lang_display, get_target_lang_display
from core.mixed_language import (
    DEFAULT_MIXED_MAX_BATCH_CHARS,
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    MIXED_ACTION_UNCERTAIN,
    MixedLanguageResult,
    MixedLanguageRunStats,
    collapse_echoed_repeat,
    split_mixed_language_sources,
    translate_mixed_language_texts,
)
from core.request_log import BatchRequestLog
from core.request_progress import BATCH_EVENT_DONE
from core.residual_pipeline import run_residual_pass
from core.runner_base import BaseTaskRunner
from core.task_runner import user_facing_reason
from core.task_runner_messages import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StoppedMsg
from core.translation_coverage import looks_like_source_text
from core.unit_ledger import (
    STATE_CLEAN,
    STATE_NEEDS_REVIEW,
    STATE_SKIPPED,
    UnitLedger,
)
from core.word_inline_marks import PLACE_MARKS_BATCH, InlineMarkEngine, place_marks, strip_marks
from engines.base_engine import engine_supports_chat
from translator_adapter import (
    MODE_BILINGUAL,
    MODE_TRANSLATION_ONLY,
    STATUS_LOCKED,
    STATUS_MEMORY,
    STATUS_NEEDS_REVIEW,
    STATUS_TRANSLATED,
    STATUS_UNTRANSLATED,
    OutputFile,
    TextUnit,
    TranslatedUnit,
)

#: 需复核清单的文件名（放在任务输出目录里）。
REVIEW_LIST_NAME = "需复核清单.txt"
#: How many review entries the task result carries; the list file has them all.
REVIEW_ITEMS_IN_RESULT = 200

#: 复核原因（写进需复核清单和台账证据链）。
REASON_UNTRANSLATED = "未获得译文"
REASON_RESIDUAL = "译文中仍有原文"
REASON_PROTECTED = "译文未原样保留代码或链接等内容，已保留原文"
REASON_TERM = "未采用设定译法"
# 仅译文模式下夹杂多种语言的内容（与 Excel、Word 同一套判据，见模块说明）。
REASON_MIXED_KEPT = "已含两种语言的内容未能翻成目标语言"
REASON_MIXED_LEFTOVER = "译文中仍夹带未翻译的外文"
REASON_MIXED_NOISE = "原文疑似夹杂错误外文"
REASON_MIXED_UNCERTAIN = "夹杂多种语言的内容未能确认"

#: 停止时还没送出的单元（不是失败，不进需复核清单，计入「部分翻译」）。
_UNFINISHED = "停止时尚未翻译"

_TOKEN_RE = re.compile(r"⟦\s*(\d+)\s*⟧")
_PROTECT_HINT = "形如 ⟦1⟧ 的标记代表不需要翻译的内容，必须原样保留在译文中对应的位置。"

# 一个文件内各步的占比（读取 / 翻译 / 写回），整批进度按文件累加，不随文件回退。
_EXTRACT_SHARE = 0.1
_TRANSLATE_SHARE = 0.8
_PROGRESS_SCALE = 10000

_STAGE_READ = "读取文件"
_STAGE_TRANSLATE = "翻译"
_STAGE_WRITE = "生成文件"


# ── 受保护片段 ──────────────────────────────────────────────────────────────


def mask_protected(text: str, fragments: list[str]) -> tuple[str, list[str]]:
    """把 ``fragments`` 在 ``text`` 里的每一处换成 ``⟦i⟧``，返回（换好的文字, 第 i 处的原片段）。

    原文里本来就有 ``⟦`` ``⟧`` 时不做替换（标记会认错），由调用方事后核对片段是否保留。
    """
    if not fragments or "⟦" in text or "⟧" in text:
        return text, []
    pattern = re.compile("|".join(re.escape(item) for item in sorted(set(fragments), key=len, reverse=True)))
    found: list[str] = []

    def replace(match: re.Match[str]) -> str:
        found.append(match.group(0))
        return f"⟦{len(found)}⟧"

    return pattern.sub(replace, text), found


def unmask_protected(translation: str, found: list[str]) -> str | None:
    """把 ``⟦i⟧`` 换回原片段；有标记丢失、重复或多出来时返回 None。"""
    if not found:
        return translation
    seen = [int(match.group(1)) for match in _TOKEN_RE.finditer(translation)]
    if sorted(seen) != list(range(1, len(found) + 1)):
        return None
    return _TOKEN_RE.sub(lambda match: found[int(match.group(1)) - 1], translation)


# ── 执行器 ──────────────────────────────────────────────────────────────────


@dataclass
class _Plan:
    """一个文件的单元，以及每个要翻译的单元送去翻译的文字。"""

    units: list[TextUnit]
    #: 单元 id → 送去翻译的文字（受保护片段已换成标记）；不在这里的单元原样写回。
    sent: dict[str, str] = field(default_factory=dict)
    #: 单元 id → 第 i 个标记对应的原片段。
    masks: dict[str, list[str]] = field(default_factory=dict)
    #: 单元 id → 带段内格式标记、送去翻译的原文（受保护片段同样换成了 ``⟦i⟧``）。
    marked: dict[str, str] = field(default_factory=dict)
    #: 送去翻译的纯文本 → 带标记的版本（``InlineMarkEngine`` 按它换）；同一句话格式不同时取第一个。
    mark_sources: dict[str, str] = field(default_factory=dict)


@dataclass
class _Translated:
    """一个文件的翻译结果，都按送去翻译的文字记。"""

    #: → 译文（定稿，不带格式标记）。
    translations: dict[str, str] = field(default_factory=dict)
    #: → 来源：memory / model。
    origins: dict[str, str] = field(default_factory=dict)
    #: → 复核原因（有译文的保留译文，没有的保留原文）。
    reasons: dict[str, str] = field(default_factory=dict)
    #: 停止时还没翻的。
    unfinished: set[str] = field(default_factory=set)
    #: 包了段内格式标记的引擎（这个文件有带标记的单元时）；``marked_translations`` 是模型带回的标记。
    mark_engine: InlineMarkEngine | None = None


@dataclass
class _ReviewEntry:
    file: str
    unit: str
    source: str
    reason: str
    #: True when the output keeps the source text here, False when it keeps the translation.
    kept_source: bool = True


def _has_text(value: str) -> bool:
    return any(char.isalpha() for char in value)


def _same_text(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def _lang_base(code: str) -> str:
    return re.split(r"[-_]", str(code or "").strip().lower(), maxsplit=1)[0]


class AdapterTaskRunner(BaseTaskRunner):
    """按文件逐个处理：取字 → 识别语言 → 记忆库 + 模型翻译 → 体检 → 写回。"""

    _thread_name = "adapter-translation"

    def __init__(
        self,
        files: list[Any],
        *,
        host: AdapterHost,
        service: Any,
        source_root: Path,
        output_dir: Path,
        target_lang: str,
        source_lang: str = AUTO_SOURCE_LANG,
        bilingual: bool = False,
        options: Mapping[str, Any] | None = None,
        key_overrides: dict[str, str] | None = None,
        label: str = "",
    ) -> None:
        self._init_runner_shell()
        self._files = list(files)
        self._host = host
        self._service = service
        self._source_root = Path(source_root)
        self._output_dir = Path(output_dir)
        self._target_lang = target_lang
        self._source_lang = str(source_lang or AUTO_SOURCE_LANG)
        self._mode = MODE_BILINGUAL if bilingual else MODE_TRANSLATION_ONLY
        # 交给适配器的开关值，带上目标语言（写回时改语言属性、断行规则用）。
        self._options = {**dict(options or {}), TARGET_LANG_OPTION_KEY: target_lang}
        self._key_overrides = dict(key_overrides or {})
        self._label = label
        self._pause_event = threading.Event()
        self._file_progress = FileProgressReporter(self._queue, self._files, self._source_root)
        weights = [1.0 + self._size_kb(item) / 50 for item in self._files] or [1.0]
        total = sum(weights)
        self._file_shares = [weight / total for weight in weights]
        self._file_offsets = [sum(self._file_shares[:index]) for index in range(len(weights))]
        self._term_memory: Any = None
        self._api_calls = 0
        self._api_calls_lock = threading.Lock()
        self._tm_hits = 0
        self._review: list[_ReviewEntry] = []
        self._languages: dict[str, list[str]] = {}

    # ── 暂停 ─────────────────────────────────────────────────────────────

    def _before_start(self) -> None:
        self._pause_event.clear()

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def _wait_if_paused(self) -> None:
        """暂停时在这里等：文件之间，或两批请求之间（批次回调里，不再发出新的一批）。"""
        while self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.05)

    # ── 进度 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _size_kb(item: Any) -> float:
        size = getattr(item, "size_kb", None)
        if isinstance(size, (int, float)):
            return float(size)
        try:
            return Path(item.path).stat().st_size / 1024
        except OSError:
            return 0.0

    def _progress(self, index: int, fraction: float, stage: str, activity: str) -> None:
        """整批进度：前面的文件 + 当前文件的 ``fraction``（phase_total = 0，界面直接用比例）。"""
        share = self._file_offsets[index] + self._file_shares[index] * max(0.0, min(1.0, fraction))
        total = len(self._files)
        detail = f"{activity}：第 {index + 1} / {total} 个文件" if total > 1 else activity
        self._queue.put(
            ProgressMsg(
                0,
                0,
                f"{stage} · {detail}",
                int(round(min(1.0, share) * _PROGRESS_SCALE)),
                _PROGRESS_SCALE,
            )
        )

    def _count_requests(self, event: Any) -> None:
        if getattr(event, "kind", "") == BATCH_EVENT_DONE:
            with self._api_calls_lock:
                self._api_calls += 1

    # ── 主流程 ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        started = time.monotonic()
        results: list[dict[str, Any]] = []
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            self._file_progress.emit_many("waiting", "extract")
            self._term_memory = self._service.term_memory(log=self._log)
            for index, item in enumerate(self._files):
                self._wait_if_paused()
                if self._stop_event.is_set():
                    self._stopped(results, index)
                    return
                results.append(self._process_file(index, item))
                if self._stop_event.is_set():
                    self._stopped(results, index + 1)
                    return
        except Exception as exc:  # noqa: BLE001 - a crash ends the task with a sentence
            logger.opt(exception=exc).error(f"[{self._label or '扩展'}] 任务出错：{exc!r}")
            message = user_facing_reason(exc, fallback="出现意外问题，任务未能完成。")
            self._log("ERROR", message)
            self._queue.put(
                ErrorMsg(
                    message=message,
                    output_dir=str(self._output_dir),
                    files=results,
                    review=self._review_summary(),
                    terms=self._finish_terms(),
                )
            )
            return
        review = self._review_summary()
        if results and not any(result.get("success") for result in results):
            self._queue.put(
                ErrorMsg(
                    message=str(results[0].get("error") or "所有文件均未能生成。"),
                    output_dir=str(self._output_dir),
                    files=results,
                    review=review,
                    terms=self._finish_terms(),
                )
            )
            return
        self._queue.put(
            DoneMsg(
                output_dir=str(self._output_dir),
                file_results=results,
                elapsed_sec=round(time.monotonic() - started, 2),
                tm_hit_count=self._tm_hits,
                api_call_count=self._api_calls,
                files=results,
                review=review,
                terms=self._finish_terms(),
                language={"detected": dict(self._languages)} if self._languages else {},
            )
        )

    def _stopped(self, results: list[dict[str, Any]], next_index: int) -> None:
        for item in self._files[next_index:]:
            results.append(self._file_result(item, status="unstarted", success=False))
        self._file_progress.emit_many("unstarted", "not_started")
        self._log("WARN", "任务已停止")
        self._queue.put(
            StoppedMsg(
                message="已停止",
                output_dir=str(self._output_dir),
                files=results,
                review=self._review_summary(),
                terms=self._finish_terms(),
            )
        )

    def _file_result(self, item: Any, *, status: str, success: bool, **extra: Any) -> dict[str, Any]:
        path = Path(item.path)
        return {
            "name": path.name,
            "file_id": file_progress_id(path),
            "output_path": "",
            "success": success,
            "status": status,
            "review_count": 0,
            **extra,
        }

    def _relative(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self._source_root.resolve())
        except ValueError:
            return Path(path.name)

    def _output_path(self, path: Path) -> Path:
        relative = self._relative(path)
        name = bilingual_output_name(
            path.name,
            get_target_lang_display(self._target_lang) or self._target_lang,
            translation_only=self._mode == MODE_TRANSLATION_ONLY,
        )
        return self._output_dir / relative.parent / name

    # ── 一个文件 ─────────────────────────────────────────────────────────

    def _process_file(self, index: int, item: Any) -> dict[str, Any]:
        path = Path(item.path)
        name = path.name
        self._file_progress.emit(path, "active", "extract")
        self._progress(index, 0.0, _STAGE_READ, "正在读取")
        try:
            units = self._host.extract(path, self._options)
        except AdapterError as exc:
            return self._failed(item, exc, fallback="无法读取此文件。")
        plan = self._plan(units)
        self._log("INFO", f"{name}：共 {len(units)} 段，需要翻译 {len(plan.sent)} 段")
        self._progress(index, _EXTRACT_SHARE, _STAGE_READ, "读取完成")

        translated = self._translate(index, item, plan)
        translations, origins, reasons, unfinished = (
            translated.translations,
            translated.origins,
            translated.reasons,
            translated.unfinished,
        )

        ledger = UnitLedger()
        written: list[TranslatedUnit] = []
        review_count = 0
        for unit in units:
            ledger.register(unit.id, source_text=unit.text, source_anchor=unit.context or unit.id)
            sent = plan.sent.get(unit.id)
            if sent is None:
                ledger.set_state(unit.id, STATE_SKIPPED, skip_reason="locked" if unit.locked else "no_text")
                written.append(TranslatedUnit(unit.id, unit.text, unit.text, STATUS_LOCKED))
                continue
            value = translations.get(sent)
            if value is not None and plan.masks.get(unit.id):
                value = unmask_protected(value, plan.masks[unit.id])
                if value is None:
                    reasons[sent] = REASON_PROTECTED
            elif value is not None and unit.protected and not plan.masks.get(unit.id):
                # 原文里有 ⟦ ⟧ 没法换标记：事后核对片段是否都在。
                if any(fragment not in value for fragment in unit.protected):
                    value = None
                    reasons[sent] = REASON_PROTECTED
            if value is None:
                if sent in unfinished:
                    ledger.set_state(unit.id, STATE_SKIPPED, skip_reason="stopped")
                    written.append(TranslatedUnit(unit.id, unit.text, unit.text, STATUS_UNTRANSLATED))
                    continue
                reason = reasons.get(sent) or REASON_UNTRANSLATED
                ledger.set_state(unit.id, STATE_NEEDS_REVIEW, evidence=reason)
                written.append(TranslatedUnit(unit.id, unit.text, unit.text, STATUS_UNTRANSLATED))
                self._review.append(_ReviewEntry(str(self._relative(path)), self._unit_label(unit), unit.text, reason))
                review_count += 1
                continue
            reason = reasons.get(sent)
            if reason:
                ledger.set_state(unit.id, STATE_NEEDS_REVIEW, evidence=reason)
                written.append(TranslatedUnit(unit.id, unit.text, value, STATUS_NEEDS_REVIEW))
                self._review.append(
                    _ReviewEntry(str(self._relative(path)), self._unit_label(unit), unit.text, reason, kept_source=False)
                )
                review_count += 1
                continue
            ledger.set_state(unit.id, STATE_CLEAN, evidence=origins.get(sent, ""))
            status = STATUS_MEMORY if origins.get(sent) == "memory" else STATUS_TRANSLATED
            written.append(TranslatedUnit(unit.id, unit.text, value, status))
        assert not ledger.unresolved(), "every unit must have a final state"
        self._attach_marks(plan, written, translated)

        if self._stop_event.is_set() and not any(unit.changed for unit in written):
            # 一处译文都没拿到就停了：不写一份和原文一样的文件。
            self._forget_review(path)
            self._file_progress.emit(path, "stopped", "not_started", force=True)
            return self._file_result(item, status="stopped", success=False)

        self._file_progress.emit(path, "active", "generate")
        self._progress(index, _EXTRACT_SHARE + _TRANSLATE_SHARE, _STAGE_WRITE, "正在写出")
        out_path = self._output_path(path)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            output = self._host.write(path, written, self._mode, out_path, self._options)
        except (AdapterError, OSError) as exc:
            self._forget_review(path)
            return self._failed(item, exc, fallback="无法写出此文件。")
        review_count += self._adapter_review(path, units, output)
        unfinished_count = sum(1 for record in ledger.records() if record.skip_reason == "stopped")
        result = self._file_result(
            item,
            status="needs_review" if review_count else "completed",
            success=True,
            output_path=str(output.path),
            review_count=review_count,
            unit_count=len(units),
            translated_count=sum(1 for unit in written if unit.status == STATUS_TRANSLATED),
            memory_count=sum(1 for unit in written if unit.status == STATUS_MEMORY),
            unfinished_text_count=unfinished_count,
            ledger=ledger.counts(),
        )
        self._file_progress.emit(path, "generated", "done", result=result)
        self._progress(index, 1.0, _STAGE_WRITE, "已写出")
        if review_count:
            self._log("WARN", f"{name}：已写出，{review_count} 段需复核")
        elif unfinished_count:
            self._log("WARN", f"{name}：已写出已翻译的部分，{unfinished_count} 段停止时尚未翻译，保留原文")
        else:
            self._log("OK", f"{name}：已写出")
        return result

    def _adapter_review(self, path: Path, units: list[TextUnit], output: OutputFile) -> int:
        """``write`` 自己报的需复核：逐条进需复核清单（写进去的是译文）；返回条数。

        只给了 ``review_count`` 的旧适配器照旧只计数；两样都给时取大的。
        """
        by_id = {unit.id: unit for unit in units}
        for item in output.review_items:
            unit = by_id.get(item.id)
            where = item.context or (self._unit_label(unit) if unit is not None else "")
            self._review.append(
                _ReviewEntry(
                    str(self._relative(path)),
                    where,
                    unit.text if unit is not None else "",
                    item.reason,
                    kept_source=False,
                )
            )
        return max(0, int(output.review_count), len(output.review_items))

    def _forget_review(self, path: Path) -> None:
        relative = str(self._relative(path))
        self._review = [entry for entry in self._review if entry.file != relative]

    def _failed(self, item: Any, exc: BaseException, *, fallback: str) -> dict[str, Any]:
        path = Path(item.path)
        # 适配器的原始报错（可能是英文、带路径）只进日志文件；界面上是一句人话。
        logger.warning(f"[{self._label or '扩展'}] {path.name}：{exc!r}")
        message = user_facing_reason(exc, fallback=fallback)
        self._log("ERROR", f"{path.name}：{message}")
        result = self._file_result(item, status="failed", success=False, error=message)
        self._file_progress.emit(path, "failed", "done", result=result, force=True)
        return result

    def _attach_marks(self, plan: _Plan, written: list[TranslatedUnit], translated: _Translated) -> None:
        """给带段内格式的单元填 ``marked_translation``。

        模型带回、去掉标记与定稿一字不差的直接用；其余（记忆库命中、另走一路翻的、标记没完整
        回来的、体检改过的）让模型只补标记（``place_marks``，与 Word 同一个），补不上的留空。
        保留原文的单元就是带标记的原文。
        """
        if not plan.marked:
            return
        by_id = {unit.id: unit for unit in plan.units}
        known = translated.mark_engine.marked_translations if translated.mark_engine is not None else {}
        missing: list[tuple[str, str, str]] = []
        for unit in written:
            marked_sent = plan.marked.get(unit.id)
            if marked_sent is None:
                continue
            source = by_id[unit.id]
            if unit.translation == unit.text:
                unit.marked_translation = source.marked
                continue
            entry = known.get(plan.sent[unit.id])
            candidate = None
            if entry is not None and entry[0] == marked_sent:
                masks = plan.masks.get(unit.id)
                candidate = unmask_protected(entry[1], masks) if masks else entry[1]
            if candidate is not None and strip_marks(candidate).strip() == unit.translation:
                unit.marked_translation = candidate.strip()
            else:
                missing.append((unit.id, source.marked, unit.translation))
        if not missing or self._stop_event.is_set():
            return
        engine = self._service.engine()
        if engine_supports_chat(engine):
            with self._api_calls_lock:
                self._api_calls += -(-len(missing) // PLACE_MARKS_BATCH)
        placed = place_marks(
            engine,
            missing,
            should_stop=self._stop_event.is_set,
            log=lambda message: logger.info(f"[{self._label or '扩展'}] {message}"),
        )
        for unit in written:
            if unit.id in placed:
                unit.marked_translation = placed[unit.id][1]
        logger.info(f"[{self._label or '扩展'}] 段内格式：补标记 {len(placed)}/{len(missing)} 处")

    @staticmethod
    def _unit_label(unit: TextUnit) -> str:
        # context 是适配器写给人看的位置（「第 3 行」）；没写才退回内部 id。
        return unit.context or unit.id

    def _plan(self, units: list[TextUnit]) -> _Plan:
        plan = _Plan(units=units)
        for unit in units:
            if unit.locked or not _has_text(unit.text):
                continue
            masked, found = mask_protected(unit.text, unit.protected)
            plan.sent[unit.id] = masked
            if found:
                plan.masks[unit.id] = found
            if unit.marked and strip_marks(unit.marked) == unit.text:
                # 受保护片段跨在格式标记两边时换不成同样的 ⟦i⟧，这一段就不带标记。
                marked, marked_found = mask_protected(unit.marked, unit.protected)
                if marked_found == found and strip_marks(marked) == masked:
                    plan.marked[unit.id] = marked
                    plan.mark_sources.setdefault(masked, marked)
        return plan

    def _lang_pairs(self, index: int, item: Any, texts: list[str]) -> list[str] | None:
        """源语言为自动识别时：这个文件一次语言预检，返回记忆库要查的语言对。"""
        if self._source_lang != AUTO_SOURCE_LANG or not texts:
            return None
        engine = self._service.engine()
        if not engine_supports_chat(engine):
            return None
        path = Path(item.path)
        self._file_progress.emit(path, "active", "detect")
        self._progress(index, _EXTRACT_SHARE, _STAGE_TRANSLATE, "正在识别原文语言")
        result = preflight_file_source_languages(
            texts, target_lang=self._target_lang, request=lambda system, user: engine.chat(system, user)
        )
        if result.requested:
            with self._api_calls_lock:
                self._api_calls += 1
        if result.source_langs:
            self._languages[path.name] = list(result.source_langs)
            self._log(
                "INFO",
                f"{path.name}：已识别原文语言：" + "、".join(get_source_lang_display(code) for code in result.source_langs),
            )
            pairs = list(result.tm_lang_pairs(self._target_lang))
            return pairs or None
        if result.error:
            self._log("WARN", f"{path.name}：未能识别原文语言，记忆库按常见原文语言查找")
        return None

    def _translate(self, index: int, item: Any, plan: _Plan) -> _Translated:
        """记忆库 + 模型翻译 + 体检，结果按送去翻译的文字记。"""
        path = Path(item.path)
        texts = list(dict.fromkeys(plan.sent.values()))
        result = _Translated()
        translations, origins, reasons = result.translations, result.origins, result.reasons
        if not texts:
            return result
        service = self._service
        lang_pairs = self._lang_pairs(index, item, texts)

        profile = ""
        if service.smart_mode and not self._stop_event.is_set():
            self._progress(index, _EXTRACT_SHARE, _STAGE_TRANSLATE, "正在通读文档，概括文档说明")
            profile = str(service.document_profile(texts, log=self._log) or "").strip()
            with self._api_calls_lock:
                self._api_calls += 1

        # 仅译文模式下夹杂多种语言的内容单独翻，不用记忆库（与 Excel、Word 相同）。
        mixed = self._mixed_texts(path, texts)
        if mixed:
            self._log("INFO", f"有 {len(mixed)} 处内容夹杂多种语言，会单独翻译，不使用记忆库。")
        mixed_set = set(mixed)
        normal_texts = [text for text in texts if text not in mixed_set]

        self._file_progress.emit(path, "active", "translate", completed=0, total=len(texts))
        session = None
        if self._term_memory is not None:
            session = self._term_memory.open(
                normal_texts,
                profile=profile,
                file_path=path,
                location=path.name,
                lang_pairs=lang_pairs,
                log=self._log,
            )
            for text, value in session.hits.items():
                if value and text in normal_texts:
                    translations[text] = value
                    origins[text] = "memory"
        if translations:
            self._tm_hits += len(translations)
            self._log("INFO", f"{path.name}：记忆库已有译文 {len(translations)} 段")

        remaining = [text for text in normal_texts if text not in translations]
        already = len(translations)
        echoed: set[str] = set()

        def report_from(offset: int):
            def report(done: int, total: int) -> None:
                completed = min(len(texts), already + offset + done)
                self._file_progress.emit(path, "active", "translate", completed=completed, total=len(texts))
                fraction = completed / max(1, len(texts))
                self._progress(index, _EXTRACT_SHARE + _TRANSLATE_SHARE * fraction, _STAGE_TRANSLATE, "正在翻译")
                # 批次回调跑在提交批次的线程里：在这里等着就不会再发出新的一批请求。
                self._wait_if_paused()

            return report

        def on_error(message: str) -> None:
            self._log("ERROR" if "未能翻译" in message else "WARN", f"{path.name}：{message}")

        # 带段内格式的原文换成带标记的版本发出；没有这样的单元时不包。
        if plan.mark_sources:
            result.mark_engine = InlineMarkEngine(service.engine(), plan.mark_sources)
        prompt = service.system_prompt()
        if plan.masks:
            prompt = f"{prompt}\n{_PROTECT_HINT}"
        request_log = BatchRequestLog(self._log, unit="段", subject="内容", thread_name="adapter-translate-wait-log")

        def on_request(event: Any) -> None:
            request_log.on_event(event)
            self._count_requests(event)

        def model_translate(batch: list[str], offset: int) -> dict[str, str]:
            return service.translate(
                batch,
                prompt=prompt,
                context=profile,
                term_session=session,
                progress=report_from(offset),
                should_stop=self._stop_event.is_set,
                error_callback=on_error,
                request_callback=on_request,
                engine=result.mark_engine,
            )

        with request_log:
            if remaining and not self._stop_event.is_set():
                model = model_translate(remaining, 0)
                for text in remaining:
                    value = str(model.get(text) or "").strip()
                    if not value:
                        continue
                    if self._echoes_source(path, text, value):
                        echoed.add(text)
                        continue
                    translations[text] = value
                    origins[text] = "model"
                if echoed:
                    self._log("WARN", f"{path.name}：{len(echoed)} 段模型返回的译文与原文相同，已保留原文并标记需复核")
            if mixed and not self._stop_event.is_set():
                self._translate_mixed(
                    mixed,
                    result,
                    prompt=append_profile(prompt, profile),
                    progress=report_from(len(remaining)),
                    on_error=on_error,
                    retranslate=lambda batch: model_translate(batch, len(remaining)),
                )
        unfinished = (
            {
                text
                for text in [*remaining, *mixed]
                if text not in translations and text not in echoed and text not in reasons
            }
            if self._stop_event.is_set()
            else set()
        )
        result.unfinished = unfinished

        # 记忆库：未采用「始终采用」词条设定译法的（已重译仍不符）保留译文、计入需复核；
        # 「自动识别场景」下模型新译的短词自动保存。夹杂多种语言的不存。
        if session is not None:
            for text in session.not_applied_texts():
                if text in translations:
                    detail = str(session.not_applied_detail(text) or "").strip()
                    reasons[text] = f"{REASON_TERM}：{detail}" if detail else REASON_TERM
            self._term_memory.close(
                session,
                {text: translations[text] for text in remaining if text in translations},
                counts=Counter(text for text in plan.sent.values() if text not in mixed_set),
            )

        # 残留体检：序号前缀之类能确定修好的直接改，改不了的保留译文、计入需复核。
        residual = run_residual_pass(
            [(text, translations[text]) for text in texts if text in translations],
            target_lang=self._target_lang,
        )
        translations.update(residual.fixes)
        for report_item in residual.needs_review:
            reasons.setdefault(report_item.source_text, REASON_RESIDUAL)
        if residual.auto_fixed:
            self._log("INFO", f"{path.name}：残留序号已自动修复 {len(residual.auto_fixed)} 段")
        return result

    # ── 仅译文：夹杂多种语言的内容 ─────────────────────────────────────────

    def _mixed_texts(self, path: Path, texts: list[str]) -> list[str]:
        """仅译文模式下要单独翻的「中文 + 外文」内容。

        判据与 Excel、Word 相同（``split_mixed_language_sources``，只认原文为中文）；原文语言为
        「自动识别」时看这个文件的预检结果里有没有中文。双语对照模式、引擎不能按结构化 JSON
        回答时不分流。
        """
        if self._mode != MODE_TRANSLATION_ONLY or not texts:
            return []
        if self._source_lang != AUTO_SOURCE_LANG:
            candidates = [self._source_lang]
        else:
            candidates = self._languages.get(path.name) or []
        if not any(_lang_base(lang) == "zh" for lang in candidates):
            return []
        if not engine_supports_chat(self._service.engine()):
            return []
        _normal, mixed = split_mixed_language_sources(texts, target_lang=self._target_lang, source_lang="zh")
        return mixed

    def _translate_mixed(
        self,
        mixed: list[str],
        result: _Translated,
        *,
        prompt: str,
        progress: Any,
        on_error: Any,
        retranslate: Any,
    ) -> None:
        service = self._service
        stats = MixedLanguageRunStats()
        outcomes: dict[str, MixedLanguageResult] = translate_mixed_language_texts(
            mixed,
            engine=service.engine(),
            target_lang=self._target_lang,
            system_prompt=prompt,
            source_lang="zh",
            concurrency=service.rate.concurrency,
            max_items_per_batch=service.rate.batch_items,
            max_chars_per_batch=max(DEFAULT_MIXED_MAX_BATCH_CHARS, service.rate.batch_chars),
            retry_attempts=service.rate.item_retry,
            progress_callback=progress,
            error_callback=on_error,
            should_stop=self._stop_event.is_set,
            api_scheduler=service.api_scheduler,
            stats=stats,
            translation_only=True,
        )
        with self._api_calls_lock:
            self._api_calls += stats.mixed_batch_count + stats.retry_count
        stopped = self._stop_event.is_set()
        kept_bilingual: list[str] = []
        leftover = 0
        for text in mixed:
            outcome = outcomes.get(text)
            if outcome is None or (stopped and outcome.note in ("stopped", "missing_result")):
                continue  # 停止时没发出去：不算复核，计入「停止时尚未翻译」
            if outcome.action == MIXED_ACTION_EXISTING_BILINGUAL:
                kept_bilingual.append(text)
            elif outcome.action in (MIXED_ACTION_TRANSLATE, MIXED_ACTION_FOREIGN_NOISE) and outcome.translation.strip():
                result.translations[text] = outcome.translation.strip()
                result.origins[text] = "model"
                if outcome.action == MIXED_ACTION_FOREIGN_NOISE:
                    result.reasons[text] = REASON_MIXED_NOISE
                elif outcome.leftover:
                    result.reasons[text] = REASON_MIXED_LEFTOVER
                    leftover += 1
            else:
                result.reasons[text] = REASON_MIXED_UNCERTAIN
        if leftover:
            self._log("WARN", f"仅输出译文：{leftover} 处译文中仍夹带未翻译的外文，已标记需复核。")
        if kept_bilingual and not self._stop_event.is_set():
            # 输出只留译文时，原样保留「中文 + 外文」，用户看到的就是没翻（反馈 FB-20261013-UVW95F）：
            # 整段当普通内容再翻一遍。
            self._log("INFO", f"仅输出译文：{len(kept_bilingual)} 处已含两种语言的内容改为整体翻成目标语言。")
            again = retranslate(kept_bilingual)
            for text in kept_bilingual:
                value = collapse_echoed_repeat(str(again.get(text) or "").strip(), text)
                if value and not _same_text(value, text):
                    result.translations[text] = value
                    result.origins[text] = "model"
                elif value or not self._stop_event.is_set():
                    result.reasons[text] = REASON_MIXED_KEPT
        self._log(
            "INFO",
            f"夹杂多种语言的内容 {stats.input_count} 处："
            f"已有双语 {stats.action_counts.get(MIXED_ACTION_EXISTING_BILINGUAL, 0)} 处，"
            f"原文疑似有误 {stats.action_counts.get(MIXED_ACTION_FOREIGN_NOISE, 0)} 处，"
            f"待人工确认 {stats.action_counts.get(MIXED_ACTION_UNCERTAIN, 0)} 处，"
            f"复查后采用 {stats.semantic_accepted_count} 处",
        )

    def _echoes_source(self, path: Path, text: str, value: str) -> bool:
        """模型原样还回了原文（质量校验把译文退回原文时也是这样）：原文是要翻的源语言文字就不算译文。

        判据与 Excel、Word 的「未译源文」相同（``looks_like_source_text``）。中译英文档里夹的英文行
        原样回来是对的，不算。
        """
        if not _same_text(text, value):
            return False
        return any(
            looks_like_source_text(text, source_lang=lang, target_lang=self._target_lang)
            for lang in self._echo_source_langs(path)
        )

    def _echo_source_langs(self, path: Path) -> list[str]:
        """按哪种源语言判断：选了就是它；自动识别时用这个文件的预检结果，没有结果按默认源语言。

        与目标语言相同的不算（英译英表里的英文本来就不用翻）。
        """
        if self._source_lang != AUTO_SOURCE_LANG:
            candidates = [self._source_lang]
        else:
            candidates = self._languages.get(path.name) or [AUTO_SOURCE_LANG]
        target = _lang_base(self._target_lang)
        return [lang for lang in candidates if _lang_base(lang) != target]

    # ── 需复核清单 ───────────────────────────────────────────────────────

    def _review_summary(self) -> dict[str, object]:
        if not self._review:
            return {"total_count": 0}
        path = self._write_review_list()
        summary: dict[str, object] = {
            "total_count": len(self._review),
            # 任务中心「结果定位」表读这些键（ui/src/views/tasks.ts toReviewRow）；
            # 全部条目都在清单文件里，这里只带前一部分，任务记录不至于过大。
            "items": [
                {
                    "file": entry.file,
                    "location": entry.unit,
                    "excerpt": entry.source,
                    "issue": entry.reason,
                    "action": "已保留原文" if entry.kept_source else "已保留译文",
                    "severity": "warning",
                }
                for entry in self._review[:REVIEW_ITEMS_IN_RESULT]
            ],
        }
        if path is not None:
            # report_path 是任务记录脱敏时保留的产物路径键（api/task_manager.py）。
            summary["report_path"] = str(path)
        return summary

    def _write_review_list(self) -> Path | None:
        def cell(value: str) -> str:
            return " ".join(str(value).split())

        lines = [
            "需复核清单",
            f"{self._label or '翻译任务'} · 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"共 {len(self._review)} 处需复核。各列以制表符分隔：文件、单元、原文、原因。",
            "",
            "文件\t单元\t原文\t原因",
        ]
        lines.extend(
            "\t".join((cell(entry.file), cell(entry.unit), cell(entry.source), cell(entry.reason)))
            for entry in self._review
        )
        path = self._output_dir / REVIEW_LIST_NAME
        try:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        except OSError as exc:
            self._log("WARN", f"无法写出需复核清单：{exc}")
            return None
        self._queue.put(LogMsg(level="INFO", message=f"需复核的 {len(self._review)} 处已列入「{REVIEW_LIST_NAME}」"))
        return path


__all__ = [
    "REASON_PROTECTED",
    "REASON_RESIDUAL",
    "REASON_TERM",
    "REASON_UNTRANSLATED",
    "REVIEW_LIST_NAME",
    "AdapterTaskRunner",
    "mask_protected",
    "unmask_protected",
]
