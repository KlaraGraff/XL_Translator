---
name: translator-direct
description: Translate Excel, Word, PDF/image or CAD files and folders by path with Translator's command line, without opening the desktop app. Use when asked to translate documents by path, to check what a folder would translate, or to run a translation smoke test, and report the output files.
---

# Translator Direct

Translator's command line drives the same engine as the desktop app. When the
app is open, tasks go into its task center; when it is closed, the command
starts the engine in the background and stops it when done (the output is the
same either way). File types, options and model come from the app; the command
has no model flags.

## Entry

- In this repository: `./.venv/bin/python3 -m cli <command> …`
- Installed app (macOS): `/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar <command> …`

## Commands

```bash
./.venv/bin/python3 -m cli types                       # file types and their --option names
./.venv/bin/python3 -m cli scan "/abs/folder"          # which files would be translated
./.venv/bin/python3 -m cli translate "/abs/file-or-folder" --to fr --dry-run   # check only, JSON
./.venv/bin/python3 -m cli translate "/abs/file-or-folder" --to fr --wait --json
./.venv/bin/python3 -m cli status [task_id] --json
./.venv/bin/python3 -m cli stop <task_id>
```

- The type follows the file extension (a folder: the type with most files); `--type` overrides.
- Options not given are the app's saved ones; `--option name=value` sets one (repeatable).
- Output goes beside the source, in a timestamped `…_翻译输出_…` folder; `--json` returns the folder, each file's output path and the review count.
- Exit codes: 0 done (some files may have failed), 1 nothing came out, 2 bad arguments or refused input, 3 engine unavailable, 4 needs confirmation (read the message; `--yes` continues).

## Workflow

1. Run `translate … --dry-run` first. `ready: true` means it can start; `problem` names what is missing (e.g. no model configured: ask the user to set it in the app); `confirmation` describes what `--yes` would accept. Ask the user before adding `--yes`.
2. Run `translate … --wait --json` and report `result.output_dir`, the generated files, `review_count` and any failed files.

## Test model

Never edit the user's own `settings.json` or `keys.json`. For tests with a cheap model, prepare a throwaway data directory and point the command at it:

```bash
TRANSLATOR_TEST_API_KEY=sk-… ./.venv/bin/python3 agent/testing/seed_test_model.py \
  --target .runtime/self-tests/<slug>/app-data --base-url https://…/v1 --model <model> --concurrency 4
TRANSLATOR_APP_DATA_DIR="$PWD/.runtime/self-tests/<slug>/app-data" ./.venv/bin/python3 -m cli translate … --wait --json
```

Copy test sources into `.runtime/self-tests/<slug>/` first, since outputs are written beside them.
