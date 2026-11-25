"""Windows 能接受的文件名和文件夹名：凡是按用户文件名拼路径的地方都走这里。

Windows 有三条 macOS 没有的限制，名字踩中任何一条都会出事：

* 不能含 ``<>:"/\\|?*`` 和控制字符；
* 名字结尾不能是点或空格。建文件夹时系统会悄悄把结尾的点去掉，可后面往里写文件时
  路径中间那一段又原样保留，于是「DEVIS CHINA CONSTRUCTION..」这种文件夹建出来叫
  「DEVIS CHINA CONSTRUCTION」，再往里写页图就报「文件不存在」（反馈 #32）；
* ``CON``、``PRN``、``AUX``、``NUL``、``COM1``–``COM9``、``LPT1``–``LPT9`` 是设备名，
  带不带扩展名都不能当文件名用。

``clean_filename_fragment`` 只洗一段会被拼进更长名字里的片段（例如语言标签）；
``safe_path_component`` 用于整段文件夹名或文件名；``safe_relative_path`` 逐段处理
一条相对路径。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath

INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    # Windows 也把上标数字的 COM¹/LPT² 等当设备名。
    | {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "¹²³"}
)


def clean_filename_fragment(value: object, fallback: str = "_") -> str:
    """换掉 Windows 禁用字符，去掉首尾空白和结尾的点、空格；洗完为空时用 ``fallback``。"""
    cleaned = INVALID_FILENAME_CHARS_RE.sub("_", str(value or "")).strip().rstrip(". ")
    return cleaned or fallback


def is_windows_reserved_name(name: str) -> bool:
    """``CON``、``nul.txt``、``Com1.tar.gz`` 这类名字在 Windows 上打不开。"""
    base = str(name or "").split(".", 1)[0].rstrip(" ").upper()
    return base in WINDOWS_RESERVED_NAMES


def safe_path_component(value: object, fallback: str = "_") -> str:
    """一整段文件名或文件夹名，保证在 Windows 上能原样建出来、再原样找到。

    结尾的点和空格逐个换成 ``_`` 而不是删掉：「报价..pdf」和「报价.pdf」的页图文件夹
    分别是「报价__」和「报价」，不会挤进同一个文件夹互相覆盖。
    """
    cleaned = INVALID_FILENAME_CHARS_RE.sub("_", str(value or ""))
    trimmed = cleaned.rstrip(". ")
    if not trimmed.strip():
        return fallback
    cleaned = trimmed + "_" * (len(cleaned) - len(trimmed))
    if is_windows_reserved_name(cleaned):
        head, dot, tail = cleaned.partition(".")
        cleaned = f"{head}_{dot}{tail}"
    return cleaned


def safe_relative_path(path: str | PurePath, fallback: str = "_") -> Path:
    """逐段洗一条相对路径；空路径返回 ``fallback``。"""
    parts = [
        safe_path_component(part, fallback)
        for part in PurePath(path).parts
        if part not in {"", ".", "/", "\\"}
    ]
    return Path(*parts) if parts else Path(fallback)
