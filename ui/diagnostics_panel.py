"""Shared Streamlit controls for diagnostic archive downloads."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from core.diagnostics import (
    build_diagnostic_zip_bytes,
    build_diagnostics_history_zip_bytes,
    count_diagnostic_records,
    estimate_record_size,
    format_size,
)


def render_current_diagnostic_download(
    record_dir: str | Path | None,
    *,
    key_prefix: str,
) -> None:
    """Render a download button for the current task diagnostic archive."""
    if not record_dir:
        return

    path = Path(record_dir)
    try:
        data, filename = build_diagnostic_zip_bytes(path)
        record_size = estimate_record_size(path)
    except Exception as exc:  # noqa: BLE001 - UI should show a recoverable notice
        st.warning(f"诊断包准备失败：{exc}")
        return

    st.download_button(
        "下载本次诊断包",
        data=data,
        file_name=filename,
        mime="application/zip",
        use_container_width=True,
        key=f"{key_prefix}_current_diagnostic_download",
    )
    st.caption(
        f"仅含诊断信息，约 {format_size(record_size)}；不包含原始文件和 API Key。"
    )


def render_history_diagnostic_export(
    *,
    key_prefix: str,
    disabled: bool = False,
) -> None:
    """Render an always-available export button for all archived diagnostics."""
    record_count = count_diagnostic_records()
    if record_count <= 0:
        st.button(
            "暂无历史诊断",
            use_container_width=True,
            disabled=True,
            key=f"{key_prefix}_history_diagnostic_empty",
        )
        st.caption("出现翻译异常后会自动留下轻量诊断归档。")
        return

    try:
        data, filename, exported_count = build_diagnostics_history_zip_bytes()
    except Exception as exc:  # noqa: BLE001 - UI should not block other actions
        st.button(
            "历史诊断不可用",
            use_container_width=True,
            disabled=True,
            key=f"{key_prefix}_history_diagnostic_error",
        )
        st.caption(f"读取历史诊断失败：{exc}")
        return

    st.download_button(
        "导出历史诊断归档",
        data=data,
        file_name=filename,
        mime="application/zip",
        use_container_width=True,
        disabled=disabled,
        key=f"{key_prefix}_history_diagnostic_download",
    )
    st.caption(f"已归档 {exported_count} 次异常任务，可统一导出排查。")
