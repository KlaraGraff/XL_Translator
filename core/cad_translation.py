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
import fnmatch
import hashlib
import os
import platform
import inspect
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from core.cad_cache import CadDxfCache
from core.cad_layout import FitSettings, fit_translated_texts
from core.cad_writeback import (
    MARK_ADDED,
    MARK_COMBINED,
    TRANSLATION_STYLE,
    HandleAllocator,
    LinePlacement,
    added_line,
    append_marker,
    compatible_font,
    ensure_app_id,
    ensure_translation_style,
    has_cjk,
    marker_records,
    restore_previous_run,
    set_style,
    shx_safe,
)
from core.cad_language import (
    LATIN_TARGET_LANGS,
    NON_LATIN_SCRIPT_RE as _NON_LATIN_SCRIPT_RE,
    TARGET as _LANG_TARGET,
    UNSURE as _LANG_UNSURE,
    base_lang as _base_lang,
    classify_text_language,
    has_latin_words as _has_latin_words,
)
from core.request_progress import (
    BATCH_EVENT_DONE,
    BATCH_EVENT_PLAN,
    BatchRequestEvent,
    InFlightRequestTracker,
)

# 摘要表格给人看；manifest / report 里仍存内部状态码。
_UNIT_STATUS_LABELS = {"translated": "已翻译", "unresolved": "未完成", "kept": "已是目标语言"}

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
# A DWG goes through the external converter three times: DWG→DXF to read it
# (scan), DXF→DWG to write the translation back (write), and DWG→DXF again to
# read the result back for verification (verify).  Each conversion takes from
# seconds to minutes on a large drawing, so those stages get a real share of
# the bar instead of the near-zero share a DXF needs.
CAD_DWG_STAGE_WEIGHTS: dict[str, int] = {
    "scan": 20,
    "match": 2,
    "translate": 55,
    "write": 15,
    "verify": 8,
}


def cad_stage_weights(path: str | Path) -> dict[str, int]:
    """Per-stage weights (summing to 100) for one drawing, by its format."""
    return CAD_DWG_STAGE_WEIGHTS if Path(path).suffix.lower() == ".dwg" else CAD_STAGE_WEIGHTS

# AI 翻译阶段：多久没有请求返回就在运行日志里说一声（每隔 _TICK_SECONDS 看一次）。
_TICK_SECONDS = 0.5
_WAIT_LOG_SECONDS = 30.0

_TEXT_ENTITY_TYPES = frozenset({"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "MINSERT"})
# MLEADER text lives in group 304 inside CONTEXT_DATA.  Group 302 and 303 are
# structural markers and must never be sent to the translator.
_MLEADER_TEXT_CODE = 304
_DXF_FORMAT_RE = re.compile(r"\\[A-Za-z]+(?!\+)")
# DXF's ``\\U+`` escape is a four-hex-digit code unit.  A greedy 4-6 digit
# expression consumes the first letters of an ASCII word after the escape
# (``\\U+4E00Door`` used to become one invalid code point).
_DXF_UNICODE_RE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
# 老图纸（R2007 之前）里的东亚文字常以「多字节转义」存：\M+ 后一位代码页编号、
# 四位十六进制的双字节编码，例如 \M+5B5A5 是 GBK 的「单」。以前不解码，这些文字
# 看起来是一串 ASCII：语种判定把它当成「已是英文」跳过，整张图的中文一处没翻。
_DXF_MBCS_RE = re.compile(r"\\M\+([1-5])([0-9A-Fa-f]{4})")
_DXF_MBCS_CODECS = {"1": "cp932", "2": "cp950", "3": "cp949", "4": "cp1361", "5": "gbk"}
# 两种转义合在一起：MTEXT 切分时它们都属于正文，不是格式命令。
_DXF_CHAR_ESCAPE_RE = re.compile(r"\\U\+[0-9A-Fa-f]{4}|\\M\+[1-5][0-9A-Fa-f]{4}")
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
_DXF_EOF_RE = re.compile(rb"(?:^|\n)[ \t]*0[ \t]*\r?\n[ \t]*EOF[ \t]*(?:\r?\n)*\Z")
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


class CadLanguageTranslator(Protocol):
    """Translate and report each source text's language: ``{原文: (译文, 语种代码)}``."""

    def __call__(self, texts: list[str], glossary: Mapping[str, str]) -> Mapping[str, Any]: ...


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
    """Preflight information used by the CAD page and task manager.

    A DWG is never converted while scanning: the external converter takes
    seconds to minutes per drawing, and the scan is one request with no
    progress.  Its counts are ``None`` with ``counts_pending`` set; the task
    converts and counts it, with log lines and progress.
    """

    path: Path
    filename: str
    format: str
    text_entity_count: int | None
    candidate_count: int | None
    # DWG while no converter is connected: the file cannot be translated yet.
    needs_conversion: bool
    # 候选里程序判断不了语种、翻译时交给模型确认的条数（已计入 candidate_count）。
    uncertain_count: int | None = 0
    counts_pending: bool = False


def _read_scan_records(path: Path) -> list[DxfRecord]:
    """Read a DXF for the preflight counts (DWG is not read here)."""
    if path.suffix.lower() != ".dxf":
        raise CadPipelineError(f"不支持这种图纸格式：{path.suffix}（只支持 DWG 和 DXF）。")
    try:
        return _parse_dxf(_read_dxf_text(path))
    except UnicodeError as exc:
        raise CadPipelineError("图纸的文字编码无法识别（不是 UTF-8，也不是 GBK 等常见编码）。") from exc


def scan_cad_file(
    path: str | Path,
    *,
    converter_available: bool = False,
    options: CadPipelineOptions | None = None,
) -> CadScanSummary:
    """Scan one CAD file without modifying it or creating a translation task.

    DXF is parsed and counted.  DWG is only listed (see ``CadScanSummary``);
    ``converter_available`` says whether a converter is connected, which
    decides ``needs_conversion``.
    """
    options = options or CadPipelineOptions()
    source = Path(path)
    if source.suffix.lower() == ".dwg":
        return CadScanSummary(
            path=source,
            filename=source.name,
            format="dwg",
            text_entity_count=None,
            candidate_count=None,
            needs_conversion=not converter_available,
            uncertain_count=None,
            counts_pending=True,
        )
    return _summarize_records(source, _read_scan_records(source), options)


def _summarize_records(source: Path, records: Sequence[DxfRecord], options: CadPipelineOptions) -> CadScanSummary:
    """文字实体数、待翻译数和拿不准语种的条数（与翻译时的口径一致）。"""
    needs_conversion = False
    units = extract_cad_text_units(
        records,
        include_block_text=options.include_block_text,
        locked_blocks=options.locked_blocks,
        locked_layers=options.locked_layers,
    )
    candidate_count = 0
    uncertain_count = 0
    for unit in units:
        if (
            len(unit.plain_text) > options.max_text_length
            or not _has_translatable_content(unit.plain_text)
            or (unit.entity_type == "DIMENSION" and "<>" in unit.plain_text)
            or not _matches_source_language(unit.plain_text, options.source_lang)
        ):
            continue
        if options.skip_target_language:
            verdict = classify_text_language(unit.plain_text, options.target_lang)
            if verdict == _LANG_TARGET:
                continue
            if verdict == _LANG_UNSURE:
                # 拿不准的照样算待翻译：翻译时由模型确认，已是目标语言的保留原文。
                uncertain_count += 1
        candidate_count += 1
    return CadScanSummary(
        path=source,
        filename=source.name,
        format=source.suffix.lower().lstrip("."),
        text_entity_count=len(units),
        candidate_count=candidate_count,
        needs_conversion=needs_conversion,
        uncertain_count=uncertain_count,
    )


def list_cad_text_containers(records: Sequence[DxfRecord]) -> dict[str, list[dict[str, Any]]]:
    """含文字的图块和图层，供「锁定不翻译」清单勾选。

    图块：有名字的图块（匿名图块 ``*U``/``*D`` 等用户不认识，不列）里直接放了几段
    文字（含属性定义），以及在整张图里被插入了几次；没有文字的图块不列。
    图层：每个图层上有几段文字。
    """
    starts = [index for index, record in enumerate(records) if record.code == 0]
    starts.append(len(records))
    block_texts: dict[str, int] = {}
    block_inserts: dict[str, int] = {}
    layer_texts: dict[str, int] = {}
    display_names: dict[str, str] = {}
    current_block: str | None = None
    for start, end in zip(starts, starts[1:]):
        if start >= end:
            continue
        typ = records[start].value.strip().upper()
        body = records[start + 1:end]
        if typ == "BLOCK":
            name = next((r.value.strip() for r in body if r.code == 2), "")
            current_block = name.upper() if name and not name.startswith("*") else None
            if current_block:
                display_names.setdefault(current_block, name)
            continue
        if typ == "ENDBLK":
            current_block = None
            continue
        if typ in {"INSERT", "MINSERT"}:
            name = next((r.value.strip() for r in body if r.code == 2), "")
            if name and not name.startswith("*"):
                block_inserts[name.upper()] = block_inserts.get(name.upper(), 0) + 1
                display_names.setdefault(name.upper(), name)
            continue
        if typ not in _TEXT_ENTITY_TYPES and typ not in {"MLEADER", "MULTILEADER"}:
            continue
        if typ == "MINSERT":
            continue
        if current_block:
            block_texts[current_block] = block_texts.get(current_block, 0) + 1
        layer = next((r.value.strip() for r in body if r.code == 8), "0") or "0"
        layer_texts[layer] = layer_texts.get(layer, 0) + 1
    blocks = [
        {"name": display_names.get(key, key), "texts": count, "inserts": block_inserts.get(key, 0)}
        for key, count in block_texts.items()
        if block_inserts.get(key, 0)
    ]
    blocks.sort(key=lambda item: (-item["inserts"] * item["texts"], item["name"]))
    layers = [{"name": name, "texts": count} for name, count in layer_texts.items()]
    layers.sort(key=lambda item: (-item["texts"], item["name"]))
    return {"blocks": blocks, "layers": layers}


@dataclass(frozen=True)
class CadInspection:
    summary: CadScanSummary
    blocks: list[dict[str, Any]]
    layers: list[dict[str, Any]]
    # 这次是否真的调用了转换器（False：DXF，或用了缓存）。
    converted: bool = False


def inspect_cad_file(
    path: str | Path,
    *,
    converter: CadConverter | None,
    cache: CadDxfCache | None = None,
    options: CadPipelineOptions | None = None,
) -> CadInspection:
    """Open one drawing (converting a DWG, via ``cache`` when given) and count it.

    这是扫描页「逐张统计」和「扫描图块名」用的：一次只处理一张，界面每处理完一张就
    更新一行，可以随时停下。转换结果存进 ``cache``，开始翻译时直接复用。
    """
    options = options or CadPipelineOptions()
    source = Path(path)
    converted = False
    if source.suffix.lower() == ".dwg":
        cached = cache.lookup(source) if cache else None
        if cached is None:
            if converter is None:
                raise CadPipelineError("翻译 DWG 需要先连接图纸转换工具。")
            with tempfile.TemporaryDirectory(prefix="xl-cad-inspect-") as temporary:
                dxf_path = Path(temporary) / f"{source.stem}.dxf"
                converter.dwg_to_dxf(source, dxf_path)
                converted = True
                stored = cache.store(source, dxf_path) if cache else None
                records = _parse_dxf(_read_dxf_text(stored or dxf_path))
        else:
            records = _parse_dxf(_read_dxf_text(cached))
    else:
        records = _read_scan_records(source)
    summary = _summarize_records(source, records, options)
    containers = list_cad_text_containers(records)
    return CadInspection(summary, containers["blocks"], containers["layers"], converted)


def list_cad_files(paths: Iterable[str | Path]) -> list[Path]:
    """Selected DWG/DXF files, with selected directories expanded recursively.

    Nothing is opened or converted; previous translation outputs and CAD work
    folders inside a selected directory are left out.
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
    return list(dict.fromkeys(files))


def scan_cad_paths(
    paths: Iterable[str | Path],
    *,
    converter_available: bool = False,
    options: CadPipelineOptions | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> list[CadScanSummary]:
    """Scan selected CAD files and recursively expand selected directories.

    DWG files are listed without conversion (see ``scan_cad_file``).  When
    ``skipped`` is given, a file that cannot be read is recorded there
    (``{"path", "name", "reason"}``) and the rest of the batch is still
    scanned; without it the first error propagates, as before.
    """
    unique_files = list_cad_files(paths)
    if skipped is None:
        return [
            scan_cad_file(path, converter_available=converter_available, options=options)
            for path in unique_files
        ]
    summaries: list[CadScanSummary] = []
    for path in unique_files:
        try:
            summaries.append(scan_cad_file(path, converter_available=converter_available, options=options))
        except Exception as exc:  # noqa: BLE001 - one unreadable drawing must not hide the others
            skipped.append({"path": str(path), "name": path.name, "reason": str(exc) or exc.__class__.__name__})
    return summaries


@dataclass
class CadTranslationStats:
    scanned_entities: int = 0
    candidate_entities: int = 0
    skipped_target_language: int = 0
    # 程序拿不准、交给模型确认语种的文字条数，以及其中模型确认已是目标语言、保留原文的条数。
    language_checked: int = 0
    kept_target_language: int = 0
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
    # 「已是目标语言」里其实没有成词文字的那部分：门窗编号、轴号、型号、尺寸（C1518、1/B、3100X2100mm）。
    skipped_codes: int = 0
    # 译文比原文长、会压到同一行相邻文字，写回时缩小了字宽或字高的条数。
    layout_fitted: int = 0
    # 其中缩到最小比例仍放不下、需要人看一眼的条数。
    layout_unresolved: int = 0
    # 落在锁定图块 / 锁定图层里、按用户要求原样保留的文字条数（不计入文字实体）。
    locked_texts: int = 0
    # 双语显示时新增的译文文字条数（单行文字的译文另起一行）。
    bilingual_added: int = 0
    # 改用兼容字体写回的译文条数。
    restyled: int = 0


@dataclass
class CadProgress:
    stage: str
    done: int
    total: int
    percent: float
    message: str = ""
    # 运行日志的级别（INFO / OK / WARN / ERROR），只在有 message 时有意义。
    level: str = "INFO"


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
    # 译文会压到同一行相邻文字时缩小字宽/字高（见 core.cad_layout）。
    fit_layout: bool = True
    # 放不下时先缩到默认比例，再按 0.1 一档缩到最小比例（字高 × 字宽的整体比例）。
    fit_default_scale: float = 0.7
    fit_min_scale: float = 0.4
    # 锁定不翻译的图块名 / 图层名（大小写不敏感，可用 * ? 通配），见 parse_lock_patterns。
    locked_blocks: tuple[str, ...] = ()
    locked_layers: tuple[str, ...] = ()
    # translation 纯译文；source_first 原文在上、译文在下；translation_first 译文在上。
    display_mode: str = "translation"
    # compatible：译文改用 AutoCAD 自带字体（见 core.cad_writeback）；original：沿用原样式。
    translation_font: str = "original"


def _parse_dxf(text: str) -> list[DxfRecord]:
    # DXF 只按 \n（或 \r\n）分行。str.splitlines 还会在 \x0b、\x1c、\x85、
    # 和单独的 \r 处断开，文字里带这类字符的正常图纸会被错位成「格式无法识别」。
    lines = text.split("\n")
    raw_lines = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        raw_lines.append(lines[-1])
    if len(raw_lines) % 2:
        raise CadPipelineError("图纸中有一段内容格式无法识别，可能已损坏，未处理。")
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
            raise CadPipelineError("图纸中有一段内容格式无法识别，可能已损坏，未处理。") from exc
        records.append(DxfRecord(code, value, ending, code_text, code_ending))
    # 被截断的图纸（拷贝中断、网盘没同步完）照样能逐行配对解析，只是后半截没了：
    # 不拦的话它会以「0 个文字实体」通过扫描，任务里还标成「已生成」，产物是一份
    # 同样残缺的 DXF。开了 SECTION 却没有收尾的 ENDSEC，就是没读完的文件。
    opened = sum(1 for record in records if record.code == 0 and record.value.strip() == "SECTION")
    closed = sum(1 for record in records if record.code == 0 and record.value.strip() == "ENDSEC")
    if opened > closed:
        raise CadPipelineError("图纸文件不完整，可能已损坏或未完整复制，未处理。")
    return records


def _serialize_dxf(records: Iterable[DxfRecord]) -> str:
    result: list[str] = []
    for record in records:
        code = record.code_text or str(record.code)
        code_eol = record.code_line_ending or record.line_ending or "\n"
        value_eol = record.line_ending
        result.append(f"{code}{code_eol}{record.value}{value_eol}")
    return "".join(result)


def _decode_mbcs_escape(match: re.Match[str]) -> str:
    try:
        return bytes.fromhex(match.group(2)).decode(_DXF_MBCS_CODECS[match.group(1)])
    except (UnicodeDecodeError, ValueError):
        # 解不出来就原样留着：宁可当成看不懂的文字，也不能凭空变成别的字。
        return match.group(0)


def _plain_text(value: str) -> str:
    """Decode DXF Unicode/multibyte escapes and remove inline formatting for matching."""
    value = _DXF_UNICODE_RE.sub(lambda match: chr(int(match.group(1), 16)), value)
    value = _DXF_MBCS_RE.sub(_decode_mbcs_escape, value)
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
            unicode_escape = _DXF_CHAR_ESCAPE_RE.match(value, i)
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


# 用户写锁定名单时常用的分隔符。图块名、图层名本身不能含这些字符（AutoCAD 的命名规则
# 不允许 , ; / 等），所以可以放心拆；中文逗号、顿号也照收。
_LOCK_SPLIT_RE = re.compile(r"[,;/\n\r\t，、；]+")


def parse_lock_patterns(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """把用户写的锁定名单拆成去重后的名字/通配符（统一大写）。"""
    if not value:
        return ()
    parts = _LOCK_SPLIT_RE.split(value) if isinstance(value, str) else [str(item) for item in value]
    return tuple(dict.fromkeys(part.strip().upper() for part in parts if part and part.strip()))


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    upper = name.strip().upper()
    return any(fnmatch.fnmatchcase(upper, pattern) for pattern in patterns)


@dataclass
class CadLockReport:
    """锁定名单在一张图里的命中情况，用来在报告里提醒「名字写错、一个都没匹配到」。"""

    locked_texts: int = 0
    # 每个锁定名命中了几个图块 / 几段文字（图层）。
    block_matches: dict[str, int] = field(default_factory=dict)
    layer_matches: dict[str, int] = field(default_factory=dict)


def extract_cad_text_units(
    records: Sequence[DxfRecord],
    *,
    include_block_text: bool = True,
    locked_blocks: Sequence[str] = (),
    locked_layers: Sequence[str] = (),
    lock_report: CadLockReport | None = None,
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
    # 图块里又插了哪些图块（嵌套），以及每个图块被插在哪些图层上。
    nested_inserts: dict[str, set[str]] = {}
    insert_layers: dict[str, set[str]] = {}
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
                if current_block:
                    nested_inserts.setdefault(current_block, set()).add(name)
                layer = next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 8), "0")
                insert_layers.setdefault(name, set()).add(layer)
    reachable_ranges = [block_ranges[name] for name in referenced_blocks if name in block_ranges]
    reachable_ranges.extend(
        block_ranges[name] for name in ("*MODEL_SPACE", "*PAPER_SPACE") if name in block_ranges
    )
    block_patterns = tuple(locked_blocks)
    layer_patterns = tuple(locked_layers)
    # 锁定的图块连同它里面嵌套的子图块整个不翻（图框里的徽标、签名栏常是子图块）。
    locked_block_names: set[str] = set()
    if block_patterns:
        pending = []
        for name in block_ranges:
            if name.startswith("*"):
                continue
            hits = [pattern for pattern in block_patterns if fnmatch.fnmatchcase(name, pattern)]
            if hits:
                pending.append(name)
                if lock_report is not None:
                    for pattern in hits:
                        lock_report.block_matches[pattern] = lock_report.block_matches.get(pattern, 0) + 1
        while pending:
            name = pending.pop()
            if name in locked_block_names:
                continue
            locked_block_names.add(name)
            pending.extend(nested_inserts.get(name, ()))
    if lock_report is not None:
        for pattern in block_patterns:
            lock_report.block_matches.setdefault(pattern, 0)
        for pattern in layer_patterns:
            lock_report.layer_matches.setdefault(pattern, 0)
    locked_ranges = [block_ranges[name] for name in locked_block_names if name in block_ranges]
    # 图块里画在 0 层的东西随插入它的图层显示：这个图块只插在锁定图层上时，一并锁定。
    layer_locked_ranges = [
        block_ranges[name]
        for name, layers in insert_layers.items()
        if layer_patterns and name in block_ranges and layers and all(_matches_any(layer, layer_patterns) for layer in layers)
    ]
    # ATTRIB 跟在所属 INSERT 后面，直到 SEQEND：记下每段属性归哪个插入。
    owner_insert: dict[int, tuple[str, str]] = {}
    current_insert: tuple[str, str] | None = None
    for start, end in zip(starts, starts[1:]):
        if start >= end:
            continue
        typ = records[start].value.strip().upper()
        if typ in {"INSERT", "MINSERT"}:
            current_insert = (
                next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 2), ""),
                next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 8), "0"),
            )
        elif typ == "ATTRIB" and current_insert is not None:
            owner_insert[start] = current_insert
        else:
            current_insert = None

    def lock_reason(start: int, entity_type: str, layer: str) -> str | None:
        """这段文字被哪个锁定名单挡下（返回命中的图层名单项，或 ``"block"``）；不锁返回 None。"""
        if any(lo <= start < hi for lo, hi in locked_ranges):
            return "block"
        owner = owner_insert.get(start) if entity_type == "ATTRIB" else None
        if owner and owner[0] in locked_block_names:
            return "block"
        if layer_patterns:
            for candidate in (layer, owner[1] if owner else None):
                if candidate is None:
                    continue
                for pattern in layer_patterns:
                    if fnmatch.fnmatchcase(candidate, pattern):
                        return pattern
            if layer in {"", "0"} and any(lo <= start < hi for lo, hi in layer_locked_ranges):
                return next(iter(layer_patterns))
        return None

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
        if block_patterns or layer_patterns:
            reason = lock_reason(start, entity_type, layer)
            if reason is not None:
                if lock_report is not None:
                    lock_report.locked_texts += 1
                    if reason != "block":
                        lock_report.layer_matches[reason] = lock_report.layer_matches.get(reason, 0) + 1
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


def _is_likely_target_language(text: str, target_lang: str) -> bool:
    """Whether the text is confidently already in ``target_lang`` (see core.cad_language)."""
    return classify_text_language(text, target_lang) == _LANG_TARGET


def _is_residual_foreign_text(text: str, target_lang: str) -> bool:
    """Whether written-back text still looks untranslated for ``target_lang``.

    For CJK/other-script targets the leftover must be real Latin words, not
    part numbers or standard codes. For Latin targets the leftover is any
    remaining CJK/other non-Latin script.
    """
    if _base_lang(target_lang) in LATIN_TARGET_LANGS:
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


def _has_real_words(text: str) -> bool:
    """有成词的文字（汉字等非拉丁文字，或不是代号的拉丁单词）；C1518、1/B、3100X2100mm 没有。"""
    return bool(_NON_LATIN_SCRIPT_RE.search(text)) or _has_latin_words(text)


def _language_result(item: Any) -> tuple[str | None, str]:
    """``(译文, 原文语种)``；没拿到译文时译文为 ``None``。

    语种翻译器交回 ``(译文, 语种)`` 二元组，或带 ``translation`` /
    ``source_lang`` 属性的对象（engine_dispatcher 的 TranslationLanguageResult）。
    """
    if item is None:
        return None, ""
    if isinstance(item, tuple) and len(item) == 2:
        value, source_lang = item
    else:
        value = getattr(item, "translation", None)
        source_lang = getattr(item, "source_lang", "")
    if value is None:
        return None, ""
    return str(value).strip(), _base_lang(str(source_lang or ""))


def _numeric_tokens(text: str) -> list[str]:
    # 只排除紧跟在拉丁字母/数字/下划线后面的数字（Q235、M10 这类代号的一部分）。
    # 以前用 \w 判断，而 \w 也匹配汉字，于是「共12页」「译文12345」里的数字全被漏掉，
    # 中文译文一律判成「数字被改动」，整条译文被丢弃并标为需复核。
    # 「2830X1700mm」里乘号后面的 1700 也是数字，不是代号的一部分：前面是「数字+X/×/*」时照样算。
    return re.findall(r"(?:(?<![A-Za-z0-9_])|(?<=\d[xX×*]))\d+(?:[.,]\d+)?", text)


def _valid_translation(source: str, target: str) -> tuple[bool, str]:
    value = str(target or "").strip()
    if not value:
        return False, "译文为空"
    if any(char in value for char in _REPLACEMENT_CHARS):
        return False, "译文中有乱码"
    if _numeric_tokens(source) != _numeric_tokens(value):
        return False, "译文中的数字与原文不一致"
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


def _encode_dxf_text(value: str, *, multiline: bool, escape: bool = True) -> str:
    """Keep one group value on one physical DXF line.

    ``escape`` writes non-ASCII as DXF ``\\U+XXXX``.  Only old (pre-2007)
    DXF needs that; in a UTF-8 DXF the characters are written as they are,
    because AutoCAD shows the escape literally in single-line text whose
    style is a TrueType font or an SHX font without a big font (verified in
    AutoCAD 2026: 「\\U+00E9」 appeared instead of é).
    """
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\n", r"\P" if multiline else " ")
    if not escape:
        return value
    return "".join(char if ord(char) < 128 else f"\\U+{ord(char):04X}" for char in value)


def _writes_real_unicode(records: Sequence[DxfRecord], encoding: str) -> bool:
    """Whether translated text may be written as real characters.

    DXF from AutoCAD 2007 on ($ACADVER AC1021 and later) is UTF-8 throughout.
    Older DXF is read in the drawing's code page, so non-ASCII stays escaped.
    """
    if encoding.replace("_", "-").lower() not in {"utf-8", "utf8"}:
        return False
    for index, record in enumerate(records[:-1]):
        if record.code == 9 and record.value.strip().upper() == "$ACADVER":
            version = records[index + 1].value.strip().upper()
            match = re.fullmatch(r"AC(\d{4})", version)
            return bool(match) and int(match.group(1)) >= 1021
        if record.code == 0 and record.value.strip().upper() == "ENDSEC":
            break
    return False


def _ascii_chunks(value: str, limit: int = 240) -> list[str]:
    # Never split a DXF Unicode escape or formatting command across records.
    # The limit counts UTF-8 bytes, so a chunk of real CJK/accented
    # characters stays within the group-value length CAD readers accept.
    tokens = re.findall(r"\\[fFhHwWcCaAtTqQoOsS][^;]*;|\\U\+[0-9A-Fa-f]{4}|\\M\+[1-5][0-9A-Fa-f]{4}|\\[A-Za-z]+|.", value)
    chunks: list[str] = []
    current = ""
    for token in tokens:
        if current and len(current.encode("utf-8")) + len(token.encode("utf-8")) > limit:
            chunks.append(current)
            current = ""
        current += token
    if current or not chunks:
        chunks.append(current)
    return chunks


def _replace_unit(records: list[DxfRecord], unit: CadTextUnit, translated: str, *, escape: bool = True) -> None:
    value = _encode_dxf_text(
        _safe_translation(unit.original, translated), multiline=unit.entity_type == "MTEXT", escape=escape,
    )
    if unit.entity_type == "MTEXT":
        chunks = _ascii_chunks(value)
        final = next((index for index in reversed(unit.record_indices) if records[index].code == 1), None)
        if final is None:
            raise CadPipelineError("图纸中有一段文字格式无法识别，可能已损坏。")
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


# MTEXT 里切换 SHX 字体的内联代码（\F...;）。译文改用兼容字体时去掉，否则它会盖过新样式；
# TrueType 的 \f...; 照留（TrueType 字体本来就有重音字母，还带着粗体、斜体设置）。
_MTEXT_SHX_FONT_RE = re.compile(r"\\F[^;]*;")


def _assemble_runs(
    records: Sequence[DxfRecord],
    unit: CadTextUnit,
    translations: Mapping[str, str],
    *,
    escape: bool = True,
) -> str:
    """The MTEXT / MLEADER value with every translated run replaced (controls kept)."""
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
            escape=escape,
        )
        assembled = assembled[:run.start] + replacement + assembled[run.end:]
    return assembled


def _write_mtext_value(records: list[DxfRecord], unit: CadTextUnit, value: str, *, escape: bool = True) -> None:
    """Write an MTEXT / MLEADER value back over the unit's records, chunking MTEXT."""
    if unit.entity_type in {"MLEADER", "MULTILEADER"}:
        index = unit.record_indices[0]
        records[index] = _record_copy(records[index], value=value, code=304)
        return
    encoded = _encode_dxf_text(value, multiline=True, escape=escape)
    chunks = _ascii_chunks(encoded)
    final = next((index for index in reversed(unit.record_indices) if records[index].code == 1), None)
    if final is None:
        raise CadPipelineError("图纸中有一段文字格式无法识别，可能已损坏。")
    continuation = [index for index in unit.record_indices if records[index].code == 3]
    for index in continuation:
        records[index] = _record_copy(records[index], value="", code=3)
    for index, chunk in zip(continuation, chunks[:-1]):
        records[index] = _record_copy(records[index], value=chunk, code=3)
    extra = chunks[len(continuation):-1]
    for offset, chunk in enumerate(extra):
        records.insert(final + offset, _record_copy(records[final], value=chunk, code=3))
    records[final + len(extra)] = _record_copy(records[final + len(extra)], value=chunks[-1], code=1)


def _replace_unit_runs(
    records: list[DxfRecord],
    unit: CadTextUnit,
    translations: Mapping[str, str],
    *,
    escape: bool = True,
    strip_shx_fonts: bool = False,
) -> None:
    """Replace MTEXT runs while preserving controls and paragraph topology."""
    if not unit.runs or unit.entity_type not in {"MTEXT", "MLEADER", "MULTILEADER"}:
        value = translations.get(unit.plain_text)
        if value:
            _replace_unit(records, unit, value, escape=escape)
        return
    assembled = _assemble_runs(records, unit, translations, escape=escape)
    if strip_shx_fonts:
        assembled = _MTEXT_SHX_FONT_RE.sub("", assembled)
    _write_mtext_value(records, unit, assembled, escape=escape)


def _entity_start(records: Sequence[DxfRecord], index: int) -> int:
    while index > 0 and records[index].code != 0:
        index -= 1
    return index


def _entity_end(records: Sequence[DxfRecord], start: int) -> int:
    for index in range(start + 1, len(records)):
        if records[index].code == 0:
            return index
    return len(records)


def _translated_plain(unit: CadTextUnit, translations: Mapping[str, str]) -> str:
    if not unit.runs:
        return translations.get(unit.plain_text, unit.plain_text)
    return " ".join(translations.get(run.text, run.text) for run in unit.runs)


_BILINGUAL_MODES = frozenset({"source_first", "translation_first"})
_CJK_TRANSLATION_FONT = compatible_font("zh")
# 双语时译文比原文小一号；多行文字里用相对字高写。
_BILINGUAL_SCALE = 0.8
_ATTRIB_SEPARATOR = " / "


def _write_bilingual(
    records: list[DxfRecord],
    unit: CadTextUnit,
    translations: Mapping[str, str],
    start: int,
    *,
    below: bool,
    handles: HandleAllocator,
    style: str | None,
    can_mark: bool,
    escape: bool,
) -> DxfRecord | None:
    """Write 原文 + 译文 for one unit; returns the ``0`` record of an added TEXT.

    * TEXT：原文不动，在下方（译文在上时为上方）新增一段译文文字，字高为原文的 0.8；
    * MTEXT / 引线：同一段里换行追加译文（相对字高 0.8），由 CAD 自己排版；
    * 尺寸标注的替代文字：同一段里换行追加；
    * 图块属性：没法另加一行，同一行写「原文 / 译文」。

    改成双语的实体都带一个隐藏标记，同一张图再翻一次时先恢复原样（见 core.cad_writeback）。
    """
    end = _entity_end(records, start)
    entity_type = unit.entity_type
    if entity_type == "TEXT":
        translation = translations.get(unit.plain_text)
        if not translation:
            return None
        value = _encode_dxf_text(_safe_translation(unit.original, translation), multiline=False, escape=escape)
        marker = marker_records(records[start], MARK_ADDED) if can_mark else []
        line = added_line(
            records[start:end], value, handles.next(), LinePlacement(below=below, scale=_BILINGUAL_SCALE), style, marker,
        )
        records[end:end] = line
        return line[0]
    original = "".join(records[index].value for index in unit.record_indices)
    if entity_type in {"MTEXT", "MLEADER", "MULTILEADER"}:
        translated = _assemble_runs(records, unit, translations, escape=escape)
        part = "{\\H%sx;%s}" % (_BILINGUAL_SCALE, translated)
        _write_mtext_value(records, unit, f"{original}\\P{part}" if below else f"{part}\\P{original}", escape=escape)
    else:
        translation = translations.get(unit.plain_text)
        if not translation:
            return None
        encoded = _encode_dxf_text(_safe_translation(unit.original, translation), multiline=False, escape=escape)
        joiner = _ATTRIB_SEPARATOR if entity_type in {"ATTRIB", "ATTDEF"} else "\\P"
        value = f"{original}{joiner}{encoded}" if below else f"{encoded}{joiner}{original}"
        index = unit.record_indices[0]
        records[index] = _record_copy(records[index], value=value)
        for extra in unit.record_indices[1:]:
            records[extra] = _record_copy(records[extra], value="")
    if can_mark:
        end = _entity_end(records, start)
        records[start + 1:end] = append_marker(
            list(records[start + 1:end]), marker_records(records[start], MARK_COMBINED, original),
        )
    return None


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
        pause_gate: Callable[[], None] | None = None,
        language_translator: CadLanguageTranslator | None = None,
        dxf_cache: CadDxfCache | None = None,
    ) -> None:
        self.converter = converter
        # 扫描时转换过的 DWG 留在这里，翻译时直接复用，不再转第二遍。
        self.dxf_cache = dxf_cache
        self.memory_lookup = memory_lookup
        self.translator = translator
        # 程序判断不了语种的文字走这个翻译器：模型同时回报每条原文的语种代码，
        # 回报的就是目标语言时保留原文。不传时这些文字和其余文字一起照常翻译。
        self.language_translator = language_translator
        self.progress = progress
        self.stop_requested = stop_requested
        # 暂停闸门：用户点了暂停时阻塞到继续或停止为止。由任务 runner 提供；
        # 不传就是不支持文件内暂停（老调用方、测试替身）。
        self.pause_gate = pause_gate
        self.last_translate_seconds = 0.0
        # Set per drawing by ``translate_file``: a DWG spends real time in the
        # converter at scan / write / verify.
        self._stage_weights: dict[str, int] = CAD_STAGE_WEIGHTS

    def _wait_if_paused(self) -> None:
        if self.pause_gate is not None:
            self.pause_gate()

    def _check_stop(self) -> None:
        # 每个检查点先过暂停闸门：只有一张图纸时，以前的暂停要等整张图翻完才生效，
        # 等于点了没用。闸门放行后再看是不是在暂停期间点了停止。
        self._wait_if_paused()
        if self.stop_requested and self.stop_requested():
            raise CadStopRequested("已停止；已完成的部分已保留。")

    def _emit(
        self,
        stage: str,
        done: int,
        total: int,
        message: str = "",
        *,
        fraction: float | None = None,
        level: str = "INFO",
    ) -> None:
        """Report a stage step.  ``fraction`` overrides ``done / total`` for the bar.

        The AI translation step passes the returned share kept monotonic, since
        returns from several request threads can arrive out of order.
        """
        stage_total = max(1, total)
        if fraction is None:
            fraction = done / stage_total
        fraction = min(1.0, max(0.0, fraction))
        weights = self._stage_weights
        stages = list(weights)
        before = sum(weights[name] for name in stages[: stages.index(stage)])
        percent = before + weights[stage] * fraction
        if self.progress:
            self.progress(CadProgress(stage, done, total, round(percent, 2), message, level))

    def _convert(
        self,
        stage: str,
        convert: Callable[[Path, Path], Path],
        source: Path,
        destination: Path,
        *,
        started: str,
        finished: str,
        done: int = 1,
        total: int = 1,
    ) -> None:
        """Run one external conversion between two progress points.

        The converter reports nothing while it runs, so the bar holds at the
        stage start and moves once the conversion has really finished; the
        run log says when it started and how long it took.
        """
        self._emit(stage, 0, total, started)
        began = time.monotonic()
        convert(source, destination)
        seconds = f"{time.monotonic() - began:.1f}"
        self._emit(stage, done, total, finished.format(seconds))

    def _call_translator(
        self,
        remaining: list[str],
        glossary: Mapping[str, str],
        *,
        already: int,
        total: int,
        translator: Callable[..., Mapping[str, Any]] | None = None,
    ) -> Mapping[str, Any]:
        """调用翻译器，把逐批返回的进度折算进「AI 翻译」阶段，并把请求情况写进运行日志。

        进度只按真正返回的条数统计，还在路上的请求不算（maple 2026-10-12：路上也加
        是造假）。翻译器肯接 ``request_callback`` 时（api/surfaces/cad.py 接的是
        translate_texts 的逐批事件），运行日志写明分几批、每次同时发几批、每批返回了
        多少条、用了多久，长时间没有返回时说明在等几批。老式翻译器照旧调用。
        """
        tracker = InFlightRequestTracker()
        emit_lock = threading.Lock()
        shown = {"fraction": 0.0}
        stage_total = max(1, total)

        def emit(message: str = "", level: str = "INFO") -> None:
            with emit_lock:
                done = already + tracker.done_items()
                # 只进不退：多个线程的返回可能乱序到达。
                fraction = max(shown["fraction"], min(1.0, done / stage_total))
                shown["fraction"] = fraction
                self._emit("translate", min(done, total), stage_total, message, fraction=fraction, level=level)

        def report(done: int, batch_total: int) -> None:
            fraction = min(1.0, max(0.0, done / max(1, batch_total)))
            tracker.note_reported_done(int(len(remaining) * fraction))
            emit()
            # 批次回调跑在提交批次的那个线程里：在这里等着，就不会再发新的一批请求，
            # 已经发出去的几批照常收回来。这就是「文件内暂停」，不用丢弃任何已付费的结果。
            self._wait_if_paused()

        def on_request(event: BatchRequestEvent) -> None:
            tracker.on_event(event)
            if event.kind == BATCH_EVENT_PLAN:
                emit(
                    f"开始翻译 {event.items} 条文字（相同内容只翻译一次）："
                    f"分 {event.batch_total} 批发送，每次同时发送 {event.concurrency} 批"
                )
            elif event.kind == BATCH_EVENT_DONE and event.stopped < event.items:
                seconds = f"{event.elapsed:.1f}"
                if event.failed:
                    emit(
                        f"第 {event.index}/{event.batch_total} 批已返回：{event.items} 条中 "
                        f"{event.failed} 条未获得译文，用时 {seconds} 秒",
                        "WARN",
                    )
                else:
                    emit(
                        f"第 {event.index}/{event.batch_total} 批已返回：{event.items} 条，用时 {seconds} 秒"
                    )

        def on_error(message: str) -> None:
            # 与 Excel 相同：整批放弃记 ERROR，拆小重试、限流降速等记 WARN。
            emit(message, "ERROR" if re.search(r"条内容未能翻译", message) else "WARN")

        translator = translator or self.translator
        try:
            parameters = inspect.signature(translator).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs: dict[str, Any] = {}
        if "progress" in parameters:
            kwargs["progress"] = report
        # 停止也要在批次之间生效，不然点了停止还得等整张图的请求全部发完。
        if "should_stop" in parameters and self.stop_requested is not None:
            kwargs["should_stop"] = self.stop_requested
        if "error_callback" in parameters:
            kwargs["error_callback"] = on_error
        ticker_stop = threading.Event()
        ticker: threading.Thread | None = None
        if "request_callback" in parameters:
            kwargs["request_callback"] = on_request

            def tick() -> None:
                next_wait_log = _WAIT_LOG_SECONDS
                while not ticker_stop.wait(_TICK_SECONDS):
                    waiting = tracker.in_flight_count()
                    quiet = tracker.seconds_since_last_return()
                    if not waiting or quiet < next_wait_log:
                        if quiet < _WAIT_LOG_SECONDS:
                            next_wait_log = _WAIT_LOG_SECONDS
                        continue
                    emit(f"正在等待 {waiting} 批请求返回，已等待 {int(quiet)} 秒")
                    next_wait_log = quiet + _WAIT_LOG_SECONDS

            ticker = threading.Thread(target=tick, name="cad-translate-wait-log", daemon=True)
        emit()
        started = time.monotonic()
        if ticker is not None:
            ticker.start()
        try:
            return translator(remaining, glossary, **kwargs)
        finally:
            ticker_stop.set()
            if ticker is not None:
                ticker.join(timeout=2)
            self.last_translate_seconds = time.monotonic() - started

    def translate_file(
        self,
        source: str | Path,
        output: str | Path,
        *,
        options: CadPipelineOptions | None = None,
        glossary: Mapping[str, str] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        pause_gate: Callable[[], None] | None = None,
    ) -> CadTranslationResult:
        options = options or CadPipelineOptions()
        if stop_requested is not None:
            self.stop_requested = stop_requested
        if pause_gate is not None:
            self.pause_gate = pause_gate
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
        self._stage_weights = cad_stage_weights(source)
        with tempfile.TemporaryDirectory(prefix="xl-cad-") as temporary:
            temp_root = Path(temporary)
            work_dxf = temp_root / f"{source.stem}.dxf"
            if source.suffix.lower() == ".dxf":
                self._emit("scan", 0, 1)
                shutil.copy2(source, work_dxf)
            elif (cached := self.dxf_cache.lookup(source) if self.dxf_cache else None) is not None:
                shutil.copyfile(cached, work_dxf)
                self._emit("scan", 0, 1, "使用扫描时已转换好的图纸，跳过格式转换")
            elif self.converter is not None:
                # DWG 转 DXF 要调用外部转换器，大图纸要几秒到几分钟：开始和结束各记一行
                # 运行日志，进度在转换真正完成时才前进。
                self._convert(
                    "scan",
                    self.converter.dwg_to_dxf,
                    source,
                    work_dxf,
                    started="正在转换图纸格式（DWG → DXF）",
                    finished="图纸格式转换完成，用时 {0} 秒",
                )
                if self.dxf_cache is not None:
                    self.dxf_cache.store(source, work_dxf)
            else:
                raise CadPipelineError("翻译 DWG 需要先连接图纸转换工具。")
            # Keep a recoverable input/work copy even if a later provider or
            # converter step fails.  Both evidence copies are removed again once
            # the drawing has passed verification (see _discard_evidence).
            shutil.copy2(work_dxf, evidence_dir / "source.dxf")
            try:
                dxf_encoding = _detect_dxf_encoding(work_dxf)
                dxf_text = _read_dxf_text(work_dxf, dxf_encoding)
            except UnicodeError as exc:
                raise CadPipelineError("图纸的文字编码无法识别（不是 UTF-8，也不是 GBK 等常见编码）。") from exc
            records = _parse_dxf(dxf_text)
            # 新格式（UTF-8）DXF 的译文直接写字符；老格式按代码页读，仍写 \U+XXXX。
            escape_unicode = not _writes_real_unicode(records, dxf_encoding)
            restored = restore_previous_run(records)
            if restored:
                self._emit("scan", 0, 1, f"图纸里有 {restored} 处上次写入的双语译文，已先恢复原文再重新翻译")
            bilingual = options.display_mode in _BILINGUAL_MODES
            handles = HandleAllocator(records)
            font = compatible_font(options.target_lang) if options.translation_font == "compatible" else None
            # 表记录要在取文字之前加：之后再插记录，取出的文字位置就全错开了。
            style_name = TRANSLATION_STYLE if font and ensure_translation_style(records, handles, font) else None
            can_mark = ensure_app_id(records, handles) if bilingual else False
            stats = CadTranslationStats(scanned_entities=sum(1 for r in records if r.code == 0))
            self._emit("scan", 1, 1, "已读取图纸")
            lock_report = CadLockReport()
            units = extract_cad_text_units(
                records,
                include_block_text=options.include_block_text,
                locked_blocks=options.locked_blocks,
                locked_layers=options.locked_layers,
                lock_report=lock_report,
            )
            stats.candidate_entities = len(units)
            stats.locked_texts = lock_report.locked_texts
            if options.locked_blocks or options.locked_layers:
                self._emit("scan", 1, 1, f"锁定不翻译的图块和图层里有 {lock_report.locked_texts} 处文字，保持原样")
                missing_blocks = [name for name, count in lock_report.block_matches.items() if not count]
                missing_layers = [name for name, count in lock_report.layer_matches.items() if not count]
                if missing_blocks:
                    self._emit(
                        "scan", 1, 1,
                        f"这张图里没有找到这些图块：{', '.join(missing_blocks)}，请检查锁定名单是否写错",
                        level="WARN",
                    )
                if missing_layers:
                    self._emit(
                        "scan", 1, 1,
                        f"这张图里没有找到这些图层：{', '.join(missing_layers)}，请检查锁定名单是否写错",
                        level="WARN",
                    )
            candidates: list[CadTextUnit] = []
            # 整条文字程序拿不准语种的那些（只在「已是目标语言的不翻译」打开时有意义）。
            uncertain_units: set[str] = set()
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
                if options.skip_target_language:
                    verdict = classify_text_language(unit.plain_text, options.target_lang)
                    if verdict == _LANG_TARGET:
                        stats.skipped_target_language += 1
                        if not _has_real_words(unit.plain_text):
                            stats.skipped_codes += 1
                        continue
                    if verdict == _LANG_UNSURE:
                        uncertain_units.add(unit.unit_id)
                candidates.append(unit)
            timings["scan"] = round(time.perf_counter() - started_at, 4)
            self._emit("scan", 1, 1, f"找到 {len(candidates)} 处可翻译文字")
            skipped_numbers = stats.skipped_non_text
            skipped_codes = stats.skipped_codes
            skipped_words = stats.skipped_target_language - stats.skipped_codes
            if skipped_numbers or skipped_codes or skipped_words:
                # 文字实体和待翻译差得多时，用户第一反应是「是不是识别错了」：把差在哪说清楚。
                self._emit(
                    "scan", 1, 1,
                    f"共 {len(units)} 处文字，不需要翻译的：纯数字和符号 {skipped_numbers} 处，"
                    f"编号、轴号、型号和尺寸 {skipped_codes} 处，已是目标语言 {skipped_words} 处",
                )
            texts = list(dict.fromkeys(
                run.text for unit in candidates for run in unit.runs if _has_translatable_content(run.text)
            ))
            if not texts:
                texts = list(dict.fromkeys(unit.plain_text for unit in candidates))
            # 拿不准语种的整条文字里，各段格式文字自己也拿不准的，翻译时让模型确认语种。
            uncertain_texts = {
                text
                for unit in candidates
                if unit.unit_id in uncertain_units
                for text in ([run.text for run in unit.runs] or [unit.plain_text])
                if classify_text_language(text, options.target_lang) == _LANG_UNSURE
            }
            kept_texts: set[str] = set()
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
            checked = [text for text in remaining if text in uncertain_texts] if self.language_translator else []
            checked_set = set(checked)
            plain = [text for text in remaining if text not in checked_set]
            sent = 0
            translate_seconds = 0.0
            if plain and self.translator is not None:
                ai_results = dict(
                    self._call_translator(plain, glossary, already=len(translations), total=len(texts))
                )
                sent += len(plain)
                translate_seconds += self.last_translate_seconds
                for text in plain:
                    value = str(ai_results.get(text, "") or "").strip()
                    valid, _reason = _valid_translation(text, value)
                    if valid:
                        translations[text] = value
                    else:
                        invalid_reasons[text] = _reason
            if checked:
                self._check_stop()
                stats.language_checked = len(checked)
                checked_results = dict(
                    self._call_translator(
                        checked,
                        glossary,
                        already=len(translations),
                        total=len(texts),
                        translator=self.language_translator,
                    )
                )
                sent += len(checked)
                translate_seconds += self.last_translate_seconds
                target_code = _base_lang(options.target_lang)
                for text in checked:
                    value, source_code = _language_result(checked_results.get(text))
                    if value is None:
                        invalid_reasons[text] = "未获得译文"
                        continue
                    if source_code == target_code:
                        # 模型确认原文已是目标语言：保留原文，不算翻译，也不用复核。
                        kept_texts.add(text)
                        continue
                    valid, _reason = _valid_translation(text, value)
                    if valid:
                        translations[text] = value
                    else:
                        invalid_reasons[text] = _reason
                stats.kept_target_language = len(kept_texts)
                self._emit(
                    "translate",
                    len(translations),
                    max(1, len(texts)),
                    f"{len(checked)} 条文字由模型确认语种，其中 {len(kept_texts)} 条已是目标语言，保留原文",
                )
            stats.ai_translated = sum(1 for text in remaining if text in translations)
            if sent:
                missing = sent - stats.ai_translated - len(kept_texts)
                seconds = f"{translate_seconds:.1f}"
                # 翻译这一步已经结束：进度条补满这一段（未获得译文的也已经有了结果），
                # 计数仍是真正拿到译文的条数。
                if missing:
                    self._emit(
                        "translate", len(translations), max(1, len(texts)),
                        f"模型翻译结束：{stats.ai_translated} 条获得译文，{missing} 条未获得译文，用时 {seconds} 秒",
                        fraction=1.0,
                        level="WARN",
                    )
                else:
                    self._emit(
                        "translate", len(translations), max(1, len(texts)),
                        f"模型翻译完成：{stats.ai_translated} 条获得译文，用时 {seconds} 秒",
                        fraction=1.0,
                        level="OK",
                    )
            else:
                self._emit("translate", len(translations), max(1, len(texts)), "翻译完成", fraction=1.0)
            stats.untranslated = sum(1 for text in texts if text not in translations and text not in kept_texts)
            timings["translate"] = round(time.perf_counter() - started_at - timings["scan"] - timings["match"], 4)
            self._check_stop()
            # 写进图纸的译文：用兼容的 SHX 字体时，弯引号、长破折号、œ 这类字体里可能没有的
            # 字符换成同义写法（报告里仍是模型给的原样）。
            written = {key: shx_safe(value) for key, value in translations.items()} if font == compatible_font("en") else translations
            fit_sources: dict[int, str] = {}
            for unit in reversed(candidates):
                fully = bool(unit.runs) and all(run.text in written for run in unit.runs if _has_translatable_content(run.text))
                changed_unit = any(run.text in written for run in unit.runs)
                if not unit.runs and unit.plain_text in written:
                    changed_unit = fully = True
                if not changed_unit:
                    continue
                stats.changed += 1
                start = _entity_start(records, unit.record_indices[0])
                if bilingual:
                    added = _write_bilingual(
                        records, unit, written, start,
                        below=options.display_mode == "source_first",
                        handles=handles,
                        style=style_name if fully else None,
                        can_mark=can_mark,
                        escape=escape_unicode,
                    )
                    if added is not None:
                        stats.bilingual_added += 1
                        fit_sources[id(added)] = unit.plain_text
                    elif unit.entity_type == "ATTRIB":
                        fit_sources[id(records[start])] = unit.plain_text
                    continue
                restyle = (
                    style_name is not None
                    and fully
                    and unit.entity_type in {"TEXT", "ATTRIB", "MTEXT"}
                    and (font == _CJK_TRANSLATION_FONT or not has_cjk(_translated_plain(unit, written)))
                )
                _replace_unit_runs(records, unit, written, escape=escape_unicode, strip_shx_fonts=restyle)
                if restyle:
                    end = _entity_end(records, start)
                    records[start + 1:end] = set_style(list(records[start + 1:end]), TRANSLATION_STYLE)
                    stats.restyled += 1
                if unit.entity_type in {"TEXT", "ATTRIB"}:
                    fit_sources[id(records[start])] = unit.plain_text
            handles.finish()
            layout_unresolved: list[dict[str, str]] = []
            if options.fit_layout:
                # 单行文字的译文伸出去会压到同一行紧挨着的另一段文字（「设备基坑：」后面的
                # 「2830X1700mm」）：这些译文按能用的空间缩小字宽/字高。
                # 实体按 0 记录对象认：双语新增的文字会让后面实体的序号整体后移。
                ordinals = {id(record): number for number, record in enumerate(r for r in records if r.code == 0)}
                stats.layout_fitted = fit_translated_texts(
                    records,
                    {ordinals[key]: text for key, text in fit_sources.items() if key in ordinals},
                    _plain_text,
                    FitSettings(options.fit_default_scale, options.fit_min_scale),
                    layout_unresolved,
                )
                stats.layout_unresolved = len(layout_unresolved)
                if stats.layout_fitted:
                    self._emit(
                        "write", 0, 1,
                        f"{stats.layout_fitted} 处译文比原文长、会压到旁边的文字，已缩小字宽或字高",
                    )
                if layout_unresolved:
                    self._emit(
                        "write", 0, 1,
                        f"{len(layout_unresolved)} 处译文缩到最小比例仍会压到旁边的文字，已列入报告，请复核",
                        level="WARN",
                    )
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
                self._convert(
                    "write",
                    self.converter.dxf_to_dwg,
                    translated_dxf,
                    staged_output,
                    started="正在将译文转换回 DWG 图纸",
                    finished="已转换回 DWG 图纸，用时 {0} 秒",
                )
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
                # 读回刚生成的 DWG 核对文字：转换完成算校验阶段的一半，检查做完才到头。
                self._convert(
                    "verify",
                    self.converter.dwg_to_dxf,
                    staged_output,
                    verification_dxf,
                    started="正在读回译文图纸以便校验",
                    finished="译文图纸读回完成，用时 {0} 秒",
                    total=2,
                )
            if options.verify_roundtrip:
                self._check_stop()
                output_records = _parse_dxf(_read_dxf_text(verification_dxf))
                output_units = extract_cad_text_units(
                    output_records,
                    include_block_text=options.include_block_text,
                    locked_blocks=options.locked_blocks,
                    locked_layers=options.locked_layers,
                )
                if options.check_entity_counts and len(output_units) != len(units) + stats.bilingual_added:
                    raise CadPipelineError("写回后的图纸文字数量不一致，为安全起见未输出。")
            else:
                output_records = records
                output_units = extract_cad_text_units(
                    output_records,
                    include_block_text=options.include_block_text,
                    locked_blocks=options.locked_blocks,
                    locked_layers=options.locked_layers,
                )
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
            output_units = extract_cad_text_units(
                    output_records,
                    include_block_text=options.include_block_text,
                    locked_blocks=options.locked_blocks,
                    locked_layers=options.locked_layers,
                )
            # 双语图纸本来就留着原文，残留原文扫描对它没有意义。
            if options.scan_residual and not bilingual:
                stats.residual_foreign_text_count = sum(
                    1 for unit in output_units
                    if _is_residual_foreign_text(unit.plain_text, options.target_lang)
                )
            translated_targets = {str(value).strip() for value in written.values() if value}
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
            unresolved = [
                {"source": text, "reason": invalid_reasons.get(text, "未获得译文")}
                for text in texts
                if text not in translations and text not in kept_texts
            ]
            unit_reports: list[dict[str, str]] = []
            for unit in candidates:
                for run in unit.runs or (CadTextRun(0, len(unit.plain_text), unit.plain_text),):
                    source_text = run.text
                    if not source_text:
                        continue
                    target_text = translations.get(source_text, "")
                    if target_text:
                        unit_reports.append({"unit_id": unit.unit_id, "source": source_text, "translation": target_text, "status": "translated", "reason": ""})
                    elif source_text in kept_texts:
                        unit_reports.append({"unit_id": unit.unit_id, "source": source_text, "translation": "", "status": "kept", "reason": ""})
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
                "layout_overflow": layout_unresolved,
                "summary": str(summary_path),
                "unsupported_features": unsupported_features,
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            report_path.write_text(json.dumps({**manifest, "unresolved": unresolved}, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_lines = [
                f"# CAD 翻译摘要：{source.name}", "",
                f"- 输出：`{output.name}`", f"- 已翻译文字：{stats.changed} 处",
                f"- 未完成：{len(unresolved)} 处", f"- 译文中仍有原文：{stats.residual_foreign_text_count} 处",
                f"- 缩到最小比例仍会压到旁边文字：{stats.layout_unresolved} 处", "",
                "| 编号 | 原文 | 译文 | 状态 | 原因 |", "|---|---|---|---|---|",
            ]
            for item in unit_reports:
                row = {**item, "status": _UNIT_STATUS_LABELS.get(str(item.get("status") or ""), item.get("status", ""))}
                summary_lines.append("| {unit_id} | {source} | {translation} | {status} | {reason} |".format(**{key: str(value).replace("|", "\\|") for key, value in row.items()}))
            summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
            # Publish only after the output and all review artifacts are ready.
            if output.exists():
                raise CadPipelineError("处理期间输出位置出现同名文件，为避免覆盖，未写入译文图纸。")
            shutil.copy2(staged_output, output)
            _discard_evidence(evidence_dir)
            return CadTranslationResult(source, output, persisted_work_dxf if options.keep_work_dxf else translated_dxf, manifest_path, report_path, stats, unresolved, timings, summary_path)


def _discard_evidence(evidence_dir: Path) -> None:
    """Drop the evidence DXF copies of a drawing that passed verification.

    ``source.dxf`` / ``translated.dxf`` exist for troubleshooting a drawing
    that failed (they stay when any step raises).  After a pass they only
    double the disk use of a large ASCII DXF; nothing reads them later — the
    manifest, report and summary are kept, and so is the optional work DXF
    (``keep_work_dxf``).  An evidence folder left empty is removed.
    """
    for name in ("source.dxf", "translated.dxf"):
        try:
            (evidence_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"[CAD] 无法删除校验用的图纸副本 {name}：{exc!r}")
    try:
        evidence_dir.rmdir()
    except OSError:
        pass  # not empty (or already gone): leave it


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

    @staticmethod
    def _output_complete(path: Path, output_type: str) -> bool:
        """DXF 必须以 ``0 / EOF`` 记录收尾；其他格式只靠大小稳定判断。"""
        if output_type.upper() != "DXF":
            return True
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 64))
                tail = stream.read()
        except OSError:
            return False
        return _DXF_EOF_RE.search(tail) is not None

    def _run_macos_app(
        self, app_bundle: Path, args: list[str], output_dir: Path, stem: str, output_type: str, timeout: int
    ) -> Path:
        """Launch ODA through LaunchServices and wait until it has exited.

        ODA's macOS build is a Cocoa application.  Invoking the binary inside
        ``Contents/MacOS`` directly can fail before conversion with a
        PasteBoard ``Connection Invalid`` error, even in an interactive user
        session.  LaunchServices supplies the application session correctly;
        ``-n`` forces a fresh instance so command-line arguments are honored;
        ``-g`` keeps that helper window out of the user's foreground; ``-W``
        makes ``open`` return only when that instance quits, i.e. when the
        conversion is finished.
        """
        launcher = shutil.which("open") or "/usr/bin/open"
        command = [launcher, "-n", "-g", "-W", "-a", str(app_bundle), "--args", *args]
        try:
            subprocess.run(
                command,
                check=True,
                timeout=timeout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            # 卡住的那个 ODA 实例不会随 open 一起退出；它的参数里带着本次独有的
            # 临时目录，按它结束，不碰用户自己开着的其他实例。
            subprocess.run(["pkill", "-f", args[0]], check=False, capture_output=True)
            raise CadPipelineError(f"图纸转换超时（{timeout} 秒）。") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()[-2000:]
            logger.warning(f"[CAD] 图纸转换工具启动失败：{detail}")
            raise CadPipelineError("图纸转换工具未能启动，请在「插件」中重新连接后重试。") from exc

        # 进程已经退出，文件就是最终结果；再核对一次：没有文件是没转出来，DXF
        # 缺结尾的 EOF 记录是 ODA 中途崩了，都不能当成图纸本身损坏去解析。
        produced = self._produced_file(output_dir, stem, output_type)
        if produced is None:
            raise CadPipelineError("图纸转换工具未生成文件。")
        if not self._output_complete(produced, output_type):
            raise CadPipelineError("图纸转换工具中途退出，这张图纸没有转换完整。")
        return produced

    def _timeout_for(self, source: Path) -> int:
        """总超时只用来防卡死：在基础时长上按图纸大小每 MB 再加 30 秒。"""
        try:
            size_mb = source.stat().st_size // (1024 * 1024)
        except OSError:
            size_mb = 0
        return self.timeout + 30 * size_mb

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
            timeout = self._timeout_for(source)
            app_bundle = self._macos_app_bundle()
            if app_bundle is not None:
                produced = self._run_macos_app(app_bundle, command[1:], out_dir, source.stem, output_type, timeout)
            else:
                try:
                    subprocess.run(
                        command, check=True, timeout=timeout, cwd=str(temp), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise CadPipelineError(f"图纸转换超时（{timeout} 秒）。") from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or "").strip()[-2000:]
                    logger.warning(f"[CAD] 图纸转换失败：{detail}")
                    raise CadPipelineError("图纸转换工具未能转换此图纸，图纸可能已损坏或版本不受支持。") from exc
                produced = self._produced_file(out_dir, source.stem, output_type)
                if produced is None:
                    raise CadPipelineError("图纸转换工具未生成文件。")
            shutil.copy2(produced, destination)
        return destination

    def dwg_to_dxf(self, source: Path, destination: Path) -> Path:
        return self._run(source, destination, "DXF")

    def dxf_to_dwg(self, source: Path, destination: Path) -> Path:
        return self._run(source, destination, "DWG")
