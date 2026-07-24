"""
后台任务线程管理器（全局聚合流水线架构）。

三阶段全局处理模型：
  阶段 1（收集）：依次扫描所有表格并提取词条，全局去重汇聚。
  阶段 2（翻译）：对去重词汇池统一查询 TM + 批量并发 API 翻译。
  阶段 3（写入）：翻译数据就绪后，逐文件串行回填写入。

优势：打破文件壁垒，最大化利用 API 批次并发能力；跨文件去重减少重复请求。
"""
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger

from core import bilingual_writer
from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from core.api_scheduler import API_REQUEST_CATEGORY_NORMAL, WeightedApiScheduler
from core.api_config_check import check_translation_api_config
from core.excel_coverage import build_excel_coverage_plan, write_untranslated_excel_file
from core.file_scanner import FileItem
from core.language_registry import (
    build_lang_pair,
    get_default_source_lang,
    get_tm_language_pairs,
    is_auto_source_lang,
)
from core.language_preflight import (
    LANGUAGE_PREFLIGHT_SYSTEM_PROMPT,
    build_language_preflight_prompt,
    preflight_files,
)
from core.mixed_language import (
    DEFAULT_MIXED_MAX_BATCH_CHARS,
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    MIXED_ACTION_UNCERTAIN,
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_SEMANTIC,
    MIXED_MARK_UNRESOLVED,
    MixedLanguageRunStats,
    split_mixed_language_sources,
    translate_mixed_language_texts,
)
from core.translation_filter import should_translate
from core.engine_dispatcher import (
    TranslationBatchRunStats,
    build_engine,
    get_system_prompt,
    translate_texts,
)
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from core.model_throughput import get_model_throughput
from core.translation_protocol import should_store_translation_in_tm
from core import tm_manager
from core.excel_automation import (
    create_excel_app,
    finalize_excel_thread,
    get_excel_process_pid,
    initialize_excel_thread,
    terminate_process_tree,
)
from core.task_logger import TaskLogger
from settings import AppSettings, provider_key_overrides

AUTOFIT_STALL_TIMEOUT_SECONDS = 180
AUTOFIT_MONITOR_POLL_SECONDS = 0.5
_EXCEL_REVIEW_MARK_PRIORITY = {
    MIXED_MARK_SEMANTIC: 10,
    MIXED_MARK_UNRESOLVED: 20,
    MIXED_MARK_FOREIGN_NOISE: 30,
}


# ── 消息类型 ──────────────────────────────────────────────────────────────────

@dataclass
class ProgressMsg:
    """全局进度消息。"""
    phase_index:  int      # 当前阶段序号（1/2/3/4）
    phase_total:  int      # 总阶段数（3）
    phase_name:   str      # 阶段名称（如"全局扫描"、"云端翻译"、"生成文件"）
    step_done:    int      # 当前阶段已完成步数
    step_total:   int      # 当前阶段总步数


@dataclass
class StatusMsg:
    phase_desc: str


@dataclass
class WordRecoveryStatusMsg:
    """Word recovery summary for the non-scrolling execution monitor."""
    retry_round: int = 0
    retry_total: int = 0
    retry_processing_count: int = 0
    retry_recovered_count: int = 0
    retry_unresolved_count: int = 0
    semantic_processing_count: int = 0
    semantic_checked_count: int = 0
    semantic_accepted_count: int = 0
    semantic_uncertain_count: int = 0


@dataclass
class PdfReviewStatusMsg:
    """PDF page-review summary for the non-scrolling execution monitor."""
    enabled: bool = False
    review_round: int = 0
    review_total: int = 0
    review_processing_count: int = 0
    review_passed_count: int = 0
    review_failed_count: int = 0


@dataclass
class PdfPageRecoveryStatusMsg:
    """PDF page retry/recovery summary for the non-scrolling execution monitor."""
    total_pages: int = 0
    completed_pages: int = 0
    submitted_page_count: int = 0
    pending_submitted_page_count: int = 0
    retrying_page_count: int = 0
    retried_page_count: int = 0
    recovered_page_count: int = 0
    placeholder_page_count: int = 0


@dataclass
class LogMsg:
    level:   str           # INFO / OK / WARN / ERROR
    message: str
    ts:      str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    visible: bool = True


@dataclass
class DoneMsg:
    output_dir:   str
    file_results: list[dict]
    elapsed_sec:  float
    tm_hit_count: int
    api_call_count: int
    issues: list[dict] = field(default_factory=list)
    report_path: str = ""


@dataclass
class ErrorMsg:
    message: str
    output_dir: str = ""
    report_path: str = ""
    manifest_path: str = ""


@dataclass
class StoppedMsg:
    message: str
    output_dir: str = ""
    report_path: str = ""
    manifest_path: str = ""


class TaskStopped(Exception):
    """后台任务收到停止信号时抛出，用于统一收尾。"""


def _set_excel_review_mark(review_marks: dict[str, str], source: str, mark: str) -> None:
    source_key = str(source or "").strip()
    if not source_key:
        return
    existing = review_marks.get(source_key)
    if existing is None or _EXCEL_REVIEW_MARK_PRIORITY.get(mark, 0) > _EXCEL_REVIEW_MARK_PRIORITY.get(existing, 0):
        review_marks[source_key] = mark


# ── TaskRunner ────────────────────────────────────────────────────────────────

class TaskRunner:
    """
    封装后台翻译任务（全局聚合流水线）。
    UI 层通过轮询 .get_message() 获取进度/日志/完成消息。
    """

    def __init__(
        self,
        file_items: list[FileItem],
        settings: AppSettings,
        source_root: Path | str | None = None,
        allow_xls_fallback: bool = False,
        source_lang: str | None = None,
        key_overrides: dict[str, str] | None = None,
        api_scheduler=None,
        untranslated_only: bool = False,
    ):
        self._files       = file_items
        self._settings    = settings
        self._source_root = Path(source_root) if source_root else None
        self._allow_xls_fallback = allow_xls_fallback
        self._source_lang = str(source_lang or settings.source_lang or "zh").strip() or "zh"
        self._key_overrides = dict(key_overrides or {})
        self._api_scheduler = api_scheduler
        self._untranslated_only = bool(untranslated_only)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._task_logger = TaskLogger(enabled=True)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_with_overrides, daemon=True)
        self._thread.start()

    @property
    def task_id(self) -> str:
        return self._task_logger.task_id

    def stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def needs_poll(self) -> bool:
        """线程存活 OR 队列中仍有待读消息（防止线程退出后 DoneMsg 被遗漏）。"""
        return self.is_running() or not self._queue.empty()

    def get_message(self, timeout: float = 0.05):
        """非阻塞获取消息；无消息时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _log(self, level: str, msg: str) -> None:
        self._queue.put(LogMsg(level=level, message=msg))
        logger.info(f"[{level}] {msg}")

    # ── 全局聚合流水线主入口 ──────────────────────────────────────────────

    def _run_with_overrides(self) -> None:
        with provider_key_overrides(self._key_overrides):
            self._run()

    def _run(self) -> None:
        start_ts     = datetime.now()
        settings     = self._settings
        source_lang  = self._source_lang
        auto_source_lang = is_auto_source_lang(source_lang)
        target_lang  = settings.target_lang
        # ``auto`` is a selector state only; it is resolved after each file's
        # one-shot preflight and never becomes a TM pair.
        lang_pair    = (
            None
            if auto_source_lang
            else build_lang_pair(target_lang, source_lang=source_lang)
        )
        max_len      = settings.tm.max_len
        tm_hit_count = 0
        api_call_count = 0
        file_results: list[dict] = []
        stopped_message: str | None = None
        fatal_error_message: str | None = None
        quality_issues: list[dict] = []

        try:
            config_check = check_translation_api_config(settings)
            if not config_check.ok:
                detail = f"（{config_check.detail}）" if config_check.detail else ""
                self._queue.put(ErrorMsg(message=f"{config_check.message}{detail}"))
                return
            engine        = build_engine(settings)
            system_prompt = get_system_prompt(
                settings,
                target_lang=target_lang,
                source_lang=source_lang if not auto_source_lang else get_default_source_lang(),
                page_key="excel",
            )
            model_config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            throughput = get_model_throughput(settings, model_config)
            batch_size = throughput.batch_size or 1
            concurrency = throughput.concurrency
        except Exception as e:
            self._queue.put(ErrorMsg(message=f"引擎初始化失败：{e}"))
            return

        def _raise_if_stopped(message: str = "任务已中止") -> None:
            if self._stop_event.is_set():
                raise TaskStopped(message)

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        custom_output_dir = self._settings.output.custom_output_dir if self._settings.output.use_custom_output_dir else None
        try:
            if self._settings.output.use_custom_output_dir:
                output_dir_error = bilingual_writer.get_custom_output_dir_error(custom_output_dir)
                if output_dir_error is not None:
                    raise ValueError(output_dir_error)

            output_dir = bilingual_writer.build_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._queue.put(ErrorMsg(message=f"输出目录初始化失败：{e}"))
            return

        # ── 任务日志：记录启动信息 ────────────────────────────────────
        self._task_logger.task_start(
            files                = self._files,
            engine_name          = engine.engine_name,
            target_lang          = target_lang,
            keep_original_sheets = settings.output.keep_original_sheets,
            formula_display_value_backfill = settings.output.formula_display_value_backfill,
            enable_excel_autofit = settings.output.enable_excel_autofit,
            lock_row_height      = settings.output.lock_row_height,
        )

        self._log("INFO", f"[诊断] source_root={self._source_root} | custom_output_dir={custom_output_dir} | output_dir={output_dir}")
        self._log("INFO", f"扫描到 {len(self._files)} 个文件")
        if self._untranslated_only:
            self._log("INFO", "[补译模式] 只补未译内容：已有译文位置将保持不变。")

        need_autofit = self._settings.output.enable_excel_autofit and not self._settings.output.lock_row_height
        phase_total = 4 if need_autofit else 3

        excel_app = None
        excel_thread_state = None
        excel_policy = "split"
        excel_policy_reason = "not_evaluated"
        reuse_excel_for_autofit = False
        xls_file_count = 0
        total_sheet_count = 0
        raw_text_count = 0
        def _configure_excel_app(app) -> None:
            """Tune Excel for unattended automation and large batch runs."""
            try:
                app.display_alerts = False
            except Exception:
                pass
            for attr, value in (
                ("screen_updating", False),
                ("enable_events", False),
                ("calculation", "manual"),
            ):
                try:
                    setattr(app, attr, value)
                except Exception:
                    continue

        def _kill_excel_pid(pid: int | None, reason: str) -> None:
            if not pid:
                return
            try:
                terminated = terminate_process_tree(pid, force=True)
                if terminated:
                    self._log("WARN", f"{reason}，已强制结束 Excel 进程 PID={pid}")
                else:
                    self._log("WARN", f"{reason}，但未能确认 Excel 进程已退出 PID={pid}")
            except Exception as e:
                self._log("WARN", f"{reason}，但强制结束 Excel 进程失败 PID={pid}: {e}")

        def _cleanup_excel_app(status_msg: str | None = None, *, force: bool = False) -> None:
            nonlocal excel_app, excel_thread_state
            if excel_app is None:
                return

            pid = get_excel_process_pid(excel_app)
            if status_msg:
                self._queue.put(StatusMsg(phase_desc=status_msg))

            try:
                if force:
                    excel_app.kill()
                else:
                    excel_app.quit()
            except Exception as e:
                self._log("WARN", f"Excel 进程清理异常: {e}")
                _kill_excel_pid(pid, "常规退出 Excel 失败")
            finally:
                excel_app = None
                finalize_excel_thread(excel_thread_state)
                excel_thread_state = None

        def _run_autofit_with_guard(file_paths: list[Path], progress_callback) -> bool:
            """Run AutoFit in a dedicated Excel process so it can be stopped safely."""
            worker_state = {
                "pid": None,
                "last_progress_ts": time.monotonic(),
                "error": None,
                "result": False,
            }
            done_event = threading.Event()

            def _worker():
                thread_state = None
                app = None
                try:
                    thread_state = initialize_excel_thread()
                    app = create_excel_app(visible=False, add_book=False)
                    _configure_excel_app(app)
                    worker_state["pid"] = get_excel_process_pid(app)

                    def _progress(done, total, current_file):
                        worker_state["last_progress_ts"] = time.monotonic()
                        progress_callback(done, total, current_file)

                    worker_state["result"] = bilingual_writer.autofit_files_batch(
                        file_paths,
                        app=app,
                        log_callback=lambda msg: self._log(
                            "WARN" if msg.startswith("[WARN]") else "INFO", msg
                        ),
                        progress_callback=_progress,
                    )
                except Exception as e:
                    worker_state["error"] = e
                finally:
                    if app is not None:
                        pid = get_excel_process_pid(app)
                        try:
                            app.quit()
                        except Exception:
                            _kill_excel_pid(pid, "AutoFit 线程退出时清理 Excel 失败")
                    finalize_excel_thread(thread_state)
                    done_event.set()

            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()

            while not done_event.wait(timeout=AUTOFIT_MONITOR_POLL_SECONDS):
                if self._stop_event.is_set():
                    self._task_logger.warning("收到停止请求，正在终止 Excel 精调。")
                    _kill_excel_pid(worker_state["pid"], "收到停止请求")
                    done_event.wait(timeout=5)
                    raise TaskStopped("任务已中止，Excel 精调已终止，已保留当前已生成结果。")

                stalled_for = time.monotonic() - worker_state["last_progress_ts"]
                if stalled_for >= AUTOFIT_STALL_TIMEOUT_SECONDS:
                    msg = (
                        f"Excel AutoFit 连续 {AUTOFIT_STALL_TIMEOUT_SECONDS}s 无进度，"
                        "已终止 Excel 精调并保留 Python 估算行高。"
                    )
                    self._log("WARN", msg)
                    self._task_logger.warning(msg)
                    _kill_excel_pid(worker_state["pid"], "Excel 精调长时间无进度")
                    done_event.wait(timeout=5)
                    return False

            if worker_state["error"] is not None:
                raise worker_state["error"]

            return bool(worker_state["result"])

        def _get_excel_app():
            nonlocal excel_app, excel_thread_state
            if excel_app is None:
                self._queue.put(StatusMsg(phase_desc="状态：正在唤醒底层 Excel 引擎，请稍候..."))
                try:
                    excel_thread_state = initialize_excel_thread()
                    self._log("INFO", "开始启动全局 Excel 进程...")
                    excel_app = create_excel_app(visible=False, add_book=False)
                    _configure_excel_app(excel_app)
                except Exception as e:
                    finalize_excel_thread(excel_thread_state)
                    excel_thread_state = None
                    self._log("WARN", f"启动全局 Excel 进程失败: {e}")
                    raise Exception(f"无法启动本地 Excel，请确认已正确安装并允许自动化控制: {e}")
            return excel_app

        try:
            _raise_if_stopped()
            # ══════════════════════════════════════════════════════════
            # 阶段 1：全局词汇提取（扫描 + .xls 转换 + 收集词条）
            # ══════════════════════════════════════════════════════════
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 1/{phase_total}] 正在扫描所有文件提取词汇..."))

            # process_paths[i] 对应 self._files[i]，若 .xls 则指向转换后的临时 .xlsx
            process_paths: list[Path] = []
            # file_texts[i] 对应 self._files[i] 的本文件词条集合
            file_texts: list[set[str]] = []
            coverage_plans = []
            global_unique_texts: set[str] = set()
            file_language_preflights = {}
            tm_language_pairs: list[str] = []
            text_source_candidates: dict[str, set[str]] = {}

            t_phase1 = datetime.now()

            for fi, file_item in enumerate(self._files):
                _raise_if_stopped()

                self._queue.put(ProgressMsg(
                    phase_index=1, phase_total=phase_total, phase_name="全局扫描",
                    step_done=fi, step_total=len(self._files),
                ))

                process_path = file_item.path
                source_is_xls = file_item.path.suffix.lower() == ".xls"

                if source_is_xls:
                    xls_file_count += 1

                # .xls 格式转换（在阶段 1 顺便完成）
                if source_is_xls:
                    self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 1/{phase_total}] 正在转换 .xls 文件：{file_item.name}"))
                    from core.xls_converter import (
                        XlwingsUnavailableError,
                        convert_with_excel,
                        convert_with_fallback,
                        is_excel_automation_permission_denied,
                    )
                    t_conv = datetime.now()
                    try:
                        if not self._allow_xls_fallback:
                            app = _get_excel_app()
                            try:
                                process_path = convert_with_excel(app, process_path)
                            except XlwingsUnavailableError as conversion_error:
                                if not is_excel_automation_permission_denied(conversion_error):
                                    raise
                                self._log(
                                    "WARN",
                                    "本机 Excel 自动化权限被 macOS 拒绝，已自动改用 .xls 兼容模式继续；"
                                    "复杂格式、图片或图表可能无法完整保留。可在系统设置的“隐私与安全性 > 自动化”"
                                    "中允许 Translator 控制 Microsoft Excel。",
                                )
                                process_path = convert_with_fallback(file_item.path)
                        else:
                            process_path = convert_with_fallback(process_path)
                        self._log("INFO", f"格式转换完成 {file_item.name}，耗时 {(datetime.now() - t_conv).total_seconds():.2f}s")
                    except Exception as e:
                        self._log("ERROR", f"源文件转换失败 {file_item.name}: {e}")
                        self._task_logger.file_error(file_item.name, str(e))
                        file_results.append({
                            "name": file_item.name,
                            "source_path": str(file_item.path),
                            "success": False,
                            "error": f"源文件转换失败: {e}",
                        })
                        process_paths.append(process_path)
                        file_texts.append(set())
                        coverage_plans.append(None)
                        continue

                process_paths.append(process_path)

                # 收集词条
                self._log("INFO", f"[阶段 1] 提取词汇：{file_item.name}（{fi+1}/{len(self._files)}）")
                t0 = datetime.now()
                try:
                    if self._untranslated_only:
                        coverage_plan = build_excel_coverage_plan(
                            process_path,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            formula_display_value_backfill=(
                                settings.output.formula_display_value_backfill
                            ),
                        )
                        coverage_plans.append(coverage_plan)
                        texts = coverage_plan.source_texts
                        sheet_count = coverage_plan.sheet_count
                        summary = coverage_plan.summary
                        self._log(
                            "INFO",
                            (
                                "  → 补译识别："
                                f"待补 {summary.get('source_only', 0)}，"
                                f"已覆盖 {summary.get('covered', 0)}，"
                                f"不确定跳过 {summary.get('ambiguous', 0)}"
                            ),
                        )
                    else:
                        texts, sheet_count = self._collect_texts(
                            process_path,
                            file_item.name,
                            target_lang=target_lang,
                            source_lang=source_lang,
                        )
                        coverage_plans.append(None)
                except Exception as e:
                    self._log("ERROR", f"源文件读取失败 {file_item.name}: {e}")
                    self._task_logger.file_error(file_item.name, f"源文件读取失败: {e}")
                    file_results.append({
                        "name": file_item.name,
                        "source_path": str(file_item.path),
                        "success": False,
                        "error": f"源文件读取失败: {e}",
                    })
                    if len(coverage_plans) < len(process_paths):
                        coverage_plans.append(None)
                    file_texts.append(set())
                    if process_path != file_item.path:
                        try:
                            os.remove(process_path)
                        except Exception as cleanup_error:
                            self._log("WARN", f"临时文件清理失败 {process_path.name}: {cleanup_error}")
                    continue
                text_set = set(texts)
                file_texts.append(text_set)
                total_sheet_count += sheet_count
                raw_text_count += len(text_set)
                collect_elapsed = (datetime.now() - t0).total_seconds()

                self._log("INFO", f"  → {file_item.name}：{len(text_set)} 个词条（{collect_elapsed:.3f}s）")
                self._task_logger.file_collected(file_item.name, len(text_set), collect_elapsed)

                global_unique_texts.update(text_set)

            # 阶段 1 收尾：发送最终进度
            self._queue.put(ProgressMsg(
                phase_index=1, phase_total=phase_total, phase_name="全局扫描",
                step_done=len(self._files), step_total=len(self._files),
            ))

            phase1_elapsed = (datetime.now() - t_phase1).total_seconds()
            self._log("OK", f"[阶段 1 完成] 全局去重词汇池：{len(global_unique_texts)} 个唯一词条（{phase1_elapsed:.2f}s）")
            self._task_logger.global_collected(
                total_unique=len(global_unique_texts),
                file_count=len(self._files),
                elapsed=phase1_elapsed,
            )

            if auto_source_lang:
                self._queue.put(
                    StatusMsg(
                        phase_desc=(
                            "状态：自动识别模式，正在对每个有候选文本的文件执行一次语言预检..."
                        )
                    )
                )

                detector_calls = {"count": 0}

                def _detect_file_language(samples, detected_target):
                    detector_calls["count"] += 1
                    raw = engine.chat(
                        LANGUAGE_PREFLIGHT_SYSTEM_PROMPT,
                        build_language_preflight_prompt(
                            samples,
                            target_lang=detected_target,
                        ),
                    )
                    return raw

                file_payloads = {
                    str(file_item.path): texts
                    for file_item, texts in zip(self._files, file_texts)
                }
                file_language_preflights = preflight_files(
                    file_payloads,
                    _detect_file_language,
                    target_lang=target_lang,
                )
                detected_sources: list[str] = []
                for result in file_language_preflights.values():
                    for detected in result.source_langs:
                        if detected not in detected_sources:
                            detected_sources.append(detected)
                if detected_sources:
                    source_lang = detected_sources[0]
                    system_prompt = get_system_prompt(
                        settings,
                        target_lang=target_lang,
                        source_lang=source_lang,
                        page_key="excel",
                    )
                else:
                    source_lang = get_default_source_lang()
                tm_language_pairs = get_tm_language_pairs(detected_sources, target_lang)
                for file_item, text_set in zip(self._files, file_texts):
                    result = file_language_preflights.get(str(file_item.path))
                    if result is None:
                        continue
                    for text in text_set:
                        text_source_candidates.setdefault(text, set()).update(result.source_langs)
                self._log(
                    "INFO",
                    (
                        f"自动语言预检完成：{detector_calls['count']} 次请求，"
                        f"实际源语言={','.join(detected_sources) or '未确定'}，"
                        f"TM 语言对={','.join(tm_language_pairs) or '无'}"
                    ),
                )
            else:
                tm_language_pairs = [lang_pair] if lang_pair else []

            excel_policy, excel_policy_reason = self._decide_excel_policy(
                need_autofit=need_autofit,
                xls_file_count=xls_file_count,
                total_sheet_count=total_sheet_count,
                raw_text_count=raw_text_count,
            )
            if excel_policy == "reuse" and excel_app is None:
                excel_policy = "split"
                excel_policy_reason = "no_reusable_excel_process"

            reuse_excel_for_autofit = (
                need_autofit
                and xls_file_count > 0
                and excel_policy == "reuse"
                and excel_app is not None
            )
            excel_policy_log = (
                f"excel_policy={excel_policy} | "
                f"xls_file_count={xls_file_count} | "
                f"total_sheet_count={total_sheet_count} | "
                f"raw_text_count={raw_text_count} | "
                f"reason={excel_policy_reason}"
            )
            self._log("INFO", f"[Excel策略] {excel_policy_log}")
            self._task_logger.excel_policy_decided(
                excel_policy=excel_policy,
                xls_file_count=xls_file_count,
                total_sheet_count=total_sheet_count,
                raw_text_count=raw_text_count,
                reason=excel_policy_reason,
            )

            if excel_app is not None and not reuse_excel_for_autofit:
                self._log("INFO", "按策略释放阶段 1 的 Excel 进程，阶段 4 将使用干净进程")
                _cleanup_excel_app(force=True)
            elif reuse_excel_for_autofit:
                self._log("INFO", "按策略保留阶段 1 的 Excel 进程，阶段 4 将直接复用")

            _raise_if_stopped()

            # ══════════════════════════════════════════════════════════
            # 阶段 2：全局统一翻译（TM 查询 + API 并发）
            # ══════════════════════════════════════════════════════════
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在比对翻译记忆库..."))

            t_phase2 = datetime.now()

            # 混合语言先分流，避免旧 TM 命中污染已双语/夹杂外文内容。
            all_texts_list = list(global_unique_texts)
            normal_texts, mixed_texts = split_mixed_language_sources(
                all_texts_list,
                target_lang=target_lang,
                source_lang=source_lang,
            )
            if mixed_texts:
                self._log(
                    "INFO",
                    f"混合语言路径命中 {len(mixed_texts)} 个词条，已从 TM 查询中分流。",
                )

            # TM 批量查询：自动模式只查询预检产生的真实语言对；同一
            # 原文在多个语言对中命中不同译文时留给模型，不做猜测复用。
            tm_values_by_text: dict[str, list[str]] = {
                text: [] for text in normal_texts
            }
            for pair in tm_language_pairs:
                tm_result = tm_manager.lookup_batch(normal_texts, pair)
                for text, value in tm_result.items():
                    if value is not None and str(value) not in tm_values_by_text.setdefault(text, []):
                        tm_values_by_text[text].append(str(value))
            hits = {
                text: values[0]
                for text, values in tm_values_by_text.items()
                if len(values) == 1
            }
            misses = [text for text in normal_texts if text not in hits]

            tm_hit_count   = len(hits)
            api_call_count = len(misses) + len(mixed_texts)

            self._log(
                "INFO",
                f"[阶段 2] TM 命中：{tm_hit_count}  普通待 API：{len(misses)}  混合语言：{len(mixed_texts)}",
            )
            self._task_logger.global_tm_result(hits=tm_hit_count, misses=api_call_count)
            self._queue.put(ProgressMsg(
                phase_index=2,
                phase_total=phase_total,
                phase_name="云端翻译",
                step_done=0 if api_call_count else 1,
                step_total=max(api_call_count, 1),
            ))

            # API 翻译未命中词条
            api_translations: dict[str, str] = {}
            normal_api_translations: dict[str, str] = {}
            excel_review_marks: dict[str, str] = {}
            if (misses or mixed_texts) and not self._stop_event.is_set():
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在请求大模型翻译未命中词汇..."))
                self._log("INFO", f"发送 API 请求，共 {api_call_count} 词条")

                progress_lock = threading.Lock()
                progress_done = {"normal": 0, "mixed": 0}

                def _emit_api_progress(kind: str, done: int) -> None:
                    with progress_lock:
                        progress_done[kind] = max(0, int(done or 0))
                        total_done = min(progress_done["normal"], len(misses)) + min(
                            progress_done["mixed"],
                            len(mixed_texts),
                        )
                    self._queue.put(ProgressMsg(
                        phase_index=2,
                        phase_total=phase_total,
                        phase_name="云端翻译",
                        step_done=min(total_done, max(api_call_count, 1)),
                        step_total=max(api_call_count, 1),
                    ))

                def progress_cb(done, total):
                    _emit_api_progress("normal", done)

                def api_error_cb(msg: str) -> None:
                    level = "ERROR" if "单条翻译仍失败" in msg else "WARN"
                    self._log(level, msg)

                t0 = datetime.now()
                batch_stats = TranslationBatchRunStats()
                shared_scheduler = None
                if settings.engine.mode != "local":
                    shared_scheduler = self._api_scheduler or WeightedApiScheduler(concurrency)

                mixed_stats = MixedLanguageRunStats()
                mixed_results = {}

                def run_normal_api_translation() -> dict[str, str]:
                    if not misses:
                        return {}
                    return translate_texts(
                        misses,
                        engine,
                        target_lang,
                        system_prompt,
                        batch_size,
                        concurrency,
                        progress_cb,
                        api_error_cb,
                        should_stop=self.stop_requested,
                        source_lang=source_lang,
                        api_scheduler=shared_scheduler,
                        stats=batch_stats,
                    )

                def run_mixed_api_translation():
                    if not mixed_texts:
                        return {}
                    self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在处理混合语言内容..."))

                    def mixed_progress_cb(done, total):
                        _emit_api_progress("mixed", done)

                    return translate_mixed_language_texts(
                        mixed_texts,
                        engine=engine,
                        target_lang=target_lang,
                        system_prompt=system_prompt,
                        source_lang=source_lang,
                        concurrency=concurrency,
                        max_items_per_batch=batch_size,
                        max_chars_per_batch=DEFAULT_MIXED_MAX_BATCH_CHARS,
                        retry_attempts=3,
                        progress_callback=mixed_progress_cb,
                        error_callback=api_error_cb,
                        should_stop=self.stop_requested,
                        api_scheduler=shared_scheduler,
                        request_category=API_REQUEST_CATEGORY_NORMAL,
                        stats=mixed_stats,
                    )

                if misses and mixed_texts and shared_scheduler is not None:
                    with ThreadPoolExecutor(max_workers=2) as main_executor:
                        normal_future = main_executor.submit(run_normal_api_translation)
                        mixed_future = main_executor.submit(run_mixed_api_translation)
                        normal_api_translations = normal_future.result()
                        mixed_results = mixed_future.result()
                else:
                    normal_api_translations = run_normal_api_translation()
                    mixed_results = run_mixed_api_translation()

                api_translations.update(normal_api_translations)
                for source, translation in normal_api_translations.items():
                    if str(source or "").strip().lower() == str(translation or "").strip().lower():
                        _set_excel_review_mark(
                            excel_review_marks,
                            source,
                            MIXED_MARK_UNRESOLVED,
                        )

                if mixed_texts:
                    for source, result in mixed_results.items():
                        if result.action in {MIXED_ACTION_TRANSLATE, MIXED_ACTION_FOREIGN_NOISE} and result.translation.strip():
                            api_translations[source] = result.translation.strip()
                        elif result.action == MIXED_ACTION_UNCERTAIN:
                            api_translations[source] = source
                        if result.mark_kind:
                            _set_excel_review_mark(
                                excel_review_marks,
                                source,
                                result.mark_kind,
                            )
                    self._log(
                        "INFO",
                        (
                            "混合语言路径："
                            f"命中 {mixed_stats.input_count} 条，"
                            f"已双语 {mixed_stats.action_counts.get(MIXED_ACTION_EXISTING_BILINGUAL, 0)}，"
                            f"疑似原文错误 {mixed_stats.action_counts.get(MIXED_ACTION_FOREIGN_NOISE, 0)}，"
                            f"不确定 {mixed_stats.action_counts.get(MIXED_ACTION_UNCERTAIN, 0)}，"
                            f"语义校验接受 {mixed_stats.semantic_accepted_count}"
                        ),
                    )
                _raise_if_stopped("任务已中止，未写入剩余翻译结果。")
                api_elapsed = (datetime.now() - t0).total_seconds()
                self._log(
                    "INFO",
                    (
                        "Excel 实际请求："
                        f"{batch_stats.batch_count} 批，"
                        f"缩小重试 {batch_stats.retry_count} 次，"
                        f"单条失败 {batch_stats.failed_batch_count} 条，"
                        f"最大请求权重 {batch_stats.max_request_weight}"
                        + (
                            f"，自适应降并发 {batch_stats.adaptive_concurrency_reductions} 次，"
                            f"最低并发 {batch_stats.adaptive_lowest_concurrency}"
                            if batch_stats.adaptive_concurrency_reductions
                            else ""
                        )
                    ),
                )
                if batch_stats.failed_batch_count:
                    quality_issues.append(
                        {
                            "type": "api_unavailable",
                            "severity": "needs_action",
                            "message": (
                                "部分内容未能从 API 获得译文，已按安全策略保留原文。"
                                "请检查 API Key、Base URL、模型名称或服务状态，重新配置后再试。"
                            ),
                            "failed_sources": list(batch_stats.failed_items),
                        }
                    )
                self._log("OK", f"API 翻译完成，返回 {len(api_translations)} 条（{api_elapsed:.2f}s）")
                self._task_logger.global_api_done(returned=len(api_translations), elapsed=api_elapsed)

                # 将 API 结果写入 TM
                if auto_source_lang:
                    pairs_to_insert: dict[str, list[tuple[str, str]]] = {}
                    for source_text, translation in normal_api_translations.items():
                        candidates = text_source_candidates.get(source_text, set())
                        # A file-level preflight with two substantive languages
                        # is intentionally not auto-written: no per-item
                        # model source report exists to disambiguate it.
                        if len(candidates) != 1 or not should_store_translation_in_tm(
                            source_text,
                            translation,
                        ):
                            continue
                        pair = build_lang_pair(target_lang, source_lang=next(iter(candidates)))
                        pairs_to_insert.setdefault(pair, []).append((source_text, translation))
                    written = sum(
                        tm_manager.insert_batch(
                            entries,
                            pair,
                            max_len,
                            engine.engine_name,
                            sync_reverse=False,
                        )
                        for pair, entries in pairs_to_insert.items()
                    )
                else:
                    new_pairs = [
                        (k, v)
                        for k, v in normal_api_translations.items()
                        if should_store_translation_in_tm(k, v)
                    ]
                    written = tm_manager.insert_batch(
                        new_pairs,
                        lang_pair,
                        max_len,
                        engine.engine_name,
                        sync_reverse=False,
                    )
                if written:
                    self._log("INFO", f"新增 TM 词条：{written} 条")

            # 汇聚全局翻译词典：TM 命中覆盖 API 结果（TM 优先）
            global_translations = {**api_translations, **hits}
            for source, translation in global_translations.items():
                if str(source or "").strip().lower() == str(translation or "").strip().lower():
                    _set_excel_review_mark(
                        excel_review_marks,
                        source,
                        MIXED_MARK_UNRESOLVED,
                    )

            phase2_elapsed = (datetime.now() - t_phase2).total_seconds()
            self._log("OK", f"[阶段 2 完成] 翻译数据就绪（{phase2_elapsed:.2f}s）")

            _raise_if_stopped()

            # ══════════════════════════════════════════════════════════
            # 阶段 3：逐文件串行回填写入
            # ══════════════════════════════════════════════════════════
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 3/{phase_total}] 正在生成双语表格..."))
            self._queue.put(ProgressMsg(
                phase_index=3,
                phase_total=phase_total,
                phase_name="生成文件",
                step_done=0,
                step_total=max(len(self._files), 1),
            ))

            t_phase3 = datetime.now()
            source_root = self._source_root if self._source_root else self._files[0].path.parent

            for fi, file_item in enumerate(self._files):
                _raise_if_stopped()

                # 跳过阶段 1 已失败的文件
                already_failed = any(
                    r.get("source_path") == str(file_item.path) and not r.get("success")
                    for r in file_results
                )
                if already_failed:
                    continue

                self._queue.put(ProgressMsg(
                    phase_index=3, phase_total=phase_total, phase_name="生成文件",
                    step_done=fi, step_total=len(self._files),
                ))

                self._log("INFO", f"[阶段 3] 写入文件：{file_item.name}（{fi+1}/{len(self._files)}）")

                process_path = process_paths[fi]

                try:
                    rel_subdir = file_item.path.parent.relative_to(source_root)
                except ValueError:
                    rel_subdir = Path()

                try:
                    t0 = datetime.now()
                    # KNOWN-ISSUE-VAL-006:
                    # The current write path intentionally stays text-only.
                    # See docs/KNOWN_ISSUES.md before reintroducing image flow.
                    if self._untranslated_only:
                        coverage_plan = coverage_plans[fi] if fi < len(coverage_plans) else None
                        if coverage_plan is None:
                            raise ValueError("缺少补译识别计划，无法安全按位置写入。")
                        out_path = write_untranslated_excel_file(
                            source_path=process_path,
                            output_dir=output_dir / rel_subdir,
                            plan=coverage_plan,
                            translations=global_translations,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            keep_original_sheets=self._settings.output.keep_original_sheets,
                            formula_display_value_backfill=(
                                self._settings.output.formula_display_value_backfill
                            ),
                            lock_row_height=self._settings.output.lock_row_height,
                            log_callback=lambda msg: self._log(
                                "OK" if msg.startswith("[OK]") else "INFO", msg
                            ),
                            original_path=file_item.original_path,
                        )
                    else:
                        out_path = bilingual_writer.write_bilingual_file(
                            source_path          = process_path,
                            output_dir           = output_dir / rel_subdir,
                            translations         = global_translations,
                            target_lang          = target_lang,
                            source_lang          = source_lang,
                            keep_original_sheets = self._settings.output.keep_original_sheets,
                            formula_display_value_backfill = self._settings.output.formula_display_value_backfill,
                            enable_print_guard   = self._settings.output.enable_print_guard,
                            lock_row_height      = self._settings.output.lock_row_height,
                            review_marks         = excel_review_marks,
                            review_mark_colors   = self._settings.word_review.mark_colors,
                            mark_review_items    = self._settings.excel_review.mark_review_items,
                            existing_fill_policy = self._settings.excel_review.existing_fill_policy,
                            log_callback         = lambda msg: self._log(
                                "OK" if msg.startswith("[OK]") else "INFO", msg
                            ),
                            original_path        = file_item.original_path,
                        )
                    write_elapsed = (datetime.now() - t0).total_seconds()
                    self._task_logger.file_write_done(file_item.name, write_elapsed)

                    # 统计该文件对应的 TM/API 使用情况
                    this_file_texts = file_texts[fi]
                    this_tm = sum(1 for t in this_file_texts if t in hits)
                    this_api = sum(1 for t in this_file_texts if t in misses or t in mixed_texts)
                    self._task_logger.file_done(
                        filename=file_item.name,
                        elapsed=write_elapsed,
                        tm_hits=this_tm,
                        api_calls=this_api,
                    )

                    file_results.append({
                        "name":    file_item.name,
                        "source_path": str(file_item.path),
                        "output":  str(out_path),
                        "success": True,
                    })
                    self._log("OK", f"文件完成：{file_item.name}（{write_elapsed:.2f}s）")
                except Exception as e:
                    self._log("ERROR", f"文件写入失败 {file_item.name}：{e}")
                    self._task_logger.file_error(file_item.name, str(e))
                    file_results.append({
                        "name": file_item.name,
                        "source_path": str(file_item.path),
                        "success": False,
                        "error": str(e),
                    })
                finally:
                    # 清理 .xls 转换后的临时 .xlsx 文件
                    if process_path != file_item.path:
                        try:
                            os.remove(process_path)
                        except Exception as e:
                            self._log("WARN", f"临时文件清理失败 {process_path.name}: {e}")

            # 阶段 3 收尾进度
            self._queue.put(ProgressMsg(
                phase_index=3, phase_total=phase_total, phase_name="生成文件",
                step_done=len(self._files), step_total=len(self._files),
            ))

            phase3_elapsed = (datetime.now() - t_phase3).total_seconds()
            self._log("OK", f"[阶段 3 完成] 文件写入完毕（{phase3_elapsed:.2f}s）")

            elapsed = (datetime.now() - start_ts).total_seconds()
            if need_autofit:
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 正在准备 Excel 精调..."))
            else:
                self._queue.put(StatusMsg(phase_desc="状态：[收尾中] 正在整理任务结果..."))

            # ── 批量 AutoFit：仅在未锁定行高时启用（且开关打开）──────────
            if need_autofit:
                out_paths = [
                    Path(r["output"]) for r in file_results
                    if r.get("success") and r.get("output")
                ]
                if out_paths:
                    if reuse_excel_for_autofit:
                        self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 正在复用阶段 1 的 Excel 进程精调行高..."))
                    else:
                        self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 正在启动干净 Excel 进程精调行高..."))
                    self._queue.put(ProgressMsg(
                        phase_index=4,
                        phase_total=phase_total,
                        phase_name="Excel 精调",
                        step_done=0,
                        step_total=len(out_paths),
                    ))
                    t0 = datetime.now()
                    self._log(
                        "INFO",
                        f"开始 Excel AutoFit，共 {len(out_paths)} 个文件 | policy={excel_policy}",
                    )

                    def autofit_progress_cb(done, total, current_file):
                        self._queue.put(ProgressMsg(
                            phase_index=4,
                            phase_total=phase_total,
                            phase_name="Excel 精调",
                            step_done=done,
                            step_total=total,
                        ))
                        if current_file is not None and done < total:
                            self._queue.put(StatusMsg(
                                phase_desc=f"状态：[阶段 4/{phase_total}] 正在精调 Excel 行高：{current_file.name}"
                            ))

                    if reuse_excel_for_autofit:
                        app = _get_excel_app()
                        autofit_success = bilingual_writer.autofit_files_batch(
                            out_paths,
                            app=app,
                            log_callback=lambda msg: self._log(
                                "WARN" if msg.startswith("[WARN]") else "INFO", msg
                            ),
                            progress_callback=autofit_progress_cb,
                        )
                    else:
                        autofit_success = _run_autofit_with_guard(out_paths, autofit_progress_cb)

                    self._queue.put(ProgressMsg(
                        phase_index=4,
                        phase_total=phase_total,
                        phase_name="Excel 精调",
                        step_done=len(out_paths),
                        step_total=len(out_paths),
                    ))
                    autofit_elapsed = (datetime.now() - t0).total_seconds()
                    if autofit_success:
                        self._log("INFO", f"Excel AutoFit 完成，耗时 {autofit_elapsed:.2f}s | policy={excel_policy}")
                        self._task_logger.info(
                            f"批量AutoFit完成 | 文件数={len(out_paths)} | 耗时={autofit_elapsed:.3f}s | excel_policy={excel_policy}"
                        )
                    else:
                        self._log("WARN", f"Excel AutoFit 未完全完成，已保留 Python 估算行高 | policy={excel_policy}")
                        self._task_logger.warning(
                            f"批量AutoFit未完全完成 | 文件数={len(out_paths)} | 耗时={autofit_elapsed:.3f}s | excel_policy={excel_policy}"
                        )
                else:
                    self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 无可精调文件，已跳过 Excel 精调。"))
                    self._queue.put(ProgressMsg(
                        phase_index=4,
                        phase_total=phase_total,
                        phase_name="Excel 精调",
                        step_done=1,
                        step_total=1,
                    ))

            if self._settings.output.lock_row_height and self._settings.output.enable_excel_autofit:
                self._log("INFO", '已启用"锁定行高，缩小字号"，跳过 Excel AutoFit。')
        except TaskStopped as e:
            stopped_message = str(e)
        except ApiKeyTemporarilyUnavailableError as e:
            fatal_error_message = str(e)
        finally:
            if excel_app is not None:
                self._log("INFO", "清理全局 Excel 进程...")
                _cleanup_excel_app(status_msg="状态：[收尾中] 正在清理 Excel 进程...")

        if stopped_message is not None:
            elapsed = (datetime.now() - start_ts).total_seconds()
            self._log("WARN", stopped_message)
            self._task_logger.warning(stopped_message)
            self._task_logger.task_end(
                elapsed_sec=elapsed,
                file_results=file_results,
            )
            self._queue.put(StoppedMsg(message=stopped_message))
            return

        if fatal_error_message is not None:
            elapsed = (datetime.now() - start_ts).total_seconds()
            self._log("ERROR", fatal_error_message)
            self._task_logger.error(fatal_error_message)
            self._task_logger.task_end(
                elapsed_sec=elapsed,
                file_results=file_results,
            )
            self._queue.put(ErrorMsg(message=fatal_error_message))
            return

        elapsed = (datetime.now() - start_ts).total_seconds()

        # ── 任务日志：记录结束信息 ────────────────────────────────────
        self._task_logger.task_end(
            elapsed_sec  = elapsed,
            file_results = file_results,
        )
        self._queue.put(DoneMsg(
            output_dir     = str(output_dir),
            file_results   = file_results,
            elapsed_sec    = elapsed,
            tm_hit_count   = tm_hit_count,
            api_call_count = api_call_count,
            issues         = quality_issues,
        ))

    @staticmethod
    def _decide_excel_policy(
        need_autofit: bool,
        xls_file_count: int,
        total_sheet_count: int,
        raw_text_count: int,
    ) -> tuple[str, str]:
        """Decide whether to reuse the stage-1 Excel process or start a clean one."""
        if not need_autofit:
            return "split", "autofit_disabled"
        if xls_file_count <= 0:
            return "split", "no_xls_conversion"

        if xls_file_count <= 3 and total_sheet_count <= 15 and raw_text_count <= 800:
            return "reuse", "light_load"

        if xls_file_count >= 10 or total_sheet_count >= 40 or raw_text_count >= 10000:
            return "split", "heavy_load"

        risk_votes = 0
        if xls_file_count >= 4:
            risk_votes += 1
        if total_sheet_count >= 20:
            risk_votes += 1
        if raw_text_count >= 2000:
            risk_votes += 1

        if risk_votes >= 2:
            return "split", "mid_load_vote"
        return "reuse", "mid_load_vote"

    @staticmethod
    def _collect_texts(
        real_path: Path,
        item_name: str,
        *,
        target_lang: str = "",
        source_lang: str = "zh",
    ) -> tuple[list[str], int]:
        """用 openpyxl 快速读取文件中需要翻译的所有唯一词条，并返回工作表数量。"""
        from openpyxl import load_workbook
        seen: set[str] = set()
        wb = load_workbook(str(real_path), read_only=True, data_only=True)
        try:
            worksheets = wb.worksheets
            for ws in worksheets:
                for row in ws.iter_rows(values_only=True):
                    for val in row:
                        if isinstance(val, str):
                            t = val.strip()
                            if (
                                t
                                and t not in seen
                                and should_translate(
                                    t,
                                    target_lang=target_lang,
                                    source_lang=source_lang,
                                )
                            ):
                                seen.add(t)
        finally:
            wb.close()
        return list(seen), len(worksheets)
