"""
文件扫描模块。
支持两种输入：
1) 文件夹路径：递归扫描目录下所有 .xlsx / .xls
2) 单文件路径：若为支持类型则仅返回该文件

图片相关字段已随 MVP 精简移除。
"""
# KNOWN-ISSUE-VAL-006:
# Image-related scan fields are intentionally absent from the active MVP path.
# See docs/KNOWN_ISSUES.md for why image_detector source is retained but offline.
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

SUPPORTED_EXCEL_SUFFIXES = {".xlsx", ".xls"}
GENERATED_OUTPUT_DIR_MARKER = "_翻译输出_"


@dataclass
class FileItem:
    path: Path
    name: str                          # 不含扩展名
    size_kb: float
    sheets: list[str] = field(default_factory=list)
    original_path: Path | None = None  # 用于记录原 .xls 路径


def is_supported_excel_file(path: str | Path) -> bool:
    """判断是否为可处理的单个 Excel 文件（排除 ~ 临时文件）。"""
    path = Path(path)
    return (
        path.is_file()
        and path.suffix.lower() in SUPPORTED_EXCEL_SUFFIXES
        and not path.name.startswith("~")
    )


def _is_generated_output(path: Path) -> bool:
    """跳过程序自己生成的输出目录，避免双语结果在下次扫描时被重复纳入任务。"""
    return any(GENERATED_OUTPUT_DIR_MARKER in part for part in path.parts)


def scan_path(path: str | Path) -> list[FileItem]:
    """统一扫描入口：支持目录或单文件路径。"""
    path = Path(path)
    if not path.exists():
        logger.warning(f"路径不存在：{path}")
        return []

    if path.is_dir():
        return scan_folder(path)

    if path.is_file():
        if not is_supported_excel_file(path):
            logger.warning(f"不支持的文件类型：{path}")
            return []
        try:
            item = _build_file_item(path)
            logger.info(f"单文件扫描完成：{path}")
            return [item]
        except Exception as e:
            logger.warning(f"扫描文件失败 {path.name}：{e}")
            return []

    logger.warning(f"路径既不是文件也不是目录：{path}")
    return []


def scan_folder(root: str | Path) -> list[FileItem]:
    """
    递归扫描文件夹，返回所有 Excel 文件列表。
    跳过以 ~ 开头的临时文件（Excel 打开时产生）。
    """
    root = Path(root)
    if not root.exists():
        logger.warning(f"文件夹不存在：{root}")
        return []

    items: list[FileItem] = []
    for ext in ("*.xlsx", "*.xls"):
        for p in root.rglob(ext):
            if p.name.startswith("~"):
                continue
            if _is_generated_output(p.relative_to(root)):
                continue
            try:
                item = _build_file_item(p)
                items.append(item)
            except Exception as e:
                logger.warning(f"扫描文件失败 {p.name}：{e}")

    items.sort(key=lambda x: x.path)
    logger.info(f"扫描完成：{root}，共发现 {len(items)} 个文件")
    return items


def _build_file_item(path: Path) -> FileItem:
    if path.suffix.lower() == ".xls":
        import xlrd
        wb = xlrd.open_workbook(str(path), on_demand=True)
        try:
            sheets = wb.sheet_names()
        finally:
            wb.release_resources()
    else:
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True, data_only=True)
        try:
            sheets = wb.sheetnames
        finally:
            wb.close()

    size_kb = path.stat().st_size / 1024

    original_path = path if path.suffix.lower() == ".xls" else None

    return FileItem(
        path=path,
        name=path.stem,
        size_kb=round(size_kb, 1),
        sheets=sheets,
        original_path=original_path,
    )
