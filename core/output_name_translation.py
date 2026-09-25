"""Translate human-facing file and worksheet names with the active text engine."""

from __future__ import annotations

import re
from pathlib import Path

from core.language_registry import get_source_lang_display, get_target_lang_display


_ILLEGAL_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def translate_names(
    engine,
    names: list[str],
    target_lang: str,
    source_lang: str = "auto",
    *,
    kind: str = "名称",
) -> dict[str, str]:
    """Return safe, translated names; retain each original when translation fails.

    This call uses the already selected translation engine and does not write TM.
    A failed name request must not discard successfully translated document text.
    """
    # Keep exact keys for workbook titles and filenames.  Excel permits titles
    # with surrounding spaces; stripping here makes the rename plan refer to a
    # title that does not exist in the workbook.
    originals = list(dict.fromkeys(str(name) for name in names if str(name).strip()))
    if not originals:
        return {}
    request_names = list(dict.fromkeys(name.strip() for name in originals))
    target = get_target_lang_display(target_lang, include_optional=True)
    source = (
        "原文实际语言"
        if source_lang == "auto"
        else get_source_lang_display(source_lang)
    )
    prompt = (
        f"把每项{kind}从{source}翻译成{target}。输出简短、自然、可直接使用的目标语言名称。"
        "保留编号、日期、型号、专名和原有意义；不要附加原文、语言标签、说明或引号。"
        "文件名不得包含路径或扩展名；工作表名不得包含 Excel 禁用字符。"
    )
    try:
        translated = engine.translate_batch(
            request_names,
            target_lang,
            prompt,
            source_lang=source_lang,
        )
    except Exception:
        return {name: name for name in originals}
    result: dict[str, str] = {}
    for name in originals:
        raw_candidate = str(translated.get(name.strip()) or "").strip().strip('"“”‘’')
        candidate = _ILLEGAL_FILE_CHARS.sub("_", raw_candidate).rstrip(". ")
        # A model response containing a path or multiline explanation is not a name.
        if not candidate or "\n" in raw_candidate or len(candidate) > 180:
            candidate = name
        result[name] = candidate
    return result


def translate_output_stem(
    engine,
    original_stem: str,
    target_lang: str,
    source_lang: str = "auto",
) -> str:
    translated = translate_names(
        engine, [original_stem], target_lang, source_lang, kind="输出文件名"
    ).get(original_stem, original_stem)
    return output_stem_from_translation(original_stem, translated)


def output_stem_from_translation(original_stem: str, translated: str) -> str:
    """Apply the filename-only cleanup to a translated name from a shared batch."""
    if translated != original_stem:
        translated = re.sub(r"\.(?:xlsx|xls|xlsm|docx|doc|pdf)$", "", translated, flags=re.I)
    return translated or original_stem


def avoid_bilingual_name_collision(
    output_dir: Path, basename: str, target_lang: str
) -> str:
    """Keep two sources whose translated stems coincide from overwriting."""
    from core.bilingual_writer import bilingual_output_name

    source = Path(basename)
    extension = source.suffix.lower()
    if extension == ".xls":
        extension = ".xlsx"
    elif extension == ".doc":
        extension = ".docx"
    label = get_target_lang_display(target_lang, include_optional=True)
    candidate = source.stem
    index = 2
    while (output_dir / bilingual_output_name(candidate + extension, label)).exists():
        candidate = f"{source.stem}_{index}"
        index += 1
    return candidate + source.suffix
