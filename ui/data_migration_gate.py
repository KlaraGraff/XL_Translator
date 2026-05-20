"""First-run gate for migrating legacy local data into the new app path."""

from __future__ import annotations

import streamlit as st

from app_meta import APP_NAME
from core.data_migration import (
    DataMigrationPlan,
    format_size,
    inspect_data_migration,
    mark_migration_skipped,
    migrate_legacy_data,
)

_SUPPORT_FILES_HELP = (
    "可选迁移排错资料：app.log、desktop_launcher.log、desktop_instance.json、"
    "diagnostics 目录和历史 zip。它们只用于排查问题，不会影响翻译数据；"
    "如果勾选，相关内容会保存在新目录的 legacy_support 下。"
)


def render_data_migration_gate() -> None:
    """Show the migration gate when a legacy data directory is detected."""
    plan = inspect_data_migration()
    if not plan.has_prompt:
        return

    if hasattr(st, "dialog"):
        @st.dialog(f"{APP_NAME} 首次启动")
        def _dialog() -> None:
            _render_gate_content(plan)

        _dialog()
    else:
        _render_gate_content(plan)

    st.stop()


def _render_gate_content(plan: DataMigrationPlan) -> None:
    st.markdown(
        f"检测到旧版数据目录 `{plan.legacy_data_dir}`，可迁移到新的 `{plan.app_data_dir}`。"
    )
    st.caption("默认只迁移翻译记忆库、设置和 API Key。")
    st.markdown(
        f"- 默认迁移大小：{format_size(plan.primary_size_bytes)}\n"
        f"- 可选排错资料大小：{format_size(plan.support_size_bytes)}"
    )

    if plan.status == "conflict":
        st.warning(
            "新目录里已经有数据，所以不会自动合并，避免覆盖现有内容。"
        )
        if plan.conflicts:
            conflict_list = "\n".join(f"- `{path}`" for path in plan.conflicts)
            st.markdown(f"冲突项：\n{conflict_list}")
        if st.button("继续使用新数据，不再提示", type="primary"):
            mark_migration_skipped(plan, reason="new_data_exists")
            st.rerun()
        if st.button("稍后再说", key="migration_conflict_later"):
            st.stop()
        return

    include_support_files = st.checkbox(
        "同时迁移排错资料",
        value=False,
        help=_SUPPORT_FILES_HELP,
    )

    left, right = st.columns(2)
    with left:
        if st.button("迁移旧数据", type="primary"):
            progress = st.progress(0.0)
            status = st.empty()

            def _report(current: int, total: int, message: str) -> None:
                progress.progress(0.0 if total <= 0 else min(1.0, current / total))
                status.write(message)

            migrate_legacy_data(
                plan,
                include_support_files=include_support_files,
                progress=_report,
            )
            st.success("迁移完成，正在重新载入应用。")
            st.rerun()
    with right:
        if st.button("使用全新数据，不再提示"):
            mark_migration_skipped(plan, reason="user_opted_out")
            st.rerun()

    st.caption("如果现在不想处理，也可以直接关闭窗口，之后重新打开时会再次提示。")
