"""Reading and writing back a text file in the encoding it came in.

Shared by the bundled text-based adapters (「文本」, 「字幕」).  Standard library only.

``read_text_file`` recognises UTF-8 (with or without a byte-order mark),
UTF-16 with a byte-order mark and GB18030 (which covers GBK and GB2312).  A
GB18030 file is written back as UTF-8 with a byte-order mark: translations
often hold characters a GBK editor cannot show (French accents and the like),
and every editor recognises UTF-8 with a byte-order mark.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TextSource:
    text: str
    #: The encoding ``encode`` writes with.
    encoding: str
    #: The byte-order mark ``encode`` puts in front (empty for none).
    bom: bytes

    def encode(self, text: str) -> bytes:
        return self.bom + text.encode(self.encoding)


def read_text_file(path: str | Path) -> TextSource:
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return TextSource(raw[len(codecs.BOM_UTF8):].decode("utf-8"), "utf-8", codecs.BOM_UTF8)
    for bom, encoding in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            return TextSource(raw[len(bom):].decode(encoding), encoding, bom)
    try:
        return TextSource(raw.decode("utf-8"), "utf-8", b"")
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("gb18030")
    except UnicodeDecodeError:
        raise ValueError("无法识别文件编码，请将文件另存为 UTF-8 后再试。") from None
    return TextSource(text, "utf-8", codecs.BOM_UTF8)
