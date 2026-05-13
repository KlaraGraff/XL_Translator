"""Background runner for Word translation tasks."""

from __future__ import annotations

import queue
import re
import threading
from datetime import datetime
from pathlib import Path

from loguru import logger

from core import tm_manager
from core.bilingual_writer import get_custom_output_dir_error
from core.engine_dispatcher import (
    build_engine,
    get_batch_size,
    get_system_prompt,
    translate_texts,
)
from core.language_registry import build_lang_pair
from core.task_logger import TaskLogger
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    TaskStopped,
)
from core.translation_protocol import should_store_translation_in_tm
from core.word_document import (
    WordFileItem,
    build_word_output_dir,
    extract_word_segments,
    write_bilingual_docx,
)
from settings import AppSettings

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class WordTaskRunner:
    """Run the Word translation pipeline on a background thread."""

    def __init__(
        self,
        file_items: list[WordFileItem],
        settings: AppSettings,
        source_root: Path | str | None = None,
        source_lang: str | None = None,
    ):
        self._files = file_items
        self._settings = settings
        self._source_root = Path(source_root) if source_root else None
        self._source_lang = str(source_lang or settings.source_lang or "zh").strip() or "zh"
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._task_logger = TaskLogger(enabled=settings.output.enable_task_log)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def needs_poll(self) -> bool:
        return self.is_running() or not self._queue.empty()

    def get_message(self, timeout: float = 0.05):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _log(self, level: str, message: str) -> None:
        self._queue.put(LogMsg(level=level, message=message))
        logger.info(f"[{level}] {message}")

    def _run(self) -> None:
        start_ts = datetime.now()
        settings = self._settings
        source_lang = self._source_lang
        target_lang = settings.target_lang
        lang_pair = build_lang_pair(target_lang, source_lang=source_lang)
        max_len = settings.tm.max_len
        tm_hit_count = 0
        api_call_count = 0
        file_results: list[dict] = []
        stopped_message: str | None = None

        try:
            engine = build_engine(settings)
            system_prompt = get_system_prompt(
                settings,
                target_lang=target_lang,
                source_lang=source_lang,
            )
            batch_size = get_batch_size(settings)
            concurrency = (
                settings.engine.ollama_concurrency
                if settings.engine.mode == "local"
                else settings.engine.concurrency
            )
        except Exception as exc:
            self._queue.put(ErrorMsg(message=f"引擎初始化失败：{exc}"))
            return

        if not self._files:
            self._queue.put(ErrorMsg(message="未选择可翻译的 Word 文件。"))
            return

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        custom_output_dir = settings.output.custom_output_dir if settings.output.use_custom_output_dir else None
        try:
            if settings.output.use_custom_output_dir:
                output_error = get_custom_output_dir_error(custom_output_dir)
                if output_error is not None:
                    raise ValueError(output_error)
            output_dir = build_word_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._queue.put(ErrorMsg(message=f"输出目录初始化失败：{exc}"))
            return

        def _raise_if_stopped(message: str = "任务已中止") -> None:
            if self._stop_event.is_set():
                raise TaskStopped(message)

        self._task_logger.task_start(
            files=self._files,
            engine_name=engine.engine_name,
            target_lang=target_lang,
            keep_original_sheets=False,
            formula_display_value_backfill=False,
            enable_excel_autofit=False,
            lock_row_height=False,
        )
        self._log("INFO", f"[诊断] source_root={self._source_root} | custom_output_dir={custom_output_dir} | output_dir={output_dir}")
        self._log("INFO", f"扫描到 {len(self._files)} 个 Word 文件")

        phase_total = 3
        file_texts: list[set[str]] = []
        global_unique_texts: set[str] = set()

        try:
            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 1/{phase_total}] 正在提取 Word 文本..."))
            t_phase1 = datetime.now()
            for index, file_item in enumerate(self._files):
                _raise_if_stopped()
                self._queue.put(
                    ProgressMsg(
                        phase_index=1,
                        phase_total=phase_total,
                        phase_name="Word 提取",
                        step_done=index,
                        step_total=len(self._files),
                    )
                )
                self._log("INFO", f"[阶段 1] 提取文本：{file_item.name}（{index + 1}/{len(self._files)}）")
                try:
                    t0 = datetime.now()
                    segments = extract_word_segments(
                        file_item.path,
                        target_lang=target_lang,
                        source_lang=source_lang,
                    )
                    text_set = {segment.source for segment in segments}
                    file_texts.append(text_set)
                    global_unique_texts.update(text_set)
                    elapsed = (datetime.now() - t0).total_seconds()
                    self._log("INFO", f"  → {file_item.name}：{len(text_set)} 个词条（{elapsed:.3f}s）")
                    self._task_logger.file_collected(file_item.name, len(text_set), elapsed)
                except Exception as exc:
                    file_texts.append(set())
                    self._log("ERROR", f"Word 文件读取失败 {file_item.name}: {exc}")
                    self._task_logger.file_error(file_item.name, f"Word 文件读取失败: {exc}")
                    file_results.append(
                        {
                            "name": file_item.name,
                            "success": False,
                            "error": f"Word 文件读取失败: {exc}",
                        }
                    )

            self._queue.put(
                ProgressMsg(
                    phase_index=1,
                    phase_total=phase_total,
                    phase_name="Word 提取",
                    step_done=len(self._files),
                    step_total=len(self._files),
                )
            )
            phase1_elapsed = (datetime.now() - t_phase1).total_seconds()
            self._log("OK", f"[阶段 1 完成] Word 去重词汇池：{len(global_unique_texts)} 个唯一词条（{phase1_elapsed:.2f}s）")
            self._task_logger.global_collected(
                total_unique=len(global_unique_texts),
                file_count=len(self._files),
                elapsed=phase1_elapsed,
            )

            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在比对翻译记忆库..."))
            t_phase2 = datetime.now()
            all_texts = list(global_unique_texts)
            tm_result = tm_manager.lookup_batch(all_texts, lang_pair)
            hits = {text: value for text, value in tm_result.items() if value is not None}
            misses = [text for text, value in tm_result.items() if value is None]
            tm_hit_count = len(hits)
            api_call_count = len(misses)
            self._log("INFO", f"[阶段 2] TM 命中：{tm_hit_count}  待 API：{api_call_count}")
            self._task_logger.global_tm_result(hits=tm_hit_count, misses=api_call_count)
            self._queue.put(
                ProgressMsg(
                    phase_index=2,
                    phase_total=phase_total,
                    phase_name="云端翻译",
                    step_done=0 if api_call_count else 1,
                    step_total=max(api_call_count, 1),
                )
            )

            api_translations: dict[str, str] = {}
            if misses and not self._stop_event.is_set():
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在请求大模型翻译未命中词汇..."))

                def progress_cb(done, total):
                    self._queue.put(
                        ProgressMsg(
                            phase_index=2,
                            phase_total=phase_total,
                            phase_name="云端翻译",
                            step_done=done,
                            step_total=total,
                        )
                    )

                t0 = datetime.now()
                api_translations = translate_texts(
                    misses,
                    engine,
                    target_lang,
                    system_prompt,
                    batch_size,
                    concurrency,
                    progress_cb,
                    lambda msg: self._log("ERROR", msg),
                    should_stop=self.stop_requested,
                    source_lang=source_lang,
                )
                _raise_if_stopped("任务已中止，未写入剩余 Word 翻译结果。")

                retry_sources = [
                    source
                    for source in misses
                    if _needs_word_translation_retry(
                        source,
                        api_translations.get(source),
                        source_lang=source_lang,
                    )
                ]
                if retry_sources:
                    self._log(
                        "WARN",
                        (
                            f"检测到 {len(retry_sources)} 条 Word 段落未获得有效译文，"
                            "正在改为单条严格重试..."
                        ),
                    )
                    retry_prompt = _build_word_retry_prompt(system_prompt)
                    retry_translations = translate_texts(
                        retry_sources,
                        engine,
                        target_lang,
                        retry_prompt,
                        batch_size=1,
                        concurrency=1,
                        progress_callback=None,
                        error_callback=lambda msg: self._log("ERROR", msg),
                        should_stop=self.stop_requested,
                        source_lang=source_lang,
                    )
                    _raise_if_stopped("任务已中止，未写入剩余 Word 翻译结果。")
                    api_translations.update(retry_translations)

                    unresolved_sources = [
                        source
                        for source in retry_sources
                        if _needs_word_translation_retry(
                            source,
                            api_translations.get(source),
                            source_lang=source_lang,
                        )
                    ]
                    if unresolved_sources:
                        self._log(
                            "ERROR",
                            (
                                f"仍有 {len(unresolved_sources)} 条 Word 段落没有有效译文，"
                                "本次将保留原文并在结果中体现为未插入译文。"
                            ),
                        )

                elapsed = (datetime.now() - t0).total_seconds()
                self._log("OK", f"API 翻译完成，返回 {len(api_translations)} 条（{elapsed:.2f}s）")
                self._task_logger.global_api_done(returned=len(api_translations), elapsed=elapsed)

                new_pairs = [
                    (source, translated)
                    for source, translated in api_translations.items()
                    if should_store_translation_in_tm(source, translated)
                ]
                written = tm_manager.insert_batch(new_pairs, lang_pair, max_len, engine.engine_name)
                if written:
                    self._log("INFO", f"新增 TM 词条：{written} 条")

            global_translations = {**api_translations, **hits}
            phase2_elapsed = (datetime.now() - t_phase2).total_seconds()
            self._log("OK", f"[阶段 2 完成] 翻译数据就绪（{phase2_elapsed:.2f}s）")

            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 3/{phase_total}] 正在生成双语 Word..."))
            self._queue.put(
                ProgressMsg(
                    phase_index=3,
                    phase_total=phase_total,
                    phase_name="生成文件",
                    step_done=0,
                    step_total=max(len(self._files), 1),
                )
            )

            t_phase3 = datetime.now()
            source_root = self._source_root if self._source_root else self._files[0].path.parent
            for index, file_item in enumerate(self._files):
                _raise_if_stopped()
                already_failed = any(
                    result["name"] == file_item.name and not result.get("success")
                    for result in file_results
                )
                if already_failed:
                    continue

                self._queue.put(
                    ProgressMsg(
                        phase_index=3,
                        phase_total=phase_total,
                        phase_name="生成文件",
                        step_done=index,
                        step_total=len(self._files),
                    )
                )
                self._log("INFO", f"[阶段 3] 写入 Word：{file_item.name}（{index + 1}/{len(self._files)}）")
                try:
                    rel_subdir = file_item.path.parent.relative_to(source_root)
                except ValueError:
                    rel_subdir = Path()

                try:
                    t0 = datetime.now()
                    out_path = write_bilingual_docx(
                        source_path=file_item.path,
                        output_dir=output_dir / rel_subdir,
                        translations=global_translations,
                        target_lang=target_lang,
                        source_lang=source_lang,
                        log_callback=lambda msg: self._log(
                            "OK" if msg.startswith("[OK]") else "INFO",
                            msg,
                        ),
                    )
                    elapsed = (datetime.now() - t0).total_seconds()
                    this_file_texts = file_texts[index]
                    this_tm = sum(1 for text in this_file_texts if text in hits)
                    this_api = sum(1 for text in this_file_texts if text in misses)
                    self._task_logger.file_done(
                        filename=file_item.name,
                        elapsed=elapsed,
                        tm_hits=this_tm,
                        api_calls=this_api,
                    )
                    file_results.append(
                        {
                            "name": file_item.name,
                            "output": str(out_path),
                            "success": True,
                        }
                    )
                    self._log("OK", f"文件完成：{file_item.name}（{elapsed:.2f}s）")
                except Exception as exc:
                    self._log("ERROR", f"Word 文件写入失败 {file_item.name}：{exc}")
                    self._task_logger.file_error(file_item.name, str(exc))
                    file_results.append(
                        {
                            "name": file_item.name,
                            "success": False,
                            "error": str(exc),
                        }
                    )

            self._queue.put(
                ProgressMsg(
                    phase_index=3,
                    phase_total=phase_total,
                    phase_name="生成文件",
                    step_done=len(self._files),
                    step_total=len(self._files),
                )
            )
            phase3_elapsed = (datetime.now() - t_phase3).total_seconds()
            self._log("OK", f"[阶段 3 完成] Word 文件写入完毕（{phase3_elapsed:.2f}s）")
        except TaskStopped as exc:
            stopped_message = str(exc)

        elapsed_sec = (datetime.now() - start_ts).total_seconds()
        self._task_logger.task_end(elapsed_sec=elapsed_sec, file_results=file_results)

        if stopped_message is not None:
            self._log("WARN", stopped_message)
            self._queue.put(StoppedMsg(message=stopped_message))
            return

        self._queue.put(
            DoneMsg(
                output_dir=str(output_dir),
                file_results=file_results,
                elapsed_sec=elapsed_sec,
                tm_hit_count=tm_hit_count,
                api_call_count=api_call_count,
            )
        )


def _needs_word_translation_retry(
    source: str,
    translated: str | None,
    *,
    source_lang: str,
) -> bool:
    """Whether a Word source paragraph should be retried before writing."""
    source_text = str(source or "").strip()
    if not source_text:
        return False
    if source_lang == "zh" and not _CJK_RE.search(source_text):
        return False

    translated_text = str(translated or "").strip()
    if not translated_text:
        return True
    return translated_text.casefold() == source_text.casefold()


def _build_word_retry_prompt(system_prompt: str) -> str:
    return (
        f"{system_prompt}\n\n"
        "Word 文档段落重试规则：\n"
        "1. 当前输入是 Word 正文中的完整段落，不是 Excel 短单元格。\n"
        "2. 只要原文含中文，就必须返回目标语言译文，不能返回空字符串，也不能返回原文。\n"
        "3. 必须完整保留原文中的所有数字、负号、小数、单位、钢筋规格、强度等级和轴线编号。\n"
        "4. 不要省略任何参数；若句子很长，也要完整翻译整段。"
    ).strip()
