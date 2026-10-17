"""写完 Office 文件后核对：不该改的部件必须和原文件一样。

翻译只改正文这类部件，图片、主题、字体、嵌入对象、自定义 XML 这些应该原样照抄。
这里逐个比对：字节一样最好；XML 部件允许被重新序列化（引号、声明、空白写法不同），
按规范化后的内容比较。原文件里被引用的部件在输出里没了，也算不对。原文件里没被任何
关系引用的孤立部件，保存时被丢掉不算问题。

Word 写回在 core/word_document._atomic_docx_writer 里调用；不对就不替换输出、报出部件名。
"""
from __future__ import annotations

import posixpath
import zipfile
from collections.abc import Callable
from pathlib import Path

from lxml import etree

_CT_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
# 和 python-docx 读 XML 部件用同样的设置（去掉可忽略的空白），它重新序列化过、内容没变的部件才比得上
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=True)


class PackagePartsChanged(RuntimeError):
    """输出文件里本不该改的部件变了。parts：部件名列表。"""

    def __init__(self, parts: list[str]):
        self.parts = list(parts)
        shown = "、".join(self.parts[:5]) + (f" 等 {len(self.parts)} 个" if len(self.parts) > 5 else "")
        super().__init__(f"保存后核对不通过：没有改动的部件与原文件不一致（{shown}），已保留原来的输出")


def _content_types(package: zipfile.ZipFile) -> dict[str, str]:
    """部件名（不带开头的 /）→ 内容类型。"""
    try:
        root = etree.fromstring(package.read("[Content_Types].xml"), _PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return {}
    defaults = {
        str(node.get("Extension") or "").lower(): str(node.get("ContentType") or "")
        for node in root.iter(f"{_CT_NS}Default")
    }
    overrides = {
        str(node.get("PartName") or "").lstrip("/"): str(node.get("ContentType") or "")
        for node in root.iter(f"{_CT_NS}Override")
    }
    types: dict[str, str] = {}
    for name in package.namelist():
        if name.endswith("/"):
            continue
        types[name] = overrides.get(name) or defaults.get(name.rsplit(".", 1)[-1].lower(), "")
    return types


def _referenced_parts(package: zipfile.ZipFile) -> set[str]:
    """所有关系文件里指向包内的目标部件。"""
    referenced: set[str] = set()
    for name in package.namelist():
        if not name.endswith(".rels"):
            continue
        # word/_rels/document.xml.rels 的目标相对 word/；_rels/.rels 相对包根
        base = posixpath.dirname(posixpath.dirname(name))
        try:
            root = etree.fromstring(package.read(name), _PARSER)
        except etree.XMLSyntaxError:
            continue
        for rel in root.iter(f"{_REL_NS}Relationship"):
            if str(rel.get("TargetMode") or "").lower() == "external":
                continue
            target = str(rel.get("Target") or "").split("#", 1)[0]
            if not target:
                continue
            path = target.lstrip("/") if target.startswith("/") else posixpath.join(base, target)
            referenced.add(posixpath.normpath(path))
    return referenced


def _canonical(blob: bytes) -> bytes | None:
    try:
        return etree.tostring(etree.fromstring(blob, _PARSER), method="c14n")
    except etree.XMLSyntaxError:
        return None


def _looks_like_xml(name: str, content_type: str) -> bool:
    return name.endswith((".xml", ".rels", ".vml")) or content_type.endswith("xml")


def changed_untouched_parts(
    source: str | Path,
    output: str | Path,
    may_change: Callable[[str, str], bool],
) -> list[str]:
    """``may_change(部件名, 内容类型)`` 为真的部件允许改；其余部件改了或丢了的，按名字返回。"""
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as written:
        types = _content_types(original)
        written_names = set(written.namelist())
        referenced: set[str] | None = None
        problems: list[str] = []
        for name in sorted(types):
            content_type = types[name]
            if name == "[Content_Types].xml" or name.endswith(".rels") or may_change(name, content_type):
                continue
            if name not in written_names:
                if referenced is None:
                    referenced = _referenced_parts(original)
                if name in referenced:
                    problems.append(name)
                continue
            before, after = original.read(name), written.read(name)
            if before == after:
                continue
            if _looks_like_xml(name, content_type):
                canonical = _canonical(before)
                if canonical is not None and canonical == _canonical(after):
                    continue
            problems.append(name)
    return problems
