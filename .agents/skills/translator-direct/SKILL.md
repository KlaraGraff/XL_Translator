---
name: translator-direct
description: Route-level direct document translation for Translator without opening or installing the desktop app. Use when Codex should translate Excel (.xlsx/.xls), Word (.docx/.doc), PDF, or supported image files/folders by path, auto-select the correct headless route, preflight missing model/API configuration, and return generated output files.
---

# Translator Direct

## Overview

Use this skill to run the repository's headless Translator pipelines by path instead of launching the PySide desktop app. It routes Excel, Word, and PDF/image-layout work to the existing CLI entrypoints and reports output folders, generated files, and review issues.

## Workflow

1. Resolve the source path and target language from the user request.
2. Run a dry-run preflight first:

```bash
python .agents/skills/translator-direct/scripts/translator_router.py "/absolute/source/path" --target-lang "英文" --dry-run --json
```

3. If `missing_requirements` is non-empty, ask only for the missing item. Offer the listed options; prefer current-agent API configuration when available.
4. Run the same router without `--dry-run` once preflight is ready.
5. Report each route's output directory, generated files, report path, and failures from the JSON result.

## Defaults

- Source language defaults to `auto`; the router samples Excel/Word content when possible and omits `--source-lang` when it cannot infer a source.
- Text concurrency defaults to `10`.
- PDF image page concurrency defaults to `3`.
- PDF review is off unless the user asks for review/repair.
- Output directory defaults to the repository CLI's timestamped output package beside the source path unless the user gives a custom destination.
- Current-agent API configuration means process-visible local configuration or environment variables, not hidden platform secrets.

## Commands

Check the local runtime:

```bash
python .agents/skills/translator-direct/scripts/bootstrap.py --json
```

Translate a file or folder:

```bash
python .agents/skills/translator-direct/scripts/translator_router.py "/absolute/source/path" --target-lang "法文" --json
```

Useful one-off overrides:

```bash
--output-dir "/absolute/output/root"
--text-concurrency 10
--pdf-page-concurrency 3
--engine-mode local --ollama-model qwen2.5:14b
--cloud-provider openai --cloud-model gpt-5.1
--image-provider openai --image-model gpt-image-2
--include-images
--pdf-review
```

## Configuration

Read `references/configuration.md` when preflight reports missing credentials, missing model names, unsupported image providers, or when the user asks how to configure keys/concurrency.

Do not require all keys during installation. Ask for text-model credentials only for Excel/Word routes; ask for image-generation credentials only for PDF layout translation; ask for vision-text review credentials only when PDF review is enabled.

If a route fails, return the router JSON summary and the route stderr/stdout detail to the user in concise form.
