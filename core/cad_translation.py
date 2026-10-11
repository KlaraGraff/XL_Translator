"""CAD/DWG translation pipeline primitives.

The module deliberately keeps CAD format handling separate from the existing
Excel/Word/PDF runners.  It operates on an intermediate ASCII DXF file and
uses a small converter protocol for DWG import/export (the production adapter
is supplied by the CAD plugin, normally ODA File Converter).  The parser and
writer preserve every DXF record and only replace text group values.
"""
from __future__ import annotations

import codecs
import json
import hashlib
import os
import platform
import inspect
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from loguru import logger

# 摘要表格给人看；manifest / report 里仍存内部状态码。
_UNIT_STATUS_LABELS = {"translated": "已翻译", "unresolved": "没翻好"}

CAD_SUPPORTED_EXTENSIONS = frozenset({".dwg", ".dxf"})
# Stage weights are part of the UI contract and sum to 100.
# Nearly all of a drawing's time is the model translating it; matching and
# verifying are local and near-instant, and scan / write only take a few seconds
# when a DWG goes through the converter.  The old 15/10/45/20/10 split jumped a
# DXF to 25% at once and from 70% to 100% at the end.
CAD_STAGE_WEIGHTS: dict[str, int] = {
    "scan": 8,
    "match": 2,
    "translate": 80,
    "write": 8,
    "verify": 2,
}

_TEXT_ENTITY_TYPES = frozenset({"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "MINSERT"})
# MLEADER text lives in group 304 inside CONTEXT_DATA.  Group 302 and 303 are
# structural markers and must never be sent to the translator.
_MLEADER_TEXT_CODE = 304
_DXF_FORMAT_RE = re.compile(r"\\[A-Za-z]+(?!\+)")
# DXF's ``\\U+`` escape is a four-hex-digit code unit.  A greedy 4-6 digit
# expression consumes the first letters of an ASCII word after the escape
# (``\\U+4E00Door`` used to become one invalid code point).
_DXF_UNICODE_RE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_REPLACEMENT_CHARS = frozenset({"�", "�", "□"})


class CadPipelineError(RuntimeError):
    """A recoverable CAD pipeline error with a user-facing message."""


class CadStopRequested(CadPipelineError):
    """Raised when the user stops the task mid-file.

    A subclass so callers that only know ``CadPipelineError`` keep working,
    while the batch runner can record the file as stopped instead of failed.
    """


class CadConverter(Protocol):
    """Adapter used to convert between DWG and an intermediate ASCII DXF."""

    def dwg_to_dxf(self, source: Path, destination: Path) -> Path: ...

    def dxf_to_dwg(self, source: Path, destination: Path) -> Path: ...


# R2007 之前的 DXF 按 $DWGCODEPAGE 声明的 ANSI 代码页存字，国内老图纸基本是
# ANSI_936（GBK）。以前一律按 UTF-8 读，这类图纸整份读不进来。
_DXF_CODEPAGES = {
    "ANSI_874": "cp874",
    "ANSI_932": "cp932",
    "ANSI_936": "gbk",
    "ANSI_949": "cp949",
    "ANSI_950": "big5",
    "ANSI_1250": "cp1250",
    "ANSI_1251": "cp1251",
    "ANSI_1252": "cp1252",
    "ANSI_1253": "cp1253",
    "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256",
    "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258",
}
_DWGCODEPAGE_RE = re.compile(rb"\$DWGCODEPAGE[ \t]*\r?\n[ \t]*3[ \t]*\r?\n[ \t]*([A-Za-z0-9_]+)")


def _dxf_unicode_escape(error: UnicodeEncodeError) -> tuple[str, int]:
    # 代码页里没有的字符（译文里的 é、œ 等）按 DXF 自己的 \U+XXXX 写法存。
    chars = error.object[error.start:error.end]
    return "".join(f"\\U+{ord(char):04X}" for char in chars), error.end


codecs.register_error("dxf_unicode_escape", _dxf_unicode_escape)


def _detect_dxf_encoding(path: Path) -> str:
    """UTF-8 能读就用 UTF-8；否则按头部 $DWGCODEPAGE，没有声明时再试 GBK。"""
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    match = _DWGCODEPAGE_RE.search(raw)
    candidates = []
    if match:
        declared = _DXF_CODEPAGES.get(match.group(1).decode("ascii").upper())
        if declared:
            candidates.append(declared)
    candidates.append("gbk")
    for encoding in candidates:
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError("图纸的文字编码无法识别（不是 UTF-8，也不是图纸里声明的编码或 GBK）。")


def _read_dxf_text(path: Path, encoding: str | None = None) -> str:
    # Disable universal-newline conversion.  DXF group code indentation and
    # CRLF bytes outside edited values are part of the preserved source.
    encoding = encoding or _detect_dxf_encoding(path)
    with path.open("r", encoding=encoding, errors="strict", newline="") as stream:
        return stream.read()


def _write_dxf_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    # 按源文件的编码写回：老格式 DXF 改存成 UTF-8，CAD 软件会按头部声明的代码页
    # 去读，中文就成了乱码。
    errors = "strict" if encoding == "utf-8" else "dxf_unicode_escape"
    with path.open("w", encoding=encoding, errors=errors, newline="") as stream:
        stream.write(text)


class CadTranslator(Protocol):
    """Translate source strings, optionally using matched terminology."""

    def __call__(self, texts: list[str], glossary: Mapping[str, str]) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class DxfRecord:
    code: int
    value: str
    line_ending: str = "\n"
    code_text: str = ""
    code_line_ending: str = ""


@dataclass(frozen=True)
class CadTextRun:
    """A translatable run inside an entity, excluding MTEXT controls."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class CadTextUnit:
    """One logical text entity and the DXF record positions it occupies."""

    unit_id: str
    entity_type: str
    record_indices: tuple[int, ...]
    original: str
    plain_text: str
    visible: bool = True
    metadata: bool = False
    runs: tuple[CadTextRun, ...] = ()


@dataclass(frozen=True)
class CadScanSummary:
    """Preflight information used by the CAD page and task manager."""

    path: Path
    filename: str
    format: str
    text_entity_count: int
    candidate_count: int
    needs_conversion: bool


def _read_scan_records(path: Path, converter: CadConverter | None) -> tuple[list[DxfRecord], bool]:
    """Read a DXF directly or convert one DWG into a disposable DXF."""
    if path.suffix.lower() == ".dxf":
        try:
            return _parse_dxf(_read_dxf_text(path)), False
        except UnicodeError as exc:
            raise CadPipelineError("图纸的文字编码无法识别（不是 UTF-8，也不是 GBK 等常见编码）。") from exc
    if path.suffix.lower() != ".dwg":
        raise CadPipelineError(f"不支持这种图纸格式：{path.suffix}（只支持 DWG 和 DXF）。")
    if converter is None:
        return [], True
    with tempfile.TemporaryDirectory(prefix="xl-cad-scan-") as temporary:
        dxf_path = Path(temporary) / f"{path.stem}.dxf"
        converter.dwg_to_dxf(path, dxf_path)
        try:
            return _parse_dxf(_read_dxf_text(dxf_path)), False
        except UnicodeError as exc:
            raise CadPipelineError("图纸的文字编码无法识别（不是 UTF-8，也不是 GBK 等常见编码）。") from exc


def scan_cad_file(
    path: str | Path,
    *,
    converter: CadConverter | None = None,
    options: CadPipelineOptions | None = None,
) -> CadScanSummary:
    """Scan one CAD file without modifying it or creating a translation task."""
    options = options or CadPipelineOptions()
    source = Path(path)
    records, needs_conversion = _read_scan_records(source, converter)
    units = extract_cad_text_units(records, include_block_text=options.include_block_text)
    candidates = [
        unit for unit in units
        if len(unit.plain_text) <= options.max_text_length
        and _has_translatable_content(unit.plain_text)
        and not (unit.entity_type == "DIMENSION" and "<>" in unit.plain_text)
        and _matches_source_language(unit.plain_text, options.source_lang)
        and not (options.skip_target_language and _is_likely_target_language(unit.plain_text, options.target_lang))
    ]
    return CadScanSummary(
        path=source,
        filename=source.name,
        format=source.suffix.lower().lstrip("."),
        text_entity_count=len(units),
        candidate_count=len(candidates),
        needs_conversion=needs_conversion,
    )


def scan_cad_paths(
    paths: Iterable[str | Path],
    *,
    converter: CadConverter | None = None,
    options: CadPipelineOptions | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> list[CadScanSummary]:
    """Scan selected CAD files and recursively expand selected directories.

    When ``skipped`` is given, a file that cannot be read is recorded there
    (``{"path", "name", "reason"}``) and the rest of the batch is still
    scanned; without it the first error propagates, as before.
    """
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(
                candidate
                for candidate in sorted(path.rglob("*"))
                if candidate.is_file()
                and candidate.suffix.lower() in CAD_SUPPORTED_EXTENSIONS
                and not any(
                    part in {"CAD翻译输出", ".cad-work", ".cad-review"}
                    or part.endswith("_翻译输出")
                    or "_翻译输出_" in part
                    for part in candidate.relative_to(path).parts
                )
            )
        elif path.is_file() and path.suffix.lower() in CAD_SUPPORTED_EXTENSIONS:
            files.append(path)
    unique_files = list(dict.fromkeys(files))
    if skipped is None:
        return [scan_cad_file(path, converter=converter, options=options) for path in unique_files]
    summaries: list[CadScanSummary] = []
    for path in unique_files:
        try:
            summaries.append(scan_cad_file(path, converter=converter, options=options))
        except Exception as exc:  # noqa: BLE001 - one unreadable drawing must not hide the others
            skipped.append({"path": str(path), "name": path.name, "reason": str(exc) or exc.__class__.__name__})
    return summaries


@dataclass
class CadTranslationStats:
    scanned_entities: int = 0
    candidate_entities: int = 0
    skipped_target_language: int = 0
    memory_hits: int = 0
    glossary_hits: int = 0
    ai_translated: int = 0
    untranslated: int = 0
    changed: int = 0
    replacement_characters: int = 0
    # 超长等确实处理不了的文字；会让文件进入"需复核"。
    unsupported_entities: int = 0
    unknown_entity_count: int = 0
    residual_foreign_text_count: int = 0
    # 纯数字/符号、标注占位符、不属于所选源语言的文字：正常跳过，不需要复核。
    skipped_non_text: int = 0
    skipped_other_language: int = 0


@dataclass
class CadProgress:
    stage: str
    done: int
    total: int
    percent: float
    message: str = ""


@dataclass
class CadTranslationResult:
    source: Path
    output: Path
    work_dxf: Path
    manifest: Path
    report: Path
    stats: CadTranslationStats
    unresolved: list[dict[str, str]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    summary: Path | None = None


@dataclass(frozen=True)
class CadPipelineOptions:
    source_lang: str = "auto"
    target_lang: str = "zh"
    skip_target_language: bool = True
    keep_work_dxf: bool = True
    include_block_text: bool = True
    max_text_length: int = 12000
    verify_roundtrip: bool = True
    scan_replacement_chars: bool = True
    check_entity_counts: bool = True
    scan_residual: bool = True
    copy_related_files: bool = False


def _parse_dxf(text: str) -> list[DxfRecord]:
    raw_lines = text.splitlines(keepends=True)
    if len(raw_lines) % 2:
        raise CadPipelineError("图纸里有一段内容格式无法识别，可能已损坏，没有处理。")
    records: list[DxfRecord] = []
    for index in range(0, len(raw_lines), 2):
        code_line = raw_lines[index]
        value_line = raw_lines[index + 1]
        code_ending = "\r\n" if code_line.endswith("\r\n") else "\n" if code_line.endswith("\n") else ""
        ending = "\r\n" if value_line.endswith("\r\n") else "\n" if value_line.endswith("\n") else ""
        code_text = code_line[:-len(code_ending)] if code_ending else code_line
        value = value_line[:-len(ending)] if ending else value_line
        try:
            code = int(code_text)
        except ValueError as exc:
            logger.debug(f"[CAD] DXF 代码行无效：{code_text!r}")
            raise CadPipelineError("图纸里有一段内容格式无法识别，可能已损坏，没有处理。") from exc
        records.append(DxfRecord(code, value, ending, code_text, code_ending))
    # 被截断的图纸（拷贝中断、网盘没同步完）照样能逐行配对解析，只是后半截没了：
    # 不拦的话它会以「0 个文字实体」通过扫描，任务里还标成「已生成」，产物是一份
    # 同样残缺的 DXF。开了 SECTION 却没有收尾的 ENDSEC，就是没读完的文件。
    opened = sum(1 for record in records if record.code == 0 and record.value.strip() == "SECTION")
    closed = sum(1 for record in records if record.code == 0 and record.value.strip() == "ENDSEC")
    if opened > closed:
        raise CadPipelineError("图纸文件不完整，可能已损坏或没有拷贝完整，没有处理。")
    return records


def _serialize_dxf(records: Iterable[DxfRecord]) -> str:
    result: list[str] = []
    for record in records:
        code = record.code_text or str(record.code)
        code_eol = record.code_line_ending or record.line_ending or "\n"
        value_eol = record.line_ending
        result.append(f"{code}{code_eol}{record.value}{value_eol}")
    return "".join(result)


def _plain_text(value: str) -> str:
    """Decode DXF Unicode escapes and remove inline formatting for matching."""
    value = _DXF_UNICODE_RE.sub(lambda match: chr(int(match.group(1), 16)), value)
    value = value.replace("\\P", "\n")
    # MTEXT style commands carry parameters up to ``;`` (for example
    # ``\fArial|b0;``). Removing only the initial letter leaves font names in
    # the sentence and causes them to be sent to the translator.
    value = re.sub(r"\\[fFhHwWcCaAtTqQoOsS][^;]*;", "", value)
    value = _DXF_FORMAT_RE.sub("", value)
    value = value.replace("{", "").replace("}", "")
    return value.strip()


def _mtext_runs(value: str) -> tuple[CadTextRun, ...]:
    """Return visible MTEXT runs while retaining every control byte.

    Formatting commands and braces are delimiters.  ``\\P`` is retained as a
    paragraph boundary in the assembled value but does not become a prompt.
    The offsets refer to the original assembled group-1/group-3 string.
    """
    runs: list[CadTextRun] = []
    i = 0
    start: int | None = None
    while i < len(value):
        if value[i] == "\\":
            unicode_escape = _DXF_UNICODE_RE.match(value, i)
            if unicode_escape:
                if start is None:
                    start = i
                i = unicode_escape.end()
                continue
            if start is not None and start < i:
                plain = _plain_text(value[start:i])
                if plain:
                    runs.append(CadTextRun(start, i, plain))
                start = None
            m = re.match(r"\\[fFhHwWcCaAtTqQoOsS][^;]*;", value[i:])
            if m:
                i += len(m.group(0))
                continue
            if value[i:i + 2] == "\\P":
                i += 2
                continue
            m = re.match(r"\\[A-Za-z]+", value[i:])
            if m:
                i += len(m.group(0))
                continue
        if value[i] in "{}":
            if start is not None and start < i:
                plain = _plain_text(value[start:i])
                if plain:
                    runs.append(CadTextRun(start, i, plain))
                start = None
            i += 1
            continue
        if start is None:
            start = i
        i += 1
    if start is not None and start < len(value):
        plain = _plain_text(value[start:])
        if plain:
            runs.append(CadTextRun(start, len(value), plain))
    return tuple(runs)


def _record_copy(record: DxfRecord, *, value: str, code: int | None = None) -> DxfRecord:
    target_code = record.code if code is None else code
    if target_code == record.code:
        code_text = record.code_text or str(record.code)
    elif record.code_text:
        code_text = str(target_code).rjust(len(record.code_text))
    else:
        code_text = str(target_code)
    return DxfRecord(
        target_code,
        value,
        record.line_ending,
        code_text,
        record.code_line_ending or record.line_ending,
    )


def _looks_like_metadata(entity_type: str, text: str, codes: Sequence[int]) -> bool:
    if entity_type == "ATTDEF" and codes and all(code == 3 for code in codes):
        # Group 3 is an attribute prompt/tag in ATTDEF.  Group 1 is rendered
        # default text and remains eligible.
        return True
    if entity_type == "MINSERT":
        return True
    stripped = text.lstrip()
    return stripped.startswith("((") or stripped.startswith("(setq")


def extract_cad_text_units(
    records: Sequence[DxfRecord],
    *,
    include_block_text: bool = True,
) -> list[CadTextUnit]:
    """Extract visible text without flattening or rewriting the drawing.

    Entities are grouped using DXF ``0`` records.  For MTEXT, continuation
    group 3 values are joined to group 1; for MLEADER only group 304 text is
    selected.  Structural leader records and attribute tags/prompts are never
    returned as translation units.
    """
    units: list[CadTextUnit] = []
    starts = [i for i, record in enumerate(records) if record.code == 0]
    starts.append(len(records))
    section_by_record: list[str] = [""] * len(records)
    section = ""
    pending_section = False
    for index, record in enumerate(records):
        if record.code == 0 and record.value.strip().upper() == "SECTION":
            pending_section = True
        elif pending_section and record.code == 2:
            section = record.value.strip().upper()
            pending_section = False
        elif record.code == 0 and record.value.strip().upper() == "ENDSEC":
            section = ""
            pending_section = False
        section_by_record[index] = section
    # Resolve layer visibility and block reachability before selecting text.
    # This prevents a BLOCKS section containing unused attribute definitions
    # from inflating the candidate list.
    hidden_layers: set[str] = set()
    table_name: str | None = None
    in_table = False
    for i, record in enumerate(records):
        val = record.value.strip().upper()
        if record.code == 0 and val == "TABLE":
            in_table = True
            table_name = None
        elif in_table and record.code == 2:
            table_name = val
        elif in_table and record.code == 70 and table_name == "LAYER":
            try:
                if int(record.value.strip()) & 1:
                    # The preceding code 2 is the layer name.
                    for prior in reversed(records[:i]):
                        if prior.code == 2:
                            hidden_layers.add(prior.value.strip().upper())
                            break
            except ValueError:
                pass
        elif record.code == 0 and val == "ENDTAB":
            in_table = False
            table_name = None
    block_ranges: dict[str, tuple[int, int]] = {}
    referenced_blocks: set[str] = set()
    current_block: str | None = None
    block_start = 0
    for start, end in zip(starts, starts[1:]):
        if start >= end:
            continue
        typ = records[start].value.strip().upper()
        if typ == "BLOCK":
            name = next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 2), "")
            current_block = name
            block_start = start
        elif typ == "ENDBLK" and current_block:
            block_ranges[current_block] = (block_start, end)
            current_block = None
        elif typ in {"INSERT", "MINSERT"}:
            name = next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 2), "")
            if name:
                referenced_blocks.add(name)
    reachable_ranges = [block_ranges[name] for name in referenced_blocks if name in block_ranges]
    reachable_ranges.extend(
        block_ranges[name] for name in ("*MODEL_SPACE", "*PAPER_SPACE") if name in block_ranges
    )

    for entity_no, (start, end) in enumerate(zip(starts, starts[1:])):
        if start >= end:
            continue
        entity_type = records[start].value.strip().upper()
        if entity_type not in _TEXT_ENTITY_TYPES and entity_type not in {"MLEADER", "MULTILEADER"}:
            continue
        if not include_block_text and section_by_record[start] == "BLOCKS":
            continue
        if include_block_text and section_by_record[start] == "BLOCKS":
            if not any(lo <= start < hi for lo, hi in reachable_ranges):
                continue
        entity_flags = {record.code: record.value for record in records[start + 1:end] if record.code in {60, 70}}
        if str(entity_flags.get(60, "")).strip() == "1":
            continue
        if entity_type == "ATTDEF":
            try:
                if int(str(entity_flags.get(70, "0")).strip()) & 1:
                    continue
            except ValueError:
                pass
        layer = next((record.value.strip().upper() for record in records[start + 1:end] if record.code == 8), "")
        if layer in hidden_layers:
            continue
        fields: list[int] = []
        if entity_type in {"MLEADER", "MULTILEADER"}:
            # MLEADER stores displayed text in CONTEXT_DATA as the first 304
            # before the LEADER_LINE 302/303 structures.  Later 304 values
            # describe leader geometry/metadata and must not be translated.
            context_started = False
            for index in range(start + 1, end):
                record = records[index]
                if record.code == 300 and record.value.strip().upper() == "CONTEXT_DATA":
                    context_started = True
                    continue
                if not context_started:
                    continue
                if record.code in {302, 303}:
                    break
                if record.code == _MLEADER_TEXT_CODE:
                    fields.append(index)
                    break
        else:
            allowed_codes = {1} if entity_type in {"ATTRIB", "ATTDEF", "DIMENSION"} else {1, 3}
            for index in range(start + 1, end):
                if records[index].code in allowed_codes:
                    fields.append(index)
        if not fields:
            continue
        assembled = "".join(records[index].value for index in fields)
        plain = _plain_text(assembled)
        metadata = _looks_like_metadata(entity_type, plain, [records[index].code for index in fields])
        visible = bool(plain) and not metadata
        if not visible:
            continue
        units.append(
            CadTextUnit(
                unit_id=f"{entity_no}:{entity_type}:{fields[0]}",
                entity_type=entity_type,
                record_indices=tuple(fields),
                original=assembled,
                plain_text=plain,
                visible=True,
                metadata=False,
                runs=_mtext_runs(assembled) if entity_type in {"MTEXT", "MLEADER", "MULTILEADER"} else (CadTextRun(0, len(assembled), plain),),
            )
        )
    return units


# 图纸里大量出现 M10、Q235、GB/T、DN50、PVC 这类型号/规范代号。它们由拉丁字母
# 组成，但不是"外文"，判断语言时不能把它们当成英文单词——否则中文图纸里每一条
# "M10螺栓"都会被当成未翻译的外文残留，整张图被误标"需复核"。
_LATIN_WORD_RE = re.compile(r"(?<![A-Za-zÀ-ÿ0-9])[A-Za-zÀ-ÿ]+(?![A-Za-zÀ-ÿ0-9])")
_NON_LATIN_SCRIPT_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u04ff\u0590-\u06ff\u0e00-\u0eff]"
)


def _has_latin_words(text: str) -> bool:
    """True when ``text`` contains real Latin-script words, not just codes.

    A word counts when it stands alone (not glued to digits like ``M10``) and
    is not an all-caps abbreviation of up to 4 letters (``GB``, ``PVC``).
    """
    for match in _LATIN_WORD_RE.finditer(text):
        word = match.group(0)
        if len(word) >= 2 and not (word.isupper() and len(word) <= 4):
            return True
    return False


def _is_likely_target_language(text: str, target_lang: str) -> bool:
    normalized = str(target_lang or "").strip().lower().replace("_", "-")
    code = normalized.split("-", 1)[0]
    if code in {"zh", "ja", "ko"}:
        patterns = {"zh": r"[\u3400-\u9fff]", "ja": r"[\u3040-\u30ff\u3400-\u9fff]", "ko": r"[\uac00-\ud7af]"}
        return bool(re.search(patterns[code], text)) and not _has_latin_words(text)
    script_patterns = {
        "ar": r"[\u0600-\u06ff]", "fa": r"[\u0600-\u06ff]", "ur": r"[\u0600-\u06ff]",
        "he": r"[\u0590-\u05ff]", "th": r"[\u0e00-\u0e7f]", "km": r"[\u1780-\u17ff]",
        "hi": r"[\u0900-\u097f]", "bn": r"[\u0980-\u09ff]", "ta": r"[\u0b80-\u0bff]",
        "te": r"[\u0c00-\u0c7f]", "ml": r"[\u0d00-\u0d7f]", "kn": r"[\u0c80-\u0cff]",
        "gu": r"[\u0a80-\u0aff]", "pa": r"[\u0a00-\u0a7f]", "my": r"[\u1000-\u109f]",
        "si": r"[\u0d80-\u0dff]", "lo": r"[\u0e80-\u0eff]", "am": r"[\u1200-\u137f]",
    }
    pattern = script_patterns.get(code)
    if pattern:
        return bool(re.search(pattern, text)) and not _has_latin_words(text)
    # Latin-script targets share the same Unicode ranges. This is deliberately
    # conservative for unknown/custom languages, which must still be scanned.
    # 混排文字（如"Q235钢板"）里只要还有中日韩等非拉丁文字，就仍需翻译。
    if code in {"en", "fr", "de", "es", "it", "pt", "nl", "sv", "da", "no", "fi", "pl", "cs", "sk", "sl", "hu", "ro", "tr", "vi", "id", "ms"}:
        return bool(re.search(r"[A-Za-zÀ-ÿ]", text)) and not _NON_LATIN_SCRIPT_RE.search(text)
    return False


def _is_residual_foreign_text(text: str, target_lang: str) -> bool:
    """Whether written-back text still looks untranslated for ``target_lang``.

    For CJK/other-script targets the leftover must be real Latin words, not
    part numbers or standard codes. For Latin targets the leftover is any
    remaining CJK/other non-Latin script.
    """
    code = str(target_lang or "").strip().lower().replace("_", "-").split("-", 1)[0]
    if code in {"en", "fr", "de", "es", "it", "pt", "nl", "sv", "da", "no", "fi", "pl", "cs", "sk", "sl", "hu", "ro", "tr", "vi", "id", "ms"}:
        return bool(_NON_LATIN_SCRIPT_RE.search(text))
    return _has_latin_words(text) and not _is_likely_target_language(text, target_lang)


def _matches_source_language(text: str, source_lang: str) -> bool:
    lang = str(source_lang or "auto").lower()
    if lang.startswith("manual:"):
        lang = lang.split(":", 1)[1]
    if lang in {"", "auto", "detect", "manual"}:
        return True
    if lang.startswith(("zh", "cn")):
        return bool(re.search(r"[\u3400-\u9fff]", text))
    if lang.startswith(("en", "fr", "de", "es", "it", "pt")):
        return bool(re.search(r"[A-Za-zÀ-ÿ]", text))
    return True


def _has_translatable_content(text: str) -> bool:
    # Digits, punctuation, CAD placeholders and symbol-only labels are not
    # translation requests.  CJK and Latin letters are valid source content.
    return bool(re.search(r"[A-Za-zÀ-ÿ\u3400-\u9fff]", text))


def _numeric_tokens(text: str) -> list[str]:
    # 只排除紧跟在拉丁字母/数字/下划线后面的数字（Q235、M10 这类代号的一部分）。
    # 以前用 \w 判断，而 \w 也匹配汉字，于是「共12页」「译文12345」里的数字全被漏掉，
    # 中文译文一律判成「数字被改动」，整条译文被丢弃并标为需复核。
    return re.findall(r"(?<![A-Za-z0-9_])\d+(?:[.,]\d+)?", text)


def _valid_translation(source: str, target: str) -> tuple[bool, str]:
    value = str(target or "").strip()
    if not value:
        return False, "译文为空"
    if any(char in value for char in _REPLACEMENT_CHARS):
        return False, "译文里有乱码"
    if _numeric_tokens(source) != _numeric_tokens(value):
        return False, "译文里的数字和原文对不上"
    return True, ""


def _protect_format_codes(value: str) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}
    counter = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"__CADFMT_{counter}__"
        protected[token] = match.group(0)
        counter += 1
        return token

    return _DXF_FORMAT_RE.sub(replace, value), protected


def _restore_format_codes(value: str, protected: Mapping[str, str]) -> str:
    for token, original in protected.items():
        value = value.replace(token, original)
    return value


def _safe_translation(original: str, translated: str) -> str:
    result = str(translated or "").strip()
    control_re = re.compile(r"\\[fFhHwWcCaAtTqQoOsS][^;]*;|\\P|[{}]")
    matches = list(control_re.finditer(original))
    if not matches:
        return result
    # Keep every control in sequence and replace all source prose with one
    # translated run.  Multi-run MTEXT uses _replace_unit_runs below for exact
    # positions; this helper remains safe for a single logical value.
    out: list[str] = []
    cursor = 0
    inserted = False
    for match in matches:
        if not inserted and match.start() > cursor and original[cursor:match.start()].strip():
            out.append(result)
            inserted = True
        out.append(match.group(0))
        cursor = match.end()
    if not inserted:
        out.append(result)
    return "".join(out)


def _encode_dxf_text(value: str, *, multiline: bool) -> str:
    """Keep one group value on one physical DXF line and escape non-ASCII."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\n", r"\P" if multiline else " ")
    return "".join(char if ord(char) < 128 else f"\\U+{ord(char):04X}" for char in value)


def _ascii_chunks(value: str, limit: int = 240) -> list[str]:
    # Never split a DXF Unicode escape or formatting command across records.
    tokens = re.findall(r"\\[fFhHwWcCaAtTqQoOsS][^;]*;|\\U\+[0-9A-Fa-f]{4}|\\[A-Za-z]+|.", value)
    chunks: list[str] = []
    current = ""
    for token in tokens:
        if current and len(current) + len(token) > limit:
            chunks.append(current)
            current = ""
        current += token
    if current or not chunks:
        chunks.append(current)
    return chunks


def _replace_unit(records: list[DxfRecord], unit: CadTextUnit, translated: str) -> None:
    value = _encode_dxf_text(_safe_translation(unit.original, translated), multiline=unit.entity_type == "MTEXT")
    if unit.entity_type == "MTEXT":
        chunks = _ascii_chunks(value)
        final = next((index for index in reversed(unit.record_indices) if records[index].code == 1), None)
        if final is None:
            raise CadPipelineError("图纸里有一段文字格式无法识别，可能已损坏。")
        continuation_fields = [index for index in unit.record_indices if records[index].code == 3]
        for index in continuation_fields:
            records[index] = _record_copy(records[index], value="", code=3)
        for index, chunk in zip(continuation_fields, chunks[:-1]):
            records[index] = _record_copy(records[index], value=chunk, code=3)
        extra_chunks = chunks[len(continuation_fields):-1]
        for offset, chunk in enumerate(extra_chunks):
            records.insert(final + offset, _record_copy(records[final], value=chunk, code=3))
        records[final + len(extra_chunks)] = _record_copy(records[final + len(extra_chunks)], value=chunks[-1], code=1)
        return
    first = unit.record_indices[0]
    records[first] = _record_copy(records[first], value=value)
    for index in unit.record_indices[1:]:
        records[index] = _record_copy(records[index], value="")


def _replace_unit_runs(
    records: list[DxfRecord], unit: CadTextUnit, translations: Mapping[str, str],
) -> None:
    """Replace MTEXT runs while preserving controls and paragraph topology."""
    if not unit.runs or unit.entity_type not in {"MTEXT", "MLEADER", "MULTILEADER"}:
        value = translations.get(unit.plain_text)
        if value:
            _replace_unit(records, unit, value)
        return
    assembled = "".join(records[index].value for index in unit.record_indices)
    for run in reversed(unit.runs):
        target = translations.get(run.text)
        if not target:
            continue
        span = assembled[run.start:run.end]
        leading = re.match(r"\s*", span).group(0)
        trailing = re.search(r"\s*$", span).group(0)
        replacement = _encode_dxf_text(
            leading + _safe_translation(span.strip(), target) + trailing,
            multiline=False,
        )
        assembled = assembled[:run.start] + replacement + assembled[run.end:]
    if unit.entity_type in {"MLEADER", "MULTILEADER"}:
        index = unit.record_indices[0]
        records[index] = _record_copy(records[index], value=assembled, code=304)
        return
    encoded = _encode_dxf_text(assembled, multiline=True)
    chunks = _ascii_chunks(encoded)
    final = next((index for index in reversed(unit.record_indices) if records[index].code == 1), None)
    if final is None:
        raise CadPipelineError("图纸里有一段文字格式无法识别，可能已损坏。")
    continuation = [index for index in unit.record_indices if records[index].code == 3]
    for index in continuation:
        records[index] = _record_copy(records[index], value="", code=3)
    for index, chunk in zip(continuation, chunks[:-1]):
        records[index] = _record_copy(records[index], value=chunk, code=3)
    extra = chunks[len(continuation):-1]
    for offset, chunk in enumerate(extra):
        records.insert(final + offset, _record_copy(records[final], value=chunk, code=3))
    records[final + len(extra)] = _record_copy(records[final + len(extra)], value=chunks[-1], code=1)


def scan_replacement_characters(records: Sequence[DxfRecord]) -> list[str]:
    return [record.value for record in records if any(char in record.value for char in _REPLACEMENT_CHARS)]


class CadTranslationPipeline:
    """Translate one DWG/DXF while preserving the surrounding DXF topology."""

    def __init__(
        self,
        *,
        converter: CadConverter | None,
        memory_lookup: Callable[[list[str]], Mapping[str, str | None]] | None = None,
        translator: CadTranslator | None = None,
        progress: Callable[[CadProgress], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.converter = converter
        self.memory_lookup = memory_lookup
        self.translator = translator
        self.progress = progress
        self.stop_requested = stop_requested

    def _check_stop(self) -> None:
        if self.stop_requested and self.stop_requested():
            raise CadStopRequested("已停止；已完成的部分已保留。")

    def _emit(self, stage: str, done: int, total: int, message: str = "") -> None:
        stage_total = max(1, total)
        fraction = min(1.0, max(0.0, done / stage_total))
        before = sum(CAD_STAGE_WEIGHTS[name] for name in CAD_STAGE_WEIGHTS if list(CAD_STAGE_WEIGHTS).index(name) < list(CAD_STAGE_WEIGHTS).index(stage))
        percent = before + CAD_STAGE_WEIGHTS[stage] * fraction
        if self.progress:
            self.progress(CadProgress(stage, done, total, round(percent, 2), message))

    def _call_translator(
        self,
        remaining: list[str],
        glossary: Mapping[str, str],
        *,
        already: int,
        total: int,
    ) -> Mapping[str, str]:
        """调用翻译器，并把它逐批的进度折算进「AI 翻译」阶段。

        翻译是整张图纸里最慢的一步（真实 DWG 要等一分多钟），以前只在全部批次返回后
        报一次进度，进度条就停在这一步开头一动不动、最后一下跳过去。翻译器肯接
        ``progress`` 参数时（api/task_manager 里接的是 translate_texts 的批次回调），
        每完成一批就推一次；老式只收两个参数的翻译器照旧调用。
        """
        self._emit("translate", already, max(1, total))

        def report(done: int, batch_total: int) -> None:
            fraction = min(1.0, max(0.0, done / max(1, batch_total)))
            self._emit("translate", already + int(len(remaining) * fraction), max(1, total))

        try:
            accepts_progress = "progress" in inspect.signature(self.translator).parameters
        except (TypeError, ValueError):
            accepts_progress = False
        if accepts_progress:
            return self.translator(remaining, glossary, progress=report)
        return self.translator(remaining, glossary)

    def translate_file(
        self,
        source: str | Path,
        output: str | Path,
        *,
        options: CadPipelineOptions | None = None,
        glossary: Mapping[str, str] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> CadTranslationResult:
        options = options or CadPipelineOptions()
        if stop_requested is not None:
            self.stop_requested = stop_requested
        self._check_stop()
        source = Path(source)
        output = Path(output)
        if source.suffix.lower() not in CAD_SUPPORTED_EXTENSIONS:
            raise CadPipelineError(f"不支持这种图纸格式：{source.suffix}（只支持 DWG 和 DXF）。")
        if source.resolve() == output.resolve():
            raise CadPipelineError("输出文件不能覆盖源文件。")
        companion_paths = (
            output.with_suffix(output.suffix + ".manifest.json"),
            output.with_suffix(output.suffix + ".report.json"),
            output.with_suffix(output.suffix + ".work.dxf"),
            output.with_suffix(output.suffix + ".summary.md"),
        )
        if output.exists() or any(path.exists() for path in companion_paths):
            raise CadPipelineError("输出文件已存在，为避免覆盖请更换输出目录。")
        output.parent.mkdir(parents=True, exist_ok=True)
        evidence_dir = output.parent / f"{output.stem}.cad-work"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        started_at = time.perf_counter()
        timings: dict[str, float] = {}
        with tempfile.TemporaryDirectory(prefix="xl-cad-") as temporary:
            temp_root = Path(temporary)
            work_dxf = temp_root / f"{source.stem}.dxf"
            # DWG 转 DXF 要调用外部转换器，大图纸要好几秒：先报一次 0，界面马上进入「读取」阶段。
            self._emit("scan", 0, 1)
            if source.suffix.lower() == ".dxf":
                shutil.copy2(source, work_dxf)
            elif self.converter is not None:
                self.converter.dwg_to_dxf(source, work_dxf)
            else:
                raise CadPipelineError("翻译 DWG 需要先连接图纸转换工具。")
            # Keep a recoverable input/work copy even if a later provider or
            # converter step fails.
            shutil.copy2(work_dxf, evidence_dir / "source.dxf")
            try:
                dxf_encoding = _detect_dxf_encoding(work_dxf)
                dxf_text = _read_dxf_text(work_dxf, dxf_encoding)
            except UnicodeError as exc:
                raise CadPipelineError("图纸的文字编码无法识别（不是 UTF-8，也不是 GBK 等常见编码）。") from exc
            records = _parse_dxf(dxf_text)
            stats = CadTranslationStats(scanned_entities=sum(1 for r in records if r.code == 0))
            self._emit("scan", 1, 1, "已读取图纸")
            units = extract_cad_text_units(records, include_block_text=options.include_block_text)
            stats.candidate_entities = len(units)
            candidates: list[CadTextUnit] = []
            for unit in units:
                if len(unit.plain_text) > options.max_text_length:
                    stats.unsupported_entities += 1
                    continue
                if not _has_translatable_content(unit.plain_text):
                    stats.skipped_non_text += 1
                    continue
                if unit.entity_type == "DIMENSION" and "<>" in unit.plain_text:
                    stats.skipped_non_text += 1
                    continue
                if not _matches_source_language(unit.plain_text, options.source_lang):
                    stats.skipped_other_language += 1
                    continue
                if options.skip_target_language and _is_likely_target_language(unit.plain_text, options.target_lang):
                    stats.skipped_target_language += 1
                    continue
                candidates.append(unit)
            timings["scan"] = round(time.perf_counter() - started_at, 4)
            self._emit("scan", 1, 1, f"找到 {len(candidates)} 处可翻译文字")
            texts = list(dict.fromkeys(
                run.text for unit in candidates for run in unit.runs if _has_translatable_content(run.text)
            ))
            if not texts:
                texts = list(dict.fromkeys(unit.plain_text for unit in candidates))
            memory_hits: dict[str, str] = {}
            self._check_stop()
            if self.memory_lookup and texts:
                memory_hits = {key: value for key, value in self.memory_lookup(texts).items() if value}
            glossary = dict(glossary or {})
            translations: dict[str, str] = dict(memory_hits)
            invalid_reasons: dict[str, str] = {}
            for key, value in list(translations.items()):
                valid, reason = _valid_translation(key, value)
                if not valid:
                    invalid_reasons[key] = reason
                    translations.pop(key, None)
            stats.memory_hits = len(memory_hits)
            self._emit("match", len(memory_hits), max(1, len(texts)), f"记忆库已有译文 {len(memory_hits)} 条")
            remaining = [text for text in texts if text not in translations]
            glossary_hits: dict[str, str] = {}
            for text in remaining:
                if text not in glossary:
                    continue
                valid, reason = _valid_translation(text, glossary[text])
                if valid:
                    glossary_hits[text] = str(glossary[text]).strip()
                else:
                    invalid_reasons[text] = reason
            translations.update(glossary_hits)
            stats.glossary_hits = len(glossary_hits)
            timings["match"] = round(time.perf_counter() - started_at - timings["scan"], 4)
            self._emit("match", len(memory_hits) + len(glossary_hits), max(1, len(texts)), f"术语表匹配 {len(glossary_hits)} 条")
            remaining = [text for text in remaining if text not in translations]
            self._check_stop()
            if remaining and self.translator is not None:
                ai_results = dict(
                    self._call_translator(remaining, glossary, already=len(translations), total=len(texts))
                )
                for text in remaining:
                    value = str(ai_results.get(text, "") or "").strip()
                    valid, _reason = _valid_translation(text, value)
                    if valid:
                        translations[text] = value
                    else:
                        invalid_reasons[text] = _reason
                stats.ai_translated = sum(1 for text in remaining if text in translations)
            stats.untranslated = sum(1 for text in texts if text not in translations)
            timings["translate"] = round(time.perf_counter() - started_at - timings["scan"] - timings["match"], 4)
            self._emit("translate", len(translations), max(1, len(texts)), "翻译完成")
            self._check_stop()
            for unit in reversed(candidates):
                changed_unit = any(run.text in translations for run in unit.runs)
                if not unit.runs and unit.plain_text in translations:
                    changed_unit = True
                _replace_unit_runs(records, unit, translations)
                if changed_unit:
                    stats.changed += 1
            write_started = time.perf_counter()
            translated_dxf = temp_root / f"{source.stem}_translated.dxf"
            _write_dxf_text(translated_dxf, _serialize_dxf(records), dxf_encoding)
            shutil.copy2(translated_dxf, evidence_dir / "translated.dxf")
            persisted_work_dxf = output.with_suffix(output.suffix + ".work.dxf")
            if options.keep_work_dxf:
                shutil.copy2(translated_dxf, persisted_work_dxf)
            staged_output = temp_root / f"{source.stem}_staged{output.suffix.lower()}"
            if output.suffix.lower() == ".dwg":
                if self.converter is None:
                    raise CadPipelineError("翻译 DWG 需要先连接图纸转换工具。")
                self.converter.dxf_to_dwg(translated_dxf, staged_output)
            else:
                shutil.copy2(translated_dxf, staged_output)
            timings["write"] = round(time.perf_counter() - write_started, 4)
            self._emit("write", 1, 1, "已写回图纸")
            verification_dxf = translated_dxf
            self._check_stop()
            if output.suffix.lower() == ".dwg" and options.verify_roundtrip:
                if self.converter is None:
                    raise CadPipelineError("翻译 DWG 需要先连接图纸转换工具。")
                verification_dxf = temp_root / f"{source.stem}_verify.dxf"
                self.converter.dwg_to_dxf(staged_output, verification_dxf)
            if options.verify_roundtrip:
                self._check_stop()
                output_records = _parse_dxf(_read_dxf_text(verification_dxf))
                output_units = extract_cad_text_units(output_records, include_block_text=options.include_block_text)
                if options.check_entity_counts and len(output_units) != len(units):
                    raise CadPipelineError("写回后的图纸文字数量对不上，为安全起见没有输出。")
            else:
                output_records = records
                output_units = extract_cad_text_units(output_records, include_block_text=options.include_block_text)
            stats.unknown_entity_count = sum(
                1 for record in output_records
                if record.code == 0 and (record.value.upper().startswith("ACAD_PROXY") or record.value.upper().startswith("PROXY"))
            )
            unsupported_features: list[str] = []
            if stats.unknown_entity_count:
                unsupported_features.append("proxy_entities")
            if any("%<\\" in record.value for record in output_records):
                unsupported_features.append("fields")
            if any(record.code == 2 and "$" in record.value for record in output_records):
                unsupported_features.append("xrefs")
            if any(record.code in {3, 7} and (".SHX" in record.value.upper() or ".TTF" in record.value.upper()) for record in output_records):
                unsupported_features.append("fonts")
            output_units = extract_cad_text_units(output_records, include_block_text=options.include_block_text)
            if options.scan_residual:
                stats.residual_foreign_text_count = sum(
                    1 for unit in output_units
                    if _is_residual_foreign_text(unit.plain_text, options.target_lang)
                )
            translated_targets = {str(value).strip() for value in translations.values() if value}
            replacements = [
                unit.plain_text
                for unit in output_units
                if unit.plain_text in translated_targets
                and any(char in unit.plain_text for char in _REPLACEMENT_CHARS)
            ]
            stats.replacement_characters = len(replacements) if options.scan_replacement_chars else 0
            # ODA is allowed to normalize DXF record ordering and auxiliary
            # tables during a DWG round trip. Entity/text counts are the
            # stable structural checks; raw record count is deliberately not
            # treated as a failure signal.
            timings["verify"] = round(time.perf_counter() - started_at - sum(timings.values()), 4)
            timings["total"] = round(time.perf_counter() - started_at, 4)
            self._emit("verify", 1, 1, "已完成文字与乱码检查")
            unresolved = [{"source": text, "reason": invalid_reasons.get(text, "未获得译文")} for text in texts if text not in translations]
            unit_reports: list[dict[str, str]] = []
            for unit in candidates:
                for run in unit.runs or (CadTextRun(0, len(unit.plain_text), unit.plain_text),):
                    source_text = run.text
                    if not source_text:
                        continue
                    target_text = translations.get(source_text, "")
                    if target_text:
                        unit_reports.append({"unit_id": unit.unit_id, "source": source_text, "translation": target_text, "status": "translated", "reason": ""})
                    else:
                        unit_reports.append({"unit_id": unit.unit_id, "source": source_text, "translation": "", "status": "unresolved", "reason": invalid_reasons.get(source_text, "未获得译文")})
            manifest_path = output.with_suffix(output.suffix + ".manifest.json")
            report_path = output.with_suffix(output.suffix + ".report.json")
            summary_path = output.with_suffix(output.suffix + ".summary.md")
            manifest = {
                "schema_version": 1,
                "source": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "output": str(output),
                "work_dxf": str(persisted_work_dxf) if options.keep_work_dxf else None,
                "source_extension": source.suffix.lower(),
                "target_lang": options.target_lang,
                "stats": asdict(stats),
                "unresolved_count": len(unresolved),
                "replacement_character_count": len(replacements),
                "timings": timings,
                "units": unit_reports,
                "summary": str(summary_path),
                "unsupported_features": unsupported_features,
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            report_path.write_text(json.dumps({**manifest, "unresolved": unresolved}, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_lines = [
                f"# CAD 翻译摘要：{source.name}", "",
                f"- 输出：`{output.name}`", f"- 已翻译文字：{stats.changed} 处",
                f"- 没翻好：{len(unresolved)} 处", f"- 译文里仍有原文：{stats.residual_foreign_text_count} 处", "",
                "| 编号 | 原文 | 译文 | 状态 | 原因 |", "|---|---|---|---|---|",
            ]
            for item in unit_reports:
                row = {**item, "status": _UNIT_STATUS_LABELS.get(str(item.get("status") or ""), item.get("status", ""))}
                summary_lines.append("| {unit_id} | {source} | {translation} | {status} | {reason} |".format(**{key: str(value).replace("|", "\\|") for key, value in row.items()}))
            summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
            # Publish only after the output and all review artifacts are ready.
            if output.exists():
                raise CadPipelineError("处理期间输出位置出现了同名文件，为避免覆盖，没有写出译文图纸。")
            shutil.copy2(staged_output, output)
            return CadTranslationResult(source, output, persisted_work_dxf if options.keep_work_dxf else translated_dxf, manifest_path, report_path, stats, unresolved, timings, summary_path)


class SubprocessCadConverter:
    """Small adapter for an installed ODA File Converter executable.

    The plugin is responsible for locating/validating the executable and can
    inject this adapter into the pipeline.  No converter is downloaded or
    bundled by this module.
    """

    def __init__(self, executable: str | Path, *, timeout: int = 600) -> None:
        self.executable = str(executable)
        self.timeout = timeout

    def _macos_app_bundle(self) -> Path | None:
        """Return the enclosing ``.app`` bundle for a macOS executable."""
        executable = Path(self.executable).expanduser()
        if platform.system().lower() != "darwin":
            return None
        try:
            resolved = executable.resolve()
        except OSError:
            resolved = executable
        return next((parent for parent in resolved.parents if parent.suffix.lower() == ".app"), None)

    @staticmethod
    def _produced_file(output_dir: Path, stem: str, output_type: str) -> Path | None:
        expected = output_dir / f"{stem}.{output_type.lower()}"
        if expected.is_file() and expected.stat().st_size > 0:
            return expected
        candidates = sorted(
            path for path in output_dir.iterdir()
            if path.is_file()
            and path.stat().st_size > 0
            and path.suffix.lower() == f".{output_type.lower()}"
            and path.stem.lower() == stem.lower()
        )
        return candidates[0] if candidates else None

    def _run_macos_app(self, app_bundle: Path, args: list[str], output_dir: Path, stem: str, output_type: str) -> Path:
        """Launch ODA through LaunchServices and wait for its output.

        ODA's macOS build is a Cocoa application.  Invoking the binary inside
        ``Contents/MacOS`` directly can fail before conversion with a
        PasteBoard ``Connection Invalid`` error, even in an interactive user
        session.  LaunchServices supplies the application session correctly;
        ``-n`` forces a fresh instance so command-line arguments are honored;
        ``-g`` keeps that helper window out of the user's foreground.
        """
        launcher = shutil.which("open") or "/usr/bin/open"
        command = [launcher, "-n", "-g", "-a", str(app_bundle), "--args", *args]
        try:
            subprocess.run(
                command,
                check=True,
                timeout=30,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CadPipelineError("图纸转换工具启动超时。") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()[-2000:]
            logger.warning(f"[CAD] 图纸转换工具启动失败：{detail}")
            raise CadPipelineError("图纸转换工具没能启动，请在 CAD 页重新连接后再试。") from exc

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            produced = self._produced_file(output_dir, stem, output_type)
            if produced is not None:
                return produced
            time.sleep(0.5)
        raise CadPipelineError(
            f"图纸转换工具在 {self.timeout} 秒内没有转出文件。"
        )

    def _run(self, source: Path, destination: Path, output_type: str) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="xl-cad-convert-") as temp:
            in_dir = Path(temp) / "in"
            out_dir = Path(temp) / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            input_file = in_dir / source.name
            shutil.copy2(source, input_file)
            command = [self.executable, str(in_dir), str(out_dir), "ACAD2018", output_type, "0", "1"]
            env = os.environ.copy()
            # ODA's macOS distribution is a Cocoa GUI application.  Forcing
            # Qt's offscreen plugin makes it fail before it even reaches the
            # conversion step because that plugin is not shipped in the app
            # bundle.  Keep the headless override only for Linux builds where
            # it is needed to run without a display server.
            if platform.system().lower() == "linux":
                env.setdefault("QT_QPA_PLATFORM", "offscreen")
            app_bundle = self._macos_app_bundle()
            if app_bundle is not None:
                produced = self._run_macos_app(app_bundle, command[1:], out_dir, source.stem, output_type)
            else:
                try:
                    subprocess.run(
                        command, check=True, timeout=self.timeout, cwd=str(temp), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise CadPipelineError(f"图纸转换超时（{self.timeout} 秒）。") from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or "").strip()[-2000:]
                    logger.warning(f"[CAD] 图纸转换失败：{detail}")
                    raise CadPipelineError("图纸转换工具没能转换这张图纸，可能已损坏或版本不受支持。") from exc
                produced = self._produced_file(out_dir, source.stem, output_type)
                if produced is None:
                    raise CadPipelineError("图纸转换工具没有转出文件。")
            shutil.copy2(produced, destination)
        return destination

    def dwg_to_dxf(self, source: Path, destination: Path) -> Path:
        return self._run(source, destination, "DXF")

    def dxf_to_dwg(self, source: Path, destination: Path) -> Path:
        return self._run(source, destination, "DWG")
