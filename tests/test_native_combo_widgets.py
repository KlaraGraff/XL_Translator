from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QHeaderView, QTableWidget

from native_app.style import APP_QSS
from native_app.widgets import (
    DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
    DEFAULT_COMBO_POPUP_MAX_HEIGHT,
    DEFAULT_TABLE_ROW_HEIGHT,
    AlignedComboBox,
    MiddleElideLabel,
    _calculate_combo_popup_geometry,
    configure_app_table,
    configure_file_selection_table,
    create_check_table_item,
    create_editable_combo,
    create_option_combo,
    create_searchable_combo,
    create_table_item,
)


class NativeComboWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_option_combo_uses_shared_popup_contract(self) -> None:
        combo = create_option_combo()

        self.assertIsInstance(combo, AlignedComboBox)
        self.assertFalse(combo.isEditable())
        self.assertTrue(combo.property("appOptionCombo"))
        self.assertEqual(combo.maxVisibleItems(), DEFAULT_COMBO_MAX_VISIBLE_ITEMS)
        self.assertEqual(combo.view().maximumHeight(), DEFAULT_COMBO_POPUP_MAX_HEIGHT)

    def test_editable_and_searchable_combos_share_contract(self) -> None:
        editable = create_editable_combo()
        searchable = create_searchable_combo()

        self.assertTrue(editable.isEditable())
        self.assertTrue(searchable.isEditable())
        self.assertTrue(editable.property("appOptionCombo"))
        self.assertTrue(searchable.property("appOptionCombo"))
        self.assertIsNotNone(editable.completer())
        self.assertIsNotNone(searchable.completer())

    def test_qss_disables_native_combobox_popup_overlay(self) -> None:
        self.assertIn("combobox-popup: 0;", APP_QSS)

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
        label = MiddleElideLabel("/Users/example/Workspace/project/final/source.docx")
        label.resize(220, 24)
        label.show()
        self.app.processEvents()

        self.assertIn("…", label.text())
        self.assertTrue(label.text().startswith("/Users"))
        self.assertTrue(label.text().endswith("/source.docx"))
        self.assertEqual(label.toolTip(), label.fullText())

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
