"""测试用：现场生成一个「设了打开密码」的 BIFF8 .xls，不依赖任何第三方库。

libreoffice 路线的遗留未核实点（docs/LIBREOFFICE_XLS_ROUTE_2026-08-30.md:97）要求
补上「损坏文件、密码保护文件」的具体报错形态。损坏文件可以用截断真实文件来造，
密码保护文件却没法用 xlwt 直接写——它只支持工作表保护，不写 FILEPASS。仓库的测试
依赖里也没有 xlwt（requirements-test.txt），构建机上连生成夹具的路都没有。

所以这里用标准库手写一份最小但合法的 CFB（复合文档）+ BIFF8 工作簿：

* CFB 部分只有三样东西：一个 FAT 扇区、一个目录扇区（Root Entry + Workbook）、
  以及 Workbook 流本身；流补齐到 4096 字节以越过 mini-stream 分界，省掉整套
  mini FAT。
* BIFF8 部分是一个真的工作簿：globals 子流（BOF、BOUNDSHEET、EOF）加一张工作表
  子流（BOF、DIMENSIONS、NUMBER、EOF）。所以它真的能被 xlrd 和 soffice 打开，
  这一点由调用方的「明文对照」用例守着。
* ``encrypted=True`` 时在 globals 的 BOF 之后插一条 FILEPASS 记录（kind1=1 RC4、
  kind2=1 BIFF8 标准加密）。这正是 Excel「另存为 → 工具 → 常规选项 → 打开权限
  密码」写进去的那条记录，也是 xlrd ``Workbook is encrypted`` 与 soffice
  ``source file could not be loaded`` 的触发点。

这里不实现解密：夹具的用途就是让「打不开」的那条路径可复核。
"""

from __future__ import annotations

import struct
from pathlib import Path

SECTOR = 512
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF

_BIFF8 = 0x0600
_BOF_RECORD = 0x0809
_EOF_RECORD = 0x000A
_BOUNDSHEET_RECORD = 0x0085
_DIMENSIONS_RECORD = 0x0200
_NUMBER_RECORD = 0x0203
_FILEPASS_RECORD = 0x002F


def _record(record: int, data: bytes) -> bytes:
    return struct.pack("<HH", record, len(data)) + data


def _sheet_substream(value: float) -> bytes:
    bof = _record(_BOF_RECORD, struct.pack("<HHHHII", _BIFF8, 0x0010, 0x0DBB, 0x07CC, 0, 0))
    dimensions = _record(_DIMENSIONS_RECORD, struct.pack("<IIHHH", 0, 1, 0, 1, 0))
    number = _record(_NUMBER_RECORD, struct.pack("<HHH", 0, 0, 0) + struct.pack("<d", value))
    return bof + dimensions + number + _record(_EOF_RECORD, b"")


def _workbook_stream(*, encrypted: bool, value: float) -> bytes:
    sheet = _sheet_substream(value)
    globals_parts = [
        _record(_BOF_RECORD, struct.pack("<HHHHII", _BIFF8, 0x0005, 0x0DBB, 0x07CC, 0, 0))
    ]
    if encrypted:
        # FILEPASS：kind1=1（RC4）、kind2=1（BIFF8 标准加密），后接 48 字节盐/校验块。
        globals_parts.append(_record(_FILEPASS_RECORD, struct.pack("<HH", 1, 1) + b"\x11" * 48))
    name = b"S"
    # BOUNDSHEET 的数据段 = 4 字节流偏移 + 2 字节可见性/类型 + 1 字节名字长度 + 名字。
    boundsheet_length = 4 + 2 + 1 + len(name)
    # BOUNDSHEET 里的偏移是绝对流偏移：globals 的 BOF、[FILEPASS]、BOUNDSHEET
    # 之后还有 globals 的 EOF（4 字节），工作表 BOF 才排在那里。
    sheet_offset = sum(len(part) for part in globals_parts) + 4 + boundsheet_length + 4
    globals_parts.append(
        _record(
            _BOUNDSHEET_RECORD,
            struct.pack("<I", sheet_offset)
            + struct.pack("<BB", 0, 0)
            + struct.pack("<B", len(name))
            + name,
        )
    )
    globals_parts.append(_record(_EOF_RECORD, b""))
    stream = b"".join(globals_parts) + sheet
    # 4096 是 CFB 的 mini-stream 分界：补齐到它，Workbook 流就走普通扇区链。
    return stream + b"\x00" * (4096 - len(stream))


def _dir_entry(
    name: str,
    kind: int,
    start: int,
    size: int,
    *,
    child: int = _NOSTREAM,
    left: int = _NOSTREAM,
    right: int = _NOSTREAM,
) -> bytes:
    encoded = (name + "\x00").encode("utf-16-le")
    entry = bytearray(128)
    entry[: len(encoded)] = encoded
    struct.pack_into("<H", entry, 0x40, len(encoded))
    entry[0x42] = kind  # 1 存储、2 流、5 根
    entry[0x43] = 1  # 红黑树颜色
    struct.pack_into("<I", entry, 0x44, left)
    struct.pack_into("<I", entry, 0x48, right)
    struct.pack_into("<I", entry, 0x4C, child)
    struct.pack_into("<I", entry, 0x74, start)
    struct.pack_into("<I", entry, 0x78, size)
    return bytes(entry)


def write_password_protected_xls(dest: Path, *, encrypted: bool = True, value: float = 12345.0) -> Path:
    """写一份 BIFF8 .xls 到 ``dest``；``encrypted=False`` 时同一份文件不设密码。

    明文那一份不是陪衬：只有它能打开、能读出 ``value``，才说明加密那一份被打不开
    是因为 FILEPASS，而不是因为夹具本身是坏文件。
    """
    stream = _workbook_stream(encrypted=encrypted, value=value)
    data_sectors = len(stream) // SECTOR
    # 扇区 0 = FAT，扇区 1 = 目录，之后是 Workbook 流。
    fat_entries = [_FATSECT, _ENDOFCHAIN]
    for index in range(data_sectors):
        fat_entries.append(2 + index + 1 if index + 1 < data_sectors else _ENDOFCHAIN)
    fat = fat_entries + [_FREESECT] * (SECTOR // 4 - len(fat_entries))

    directory = (
        _dir_entry("Root Entry", 5, _ENDOFCHAIN, 0, child=1)
        + _dir_entry("Workbook", 2, 2, len(stream))
        + bytes(128 * 2)
    )

    header = bytearray(SECTOR)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x18, 0x003E)  # minor version
    struct.pack_into("<H", header, 0x1A, 0x0003)  # major version 3
    struct.pack_into("<H", header, 0x1C, 0xFFFE)  # byte order
    struct.pack_into("<H", header, 0x1E, 9)  # 512 字节扇区
    struct.pack_into("<H", header, 0x20, 6)  # 64 字节 mini 扇区
    struct.pack_into("<I", header, 0x2C, 1)  # FAT 扇区数
    struct.pack_into("<I", header, 0x30, 1)  # 目录起始扇区
    struct.pack_into("<I", header, 0x38, 4096)  # mini-stream 分界
    struct.pack_into("<I", header, 0x3C, _ENDOFCHAIN)  # mini FAT 起始扇区
    struct.pack_into("<I", header, 0x40, 0)  # mini FAT 扇区数
    struct.pack_into("<I", header, 0x44, _ENDOFCHAIN)  # DIFAT 起始扇区
    struct.pack_into("<I", header, 0x48, 0)  # DIFAT 扇区数
    for slot in range(109):
        struct.pack_into("<I", header, 0x4C + slot * 4, 0 if slot == 0 else _FREESECT)

    dest.write_bytes(bytes(header) + struct.pack(f"<{len(fat)}I", *fat) + directory + stream)
    return dest
