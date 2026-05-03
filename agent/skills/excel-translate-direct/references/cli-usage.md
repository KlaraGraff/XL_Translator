# Headless CLI Usage

The repository exposes a no-UI command entrypoint at:

```bash
./.venv/bin/python3 scripts/translate_excel_cli.py
```

Run it from the project root:

```bash
./.venv/bin/python3 scripts/translate_excel_cli.py "/absolute/path/to/file.xlsx" --target-lang "英文" --quiet --json
```

Useful options:

- `--target-lang`: accepts either a language code like `en` or a display name like `英文`.
- `--source-lang`: accepts either a language code like `zh` or a display name like `中文`.
- `--output-dir`: writes the timestamped output folder under a custom root instead of next to the source file.
- `--allow-xls-fallback`: allows pure-code fallback when `.xls` files need conversion and local Excel automation is unavailable.
- `--engine-mode`, `--cloud-provider`, `--cloud-model`, `--ollama-model`, `--batch-size`: one-off runtime overrides.
- `--quiet --json`: best for Codex, because the final stdout is machine-readable JSON.

Behavior notes:

- The command reuses the existing project logic in `TaskRunner`; it is not a second implementation.
- API keys, TM database, and saved settings still live under `~/.xl_translator/` unless the caller isolates `HOME`.
- When `--output-dir` is omitted, the command writes to the default timestamped output folder next to the source path.
