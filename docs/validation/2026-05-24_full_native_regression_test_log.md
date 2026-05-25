# 2026-05-24 Full Native Regression Test Log

Historical note: this file records the validation state during the migration.
V5.0 and later do not retain or require Streamlit wrapper smoke tests; current
maintenance uses PySide6 native/offscreen tests plus the standard quality gate.

Scope: native PySide6 UI refactor, core translation utilities, TM management, Word/Excel task pages, abnormal interaction paths.

## Running Notes

- All dynamic tests must use isolated app data where possible.
- `PASS` means the checked behavior matched expectations.
- `FAIL` means a bug was found and must be fixed before final handoff.
- `BLOCKED` means the environment cannot exercise the path directly.

## Step Results

| Step | Area | Check | Result | Notes |
| --- | --- | --- | --- | --- |
| 1 | Inventory | Located available checks: `quality_gate.ps1`, pytest tests, native UI smoke under `.runtime/self-tests/native-ui-refactor/check_native_pages.py`. | PASS | Baseline test entry points identified. |
| 2 | Static | Ran `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`. | PASS | Ruff quality gate passed. |
| 3 | Unit entry | Tried `./.venv/bin/python3 -m pytest tests`. | BLOCKED | `.venv` has no `pytest`; tests are `unittest` files, so switch to `python -m unittest discover -s tests`. |
| 4 | Unit | Ran `./.venv/bin/python3 -m unittest discover -s tests`. | PASS | 75 tests passed. Existing core coverage includes app paths, model catalog, connectivity, API health/config/concurrency, data migration, dispatcher, Word document/batching, diagnostics, update checker. |
| 5 | Native abnormal UI | First draft of `full-native-regression` script clicked a disabled Start button expecting a warning. | TEST SCRIPT FIXED | Qt correctly ignores clicks on disabled buttons; script was changed to call the start handler directly for validation. |
| 6 | Native abnormal UI | First draft manually set `phase="running"` without calling the real input-lock path. | TEST SCRIPT FIXED | Real start flow calls `_lock_inputs(True)`; script now verifies the real lock helper instead of a synthetic partial state. |
| 7 | Native abnormal UI | Ran `Run-IsolatedVenvPython.ps1 -TaskSlug full-native-regression -ScriptPath .runtime/self-tests/full-native-regression/check_native_full_behaviour.py`. | PASS | Covered sidebar settings/model fetch/connectivity/update with stubs; Excel/Word normal scan and invalid scan paths; output-dir validation; browse cancel; TM layout, search, pin, add, export/import, delete cancel/confirm; running-state lock guards. |
| 8 | Native smoke | Ran existing `native-ui-refactor/check_native_pages.py`. | PASS | Existing smoke still passes after the broader abnormal-interaction script. |
| 9 | Streamlit legacy wrappers | Ran `streamlit-page-smoke/check_streamlit_pages.py`. | PASS | Rendered translate, Word translate, TM, and full app wrappers without exceptions. |
| 10 | CLI | Ran `./.venv/bin/python3 scripts/translate_excel_cli.py --help`. | PASS | Help text renders and exits 0. |
| 11 | CLI | Ran `./.venv/bin/python3 scripts/translate_word_cli.py --help`. | PASS | Help text renders and exits 0. |
| 12 | GUI launcher | Tried `./.venv/bin/python3 scripts/launch_native.py --help`. | BLOCKED | This script is a GUI launcher, not an argparse CLI; in the headless test shell it attempts Qt/macOS services and exits with pasteboard/HiServices errors. Native app functionality is covered by offscreen native UI scripts above. |
| 13 | Final regression | Re-ran `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1`. | PASS | Product quality gate passed. |
| 14 | Final regression | Re-ran `./.venv/bin/python3 -m unittest discover -s tests`. | PASS | 75 tests passed. |
| 15 | Test script lint | Ran ruff on the two new `.runtime/self-tests` scripts. | PASS | Added explicit E402 allowance to the Streamlit smoke script because it must set isolated app data before importing Streamlit. |
| 16 | Final regression | Re-ran existing native smoke. | PASS | `native-ui-refactor smoke passed`. |
| 17 | Final regression | Re-ran full native abnormal-interaction script. | PASS | All scripted normal and abnormal UI paths passed. |
| 18 | Final regression | Re-ran Streamlit wrapper smoke. | PASS | All four wrappers rendered without exceptions. |

## Issues Found

No product defects were found in this cycle. Two early failures were defects in the newly written test script assumptions and were corrected before final regression.

## Fix Log

- Added `.runtime/self-tests/full-native-regression/check_native_full_behaviour.py` to cover native normal and abnormal UI interactions.
- Added `.runtime/self-tests/streamlit-page-smoke/check_streamlit_pages.py` to cover retained Streamlit wrapper render paths.
- Corrected test-script assumptions around disabled Qt button clicks and synthetic running-state setup.
- Added explicit `# ruff: noqa: E402` to the Streamlit smoke script because environment isolation must happen before importing Streamlit.

## Final Status

PASS with one environment-only blocker: `scripts/launch_native.py --help` is not a supported CLI help path and cannot be exercised in the headless shell as a GUI launcher. Native app behavior was covered via offscreen PySide6 tests.
