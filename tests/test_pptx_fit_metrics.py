"""PPT 缩字量字宽（core/font_metrics.py）：按名字找字体、读字宽、找不到时估算。

测试用 Pillow 自带的 Aileron 字体（写进临时目录），不依赖机器上装了什么字体。
"""
from __future__ import annotations

import struct
from io import BytesIO

import pytest
from PIL import ImageFont

from core import font_metrics
from core.font_metrics import FALLBACK_SAFETY, FontLibrary, fallback_advance


def _aileron_bytes() -> bytes:
    font = ImageFont.load_default(size=100)
    data = getattr(font, "font_bytes", None)
    if not data:
        pytest.skip("Pillow 没有带 FreeType 字体")
    return data


def _as_ttc(ttf: bytes, copies: int = 2) -> bytes:
    """把一个 .ttf 包成含 copies 份的 .ttc（表偏移改成从文件头算）。"""
    header = 12 + 4 * copies
    count = struct.unpack(">H", ttf[4:6])[0]
    body = bytearray(ttf)
    for i in range(count):
        base = 12 + 16 * i
        offset = struct.unpack(">I", ttf[base + 8 : base + 12])[0]
        body[base + 8 : base + 12] = struct.pack(">I", offset + header)
    out = BytesIO()
    out.write(b"ttcf" + struct.pack(">HHI", 1, 0, copies))
    out.write(struct.pack(f">{copies}I", *([header] * copies)))
    out.write(bytes(body))
    return out.getvalue()


@pytest.fixture()
def font_dir(tmp_path):
    (tmp_path / "Aileron-Regular.ttf").write_bytes(_aileron_bytes())
    return tmp_path


def test_finds_font_by_family_name_case_insensitive(font_dir):
    library = FontLibrary([font_dir])
    face = library.resolve("aileron")
    assert face is not None
    assert face.metrics.face.family == "Aileron"
    assert library.resolve("  AILERON  ") is face
    assert library.resolve("Calibri") is None


def test_advances_match_pillow_getlength(font_dir):
    pil = ImageFont.truetype(str(font_dir / "Aileron-Regular.ttf"), size=1000)
    face = FontLibrary([font_dir]).resolve("Aileron")
    for char in "AaWwil.,: 019":
        assert face.advance(char) == pytest.approx(pil.getlength(char) / 1000, abs=1e-3)


def test_missing_glyph_returns_none(font_dir):
    # Pillow 带的 Aileron 是精简字符集，没有带音调的字母和汉字。
    face = FontLibrary([font_dir]).resolve("Aileron")
    assert face.advance("é") is None
    assert face.advance("中") is None


def test_bold_without_bold_face_is_synthetic(font_dir):
    library = FontLibrary([font_dir])
    regular = library.resolve("Aileron")
    bold = library.resolve("Aileron", bold=True)
    assert bold.synthetic_bold and not regular.synthetic_bold
    assert bold.advance("A") == pytest.approx(regular.advance("A") * 1.02)


def test_reads_every_face_in_a_ttc(tmp_path):
    (tmp_path / "pair.ttc").write_bytes(_as_ttc(_aileron_bytes()))
    library = FontLibrary([tmp_path])
    face = library.resolve("Aileron")
    assert face is not None and face.metrics.face.path.endswith("pair.ttc")
    pil = ImageFont.truetype(BytesIO(_aileron_bytes()), size=1000)
    assert face.advance("W") == pytest.approx(pil.getlength("W") / 1000, abs=1e-3)
    faces = library._index()["aileron"]
    assert sorted(f.index for f in faces) == [0, 1]


def test_metric_compatible_alias(font_dir, monkeypatch):
    monkeypatch.setitem(font_metrics.METRIC_ALIASES, "calibri", ("aileron",))
    face = FontLibrary([font_dir]).resolve("Calibri")
    assert face is not None and face.metrics.face.family == "Aileron"


def test_broken_and_foreign_files_are_skipped(tmp_path):
    (tmp_path / "broken.ttf").write_bytes(b"not a font at all")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Aileron.otf").write_bytes(_aileron_bytes())
    assert FontLibrary([tmp_path]).resolve("Aileron") is not None
    assert FontLibrary([tmp_path / "missing"]).resolve("Aileron") is None


def test_fallback_estimate():
    assert fallback_advance("中") == 1.0
    assert fallback_advance("，") == 1.0
    assert fallback_advance("A") == pytest.approx(0.667 * FALLBACK_SAFETY)
    # 带音调的字母按去掉音调的算
    assert fallback_advance("é") == fallback_advance("e")
    assert fallback_advance("œ") == pytest.approx(0.944 * FALLBACK_SAFETY)
    assert fallback_advance("́") == 0.0


def test_system_font_dirs_lists_platform_dirs():
    dirs = font_metrics.system_font_dirs()
    assert dirs and all(hasattr(d, "is_dir") for d in dirs)
