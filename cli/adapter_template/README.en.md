# Translator extension template

[中文](README.md)

Use this template to teach [Translator](https://github.com/KlaraGraff/XL_Translator) one more file type. An extension is a folder: `adapter.json` names the type and the file extensions it handles, and the functions in `adapter.py` take the text out of a file and write the translations back. Translator does the rest: model calls, the translation memory, retries, review and progress.

The template is a working extension in its own right: it translates "key = value" `.myfmt` files, with two samples in `samples/`. Change it into your format.

## Step 1: create the project

On GitHub, click "Use this template" to create your own repository, then clone it. Without GitHub, run `translator-sidecar adapter new <id>` to create the same files in an `<id>` folder under the current folder.

`translator-sidecar` is the command line that ships with Translator:

- macOS: `/Applications/Translator.app/Contents/Resources/sidecar/translator-sidecar/translator-sidecar`
- Windows: `C:\Users\<user name>\AppData\Local\Translator\sidecar\translator-sidecar\translator-sidecar.exe`

## Step 2: make it your format

1. Edit `adapter.json`:
   - `id`: lower-case letters, digits, `-` and `_`, at most 40 characters; do not change it once published.
   - `label`, `file_noun`, `description`: the type's name, what its files are called, and the text under the page's "?", each in Chinese, English and French.
   - `extensions`: the file extensions it handles.
   - `prompt`: the built-in rules sent to the model; optional, and users can rewrite it under Settings → Translation options.
   - `capabilities`: which outputs it supports, translation only and/or source + translation. `options`: switches shown on the page; optional.
   - `version`, `license`, `repo` (the project's home page).
2. Rewrite `extract` and `write` in `adapter.py`, and the optional `scan`. Each parameter is explained in the function's docstring.
3. Put two or three small samples in `samples/`; the self-test and the extension list's checks both use them.

An extension may use only the Python standard library and `translator_adapter`. It may not go online, start other programs, load native libraries or execute code dynamically. The package may contain only `.py`, `.json`, `.md` and `.txt` files and `LICENSE` (samples can be anything), 2 MB in total. Translator checks these rules before installing, but the checks only stop obvious overreach and are not a sandbox: an extension runs with the user's permissions, which is why users are told to install only from sources they trust.

## Step 3: test and try it

```
translator-sidecar adapter test .
```

This checks the manifest and the code, then runs extraction and writing on `samples/` in a separate process: written back with the source as the translation, the file must give the same text when extracted again. It exits with 0 when everything passes.

Then, on Translator's Extensions page, click "Add from folder…" and choose this folder (or run `translator-sidecar adapter install . --yes`). Once the new type appears in the sidebar, translate a real file to see the result. After changing the code, add it again to update it.

## Share it

1. Set `version` in `adapter.json` to the new version number, then commit and push.
2. Tag and push the tag; it must match `version`: `git tag v0.1.0`, `git push origin v0.1.0`.
3. The repository's Release workflow builds `<id>-<version>.zip`, attaches it to the tag's release, and prints the extension list entry in the log of its "Registry entry" step.
4. Open a pull request on [XL_Translator-extensions](https://github.com/KlaraGraff/XL_Translator-extensions) that adds `extensions/<id>.json` with that entry. Once the checks pass and the code has been reviewed, it is merged and every user can find and install it on the Extensions page.

You can also send the zip to someone directly, without the extension list: `python scripts/pack.py` (or `translator-sidecar adapter pack .`) builds it in `dist/`, and they choose it under "Add extension…" on the Extensions page.

## Working with a coding assistant

Coding assistants such as Claude Code and Codex read [AGENTS.md](AGENTS.md), which describes the protocol, the limits and how to test.

## License

The template is offered under the MIT license; replace the year and name in `LICENSE` with yours. Your extension may use another license; name it under `license` in `adapter.json`.
