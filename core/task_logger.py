"""
任务日志模块。

每次翻译任务实例化一个 TaskLogger，通过 LoggerAdapter 注入 task_id。
关闭时所有方法立即返回，零开销。

日志文件：平台原生应用数据目录下的 app.log
轮转策略：RotatingFileHandler，5 MB / 文件，保留 4 个备份（加当前文件共 5 个）
"""
import logging
import logging.handlers
import traceback
from datetime import datetime

from config import APP_DATA_DIR, LOG_PATH
from core.language_registry import get_target_lang_display

# ── 日志路径：统一写入用户数据目录 app.log ───────────────────────────────
LOG_DIR = APP_DATA_DIR

# ── 全局 logger（仅初始化一次）───────────────────────────────────────────
_LOGGER_NAME = "xl_translator.task"
_handler_installed = False


def setup_file_handler() -> None:
    """
    初始化 RotatingFileHandler，应用启动时调用一次。
    重复调用安全（幂等）。
    """
    global _handler_installed
    if _handler_installed:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger(_LOGGER_NAME)
    root_logger.setLevel(logging.DEBUG)

    # 避免重复添加 handler（热重载场景）
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root_logger.handlers):
        _handler_installed = True
        return

    handler = logging.handlers.RotatingFileHandler(
        filename=str(LOG_PATH),
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=4,               # 保留 4 个备份，加当前共 5 个
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-5s] [task:%(task_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.propagate = False   # 不向上冒泡，避免与 loguru 重复
    _handler_installed = True


# ── TaskLogger ───────────────────────────────────────────────────────────

class TaskLogger:
    """
    单次翻译任务的日志记录器。

    用法：
        logger = TaskLogger(enabled=True, task_id="20260329_143201")
        logger.task_start(files, settings)
        logger.info("[文件 1/3] 词条收集完成")
        logger.task_end(elapsed=12.3, results=[...])

    enabled=False 时所有方法立即返回，调用方无需判断。
    """

    def __init__(self, enabled: bool, task_id: str | None = None):
        self.enabled = enabled
        self.task_id = task_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._adapter: logging.LoggerAdapter | None = None

        if enabled:
            setup_file_handler()
            base = logging.getLogger(_LOGGER_NAME)
            self._adapter = logging.LoggerAdapter(
                base, extra={"task_id": self.task_id}
            )

    # ── 基础日志方法 ─────────────────────────────────────────────────────

    def info(self, msg: str) -> None:
        if self.enabled and self._adapter:
            self._adapter.info(msg)

    def warning(self, msg: str) -> None:
        if self.enabled and self._adapter:
            self._adapter.warning(msg)

    def error(self, msg: str, exc_info: bool = False) -> None:
        if not self.enabled or not self._adapter:
            return
        if exc_info:
            self._adapter.error(msg + "\n" + traceback.format_exc())
        else:
            self._adapter.error(msg)

    # ── 结构化埋点方法（全局阶段）─────────────────────────────────────────

    def global_collected(self, total_unique: int, file_count: int, elapsed: float) -> None:
        """全局词汇提取完成。"""
        self.info(
            f"[全局] 词汇提取完成 | 文件数={file_count} | 去重词条={total_unique} | 耗时={elapsed:.3f}s"
        )

    def global_tm_result(self, hits: int, misses: int) -> None:
        """全局 TM 查询结果。"""
        self.info(f"[全局] TM命中={hits} | 待API翻译={misses}")

    def global_api_done(self, returned: int, elapsed: float) -> None:
        """全局 API 翻译完成。"""
        self.info(f"[全局] API翻译完成 | 返回={returned}条 | 耗时={elapsed:.3f}s")

    # ── 结构化埋点方法（单文件 / 任务级）──────────────────────────────────

    def excel_policy_decided(
        self,
        excel_policy: str,
        xls_file_count: int,
        total_sheet_count: int,
        raw_text_count: int,
        reason: str,
    ) -> None:
        """Record the Excel reuse/split policy decision for later tuning."""
        self.info(
            "[GLOBAL] Excel policy decided | "
            f"excel_policy={excel_policy} | "
            f"xls_file_count={xls_file_count} | "
            f"total_sheet_count={total_sheet_count} | "
            f"raw_text_count={raw_text_count} | "
            f"reason={reason}"
        )

    def task_start(
        self,
        files: list,
        engine_name: str,
        target_lang: str,
        keep_original_sheets: bool,
        formula_display_value_backfill: bool,
        enable_excel_autofit: bool,
        lock_row_height: bool = False,
    ) -> None:
        """记录任务启动段落。"""
        if not self.enabled:
            return
        sep = "=" * 60
        self.info(sep)
        self.info(f"===== TASK START [{self.task_id}] =====")
        self.info(
            f"文件数={len(files)} | 引擎={engine_name} | "
            f"目标语言={get_target_lang_display(target_lang)}"
        )
        self.info(
            f"保留原始表格={keep_original_sheets} | 公式显示值回填={formula_display_value_backfill} | "
            f"ExcelAutoFit={enable_excel_autofit} | "
            f"锁定行高缩字号={lock_row_height}"
        )
        for i, f in enumerate(files, 1):
            name = getattr(f, 'name', str(f))
            self.info(f"  [{i}] {name}")

    def task_end(
        self,
        elapsed_sec: float,
        file_results: list[dict],
    ) -> None:
        """记录任务结束段落。"""
        if not self.enabled:
            return
        success = sum(1 for r in file_results if r.get("success"))
        failed  = len(file_results) - success
        self.info(
            f"===== TASK END | 总耗时={elapsed_sec:.2f}s "
            f"| 成功={success} 失败={failed} ====="
        )
        self.info("=" * 60)

    def file_collected(self, filename: str, count: int, elapsed: float) -> None:
        """词条收集完成。"""
        self.info(f"[{filename}] 词条收集完成 | 词条数={count} | 耗时={elapsed:.3f}s")

    def file_tm_result(self, filename: str, hits: int, misses: int) -> None:
        """TM 查询结果。"""
        self.info(f"[{filename}] TM命中={hits} | 待API翻译={misses}")

    def file_api_done(self, filename: str, returned: int, elapsed: float) -> None:
        """API 翻译完成。"""
        self.info(f"[{filename}] API翻译完成 | 返回={returned}条 | 耗时={elapsed:.3f}s")

    def file_write_done(self, filename: str, elapsed: float) -> None:
        """回填写入完成。"""
        self.info(f"[{filename}] 回填写入完成 | 耗时={elapsed:.3f}s")

    def file_autofit_done(self, filename: str, elapsed: float) -> None:
        """AutoFit 完成。"""
        self.info(f"[{filename}] Excel AutoFit完成 | 耗时={elapsed:.3f}s")

    def file_autofit_skipped(self, filename: str, reason: str) -> None:
        """AutoFit 跳过。"""
        self.warning(f"[{filename}] AutoFit跳过 | 原因={reason}")

    def file_done(
        self,
        filename: str,
        elapsed: float,
        tm_hits: int,
        api_calls: int,
    ) -> None:
        """单文件处理完成。"""
        self.info(
            f"[{filename}] 完成 ✓ | 总耗时={elapsed:.3f}s "
            f"| TM命中={tm_hits} | API调用={api_calls}"
        )

    def file_error(self, filename: str, error: str) -> None:
        """单文件处理失败。"""
        self.error(f"[{filename}] 处理失败 | {error}", exc_info=True)
