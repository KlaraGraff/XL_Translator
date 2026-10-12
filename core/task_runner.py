"""
后台任务线程管理器（全局聚合流水线架构）。

三阶段全局处理模型：
  阶段 1（收集）：依次扫描所有表格并提取词条，全局去重汇聚。
  阶段 2（翻译）：对去重词汇池统一查询 TM + 批量并发 API 翻译。
  阶段 3（写入）：翻译数据就绪后，逐文件串行回填写入。

优势：打破文件壁垒，最大化利用 API 批次并发能力；跨文件去重减少重复请求。
"""
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from loguru import logger
from lxml import etree

from core import bilingual_writer
from core.excel_sheet_naming import rename_worksheets
from core.phase_progress import PhaseProgress
from core.output_name_translation import (
    avoid_bilingual_name_collision,
    output_stem_from_translation,
    translate_names,
)
from core.api_concurrency_control import ApiCredentialRejectedError, ApiKeyTemporarilyUnavailableError
from core.api_scheduler import API_REQUEST_CATEGORY_NORMAL, WeightedApiScheduler
from core.api_config_check import check_translation_api_config
from core.coverage_arbitration import RETRANSLATE_MODEL
from core.coverage_review import arbitrate_coverage_units
from core.excel_coverage import (
    build_excel_coverage_plan,
    workbook_sheet_titles,
    write_untranslated_excel_file,
)
from core.file_scanner import FileItem
from core.file_progress import FileProgressReporter
from core.language_registry import (
    build_lang_pair,
    get_default_source_lang,
    get_source_lang_display,
    get_target_lang_display,
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
    MIXED_MARK_UNRESOLVED,
    MixedLanguageRunStats,
    split_mixed_language_sources,
    translate_mixed_language_texts,
)
from core.translation_coverage import format_ignored_coverage_report, resolve_coverage_source_lang
from core.translation_filter import should_translate
from core.engine_dispatcher import (
    TranslationBatchRunStats,
    build_engine,
    build_role_engine,
    get_system_prompt,
    smart_context_enabled,
    translate_texts,
    translate_texts_with_sources,
)
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from core.model_throughput import get_model_throughput
from core.residual_classifier import (
    check_heading_consistency,
    is_section_heading_source,
)
from core.residual_pipeline import ResidualPassResult, run_residual_pass
from core.residual_repair import (
    DEFAULT_REPAIR_BREAKER_THRESHOLD,
    DEFAULT_REPAIR_MAX_UNITS,
    REPAIR_METHOD_LABELS,
    run_repair_ladder,
)
from core.tm_hygiene import sanitize_tm_pairs, tm_hygiene_log_lines
from engines.base_engine import engine_supports_chat
from core.translation_protocol import should_store_translation_in_tm
from core.concurrent_calls import map_concurrently, run_in_background, wait_result
from core.document_profile import (
    append_profile,
    drop_unlocked_short_hits,
    is_context_sensitive,
    lookup_locked_tm,
    request_document_profile,
)
from core import tm_manager
from core.xlsx_embedded_text import KIND_SHAPE, EmbeddedTextOptions, collect_embedded_units
from core.xlsx_patcher import is_generated_original_sheet_title
from core.excel_automation import (
    create_excel_app,
    finalize_excel_thread,
    get_excel_process_pid,
    initialize_excel_thread,
    terminate_process_tree,
)
from core.task_logger import TaskLogger
from core.process_artifacts import ProcessArtifacts
from core.user_facing_errors import humanize_error
from core.runner_base import BaseTaskRunner
# 消息类型移到了 core/task_runner_messages.py（所有任务运行器共用），这里照旧可以导入。
from core.task_runner_messages import (  # noqa: F401 - re-exported, imported from here across the codebase
    ProgressMsg,
    StatusMsg,
    WordRecoveryStatusMsg,
    PdfReviewStatusMsg,
    PdfPageRecoveryStatusMsg,
    LogMsg,
    DoneMsg,
    ErrorMsg,
    StoppedMsg,
    TaskStopped,
)
from settings import AppSettings

# 「给用户看的一句话」的判定标准，写在这里是因为三个 runner 都要用同一条标准。
# humanize_error 认识的原因会被换成中文；它认不出的照原样返回——那是库该有的
# 默认，却正是我们不能端到界面上的东西。所以这里再加一道闸：句子里必须有中文，
# 且不能带请求地址或 JSON 错误体，否则就用调用点给的兜底句，原文只留给 debug 日志。
_USER_REASON_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_USER_REASON_URL_RE = re.compile(r"\b(?:https?|wss?)://", re.IGNORECASE)
_USER_REASON_JSON_RE = re.compile(r"\{\s*[\"']")


def _detect_post_failure_source_language(
    *,
    engine,
    samples: list[str],
    requested_source: str,
    target_lang: str,
    reset_count: int,
    original_count: int,
) -> str | None:
    """Probe once after a near-total quality reset and return a real mismatch.

    Manual source selection normally avoids a language-detection request.  A
    near-total reset is exceptional evidence that the selection may be wrong,
    so one bounded probe is justified there.  Any unavailable/uncertain result
    falls back to the ordinary quality warning.
    """
    if (
        not samples
        or is_auto_source_lang(requested_source)
        or reset_count < 3
        or original_count <= 0
        or reset_count * 5 < original_count * 4
        or not engine_supports_chat(engine)
    ):
        return None
    try:
        probe = preflight_files(
            {"post_failure": samples},
            lambda candidates, detected_target: engine.chat(
                LANGUAGE_PREFLIGHT_SYSTEM_PROMPT,
                build_language_preflight_prompt(
                    candidates,
                    target_lang=detected_target,
                ),
            ),
            target_lang=target_lang,
        )["post_failure"]
    except Exception as exc:  # defensive: diagnosis must not break delivery
        logger.debug("语言方向失败后预检失败：{}", exc)
        return None
    detected = probe.primary_source_lang
    if not detected or detected == requested_source:
        return None
    return detected


def _language_mismatch_message(
    detected_source: str,
    requested_source: str,
    target_lang: str,
) -> str:
    detected_name = get_source_lang_display(detected_source)
    requested_name = get_source_lang_display(requested_source)
    target_name = get_target_lang_display(target_lang)
    if detected_source == target_lang:
        suggestion = f"建议反向选择“{target_name} → {requested_name}”后重新翻译。"
    else:
        suggestion = (
            f"请将源语言改为“{detected_name}”，"
            f"并确认目标语言“{target_name}”后重新翻译。"
        )
    return (
        f"语言方向可能有误：检测到源文本更接近{detected_name}，"
        f"当前选择为“{requested_name} → {target_name}”。{suggestion}"
    )


def user_facing_reason(value: object, *, fallback: str) -> str:
    """Return one plain-Chinese sentence describing ``value`` to a user."""
    sentence = humanize_error(value)
    if not _USER_REASON_CJK_RE.search(sentence):
        return fallback
    if _USER_REASON_URL_RE.search(sentence) or _USER_REASON_JSON_RE.search(sentence):
        return fallback
    return sentence


# 写入器（bilingual_writer、xlsx_patcher、excel_coverage、word_document……）用
# 「[WARN] / [OK] / [INFO]」前缀给每行定级别。前缀只是给 runner 看的记号，不该原样
# 出现在用户的运行日志里：这里剥掉它，并按它定级别。
_LOG_PREFIX_LEVELS = (
    ("[WARN]", "WARN"),
    ("[ERROR]", "ERROR"),
    ("[OK]", "OK"),
    ("[INFO]", "INFO"),
)


def split_log_prefix(message: object, default: str = "INFO") -> tuple[str, str]:
    """Return ``(level, text)`` with a leading ``[WARN]``-style tag removed."""
    text = str(message or "")
    stripped = text.lstrip()
    for prefix, level in _LOG_PREFIX_LEVELS:
        if stripped.startswith(prefix):
            return level, stripped[len(prefix):].lstrip()
    return default, text


AUTOFIT_STALL_TIMEOUT_SECONDS = 180
AUTOFIT_MONITOR_POLL_SECONDS = 0.5
# 单元格底色和 Word 段落底色是同一条规矩：只留给真正要人动手的两类——译文没出来
# （保留了原文）和原文本身可疑。同一条原文被判进两类时按这里的大小取重的那个。
# 「语义校验接受」在 9.3.1 整类删除，见 MixedLanguageResult.mark_kind。
_EXCEL_REVIEW_MARK_PRIORITY = {
    MIXED_MARK_UNRESOLVED: 20,
    MIXED_MARK_FOREIGN_NOISE: 30,
}

# 修复方法中文名只在 core/residual_repair 维护一份（Word 共用）
_RESIDUAL_REPAIR_METHOD_LABELS = REPAIR_METHOD_LABELS

# 残留修复阶梯的护栏值只在 core/residual_repair 维护一份（Word 共用）
_RESIDUAL_REPAIR_MAX_UNITS = DEFAULT_REPAIR_MAX_UNITS
_RESIDUAL_REPAIR_BREAKER_THRESHOLD = DEFAULT_REPAIR_BREAKER_THRESHOLD


def _build_excel_source_excerpt(text: str, *, head: int = 18, tail: int = 16) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= head + tail + 3:
        return normalized
    return f"{normalized[:head]}……{normalized[-tail:]}"


def _set_excel_review_mark(review_marks: dict[str, str], source: str, mark: str) -> None:
    source_key = str(source or "").strip()
    if not source_key:
        return
    existing = review_marks.get(source_key)
    if existing is None or _EXCEL_REVIEW_MARK_PRIORITY.get(mark, 0) > _EXCEL_REVIEW_MARK_PRIORITY.get(existing, 0):
        review_marks[source_key] = mark


def _already_in_target_language(source: str, item, target_lang: str) -> bool:
    """模型报的实际语言就是目标语言，且译文与原文相同。"""
    translation = str(getattr(item, "translation", "") or "").strip()
    reported = str(getattr(item, "source_lang", "") or "").strip().lower()
    return (
        bool(translation)
        and translation.lower() == str(source or "").strip().lower()
        and reported == str(target_lang or "").strip().lower()
    )


# ── TaskRunner ────────────────────────────────────────────────────────────────

class TaskRunner(BaseTaskRunner):
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
        connection_chain: tuple[str, ...] | list[str] | None = None,
        resume_output_dir: str | None = None,
    ):
        self._files       = file_items
        self._settings    = settings
        self._source_root = Path(source_root) if source_root else None
        self._allow_xls_fallback = allow_xls_fallback
        self._source_lang = str(source_lang or settings.source_lang or "zh").strip() or "zh"
        self._key_overrides = dict(key_overrides or {})
        self._api_scheduler = api_scheduler
        self._untranslated_only = bool(untranslated_only)
        # 只读：上次输出目录，补译时用它的双语产物做底稿。绝不写入。
        self._resume_output_dir = Path(resume_output_dir) if resume_output_dir else None
        # The pool entries this task may fall back to, frozen at start.
        self._connection_chain = tuple(connection_chain or ())
        self._queue: queue.Queue = queue.Queue()
        self._file_progress = FileProgressReporter(
            self._queue, self._files, self._source_root
        )
        self._file_progress.emit_many("waiting", "extract")
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._task_logger = TaskLogger(enabled=True)
        # Runtime connection switches, in order. A switch changes who translated
        # the rest of the run, so it belongs in the terminal record and the
        # report — not only in a WARN line that scrolls away.
        self._connection_switches: list[dict[str, str]] = []
        self._switch_lock = threading.Lock()
        # _run 按实际阶段数重建；这里先放一个，直接调用单个阶段方法的测试也能用。
        self._translate_progress = self._new_translate_progress(phase_total=3)

    def _new_translate_progress(self, *, phase_total: int) -> PhaseProgress:
        # 「翻译」这一段把所有要等模型的步骤都算进来：识别原文语言、主批次、
        # 补译遗留原文、翻译工作表名称。只算主批次的话，其余几步进行时进度条
        # 一动不动，补译多的时候能在 80% 停上好几分钟。
        return PhaseProgress(
            self._queue.put,
            phase_index=2,
            phase_total=phase_total,
            phase_name="翻译",
            steps=(("detect", 5), ("batches", 82), ("repair", 8), ("names", 5)),
        )

    @property
    def task_id(self) -> str:
        return self._task_logger.task_id

    def _report_connection_switch(self, previous, current, failure_kind, error) -> None:
        """Surface a runtime connection switch in the task's own event log."""
        reason = {
            "endpoint": "该连接的服务暂时不可用",
            "credential": "密钥被拒绝或额度耗尽",
        }.get(failure_kind, failure_kind)
        # Batches run on a thread pool, so two threads can report a switch at once.
        with self._switch_lock:
            self._connection_switches.append(
                {
                    "from_label": str(getattr(previous, "display_label", "") or ""),
                    "to_label": str(getattr(current, "display_label", "") or ""),
                    "reason": reason,
                    "at": datetime.now().strftime("%H:%M:%S"),
                }
            )
        self._log(
            "WARN",
            f"已切换连接：{previous.display_label} → {current.display_label}（{reason}）",
        )

    # ── 全局聚合流水线主入口 ──────────────────────────────────────────────

    def _run(self) -> None:
        start_ts     = datetime.now()
        settings     = self._settings
        excel_output = settings.excel_output
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
        # 接口持续限流、但已经拿到一部分译文时，不再整批失败：已翻好的照常写出，
        # 没翻到的保留原文，之后可「接着上次继续」补译。这里记下那次限流。
        rate_limited: list[ApiKeyTemporarilyUnavailableError] = []
        api_call_count = 0
        file_results: list[dict] = []
        stopped_message: str | None = None
        # 停止后写出的文件里还剩多少格原文；None 表示没数（没走到写盘后的清点）
        # 或数不出来——那时不报数，更不能报 0 冒充「已经全部翻完」。
        unfinished_cells: int | None = None
        fatal_error_message: str | None = None
        quality_issues: list[dict] = []
        file_language_preflights: dict = {}
        file_texts: list[set[str]] = []
        normal_api_language_results: dict = {}
        excel_review_marks: dict[str, str] = {}

        try:
            config_check = check_translation_api_config(settings)
            if not config_check.ok:
                detail = f"（{config_check.detail}）" if config_check.detail else ""
                self._file_progress.emit_many("unstarted", "not_started")
                self._queue.put(ErrorMsg(message=f"{config_check.message}{detail}"))
                return
            # Only take the failover path when there is somewhere to fail over
            # to; a single-connection setup keeps exactly its previous engine.
            engine        = (
                build_role_engine(
                    settings,
                    ROLE_TRANSLATION,
                    connection_ids=self._connection_chain,
                    on_switch=self._report_connection_switch,
                )
                if len(self._connection_chain) > 1
                else build_engine(settings)
            )
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
            # 调度器提到阶段 1 之前建：补译复核在阶段 1 就要发请求，它和阶段 2 的
            # 正文翻译必须共用同一个限流器，否则两边各按满并发跑会撞上接口上限。
            shared_scheduler = (
                (self._api_scheduler or WeightedApiScheduler(concurrency))
                if settings.engine.mode != "local"
                else None
            )
        except Exception as e:
            logger.debug(f"引擎初始化失败原始错误：{e!r}")
            self._file_progress.emit_many("unstarted", "not_started")
            self._queue.put(
                ErrorMsg(
                    message="引擎初始化失败："
                    + user_facing_reason(
                        e,
                        fallback="翻译设置中有未填写完整的项，请检查所选连接和模型。",
                    )
                )
            )
            return

        def _raise_if_stopped(message: str = "任务已停止") -> None:
            if self._stop_event.is_set():
                raise TaskStopped(message)

        def _cleanup_excel_conversion_temp(
            process_path: Path, file_item, *, is_resume_baseline: bool
        ) -> None:
            """删掉 .xls → .xlsx 转换产生的临时件（`$TMPDIR/xl_translator_temp/*.xlsx`）。

            两种情况都不是「临时件」，直接跳过：process_path 就是源文件本身
            （没转换过）；或它是续译底稿（上次的正式产物，只读绝不删）。文件
            已经被别处删过时 `.exists()` 挡住，不会在日志里报一次假的
            「删不掉」——阶段 1 抽词失败、续译换底稿都会提前删一次，这里必须
            能被安全地重复调用（中-8：兜底扫一遍时不能对已删文件出警告）。
            """
            if process_path == file_item.path or is_resume_baseline:
                return
            if not process_path.exists():
                return
            try:
                os.remove(process_path)
            except Exception as e:
                self._log(
                    "WARN",
                    f"临时文件清理失败 {process_path.name}: "
                    f"{user_facing_reason(e, fallback='无法删除临时文件。')}",
                )

        # 阶段 1/3 内部各有自己的清理点，但停止、致命失败连阶段 3 都进不去，
        # 阶段 3 里提前停止时后面还没轮到的文件、以及 already_failed 被跳过的
        # 文件也一样漏删——预先声明成空列表，好让下面 finally 里的兜底扫描
        # 在任务连阶段 1 都没跑起来就被停止时，不会因为变量从未赋值而炸掉。
        process_paths: list[Path] = []
        resume_baseline_used: list[bool] = []

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        custom_output_dir = (
            excel_output.custom_output_dir
            if excel_output.use_custom_output_dir
            else None
        )
        try:
            if excel_output.use_custom_output_dir:
                output_dir_error = bilingual_writer.get_custom_output_dir_error(custom_output_dir)
                if output_dir_error is not None:
                    raise ValueError(output_dir_error)

            output_dir = bilingual_writer.build_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug(f"输出目录初始化失败原始错误：{e!r}")
            self._file_progress.emit_many("unstarted", "not_started")
            self._queue.put(
                ErrorMsg(
                    message="输出目录初始化失败："
                    + user_facing_reason(
                        e,
                        fallback="输出目录不可用，请在设置中另选可写入的目录。",
                    )
                )
            )
            return

        process_artifacts: ProcessArtifacts | None = None
        process_finished = False

        def _finish_process(status: str, *, elapsed_sec: float | None = None) -> None:
            """Close the internal run record without affecting deliverables."""
            nonlocal process_finished
            if process_artifacts is None or process_finished:
                return
            if elapsed_sec is not None:
                try:
                    (process_artifacts.work_dir / "run-summary.json").write_text(
                        json.dumps(
                            {
                                "task_id": self.task_id,
                                "status": status,
                                "output_dir": str(output_dir),
                                "files": len(self._files),
                                "elapsed_sec": round(float(elapsed_sec), 3),
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except OSError as exc:
                    logger.debug("Excel 过程摘要写入失败：{}", exc)
            try:
                process_artifacts.finish(status)
                process_finished = True
            except Exception as exc:  # process evidence must never block delivery
                logger.warning("Excel 过程记录收尾失败：{}", exc)

        try:
            process_artifacts = ProcessArtifacts.create("excel", output_dir, self.task_id)
        except Exception as exc:  # process evidence is best effort
            logger.warning("Excel 过程目录创建失败，继续输出成果文件：{}", exc)

        # ── 任务日志：记录启动信息 ────────────────────────────────────
        self._task_logger.task_start(
            files                = self._files,
            engine_name          = engine.engine_name,
            target_lang          = target_lang,
            keep_original_sheets = excel_output.keep_original_sheets,
            output_translation_only = excel_output.output_translation_only,
            formula_display_value_backfill = excel_output.formula_display_value_backfill,
            enable_excel_autofit = excel_output.enable_excel_autofit,
            lock_row_height      = excel_output.lock_row_height,
        )

        # 只写进 app.log：路径和内部参数对排查有用，但不该出现在用户看的运行日志里。
        logger.info(f"[诊断] source_root={self._source_root} | custom_output_dir={custom_output_dir} | output_dir={output_dir}")
        self._log("INFO", f"扫描到 {len(self._files)} 个文件")
        if self._untranslated_only:
            self._log("INFO", "[补译模式] 只补未译内容：已有译文位置将保持不变。")

        need_autofit = excel_output.enable_excel_autofit and not excel_output.lock_row_height
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
                logger.info(f"强制结束 Excel 进程 PID={pid}（{reason}）：terminated={terminated}")
                if terminated:
                    self._log("WARN", f"{reason}，已强制关闭 Excel。")
                else:
                    self._log("WARN", f"{reason}，但未能确认 Excel 已关闭；如仍在运行，请手动退出。")
            except Exception as e:
                logger.debug(f"强制结束 Excel 进程失败原始错误 PID={pid}：{e!r}")
                self._log(
                    "WARN",
                    f"{reason}，但未能强制关闭 Excel："
                    + user_facing_reason(e, fallback="请手动退出 Excel 后重试。"),
                )

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
                logger.debug(f"Excel 进程清理异常原始错误：{e!r}")
                self._log(
                    "WARN",
                    "关闭 Excel 时出错："
                    + user_facing_reason(e, fallback="程序将尝试强制关闭 Excel。"),
                )
                _kill_excel_pid(pid, "Excel 未正常退出")
            finally:
                excel_app = None
                finalize_excel_thread(excel_thread_state)
                excel_thread_state = None

        def _run_autofit_with_guard(
            file_paths: list[Path], progress_callback, file_start_callback=None
        ) -> bool:
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

                    def _file_start(current_file):
                        worker_state["last_progress_ts"] = time.monotonic()
                        if file_start_callback is not None:
                            file_start_callback(current_file)

                    worker_state["result"] = bilingual_writer.autofit_files_batch(
                        file_paths,
                        app=app,
                        log_callback=lambda msg: self._log(*split_log_prefix(msg)),
                        progress_callback=_progress,
                        file_start_callback=_file_start,
                    )
                except Exception as e:
                    worker_state["error"] = e
                finally:
                    if app is not None:
                        pid = get_excel_process_pid(app)
                        try:
                            app.quit()
                        except Exception:
                            _kill_excel_pid(pid, "调整行高结束后 Excel 未正常退出")
                    finalize_excel_thread(thread_state)
                    done_event.set()

            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()

            while not done_event.wait(timeout=AUTOFIT_MONITOR_POLL_SECONDS):
                if self._stop_event.is_set():
                    self._task_logger.warning("收到停止请求，正在终止 Excel 精调。")
                    _kill_excel_pid(worker_state["pid"], "收到停止请求")
                    done_event.wait(timeout=5)
                    raise TaskStopped("任务已停止，行高调整已中止，已生成的文件均已保留。")

                stalled_for = time.monotonic() - worker_state["last_progress_ts"]
                if stalled_for >= AUTOFIT_STALL_TIMEOUT_SECONDS:
                    msg = (
                        f"Excel 连续 {AUTOFIT_STALL_TIMEOUT_SECONDS} 秒没有进展，"
                        "已停止自动调整行高，行高按估算值保存。"
                    )
                    self._log("WARN", msg)
                    self._task_logger.warning(msg)
                    _kill_excel_pid(worker_state["pid"], "调整行高长时间无进展")
                    done_event.wait(timeout=5)
                    return False

            if worker_state["error"] is not None:
                raise worker_state["error"]

            return bool(worker_state["result"])

        def _get_excel_app():
            nonlocal excel_app, excel_thread_state
            if excel_app is None:
                self._queue.put(StatusMsg(phase_desc="状态：正在启动 Excel，请稍候…"))
                try:
                    excel_thread_state = initialize_excel_thread()
                    self._log("INFO", "正在启动 Excel…")
                    excel_app = create_excel_app(visible=False, add_book=False)
                    _configure_excel_app(excel_app)
                except Exception as e:
                    finalize_excel_thread(excel_thread_state)
                    excel_thread_state = None
                    logger.debug(f"启动全局 Excel 进程失败原始错误：{e!r}")
                    start_reason = user_facing_reason(
                        e,
                        fallback="本机 Excel 未能启动，可能尚未安装，或未授权本程序控制。",
                    )
                    self._log("WARN", f"Excel 未能启动：{start_reason}")
                    raise Exception(
                        "本机 Excel 未能启动，请确认已安装 Excel 并已允许本程序控制："
                        f"{start_reason}"
                    )
            return excel_app

        try:
            _raise_if_stopped()
            # ══════════════════════════════════════════════════════════
            # 阶段 1：全局词汇提取（扫描 + .xls 转换 + 收集词条）
            # ══════════════════════════════════════════════════════════
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 1/{phase_total}] 正在读取所有文件中的文字…"))

            # process_paths[i] 对应 self._files[i]，若 .xls 则指向转换后的临时 .xlsx，
            # 若续译换了底稿则指向上次输出的双语文件
            process_paths: list[Path] = []
            # resume_baseline_used[i] 标记 process_paths[i] 是否为上次输出的双语底稿
            # （而非源文件本身/其转换件），写盘阶段据此纠正输出命名基准
            resume_baseline_used: list[bool] = []
            # deferred_resume_baselines[i]：自动识别源语言时，阶段 1 不能换底稿（换了
            # 会拿双语文件当语言预检样本，译文混进去把源语言判花）。这里先把匹配好、
            # 验证过的底稿记下来，等预检定出源语言、重算补译清单之前再换。
            deferred_resume_baselines: list[Path | None] = []
            # file_texts[i] 对应 self._files[i] 的本文件词条集合
            file_texts = []
            coverage_plans = []
            global_unique_texts: set[str] = set()
            file_language_preflights = {}
            text_source_scopes: dict[str, list[frozenset[str]]] = {}
            # 自动识别时每个文件自己的源语言（路径 → 语言），见下方语言预检
            file_source_langs: dict[str, str] = {}
            file_conversion_modes: dict[str, str] = {
                str(item.path): "native_xlsx"
                for item in self._files
            }

            t_phase1 = datetime.now()

            for fi, file_item in enumerate(self._files):
                xls_file_count, total_sheet_count, raw_text_count = self._collect_file_texts(
                    _raise_if_stopped=_raise_if_stopped,
                    file_item=file_item,
                    fi=fi,
                    phase_total=phase_total,
                    root_for_output=root_for_output,
                    target_lang=target_lang,
                    auto_source_lang=auto_source_lang,
                    excel_output=excel_output,
                    source_lang=source_lang,
                    resume_baseline_used=resume_baseline_used,
                    deferred_resume_baselines=deferred_resume_baselines,
                    xls_file_count=xls_file_count,
                    _get_excel_app=_get_excel_app,
                    file_conversion_modes=file_conversion_modes,
                    file_results=file_results,
                    process_paths=process_paths,
                    file_texts=file_texts,
                    coverage_plans=coverage_plans,
                    concurrency=concurrency,
                    engine=engine,
                    excel_review_marks=excel_review_marks,
                    lang_pair=lang_pair,
                    quality_issues=quality_issues,
                    shared_scheduler=shared_scheduler,
                    _cleanup_excel_conversion_temp=_cleanup_excel_conversion_temp,
                    total_sheet_count=total_sheet_count,
                    raw_text_count=raw_text_count,
                    global_unique_texts=global_unique_texts,
                )

            # 阶段 1 收尾：发送最终进度
            self._queue.put(ProgressMsg(
                phase_index=1, phase_total=phase_total, phase_name="读取文件",
                step_done=len(self._files), step_total=len(self._files),
            ))

            phase1_elapsed = (datetime.now() - t_phase1).total_seconds()
            self._log("OK", f"读取完成：全部文件合计 {len(global_unique_texts)} 处待翻译文本，相同内容仅翻译一次")
            self._task_logger.global_collected(
                total_unique=len(global_unique_texts),
                file_count=len(self._files),
                elapsed=phase1_elapsed,
            )

            self._translate_progress = self._new_translate_progress(phase_total=phase_total)

            smart_mode = smart_context_enabled(settings, "excel")
            self._smart_mode = smart_mode
            wants_profile = bool(smart_mode and global_unique_texts and engine_supports_chat(engine))
            parallel_requests = concurrency if settings.engine.mode != "local" else 1
            # 「自动识别场景」的通读请求和语言识别互不依赖：先在后台发出去，和语言识别
            # 同时进行。只补译未翻译内容时，语言识别之后待译文字还会重算，那时再发。
            profile_future = None
            if wants_profile and not (auto_source_lang and self._untranslated_only):
                profile_future = self._start_document_profile(engine, global_unique_texts, target_lang)

            if auto_source_lang:
                source_lang, system_prompt, raw_text_count, file_language_preflights = self._preflight_source_languages(
                    detect_workers=max(1, parallel_requests - (1 if profile_future else 0)),
                    concurrency=concurrency,
                    coverage_plans=coverage_plans,
                    deferred_resume_baselines=deferred_resume_baselines,
                    engine=engine,
                    excel_output=excel_output,
                    excel_review_marks=excel_review_marks,
                    file_conversion_modes=file_conversion_modes,
                    file_results=file_results,
                    file_source_langs=file_source_langs,
                    file_texts=file_texts,
                    global_unique_texts=global_unique_texts,
                    process_paths=process_paths,
                    quality_issues=quality_issues,
                    resume_baseline_used=resume_baseline_used,
                    settings=settings,
                    shared_scheduler=shared_scheduler,
                    target_lang=target_lang,
                    text_source_scopes=text_source_scopes,
                    system_prompt=system_prompt,
                    raw_text_count=raw_text_count,
                )

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
            logger.info(f"[Excel策略] {excel_policy_log}")  # 同上，只进 app.log
            self._task_logger.excel_policy_decided(
                excel_policy=excel_policy,
                xls_file_count=xls_file_count,
                total_sheet_count=total_sheet_count,
                raw_text_count=raw_text_count,
                reason=excel_policy_reason,
            )

            if excel_app is not None and not reuse_excel_for_autofit:
                logger.info("按策略释放阶段 1 的 Excel 进程，阶段 4 将使用干净进程")
                _cleanup_excel_app(force=True)
            elif reuse_excel_for_autofit:
                logger.info("按策略保留阶段 1 的 Excel 进程，阶段 4 将直接复用")

            _raise_if_stopped()

            if wants_profile and global_unique_texts:
                # 「自动识别场景」：正式翻译前先读一遍全部文字，概括出文档说明带进每批提示词。
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在通读文档…"))
                if profile_future is None:
                    profile_future = self._start_document_profile(engine, global_unique_texts, target_lang)
                system_prompt = append_profile(
                    system_prompt,
                    wait_result(profile_future, should_stop=self._stop_event.is_set, default=""),
                )
                _raise_if_stopped()

            # ══════════════════════════════════════════════════════════
            # 阶段 2：全局统一翻译（TM 查询 + API 并发）
            # ══════════════════════════════════════════════════════════
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在查找记忆库中已有的译文…"))

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
                    f"有 {len(mixed_texts)} 处内容夹杂多种语言，会单独翻译，不使用记忆库。",
                )

            # TM 查询按“文件预检 -> 该文件文本 -> 实际语言对”分组。不能
            # 将全局词池交叉查到每一个语言对，否则 zh 文件会误命中 en-*。
            text_tm_pairs: dict[str, set[str]] = {}
            if auto_source_lang:
                for file_item, text_set in zip(self._files, file_texts):
                    result = file_language_preflights.get(str(file_item.path))
                    allowed_pairs = (
                        set(result.tm_lang_pairs(target_lang)) if result is not None else set()
                    )
                    for text in text_set:
                        if text in normal_texts and allowed_pairs:
                            text_tm_pairs.setdefault(text, set()).update(allowed_pairs)
            elif lang_pair:
                text_tm_pairs = {text: {lang_pair} for text in normal_texts}

            # Same-value hits across two permitted pairs are usable, but a
            # conflicting value remains a model miss and is never guessed.
            tm_values_by_text: dict[str, list[str]] = {
                text: [] for text in normal_texts
            }
            tm_texts_by_pair: dict[str, list[str]] = {}
            for text, pairs in text_tm_pairs.items():
                for pair in pairs:
                    tm_texts_by_pair.setdefault(pair, []).append(text)
            for pair, pair_texts in tm_texts_by_pair.items():
                tm_result = tm_manager.lookup_batch(pair_texts, pair)
                for text, value in tm_result.items():
                    if value is not None and str(value) not in tm_values_by_text.setdefault(text, []):
                        tm_values_by_text[text].append(str(value))
            hits = {
                text: values[0]
                for text, values in tm_values_by_text.items()
                if len(values) == 1
            }
            if smart_mode:
                dropped = drop_unlocked_short_hits(hits, tm_texts_by_pair, lookup_locked_tm)
                if dropped:
                    self._log(
                        "INFO",
                        f"自动识别场景：{dropped} 个短词和缩写不直接套用记忆库里自动存下的译文，按本文档重新翻译",
                    )
            misses = [text for text in normal_texts if text not in hits]

            tm_hit_count   = len(hits)
            api_call_count = len(misses) + len(mixed_texts)

            self._log(
                "INFO",
                f"记忆库已有译文 {tm_hit_count} 处，需要模型翻译 {len(misses)} 处，"
                f"夹杂多种语言 {len(mixed_texts)} 处",
            )
            self._task_logger.global_tm_result(hits=tm_hit_count, misses=api_call_count)
            self._translate_progress.report(
                "batches", 0 if api_call_count else 1, max(api_call_count, 1)
            )

            # API 翻译未命中词条
            api_translations: dict[str, str] = {}
            normal_api_translations: dict[str, str] = {}
            normal_api_language_results = {}
            # 不许在这里重绑定 excel_review_marks：它在函数顶部声明一次，
            # 阶段 1 的 _arbitrate_excel_coverage_pairs 已经把仲裁标记写进去了
            # （中-3）——这里再赋一个新空 dict 会把那些标记全部丢弃，底色不涂、
            # review 计数归零。下面继续用同一个字典累加阶段 2 自己的标记。
            normal_api_translations = self._translate_misses_via_api(
                api_call_count=api_call_count,
                api_translations=api_translations,
                auto_source_lang=auto_source_lang,
                batch_size=batch_size,
                concurrency=concurrency,
                engine=engine,
                excel_review_marks=excel_review_marks,
                file_texts=file_texts,
                global_unique_texts=global_unique_texts,
                hits=hits,
                lang_pair=lang_pair,
                max_len=max_len,
                misses=misses,
                mixed_texts=mixed_texts,
                normal_api_language_results=normal_api_language_results,
                phase_total=phase_total,
                quality_issues=quality_issues,
                rate_limited=rate_limited,
                shared_scheduler=shared_scheduler,
                source_lang=source_lang,
                system_prompt=system_prompt,
                target_lang=target_lang,
                text_source_scopes=text_source_scopes,
                normal_api_translations=normal_api_translations,
            )
                # TM 写入挪到了残留修复阶梯之后：这里写会把「修复前」的
                # 带残留译文存进库，下个文档 TM 命中直接短路，残留就固化了。

            # 汇聚全局翻译词典：TM 命中覆盖 API 结果（TM 优先）
            global_translations = {**api_translations, **hits}
            for source, translation in global_translations.items():
                if str(source or "").strip().lower() == str(translation or "").strip().lower():
                    _set_excel_review_mark(
                        excel_review_marks,
                        source,
                        MIXED_MARK_UNRESOLVED,
                    )

            # ── 残留中文体检 + 确定性序号修复（0 API，与 Word 共用分类器）──
            # 放在写盘前：能确定修的（序号前缀）直接改词典，改不了的标记复核。
            residual_result = self._repair_residuals(
                global_translations=global_translations,
                target_lang=target_lang,
                quality_issues=quality_issues,
                engine=engine,
                rate_limited=rate_limited,
                phase_total=phase_total,
                excel_review_marks=excel_review_marks,
                # 补译遗留原文按翻译的并发设置同时修；本地引擎的并发交给引擎自己。
                repair_concurrency=concurrency if settings.engine.mode != "local" else 1,
            )

            # ── 文档级标题写法巡检（0 API）：逐段各自都对，全篇混两套写法
            # 只有聚在一起才看得出来。多数派可确定性改写时直接改词典。
            heading_majority = self._normalize_heading_style(
                global_translations=global_translations,
                target_lang=target_lang,
                quality_issues=quality_issues,
            )

            # ── TM 写入（从 API 返回后挪到这里）：必须存「文件最终译文」——
            # 序号确定性修复、修复阶梯、标题归一都改完之后的版本。早写会把
            # 带残留的译文固化进库，下个文档命中 TM 直接短路，永远修不到。
            # 停止信号不拦这一步：能走到这里说明 API 结果已经付费拿到，写入
            # 是本地库操作，跳过只会让下次运行整批重新付费；仍带残留的配对
            # 由 sanitize_tm_pairs 在入库口拦下，不会因停止而混入库里。
            self._store_final_translations_in_tm(
                normal_api_language_results=normal_api_language_results,
                normal_api_translations=normal_api_translations,
                auto_source_lang=auto_source_lang,
                engine=engine,
                global_translations=global_translations,
                heading_majority=heading_majority,
                lang_pair=lang_pair,
                max_len=max_len,
                residual_result=residual_result,
                target_lang=target_lang,
                text_source_scopes=text_source_scopes,
                quality_issues=quality_issues,
            )

            # 耗时只进 app.log 排查用，运行日志里不给用户看秒数。
            logger.info(f"阶段 2 耗时 {(datetime.now() - t_phase2).total_seconds():.2f}s")
            self._log("OK", "翻译完成，开始生成文件")

            # 阶段 3 的入口闸（与 Word 同一立场）：停止落在翻译中途时，已经到手的
            # 译文照常写进文件，没翻到的格子保留原文，之后「接着翻译」只补这部分。
            # 一条译文都没拿到才整批放弃——写出来也只是一份原文副本。
            stop_write_note = ""
            if self._stop_event.is_set():
                if not global_translations:
                    raise TaskStopped("任务已停止，尚无已翻译的内容，未生成文件。")
                stop_write_note = (
                    "任务已停止，已翻译的内容已写入文件，未翻译的部分保留原文，可继续翻译。"
                )

            self._file_progress.emit_many("waiting", "generate")

            # ══════════════════════════════════════════════════════════
            # 阶段 3：逐文件串行回填写入
            # ══════════════════════════════════════════════════════════
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 3/{phase_total}] 正在生成双语表格…"))

            t_phase3 = datetime.now()
            source_root = self._source_root if self._source_root else self._files[0].path.parent

            # Collect names across the task before serial file writes.  The
            # default worksheet-name option used to make one model request per
            # workbook here, even when every workbook had the same tab title.
            filename_names: list[str] = []
            sheet_names_to_translate: list[str] = []
            if excel_output.translate_output_filename or excel_output.translate_sheet_names:
                for fi, file_item in enumerate(self._files):
                    if any(
                        r.get("source_path") == str(file_item.path) and not r.get("success")
                        for r in file_results
                    ):
                        continue
                    if excel_output.translate_output_filename:
                        original_path = file_item.original_path or file_item.path
                        filename_names.append(original_path.stem)
                    if excel_output.translate_sheet_names:
                        try:
                            with ZipFile(process_paths[fi]) as package:
                                workbook = etree.fromstring(package.read("xl/workbook.xml"))
                            namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                            names = [
                                node.get("name", "")
                                for node in workbook.iter(f"{{{namespace}}}sheet")
                            ]
                        except Exception as exc:  # File-level write still reports a bad workbook.
                            logger.debug(f"读取工作表名称失败 {file_item.name}：{exc!r}")
                            names = list(file_item.sheets)
                        # 续译时这里读的是上次的产物：母表可能已经是译名，「报价单_原文」
                        # 要拿源文件的分表名才认得出是副本，否则副本也被拿去翻译改名。
                        clone_parents = set(names)
                        if Path(process_paths[fi]) != Path(file_item.path):
                            clone_parents.update(workbook_sheet_titles(file_item.path))
                        sheet_names_to_translate.extend(
                            name
                            for name in names
                            if not is_generated_original_sheet_title(name, clone_parents)
                        )

            def _translate_name_pool(names: list[str], kind: str) -> dict[str, str]:
                unique = list(dict.fromkeys(name for name in names if name.strip()))
                if not unique:
                    return {}
                batches: list[list[str]] = []
                current: list[str] = []
                current_chars = 0
                for name in unique:
                    if current and (len(current) >= 24 or current_chars + len(name) > 1800):
                        batches.append(current)
                        current = []
                        current_chars = 0
                    current.append(name)
                    current_chars += len(name)
                if current:
                    batches.append(current)
                translated: dict[str, str] = {}
                for number, batch in enumerate(batches, start=1):
                    if self._stop_event.is_set():
                        # 阶段 3 不再整批弃文件：名称翻译途中停止，没轮到的名称保持
                        # 原样，格子照常写出。抛 TaskStopped 会把已翻好的内容一起丢掉。
                        break
                    # 名称翻译也在等模型，进度算在「翻译」段的末尾；先报进度再报状态，
                    # 进度卡标题才显示「正在翻译工作表名称」。
                    self._translate_progress.report("names", number - 1, len(batches))
                    self._queue.put(StatusMsg(
                        phase_desc=(
                            f"状态：[阶段 3/{phase_total}] 正在翻译{kind}（{number}/{len(batches)} 批）…"
                        )
                    ))
                    translated.update(
                        translate_names(engine, batch, target_lang, source_lang, kind=kind)
                    )
                return translated

            if rate_limited and (filename_names or sheet_names_to_translate):
                # 接口还在限流，再请求只会等满两分钟后失败；名称保持原样。
                self._log("WARN", "服务持续限流，本次不翻译输出文件名和工作表名称。")
                filename_names, sheet_names_to_translate = [], []
            if self._stop_event.is_set():
                # 已经停止：名称翻译还要再发请求，名称保持原样，续译时再翻。
                filename_names, sheet_names_to_translate = [], []
            filename_translations = _translate_name_pool(filename_names, "输出文件名")
            sheet_translations = _translate_name_pool(sheet_names_to_translate, "工作表名称")
            self._translate_progress.finish()
            self._queue.put(ProgressMsg(
                phase_index=3,
                phase_total=phase_total,
                phase_name="生成文件",
                step_done=0,
                step_total=max(len(self._files), 1),
            ))
            self._queue.put(StatusMsg(
                phase_desc=f"状态：[阶段 3/{phase_total}] 正在生成双语表格…"
            ))

            reserved_output_paths: set[Path] = set()
            for fi, file_item in enumerate(self._files):
                # 阶段 3 进了门就不再半路丢文件：这一步只把已经算好的译文落盘，
                # 不发请求；为了「响应停止」把后面文件已经翻好的内容扔掉不划算。
                self._write_file_output(
                    _raise_if_stopped=lambda *_args, **_kwargs: None,
                    file_item=file_item,
                    file_results=file_results,
                    fi=fi,
                    phase_total=phase_total,
                    process_paths=process_paths,
                    resume_baseline_used=resume_baseline_used,
                    source_root=source_root,
                    excel_output=excel_output,
                    filename_translations=filename_translations,
                    output_dir=output_dir,
                    reserved_output_paths=reserved_output_paths,
                    target_lang=target_lang,
                    coverage_plans=coverage_plans,
                    excel_review_marks=excel_review_marks,
                    file_source_langs=file_source_langs,
                    global_translations=global_translations,
                    need_autofit=need_autofit,
                    process_artifacts=process_artifacts,
                    source_lang=source_lang,
                    sheet_translations=sheet_translations,
                    quality_issues=quality_issues,
                    file_texts=file_texts,
                    hits=hits,
                    misses=misses,
                    mixed_texts=mixed_texts,
                    file_conversion_modes=file_conversion_modes,
                    _cleanup_excel_conversion_temp=_cleanup_excel_conversion_temp,
                )

            # 阶段 3 收尾进度
            self._queue.put(ProgressMsg(
                phase_index=3, phase_total=phase_total, phase_name="生成文件",
                step_done=len(self._files), step_total=len(self._files),
            ))

            # 耗时只进 app.log 排查用，运行日志里不给用户看秒数。
            logger.info(f"阶段 3 耗时 {(datetime.now() - t_phase3).total_seconds():.2f}s")
            self._log("OK", "全部文件已生成")

            if self._stop_event.is_set():
                # 停止后不再调整行高：续译跑完时会对整份文件重新调整。
                unfinished_cells = self._record_unfinished_after_stop(
                    file_results,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    formula_display_value_backfill=excel_output.formula_display_value_backfill,
                )
                raise TaskStopped(
                    stop_write_note
                    or "任务已停止，文件已写出，但本次未调整行高，将在继续翻译时一并处理。"
                )

            elapsed = (datetime.now() - start_ts).total_seconds()
            if need_autofit:
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 正在准备调整行高…"))
            else:
                self._queue.put(StatusMsg(phase_desc="状态：[收尾中] 正在整理任务结果…"))

            # ── 批量 AutoFit：仅在未锁定行高时启用（且开关打开）──────────
            self._run_autofit_phase(
                need_autofit=need_autofit,
                file_results=file_results,
                reuse_excel_for_autofit=reuse_excel_for_autofit,
                phase_total=phase_total,
                excel_policy=excel_policy,
                _get_excel_app=_get_excel_app,
                _run_autofit_with_guard=_run_autofit_with_guard,
            )

            if excel_output.lock_row_height and excel_output.enable_excel_autofit:
                self._log("INFO", "已启用「保持行高，缩小字号」，不再自动调整行高。")
        except TaskStopped as e:
            stopped_message = str(e)
        except ApiKeyTemporarilyUnavailableError as e:
            fatal_error_message = str(e)
        finally:
            if excel_app is not None:
                self._log("INFO", "正在关闭 Excel…")
                _cleanup_excel_app(status_msg="状态：[收尾中] 正在关闭 Excel…")
            # 兜底扫一遍所有文件的 .xls 转换临时件：正常跑完时阶段 3 已经在
            # 各自的清理点删过；停止、致命失败连阶段 3 都进不去，阶段 3 内部
            # 提前停止时后面还没轮到的文件也一样——都靠这里补删一遍。已经删过
            # 的文件 _cleanup_excel_conversion_temp 会因为不存在直接跳过，
            # 不会重复报错（中-8）。
            for _fi, _file_item in enumerate(self._files):
                if _fi >= len(process_paths):
                    continue
                _is_resume_baseline = (
                    _fi < len(resume_baseline_used) and resume_baseline_used[_fi]
                )
                _cleanup_excel_conversion_temp(
                    process_paths[_fi], _file_item, is_resume_baseline=_is_resume_baseline
                )

        if stopped_message is not None:
            self._file_progress.emit_many("stopped", "stopped")
            elapsed = (datetime.now() - start_ts).total_seconds()
            if unfinished_cells:
                stopped_message = (
                    f"{stopped_message}尚有 {unfinished_cells} 个单元格未翻译。"
                )
            self._log("WARN", stopped_message)
            self._task_logger.warning(stopped_message)
            self._task_logger.task_end(
                elapsed_sec=elapsed,
                file_results=file_results,
            )
            contract = self._build_result_contract(
                file_results=file_results,
                output_dir=str(output_dir),
                tm_hit_count=tm_hit_count,
                api_text_count=api_call_count,
                elapsed_sec=elapsed,
                stopped=True,
                source_lang=source_lang,
                target_lang=target_lang,
                preflights=file_language_preflights,
                file_texts=file_texts,
                actual_results=normal_api_language_results,
                review_marks=excel_review_marks,
            )
            # 与续译弹窗「补 N 格」同一口径（产物里还是原文的格子数），横幅据此报
            # 「还有 N 格没翻」。数不出来时不写这个键（与 Word 一致），界面按「没数清」
            # 处理，仍给出「接着翻译」。
            if unfinished_cells is not None:
                contract["kpi"]["unstarted_text_count"] = unfinished_cells
            _finish_process("cancelled", elapsed_sec=elapsed)
            self._queue.put(
                StoppedMsg(
                    message=stopped_message,
                    output_dir=str(output_dir),
                    # 正在处理的那份文件在停止前已经跑完阶段 2（含残留中文修复梯队），
                    # quality_issues 里可能已经有它的记录（包括本次新增的
                    # caused_by_stop 标记）——DoneMsg 一直把这份列表接到 issues 上，
                    # 这里之前漏接，导致停止路径下这份信息始终到不了前端。
                    issues=quality_issues,
                    **contract,
                )
            )
            return

        if fatal_error_message is not None:
            self._file_progress.emit_many("failed", "translate")
            elapsed = (datetime.now() - start_ts).total_seconds()
            self._log("ERROR", fatal_error_message)
            self._task_logger.error(fatal_error_message)
            self._task_logger.task_end(
                elapsed_sec=elapsed,
                file_results=file_results,
            )
            contract = self._build_result_contract(
                file_results=file_results,
                output_dir=str(output_dir),
                tm_hit_count=tm_hit_count,
                api_text_count=api_call_count,
                elapsed_sec=elapsed,
                stopped=False,
                source_lang=source_lang,
                target_lang=target_lang,
                preflights=file_language_preflights,
                file_texts=file_texts,
                actual_results=normal_api_language_results,
                review_marks=excel_review_marks,
                error_message=fatal_error_message,
            )
            _finish_process("failed", elapsed_sec=elapsed)
            self._queue.put(
                ErrorMsg(
                    message=fatal_error_message,
                    output_dir=str(output_dir),
                    **contract,
                )
            )
            return

        elapsed = (datetime.now() - start_ts).total_seconds()

        self._file_progress.emit_many("unstarted", "not_started")

        # ── 任务日志：记录结束信息 ────────────────────────────────────
        self._task_logger.task_end(
            elapsed_sec  = elapsed,
            file_results = file_results,
        )
        contract = self._build_result_contract(
            file_results=file_results,
            output_dir=str(output_dir),
            tm_hit_count=tm_hit_count,
            api_text_count=api_call_count,
            elapsed_sec=elapsed,
            stopped=False,
            source_lang=source_lang,
            target_lang=target_lang,
            preflights=file_language_preflights,
            file_texts=file_texts,
            actual_results=normal_api_language_results,
            review_marks=excel_review_marks,
        )
        process_status = (
            "needs_review"
            if any(not bool(item.get("success")) for item in file_results)
            or any(str(issue.get("severity") or "") == "needs_review" for issue in quality_issues)
            else "succeeded"
        )
        _finish_process(process_status, elapsed_sec=elapsed)
        self._queue.put(DoneMsg(
            output_dir     = str(output_dir),
            file_results   = file_results,
            elapsed_sec    = elapsed,
            tm_hit_count   = tm_hit_count,
            api_call_count = api_call_count,
            issues         = quality_issues,
            **contract,
        ))

    def _start_document_profile(self, engine, texts, target_lang):
        """「自动识别场景」：在后台请模型通读全部文字、写文档说明，返回 Future。"""
        texts = list(texts)

        def run() -> str:
            try:
                return request_document_profile(
                    engine.chat,
                    texts,
                    target_language=get_target_lang_display(target_lang),
                    log=self._log,
                )
            except Exception as exc:  # noqa: BLE001 - the description is optional.
                logger.debug(f"文档说明请求失败：{exc!r}")
                return ""

        return run_in_background(run, name="document-profile")

    def _preflight_source_languages(
        self,
        *,
        concurrency,
        coverage_plans,
        deferred_resume_baselines,
        engine,
        excel_output,
        excel_review_marks,
        file_conversion_modes,
        file_results,
        file_source_langs,
        file_texts,
        global_unique_texts,
        process_paths,
        quality_issues,
        resume_baseline_used,
        settings,
        shared_scheduler,
        target_lang,
        text_source_scopes,
        system_prompt,
        raw_text_count,
        detect_workers=1,
    ) -> tuple[str, str, int, dict]:
        """自动识别源语言：每个文件做一次语言预检，定出源语言、补译范围和 TM 语言对；返回更新后的源语言、提示词、待译计数和预检结果。"""
        self._queue.put(
            StatusMsg(
                phase_desc=(
                    "状态：正在识别每个文件的原文语言…"
                )
            )
        )

        detector_calls = {"count": 0}
        detector_lock = threading.Lock()

        def _detect_file_language(samples, detected_target):
            with detector_lock:
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
        file_language_preflights = {}
        # 每个文件一次模型请求，按并发设置同时发出（各文件互不依赖）。每返回一个
        # 文件推一次进度、报「正在检测语言 x/y」（x 是已识别完的文件数），进度条
        # 和进度卡标题都看得出还在往前走（先报进度再报状态，标题才是这句状态）。
        detect_total = len(file_payloads)
        detect_paths = list(file_payloads)
        self._translate_progress.report("detect", 0, detect_total)
        self._queue.put(StatusMsg(phase_desc=f"状态：正在检测语言 0/{detect_total}…"))
        for file_path in detect_paths:
            self._file_progress.emit(file_path, "active", "detect")

        def _detected(index: int, done: int) -> None:
            self._translate_progress.report("detect", done, detect_total)
            self._queue.put(StatusMsg(phase_desc=f"状态：正在检测语言 {done}/{detect_total}…"))
            self._file_progress.emit(detect_paths[index], "waiting", "translate")

        for detected_result in map_concurrently(
            lambda file_path: preflight_files(
                {file_path: file_payloads[file_path]},
                _detect_file_language,
                target_lang=target_lang,
            ),
            detect_paths,
            max_workers=detect_workers,
            on_done=_detected,
        ):
            file_language_preflights.update(detected_result)
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
        # 补译判定按每个文件自己的语言做（审计 高-8）：一批里英文件排在日文件
        # 前面时，拿全局第一个语言去判日文件，它的汉字标题会整份被当成「已是
        # 译文」跳过。某个文件没定出语言时才退回全局值。
        file_source_langs.update(
            (file_path, result.source_langs[0])
            for file_path, result in file_language_preflights.items()
            if result.source_langs
        )
        for file_item, text_set in zip(self._files, file_texts):
            result = file_language_preflights.get(str(file_item.path))
            if result is None:
                continue
            for text in text_set:
                text_source_scopes.setdefault(text, []).append(
                    frozenset(result.source_langs)
                )
        logger.info(
            f"自动语言预检完成：{detector_calls['count']} 次请求，"
            f"实际源语言={','.join(detected_sources) or '未确定'}，"
            f"TM 语言对={','.join(tm_language_pairs) or '无'}"
        )
        self._log(
            "INFO",
            "已识别原文语言："
            + (
                "、".join(get_source_lang_display(code) for code in detected_sources)
                or "未能确定"
            ),
        )
        if self._untranslated_only:
            self._apply_deferred_resume_baselines(
                process_paths=process_paths,
                deferred_resume_baselines=deferred_resume_baselines,
                resume_baseline_used=resume_baseline_used,
                file_results=file_results,
                target_lang=target_lang,
                source_lang=source_lang,
                file_source_langs=file_source_langs,
                formula_display_value_backfill=(
                    excel_output.formula_display_value_backfill
                ),
            )
            raw_text_count = self._rebuild_coverage_plans_after_preflight(
                process_paths=process_paths,
                coverage_plans=coverage_plans,
                file_texts=file_texts,
                file_results=file_results,
                file_conversion_modes=file_conversion_modes,
                global_unique_texts=global_unique_texts,
                resume_baseline_used=resume_baseline_used,
                target_lang=target_lang,
                source_lang=source_lang,
                file_source_langs=file_source_langs,
                formula_display_value_backfill=(
                    excel_output.formula_display_value_backfill
                ),
                engine=engine,
                api_scheduler=shared_scheduler,
                lang_pair=(tm_language_pairs[0] if tm_language_pairs else None),
                concurrency=concurrency,
                quality_issues=quality_issues,
                review_marks=excel_review_marks,
            )
        return source_lang, system_prompt, raw_text_count, file_language_preflights

    def _translate_misses_via_api(
        self,
        *,
        api_call_count,
        api_translations,
        auto_source_lang,
        batch_size,
        concurrency,
        engine,
        excel_review_marks,
        file_texts,
        global_unique_texts,
        hits,
        lang_pair,
        max_len,
        misses,
        mixed_texts,
        normal_api_language_results,
        phase_total,
        quality_issues,
        rate_limited,
        shared_scheduler,
        source_lang,
        system_prompt,
        target_lang,
        text_source_scopes,
        normal_api_translations,
    ) -> dict[str, str]:
        """阶段 2：把 TM 未命中的词条（含混合语言词条）送模型翻译，结果写进 api_translations；返回普通词条的译文表。"""
        if (misses or mixed_texts) and not self._stop_event.is_set():
            api_translation_sources = set(misses) | set(mixed_texts)
            active_translation_paths = [
                self._files[index].path
                for index, texts in enumerate(file_texts)
                if index < len(self._files)
                and any(text in api_translation_sources for text in texts)
            ]
            self._file_progress.emit_many(
                "active", "translate", active_translation_paths
            )
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在翻译记忆库中没有的内容…"))
            self._log("INFO", f"开始翻译 {api_call_count} 处内容")

            progress_lock = threading.Lock()
            progress_done = {"normal": 0, "mixed": 0}

            def _emit_api_progress(kind: str, done: int) -> None:
                with progress_lock:
                    progress_done[kind] = max(0, int(done or 0))
                    total_done = min(progress_done["normal"], len(misses)) + min(
                        progress_done["mixed"],
                        len(mixed_texts),
                    )
                self._translate_progress.report(
                    "batches", min(total_done, max(api_call_count, 1)), max(api_call_count, 1)
                )

            def progress_cb(done, total):
                _emit_api_progress("normal", done)

            def api_error_cb(msg: str) -> None:
                # 只有整批放弃（engine_dispatcher「有 N 条内容未能翻译」）记 ERROR；
                # 夹杂多种语言的单条失败（「有一处…未能翻译」）一直是 WARN。
                level = "ERROR" if re.search(r"条内容未能翻译", msg) else "WARN"
                self._log(level, msg)

            t0 = datetime.now()
            batch_stats = TranslationBatchRunStats()

            mixed_stats = MixedLanguageRunStats()
            mixed_results = {}

            def keep_paid_results(exc: ApiKeyTemporarilyUnavailableError) -> None:
                """Key 被判暂时不可用、整轮任务要失败时，先把已付费的译文存进记忆库。

                这一轮不会再出文件，但用户换 key 或等限流过去重跑时，这些词条直接
                命中记忆库，不用再付一次钱（审计 高-6）。
                """
                partial = getattr(exc, "partial_results", None) or {}
                if not partial:
                    return
                if auto_source_lang:
                    language_partial, plain_partial = dict(partial), {}
                else:
                    language_partial, plain_partial = {}, dict(partial)
                written, tm_error = self.store_api_results_in_tm(
                    auto_source_lang=auto_source_lang,
                    normal_api_language_results=language_partial,
                    normal_api_translations=plain_partial,
                    text_source_scopes=text_source_scopes,
                    target_lang=target_lang,
                    lang_pair=lang_pair,
                    max_len=max_len,
                    engine_name=engine.engine_name,
                )
                if tm_error:
                    self._log("WARN", tm_error)
                elif written:
                    self._log(
                        "INFO",
                        f"任务中断前已获得 {len(partial)} 条译文，其中 {written} 条已保存到记忆库；"
                        "重新翻译时将直接从记忆库调用，不会重复计费。",
                    )

            def run_normal_api_translation() -> dict[str, str]:
                try:
                    return _run_normal_api_translation()
                except ApiKeyTemporarilyUnavailableError as exc:
                    partial = getattr(exc, "partial_results", None) or {}
                    if not partial or isinstance(exc, ApiCredentialRejectedError):
                        # 一条都没翻到：写出去也只是原文副本，照旧整批失败。Key 被拒
                        # 也一样整批失败，那不是等一等就会好的事。
                        keep_paid_results(exc)
                        raise
                    rate_limited.append(exc)
                    self._log(
                        "WARN",
                        f"服务持续限流，已停止发送请求；已获得的 {len(partial)} 条译文照常写入文件，"
                        "其余内容保留原文，可稍后使用「继续上次翻译」补译。",
                    )
                    if auto_source_lang:
                        normal_api_language_results.update(partial)
                        return {
                            source: result.translation
                            for source, result in partial.items()
                        }
                    return dict(partial)

            def _run_normal_api_translation() -> dict[str, str]:
                if not misses:
                    return {}
                if auto_source_lang:
                    language_results = translate_texts_with_sources(
                        misses,
                        engine,
                        target_lang,
                        system_prompt,
                        batch_size,
                        concurrency,
                        progress_cb,
                        api_error_cb,
                        should_stop=self.stop_requested,
                        api_scheduler=shared_scheduler,
                        stats=batch_stats,
                        throughput_unlocked=self._settings.engine.concurrency_unlocked,
                        transient_retries=self._settings.request_retry.excel,
                    )
                    normal_api_language_results.update(language_results)
                    return {
                        source: result.translation
                        for source, result in language_results.items()
                    }
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
                    throughput_unlocked=self._settings.engine.concurrency_unlocked,
                    transient_retries=self._settings.request_retry.excel,
                )

            def run_mixed_api_translation():
                try:
                    return _run_mixed_api_translation()
                except ApiKeyTemporarilyUnavailableError as exc:
                    if isinstance(exc, ApiCredentialRejectedError):
                        raise
                    rate_limited.append(exc)
                    self._log("WARN", "服务持续限流，夹杂多种语言的内容本次未能翻译，已保留原文。")
                    return {}

            def _run_mixed_api_translation():
                if not mixed_texts:
                    return {}
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在翻译夹杂多种语言的内容…"))

                def mixed_progress_cb(done, total):
                    _emit_api_progress("mixed", done)

                if rate_limited:
                    return {}
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

            # 停止时没翻的批次按原文占位交回（batch_stats.stopped_sources）。占位留在
            # 词典里，写盘会把它当成「模型原样返回」——整格标黄需复核、续译核查也认不出
            # 它还没翻。摘掉之后这些格子不进写入词典，输出里保留原文，续译时重新排队。
            stopped_placeholders = {
                source
                for source in batch_stats.stopped_sources
                if normal_api_translations.get(source) == source
            }
            for source in stopped_placeholders:
                normal_api_translations.pop(source, None)
                normal_api_language_results.pop(source, None)
            api_translations.update(normal_api_translations)
            # 自动识别时模型会报每一格的实际语言：报的就是目标语言、译文又原样
            # 返回的格子（英译英表里的 Total、Steel plate）本来就不用翻，不该进
            # 写入词典——进了就会被当成「没翻成功」整格标黄需复核。
            already_target = {
                source
                for source, item in normal_api_language_results.items()
                if _already_in_target_language(source, item, target_lang)
            }
            for source in already_target:
                api_translations.pop(source, None)
            if already_target:
                self._log(
                    "INFO",
                    f"{len(already_target)} 处内容已是目标语言，保持原样，不标记复核。",
                )
            for source, translation in normal_api_translations.items():
                if source in already_target:
                    continue
                if str(source or "").strip().lower() == str(translation or "").strip().lower():
                    _set_excel_review_mark(
                        excel_review_marks,
                        source,
                        MIXED_MARK_UNRESOLVED,
                    )

            if mixed_texts:
                stopping = self._stop_event.is_set()
                for source, result in mixed_results.items():
                    if stopping and result.note in {"stopped", "missing_result"}:
                        # 停止时还没轮到的混合语言词条：不是「待人工确认」，保留原文待续译。
                        continue
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
                        f"夹杂多种语言的内容 {mixed_stats.input_count} 处："
                        f"已有双语 {mixed_stats.action_counts.get(MIXED_ACTION_EXISTING_BILINGUAL, 0)} 处，"
                        f"原文疑似有误 {mixed_stats.action_counts.get(MIXED_ACTION_FOREIGN_NOISE, 0)} 处，"
                        f"待人工确认 {mixed_stats.action_counts.get(MIXED_ACTION_UNCERTAIN, 0)} 处，"
                        f"复查后采用 {mixed_stats.semantic_accepted_count} 处"
                    ),
                )
            if rate_limited:
                if not api_translations and not hits:
                    raise rate_limited[0]
                unfinished = [
                    text
                    for text in dict.fromkeys([*misses, *mixed_texts])
                    if text not in api_translations
                ]
                for text in unfinished:
                    # 原样写回并标需复核：用户在文件里看得到哪些格子还没翻。
                    api_translations[text] = text
                    _set_excel_review_mark(excel_review_marks, text, MIXED_MARK_UNRESOLVED)
                if unfinished:
                    quality_issues.append(
                        {
                            "type": "rate_limited_partial",
                            "severity": "needs_action",
                            "count": len(unfinished),
                            "message": (
                                f"服务持续限流，有 {len(unfinished)} 条内容未能翻译，已保留原文并标记；限流解除后，"
                                "可使用「继续上次翻译」补译，已翻译的内容不会重复计费。"
                            ),
                        }
                    )
            # 注意：这里不能拦停止信号。已经跑到这一步的批次，API 费用已经
            # 付了——`misses`/`mixed_texts` 里凡是进了 normal_api_translations /
            # normal_api_language_results 的词条，都要走到下面的 TM 写入，
            # 否则重跑整批要重新付费（中-2）。真正该拦停止的位置是阶段 3
            # 开始前那一处 `_raise_if_stopped()`，出文件才是可以整批放弃的部分。
            api_elapsed = (datetime.now() - t0).total_seconds()
            logger.info(
                "Excel 实际请求："
                f"{batch_stats.batch_count} 批，"
                f"缩小重试 {batch_stats.retry_count} 次，"
                f"整批重发 {batch_stats.transient_retry_count} 次，"
                f"未翻译 {batch_stats.untranslated_count} 条，"
                f"最大请求权重 {batch_stats.max_request_weight}"
                + (
                    f"，自适应降并发 {batch_stats.adaptive_concurrency_reductions} 次，"
                    f"最低并发 {batch_stats.adaptive_lowest_concurrency}"
                    if batch_stats.adaptive_concurrency_reductions
                    else ""
                )
            )
            if batch_stats.batch_count:
                self._log(
                    "INFO",
                    f"共分 {batch_stats.batch_count} 批发送"
                    + (
                        f"，其中 {batch_stats.retry_count} 次拆分为更小的批次重试"
                        if batch_stats.retry_count
                        else ""
                    )
                    + "。",
                )
            untranslated_count = (
                batch_stats.untranslated_count or batch_stats.failed_batch_count
            )
            if untranslated_count:
                # The file still gets written with the source text in place,
                # so the count has to travel with the result: an unflagged
                # run must never hand back source text dressed as translation.
                quality_issues.append(
                    {
                        "type": "api_unavailable",
                        "severity": "needs_action",
                        "count": untranslated_count,
                        "message": (
                            f"有 {untranslated_count} 条内容未能从模型获得译文，文件中这些位置保留的是原文。"
                            "请检查 API 密钥、Base URL、模型名称或服务状态，重新配置后重试。"
                        ),
                        "failed_sources": list(batch_stats.failed_items),
                    }
                )
                self._log(
                    "ERROR",
                    f"本次有 {untranslated_count} 条内容未获得译文，已在结果中标记为未翻译。",
                )
            if batch_stats.quality_reset_count:
                # 质量校验把译文重置回了原文。文件里这些格子保留的是原文，
                # 只在日志里提一句会被滚过去——必须进结果报告，逐条可查。
                # 用户按了停止就不再发任何请求，语言方向探测也不例外。
                mismatch = None
                if not self._stop_event.is_set():
                    mismatch = _detect_post_failure_source_language(
                        engine=engine,
                        samples=batch_stats.quality_reset_items,
                        requested_source=source_lang,
                        target_lang=target_lang,
                        reset_count=batch_stats.quality_reset_count,
                        original_count=(
                            batch_stats.original_count or len(global_unique_texts)
                        ),
                    )
                issue_type = "suspected_source_language_mismatch" if mismatch else "quality_filter_reset"
                if mismatch:
                    message = _language_mismatch_message(
                        mismatch,
                        source_lang,
                        target_lang,
                    )
                else:
                    message = (
                        f"有 {batch_stats.quality_reset_count} 条译文未通过质量校验，"
                        "已回退为原文。这些单元格中保留原文，"
                        + (
                            "请按下方源文清单逐条核对。"
                            if self._untranslated_only
                            or not self._settings.excel_review.mark_review_items
                            else "已标记为需复核，请在输出文件中核对带标记的位置。"
                        )
                    )
                quality_issues.append(
                    {
                        "type": issue_type,
                        "severity": "needs_action",
                        "count": batch_stats.quality_reset_count,
                        "message": message,
                        "failed_sources": [
                            {"source": source, "error": "译文未通过质量校验，已回退为原文"}
                            for source in batch_stats.quality_reset_items
                        ],
                    }
                )
                self._log(
                    "WARN",
                    f"有 {batch_stats.quality_reset_count} 条译文未通过质量检查，已恢复为原文并记入结果报告。",
                )
            self._log("OK", f"模型翻译完成，共 {len(api_translations)} 处")
            self._task_logger.global_api_done(returned=len(api_translations), elapsed=api_elapsed)
        return normal_api_translations

    def _collect_file_texts(
        self,
        *,
        _raise_if_stopped,
        file_item,
        fi,
        phase_total,
        root_for_output,
        target_lang,
        auto_source_lang,
        excel_output,
        source_lang,
        resume_baseline_used,
        deferred_resume_baselines,
        xls_file_count,
        _get_excel_app,
        file_conversion_modes,
        file_results,
        process_paths,
        file_texts,
        coverage_plans,
        concurrency,
        engine,
        excel_review_marks,
        lang_pair,
        quality_issues,
        shared_scheduler,
        _cleanup_excel_conversion_temp,
        total_sheet_count,
        raw_text_count,
        global_unique_texts,
    ) -> tuple[int, int, int]:
        """阶段 1（单个文件）：选底稿、转换 .xls、扫描待译文本并登记补译计划；返回累加后的三个计数。"""
        _raise_if_stopped()
        self._file_progress.emit(file_item.path, "active", "extract")

        self._queue.put(ProgressMsg(
            phase_index=1, phase_total=phase_total, phase_name="读取文件",
            step_done=fi, step_total=len(self._files),
        ))

        process_path = file_item.path
        source_is_xls = file_item.path.suffix.lower() == ".xls"
        resume_used = False

        # 换底稿只在补译覆盖率计划真的会跑的前提下才安全：覆盖率计划负责
        # 分清「原文」和「已译」，没有它就没法阻止把双语底稿的译文列再整份
        # 送去翻一遍。源语言已知时这里直接换；自动识别时不能现在换——阶段 1
        # 取的词条要当语言预检的样本，双语底稿会把译文混进样本、把源语言判
        # 花——先把匹配好、验证过的底稿记下来，等预检定出源语言、重算补译
        # 清单之前再换（见 _apply_deferred_resume_baselines）。
        deferred_baseline: Path | None = None
        if self._untranslated_only and self._resume_output_dir is not None:
            try:
                rel_dir = file_item.path.parent.relative_to(root_for_output)
            except ValueError:
                rel_dir = Path()
            naming_source_name = (
                file_item.original_path.name
                if file_item.original_path
                else file_item.path.name
            )
            try:
                from core.resume_detection import (
                    baseline_missing_source_texts,
                    describe_baseline_divergence,
                    match_previous_output,
                )
                candidate = match_previous_output(
                    self._resume_output_dir,
                    str(rel_dir),
                    naming_source_name,
                    target_lang,
                    "excel",
                )
            except Exception as e:
                candidate = None
                logger.debug(f"续译底稿匹配失败原始错误 {file_item.name}：{e!r}")
                self._log(
                    "WARN",
                    f"查找 {file_item.name} 的上次翻译结果时出错，本次将完整翻译："
                    + user_facing_reason(e, fallback="查找时出错。"),
                )

            if candidate is not None:
                if not self._validate_resume_baseline(candidate):
                    self._log(
                        "WARN",
                        f"{file_item.name} 上次的翻译结果（{candidate.name}）已损坏或无法打开，本次将从头完整翻译。",
                    )
                elif auto_source_lang:
                    deferred_baseline = candidate
                    self._log(
                        "INFO",
                        f"继续上次翻译：已找到 {file_item.name} 上次的翻译结果 {candidate.name}，"
                        "识别原文语言后将在此基础上补译。",
                    )
                elif (
                    missing := baseline_missing_source_texts(
                        process_path,
                        candidate,
                        surface="excel",
                        target_lang=target_lang,
                        source_lang=source_lang,
                        formula_display_value_backfill=(
                            excel_output.formula_display_value_backfill
                        ),
                    )
                ):
                    # 补译计划只看底稿——源文件在上次翻译后新增的内容不在
                    # 底稿里，硬换底稿会把它静默丢掉（不翻译也不进产物）；
                    # 源文件删掉的内容则相反，会随底稿原样留在产物里。
                    self._log(
                        "WARN",
                        f"继续上次翻译：{file_item.name} 的源文件比上次翻译时{describe_baseline_divergence(missing)}，"
                        "无法在上次结果的基础上补译，"
                        "本次将完整翻译（已翻译的内容将尽量直接从记忆库调用）。",
                    )
                else:
                    process_path = candidate
                    source_is_xls = False
                    resume_used = True
                    self._log(
                        "INFO",
                        f"继续上次翻译：{file_item.name} 在上次的翻译结果 {candidate.name} 上补译",
                    )

        resume_baseline_used.append(resume_used)
        deferred_resume_baselines.append(deferred_baseline)

        if source_is_xls:
            xls_file_count += 1

        # .xls 格式转换（在阶段 1 顺便完成）
        if source_is_xls:
            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 1/{phase_total}] 正在转换 .xls 文件：{file_item.name}"))
            from core.xls_converter import (
                convert_with_excel,
                convert_with_fallback,
                convert_with_libreoffice,
            )
            t_conv = datetime.now()
            try:
                if not self._allow_xls_fallback:
                    app = _get_excel_app()
                    process_path = convert_with_excel(app, process_path)
                    file_conversion_modes[str(file_item.path)] = "excel_automation"
                else:
                    # 兼容转换内部分两级：本机有 LibreOffice 就优先用它保留公式/
                    # 样式/合并单元格；没装或转换失败再退回 xlrd 纯取值——用户侧
                    # 的授权仍是二选一（高保真/兼容），这两级不额外弹选项。
                    try:
                        process_path = convert_with_libreoffice(process_path)
                        file_conversion_modes[str(file_item.path)] = "libreoffice_conversion"
                    except Exception as lo_error:
                        lo_reason = user_facing_reason(
                            lo_error, fallback="LibreOffice 转换不可用。"
                        )
                        # WARN 而不是 INFO：这一步意味着产物质量降级（公式
                        # 变数值），用户扫日志时应当一眼看到。
                        self._log(
                            "WARN",
                            f"{file_item.name}：兼容转换不可用，已改为只保留计算结果"
                            f"（公式会变成数值）：{lo_reason}",
                        )
                        process_path = convert_with_fallback(process_path)
                        file_conversion_modes[str(file_item.path)] = "compatibility_fallback"
                self._log("INFO", f"格式转换完成 {file_item.name}，耗时 {(datetime.now() - t_conv).total_seconds():.2f}s")
            except Exception as e:
                logger.debug(f"源文件转换失败 {file_item.name} 原始错误：{e!r}")
                conversion_reason = user_facing_reason(
                    e,
                    fallback="此 .xls 文件未能转换为 .xlsx，请改用兼容转换后重试。",
                )
                self._log("ERROR", f"源文件转换失败 {file_item.name}: {conversion_reason}")
                self._task_logger.file_error(file_item.name, conversion_reason)
                file_results.append({
                    "name": file_item.name,
                    "source_path": str(file_item.path),
                    "source_relative_path": self._relative_source_path(file_item.path),
                    "format": file_item.format,
                    "conversion_mode": file_conversion_modes.get(
                        str(file_item.path), "high_fidelity_failed"
                    ),
                    "status": "failed",
                    "success": False,
                    "error": f"源文件转换失败: {conversion_reason}",
                })
                self._file_progress.emit(
                    file_item.path, "failed", "extract",
                    result={"error": f"源文件转换失败: {conversion_reason}"},
                )
                process_paths.append(process_path)
                file_texts.append(set())
                coverage_plans.append(None)
                return xls_file_count, total_sheet_count, raw_text_count

        process_paths.append(process_path)

        # 收集词条
        self._log("INFO", f"读取文件：{file_item.name}（{fi+1}/{len(self._files)}）")
        t0 = datetime.now()
        try:
            if self._untranslated_only and not auto_source_lang:
                coverage_plan = build_excel_coverage_plan(
                    process_path,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    formula_display_value_backfill=(
                        excel_output.formula_display_value_backfill
                    ),
                    original_sheet_names=(
                        workbook_sheet_titles(file_item.path) if resume_used else ()
                    ),
                )
                self._arbitrate_excel_coverage_pairs(
                    coverage_plan,
                    engine=engine,
                    api_scheduler=shared_scheduler,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    lang_pair=lang_pair,
                    concurrency=concurrency,
                    file_name=file_item.name,
                    quality_issues=quality_issues,
                    review_marks=excel_review_marks,
                )
                coverage_plans.append(coverage_plan)
                texts = coverage_plan.source_texts
                sheet_count = coverage_plan.sheet_count
                self._log_excel_coverage_plan(
                    file_item.name, coverage_plan, resume_baseline=resume_used
                )
            else:
                texts, sheet_count = self._collect_texts(
                    process_path,
                    file_item.name,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    formula_display_value_backfill=(
                        excel_output.formula_display_value_backfill
                    ),
                )
                coverage_plans.append(None)
        except Exception as e:
            # 底稿探活（load_workbook 打得开）过了、真正建补译计划才炸的
            # 区间：底稿不行不等于源文件不行。用户只是选了「接着上次继续」，
            # 不能因此拿到一个「翻译失败」——退回源文件按全量翻译再试一次，
            # 只有源文件自己也读不出来才算失败。（.xls 源的转换件在换底稿
            # 时被跳过，退无可退，维持失败但把话术指向底稿。）
            degraded = False
            if resume_used and file_item.path.suffix.lower() != ".xls":
                logger.debug(
                    f"续译底稿建计划失败原始错误 {file_item.name}：{e!r}"
                )
                self._log(
                    "WARN",
                    f"{file_item.name} 无法基于上次的结果补译，本次将从头完整翻译："
                    + user_facing_reason(e, fallback="无法读取上次的翻译结果。"),
                )
                process_path = file_item.path
                process_paths[-1] = process_path
                resume_used = False
                resume_baseline_used[-1] = False
                try:
                    if self._untranslated_only and not auto_source_lang:
                        coverage_plan = build_excel_coverage_plan(
                            process_path,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            formula_display_value_backfill=(
                                excel_output.formula_display_value_backfill
                            ),
                        )
                        self._arbitrate_excel_coverage_pairs(
                            coverage_plan,
                            engine=engine,
                            api_scheduler=shared_scheduler,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            lang_pair=lang_pair,
                            concurrency=concurrency,
                            file_name=file_item.name,
                            quality_issues=quality_issues,
                            review_marks=excel_review_marks,
                        )
                        coverage_plans.append(coverage_plan)
                        texts = coverage_plan.source_texts
                        sheet_count = coverage_plan.sheet_count
                        self._log_excel_coverage_plan(
                            file_item.name, coverage_plan
                        )
                    else:
                        texts, sheet_count = self._collect_texts(
                            process_path,
                            file_item.name,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            formula_display_value_backfill=(
                                excel_output.formula_display_value_backfill
                            ),
                        )
                        coverage_plans.append(None)
                    degraded = True
                except Exception as retry_error:
                    e = retry_error
            if not degraded:
                fail_label = "上次的翻译结果读取失败" if resume_used else "源文件读取失败"
                logger.debug(f"{fail_label}原始错误 {file_item.name}：{e!r}")
                read_reason = user_facing_reason(
                    e,
                    fallback="无法打开该文件，可能已损坏或不是有效的 Excel 文件。",
                )
                self._log("ERROR", f"{fail_label} {file_item.name}：{read_reason}")
                self._task_logger.file_error(file_item.name, f"{fail_label}: {read_reason}")
                file_results.append({
                    "name": file_item.name,
                    "source_path": str(file_item.path),
                    "source_relative_path": self._relative_source_path(file_item.path),
                    "format": file_item.format,
                    "conversion_mode": file_conversion_modes.get(str(file_item.path), "native_xlsx"),
                    "status": "failed",
                    "success": False,
                    "error": f"{fail_label}: {read_reason}",
                })
                self._file_progress.emit(
                    file_item.path, "failed", "extract",
                    result={"error": f"{fail_label}: {read_reason}"},
                )
                if len(coverage_plans) < len(process_paths):
                    coverage_plans.append(None)
                file_texts.append(set())
                # resume_used 时 process_path 是上次输出的续译底稿，只读，绝不删除
                _cleanup_excel_conversion_temp(
                    process_path, file_item, is_resume_baseline=resume_used
                )
                return xls_file_count, total_sheet_count, raw_text_count
        text_set = set(texts)
        text_set |= self._collect_embedded_texts(
            process_path,
            file_item.name,
            excel_output=excel_output,
            target_lang=target_lang,
            source_lang=source_lang,
            # 自动识别 + 补译：这一遍只是给语言预检取样，补译清单等识别完再重算。
            coverage=self._untranslated_only and not auto_source_lang,
        )
        file_texts.append(text_set)
        total_sheet_count += sheet_count
        raw_text_count += len(text_set)
        collect_elapsed = (datetime.now() - t0).total_seconds()

        if self._untranslated_only and auto_source_lang:
            # 这一遍取的是全量候选，只拿来给语言预检当样本。真正要补译的
            # 清单要等识别出源语言之后才算得出来（见下面的补译识别重建）。
            self._log(
                "INFO",
                f"  → {file_item.name}：{len(text_set)} 处候选文本，先用于识别原文语言",
            )
        else:
            self._log("INFO", f"  → {file_item.name}：{len(text_set)} 处待翻译文本")
        self._task_logger.file_collected(file_item.name, len(text_set), collect_elapsed)
        self._file_progress.emit(file_item.path, "waiting", "translate")

        global_unique_texts.update(text_set)
        return xls_file_count, total_sheet_count, raw_text_count

    def _write_file_output(
        self,
        *,
        _raise_if_stopped,
        file_item,
        file_results,
        fi,
        phase_total,
        process_paths,
        resume_baseline_used,
        source_root,
        excel_output,
        filename_translations,
        output_dir,
        reserved_output_paths,
        target_lang,
        coverage_plans,
        excel_review_marks,
        file_source_langs,
        global_translations,
        need_autofit,
        process_artifacts,
        source_lang,
        sheet_translations,
        quality_issues,
        file_texts,
        hits,
        misses,
        mixed_texts,
        file_conversion_modes,
        _cleanup_excel_conversion_temp,
    ) -> None:
        """阶段 3（单个文件）：按全局译文词典写出这份文件的双语表格，结果追加进 file_results。"""
        _raise_if_stopped()

        # 跳过阶段 1 已失败的文件
        already_failed = any(
            r.get("source_path") == str(file_item.path) and not r.get("success")
            for r in file_results
        )
        if already_failed:
            # 这个文件在阶段 1/2 之间被标失败（例如自动识别源语言后
            # 重算补译计划时读不出来），它的 .xls 转换临时件当时未必删
            # 过——但不必在这里现删：下面 finally 里的兜底扫描会对
            # self._files 里的每一个文件都补删一遍，覆盖这条路径，
            # 这里再删一次纯属重复（中-8）。
            return

        self._file_progress.emit(file_item.path, "active", "generate")

        self._queue.put(ProgressMsg(
            phase_index=3, phase_total=phase_total, phase_name="生成文件",
            step_done=fi, step_total=len(self._files),
        ))

        self._log("INFO", f"生成文件：{file_item.name}（{fi+1}/{len(self._files)}）")

        process_path = process_paths[fi]
        # 换了续译底稿时，output 命名/相对目录必须仍按「原始源文件」算，
        # 否则底稿文件名（已经带过一次「双语」后缀）会在输出名里叠加一遍。
        naming_original_path = file_item.original_path
        if (
            fi < len(resume_baseline_used)
            and resume_baseline_used[fi]
            and naming_original_path is None
        ):
            naming_original_path = file_item.path

        try:
            rel_subdir = file_item.path.parent.relative_to(source_root)
        except ValueError:
            rel_subdir = Path()

        try:
            t0 = datetime.now()
            original_name = naming_original_path.name if naming_original_path else file_item.path.name
            output_basename = original_name
            if excel_output.translate_output_filename:
                original_stem = Path(original_name).stem
                translated_stem = output_stem_from_translation(
                    original_stem,
                    filename_translations.get(original_stem, original_stem),
                )
                if translated_stem != original_stem:
                    output_basename = translated_stem + Path(original_name).suffix
                else:
                    self._log("WARN", f"[{file_item.name}] 输出文件名未获得可用译名，沿用原名。")
            output_basename = avoid_bilingual_name_collision(
                output_dir / rel_subdir,
                output_basename,
                target_lang,
                reserved_output_paths,
                translation_only=excel_output.output_translation_only,
            )
            file_review_positions: list[dict[str, str]] = []
            # 写入器要知道后面还会不会跑 Excel 整表 AutoFit：会跑就得把整张表的
            # 悬浮图片锚点全部固定，否则 Excel 重排行高会把没冻结的图片拉变形。
            write_stats: dict[str, object] = {}
            # KNOWN-ISSUE-VAL-006:
            # The current write path intentionally stays text-only.
            # See docs/KNOWN_ISSUES.md before reintroducing image flow.
            if self._untranslated_only:
                coverage_plan = coverage_plans[fi] if fi < len(coverage_plans) else None
                if coverage_plan is None:
                    raise ValueError("补译前的内容核对未完成，为避免写入错误位置，未输出此文件。")
                # 「保留原文副本」的快照取自写入源。续译时写入源是上次的双语
                # 底稿——从双语内容里克隆不出「未翻译的原始副本」，硬克隆出来
                # 的「X_原文」里装的是「原文\n译文」，名字在撒谎、体积还翻倍。
                # 底稿里上一轮已有的「X_原文」是普通分表，复制产物时原样保留，
                # 不受这里影响。
                resume_used_here = (
                    fi < len(resume_baseline_used) and resume_baseline_used[fi]
                )
                if excel_output.keep_original_sheets and resume_used_here:
                    self._log(
                        "INFO",
                        (
                            f"  → [继续上次翻译] {file_item.name}：上次的翻译结果已是双语内容，"
                            "无法从中复制纯原文分表，本次不新增「_原文」分表（已有分表保持不变）。"
                        ),
                    )
                out_path = write_untranslated_excel_file(
                    embedded_text=self._embedded_text_options(
                        process_path,
                        excel_output=excel_output,
                        translations=global_translations,
                        target_lang=target_lang,
                        source_lang=file_source_langs.get(str(file_item.path), source_lang),
                        coverage=True,
                    ),
                    source_path=process_path,
                    output_dir=output_dir / rel_subdir,
                    plan=coverage_plan,
                    translations=global_translations,
                    target_lang=target_lang,
                    source_lang=file_source_langs.get(
                        str(file_item.path), source_lang
                    ),
                    keep_original_sheets=(
                        excel_output.keep_original_sheets and not resume_used_here
                    ),
                    formula_display_value_backfill=(
                        excel_output.formula_display_value_backfill
                    ),
                    output_translation_only=excel_output.output_translation_only,
                    lock_row_height=excel_output.lock_row_height,
                    review_marks=excel_review_marks,
                    review_mark_colors=self._settings.excel_review.mark_colors,
                    mark_review_items=(
                        self._settings.excel_review.mark_review_items
                    ),
                    existing_fill_policy=(
                        self._settings.excel_review.existing_fill_policy
                    ),
                    review_positions=file_review_positions,
                    log_callback=lambda msg: self._log(*split_log_prefix(msg)),
                    original_path=naming_original_path,
                    external_autofit_planned=need_autofit,
                    stats=write_stats,
                    output_basename=output_basename,
                    staging_dir=(process_artifacts.work_dir if process_artifacts else None),
                )
            else:
                out_path = bilingual_writer.write_bilingual_file(
                    embedded_text        = self._embedded_text_options(
                        process_path,
                        excel_output=excel_output,
                        translations=global_translations,
                        target_lang=target_lang,
                        source_lang=source_lang,
                        coverage=False,
                    ),
                    source_path          = process_path,
                    output_dir           = output_dir / rel_subdir,
                    translations         = global_translations,
                    target_lang          = target_lang,
                    source_lang          = source_lang,
                    keep_original_sheets = excel_output.keep_original_sheets,
                    formula_display_value_backfill = excel_output.formula_display_value_backfill,
                    output_translation_only = excel_output.output_translation_only,
                    lock_row_height      = excel_output.lock_row_height,
                    review_marks         = excel_review_marks,
                    review_mark_colors   = self._settings.excel_review.mark_colors,
                    mark_review_items    = self._settings.excel_review.mark_review_items,
                    existing_fill_policy = self._settings.excel_review.existing_fill_policy,
                    review_positions     = file_review_positions,
                    log_callback         = lambda msg: self._log(*split_log_prefix(msg)),
                    original_path        = naming_original_path,
                    external_autofit_planned = need_autofit,
                    stats                = write_stats,
                    output_basename      = output_basename,
                    staging_dir          = (process_artifacts.work_dir if process_artifacts else None),
                )
            embedded_parts = []
            if write_stats.get("shape_paragraphs"):
                embedded_parts.append(f"文本框 {write_stats['shape_paragraphs']} 段")
            if write_stats.get("comments"):
                embedded_parts.append(f"批注 {write_stats['comments']} 条")
            if embedded_parts:
                shrunk = int(write_stats.get("shrunk_shapes") or 0)
                self._log(
                    "INFO",
                    f"  → {file_item.name}：已翻译" + "、".join(embedded_parts)
                    + (f"；{shrunk} 个文本框的译文放不下，已缩小字号" if shrunk else ""),
                )
            if excel_output.translate_sheet_names:
                with ZipFile(out_path) as workbook_package:
                    workbook_root = etree.fromstring(workbook_package.read("xl/workbook.xml"))
                sheet_namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                sheet_names = [
                    node.get("name", "")
                    for node in workbook_root.iter(f"{{{sheet_namespace}}}sheet")
                ]
                requested_sheets = {
                    old: sheet_translations[old]
                    for old in sheet_names
                    if old in sheet_translations and sheet_translations[old] != old
                }
                if requested_sheets:
                    rename_result = rename_worksheets(out_path, out_path, requested_sheets)
                    write_stats["sheet_name_status"] = rename_result.status
                    write_stats["sheet_name_changes"] = rename_result.names
                    if rename_result.status == "preserved":
                        logger.debug(f"工作表改名保留原名原始理由 {file_item.name}：{rename_result.reason}")
                        rename_reason = user_facing_reason(
                            rename_result.reason,
                            fallback="重命名时出错，为避免损坏工作簿，已保留原名。",
                        )
                        message = f"[{file_item.name}] 工作表名称保留原名：{rename_reason}"
                        self._log("WARN", message)
                        quality_issues.append({
                            "file": file_item.name,
                            "type": "sheet_rename_preserved",
                            "severity": "needs_review",
                            "problem": "工作表名称无法安全改写",
                            "status": f"已保留原工作表名称：{rename_reason}",
                            "message": message,
                        })
                else:
                    self._log("INFO", f"[{file_item.name}] 工作表名称无可用译名，保留原名。")
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
                "source_relative_path": self._relative_source_path(file_item.path),
                "format": file_item.format,
                "conversion_mode": file_conversion_modes.get(str(file_item.path), "native_xlsx"),
                "output":  str(out_path),
                "output_path": str(out_path),
                "status": "succeeded",
                "review_count": len(file_review_positions),
                "review_items": file_review_positions,
                "sheet_name_status": write_stats.get("sheet_name_status", "unchanged"),
                "sheet_name_changes": dict(write_stats.get("sheet_name_changes") or {}),
                # 悬浮图片/形状里被我们固定住锚点的数量（0 表示没动过）
                "anchor_frozen_count": int(
                    write_stats.get("anchor_frozen_count", 0) or 0
                ),
                # 超过 Excel 32767 字符上限、被截断的单元格。用户必须看得见，
                # 否则那几格译文是残缺的而界面上毫无痕迹。
                "truncated_cells": int(
                    write_stats.get("truncated_cells", 0) or 0
                ),
                "truncated_positions": list(
                    write_stats.get("truncated_positions") or []
                ),
                "success": True,
            })
            self._file_progress.emit(
                file_item.path,
                "waiting" if need_autofit else "generated",
                "adjust" if need_autofit else "generate",
                result={
                    key: value
                    for key, value in file_results[-1].items()
                    if key not in {"source_path", "source_relative_path"}
                } if not need_autofit else None,
            )
            truncated_positions = list(
                write_stats.get("truncated_positions") or []
            )
            if truncated_positions:
                # 截断的单元格里译文是残缺的。只写日志不够——日志会被滚过去，
                # 用户拿到的是一份看起来正常的文件。
                shown = "、".join(truncated_positions[:5])
                more = (
                    f" 等 {len(truncated_positions)} 处"
                    if len(truncated_positions) > 5
                    else ""
                )
                quality_issues.append(
                    {
                        "type": "cell_text_truncated",
                        "severity": "needs_action",
                        "count": len(truncated_positions),
                        "file": file_item.name,
                        "message": (
                            f"{file_item.name} 有 {len(truncated_positions)} 个单元格超过 Excel 的 32767 字符上限，"
                            f"译文已被截断（{shown}{more}）。这些单元格的内容不完整。"
                        ),
                        "positions": truncated_positions,
                    }
                )
            self._log("OK", f"文件完成：{file_item.name}")
        except Exception as e:
            logger.debug(f"文件写入失败 {file_item.name} 原始错误：{e!r}")
            write_reason = user_facing_reason(
                e,
                fallback="译文文件未能写出，请检查输出目录的剩余空间和写入权限。",
            )
            self._log("ERROR", f"文件写入失败 {file_item.name}：{write_reason}")
            self._task_logger.file_error(file_item.name, write_reason)
            file_results.append({
                "name": file_item.name,
                "source_path": str(file_item.path),
                "source_relative_path": self._relative_source_path(file_item.path),
                "format": file_item.format,
                "conversion_mode": file_conversion_modes.get(str(file_item.path), "native_xlsx"),
                "status": "failed",
                "success": False,
                "error": write_reason,
            })
            self._file_progress.emit(
                file_item.path, "failed", "generate",
                result={"error": write_reason},
            )
        finally:
            # 清理 .xls 转换后的临时 .xlsx 文件；续译底稿是上次输出的历史产物，
            # 只读绝不删除
            is_resume_baseline = (
                fi < len(resume_baseline_used) and resume_baseline_used[fi]
            )
            _cleanup_excel_conversion_temp(
                process_path, file_item, is_resume_baseline=is_resume_baseline
            )

    def _repair_residuals(
        self,
        *,
        global_translations: dict[str, str],
        target_lang: str,
        quality_issues: list[dict],
        engine,
        rate_limited: list[ApiKeyTemporarilyUnavailableError],
        phase_total: int,
        excel_review_marks: dict[str, str],
        repair_concurrency: int = 1,
    ) -> ResidualPassResult:
        """残留中文体检 + 确定性序号修复 + 修复阶梯；改得了的直接改词典，改不了的标记复核。"""
        residual_result = run_residual_pass(
            global_translations.items(), target_lang=target_lang
        )
        if residual_result.fixes:
            global_translations.update(residual_result.fixes)
            logger.info(
                f"残留序号确定性修复 {len(residual_result.fixes)} 条"
                f"（序号惯例：{residual_result.convention}）"
            )
            self._log(
                "OK",
                f"已自动修正 {len(residual_result.fixes)} 处未翻译的编号。",
            )
            quality_issues.append(
                {
                    "type": "residual_numbering_autofixed",
                    "severity": "info",
                    "count": len(residual_result.fixes),
                    "message": (
                        f"有 {len(residual_result.fixes)} 条译文中未翻译的编号已按全文编号格式自动修正，无需处理。"
                    ),
                }
            )
        # 修复阶梯：外科修补 → 带反馈重译（验收不过绝不覆盖原译文）。
        # 上限/熔断/进度护栏在 run_repair_ladder 里，与 Word 主流程共用。
        still_needs_review = list(residual_result.needs_review)
        repair_method_counts: dict[str, int] = {}
        # 拒收理由跟着源文走：残留告警里「为什么没修上」必须逐条可查
        repair_reject_reasons: dict[str, tuple[str, ...]] = {}
        # 超限/熔断要写进结果报告：只在日志里提会被滚走，用户就不知道
        # 「这批单元根本没尝试修复，重跑还有救」
        repair_over_cap_count = 0
        repair_breaker_tripped = False
        # 停止导致的「没来得及修」跟「超限/熔断没修成」是三种不同的成因——
        # 审计批次 2 第④条：这批未完成的内容不该被说成「修复失败」，用户按了
        # 停止不算失败。判据用 ladder.stopped_count（阶梯逐单元记录「停止时还
        # 没开跑」的条数），不用事后探停止标志：停止落在最后一个单元的请求
        # 进行中时，remaining 全是真拒收、标志却已置位，事后探会把归因算反。
        repair_stopped_by_user = False
        if still_needs_review and engine_supports_chat(engine) and not rate_limited:

            def _on_repair_progress(done: int, total: int) -> None:
                # done 是已经修完（拿到回复）的条数；几条同时在修，按返回的算。
                self._translate_progress.report("repair", done, total)
                self._queue.put(StatusMsg(
                    phase_desc=(
                        f"状态：[阶段 2/{phase_total}] 正在补译遗留的原文（{done}/{total}）…"
                    )
                ))

            _on_repair_progress(0, min(len(still_needs_review), _RESIDUAL_REPAIR_MAX_UNITS))

            ladder = run_repair_ladder(
                still_needs_review,
                target_lang=target_lang,
                send=engine.chat,
                convention=residual_result.convention,
                max_units=_RESIDUAL_REPAIR_MAX_UNITS,
                breaker_threshold=_RESIDUAL_REPAIR_BREAKER_THRESHOLD,
                should_stop=self._stop_event.is_set,
                on_progress=_on_repair_progress,
                max_workers=repair_concurrency,
            )
            if ladder.over_cap_count:
                self._log(
                    "WARN",
                    (
                        f"遗留原文的译文有 {len(still_needs_review)} 条，超出单次自动补译上限（{_RESIDUAL_REPAIR_MAX_UNITS} 条），"
                        f"超出的 {ladder.over_cap_count} 条本次未自动补译，已标为待复核。"
                    ),
                )
            if ladder.breaker_tripped:
                self._log(
                    "WARN",
                    (
                        f"自动补译连续 {_RESIDUAL_REPAIR_BREAKER_THRESHOLD} 次失败，"
                        "已停止自动补译，剩下的已标为待复核。"
                    ),
                )
            global_translations.update(ladder.accepted)
            repair_method_counts = ladder.method_counts
            repair_reject_reasons = ladder.reject_reasons
            repair_over_cap_count = ladder.over_cap_count
            repair_breaker_tripped = ladder.breaker_tripped
            still_needs_review = ladder.remaining
            # 跟「超限」「熔断」不冲突，三个成因可能同时出现，下面拼消息时各说各的。
            repair_stopped_by_user = ladder.stopped_count > 0
        repaired_total = sum(repair_method_counts.values())
        if repaired_total:
            method_desc = "、".join(
                f"{_RESIDUAL_REPAIR_METHOD_LABELS.get(method, method)} {count} 处"
                for method, count in sorted(repair_method_counts.items())
            )
            self._log("OK", f"已自动补译 {repaired_total} 处遗留的原文（{method_desc}）")
            quality_issues.append(
                {
                    "type": "residual_repaired",
                    "severity": "info",
                    "count": repaired_total,
                    "message": (
                        f"有 {repaired_total} 条译文中遗留的原文已自动补译（{method_desc}），均已通过自动检查。"
                    ),
                }
            )
        if still_needs_review:
            for unit in still_needs_review:
                _set_excel_review_mark(
                    excel_review_marks,
                    unit.source_text,
                    MIXED_MARK_UNRESOLVED,
                )
            quality_issues.append(
                {
                    "type": "residual_source_language",
                    "severity": "needs_action",
                    "count": len(still_needs_review),
                    # 附加的机器可判字段，不改动 type/severity：报告/界面想按「是不是
                    # 停止导致的」单独处理时读这个字段，不用去猜 message 里的措辞；
                    # 旧任务记录没有这个键，读取方按缺省 False（当作非停止）处理即可，
                    # 不影响旧数据可读——新增字段只加不改，兼容旧结果契约。
                    "caused_by_stop": repair_stopped_by_user,
                    "message": (
                        f"有 {len(still_needs_review)} 条译文中仍有未翻译的原文片段，"
                        + (
                            # 停止不是失败：这批是任务被停止时还没轮到自动修复的部分，
                            # 不是「模型改了还是不对」。放在最前面说，不然用户会把它跟
                            # 下面「熔断/超限」这些真正尝试过、没修成的情况混为一谈。
                            "其中部分内容在任务停止时尚未自动补译（并非补译失败），"
                            if repair_stopped_by_user
                            else ""
                        )
                        + (
                            # 补译模式的输出文件不带标记列；双语模式下用户
                            # 也可能关掉「标记需复核内容」——文件里真没标记
                            # 时不许许诺有标记。停止时已翻好的内容照常写盘，
                            # 标记也跟着写进去，这里不再按停止另说。
                            "请按下方源文清单逐条核对。"
                            if self._untranslated_only
                            or not self._settings.excel_review.mark_review_items
                            else "已在输出文件中标记需复核的位置。"
                        )
                        + (
                            f"其中 {repair_over_cap_count} 条超出单次自动补译上限（{_RESIDUAL_REPAIR_MAX_UNITS} 条），本次未尝试补译，"
                            "重新运行任务可继续补译。"
                            if repair_over_cap_count
                            else ""
                        )
                        + (
                            "自动补译连续失败，已停止，部分内容未尝试补译；"
                            "请确认网络和模型连接正常后重新运行任务，以继续补译。"
                            if repair_breaker_tripped
                            else ""
                        )
                        + (
                            # 停止后续译只补还是原文的格子，这批「译文里夹着原文」
                            # 的格子不会再进补译清单；要自动补译它们得整份重新
                            # 翻译（已入库的译文会经记忆库直接复用）。
                            "全部重新翻译时，已完成的译文将直接从记忆库取用，"
                            "遗留的原文将重新尝试自动补译。"
                            if repair_stopped_by_user
                            and not repair_over_cap_count
                            and not repair_breaker_tripped
                            else ""
                        )
                    ),
                    "failed_sources": [
                        {
                            "source": unit.source_text,
                            "error": (
                                "译文中未翻译的原文："
                                + "、".join(f"«{span}»" for span in unit.spans)
                                + (
                                    "；自动补译没有通过："
                                    + "；".join(
                                        repair_reject_reasons[unit.source_text]
                                    )
                                    if unit.source_text in repair_reject_reasons
                                    else ""
                                )
                            ),
                        }
                        for unit in still_needs_review
                    ],
                }
            )
            # 运行日志同样要把「停止」和「修复失败」分开说——面板里这条 WARN 是用户
            # 唯一能看到的第一手记录，跟上面 quality_issues 的 message 措辞保持一致，
            # 不能一边在报告里说「不是修复失败」，日志这行却只字不提成因。级别仍用
            # WARN：不管是不是因为停止，这批内容都还需要用户处理，不该看起来比真实
            # 状态更轻。
            self._log(
                "WARN",
                (
                    f"有 {len(still_needs_review)} 条译文中仍有未翻译的原文，"
                    + (
                        "其中部分内容在任务停止时尚未自动补译，"
                        if repair_stopped_by_user
                        else ""
                    )
                    + "已列入待复核。"
                ),
            )
        if residual_result.released_notes:
            # 万/亿等数量单位残留：不拦发布，但报告里留痕
            quality_issues.append(
                {
                    "type": "residual_quantity_unit",
                    "severity": "info",
                    "count": len(residual_result.released_notes),
                    "message": (
                        f"有 {len(residual_result.released_notes)} 条译文保留了「万/亿」等数量单位，通常不影响理解；如需统一，"
                        "请手动调整。"
                    ),
                }
            )
        return residual_result

    def _normalize_heading_style(
        self,
        *,
        global_translations: dict[str, str],
        target_lang: str,
        quality_issues: list[dict],
    ) -> str | None:
        """全篇节标题写法巡检：多数派可确定性改写时直接改词典，返回多数派写法。"""
        heading_observations = [
            (source, target, source)
            for source, target in global_translations.items()
            if is_section_heading_source(source)
        ]
        # 全篇多数派要贯通到 TM 入库口：TM 只拿到 API 未命中子集，
        # 让它自己投票可能与全篇结论相反，把库改成与交付文件相左的写法
        heading_majority: str | None = None
        if heading_observations:
            heading_consistency = check_heading_consistency(
                heading_observations, target_lang=target_lang
            )
            heading_majority = heading_consistency.majority_form
            if heading_consistency.fixes:
                global_translations.update(heading_consistency.fixes)
                self._log(
                    "OK",
                    (
                        f"已按全文最常见的写法统一 {len(heading_consistency.fixes)} 处章节标题。"
                    ),
                )
                quality_issues.append(
                    {
                        "type": "heading_style_normalized",
                        "severity": "info",
                        "count": len(heading_consistency.fixes),
                        "message": (
                            f"有 {len(heading_consistency.fixes)} 条章节标题译文的写法"
                            "与全文多数不一致，已自动统一，无需处理。"
                        ),
                    }
                )
            elif heading_consistency.outliers:
                # 多数派是序数词写法时不做词形猜测，只报告不改
                quality_issues.append(
                    {
                        "type": "heading_style_outliers",
                        "severity": "info",
                        "count": len(heading_consistency.outliers),
                        "message": (
                            f"有 {len(heading_consistency.outliers)} 条章节标题译文与全文多数写法不一致；统一这些写法需要改动词形，"
                            "因此未自动改写；如需统一，请手动调整。"
                        ),
                    }
                )
        return heading_majority

    def _store_final_translations_in_tm(
        self,
        *,
        normal_api_language_results: dict,
        normal_api_translations: dict[str, str],
        auto_source_lang: bool,
        engine,
        global_translations: dict[str, str],
        heading_majority: str | None,
        lang_pair: str | None,
        max_len: int,
        residual_result: ResidualPassResult,
        target_lang: str,
        text_source_scopes: dict[str, list[frozenset[str]]],
        quality_issues: list[dict],
    ) -> None:
        """把本次 API 结果按「文件最终译文」写进 TM，失败记成需处理的质量问题。"""
        if normal_api_translations or normal_api_language_results:
            written, tm_write_error = self.store_api_results_in_tm(
                auto_source_lang=auto_source_lang,
                normal_api_language_results=normal_api_language_results,
                normal_api_translations=normal_api_translations,
                text_source_scopes=text_source_scopes,
                target_lang=target_lang,
                lang_pair=lang_pair,
                max_len=max_len,
                engine_name=engine.engine_name,
                final_translations=global_translations,
                convention=residual_result.convention,
                heading_majority=heading_majority,
            )
            if tm_write_error:
                self._log("WARN", tm_write_error)
                self._task_logger.warning(tm_write_error)
                quality_issues.append(
                    {
                        "type": "tm_write_failed",
                        # 写 TM 是真的失败了，不是「按设计如此」的提示。severity=info
                        # 已经被界面定义成「用户不用管」的一档（自动目录那类），这一条
                        # 留在 info 里会连着 KPI 一起被抹掉。
                        "severity": "needs_action",
                        "message": tm_write_error,
                    }
                )
            if written:
                self._log("INFO", f"已存入记忆库 {written} 条")

    def _run_autofit_phase(
        self,
        *,
        need_autofit: bool,
        file_results: list[dict],
        reuse_excel_for_autofit: bool,
        phase_total: int,
        excel_policy: str,
        _get_excel_app,
        _run_autofit_with_guard,
    ) -> None:
        """阶段 4：用 Excel 对已写好的双语表批量 AutoFit 行高（复用或新起 Excel 进程）。"""
        if need_autofit:
            out_paths = [
                Path(r["output"]) for r in file_results
                if r.get("success") and r.get("output")
            ]
            if out_paths:
                autofit_results_by_path = {
                    Path(result["output"]).resolve(): result
                    for result in file_results
                    if result.get("success") and result.get("output")
                }
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 正在自动调整行高…"))
                self._queue.put(ProgressMsg(
                    phase_index=4,
                    phase_total=phase_total,
                    phase_name="调整行高",
                    step_done=0,
                    step_total=len(out_paths),
                ))
                t0 = datetime.now()
                logger.info(f"开始 Excel AutoFit，共 {len(out_paths)} 个文件 | policy={excel_policy}")
                self._log("INFO", f"开始自动调整行高，共 {len(out_paths)} 个文件")

                def autofit_file_start_cb(current_file):
                    result = autofit_results_by_path.get(
                        Path(current_file).resolve()
                    )
                    if result is not None:
                        self._file_progress.emit(
                            result.get("source_path"), "active", "adjust"
                        )

                def autofit_progress_cb(done, total, current_file):
                    if current_file is not None:
                        result = autofit_results_by_path.get(
                            Path(current_file).resolve()
                        )
                        if result is not None:
                            self._file_progress.emit(
                                result.get("source_path"),
                                "generated",
                                "adjust",
                                result={
                                    key: value
                                    for key, value in result.items()
                                    if key not in {"source_path", "source_relative_path"}
                                },
                            )
                    self._queue.put(ProgressMsg(
                        phase_index=4,
                        phase_total=phase_total,
                        phase_name="调整行高",
                        step_done=done,
                        step_total=total,
                    ))
                    if current_file is not None and done < total:
                        self._queue.put(StatusMsg(
                            phase_desc=f"状态：[阶段 4/{phase_total}] 正在调整行高：{current_file.name}"
                        ))

                if reuse_excel_for_autofit:
                    app = _get_excel_app()
                    autofit_success = bilingual_writer.autofit_files_batch(
                        out_paths,
                        app=app,
                        log_callback=lambda msg: self._log(*split_log_prefix(msg)),
                        progress_callback=autofit_progress_cb,
                        file_start_callback=autofit_file_start_cb,
                    )
                else:
                    autofit_success = _run_autofit_with_guard(
                        out_paths,
                        autofit_progress_cb,
                        autofit_file_start_cb,
                    )

                self._queue.put(ProgressMsg(
                    phase_index=4,
                    phase_total=phase_total,
                    phase_name="调整行高",
                    step_done=len(out_paths),
                    step_total=len(out_paths),
                ))
                autofit_elapsed = (datetime.now() - t0).total_seconds()
                for result in file_results:
                    if result.get("success") and result.get("output"):
                        self._file_progress.emit(
                            result.get("source_path"),
                            "generated",
                            "adjust",
                            result={
                                **{
                                    key: value
                                    for key, value in result.items()
                                    if key not in {"source_path", "source_relative_path"}
                                },
                            },
                        )
                if autofit_success:
                    self._log("INFO", "自动调整行高完成")
                    self._task_logger.info(
                        f"批量AutoFit完成 | 文件数={len(out_paths)} | 耗时={autofit_elapsed:.3f}s | excel_policy={excel_policy}"
                    )
                else:
                    self._log("WARN", "Excel 未能完成自动调整行高，未调整的部分已按估算行高保存。")
                    self._task_logger.warning(
                        f"批量AutoFit未完全完成 | 文件数={len(out_paths)} | 耗时={autofit_elapsed:.3f}s | excel_policy={excel_policy}"
                    )
            else:
                for result in file_results:
                    if result.get("success") and result.get("output"):
                        self._file_progress.emit(
                            result.get("source_path"), "generated", "generate",
                            result={
                                key: value
                                for key, value in result.items()
                                if key not in {"source_path", "source_relative_path"}
                            },
                        )
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 4/{phase_total}] 没有需要调整行高的文件，已跳过。"))
                self._queue.put(ProgressMsg(
                    phase_index=4,
                    phase_total=phase_total,
                    phase_name="调整行高",
                    step_done=1,
                    step_total=1,
                ))

    def store_api_results_in_tm(self, **kwargs) -> tuple[int, str | None]:
        """Write TM without ever letting that write kill a finished translation.

        TM insertion is bookkeeping that runs *after* every API call has been
        paid for and *before* the translated files are written.  A locked
        database — someone running a long write on the glossary page — used to
        raise straight out of the worker thread and take the whole task with
        it, so any failure here becomes a warning plus a result note instead.
        """
        try:
            return self._write_api_results_to_tm(**kwargs), None
        except Exception as tm_error:  # noqa: BLE001 - never lose finished work
            logger.warning(f"记忆库写入失败原始错误：{tm_error!r}")
            message = (
                "记忆库写入失败。本次译文不受影响，但该批译文未保存到记忆库："
                + user_facing_reason(tm_error, fallback="记忆库暂时无法写入。")
            )
            return 0, message

    def _write_api_results_to_tm(
        self,
        *,
        auto_source_lang: bool,
        normal_api_language_results: dict,
        normal_api_translations: dict,
        text_source_scopes: dict,
        target_lang: str,
        lang_pair,
        max_len: int,
        engine_name: str,
        final_translations: dict | None = None,
        convention: str = "",
        heading_majority: str | None = None,
    ) -> int:
        """Store this run's API results in TM and return the entry count.

        ``final_translations`` 是主流程改完（序号修复/修复阶梯/标题归一）
        之后的全局词典：入库必须以它为准，API 原始返回只是兜底。
        ``convention`` 是全篇投出的序号惯例，``heading_majority`` 是全篇
        投出的节标题写法多数派——卫生层子集都不再自行投票，避免与交付
        文件相左。
        """
        finals = final_translations or {}

        def _final_text(source_text: str, fallback: str) -> str:
            value = finals.get(source_text)
            return value if isinstance(value, str) and value.strip() else fallback

        if auto_source_lang:
            # The model-reported item source language is the final gate.
            # ``mixed``/``und``/``auto`` and anything outside that file's
            # preflight scope are rejected by TM manager.
            hygiene = sanitize_tm_pairs(
                (
                    (source_text, _final_text(source_text, item.translation))
                    for source_text, item in normal_api_language_results.items()
                ),
                target_lang=target_lang,
                convention=convention,
                heading_majority=heading_majority,
            )
            self._log_tm_hygiene(hygiene)
            normalized_targets = dict(hygiene.pairs)
            rejected_sources = {source for source, _reason in hygiene.rejected}
            tm_entries = []
            for source_text, item in normal_api_language_results.items():
                translation = normalized_targets.get(
                    source_text, _final_text(source_text, item.translation)
                )
                source_scopes = text_source_scopes.get(source_text)
                if source_scopes is None:
                    # 这个词条从未出现在预检前的全文扫描样本里——它是补译计划
                    # 拿到真实源语言之后重算才冒出来的新词条（低-
                    # text_source_scopes）。没有跨文件源语言冲突的证据可查，
                    # 不该被下面这条「每个出现过的文件都得认同源语言」的一致
                    # 性校验保守拒收，否则这类新词条永远进不了 TM。
                    source_in_every_file_scope = True
                else:
                    source_in_every_file_scope = bool(source_scopes) and all(
                        item.source_lang in scope for scope in source_scopes
                    )
                tm_entries.append(
                    {
                        "source_text": source_text,
                        "translation": translation,
                        "source_lang": item.source_lang,
                        "tm_eligible": item.tm_eligible
                        and source_text not in rejected_sources
                        and source_in_every_file_scope
                        and should_store_translation_in_tm(source_text, translation)
                        and not self._smart_skips_tm(source_text),
                    }
                )
            return tm_manager.insert_auto_entries(
                tm_entries,
                target_lang,
                max_len,
                engine_name,
                task_id=self.task_id,
            )

        new_pairs = [
            (k, _final_text(k, v))
            for k, v in normal_api_translations.items()
            if should_store_translation_in_tm(k, _final_text(k, v)) and not self._smart_skips_tm(k)
        ]
        hygiene = sanitize_tm_pairs(
            new_pairs,
            target_lang=target_lang,
            convention=convention,
            heading_majority=heading_majority,
        )
        self._log_tm_hygiene(hygiene)
        return tm_manager.insert_batch(
            list(hygiene.pairs),
            lang_pair,
            max_len,
            engine_name,
            sync_reverse=False,
        )

    def _smart_skips_tm(self, source_text: str) -> bool:
        """「自动识别场景」下，离开上下文就会译错的短词不自动存进记忆库。"""
        return bool(getattr(self, "_smart_mode", False)) and is_context_sensitive(source_text)

    def _log_tm_hygiene(self, hygiene) -> None:
        """入库卫生留痕：文案在 core/tm_hygiene 维护一份（Word 共用）。"""
        for level, message in tm_hygiene_log_lines(hygiene):
            self._log(level, message)

    def _arbitrate_excel_coverage_pairs(
        self,
        coverage_plan,
        *,
        engine,
        api_scheduler,
        target_lang: str,
        source_lang: str,
        lang_pair: str | None,
        concurrency: int,
        file_name: str,
        quality_issues: list[dict],
        review_marks: dict[str, str],
    ) -> None:
        """复核「这一格里的译文真的是这一格原文的译文吗」，判错的打回去重新翻译。

        判定与模型接线在 :mod:`core.coverage_review`，和 Word 是同一份——同一份内容
        换个格式跑，不该有两种复核尺度。这里只负责把结论翻成 Excel 的报告条目和底色。
        """
        outcome = arbitrate_coverage_units(
            coverage_plan.units,
            engine=engine,
            api_scheduler=api_scheduler,
            target_lang=target_lang,
            source_lang=source_lang,
            lang_pair=lang_pair,
            concurrency=concurrency,
            file_name=file_name,
            log=self._log,
            stop_event=self._stop_event,
            scheduler_label="Excel 补译复核",
        )
        if outcome is None:
            return
        for review in outcome.retranslated:
            unit = review.unit
            judged = review.reason == RETRANSLATE_MODEL
            if judged:
                # 模型判了"这一格里的译文说的是另一回事"：程序只能在原有内容后面再补
                # 一条译文，没法替用户判断旧的那条是错配、是从别的格子复制过来的、
                # 还是压根不该在这儿。要对着原件看，所以归进「疑似原文异常」上底色。
                _set_excel_review_mark(
                    review_marks, unit.source_text, MIXED_MARK_FOREIGN_NOISE
                )
            # 没问出结果的那批不上底色：接口抖一下就是整整一批落到 uncertain，
            # 全涂上底色，用户点开发现全是好好的译文——底色一失信，真正判出问题的
            # 那几格就跟着一起被跳过了。这一类只在报告里留一行。
            quality_issues.append(
                {
                    "file": file_name,
                    "sheet": str(unit.data.get("sheet") or ""),
                    "cell": str(unit.data.get("coordinate") or ""),
                    "location": unit.location,
                    "snippet": _build_excel_source_excerpt(unit.source_text),
                    "problem": (
                        "同一单元格中的译文与该单元格原文不对应"
                        if judged
                        else "无法判定同一单元格中的译文是否与该单元格原文对应"
                    ),
                    "status": (
                        (
                            "已按未翻译处理并补充译文；原有内容保持不变，但很可能不是该单元格的译文，"
                            "请对照源文件人工核对并修改或删除。"
                        )
                        if judged
                        else (
                            "未取得判定结果，已从严按未翻译处理并补上译文，"
                            "原有内容保持不变。"
                        )
                    ),
                    "severity": "needs_review" if judged else "resolved",
                }
            )

    def _log_excel_coverage_plan(
        self, file_name: str, plan, *, resume_baseline: bool = False
    ) -> None:
        """Report one file's untranslated-only plan, including the empty case."""
        summary = plan.summary
        self._log(
            "INFO",
            (
                "  → 补译识别："
                f"待补 {summary.get('source_only', 0)}，"
                f"已覆盖 {summary.get('covered', 0)}，"
                f"不确定跳过 {summary.get('ambiguous', 0)}"
            ),
        )
        self._log_ignored_coverage_units(file_name, plan.units)
        # 「一条都不用补」是合法结果，但普通补译时那样输出文件会和原文一模一样。
        # 不明说的话，用户看到的就是一份没翻译的文件，看不出是「本来就不用翻」
        # 还是程序没干活。续译时计划建在上次的双语底稿上，「不用补」意味着上次
        # 已经翻完了——输出等于上次产物（不是原文），这是好消息不是警告。
        if not summary.get("source_only", 0):
            if resume_baseline:
                self._log(
                    "INFO",
                    f"  → {file_name}：上次的翻译已覆盖全部内容，无需补译，"
                    "输出与上次的翻译结果一致。",
                )
            else:
                self._log(
                    "WARN",
                    f"  → {file_name}：未找到需要补译的内容，输出文件将与原文一致。"
                    "如该文件尚未翻译过，请关闭「仅补译未翻译内容」后重新运行。",
                )

    def _log_ignored_coverage_units(self, file_name: str, units: list) -> None:
        """按理由分组回报补译计划里被跳过（ignored）的格子——审计批次 2 第④条。

        旧版补译日志只报 covered / source_only / ambiguous 三类计数，ignored（看起来
        已经是译文的格、不符合候选规则的格、回填关闭时被保护的公式格）从不出现在日志
        里：用户看到"补了几格"，看不到"跳过了几格、为什么跳过"，翻完对着原表发现漏译
        也无从查起。这里补上这条自查线索，语气是"如实告知"，不是"报警"——大多数
        ignored 本来就是正确判断（已经是双语的格不用再翻、用户关了回填的公式格不该
        被覆盖），只有当用户凭位置去核对、发现误判时，这条线索才用得上。

        零 ignored 时一行都不出：仓库立场是"无关提示不挂"，见 CLAUDE.md。
        """
        lines, full_detail = format_ignored_coverage_report(file_name, units)
        for line in lines:
            self._log("INFO", line)
        # 全量坐标只进 loguru 调试输出（stderr，本仓库没配文件 sink）：任务面板
        # 不按 level 过滤，发进任务日志的话会跟分组摘要一起整段刷在用户面前，
        # 反而淹掉摘要。打包用户看不到 stderr——这是取舍：全量明细服务于开发排
        # 查，用户核对靠摘要里的样例位置就够。
        if full_detail:
            logger.debug(full_detail)

    def _apply_deferred_resume_baselines(
        self,
        *,
        process_paths: list[Path],
        deferred_resume_baselines: list[Path | None],
        resume_baseline_used: list[bool],
        file_results: list[dict],
        target_lang: str,
        source_lang: str,
        formula_display_value_backfill: bool,
        file_source_langs: dict[str, str] | None = None,
    ) -> None:
        """自动识别源语言的续译：预检定完语言、重算补译清单之前，把底稿换上。

        阶段 1 为了让语言预检拿到干净的源文样本，一直按源文件（或其 .xls 转换件）
        取词。现在源语言已定，补译清单马上要按 process_paths 重算——此刻换成上次
        的双语底稿，重算出来的才是「还缺哪些译文」，而不是把整份原文再翻一遍。

        换之前先做底稿资格核查（baseline_missing_source_texts）：源文件在上次翻译
        之后新增的内容不在底稿里，硬换底稿会把它静默丢掉。核查放在这里而不是阶段 1，
        是因为文本分类依赖源语言——此刻语言刚定下来，核查结果才作数。
        """
        from core.resume_detection import (
            baseline_missing_source_texts,
            describe_baseline_divergence,
        )

        failed_sources = {
            r.get("source_path") for r in file_results if not r.get("success")
        }
        for fi, file_item in enumerate(self._files):
            if fi >= len(process_paths) or fi >= len(deferred_resume_baselines):
                continue
            baseline = deferred_resume_baselines[fi]
            if baseline is None or str(file_item.path) in failed_sources:
                continue
            previous = process_paths[fi]
            missing = baseline_missing_source_texts(
                previous,
                baseline,
                surface="excel",
                target_lang=target_lang,
                source_lang=(file_source_langs or {}).get(str(file_item.path), source_lang),
                formula_display_value_backfill=formula_display_value_backfill,
            )
            if missing:
                self._log(
                    "WARN",
                    f"继续上次翻译：{file_item.name} 的源文件比上次翻译时{describe_baseline_divergence(missing)}，"
                    "无法在上次结果的基础上补译，"
                    "本次将完整翻译（已翻译的内容将尽量直接从记忆库调用）。",
                )
                continue
            process_paths[fi] = baseline
            if fi < len(resume_baseline_used):
                resume_baseline_used[fi] = True
            self._log(
                "INFO",
                f"继续上次翻译：{file_item.name} 在上次的翻译结果 {baseline.name} 上补译",
            )
            # 阶段 1 若做过 .xls → .xlsx 转换，那个临时件从此用不上了
            if previous != file_item.path and previous != baseline:
                try:
                    os.remove(previous)
                except Exception as e:
                    self._log(
                        "WARN",
                        f"临时文件清理失败 {previous.name}: "
                        f"{user_facing_reason(e, fallback='无法删除临时文件。')}",
                    )

    def _rebuild_coverage_plans_after_preflight(
        self,
        *,
        process_paths: list[Path],
        coverage_plans: list,
        file_texts: list,
        file_results: list[dict],
        file_conversion_modes: dict[str, str],
        global_unique_texts: set[str],
        resume_baseline_used: list[bool],
        target_lang: str,
        source_lang: str,
        formula_display_value_backfill: bool,
        file_source_langs: dict[str, str] | None = None,
        engine=None,
        api_scheduler=None,
        lang_pair: str | None = None,
        concurrency: int = 4,
        quality_issues: list[dict] | None = None,
        review_marks: dict[str, str] | None = None,
    ) -> int:
        """Build the untranslated-only plans now that auto-detect settled the language.

        补译判定得先知道源语言是哪一门，才分得清「原文」和「已经翻好的译文」；而
        自动识别要等词条提出来、发给模型问过一轮才有答案。所以自动识别 + 补译时，
        阶段 1 先按普通方式取一遍全量候选当预检样本，真正的待补清单留到这里重算。
        返回重算后的候选文本总数（Excel 进程策略要用）。
        """
        global_unique_texts.clear()
        raw_text_count = 0
        file_source_langs = file_source_langs or {}
        detected_langs = sorted(set(file_source_langs.values())) or [source_lang]
        self._log(
            "INFO",
            "[补译模式] 原文语言已识别为 "
            + "、".join(get_source_lang_display(code) for code in detected_langs)
            + "，开始按各文件的语言重新识别需补译的内容。",
        )
        for fi, file_item in enumerate(self._files):
            if fi >= len(process_paths) or fi >= len(coverage_plans):
                continue
            file_source_lang = file_source_langs.get(str(file_item.path), source_lang)
            if any(
                r.get("source_path") == str(file_item.path) and not r.get("success")
                for r in file_results
            ):
                continue
            try:
                coverage_plan = build_excel_coverage_plan(
                    process_paths[fi],
                    target_lang=target_lang,
                    source_lang=file_source_lang,
                    formula_display_value_backfill=formula_display_value_backfill,
                    original_sheet_names=(
                        workbook_sheet_titles(file_item.path)
                        if fi < len(resume_baseline_used) and resume_baseline_used[fi]
                        else ()
                    ),
                )
            except Exception as e:  # noqa: BLE001 - 单个文件读失败不该带走整批
                # 刚被 _apply_deferred_resume_baselines 换上的底稿探活能过、建计划
                # 才炸：底稿不行不等于源文件不行，先退回源文件重建一次，只有源
                # 文件也读不出来才标失败。（.xls 源的转换件在换底稿时已被清理，
                # 退无可退，维持失败。）
                degraded = False
                if (
                    fi < len(resume_baseline_used)
                    and resume_baseline_used[fi]
                    and file_item.path.suffix.lower() != ".xls"
                ):
                    logger.debug(f"续译底稿建计划失败原始错误 {file_item.name}：{e!r}")
                    self._log(
                        "WARN",
                        f"{file_item.name} 无法基于上次的结果补译，本次将从头完整翻译："
                        + user_facing_reason(e, fallback="无法读取上次的翻译结果。"),
                    )
                    process_paths[fi] = file_item.path
                    resume_baseline_used[fi] = False
                    try:
                        coverage_plan = build_excel_coverage_plan(
                            file_item.path,
                            target_lang=target_lang,
                            source_lang=file_source_lang,
                            formula_display_value_backfill=formula_display_value_backfill,
                        )
                        degraded = True
                    except Exception as retry_error:  # noqa: BLE001
                        e = retry_error
                if not degraded:
                    fail_label = (
                        "上次的翻译结果读取失败"
                        if fi < len(resume_baseline_used) and resume_baseline_used[fi]
                        else "补译识别失败"
                    )
                    logger.debug(f"{fail_label}原始错误 {file_item.name}：{e!r}")
                    reason = user_facing_reason(
                        e,
                        fallback="无法打开该文件，可能已损坏或不是有效的 Excel 文件。",
                    )
                    self._log("ERROR", f"{fail_label} {file_item.name}：{reason}")
                    self._task_logger.file_error(file_item.name, f"{fail_label}: {reason}")
                    file_results.append({
                        "name": file_item.name,
                        "source_path": str(file_item.path),
                        "source_relative_path": self._relative_source_path(file_item.path),
                        "format": file_item.format,
                        "conversion_mode": file_conversion_modes.get(
                            str(file_item.path), "native_xlsx"
                        ),
                        "status": "failed",
                        "success": False,
                        "error": f"{fail_label}: {reason}",
                    })
                    file_texts[fi] = set()
                    continue
            if engine is not None:
                self._arbitrate_excel_coverage_pairs(
                    coverage_plan,
                    engine=engine,
                    api_scheduler=api_scheduler,
                    target_lang=target_lang,
                    source_lang=file_source_lang,
                    lang_pair=lang_pair,
                    concurrency=concurrency,
                    file_name=file_item.name,
                    quality_issues=quality_issues if quality_issues is not None else [],
                    review_marks=review_marks if review_marks is not None else {},
                )
            coverage_plans[fi] = coverage_plan
            text_set = set(coverage_plan.source_texts)
            text_set |= self._collect_embedded_texts(
                process_paths[fi],
                file_item.name,
                excel_output=self._settings.excel_output,
                target_lang=target_lang,
                source_lang=file_source_lang,
                coverage=True,
            )
            file_texts[fi] = text_set
            raw_text_count += len(text_set)
            global_unique_texts.update(text_set)
            self._log("INFO", f"[补译识别] {file_item.name}")
            self._log_excel_coverage_plan(
                file_item.name,
                coverage_plan,
                resume_baseline=(
                    fi < len(resume_baseline_used) and resume_baseline_used[fi]
                ),
            )
        self._log(
            "OK",
            f"[补译识别完成] 全部文件合计 {len(global_unique_texts)} 处待补译文本。",
        )
        return raw_text_count

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

    def _relative_source_path(self, path: Path) -> str:
        """Return a stable, user-facing relative path for a file result."""
        root = self._source_root
        if root is not None:
            try:
                return str(path.relative_to(root))
            except ValueError:
                pass
        return path.name

    def _build_result_contract(
        self,
        *,
        file_results: list[dict],
        output_dir: str,
        tm_hit_count: int,
        api_text_count: int,
        elapsed_sec: float,
        stopped: bool,
        source_lang: str,
        target_lang: str,
        preflights: dict,
        file_texts: list[set[str]],
        actual_results: dict,
        review_marks: dict[str, str],
        error_message: str = "",
    ) -> dict[str, object]:
        """Build the Phase 4 result contract without exposing document text."""
        files = [dict(item) for item in file_results]
        terminal_sources = {str(item.get("source_path") or "") for item in files}
        # 停止和整批失败（持续限流、上游不可用……）都会在写盘前收尾：此时 file_results
        # 是空的，不补上这些文件，KPI 就成了「已选 2 / 已生成 0 / 未生成 0」，而同一屏
        # 的文件清单里两行都写着未生成。
        if stopped or error_message:
            for item in self._files:
                if str(item.path) in terminal_sources:
                    continue
                files.append(
                    {
                        "name": item.name,
                        "source_path": str(item.path),
                        "source_relative_path": self._relative_source_path(item.path),
                        "format": getattr(item, "format", item.path.suffix.lstrip(".")),
                        "conversion_mode": "not_started",
                        "status": "unstarted" if stopped else "failed",
                        "success": False,
                        "error": "任务在开始该文件前已停止。" if stopped else "任务整体失败，未输出此文件。",
                    }
                )

        actual_by_file: list[dict[str, object]] = []
        for index, item in enumerate(self._files):
            preflight = preflights.get(str(item.path))
            counts: dict[str, int] = {}
            for text in file_texts[index] if index < len(file_texts) else ():
                actual = actual_results.get(text)
                code = str(getattr(actual, "source_lang", "") or "")
                if code:
                    counts[code] = counts.get(code, 0) + 1
            actual_by_file.append(
                {
                    "source_path": self._relative_source_path(item.path),
                    "preflight": (
                        preflight.to_dict(target_lang)
                        if preflight is not None
                        else {
                            "source_langs": [source_lang]
                            if source_lang != "auto"
                            else [],
                            "requested": False,
                            "request_count": 0,
                        }
                    ),
                    "actual_source_counts": counts,
                }
            )

        review_counts: dict[str, int] = {}
        review_items: list[dict[str, object]] = []
        for file_item in files:
            for item in file_item.get("review_items", []) or []:
                if not isinstance(item, dict):
                    continue
                category = str(item.get("category") or "")
                if category:
                    review_counts[category] = review_counts.get(category, 0) + 1
                review_items.append(
                    {
                        "file": file_item.get("source_relative_path", ""),
                        "worksheet": item.get("worksheet", ""),
                        "cell": item.get("cell", ""),
                        "category": category,
                        "action": item.get("action", ""),
                    }
                )
        if not review_items:
            for mark in review_marks.values():
                review_counts[mark] = review_counts.get(mark, 0) + 1
        succeeded = sum(1 for item in files if item.get("status") == "succeeded")
        failed = sum(1 for item in files if item.get("status") == "failed")
        unstarted = sum(1 for item in files if item.get("status") == "unstarted")
        return {
            "files": files,
            "kpi": {
                "selected_file_count": len(self._files),
                "succeeded_file_count": succeeded,
                "failed_file_count": failed,
                "unstarted_file_count": unstarted,
                "output_dir": output_dir,
                "elapsed_sec": elapsed_sec,
                "tm_hit_count": tm_hit_count,
                "model_translation_text_count": api_text_count,
            },
            "review": {
                "counts": review_counts,
                "total_count": sum(review_counts.values()),
                "items": review_items,
            },
            # Which connection actually did the work. A run that failed over
            # halfway was attributed entirely to the connection frozen at start,
            # so a user comparing two runs' quality had no way to see that the
            # second half came from a different provider.
            "connections": self._connection_switch_summary(),
            "language": {
                "mode": "automatic" if self._source_lang == "auto" else "manual",
                "selected_source_lang": self._source_lang,
                "target_lang": target_lang,
                "files": actual_by_file,
            },
            "error": {"message": error_message} if error_message else {},
        }

    def _record_unfinished_after_stop(
        self,
        file_results: list[dict],
        *,
        target_lang: str,
        source_lang: str,
        formula_display_value_backfill: bool,
    ) -> int | None:
        """停止后写出的文件逐个数「还是原文的格子」，记在逐文件结果上并返回合计。

        口径与续译检测完全相同（core.resume_detection.count_untranslated_in_output）：
        停止横幅说「还有 N 格没翻」，重新扫描后的续译弹窗说「补 N 格」，两个数是同一个数。
        文件状态也据此从「已生成」改成「部分翻译」，不再让翻了一半的文件看起来像翻完了。
        任何一个写出的文件数不出来就返回 None：合计少了它就是个凑出来的数。
        """
        from core.resume_detection import count_untranslated_in_output

        total: int | None = 0
        for entry in file_results:
            if not entry.get("success"):
                continue
            output = str(entry.get("output_path") or entry.get("output") or "")
            source = str(entry.get("source_path") or "")
            if not source:
                total = None
                continue
            count = None if not output else count_untranslated_in_output(
                "excel",
                output,
                source_path=source,
                target_lang=target_lang,
                source_lang=source_lang,
                formula_display_value_backfill=formula_display_value_backfill,
            )
            if count is not None:
                entry["unfinished_text_count"] = count
                if total is not None:
                    total += count
            else:
                total = None
            # 开着行高调整时写完的文件还挂在「等待调整行高」，停止后不再调整——
            # 这里一并把它落成「已生成」，免得被收尾的 emit_many 改成「已停止」。
            self._file_progress.emit(
                source,
                "generated",
                "generate",
                result={
                    key: value
                    for key, value in entry.items()
                    if key not in {"source_path", "source_relative_path"}
                },
            )
        return total

    def _connection_switch_summary(self) -> dict[str, object]:
        """The failover history, in the shape the task center and report read."""
        with self._switch_lock:
            switches = [dict(item) for item in self._connection_switches]
        summary: dict[str, object] = {
            "switch_count": len(switches),
            "switches": switches,
        }
        if switches:
            summary["final_label"] = switches[-1].get("to_label", "")
            summary["started_with_label"] = switches[0].get("from_label", "")
        return summary

    @staticmethod
    def _validate_resume_baseline(path: Path) -> bool:
        """续译底稿只读探活：打得开才敢当底稿用，打不开就必须降级到源文件。"""
        try:
            from core.openpyxl_compat import load_workbook
            wb = load_workbook(str(path), read_only=True)
            wb.close()
            return True
        except Exception as e:
            logger.debug(f"续译底稿探活失败原始错误 {path}：{e!r}")
            return False

    def _collect_embedded_texts(
        self,
        path: Path,
        item_name: str,
        *,
        excel_output,
        target_lang: str,
        source_lang: str,
        coverage: bool,
    ) -> set[str]:
        """文本框里的字和批注（按设置开关），过一遍与单元格相同的「值不值得翻」判断。"""
        shapes = bool(getattr(excel_output, "translate_shapes", False))
        comments = bool(getattr(excel_output, "translate_comments", False))
        if not (shapes or comments):
            return set()
        coverage_langs = (
            (resolve_coverage_source_lang(source_lang), target_lang) if coverage else None
        )
        try:
            units = collect_embedded_units(
                path, shapes=shapes, comments=comments, coverage=coverage_langs
            )
        except Exception as exc:  # noqa: BLE001 - 读不出来就只翻单元格，不连累整个文件
            logger.debug(f"读取文本框/批注失败 {item_name}：{exc!r}")
            self._log("WARN", f"{item_name}：文本框和批注读取失败，本次只翻译单元格。")
            return set()
        texts: set[str] = set()
        shape_count = comment_count = 0
        for unit in units:
            if not should_translate(unit.text, target_lang=target_lang, source_lang=source_lang):
                continue
            texts.add(unit.text)
            if unit.kind == KIND_SHAPE:
                shape_count += 1
            else:
                comment_count += 1
        parts = []
        if shape_count:
            parts.append(f"文本框 {shape_count} 段")
        if comment_count:
            parts.append(f"批注 {comment_count} 条")
        if parts:
            self._log("INFO", f"  → {item_name}：另有" + "、".join(parts) + "待翻译")
        return texts

    @staticmethod
    def _embedded_text_options(
        path: Path,
        *,
        excel_output,
        translations: dict[str, str],
        target_lang: str,
        source_lang: str,
        coverage: bool,
    ) -> EmbeddedTextOptions | None:
        shapes = bool(getattr(excel_output, "translate_shapes", False))
        comments = bool(getattr(excel_output, "translate_comments", False))
        if not (shapes or comments):
            return None
        allowed = None
        if coverage:
            # 补译只改还没有译文的段落和批注；按写入源当场重算，和收集时同一套判断。
            try:
                allowed = frozenset(
                    unit.key
                    for unit in collect_embedded_units(
                        path,
                        shapes=shapes,
                        comments=comments,
                        coverage=(resolve_coverage_source_lang(source_lang), target_lang),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"补译写入前重读文本框/批注失败 {path.name}：{exc!r}")
                return None
        return EmbeddedTextOptions(
            shapes=shapes,
            comments=comments,
            translations=translations,
            allowed_keys=allowed,
        )

    @staticmethod
    def _collect_texts(
        real_path: Path,
        item_name: str,
        *,
        target_lang: str = "",
        source_lang: str = "zh",
        formula_display_value_backfill: bool = True,
    ) -> tuple[list[str], int]:
        """用 openpyxl 快速读取文件中需要翻译的所有唯一词条，并返回工作表数量。

        「公式显示值回填」关闭时写入器不会覆盖公式格，这里也就不能把公式格的
        显示值送去翻译——否则是白付一次 API 费用（审计 高-12），规则与
        excel_coverage 的补译计划保持一致。
        """
        from core.openpyxl_compat import load_workbook
        seen: set[str] = set()
        # 回填开着读缓存的显示值；关着则读公式本身，好按 data_type 认出公式格跳过
        wb = load_workbook(
            str(real_path), read_only=True, data_only=formula_display_value_backfill
        )
        try:
            worksheets = wb.worksheets
            for ws in worksheets:
                for row in ws.iter_rows():
                    for cell in row:
                        if not formula_display_value_backfill and cell.data_type == "f":
                            continue
                        val = cell.value
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
