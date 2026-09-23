"""Reference-aware, byte-preserving worksheet-title changes for OOXML workbooks.

Only XML parts containing a changed title/reference are serialized. An unsafe
reference cancels the entire rename; the output then contains the original book.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree
from openpyxl.formula import Tokenizer

from core.xlsx_patcher import is_generated_original_sheet_title

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_FORMULA_TAGS = {f"{{{_MAIN}}}{name}" for name in ("f", "definedName", "formula", "formula1", "formula2")}
_FORMULA_TAGS.add(f"{{{_CHART}}}f")
_INVALID_TITLE = re.compile(r"[\\/*?:\[\]]")
_DYNAMIC = re.compile(r"\b(?:_xlfn\.)?INDIRECT\s*\(", re.I)


@dataclass(frozen=True)
class RenameResult:
    status: str  # renamed, unchanged, or preserved
    names: dict[str, str]
    changed_parts: tuple[str, ...] = ()
    reason: str = ""


class UnsafeSheetRename(ValueError):
    """A sheet reference cannot be changed with confidence."""


def _title(value: str) -> str:
    value = value.strip().strip("'")
    value = _INVALID_TITLE.sub("_", value).strip()
    return value[:31]


def _plans(names: list[str], requested: dict[str, str]) -> dict[str, str]:
    unknown = set(requested) - set(names)
    if unknown:
        raise UnsafeSheetRename(f"未知工作表：{', '.join(sorted(unknown))}")
    originals = {name for name in names if is_generated_original_sheet_title(name, names)}
    candidates = {name: _title(requested[name]) for name in names
                  if name in requested and name not in originals}
    fixed = {name.casefold() for name in names if not candidates.get(name)}
    plan: dict[str, str] = {}
    for old in names:
        base = candidates.get(old)
        if not base:
            continue
        candidate = base
        suffix = 2
        while candidate.casefold() in fixed:
            ending = f"_{suffix}"
            candidate = base[: 31 - len(ending)] + ending
            suffix += 1
        fixed.add(candidate.casefold())
        if candidate != old:
            plan[old] = candidate
    return plan


def _rewrite_range(value: str, plan: dict[str, str]) -> str:
    # One substitution pass prevents cascades when two translated titles swap.
    patterns = []
    replacements = {}
    for old, new in plan.items():
        quoted = "'" + old.replace("'", "''") + "'!"
        replacement = "'" + new.replace("'", "''") + "'!"
        patterns.append(re.escape(quoted))
        replacements[quoted.casefold()] = replacement
        if re.fullmatch(r"[\w.]+", old):
            patterns.append(rf"(?<![\w.\[\]']){re.escape(old)}!")
            replacements[(old + "!").casefold()] = replacement
    if not patterns:
        return value
    compiled = re.compile("|".join(sorted(patterns, key=len, reverse=True)), re.I)
    return compiled.sub(lambda match: replacements[match.group().casefold()], value)


def _rewrite_formula(value: str, plan: dict[str, str]) -> str:
    if _DYNAMIC.search(value):
        raise UnsafeSheetRename("存在 INDIRECT 动态工作表引用")
    had_equals = value.startswith("=")
    try:
        tokens = Tokenizer(value if had_equals else "=" + value).items
    except Exception as exc:
        raise UnsafeSheetRename("公式无法解析") from exc
    updated = []
    for token in tokens:
        piece = token.value
        if token.type == "OPERAND" and token.subtype == "RANGE":
            head = piece.split("!", 1)[0] if "!" in piece else ""
            if (":" in head or "[" in head) and any(
                old.casefold() in head.casefold() for old in plan
            ):
                raise UnsafeSheetRename("存在三维或外部工作簿引用")
            piece = _rewrite_range(piece, plan)
            if piece == token.value and any(
                re.search(re.escape(old) + r"!", token.value, re.I)
                for old in plan
            ):
                raise UnsafeSheetRename("存在无法安全改写的工作表引用")
            if "!" in piece and (":" in piece.split("!", 1)[0] or "[" in piece.split("!", 1)[0]):
                if piece != token.value:
                    raise UnsafeSheetRename("存在三维或外部工作簿引用")
        updated.append(piece)
    return ("=" if had_equals else "") + "".join(updated)


def _has_direct_reference(value: str, plan: dict[str, str]) -> bool:
    if "!" not in value:
        return False
    for old in plan:
        quoted = "'" + old.replace("'", "''") + "'!"
        if re.search(re.escape(quoted), value, re.I):
            return True
        if re.fullmatch(r"[\w.]+", old) and re.search(
            rf"(?<![\w.\[\]']){re.escape(old)}!", value, re.I
        ):
            return True
    return False


def _rewrite_part(filename: str, data: bytes, plan: dict[str, str]) -> bytes:
    if not filename.endswith((".xml", ".rels")):
        return data
    known_part = (filename == "xl/workbook.xml" or filename.startswith("xl/worksheets/")
                  or filename.startswith("xl/charts/") or filename.endswith(".rels"))
    # An unknown part can hold references in XML entities, so inspect it when
    # it contains an exclamation mark even if the old title is not raw UTF-8.
    if not known_part and b"!" not in data and b"&#" not in data and b"\x00" not in data[:100]:
        return data
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise UnsafeSheetRename(f"XML 无法解析：{filename}") from exc
    dirty = False
    for node in root.iter():
        handled_text = False
        handled_attrs: set[str] = set()
        if filename == "xl/workbook.xml" and node.tag == f"{{{_MAIN}}}sheet":
            handled_attrs.add("name")
            current = node.get("name")
            if current in plan:
                node.set("name", plan[current])
                dirty = True
        if known_part and node.tag in _FORMULA_TAGS and node.text:
            handled_text = True
            changed = _rewrite_formula(node.text, plan)
            if changed != node.text:
                node.text = changed
                dirty = True
        if node.tag == f"{{{_MAIN}}}hyperlink":
            handled_attrs.add("location")
            location = node.get("location")
            if location:
                changed = _rewrite_range(location, plan)
                if changed != location:
                    node.set("location", changed)
                    dirty = True
        if node.tag == f"{{{_PKG_REL}}}Relationship" and str(node.get("Type", "")).endswith("/hyperlink"):
            target = node.get("Target", "")
            if target.startswith("#"):
                handled_attrs.add("Target")
                changed = "#" + _rewrite_range(target[1:], plan)
                if changed != target:
                    node.set("Target", changed)
                    dirty = True
        # Unknown fields are preserved only when they cannot contain a direct
        # reference to a title being changed. This covers table/pivot parts.
        if not handled_text and node.text and _has_direct_reference(node.text, plan):
            raise UnsafeSheetRename(f"未处理的工作表引用：{filename}")
        if node.tail and _has_direct_reference(node.tail, plan):
            raise UnsafeSheetRename(f"未处理的工作表引用：{filename}")
        for key, value in node.attrib.items():
            if key not in handled_attrs and _has_direct_reference(value, plan):
                raise UnsafeSheetRename(f"未处理的工作表引用：{filename}")
    return etree.tostring(root, encoding="UTF-8", xml_declaration=data.lstrip().startswith(b"<?xml")) if dirty else data


def rename_worksheets(source: str | Path, destination: str | Path, requested: dict[str, str]) -> RenameResult:
    """Apply requested translated titles, or preserve the entire original file.

    ``requested`` maps existing titles to translated titles. Invalid characters
    become underscores; collisions receive _2, _3, etc. Original-copy sheets
    generated by the writer keep their original titles. The destination is
    atomically replaced only after a complete, readable ZIP has been written.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage_fd, stage_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(stage_fd)
    stage = Path(stage_name)
    try:
        try:
            with ZipFile(source) as package:
                workbook = etree.fromstring(package.read("xl/workbook.xml"))
                names = [node.get("name", "") for node in workbook.iter(f"{{{_MAIN}}}sheet")]
                plan = _plans(names, requested)
                if plan:
                    changed_parts = []
                    with ZipFile(stage, "w") as output:
                        for info in package.infolist():
                            original = package.read(info.filename)
                            changed = _rewrite_part(info.filename, original, plan)
                            if changed != original:
                                changed_parts.append(info.filename)
                            output.writestr(info, changed)
                    with ZipFile(stage) as check:
                        if check.testzip() is not None:
                            raise UnsafeSheetRename("输出压缩包校验未通过")
                    stage.replace(destination)
                    return RenameResult("renamed", plan, tuple(changed_parts))
                reason = "无可改名工作表"
                status = "unchanged"
        except (UnsafeSheetRename, BadZipFile, KeyError, etree.XMLSyntaxError) as exc:
            reason = str(exc)
            status = "preserved"
        # Preserve a complete deliverable even if references are unsupported.
        with source.open("rb") as inp, stage.open("wb") as out:
            while chunk := inp.read(1024 * 1024):
                out.write(chunk)
        stage.replace(destination)
        return RenameResult(status, {}, reason=reason)
    finally:
        stage.unlink(missing_ok=True)
