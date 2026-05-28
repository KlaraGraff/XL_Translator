"""Shared native translation result view helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QHeaderView, QLabel, QTableWidget, QVBoxLayout

from native_app.widgets import (
    MiddleElideLabel,
    configure_app_table,
    configure_file_result_table,
    create_elide_table_item,
    create_table_item,
)


@dataclass(frozen=True)
class ResultIssueRow:
    issue_type: str
    file_name: str
    position: str
    problem: str
    status: str


def format_elapsed(seconds: float) -> str:
    minutes = int(seconds // 60)
    rest = int(seconds % 60)
    if minutes:
        return f"{minutes}分{rest}秒"
    return f"{rest}秒"


def build_done_summary(
    *,
    generated_count: int,
    failed_count: int,
    review_count: int = 0,
    resolved_count: int = 0,
) -> str:
    parts = [f"已生成 {generated_count} 个文件"]
    if failed_count:
        parts.append(f"生成失败 {failed_count} 个文件")
    if review_count:
        parts.append(f"需复核 {review_count} 段")
    if resolved_count:
        parts.append(f"已自动处理 {resolved_count} 段")
    return "任务完成：" + "，".join(parts) + "。"


def render_translation_result(
    layout: QVBoxLayout,
    *,
    empty_message: str,
    done,
    summary_text: str,
    summary_success: bool,
    kpi_items: list[tuple[str, str]],
    file_status_formatter: Callable[[dict], str],
    issue_rows: list[ResultIssueRow] | None = None,
    file_status_width: int = 220,
    file_detail_width: int = 180,
) -> None:
    layout.addWidget(_label("任务结果", "SectionTitle"))
    if done is None:
        layout.addWidget(QLabel(empty_message))
        return

    summary_label = QLabel(summary_text)
    summary_label.setWordWrap(True)
    summary_label.setObjectName("ResultSuccess" if summary_success else "ResultWarning")
    layout.addWidget(summary_label)
    layout.addLayout(_build_result_kpis(kpi_items))

    output_label = _label("输出目录", "PillLabel")
    layout.addWidget(output_label)
    output = MiddleElideLabel(done.output_dir)
    output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    output.setObjectName("MutedText")
    layout.addWidget(output)

    if issue_rows:
        _add_issue_table(layout, issue_rows)

    show_detail = any(str(result.get("error") or "").strip() for result in done.file_results)
    file_table = QTableWidget(len(done.file_results), 3 if show_detail else 2)
    file_table.setHorizontalHeaderLabels(
        ["文件名", "状态", "错误原因"] if show_detail else ["文件名", "状态"]
    )
    configure_app_table(file_table, row_height=40, word_wrap=False)
    configure_file_result_table(
        file_table,
        status_width=file_status_width,
        detail_width=file_detail_width,
    )
    for row, result in enumerate(done.file_results):
        file_table.setItem(row, 0, create_elide_table_item(result.get("name") or ""))
        file_table.setItem(
            row,
            1,
            create_elide_table_item(
                file_status_formatter(result),
                alignment=Qt.AlignmentFlag.AlignCenter,
            ),
        )
        if show_detail:
            file_table.setItem(
                row,
                2,
                create_elide_table_item(result.get("error") or ""),
            )
    layout.addWidget(file_table, 1)


def _label(text: str, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


def _build_result_kpis(items: list[tuple[str, str]]) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    for label, value in items:
        tile = QFrame()
        tile.setObjectName("KpiTile")
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(_label(label, "PillLabel"))
        value_label = _label(value, "PillValue")
        value_label.setWordWrap(True)
        layout.addWidget(value_label)
        row.addWidget(tile, 1)
    return row


def _add_issue_table(layout: QVBoxLayout, issue_rows: list[ResultIssueRow]) -> None:
    layout.addWidget(_label("结果定位清单", "SectionTitle"))
    table = QTableWidget(len(issue_rows), 5)
    table.setHorizontalHeaderLabels(["类型", "文件", "位置", "问题", "处理"])
    configure_app_table(table, row_height=58, word_wrap=True)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(20)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
    header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
    header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
    table.setColumnWidth(0, 88)
    table.setColumnWidth(2, 160)
    table.setColumnWidth(3, 280)
    table.setColumnWidth(4, 330)
    table.setTextElideMode(Qt.TextElideMode.ElideNone)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    for row, issue in enumerate(issue_rows):
        table.setItem(
            row,
            0,
            create_table_item(issue.issue_type, alignment=Qt.AlignmentFlag.AlignCenter),
        )
        table.setCellWidget(row, 1, _table_elide_label(issue.file_name))
        table.setCellWidget(row, 2, _table_elide_label(issue.position))
        for col, value in ((3, issue.problem), (4, issue.status)):
            item = create_table_item(
                value,
                alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            )
            if value:
                item.setToolTip(value)
            table.setItem(row, col, item)
    table.resizeRowsToContents()
    _fit_table_height_to_contents(table)
    layout.addWidget(table)


def _table_elide_label(text: str) -> MiddleElideLabel:
    label = MiddleElideLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    label.setContentsMargins(8, 0, 8, 0)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _fit_table_height_to_contents(table: QTableWidget) -> None:
    header_height = max(
        table.horizontalHeader().height(),
        table.horizontalHeader().sizeHint().height(),
        34,
    )
    row_height = sum(table.rowHeight(row) for row in range(table.rowCount()))
    target_height = header_height + row_height + 2 * table.frameWidth() + 8
    table.setMinimumHeight(target_height)
    table.setMaximumHeight(target_height)
