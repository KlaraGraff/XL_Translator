from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QHeaderView, QScrollArea, QSpinBox, QTableWidget, QVBoxLayout, QWidget

from native_app.style import APP_QSS
from native_app.widgets import (
    DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
    DEFAULT_COMBO_POPUP_MAX_HEIGHT,
    DEFAULT_TABLE_ROW_HEIGHT,
    AlignedComboBox,
    CenteredTextComboBox,
    CurrentTextOverrideComboBox,
    MiddleElideLabel,
    MiddleElideLineEdit,
    _calculate_combo_popup_geometry,
    build_app_tooltip_html,
    configure_app_table,
    configure_file_selection_table,
    create_centered_option_combo,
    create_check_table_item,
    create_current_text_override_combo,
    create_editable_combo,
    create_option_combo,
    create_searchable_combo,
    create_table_item,
    install_scroll_wheel_focus_guard,
    select_combo_text_match,
    set_combo_item_search_aliases,
)


class NativeComboWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _scroll_host(self, control: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        content = QWidget()
        layout = QVBoxLayout(content)
        for _ in range(18):
            layout.addWidget(QLabel("before"))
        layout.addWidget(control)
        for _ in range(18):
            layout.addWidget(QLabel("after"))
        scroll.setWidget(content)
        scroll.resize(220, 180)
        scroll.show()
        self.app.processEvents()
        self.addCleanup(scroll.close)
        self.addCleanup(scroll.deleteLater)
        return scroll

    def _send_wheel(self, widget: QWidget, angle_delta: int = 120) -> QWheelEvent:
        pos = widget.rect().center()
        global_pos = widget.mapToGlobal(pos)
        event = QWheelEvent(
            QPointF(pos),
            QPointF(global_pos),
            QPoint(0, 0),
            QPoint(0, angle_delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(widget, event)
        self.app.processEvents()
        return event

    def test_option_combo_uses_shared_popup_contract(self) -> None:
        combo = create_option_combo()

        self.assertIsInstance(combo, AlignedComboBox)
        self.assertFalse(combo.isEditable())
        self.assertTrue(combo.property("appOptionCombo"))
        self.assertEqual(combo.maxVisibleItems(), DEFAULT_COMBO_MAX_VISIBLE_ITEMS)
        self.assertEqual(combo.view().maximumHeight(), DEFAULT_COMBO_POPUP_MAX_HEIGHT)
        self.assertEqual(combo.view().textElideMode(), Qt.TextElideMode.ElideRight)

    def test_centered_option_combo_keeps_select_contract(self) -> None:
        combo = create_centered_option_combo()

        self.assertIsInstance(combo, CenteredTextComboBox)
        self.assertFalse(combo.isEditable())
        self.assertTrue(combo.property("appOptionCombo"))
        self.assertEqual(combo.maxVisibleItems(), DEFAULT_COMBO_MAX_VISIBLE_ITEMS)

    def test_current_text_override_combo_keeps_popup_contract(self) -> None:
        combo = create_current_text_override_combo()

        self.assertIsInstance(combo, CurrentTextOverrideComboBox)
        self.assertTrue(combo.property("appOptionCombo"))
        combo.setCurrentDisplayTextOverride("自定义")
        self.assertEqual(combo.currentDisplayTextOverride(), "自定义")

    def test_editable_and_searchable_combos_share_contract(self) -> None:
        editable = create_editable_combo()
        searchable = create_searchable_combo()

        self.assertTrue(editable.isEditable())
        self.assertTrue(searchable.isEditable())
        self.assertTrue(editable.property("appOptionCombo"))
        self.assertTrue(searchable.property("appOptionCombo"))
        self.assertIsNotNone(editable.completer())
        self.assertIsNotNone(searchable.completer())
        self.assertEqual(searchable.view().textElideMode(), Qt.TextElideMode.ElideRight)
        self.assertEqual(
            searchable.completer().popup().textElideMode(),
            Qt.TextElideMode.ElideRight,
        )

    def test_searchable_combo_reverts_unknown_manual_text(self) -> None:
        combo = create_searchable_combo()
        combo.addItems(["中文", "英文", "法文"])
        combo.setCurrentIndex(0)

        combo.setEditText("中文我问k")
        matched = select_combo_text_match(combo)

        self.assertFalse(matched)
        self.assertEqual(combo.currentText(), "中文")
        self.assertEqual(combo.currentIndex(), 0)

    def test_searchable_combo_selected_text_starts_from_left(self) -> None:
        combo = create_searchable_combo()
        combo.addItems(["英文", "柬埔寨语（高棉语）"])

        combo.setCurrentIndex(1)
        self.app.processEvents()

        line_edit = combo.lineEdit()
        self.assertIsNotNone(line_edit)
        self.assertEqual(line_edit.cursorPosition(), 0)

    def test_searchable_combo_reverts_to_latest_valid_selection(self) -> None:
        combo = create_searchable_combo()
        combo.addItems(["中文", "英文", "法文"])
        combo.setCurrentIndex(1)

        combo.setEditText("not-a-language")
        combo.lineEdit().editingFinished.emit()

        self.assertEqual(combo.currentText(), "英文")
        self.assertEqual(combo.currentIndex(), 1)

    def test_searchable_combo_commits_partial_match(self) -> None:
        combo = create_searchable_combo()
        combo.addItems(["中文", "英文", "法文"])
        combo.setCurrentIndex(0)

        combo.setEditText("英")
        combo.lineEdit().editingFinished.emit()

        self.assertEqual(combo.currentText(), "英文")
        self.assertEqual(combo.currentIndex(), 1)

    def test_searchable_combo_commits_hidden_alias_match(self) -> None:
        combo = create_searchable_combo()
        combo.addItem("中文", "zh")
        combo.addItem("法文", "fr")
        set_combo_item_search_aliases(combo, 1, ["法语"])
        combo.setCurrentIndex(0)

        combo.setEditText("法语")
        combo.lineEdit().editingFinished.emit()

        self.assertEqual(combo.currentText(), "法文")
        self.assertEqual(combo.currentData(), "fr")

    def test_editable_combo_keeps_custom_manual_text_valid(self) -> None:
        combo = create_editable_combo()
        combo.addItems(["model-a", "model-b"])
        combo.setCurrentIndex(0)

        combo.setEditText("custom-model")
        combo.lineEdit().editingFinished.emit()

        self.assertEqual(combo.currentText(), "custom-model")

    def test_qss_disables_native_combobox_popup_overlay(self) -> None:
        self.assertIn("combobox-popup: 0;", APP_QSS)

    def test_spin_box_wheel_scrolls_parent_until_focused(self) -> None:
        install_scroll_wheel_focus_guard(self.app)
        spin = QSpinBox()
        spin.setRange(0, 99)
        spin.setValue(10)
        scroll = self._scroll_host(spin)
        bar = scroll.verticalScrollBar()

        self.assertEqual(spin.focusPolicy(), Qt.FocusPolicy.ClickFocus)
        bar.setValue(bar.maximum() // 2)
        scroll.setFocus(Qt.FocusReason.MouseFocusReason)
        spin.clearFocus()
        self.app.processEvents()
        start_scroll = bar.value()

        self._send_wheel(spin)

        self.assertEqual(spin.value(), 10)
        self.assertNotEqual(bar.value(), start_scroll)

        bar.setValue(bar.maximum() // 2)
        spin.setFocus(Qt.FocusReason.MouseFocusReason)
        self.app.processEvents()
        start_scroll = bar.value()
        start_value = spin.value()

        self._send_wheel(spin)

        self.assertNotEqual(spin.value(), start_value)
        self.assertEqual(bar.value(), start_scroll)

    def test_combo_box_wheel_scrolls_parent_until_focused(self) -> None:
        install_scroll_wheel_focus_guard(self.app)
        combo = create_option_combo()
        combo.addItems(["低", "中", "高"])
        combo.setCurrentIndex(1)
        scroll = self._scroll_host(combo)
        bar = scroll.verticalScrollBar()

        self.assertEqual(combo.focusPolicy(), Qt.FocusPolicy.ClickFocus)
        bar.setValue(bar.maximum() // 2)
        scroll.setFocus(Qt.FocusReason.MouseFocusReason)
        combo.clearFocus()
        self.app.processEvents()
        start_scroll = bar.value()

        self._send_wheel(combo)

        self.assertEqual(combo.currentIndex(), 1)
        self.assertNotEqual(bar.value(), start_scroll)

        bar.setValue(bar.maximum() // 2)
        combo.setFocus(Qt.FocusReason.MouseFocusReason)
        self.app.processEvents()
        start_scroll = bar.value()
        start_index = combo.currentIndex()

        self._send_wheel(combo)

        self.assertNotEqual(combo.currentIndex(), start_index)
        self.assertEqual(bar.value(), start_scroll)

    def test_searchable_combo_wheel_focus_is_removed_when_guard_is_installed(self) -> None:
        install_scroll_wheel_focus_guard(self.app)
        combo = create_searchable_combo()
        combo.addItems(["中文", "英文"])
        self._scroll_host(combo)

        self.assertEqual(combo.focusPolicy(), Qt.FocusPolicy.ClickFocus)
        self.assertEqual(combo.lineEdit().focusPolicy(), Qt.FocusPolicy.StrongFocus)

    def test_tooltip_palette_matches_v46_light_card_style(self) -> None:
        self.assertIn("QToolTip {\n    background: #FFFFFF;", APP_QSS)
        self.assertIn("QFrame#InAppToolTip {\n    background: #FFFFFF;", APP_QSS)
        self.assertIn("color: #425267;", APP_QSS)
        self.assertNotIn("QToolTip {\n    background: #111827;", APP_QSS)

        markup = build_app_tooltip_html(
            "标题",
            "说明正文",
            ["选项一", "选项二"],
            title_meta="补充",
        )

        self.assertIn("color:#1F2C3D", markup)
        self.assertIn("color:#6E7C8D", markup)
        self.assertIn("color:#425267", markup)
        self.assertIn("color:#97A4B3", markup)
        self.assertIn('align="right"', markup)
        self.assertIn("font-size:13px; font-weight:700; white-space:nowrap;", markup)

    def test_app_table_helper_applies_shared_read_only_contract(self) -> None:
        table = QTableWidget(1, 2)
        configure_app_table(table)
        table.setItem(0, 0, create_check_table_item())
        table.setItem(0, 1, create_table_item("只读内容"))

        self.assertTrue(table.property("appTable"))
        self.assertFalse(table.verticalHeader().isVisible())
        self.assertTrue(table.alternatingRowColors())
        self.assertEqual(
            table.editTriggers(),
            QTableWidget.EditTrigger.NoEditTriggers,
        )
        self.assertEqual(
            table.verticalHeader().defaultSectionSize(),
            DEFAULT_TABLE_ROW_HEIGHT,
        )
        self.assertFalse(table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable)

    def test_file_selection_table_keeps_metrics_fixed_and_filename_stretching(self) -> None:
        table = QTableWidget(1, 5)
        configure_file_selection_table(
            table,
            fixed_column_widths={2: 112, 3: 72, 4: 72},
        )

        header = table.horizontalHeader()
        self.assertEqual(header.sectionResizeMode(0), QHeaderView.ResizeMode.Fixed)
        self.assertEqual(header.sectionResizeMode(1), QHeaderView.ResizeMode.Stretch)
        self.assertEqual(header.sectionResizeMode(2), QHeaderView.ResizeMode.Fixed)
        self.assertEqual(table.columnWidth(0), 58)
        self.assertEqual(table.columnWidth(2), 112)
        self.assertEqual(table.textElideMode(), Qt.TextElideMode.ElideMiddle)
        self.assertEqual(table.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def test_middle_elide_label_preserves_full_value_in_tooltip(self) -> None:
        label = MiddleElideLabel(r"C:\Users\example\Workspace\project\final\source.docx")
        label.resize(220, 24)
        label.show()
        self.app.processEvents()

        self.assertIn("…", label.text())
        self.assertTrue(label.text().startswith("C:"))
        self.assertEqual(label.toolTip(), label.fullText())
        self.assertTrue(label.fullText().endswith(r"\source.docx"))

    def test_middle_elide_line_edit_keeps_full_text_value(self) -> None:
        full_path = (
            r"C:\Users\example\Workspace\1001 Creativity\001 Translate for excel"
            r"\github_Product_TranslateForExcel\.runtime\self-tests"
            r"\queue-ui-regression\sample-files\queue_test_sample.xlsx"
        )
        field = MiddleElideLineEdit(full_path)

        elided = field.elided_text(300)

        self.assertEqual(field.text(), full_path)
        self.assertIn("…", elided)
        self.assertTrue(elided.startswith("C:"))
        self.assertTrue(elided.endswith(".xlsx"))
        self.assertIn("sample", elided)

    def test_popup_geometry_attaches_below_field_when_space_allows(self) -> None:
        geometry = _calculate_combo_popup_geometry(
            anchor=QRect(40, 100, 240, 36),
            available=QRect(0, 0, 800, 600),
            requested_height=240,
        )

        self.assertEqual(geometry.top(), 136)
        self.assertEqual(geometry.left(), 40)
        self.assertEqual(geometry.width(), 240)
        self.assertEqual(geometry.height(), 240)

    def test_popup_geometry_can_be_wider_than_anchor(self) -> None:
        geometry = _calculate_combo_popup_geometry(
            anchor=QRect(40, 100, 80, 36),
            available=QRect(0, 0, 800, 600),
            requested_height=240,
            requested_width=236,
        )

        self.assertEqual(geometry.top(), 136)
        self.assertEqual(geometry.left(), 40)
        self.assertEqual(geometry.width(), 236)

    def test_popup_geometry_attaches_above_field_when_lower_space_is_short(self) -> None:
        geometry = _calculate_combo_popup_geometry(
            anchor=QRect(40, 520, 240, 36),
            available=QRect(0, 0, 800, 600),
            requested_height=240,
        )

        self.assertEqual(geometry.bottom() + 1, 520)
        self.assertEqual(geometry.height(), 240)

    def test_popup_geometry_shrinks_in_the_larger_available_direction(self) -> None:
        geometry = _calculate_combo_popup_geometry(
            anchor=QRect(40, 120, 240, 36),
            available=QRect(0, 0, 800, 260),
            requested_height=240,
        )

        self.assertEqual(geometry.bottom() + 1, 120)
        self.assertEqual(geometry.height(), 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
