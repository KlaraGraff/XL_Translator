# ADR 0002: Rebuild V6.0 from the V5.0 Baseline

Date: 2026-05-29

## Status

Accepted

## Context

The V6.1 translation task queue changed the task lifecycle for Excel, Word, and PDF translation pages, including support for arranging additional tasks before the current task finishes. V6.4 removed the user-facing queue entry points, but queue-era state and page synchronization risks can still remain mixed into the current code.

The published V6.1 through V6.4 line is considered unstable because of queue-era behavior. Those versions can be retained as backup/reference artifacts, but they should no longer be promoted as downloadable releases.

## Decision

The next repair line will start from the V5.0 native desktop codebase and be released as V6.0. It will migrate forward the post-V5.0 feature upgrades that are not part of the translation task queue mechanism.

The rebuilt V6.0 line will delete the Hermes native engine path rather than carrying it forward from V5.0.

The rebuild branch should be created from the `v5.0` tag, using a name such as `rebuild/v6.0-from-v5.0`. `APP_VERSION` and package names should use `6.0`. Migration should be manual and selective rather than cherry-picking the V6.1 PDF-and-queue commit wholesale.

## Consequences

The rebuild prioritizes the stable single-current-task lifecycle from V5.0 over incremental bug fixing on the V6.4 branch. Post-V5.0 features must be reviewed and migrated deliberately, with queue-specific files, UI flows, task snapshots, and same-type or cross-type queued task behavior excluded from the rebuilt line.

Release documentation, package names, update checks, and release assets must treat V6.0 as the next valid public line after V5.0. GitHub release assets do not have an independent hidden state, so the V6.1 through V6.4 release pages should be converted to draft releases to keep their installer assets as maintainer-only backup artifacts. They should not remain reachable as the latest recommended download.

The migration should exclude `core/task_queue.py`, `native_app/task_queue_controller.py`, `native_app/task_queue_view.py`, task-queue mockups, `tests/test_task_queue.py`, translation-list UI, queued task snapshots, queued task history, and same-type or cross-type queued task behavior. It should preserve the single-current-task lifecycle: Excel, Word, and PDF pages lock current task inputs while running and require completion or termination before starting another task.

PDF image translation remains in scope as a V6.0 feature, but it should be migrated as a single-task workflow. PDF page concurrency, page-level recovery, failure placeholder pages, diagnostic reports, and the PDF review model are internal to one running PDF task and are not part of the excluded translation task queue.

The rebuild can assume the only V6.1 through V6.4 user state that matters is the current maintainer's development setup. It does not need broad compatibility for external users who may have run those withdrawn versions.

The app name, bundle identifier, and local data directory stay unchanged so V5.0 user settings and data continue naturally. Settings schema versions should continue moving forward; the product version can return to V6.0, but persisted configuration schema must not move backward.

Model configuration import/export remains in scope for cloud model roles and scoped cloud API keys. It should not export local model configuration, because local model availability depends on locally installed services and imported local settings are not reliably usable on another machine.

Scoped cloud API keys keyed by provider and Base URL remain in scope. Cloud model configuration export may include cloud API keys in plaintext with an explicit warning because cross-machine migration is the purpose of that feature.

The PDF workspace should appear as `PDF 翻译` in the main navigation. The navigation order should be Excel translation, Word translation, PDF translation, then translation memory. The rebuild is complete only when tests and source scans confirm there are no translation task queue modules, UI entries, or tests left behind.

Implementation should start in a separate `rebuild/v6.0-from-v5.0` worktree so the current V6.4-side planning notes are preserved without being mixed into the rebuild. The preferred implementation sequence is: restore the V5.0 baseline, delete Hermes, set V6.0 version and release docs, migrate cloud model roles and configuration import/export, migrate PDF as a single-task workflow, migrate non-queue UI refinements, then run targeted and full regression tests.

The rebuilt runtime dependency additions should stay limited to PDF needs such as PyMuPDF and Pillow unless a later decision explicitly accepts more dependencies.

V6.0 should keep the useful cloud endpoint normalization from the withdrawn V6 line, including default official endpoints and automatic `/v1` normalization for compatible endpoints. It should not restore V5.0's packaged default custom endpoint or default model. The default cloud configuration should remain empty until the user configures it.

LM Studio and custom local OpenAI-compatible local model options remain in scope for the sidebar, but local model configuration is not part of import/export.

The application should keep update checking, but update logic must avoid recommending withdrawn V6.1 through V6.4 releases. If GitHub still reports V6.4 as latest, the app should behave as though there is no valid public update rather than prompting users to install it.
