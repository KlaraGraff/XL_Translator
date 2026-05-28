# 2026-05-28 Queue UI Regression Fix Log

This log records queue-related UI defects found by the scripted click test and the repair status for each item. It is kept current while fixes are made so the investigation state survives context switches.

## Canonical Terms

- `翻译列表`: the user-facing list of running, queued, and current-lifecycle historical tasks.
- `翻译任务排队`: a pending task waiting for queue startup conditions.
- `新任务安排态`: the temporary state used to scan files and choose parameters for another task while a same-type task is already running.

## Active Findings

| ID | Surface | Reproduction | Current finding | Status |
| --- | --- | --- | --- | --- |
| QREG-001 | Excel | Start Excel task, click `安排新任务`, scan, click `开始翻译`, then try to arrange another task. | The original mismatch was a test-harness false positive: the fake runner returned `needs_poll() == False` while it was supposed to represent a still-running task, so the page cleared its local runner. After aligning the fake runner with the real runner contract, arranging a queued Excel task returns to `执行监控` and keeps `安排新任务` visible. | Verified pass |
| QREG-002 | Excel | Open `翻译列表`, cancel queued items, clear history, click `关闭翻译列表`. | Same fake-runner source as QREG-001. After the harness fix, closing the list while an Excel task is running returns to `执行监控`; `终止翻译` remains visible. | Verified pass |
| QREG-003 | Word | Start Word task, arrange a queued task, open list, cancel queued item, clear history, click `关闭翻译列表`. | Same fake-runner source as QREG-001. After the harness fix, closing the list while a Word task is running returns to `执行监控`; `终止翻译` remains visible. | Verified pass |
| QREG-004 | Excel/Word | Follow QREG-002 or QREG-003, then try to stop the running task or return from stopped result. | Downstream symptom of QREG-002/QREG-003. After the harness fix, `终止翻译` and `返回并开始新任务` are reachable and operate correctly in both Excel and Word flows. | Verified pass |
| QREG-005 | Excel/Word/PDF | Keep a task running, switch the workspace from `执行监控` to `翻译列表`, then let the poll timer refresh progress. | Running widgets such as `running_status`, `progress_bar`, `log_view`, and recovery summaries can be deleted when the workspace is re-rendered. The refresh code only checked whether attributes existed, not whether the underlying Qt C++ widget was still alive, causing `Internal C++ object already deleted`. | Fixed and verified |
| QREG-006 | Test environment | Queue UI tests import native Excel-related modules on macOS sandboxed shells. | xlwings registers a macOS atexit cleanup hook that scans processes; sandboxed process enumeration can emit `PermissionError` after successful tests. This is test-environment noise, not product behavior. | Fixed and verified in tests |

## Fix Log

- Updated `.runtime/self-tests/queue-ui-regression/run_queue_ui_test.py` so `FakeRunner.needs_poll()` remains true while the fake task is alive and becomes false only after a terminal `DoneMsg` or `StoppedMsg` is consumed. This matches the real `TaskRunner` contract, where a task remains pollable while its worker thread is alive.
- The queue UI regression script now clears old screenshots before each run, so generated reports cannot accidentally include stale failure images from a previous pass.
- Added `native_app.widgets.is_live_widget()` and updated Excel, Word, and PDF running/log/recovery refresh paths to skip stale Qt widget references after workspace re-rendering. This fixes QREG-005.
- Added a focused unit regression for Excel and Word: arranging a queued task while a same-type task is running must return to `执行监控`; opening the `翻译列表` and polling while it is open must not touch deleted running widgets; closing the list after canceling queued work must keep the running controls visible.
- Unregistered the xlwings macOS cleanup hook in the queue UI regression script and native translation page tests because those tests do not start Excel automation, and the hook can produce sandbox-only process enumeration errors at exit. This fixes QREG-006.
- Updated the Word report generator so its summary reflects the current run result instead of retaining the original failure wording after all checks pass.

## Validation Runs

- Initial scripted run before fixes: 41 recorded steps, 7 mismatches. Report: `.runtime/self-tests/queue-ui-regression/Translator_Queue_UI_Test_Report.docx`.
- Focused regression: `.venv/bin/python3 -m unittest -v tests.test_native_translation_pages.NativeTranslationPageTests.test_arranging_queued_task_restores_running_view_and_close_list_keeps_it` → passed.
- Related unit tests: `.venv/bin/python3 -m unittest tests.test_native_translation_pages tests.test_task_queue` → 47 tests passed.
- Quality gate: `powershell -ExecutionPolicy Bypass -File ./quality_gate.ps1` → passed.
- Final scripted click run after quality gate: `.venv/bin/python3 .runtime/self-tests/queue-ui-regression/run_queue_ui_test.py` → 41 recorded steps, 0 mismatches.
- Final report: `.runtime/self-tests/queue-ui-regression/Translator_Queue_UI_Test_Report.docx` with 41 screenshots and 41 step records.
- DOCX structural check: 41 embedded screenshots, 41 step tables, 132 paragraphs. LibreOffice render QA could not run because `soffice` is not installed at `/Applications/LibreOffice.app/Contents/MacOS/soffice` or on `PATH`.
