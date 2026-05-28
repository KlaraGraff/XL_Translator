# Translation Task Queue Design Notes

This note preserves decisions agreed during the task-queue design discussion, including the UI direction, functional behavior, and implementation direction.

## Agreed UI Direction

- Keep the existing main translation workspace content unchanged.
- Do not show the translation task list by default.
- Add queue-related actions only inside the right-side execution actions card.
- Show the translation-list entry only when the current translation list is not empty. When there are no running, queued, completed, failed, stopped, or canceled tasks in the current translation-list lifecycle, the entry does not exist.
- Open the translation list by replacing the main workspace's task-list/list area, not by overlaying a separate right-side drawer.
- Use the same action-card entry to open and close the translation list. When closed, show `查看翻译列表，当前 x/x` if there are running or queued tasks, and show `查看翻译列表` if the current list only has history. When open, show `关闭翻译列表`.
- Place `关闭翻译列表` in the right-side execution/actions area above the selected-task card, not beside the source-path browse/scan controls.
- In the translation-list panel, group tasks by status: running, queued, and history; show Excel, Word, or PDF as a task type label on each item.
- Historical tasks are completed, failed, stopped, or canceled tasks from the current translation-list lifecycle, not tasks from a previous app session or previous translation list.
- Keep at most the latest 20 historical tasks in the first version.
- Allow users to clear historical tasks in the current translation-list lifecycle without affecting running or queued tasks.
- Queue task actions: queued tasks show move up, move down, cancel, and view details; running tasks show view details and terminate; historical tasks show view details and open output location.
- Queue ordering uses move up and move down controls in the first version, not drag-and-drop.
- Selecting a translation-list task switches the left sidebar and right-side execution/settings area into read-only task snapshot mode. This replaces a separate details page.
- The left sidebar snapshot shows the selected task's domain, prompt, model-role, provider, model, and masked API Key. It must be visually marked as a read-only task snapshot so users do not mistake it for editable global settings.
- The right-side snapshot shows the selected task's actions, progress or result summary, output location, and task parameters.
- API Key fingerprints use the common masked form with the leading and trailing characters visible, such as `sk-abcd...wxyz`.
- Ordinary failed tasks move to history. Model-level or API-group blocked tasks remain queued with a blocked state and can be canceled or inspected.
- Task details are collapsed by default and open only after the user selects a task.
- Use the button text `查看翻译列表，当前 x/x`.
- `x/x` means the current task position and the total active queue size.
- Finished task history does not count toward `当前 x/x`.
- Do not use a badge for queue count.
- Put `安排新任务` and `终止翻译` on the same row for a more compact action card.
- Continue UI discussion after functional behavior and implementation logic are clarified.

## Mockups

- `docs/mockups/task-queue-action-card-compact-no-note.png`
- `docs/mockups/task-queue-action-card-compact.svg`
- `docs/mockups/task-queue-side-panel-preview.png`
- `docs/mockups/task-queue-center-list-snapshot-preview-v2.png`

## Agreed Functional Behavior

- API concurrency group identity is based on cloud mode plus the normalized effective Base URL and API Key. Service provider is used only to derive a default official endpoint when needed; model name, model role, and translation type do not split the group.
- API Keys must not be stored or displayed in plaintext as queue/group identifiers; use a non-reversible key hash or equivalent internal fingerprint.
- Arranged tasks capture a complete configuration snapshot at arrangement time.
- Queued tasks do not support in-place editing; users cancel and re-arrange with current page settings when changes are needed.
- Queued tasks that have not started can be canceled. Running tasks use the existing stop/terminate behavior.
- When a page already has a running task, scanning and file selection can be used to prepare the next task without changing the running task.
- The same translation type can have multiple queued tasks, but only one running task per translation type is allowed in the first version. This avoids progress, stop, log, and result-state collisions in the current page-owned runner model.
- Queued tasks in the same API concurrency group can be moved up or down, but cannot move ahead of already running work.
- The `当前 x/x` indicator is scoped to the active translation type page and counts that page's running plus queued tasks.
- The translation list keeps completed, failed, stopped, and canceled task history for review.
- The first version does not persist the running queue or translation-list history across app restarts.
- Local model translation tasks do not enter the translation queue in the first version.
- Task arrangement performs all existing preflight validation and user confirmations, including selected files, language choices, output settings, model configuration, and PDF image/review model availability prompts. A queued task should not display blocking prompts when it later starts automatically.
- A task configuration snapshot includes selected files, source root, language choices, output options, effective model access, relevant concurrency settings, and API concurrency-group requirements.
- Queued tasks use their arrangement-time configuration snapshot even if the user later changes API Key, Base URL, model settings, language choices, file selections, output settings, or concurrency.
- The main page does not restore file selections or parameters to a running task's original values after that task ends. The main page reflects the current setup for the next arranged task; running and historical task configuration is inspected from the translation list.
- Output directories are created when the task starts, not when the task is arranged, to avoid empty or stale output folders for queued work.
- API concurrency-group capacity is recalculated when tasks are arranged, started, completed, failed, stopped, or canceled. Capacity is the maximum declared concurrency among running and queued tasks in the group; finished historical tasks do not affect capacity.
- Canceling a queued task immediately removes it from active capacity and `当前 x/x` calculations. The canceled entry may remain in translation-list history.
- When the first queued task cannot start because resources are temporarily unavailable or the same translation type is already running, the scheduler may skip it and evaluate later tasks. When a task is blocked by model-level or credential-level unavailability, later tasks requiring the same API concurrency group remain blocked too.
- A queued task may start once each required API concurrency group can provide at least one slot.
- Queue startup is evaluated in order; start the first eligible queued task, then re-evaluate.
- A task that requires multiple API concurrency groups may start only when all required groups meet the minimum startup condition.
- User-stopping or ordinary task failure does not stop later queued tasks.
- A model-level or credential-level unavailable error blocks later queued tasks that require the same API concurrency group until the user resolves the configuration.
- Adaptive concurrency reductions are shared by all tasks in the same API concurrency group because they use the same group-level scheduler state. Different API concurrency groups keep independent scheduler state.
- PDF image generation and PDF review requests share the same API concurrency group when their effective endpoint and API Key match.
- PDF image generation and PDF review use separate API concurrency groups when their effective endpoint and API Key differ.

## Agreed Implementation Direction

- Add a cross-page task queue module such as `core/task_queue.py` to own arranged tasks, API concurrency groups, group capacity, and startup decisions.
- Keep `core/api_scheduler.py` focused on weighted slot scheduling inside one API concurrency group.
- The main window owns a shared translation task queue instance and connects Excel, Word, and PDF pages to it.
- Inject shared scheduling or scheduler-provider hooks into existing Excel, Word, and PDF runners instead of moving their business logic into the queue.
- Queue state changes and runner startup decisions are coordinated on the UI main thread; existing runner work remains on the current background worker threads.
- UI details for queue inspection, ordering controls, and scheduler state are deferred to the final UI design stage and should live behind the `查看翻译列表` entry rather than on the main workspace.
