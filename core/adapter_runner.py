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
有译文但体检发现仍有原文、或未采用设定译法的，保留译文并计入需复核。

``TextUnit.protected`` 里的片段（行内代码、链接地址）翻译前换成 ``⟦1⟧`` 这样的标记，
翻译后换回；标记没有原样回来的单元保留原文并计入需复核，保证这些片段一个字符不变。

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
from core.file_progress import FileProgressReporter, file_progress_id
from core.format_adapter import AdapterError, AdapterHost
from core.language_preflight import AUTO_SOURCE_LANG, preflight_file_source_languages
from core.language_registry import get_source_lang_display, get_target_lang_display
from core.request_log import BatchRequestLog
from core.request_progress import BATCH_EVENT_DONE
from core.residual_pipeline import run_residual_pass
from core.runner_base import BaseTaskRunner
from core.task_runner import user_facing_reason
from core.task_runner_messages import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StoppedMsg
from core.unit_ledger import (
    STATE_CLEAN,
    STATE_NEEDS_REVIEW,
    STATE_SKIPPED,
    UnitLedger,
)
from engines.base_engine import engine_supports_chat
from translator_adapter import (
    MODE_BILINGUAL,
    MODE_TRANSLATION_ONLY,
    STATUS_LOCKED,
    STATUS_MEMORY,
    STATUS_NEEDS_REVIEW,
    STATUS_TRANSLATED,
    STATUS_UNTRANSLATED,
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
        self._options = dict(options or {})
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
            units = self._host.extract(path)
        except AdapterError as exc:
            return self._failed(item, exc, fallback="无法读取此文件。")
        plan = self._plan(units)
        self._log("INFO", f"{name}：共 {len(units)} 段，需要翻译 {len(plan.sent)} 段")
        self._progress(index, _EXTRACT_SHARE, _STAGE_READ, "读取完成")

        translations, origins, reasons, unfinished = self._translate(index, item, plan)

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
        review_count += max(0, int(output.review_count))
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

    def _translate(
        self, index: int, item: Any, plan: _Plan
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str], set[str]]:
        """返回（送去翻译的文字 → 译文, → 来源 memory/model, → 复核原因, 停止时没翻的文字）。"""
        path = Path(item.path)
        texts = list(dict.fromkeys(plan.sent.values()))
        translations: dict[str, str] = {}
        origins: dict[str, str] = {}
        reasons: dict[str, str] = {}
        if not texts:
            return translations, origins, reasons, set()
        service = self._service
        lang_pairs = self._lang_pairs(index, item, texts)

        profile = ""
        if service.smart_mode and not self._stop_event.is_set():
            self._progress(index, _EXTRACT_SHARE, _STAGE_TRANSLATE, "正在通读文档，概括文档说明")
            profile = str(service.document_profile(texts, log=self._log) or "").strip()
            with self._api_calls_lock:
                self._api_calls += 1

        self._file_progress.emit(path, "active", "translate", completed=0, total=len(texts))
        session = None
        if self._term_memory is not None:
            session = self._term_memory.open(
                texts,
                profile=profile,
                file_path=path,
                location=path.name,
                lang_pairs=lang_pairs,
                log=self._log,
            )
            for text, value in session.hits.items():
                if value and text in texts:
                    translations[text] = value
                    origins[text] = "memory"
        if translations:
            self._tm_hits += len(translations)
            self._log("INFO", f"{path.name}：记忆库已有译文 {len(translations)} 段")

        remaining = [text for text in texts if text not in translations]
        already = len(translations)

        def report(done: int, total: int) -> None:
            fraction = (already + done) / max(1, len(texts))
            self._file_progress.emit(path, "active", "translate", completed=already + done, total=len(texts))
            self._progress(index, _EXTRACT_SHARE + _TRANSLATE_SHARE * fraction, _STAGE_TRANSLATE, "正在翻译")
            # 批次回调跑在提交批次的线程里：在这里等着就不会再发出新的一批请求。
            self._wait_if_paused()

        def on_error(message: str) -> None:
            self._log("ERROR" if "未能翻译" in message else "WARN", f"{path.name}：{message}")

        if remaining and not self._stop_event.is_set():
            request_log = BatchRequestLog(self._log, unit="段", subject="内容", thread_name="adapter-translate-wait-log")

            def on_request(event: Any) -> None:
                request_log.on_event(event)
                self._count_requests(event)

            prompt = service.system_prompt()
            if plan.masks:
                prompt = f"{prompt}\n{_PROTECT_HINT}"
            with request_log:
                model = service.translate(
                    remaining,
                    prompt=prompt,
                    context=profile,
                    term_session=session,
                    progress=report,
                    should_stop=self._stop_event.is_set,
                    error_callback=on_error,
                    request_callback=on_request,
                )
            for text in remaining:
                value = str(model.get(text) or "").strip()
                if value:
                    translations[text] = value
                    origins[text] = "model"
        unfinished = {text for text in remaining if text not in translations} if self._stop_event.is_set() else set()

        # 记忆库：未采用「始终采用」词条设定译法的（已重译仍不符）保留译文、计入需复核；
        # 「自动识别场景」下模型新译的短词自动保存。
        if session is not None:
            for text in session.not_applied_texts():
                if text in translations:
                    detail = str(session.not_applied_detail(text) or "").strip()
                    reasons[text] = f"{REASON_TERM}：{detail}" if detail else REASON_TERM
            self._term_memory.close(
                session,
                {text: translations[text] for text in remaining if text in translations},
                counts=Counter(plan.sent.values()),
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
        return translations, origins, reasons, unfinished

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
