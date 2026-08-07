"""
双语回填与文件导出模块。

回填规则：
  - 直接修改目标单元格，格式：原文 + "\\n" + 译文
  - 若译文与原文实质相同（剔除空格大小写），仅保留原文
  - 不插入新行或新列

输出：
  - 输出目录：{源目录}_翻译输出_{timestamp}/
  - 文件名前缀：双语({语言})_{原文件名}.xlsx
  - 可选：保留原始中文分表（sheet 名称加 _原文 后缀）
"""
import os
import re
import shutil
import stat
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger

from config import EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT
from core.language_registry import get_target_lang_display
from core.xlsx_patcher import write_bilingual_workbook

_INVALID_FILENAME_FRAGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _ensure_owner_writable(path: Path) -> None:
    """Ensure copied outputs stay writable even when the source workbook is read-only."""
    current_mode = path.stat().st_mode
    if current_mode & stat.S_IWUSR:
        return
    path.chmod(current_mode | stat.S_IWUSR)


def patch_into_output(source_path: Path, out_path: Path, patch) -> Path:
    """先在临时文件上打补丁，成功了才让它占用最终文件名。

    输出副本是在打补丁**之前**拷过去的。若直接拷成最终文件名，补丁中途失败
    （译文含非法 XML 字符、包结构异常、磁盘写满……）就会在输出目录里留下一个
    文件名完全正常的「双语(xx)_xxx.xlsx」，内容却一个字没翻。用户看不出区别，
    极可能直接发出去。所以：要么产出一个翻译好的文件，要么什么都不留。

    ``patch`` 接收临时文件路径，就地改写它。
    """
    staging = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:8]}.partial")
    shutil.copy2(source_path, staging)
    _ensure_owner_writable(staging)
    try:
        patch(staging)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    os.replace(staging, out_path)
    return out_path


def resolve_custom_output_dir(custom_output_dir: str | Path | None) -> Path | None:
    """Normalize a custom output root; return None for empty input."""
    if custom_output_dir is None:
        return None

    normalized = str(custom_output_dir).strip()
    if not normalized:
        return None

    return Path(normalized).expanduser()


def _find_blocking_existing_path(target_path: Path) -> Path | None:
    """Find the first existing ancestor that blocks directory creation."""
    current = target_path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None if current.is_dir() else current


def get_custom_output_dir_error(custom_output_dir: str | Path | None) -> str | None:
    """Return a user-friendly validation error for a custom output root."""
    output_root = resolve_custom_output_dir(custom_output_dir)
    if output_root is None:
        return "自定义输出目录不能为空"

    if output_root.exists():
        if output_root.is_dir():
            return None
        return f"输出路径不是目录：{output_root}"

    blocking_path = _find_blocking_existing_path(output_root)
    if blocking_path is not None:
        return f"无法在文件路径下创建目录：{blocking_path}"

    return None


def custom_output_dir_will_be_created(custom_output_dir: str | Path | None) -> bool:
    """Whether the custom output root will be created at runtime."""
    output_root = resolve_custom_output_dir(custom_output_dir)
    if output_root is None or get_custom_output_dir_error(output_root) is not None:
        return False
    return not output_root.exists()


def build_output_dir(source_dir: str | Path, custom_output_dir: str | Path | None = None) -> Path:
    """生成带时间戳的输出目录路径（不创建目录）。
    
    :param source_dir: 源文件夹路径
    :param custom_output_dir: 自定义输出目录（None 或空字符串时使用默认位置）
    :return: 输出目录路径
    
    默认行为：在源文件夹内部创建 {源文件夹名}_翻译输出_{时间戳}
    自定义行为：在指定目录内创建 {源文件夹名}_翻译输出_{时间戳}
    """
    source_dir = Path(source_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_subdir_name = (
        f"{source_dir.name}_翻译输出_{timestamp}_{uuid.uuid4().hex[:8]}"
    )
    
    custom_output_root = resolve_custom_output_dir(custom_output_dir)
    if custom_output_root is not None:
        return custom_output_root / output_subdir_name

    # 默认：在源文件夹内部创建
    return source_dir / output_subdir_name


def write_bilingual_file(
    source_path: Path,
    output_dir: Path,
    translations: dict[str, str],
    target_lang: str,
    keep_original_sheets: bool,
    formula_display_value_backfill: bool,
    enable_print_guard: bool,
    source_lang: str = "zh",
    lock_row_height: bool = False,
    review_marks: dict[str, str] | None = None,
    review_mark_colors: dict[str, str] | None = None,
    mark_review_items: bool = True,
    existing_fill_policy: str = EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT,
    log_callback=None,
    original_path: Path | None = None,
    review_positions: list[dict[str, str]] | None = None,
    external_autofit_planned: bool = False,
    stats: dict[str, object] | None = None,
) -> Path:
    """
    将翻译结果回填至 Excel 文件并保存至输出目录。

    :param source_path:          原始文件路径
    :param output_dir:           输出目录（已创建）
    :param translations:         {原文: 译文} 字典
    :param target_lang:          目标语言代码
    :param keep_original_sheets: 是否保留原始中文分表
    :param formula_display_value_backfill:
                                 是否对公式生成的显示文本按显示值匹配后回填
    :param enable_print_guard:   保留参数（MVP 阶段不生效）
    :param lock_row_height:      是否锁定行高并通过缩小字号适配内容
    :param review_marks:         {原文: 风险标记类型}，用于整格标记需复核内容
    :param review_mark_colors:   风险标记类型到 RGB 色值的映射
    :param mark_review_items:    是否写入需复核标记
    :param existing_fill_policy: 已有底色处理策略：skip/overwrite/red_font
    :param log_callback:         日志回调 log_callback(msg: str)
    :param original_path:        原 .xls 路径（如果是经过转换的临时文件）
    :param external_autofit_planned:
                                 写完之后调用方还会跑 Excel COM 的整表 AutoFit。
                                 为 True 时写入器会把整张表的悬浮图片锚点全部固定，
                                 否则 Excel 重排行高会把没冻结的图片拉变形
    :param stats:                可选统计出参，写入 mutated_cells / anchor_frozen_count
    :return:                     输出文件路径
    """
    lang_display = _sanitize_filename_fragment(
        get_target_lang_display(target_lang, include_optional=True)
    )
    
    # 确定输出文件名，处理源文件可能是临时文件的场景
    basename = original_path.name if original_path else source_path.name
    # 强制最终输出为 .xlsx（即使源文件是 .xls）
    if basename.lower().endswith(".xls"):
        basename = basename[:-4] + ".xlsx"
        
    out_name     = f"双语({lang_display})_{basename}"
    out_path     = output_dir / out_name

    output_dir.mkdir(parents=True, exist_ok=True)

    patch_into_output(
        source_path,
        out_path,
        lambda staging: write_bilingual_workbook(
            staging,
            translations=translations,
            target_lang=target_lang,
            source_lang=source_lang,
            keep_original_sheets=keep_original_sheets,
            formula_display_value_backfill=formula_display_value_backfill,
            lock_row_height=lock_row_height,
            review_marks=review_marks,
            review_mark_colors=review_mark_colors,
            mark_review_items=mark_review_items,
            existing_fill_policy=existing_fill_policy,
            log_callback=log_callback,
            review_positions=review_positions,
            external_autofit_planned=external_autofit_planned,
            stats=stats,
        ),
    )

    if log_callback:
        log_callback(f"[OK] 已输出：{out_path.name}")

    return out_path


def _sanitize_filename_fragment(value: str) -> str:
    """Remove Windows-illegal filename characters from user-facing fragments."""
    cleaned = _INVALID_FILENAME_FRAGMENT_RE.sub("_", str(value or "")).strip().rstrip(". ")
    return cleaned or "目标语言"


def autofit_files_batch(
    file_paths: list[Path],
    log_callback=None,
    app=None,
    progress_callback=None,
) -> bool:
    """使用一次 Excel 进程对多个文件批量执行 AutoFit 行高调整。

    相比逐文件启动 Excel，只付出一次进程启动开销（约 5-8s），
    N 个文件的总耗时从 N×8s 降为 5s + N×1-2s。

    :param file_paths:   要处理的 Excel 文件路径列表
    :param log_callback: 日志回调
    :param app:          如果有现成的 xlwings App，可直接传入复用
    :param progress_callback: 进度回调 progress_callback(done, total, current_file)
    :return:             True 表示成功，False 表示 xlwings 不可用（已静默降级）
    """
    if not file_paths:
        return True

    try:
        import xlwings as xw
    except ImportError:
        if log_callback:
            log_callback("[WARN] xlwings 未安装，已跳过 Excel 行高优化（当前使用 Python 估算值）")
        logger.warning("xlwings 未安装，跳过 AutoFit")
        return False

    staged_paths: list[tuple[Path, Path]] = []
    temp_workspace = tempfile.TemporaryDirectory(prefix="xl_translator_autofit_")
    try:
        temp_root = Path(temp_workspace.name)
        for index, file_path in enumerate(file_paths, start=1):
            staged_path = temp_root / f"{index:03d}_{file_path.name}"
            shutil.copy2(file_path, staged_path)
            _ensure_owner_writable(staged_path)
            staged_paths.append((file_path, staged_path))

        def _do_autofit(current_app):
            total = len(staged_paths)
            done = 0
            for original_path, staged_path in staged_paths:
                try:
                    if log_callback:
                        log_callback(
                            f"[INFO] Excel AutoFit 打开临时副本：{staged_path}（原文件：{original_path}）"
                        )
                    wb = current_app.books.open(str(staged_path))
                    try:
                        for ws in wb.sheets:
                            ws.used_range.rows.autofit()
                        wb.save()
                    finally:
                        # A failed autofit/save must still close the book, or
                        # it lingers in the shared Excel process and keeps the
                        # staged copy locked so the temp cleanup fails too.
                        try:
                            wb.close()
                        except Exception:  # noqa: BLE001 - best-effort cleanup only
                            pass
                    shutil.copy2(staged_path, original_path)
                    _ensure_owner_writable(original_path)
                    if log_callback:
                        log_callback(f"[INFO] Excel AutoFit 已回写原文件：{original_path}")
                    logger.info(f"AutoFit 完成：{original_path.name}")
                except Exception as e:
                    logger.warning(f"AutoFit 异常（{original_path.name}）：{e}")
                    if log_callback:
                        log_callback(f"[WARN] {original_path.name} AutoFit 失败，已保留 Python 估算值：{e}")
                finally:
                    done += 1
                    if progress_callback:
                        progress_callback(done, total, original_path)

        if app is not None:
            _do_autofit(app)
        else:
            with xw.App(visible=False) as new_app:
                new_app.display_alerts = False
                _do_autofit(new_app)

        if log_callback:
            log_callback(f"[INFO] Excel AutoFit 完成，共处理 {len(file_paths)} 个文件")
        return True
    except Exception as e:
        if log_callback:
            log_callback(f"[WARN] Excel AutoFit 失败，已保留 Python 估算值：{e}")
        logger.warning(f"AutoFit 批量处理异常：{e}")
        return False
    finally:
        temp_workspace.cleanup()
