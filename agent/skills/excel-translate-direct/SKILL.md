---
name: excel-translate-direct
description: Use this skill when the user wants Codex to run the local Product_TranslateForExcel workbook translator directly on an Excel file or folder path, without launching the Streamlit app, and return the translated output files.
---

# Excel Translate Direct

## Overview

Use this skill when the user asks for path-driven Excel translation with the existing local translator project, for example “把这个 Excel 翻译成英文” or “直接用表格翻译器处理这个目录”.

This skill does not reimplement translation. It calls the repository's headless CLI so Codex can translate files by path and then report the output folder and generated files back to the user.

## Workflow

1. Confirm the current workspace is the `Product_TranslateForExcel` repository, or that the CLI exists at:
   `/Users/lijianwei/Workspace/1001 Creativity/001 Translate for excel/Translator_excel_mac/Product_TranslateForExcel/scripts/translate_excel_cli.py`
2. Extract the source path, target language, and optional output directory from the user request.
3. Run the headless CLI from the repository root with `./.venv/bin/python3`.
4. Prefer `--quiet --json` so the final stdout is easy to parse.
5. After completion, tell the user where the output directory is and list the generated translated file paths.

## Command Pattern

Run this command from:
`/Users/lijianwei/Workspace/1001 Creativity/001 Translate for excel/Translator_excel_mac/Product_TranslateForExcel`

```bash
./.venv/bin/python3 scripts/translate_excel_cli.py "/absolute/path/to/input.xlsx" --target-lang "英文" --quiet --json
```

If the user provides a folder path instead of a single file, pass the folder path directly and let the CLI scan it recursively.

When the user asks for a custom destination, add:

```bash
--output-dir "/absolute/path/to/output-root"
```

When legacy `.xls` files are present and the user agrees to compatibility fallback, add:

```bash
--allow-xls-fallback
```

## Runtime Notes

- The CLI reuses the repository's existing `TaskRunner`, TM lookup, and bilingual writer logic.
- API keys and TM data come from Translator's platform-native app data directory unless the caller intentionally isolates it with `TRANSLATOR_APP_DATA_DIR`.
- If the user does not specify a target language, you may omit `--target-lang` and let the repository's saved settings decide.
- If the user asks to change model behavior for one run, use the CLI overrides like `--engine-mode`, `--cloud-provider`, `--cloud-model`, or `--ollama-model`.

## Reporting Back

- Always report the output directory.
- If JSON output includes `successful_outputs`, list those exact files in the reply.
- If some files fail, say which ones failed and include the error summary from `file_results`.

## Reference

See [references/cli-usage.md](references/cli-usage.md) for concrete CLI options and behavior notes.
