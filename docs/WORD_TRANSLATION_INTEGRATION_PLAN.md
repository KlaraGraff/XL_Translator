# Word Translation Integration Plan

This project is moving from an Excel-only translator to a unified translation
workspace. The guiding principle is to share language, engine, prompt, API key,
translation memory, progress, and output settings while keeping each document
format behind its own small adapter.

## Product Goal

- Keep the existing Excel translation flow working as-is.
- Add a Word translation flow for `.docx` files, with legacy `.doc`
  compatibility through conversion to `.docx`.
- Let users switch between table translation, Word translation, and TM
  management from one sidebar.
- Reuse the existing local settings and key storage. API keys must stay in
  the platform-native app data directory as `keys.json` and must never be
  committed.
- Reuse the existing SQLite translation memory so Excel and Word translations
  reinforce the same terminology choices.

## MVP Boundary

- Supported Word input: `.docx` directly and `.doc` through a conversion step.
- `.doc` conversion prefers local Microsoft Word when enabled. If Word is not
  available or conversion fails, the app falls back quietly to compatible
  converters such as LibreOffice/soffice or macOS textutil when present.
- Unsupported for now: WPS-native formats, password-protected files,
  native Word macro preprocessing, and automatic TOC refresh.
- Macro-enabled legacy `.doc` files may be converted to ordinary `.docx`; macros
  are not preserved, executed, or written to the output.
- Output defaults to bilingual Word files.
- The source file is never modified. A timestamped output directory is created
  with copied results.
- Body paragraphs use paragraph-pair bilingual layout: source paragraph followed
  by translated paragraph.
- Table cells use the Hermes-derived rule: original source block, real newline,
  translated block.
- TOC-style paragraphs and Word field paragraphs are skipped conservatively.

## Shared Components

- `settings.AppSettings`: engine, target/source language, output root, prompts.
- `core.engine_dispatcher`: engine construction and shared prompt building.
- `core.tm_manager`: TM lookup and insertion.
- `core.translation_filter`: decide whether text needs translation.
- `core.translation_protocol`: avoid storing replace/control results in TM.
- `core.task_runner` message dataclasses: UI progress/status/log/done messages.
- `core.bilingual_writer.build_output_dir`: timestamped output folder naming.

## Word-Specific Components

- `core.word_converter`: convert legacy `.doc` to temporary `.docx` using local
  Word, LibreOffice/soffice, or macOS textutil fallback.
- `core.word_document`: scan `.docx` / `.doc`, extract translatable segments, write
  bilingual `.docx`, and perform light structural QA.
- `core.word_batching`: Word-only character-budget batching, long-paragraph
  splitting, batch integrity checks, and shrinking retry fallback.
- `core.word_task_runner`: Word pipeline mirroring the Excel runner:
  scan/extract, TM/API translate, write output.
- `core.headless_word_translate`: CLI-friendly runner wrapper.
- Historical V7 note: the PySide6 page described here was retired in V8; the
  maintained Word surface is the Tauri UI in `ui/`.

## Hermes Word Notes Applied

- Work on a copy only.
- Do not translate TOC directly.
- Tables must append translation after the original block with a real newline.
- Multi-line table cells keep all original source lines before the translated
  block.
- Prefer native Microsoft Word for a future high-fidelity route, especially for
  numbering flattening, TOC updates, and page-rendered QA.
- For this product MVP, use `python-docx` for a testable `.docx` draft route and
  keep the native Word route as a later enhancement.

## Validation Checklist

- Existing Excel page still renders and imports.
- Word page scans a single `.docx` / `.doc` and a folder containing supported
  Word files.
- Generated Word table cells contain actual newline-separated source and target
  text.
- Word body paragraphs insert translated paragraphs after source paragraphs.
- Shared TM lookup/insert path is used by both Excel and Word.
- Word translation uses its own paragraph and character-budget batching instead
  of the Excel cell batch size.
- Word batches retry automatically when an API response cannot be mapped back to
  every requested paragraph or split part.
- Sensitive scan finds no API keys or tokens in committed files.
- `quality_gate.ps1` and at least one Word-specific dynamic test run before
  delivery.
