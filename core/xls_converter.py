"""
XLS 格式转换器模块。
提供将老的 .xls 格式转换为 .xlsx 格式的功能。
提供两种策略：
1. xlwings（优先复用本地 Excel 链路）：保真度更高，但运行前提取决于本机实际环境。
2. xlrd + openpyxl（纯 Python）：降级转换，可能丢失复杂格式（合并单元格样式、图片、宏等）。
"""
import datetime
import platform
import tempfile
from pathlib import Path

from loguru import logger

from core.excel_automation import probe_local_excel_automation
from core.user_facing_errors import humanize_error


class XlwingsUnavailableError(Exception):
    """当尝试使用 xlwings 但环境不可用时抛出。"""


def is_excel_automation_permission_denied(exc: BaseException | str) -> bool:
    """Return whether an Excel automation error is a macOS privacy denial."""
    text = str(exc or "").lower()
    return (
        "oserror: -1743" in text
        or "the user has declined permission" in text
        or "not authorized to send apple events" in text
        or "自动化权限" in text
    )


def _format_excel_conversion_error(exc: BaseException) -> str:
    """Compose the sentence the user sees when Excel refuses to convert a .xls.

    The raw AppleScript/COM text stays in the debug log: it is either an error
    code the reader cannot act on, or — in the permission case — noise appended
    to an explanation that already says exactly what to do.
    """
    message = str(exc)
    logger.debug(f"使用 Excel 转换 .xls 失败原始错误：{exc!r}")
    if not is_excel_automation_permission_denied(message):
        return "使用 Excel 转换失败：" + humanize_error(
            message,
            fallback="本机 Excel 没能完成这次转换，可返回任务设置并选择兼容转换后重试。",
        )

    return (
        "使用 Excel 转换失败：macOS 已拒绝 Translator 控制 Microsoft Excel 的自动化权限。"
        f"请在「{macos_excel_automation_privacy_path()}」中允许 Translator 控制 Microsoft Excel，"
        "或返回任务设置并明确选择兼容转换；兼容转换后，输出文件里公式会变成算好的数值，"
        "样式、合并单元格、图片和图表不会保留；原始文件不会被改动。"
    )


def macos_excel_automation_privacy_path() -> str:
    """Return the user-visible automation permission path for this macOS."""
    if platform.system() != "Darwin":
        return "系统的自动化隐私设置"
    try:
        major = int(str(platform.mac_ver()[0]).split(".", 1)[0])
    except (TypeError, ValueError):
        major = 13
    if major <= 12:
        return "系统偏好设置 > 安全性与隐私 > 隐私 > 自动化"
    return "系统设置 > 隐私与安全性 > 自动化"


def get_local_excel_availability() -> tuple[bool, str]:
    """检查当前环境是否真的可用本地 Excel 自动化。"""
    return probe_local_excel_automation()


def check_xlwings_available() -> bool:
    """兼容旧接口：返回当前环境是否可用本地 Excel 自动化。"""
    available, _reason = get_local_excel_availability()
    return available


def is_excel_installed() -> bool:
    """兼容旧接口：沿用真实可用性作为判断。"""
    return check_xlwings_available()


def _discard_partial_output(out_path: Path) -> None:
    """转换半途失败时删掉写了一半的 .xlsx。

    Excel 的 Save As 和 openpyxl 的 save 都可能先建好文件再写坏中途退出。留着它
    没有任何人会回收：临时目录里那个半截文件既不会被后续任务读到，也不在任何
    清扫清单上，只会一直占着磁盘。转换失败的调用方拿到的是异常，不是路径。
    """
    try:
        out_path.unlink(missing_ok=True)
    except OSError as error:
        # 清理失败不该盖住真正的转换异常，记一笔就够了。
        logger.debug(f"清理半成品 .xlsx 失败（{out_path}）：{error}")


def _get_temp_xlsx_path(original_path: Path) -> Path:
    """在系统临时目录生成一个对应的 .xlsx 路径。"""
    temp_dir = Path(tempfile.gettempdir()) / "xl_translator_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # 避免重名冲突，加入文件大小或修改时间等哈希，这里简单用随机/自增
    import uuid
    safe_name = f"{original_path.stem}_{uuid.uuid4().hex[:6]}.xlsx"
    return temp_dir / safe_name


def convert_with_excel(app, xls_path: Path) -> Path:
    """
    使用由外部管理的 xlwings App 将 .xls 转换为 .xlsx。
    
    :param app: 外部传入的 xlwings.App 实例，实现全局进程复用。
    :param xls_path: 原 .xls 文件路径
    :return: 转换后的临时 .xlsx 文件路径
    """
    out_path = _get_temp_xlsx_path(xls_path)
    logger.info(f"使用 xlwings 将 {xls_path.name} 转换为临时 .xlsx")
    
    wb = None
    failure: Exception | None = None
    try:
        wb = app.books.open(str(xls_path))
        # 统一通过 xlwings 的跨平台 save() 触发 Save As，
        # 由目标扩展名 .xlsx 决定输出格式，避免写死 Windows COM 风格接口。
        wb.save(str(out_path))
    except Exception as e:
        # 这里只记下失败，删半成品要等工作簿关掉之后——见下面。
        failure = e
    finally:
        # A failed save must still close the book, or it lingers open in the
        # shared Excel automation process and poisons later conversions.
        if wb is not None:
            try:
                wb.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                pass

    if failure is not None:
        # 清理必须排在 wb.close() 之后：Save As 中途失败时 Excel 已经建好 out_path
        # 并仍持有它，Windows 上这时 unlink 会抛 PermissionError（被
        # _discard_partial_output 吞成一条 debug 日志），半截文件照样留在临时目录里
        # 没人回收。except 里删、finally 里关，正好是反的。
        _discard_partial_output(out_path)
        raise XlwingsUnavailableError(_format_excel_conversion_error(failure)) from failure

    return out_path


def _cell_value_by_ctype(cell, datemode: int):
    """把一个 xlrd 单元格还原成 openpyxl 认得的 Python 值。

    ``row_values()`` 只给裸值不给类型：.xls 里日期和布尔在底层都是数字，日期
    ``2023-07-15`` 是 ``45122.0``、``True`` 是 ``1.0``。照抄进 .xlsx 就是一串序列号
    和 0/1，还会一路带进最终的双语文件——那不是「损失样式」，是数据被改写。所以
    必须看 ``ctype`` 逐类还原。

    返回 ``None`` 表示这一格不用写（空格/空白格）。
    """
    import xlrd

    ctype = cell.ctype
    value = cell.value

    if ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if ctype == xlrd.XL_CELL_TEXT:
        return value or None
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(value)
    if ctype == xlrd.XL_CELL_ERROR:
        # 错误值在 xlrd 里是错误码整数（0x07 = #DIV/0! 之类）。写成数字会被当成
        # 正常数据参与后续处理，写回它本来的文本形态才是原样：openpyxl 认得
        # ``#N/A`` 这类合法错误码，会把它存成真正的 ``<c t="e">`` 错误格。
        # 认不出的码一律不写：编一个 ``#ERR`` 出来不是 Excel 的错误码，落进 .xlsx
        # 只是个普通文本格，会被抽取端当正文送去翻译。
        text = xlrd.error_text_from_code.get(value)
        if text is None:
            logger.debug(f"未知的 .xls 错误码 {value!r}，该单元格留空")
        return text
    if ctype == xlrd.XL_CELL_DATE:
        return _xls_date_value(value, datemode)
    # 剩下的就是普通数字：整数形态的浮点还原成 int，免得 5 变成 5.0。
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _xls_date_value(value: float, datemode: int):
    """把 .xls 的日期序列号还原成 date / time / datetime。

    只有日期没有时间的写成 ``date``，只有时间的写成 ``time``，两者都有才写
    ``datetime``——否则 openpyxl 会给纯日期配上 ``00:00:00`` 的时间格式，
    表面上又变成另一种「不是原来那个值」。序列号本身越界时（.xls 允许写出
    xlrd 换算不了的日期）退回原始数字，至少不丢内容。
    """
    import xlrd

    try:
        year, month, day, hour, minute, second = xlrd.xldate_as_tuple(value, datemode)
    except (xlrd.XLDateError, ValueError, OverflowError) as error:
        logger.debug(f"日期序列号 {value!r} 无法换算，按数字写入：{error}")
        return value

    if not (year or month or day):
        return datetime.time(hour, minute, second)
    if not (hour or minute or second):
        return datetime.date(year, month, day)
    return datetime.datetime(year, month, day, hour, minute, second)


def convert_with_fallback(xls_path: Path) -> Path:
    """
    纯 Python 降级方案：使用 xlrd 读取，openpyxl 写出。

    :param xls_path: 原 .xls 文件路径
    :return: 转换后的临时 .xlsx 文件路径
    """
    import xlrd
    from openpyxl import Workbook

    out_path = _get_temp_xlsx_path(xls_path)
    logger.info(f"使用降级方案将 {xls_path.name} 转换为临时 .xlsx")

    wb_in = xlrd.open_workbook(str(xls_path), formatting_info=False)
    try:
        wb_out = Workbook()
        try:
            # 删除默认创建的第一个 sheet
            if wb_out.sheetnames:
                del wb_out[wb_out.sheetnames[0]]

            for sheet_idx in range(wb_in.nsheets):
                ws_in = wb_in.sheet_by_index(sheet_idx)
                # 防止重名限制 (最大31字符等 openpyxl 自身会校验，这里直接传递)
                ws_out = wb_out.create_sheet(title=ws_in.name)

                for rowx in range(ws_in.nrows):
                    # openpyxl 行和列是从 1 开始的
                    for colx in range(ws_in.row_len(rowx)):
                        # 值按 ctype 还原，样式、合并和图片一律不管
                        value = _cell_value_by_ctype(
                            ws_in.cell(rowx, colx), wb_in.datemode
                        )
                        if value is not None:
                            ws_out.cell(row=rowx + 1, column=colx + 1, value=value)

            wb_out.save(str(out_path))
        finally:
            wb_out.close()
    except Exception:
        _discard_partial_output(out_path)
        raise
    finally:
        wb_in.release_resources()

    return out_path
