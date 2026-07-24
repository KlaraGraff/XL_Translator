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
# Translator owns output folders with this marker.  Keeping the exclusion at
# the scanner boundary prevents a recursive scan from treating a previous
# bilingual result as new source input.
GENERATED_OUTPUT_DIR_MARKER = "_翻译输出_"


@dataclass
class FileItem:
    path: Path
    name: str                          # 不含扩展名
    size_kb: float
    sheets: list[str] = field(default_factory=list)
    original_path: Path | None = None  # 用于记录原 .xls 路径
    relative_path: str = ""
    format: str = "xlsx"
    risk: dict[str, object] = field(default_factory=dict)


@dataclass
class ScanSkippedItem:
    """One source that was visible to the scan but cannot be selected."""

    path: Path
    relative_path: str
    reason: str
    format: str = ""


@dataclass
class ExcelScanResult:
    """Typed Excel scan result used by the local API/UI contract.

    ``scan_path`` remains a compatibility wrapper returning only selectable
    items; new code should call :func:`scan_excel_sources` when it must expose
    skipped files, counts and `.xls` compatibility risk to the user.
    """

    root: Path
    items: list[FileItem] = field(default_factory=list)
    skipped: list[ScanSkippedItem] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "scanned_count": len(self.items),
            "selected_count": len(self.items),
            "sheet_count": sum(len(item.sheets) for item in self.items),
            "xls_count": sum(1 for item in self.items if item.format == "xls"),
            "skipped_count": len(self.skipped),
        }

    @property
    def risk(self) -> dict[str, object]:
        xls_count = self.summary["xls_count"]
        return {
            "has_xls": bool(xls_count),
            "xls_count": xls_count,
            "requires_explicit_compatibility_confirmation": bool(xls_count),
            "message": (
                "检测到 .xls 文件：优先使用本机 Microsoft Excel 高保真转换；"
                "若选择兼容转换，复杂样式、合并单元格、图片、图表和宏可能无法完整保留。"
                if xls_count
                else "",
            ),
        }


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
    """Compatibility entry point returning selectable Excel items only."""
    return scan_excel_sources(path).items


def scan_excel_sources(path: str | Path) -> ExcelScanResult:
    """Recursively scan Excel input while retaining skipped-source evidence."""
    source = Path(path).expanduser()
    root = source if source.is_dir() else source.parent
    result = ExcelScanResult(root=root)
    if not source.exists():
        reason = f"路径不存在：{source}"
        logger.warning(reason)
        result.skipped.append(ScanSkippedItem(source, source.name, reason))
        return result

    if source.is_file():
        _scan_one_excel_file(source, source.parent, result)
    elif source.is_dir():
        for candidate in sorted(source.rglob("*")):
            if not candidate.is_file():
                continue
            if candidate.name.startswith("~"):
                continue
            try:
                relative = candidate.relative_to(source)
            except ValueError:
                relative = candidate.name
            if _is_generated_output(Path(relative)):
                continue
            if candidate.suffix.lower() not in SUPPORTED_EXCEL_SUFFIXES:
                continue
            _scan_one_excel_file(candidate, source, result)
    else:
        result.skipped.append(
            ScanSkippedItem(source, source.name, f"路径既不是文件也不是目录：{source}")
        )

    result.items.sort(key=lambda item: str(item.path))
    result.skipped.sort(key=lambda item: str(item.path))
    logger.info(
        f"Excel 扫描完成：{source}，可选 {len(result.items)}，跳过 {len(result.skipped)}"
    )
    return result


def scan_folder(root: str | Path) -> list[FileItem]:
    """
    递归扫描文件夹，返回所有 Excel 文件列表。
    跳过以 ~ 开头的临时文件（Excel 打开时产生）。
    """
    return scan_excel_sources(root).items


def _scan_one_excel_file(
    path: Path,
    root: Path,
    result: ExcelScanResult,
) -> None:
    if not is_supported_excel_file(path):
        result.skipped.append(
            ScanSkippedItem(
                path=path,
                relative_path=_relative_path(path, root),
                reason="不支持的 Excel 文件或 Office 临时文件",
                format=path.suffix.lower().lstrip("."),
            )
        )
        return
    try:
        result.items.append(_build_file_item(path, root=root))
    except Exception as exc:  # scan must not hide a corrupt/unreadable file
        message = f"读取失败：{exc}"
        logger.warning(f"扫描文件失败 {path.name}：{exc}")
        result.skipped.append(
            ScanSkippedItem(
                path=path,
                relative_path=_relative_path(path, root),
                reason=message,
                format=path.suffix.lower().lstrip("."),
            )
        )


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _build_file_item(path: Path, *, root: Path | None = None) -> FileItem:
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
        relative_path=_relative_path(path, root or path.parent),
        format=path.suffix.lower().lstrip("."),
        risk=(
            {
                "compatibility_required": True,
                "message": (
                    ".xls 需通过 Microsoft Excel 高保真转换，或经用户明确确认后"
                    "使用可能损失样式/合并单元格/图片/图表/宏的兼容转换。"
                ),
            }
            if path.suffix.lower() == ".xls"
            else {}
        ),
    )
