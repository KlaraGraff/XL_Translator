"""原位替换写回译文用的字体：按译文的文字挑电脑自带的字体，缺字时换下一款。

「译文字体」两种（``pdf.inplace_font``，与 CAD 的「译文字体」对应）：

* ``sans``：无衬线字体（中文黑体、日文ゴシック、韩文맑은 고딕、西文 Arial），图纸常用；
* ``original``：接近原文字体，原文是宋体、Times 一类衬线字体时用衬线字体。

两种都随原文用粗体：原文这一行是粗体（字体名带 Bold，或文字层标了粗体）时写粗体。
画成线条、靠识别读出的字没有字体信息，按常规字形写。

字体都是各系统自带的；同一行译文里的每个字都要在这款字体里有字形，缺一个就换下一款，
都缺就不写这一行（保留原文、列进复核清单），不会写出方框或问号。

阿拉伯文、希伯来文、泰文、印度各文字等需要连写或从右往左排的文字，写回时要做排版
（字形连写、重排顺序），这里只会把字一个挨一个从左往右摆，写出来是错的：这些目标语言
不走原位替换（``target_supported``），自定义语言的译文里出现这类文字时那一行不写。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.cad_language import base_lang

from .common import fitz

SANS = "sans"
ORIGINAL = "original"
FONT_STYLES = (SANS, ORIGINAL)

# 写回时要做字形连写或从右往左排的文字（PyMuPDF 的 insert_text 不做这些）。
COMPLEX_SCRIPT_TARGETS = frozenset({
    "ar", "fa", "ur", "he", "th", "lo", "km", "my", "hi", "mr", "ne", "bn", "pa", "gu",
    "ta", "te", "kn", "ml", "si", "am",
})
_COMPLEX_SCRIPT_RE = re.compile(
    "[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff"   # 希伯来文、阿拉伯文
    "\u0900-\u0dff"                              # 天城文到僧伽罗文
    "\u0e00-\u0eff\u0f00-\u0fff\u1000-\u109f"    # 泰文、老挝文、藏文、缅甸文
    "\u1200-\u139f\u1780-\u17ff]"                # 埃塞俄比亚文、高棉文
)
_HAN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
CJK_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af]")

_SERIF_NAME_RE = re.compile(
    r"times|(?<!sans)(?<!sans-)serif|garamond|georgia|cambria|minion|palatino|bookman|century|"
    r"song|sun|ming|mincho|batang|myungjo|fangsong|kaiti|宋|明|仿宋|楷",
    re.I,
)
_BOLD_NAME_RE = re.compile(r"bold|black|heavy|semibold|demibold|粗", re.I)


def target_supported(target_lang: str) -> bool:
    """这种目标语言能不能原位替换（需要连写或从右往左排的文字不能）。"""
    return base_lang(target_lang) not in COMPLEX_SCRIPT_TARGETS


def needs_shaping(text: str) -> bool:
    return bool(_COMPLEX_SCRIPT_RE.search(text or ""))


def has_cjk(text: str) -> bool:
    return bool(CJK_TEXT_RE.search(text or ""))


_SANS_NAME_RE = re.compile(
    r"romans|sans|arial|helvetica|verdana|tahoma|calibri|segoe|roboto|gothic|hei|yahei|dengxian|等线|黑", re.I)


def looks_serif(font_name: str, flags: int = 0) -> bool:
    """Serif by the font's name; a name that says nothing (subset fonts such as
    「CIDFont+F1」) goes by the font's own serif flag (span flags bit 4)."""
    name = str(font_name or "").split("+", 1)[-1]
    if _SANS_NAME_RE.search(name):
        return False
    if _SERIF_NAME_RE.search(name):
        return True
    return bool(int(flags or 0) & 4)


def looks_bold(font_name: str, flags: int = 0) -> bool:
    name = str(font_name or "").split("+", 1)[-1]
    return bool(int(flags or 0) & 16) or bool(_BOLD_NAME_RE.search(name))


# ── 各系统自带字体的位置 ──────────────────────────────────────────────

_WIN = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_MAC = Path("/System/Library/Fonts")
_MAC_SUP = _MAC / "Supplemental"
_MAC_LIB = Path("/Library/Fonts")
_LINUX = Path("/usr/share/fonts")
_NOTO_CJK = [_LINUX / "opentype/noto/NotoSansCJK-Regular.ttc", _LINUX / "truetype/noto/NotoSansCJK-Regular.ttc",
             _LINUX / "noto-cjk/NotoSansCJK-Regular.ttc"]
_NOTO_CJK_BOLD = [_LINUX / "opentype/noto/NotoSansCJK-Bold.ttc", _LINUX / "truetype/noto/NotoSansCJK-Bold.ttc",
                  _LINUX / "noto-cjk/NotoSansCJK-Bold.ttc"]
_NOTO_SERIF_CJK = [_LINUX / "opentype/noto/NotoSerifCJK-Regular.ttc", _LINUX / "truetype/noto/NotoSerifCJK-Regular.ttc",
                   _LINUX / "noto-cjk/NotoSerifCJK-Regular.ttc"]
_WQY = [_LINUX / "truetype/wqy/wqy-zenhei.ttc", _LINUX / "truetype/wqy/wqy-microhei.ttc"]

# 中文黑体：排在最前的是以前原位替换一直用的那几款（顺序不变，译成中文的页面看起来和以前一样）。
_ZH_SANS = [_WIN / "simhei.ttf", _WIN / "msyh.ttc", _WIN / "simsun.ttc", _MAC_SUP / "Arial Unicode.ttf",
            _MAC / "STHeiti Medium.ttc", _MAC_LIB / "Arial Unicode.ttf", *_NOTO_CJK, *_WQY]
_ZH_SERIF = [_WIN / "simsun.ttc", _MAC_SUP / "Songti.ttc", *_NOTO_SERIF_CJK]
_JA_SANS = [_WIN / "YuGothM.ttc", _WIN / "meiryo.ttc", _WIN / "msgothic.ttc", _MAC / "ヒラギノ角ゴシック W3.ttc",
            *_NOTO_CJK]
_JA_SANS_BOLD = [_WIN / "YuGothB.ttc", _WIN / "meiryob.ttc", _MAC / "ヒラギノ角ゴシック W6.ttc", *_NOTO_CJK_BOLD]
_JA_SERIF = [_WIN / "yumin.ttf", _WIN / "msmincho.ttc", _MAC / "ヒラギノ明朝 ProN.ttc", *_NOTO_SERIF_CJK]
_KO_SANS = [_WIN / "malgun.ttf", _MAC / "AppleSDGothicNeo.ttc", *_NOTO_CJK]
_KO_SANS_BOLD = [_WIN / "malgunbd.ttf", *_NOTO_CJK_BOLD]
_KO_SERIF = [_WIN / "batang.ttc", _MAC_SUP / "AppleMyungjo.ttf", *_NOTO_SERIF_CJK]
_LATIN_SANS = [_WIN / "arial.ttf", _MAC_SUP / "Arial.ttf", _MAC_LIB / "Arial.ttf",
               _LINUX / "truetype/liberation/LiberationSans-Regular.ttf",
               _LINUX / "truetype/liberation2/LiberationSans-Regular.ttf", _LINUX / "truetype/dejavu/DejaVuSans.ttf"]
_LATIN_SANS_BOLD = [_WIN / "arialbd.ttf", _MAC_SUP / "Arial Bold.ttf", _MAC_LIB / "Arial Bold.ttf",
                    _LINUX / "truetype/liberation/LiberationSans-Bold.ttf",
                    _LINUX / "truetype/liberation2/LiberationSans-Bold.ttf", _LINUX / "truetype/dejavu/DejaVuSans-Bold.ttf"]
_LATIN_SERIF = [_WIN / "times.ttf", _MAC_SUP / "Times New Roman.ttf", _MAC_LIB / "Times New Roman.ttf",
                _LINUX / "truetype/liberation/LiberationSerif-Regular.ttf",
                _LINUX / "truetype/liberation2/LiberationSerif-Regular.ttf", _LINUX / "truetype/dejavu/DejaVuSerif.ttf"]
_LATIN_SERIF_BOLD = [_WIN / "timesbd.ttf", _MAC_SUP / "Times New Roman Bold.ttf", _MAC_LIB / "Times New Roman Bold.ttf",
                     _LINUX / "truetype/liberation/LiberationSerif-Bold.ttf",
                     _LINUX / "truetype/liberation2/LiberationSerif-Bold.ttf",
                     _LINUX / "truetype/dejavu/DejaVuSerif-Bold.ttf"]

# (文字, 衬线, 粗体) → 依次试的字体。粗体、衬线没有时退到常规、无衬线（见 _candidates）。
_TABLE: dict[tuple[str, bool, bool], list[Path]] = {
    ("zh", False, False): _ZH_SANS,
    # 黑体本身就粗，中文粗体也用它，和同一页的常规字是同一款字体。
    ("zh", False, True): [_WIN / "simhei.ttf", _WIN / "msyhbd.ttc", _MAC / "STHeiti Medium.ttc", *_NOTO_CJK_BOLD],
    ("zh", True, False): _ZH_SERIF,
    ("ja", False, False): _JA_SANS,
    ("ja", False, True): _JA_SANS_BOLD,
    ("ja", True, False): _JA_SERIF,
    ("ko", False, False): _KO_SANS,
    ("ko", False, True): _KO_SANS_BOLD,
    ("ko", True, False): _KO_SERIF,
    ("latin", False, False): _LATIN_SANS,
    ("latin", False, True): _LATIN_SANS_BOLD,
    ("latin", True, False): _LATIN_SERIF,
    ("latin", True, True): _LATIN_SERIF_BOLD,
}


def find_cjk_font() -> Path | None:
    """译成中文用的那款黑体（以前原位替换唯一用的字体）。"""
    return next((path for path in _ZH_SANS if path.exists()), None)


def text_script(text: str, target_lang: str = "") -> str:
    """译文主要用哪套字体：韩文、日文、中文或西文（拉丁、西里尔、希腊字母都算西文）。"""
    if _HANGUL_RE.search(text):
        return "ko"
    if _KANA_RE.search(text):
        return "ja"
    if _HAN_RE.search(text):
        return "ja" if base_lang(target_lang) == "ja" else "zh"
    return "latin"


def _candidates(script: str, serif: bool, bold: bool) -> list[Path]:
    order = [(serif, bold), (serif, False), (False, bold), (False, False)]
    out: list[Path] = []
    for s, b in order:
        out += _TABLE.get((script, s, b), [])
    # 本套都缺字时，再试别的几套（中日韩字体也带西文字母）。
    for other in ("zh", "ja", "ko", "latin"):
        if other != script:
            out += _TABLE.get((other, False, False), [])
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class FontChoice:
    path: Path
    name: str        # 页面里的字体资源名
    font: object     # fitz.Font，量宽度用


class FontSet:
    """一页用到的几款字体。``forced`` 给定时所有译文都用这一款（命令行、测试）。"""

    def __init__(self, target_lang: str = "zh", style: str = SANS, forced: Path | None = None) -> None:
        self.target_lang = target_lang
        self.style = style if style in FONT_STYLES else SANS
        self.forced = Path(forced) if forced else None
        self._fonts: dict[Path, object] = {}
        self._names: dict[Path, str] = {}

    def _font(self, path: Path):
        if path not in self._fonts:
            try:
                self._fonts[path] = fitz.Font(fontfile=str(path))
            except Exception:  # noqa: BLE001 - a broken font file is skipped like a missing one
                self._fonts[path] = None
        return self._fonts[path]

    def _choice(self, path: Path) -> FontChoice:
        name = self._names.setdefault(path, f"XLT{len(self._names)}")
        return FontChoice(path, name, self._fonts[path])

    def pick(self, text: str, *, bold: bool = False, serif: bool = False) -> FontChoice | None:
        """能把 ``text`` 每个字都写出来的第一款字体；没有就是 None（这一行不写）。"""
        chars = {ch for ch in text if not ch.isspace()}
        paths = [self.forced] if self.forced else _candidates(
            text_script(text, self.target_lang), serif and self.style == ORIGINAL, bold)
        for path in paths:
            if not path.exists():
                continue
            font = self._font(path)
            if font is not None and all(font.has_glyph(ord(ch)) for ch in chars):
                return self._choice(path)
        return None

    def used(self) -> list[FontChoice]:
        return [self._choice(path) for path in self._names]
