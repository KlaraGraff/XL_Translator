"""按字体名找到电脑上的字体文件，量一段文字在 PowerPoint 里有多宽（PPT 缩字用）。

幻灯片不像 Excel 文本框那样裁字，按「宁可高估」的估算缩字只会让字无谓变小
（调研样张：同一个框估算要缩到 70%，按真实字宽 90% 就够），所以 PPT 先量真字宽：

  - 找字体：按字体名在系统字体目录里找文件（Windows 系统和用户字体目录，Mac 系统、
    用户和 PowerPoint 自带字体目录，Linux 的 fontconfig 目录），读字体自己的名字表，
    中文名（微软雅黑、等线）和英文名都认；装的不是原字体但有字宽完全相同的替代字体时
    （Calibri → Carlito，Arial → Liberation Sans 等），用替代字体量。
  - 量宽：直接读字体的 cmap（字符 → 字形）和 hmtx（字形前进宽度），和 PIL
    ``ImageFont.getlength`` 累加的是同一组数（tests/test_pptx_fit_metrics.py 对照过），
    不用为每个字号各开一个 FreeType 字体；字宽按字号线性换算，不计字距微调（只会略宽）。
  - 字体里没有这个字、或电脑上找不到这个字体时，退回 ``fallback_advance`` 的估算。

字体目录可以传参数指定（测试用），不依赖机器上装了什么。
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import unicodedata
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}

# 字宽完全相同（metric-compatible）的开源替代字体：原字体没装时用它们量，结果与原字体一致。
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "calibri": ("carlito",),
    # 近似：Calibri Light 没有开源同宽字体，细体比常规体略窄，用 Carlito 量只会略宽。
    "calibri light": ("carlito",),
    "cambria": ("caladea",),
    "arial": ("liberation sans", "arimo"),
    "helvetica": ("arial", "liberation sans", "arimo"),
    "times new roman": ("liberation serif", "tinos"),
    "times": ("times new roman", "liberation serif", "tinos"),
    "courier new": ("liberation mono", "cousine"),
    "arial narrow": ("liberation sans narrow",),
}

# 找不到字体时的西文字宽：Arial（Liberation Sans 实测，字宽与 Arial 相同）的前进宽度，
# 千分之一个字号，覆盖 U+0020–U+007E。带音调的字母按去掉音调的字母算（é → e）。
# Calibri 比 Arial 窄约一成，Verdana、微软雅黑的西文比 Arial 宽一些，所以估算再乘
# ``FALLBACK_SAFETY``：宁可多缩一点，不让找不到字体的框溢出。
_ARIAL_ASCII = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_ARIAL_EXTRA = {
    "\u00a0": 278, "«": 556, "»": 556, "°": 400, "·": 333, "€": 556, "œ": 944, "Œ": 1000,
    "æ": 889, "Æ": 1000, "ß": 611, "\u2013": 556, "\u2014": 1000, "\u2018": 222, "\u2019": 222,
    "\u201c": 333, "\u201d": 333, "\u2026": 1000, "\u2022": 350, "\u202f": 200, "\u2009": 200,
}
FALLBACK_SAFETY = 1.05
_FALLBACK_OTHER_EM = 0.6


def is_wide(char: str) -> bool:
    """全角字符（汉字、假名、谚文、全角标点）：PowerPoint 用东亚字体（``a:ea``）画。"""
    return unicodedata.east_asian_width(char) in {"W", "F"}


def fallback_advance(char: str) -> float:
    """找不到字体或字体里没有这个字时的估算字宽（字号的倍数）。"""
    if char in "\n\r":
        return 0.0
    if is_wide(char):
        return 1.0
    code = ord(char)
    if 0x20 <= code <= 0x7E:
        return _ARIAL_ASCII[code - 0x20] / 1000 * FALLBACK_SAFETY
    if char in _ARIAL_EXTRA:
        return _ARIAL_EXTRA[char] / 1000 * FALLBACK_SAFETY
    if unicodedata.combining(char):
        return 0.0
    base = unicodedata.normalize("NFD", char)[:1]
    if base and base != char and 0x20 <= ord(base) <= 0x7E:
        return _ARIAL_ASCII[ord(base) - 0x20] / 1000 * FALLBACK_SAFETY
    return _FALLBACK_OTHER_EM * FALLBACK_SAFETY


def system_font_dirs() -> list[Path]:
    """本机的字体目录（不存在的也列着，扫描时跳过）。"""
    home = Path.home()
    if sys.platform.startswith("win"):
        windir = Path(os.environ.get("WINDIR") or "C:\\Windows")
        dirs = [windir / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    if sys.platform == "darwin":
        return [
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            home / "Library" / "Fonts",
            # PowerPoint for Mac 自带的 Calibri、等线等不装进系统，放在应用包里。
            Path("/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts"),
        ]
    xdg = os.environ.get("XDG_DATA_HOME")
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path(xdg) / "fonts" if xdg else home / ".local" / "share" / "fonts",
        home / ".fonts",
    ]


@dataclass(frozen=True)
class FontFace:
    """字体文件里的一个字形集（.ttc 里有好几个，``index`` 区分）。"""

    path: str
    index: int
    family: str
    weight: int
    italic: bool

    @property
    def bold(self) -> bool:
        return self.weight >= 600


class FaceMetrics:
    """一个字形集的字宽：``advance(ch)`` 返回字号的倍数，字体里没有这个字返回 None。"""

    def __init__(self, face: FontFace, data: _SfntTables) -> None:
        self.face = face
        self._units = data.units_per_em
        self._advances = data.advances
        self._lookup = data.glyph_for
        self._cache: dict[str, float | None] = {}

    def advance(self, char: str) -> float | None:
        cached = self._cache.get(char, ...)
        if cached is not ...:
            return cached
        glyph = self._lookup(ord(char))
        if not glyph:
            value = None
        else:
            advances = self._advances
            value = (advances[glyph] if glyph < len(advances) else advances[-1]) / self._units
        self._cache[char] = value
        return value


@dataclass(frozen=True)
class ResolvedFace:
    """按名字找到的字形集；``synthetic_bold``：要粗体但只有常规体，PowerPoint 会加粗画。"""

    metrics: FaceMetrics
    synthetic_bold: bool = False

    def advance(self, char: str) -> float | None:
        value = self.metrics.advance(char)
        if value is None or not self.synthetic_bold:
            return value
        return value * _SYNTHETIC_BOLD_WIDEN


# 假粗体每个字笔画加粗一点，前进宽度也跟着宽一点（约 2%）。
_SYNTHETIC_BOLD_WIDEN = 1.02


class FontLibrary:
    """按名字找字体、量字宽。第一次找字时扫描字体目录（只读各文件的名字表），之后都查表。"""

    def __init__(self, directories: Iterable[str | os.PathLike[str]] | None = None) -> None:
        self._dirs = [Path(d) for d in directories] if directories is not None else system_font_dirs()
        self._lock = threading.Lock()
        self._families: dict[str, list[FontFace]] | None = None
        self._metrics: dict[tuple[str, int], FaceMetrics | None] = {}
        self._resolved: dict[tuple[str, bool, bool], ResolvedFace | None] = {}

    def resolve(self, family: str, *, bold: bool = False, italic: bool = False) -> ResolvedFace | None:
        """按 PowerPoint 里写的字体名（``a:latin``/``a:ea`` 的 typeface）找字形集，找不到返回 None。"""
        name = " ".join((family or "").split()).casefold()
        if not name:
            return None
        key = (name, bool(bold), bool(italic))
        with self._lock:
            if key in self._resolved:
                return self._resolved[key]
            families = self._index()
            resolved = None
            for candidate in (name, *METRIC_ALIASES.get(name, ())):
                faces = families.get(candidate)
                if not faces:
                    continue
                face = _pick_face(faces, bold=bold, italic=italic)
                metrics = self._face_metrics(face)
                if metrics is not None:
                    resolved = ResolvedFace(metrics, synthetic_bold=bool(bold) and not face.bold)
                    break
            self._resolved[key] = resolved
            return resolved

    def _face_metrics(self, face: FontFace) -> FaceMetrics | None:
        key = (face.path, face.index)
        if key not in self._metrics:
            try:
                tables = _read_metric_tables(face.path, face.index)
            except (OSError, ValueError, struct.error):
                tables = None
            self._metrics[key] = FaceMetrics(face, tables) if tables is not None else None
        return self._metrics[key]

    def _index(self) -> dict[str, list[FontFace]]:
        if self._families is None:
            families: dict[str, list[FontFace]] = {}
            for path in _font_files(self._dirs):
                try:
                    entries = _read_name_entries(path)
                except (OSError, ValueError, struct.error):
                    continue
                for face, names in entries:
                    for name in names:
                        families.setdefault(name, []).append(face)
            self._families = families
        return self._families


_default_library: FontLibrary | None = None
_default_lock = threading.Lock()


def default_library() -> FontLibrary:
    """全进程共用一份本机字体表（扫描一次）。"""
    global _default_library
    with _default_lock:
        if _default_library is None:
            _default_library = FontLibrary()
        return _default_library


def _font_files(dirs: Iterable[Path]) -> list[str]:
    seen: set[str] = set()
    files: list[str] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for root, _subdirs, names in os.walk(directory):
            for name in sorted(names):
                if Path(name).suffix.lower() not in _FONT_SUFFIXES:
                    continue
                path = os.path.join(root, name)
                real = os.path.realpath(path)
                if real not in seen:
                    seen.add(real)
                    files.append(path)
    return files


def _pick_face(faces: list[FontFace], *, bold: bool, italic: bool) -> FontFace:
    """同名的几个字形集里挑粗细、斜体最接近的；要粗体没有粗体时给常规体（PowerPoint 假粗体）。"""
    wanted = 700 if bold else 400

    def score(face: FontFace) -> tuple[int, int, int]:
        return (
            0 if face.bold == bool(bold) else 1,
            0 if face.italic == bool(italic) else 1,
            abs(face.weight - wanted),
        )

    best = min(faces, key=score)
    if bold and not best.bold:
        # 没有粗体：退回最接近常规的那一个，不拿细体凑。
        regular = [face for face in faces if face.italic == best.italic]
        best = min(regular or faces, key=lambda face: abs(face.weight - 400))
    return best


# ---------------------------------------------------------------- sfnt 读取


@dataclass(frozen=True)
class _SfntTables:
    units_per_em: int
    advances: tuple[int, ...]
    glyph_for: object  # callable: codepoint -> glyph id（0 = 没有）


def _font_offsets(handle) -> list[int]:
    handle.seek(0)
    head = handle.read(12)
    if len(head) < 12:
        raise ValueError("too short")
    if head[:4] == b"ttcf":
        count = struct.unpack(">I", head[8:12])[0]
        if count > 512:
            raise ValueError("bad ttc")
        return list(struct.unpack(f">{count}I", handle.read(4 * count)))
    return [0]


def _table_directory(handle, offset: int) -> dict[bytes, tuple[int, int]]:
    handle.seek(offset)
    header = handle.read(12)
    if len(header) < 12 or header[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise ValueError("not an sfnt")
    count = struct.unpack(">H", header[4:6])[0]
    raw = handle.read(16 * count)
    tables = {}
    for i in range(count):
        tag, _checksum, table_offset, length = struct.unpack(">4sIII", raw[16 * i : 16 * i + 16])
        tables[tag] = (table_offset, length)
    return tables


def _read_table(handle, tables, tag: bytes) -> bytes | None:
    if tag not in tables:
        return None
    offset, length = tables[tag]
    handle.seek(offset)
    data = handle.read(length)
    return data if len(data) == length else None


def _decode_name(platform: int, encoding: int, raw: bytes) -> str | None:
    if platform in (0, 3):
        if platform == 3 and encoding not in (0, 1, 10):
            return None
        return raw.decode("utf-16-be", errors="ignore")
    if platform == 1:
        if encoding == 0:
            return raw.decode("mac_roman", errors="ignore")
        if encoding == 25:
            return raw.decode("gb2312", errors="ignore")
    return None


def _read_name_entries(path: str) -> list[tuple[FontFace, set[str]]]:
    """文件里每个字形集，和它能被叫的名字（族名、排版族名、全名，各种语言），都已小写。"""
    entries = []
    with open(path, "rb") as handle:
        for index, offset in enumerate(_font_offsets(handle)):
            tables = _table_directory(handle, offset)
            name_table = _read_table(handle, tables, b"name")
            if not name_table or len(name_table) < 6:
                continue
            count, string_offset = struct.unpack(">HH", name_table[2:6])
            names: set[str] = set()
            legacy_family = ""
            subfamily = ""
            for i in range(count):
                base = 6 + 12 * i
                if base + 12 > len(name_table):
                    break
                platform, encoding, language, name_id, length, offset_in = struct.unpack(
                    ">HHHHHH", name_table[base : base + 12]
                )
                if name_id not in (1, 2, 4, 16):
                    continue
                start = string_offset + offset_in
                text = _decode_name(platform, encoding, name_table[start : start + length])
                if not text:
                    continue
                text = " ".join(text.split())
                if name_id == 2:
                    if platform == 3 and language == 0x409 or not subfamily:
                        subfamily = text
                    continue
                names.add(text.casefold())
                if name_id == 1 and (platform == 3 and language == 0x409 or not legacy_family):
                    legacy_family = text
            if not names:
                continue
            weight, italic = _style(handle, tables, subfamily)
            face = FontFace(path=path, index=index, family=legacy_family, weight=weight, italic=italic)
            entries.append((face, names))
    return entries


def _style(handle, tables, subfamily: str) -> tuple[int, bool]:
    lowered = subfamily.casefold()
    os2 = _read_table(handle, tables, b"OS/2")
    if os2 and len(os2) >= 64:
        weight = struct.unpack(">H", os2[4:6])[0]
        selection = struct.unpack(">H", os2[62:64])[0]
        italic = bool(selection & 0x01)
        if selection & 0x20:
            weight = max(weight, 700)
        return (weight or 400), italic
    weight = 700 if "bold" in lowered else 400
    return weight, ("italic" in lowered or "oblique" in lowered)


def _read_metric_tables(path: str, index: int) -> _SfntTables | None:
    with open(path, "rb") as handle:
        offsets = _font_offsets(handle)
        if index >= len(offsets):
            return None
        tables = _table_directory(handle, offsets[index])
        head = _read_table(handle, tables, b"head")
        hhea = _read_table(handle, tables, b"hhea")
        cmap = _read_table(handle, tables, b"cmap")
        if not (head and hhea and cmap):
            return None
        units = struct.unpack(">H", head[18:20])[0] or 1000
        metric_count = struct.unpack(">H", hhea[34:36])[0]
        hmtx_offset, hmtx_length = tables.get(b"hmtx", (0, 0))
        if not metric_count or hmtx_length < 4 * metric_count:
            return None
        handle.seek(hmtx_offset)
        raw = handle.read(4 * metric_count)
        # 只有前 metric_count 个字形各记宽度，之后的字形（多为汉字）都和最后一个一样宽。
        advances = struct.unpack(f">{2 * metric_count}H", raw)[0::2]
    glyph_for = _cmap_lookup(cmap)
    if glyph_for is None:
        return None
    return _SfntTables(units_per_em=units, advances=tuple(advances), glyph_for=glyph_for)


def _cmap_lookup(cmap: bytes):
    """选一张 Unicode 子表（优先 format 12，其次 format 4），返回 码位 → 字形号 的函数。"""
    if len(cmap) < 4:
        return None
    count = struct.unpack(">H", cmap[2:4])[0]
    candidates = []
    for i in range(count):
        base = 4 + 8 * i
        if base + 8 > len(cmap):
            break
        platform, encoding, offset = struct.unpack(">HHI", cmap[base : base + 8])
        if offset + 2 > len(cmap):
            continue
        fmt = struct.unpack(">H", cmap[offset : offset + 2])[0]
        unicode_table = platform == 0 or (platform == 3 and encoding in (1, 10))
        symbol_table = platform == 3 and encoding == 0
        if fmt == 12 and unicode_table:
            candidates.append((0, fmt, offset))
        elif fmt == 4 and unicode_table:
            candidates.append((1, fmt, offset))
        elif fmt == 4 and symbol_table:
            candidates.append((2, fmt, offset))
    if not candidates:
        return None
    _rank, fmt, offset = min(candidates)
    if fmt == 12:
        return _format12(cmap, offset)
    lookup = _format4(cmap, offset)
    if _rank == 2:
        # 符号字体（Wingdings 等）的字符放在 U+F000 区：普通码位映射过去再查。
        return lambda code: lookup(code) or (lookup(0xF000 + code) if code < 0x100 else 0)
    return lookup


def _format12(cmap: bytes, offset: int):
    groups_count = struct.unpack(">I", cmap[offset + 12 : offset + 16])[0]
    starts, ends, glyphs = [], [], []
    for i in range(groups_count):
        base = offset + 16 + 12 * i
        if base + 12 > len(cmap):
            break
        start, end, glyph = struct.unpack(">III", cmap[base : base + 12])
        starts.append(start)
        ends.append(end)
        glyphs.append(glyph)

    def lookup(code: int) -> int:
        i = bisect_right(starts, code) - 1
        if i < 0 or code > ends[i]:
            return 0
        return glyphs[i] + code - starts[i]

    return lookup


def _format4(cmap: bytes, offset: int):
    seg_count = struct.unpack(">H", cmap[offset + 6 : offset + 8])[0] // 2
    base = offset + 14
    ends = struct.unpack(f">{seg_count}H", cmap[base : base + 2 * seg_count])
    base += 2 * seg_count + 2
    starts = struct.unpack(f">{seg_count}H", cmap[base : base + 2 * seg_count])
    base += 2 * seg_count
    deltas = struct.unpack(f">{seg_count}h", cmap[base : base + 2 * seg_count])
    base += 2 * seg_count
    range_base = base
    range_offsets = struct.unpack(f">{seg_count}H", cmap[base : base + 2 * seg_count])

    def lookup(code: int) -> int:
        if code > 0xFFFF:
            return 0
        i = _first_end_at_least(ends, code)
        if i >= seg_count or starts[i] > code:
            return 0
        if range_offsets[i] == 0:
            return (code + deltas[i]) & 0xFFFF
        position = range_base + 2 * i + range_offsets[i] + 2 * (code - starts[i])
        if position + 2 > len(cmap):
            return 0
        glyph = struct.unpack(">H", cmap[position : position + 2])[0]
        return (glyph + deltas[i]) & 0xFFFF if glyph else 0

    return lookup


def _first_end_at_least(ends: tuple[int, ...], code: int) -> int:
    lo, hi = 0, len(ends)
    while lo < hi:
        mid = (lo + hi) // 2
        if ends[mid] < code:
            lo = mid + 1
        else:
            hi = mid
    return lo
