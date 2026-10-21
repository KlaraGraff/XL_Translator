# Agent Instructions

This project enables "self-test after changes, before delivery" by default.

Read the entry points first:
- `agent/README.md`
- `agent/SELF_TESTING_PLAYBOOK.md`
- `agent/testing/README.md`

Hard rules:
1. Before delivering any code change, you must run `powershell -ExecutionPolicy Bypass -File .\quality_gate.ps1`.
2. After static checks pass, you must also run at least 1 dynamic test directly related to this change.
3. Always prefer `./.venv/bin/python3`; don't mix in the system Python. The first-time bootstrap may only be used to create `.venv`.
4. Tests involving `settings.json`, `keys.json`, the TM database, temp directories, or the user directory must be isolated first.
5. Dynamic test artifacts all go under `.runtime\self-tests\<task-slug>\`.
6. For Tauri/vanilla TypeScript UI changes, prefer running the `ui/` TypeScript build and the `src-tauri/` Rust check, and verify by launching the dev shell with isolated app data; add screenshots or DOM state assertions when needed.
7. If testing can't be completed, state explicitly before delivery what wasn't run, why it was blocked, and the scope of the risk.
8. Before tagging a release, you must run `bash scripts/run_release_env_tests.sh` (about 8 seconds when warm).
   Not required for everyday changes. The build machine runs Python 3.11 without Microsoft Office, and dev machines
   usually differ on both — an all-green `.venv` doesn't mean the build machine will pass: code that depends on object
   lifetimes (typically using `id()` as an identity) can behave in exactly opposite ways under different versions'
   memory allocators, and any test that actually launches Excel will silently pass on a local machine with Office installed.
   V9.1.0 tripped on each of these once before this was discovered.
9. After tagging, the workflow produces a **draft Release**, not a published one. Only after the body has been hand-written
   to replace the 「整理中」 ("being prepared") placeholder is it formally published (draft cleared). The order must never be reversed: a draft isn't publicly visible, so if writing the notes is interrupted halfway,
   nobody sees a half-finished version; if you publish first and fill in the body afterward, then in the meantime what hangs on Latest is a single
   line 「发布说明整理中」 ("release notes being prepared"), with no guarantee it ever gets filled in. V9.2.0 was exposed like this for 11 minutes.
   Don't stitch it together from `docs/CHANGELOG.md` — the changelog is written for developers, and copying it over produces
   a pile of jargon. How to write it:
   - The reader is an ordinary user. One bold heading per item, followed by one or two sentences of "how it was before, how it is now."
     Terms like connection pool, SSE, TM, anchor, DISPIMG are always replaced with plain language or dropped entirely.
   - Chinese first, English after, matched item by item.
   - End with downloads: one line per platform with the filename, plus a one-sentence install hint.
   - Signing and notarization status must be checked against the actual result of that build (whether the `Configure Developer
     ID` step in the macOS job was skipped or success) — never copied from the previous version. The old notes for V8.1.0 and V8.0.1
     said `formal-release`, but the actual builds were neither signed nor notarized.
10. Tagging and formal publishing both use `.github/workflows/release.yml` (workflow_dispatch);
   don't ask a local machine to push tags or run `gh release edit` on your behalf anymore. Cloud sessions can't push tags or edit
   Releases, but they can trigger this workflow; every step requires maple's confirmation in the conversation first.
   - `step=tag`: tags `v{version}` on main and triggers the formal build. It requires the PR checks for main's current commit
     to have passed (Python 3.11 + locked dependencies, no Office — the cloud equivalent of item 8);
     run the local `run_release_env_tests.sh` as well when possible.
   - `step=publish`: the body lives in `docs/release-notes/v{version}.md` and is merged into main first.
     The workflow first validates the body, then replaces the draft body, reads it back to compare, then clears the draft — fixed order;
     finally it confirms that the latest version installed apps see (`releases/latest`, `latest.json`) has switched over.
   - `step=prerelease`: same checks and body replacement as publish, but the release goes out marked Pre-release: visible and
     downloadable on the releases page, while `releases/latest`, `latest.json` and the README download link stay on the previous
     formal release (installed apps get no update prompt) and no feedback emails are sent. Use it to let specific users try a build.
     `step=promote` later turns it into the formal release (Latest, confirms both endpoints switched, then sends feedback emails);
     both need maple's confirmation, and promoting needs its own.
   - `step=notes`: revise the body after publishing; `step=retract`: revert to draft (no artifacts deleted, tag untouched),
     and confirm the latest version has fallen back to the previous formal release. Retracting also requires maple's confirmation.
   - `step=discard`: delete an unpublished draft and its tag so the same version number can be rebuilt; before deleting,
     preserve the original commit (an archive tag, or confirm it's already in main's history). A published release must be retracted first, likewise with
     maple's confirmation.
   - Step details are in the 「发版步骤」 ("Release steps") section of `docs/TAURI_DISTRIBUTION_WORKFLOW.md`.

Wrap-up for local Tauri UI testing:
- Whenever you've locally modified `ui/`, `src-tauri/`, the Tauri startup path, or code that affects UI state, after self-testing you must proactively shut down old `Translator` / `tauri dev` / `api.launcher` processes, then start a fresh process from the current source.
- On macOS, from the repo root run `cd src-tauri && ../ui/node_modules/.bin/tauri dev` with an isolated `TRANSLATOR_APP_DATA_DIR` set.
- After launch you must confirm the new process's PID, start time, launch path, and sidecar health check, to make sure it isn't the old `/Applications/Translator.app` install or a stale in-memory process.
- This step belongs only to the local test-delivery flow. Unless the user explicitly asks, don't commit, push, or upload to the cloud just because of recording or performing this local testing rule.

Legacy data compatibility rules (in effect since V9.2.1):
- Every schema bump must be able to read old data: additive changes are auto-filled and stamped with the version; breaking changes require an explicit migration + automatic backup before migrating.
- "Refuse to write and offer no way out" is never allowed under any circumstances. If it can't be read, back up the old file, create a usable new one, and tell the user in the UI where the backup is — never stop at an error and freeze.
- Before release, run an upgrade check against the previous version's real data directory.
- Scope: `settings.json`, `keys.json`, `tm.db`, task history, and any persisted data later written into `APP_DATA_DIR`.
- This rule supersedes the pre-V9.2.1 convention of "not distributed, so no legacy compatibility at all."

When reusing this rule set in a new project, prefer copying the root `AGENTS.md` and the entire `agent/` directory, then adapt `quality_gate.ps1` and the dynamic test scripts to the new project's actual structure.

GitHub sync rules:
- This project's remote repository: `https://github.com/KlaraGraff/XL_Translator`
- Default branch: `main`
- Don't create new GitHub repositories; unless the user explicitly asks, don't create unrelated branches either.
- If the current directory isn't a Git repository, remind the user to confirm the working directory first; don't initialize one somewhere else on your own.
- After finishing changes and passing self-tests, run in order: `git status`, `git add ...`, `git commit -m "<message>"`, `git push origin main`.
- Before pushing, confirm `git remote -v` points to `https://github.com/KlaraGraff/XL_Translator.git`.
