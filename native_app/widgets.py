"""Small Qt widget helpers shared by native pages."""

from __future__ import annotations

import html

from PySide6.QtCore import (
    QChildEvent,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    QStringListModel,
)
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QCompleter,
    QFrame,
    QGraphicsDropShadowEffect,
    QHeaderView,
    QLabel,
    QLineEdit,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionFrame,
    QStylePainter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid


DEFAULT_COMBO_MAX_VISIBLE_ITEMS = 12
DEFAULT_COMBO_POPUP_MAX_HEIGHT = 320
DEFAULT_TABLE_ROW_HEIGHT = 40
TOOLTIP_MARGIN = 12
TOOLTIP_CURSOR_OFFSET = QPoint(14, 18)
TOOLTIP_MAX_WIDTH = 380
TOOLTIP_MIN_WIDTH = 220
TOOLTIP_TITLE_COLOR = "#1F2C3D"
TOOLTIP_BODY_COLOR = "#6E7C8D"
TOOLTIP_LIST_COLOR = "#425267"
TOOLTIP_META_COLOR = "#97A4B3"
_COMBO_ALLOW_FREE_TEXT_PROPERTY = "appComboAllowFreeText"
_COMBO_LAST_VALID_TEXT_PROPERTY = "appComboLastValidText"
_COMBO_VALIDATION_CONFIGURED_PROPERTY = "appComboValidationConfigured"


def is_live_widget(widget: QWidget | None) -> bool:
    """Return whether a Qt widget reference still points to a live C++ object."""
    return widget is not None and isValid(widget)


def build_app_tooltip_html(
    title: str,
    summary: str,
    items: list[str] | None = None,
    *,
    title_meta: str = "",
) -> str:
    """Build compact rich-text tooltip markup for Qt labels."""
    title_html = html.escape(title)
    title_style = f"color:{TOOLTIP_TITLE_COLOR}; font-size:13px; font-weight:700;"
    if title_meta:
        title_block = (
            '<table width="100%" cellspacing="0" cellpadding="0" '
            'style="margin-bottom:4px; border-collapse:collapse;">'
            "<tr>"
            f'<td style="{title_style}">{title_html}</td>'
            f'<td align="right" style="color:{TOOLTIP_META_COLOR}; '
            'font-size:13px; font-weight:700; white-space:nowrap;">'
            f"{html.escape(title_meta)}</td>"
            "</tr>"
            "</table>"
        )
    else:
        title_block = (
            f'<div style="{title_style} margin-bottom:4px;">{title_html}</div>'
        )
    body = title_block + (
        f'<div style="color:{TOOLTIP_BODY_COLOR}; line-height:1.35;">'
        f"{html.escape(summary)}</div>"
    )
    if items:
        rows = "".join(
            "<tr>"
            f'<td style="color:{TOOLTIP_META_COLOR}; padding:3px 7px 1px 0; '
            'vertical-align:top;">&bull;</td>'
            f'<td style="color:{TOOLTIP_LIST_COLOR}; padding:3px 0 1px 0; '
            f'line-height:1.32;">{html.escape(item)}</td>'
            "</tr>"
            for item in items
        )
        body += (
            '<table style="margin-top:6px; border-collapse:collapse;" '
            'cellspacing="0" cellpadding="0">'
            f"{rows}"
            "</table>"
        )
    return body


class InAppToolTipManager(QObject):
    """Render tooltips inside the active application window."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        self._tooltip: QFrame | None = None
        self._tooltip_label: QLabel | None = None
        self._anchor: QWidget | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_tooltip)
        app.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API name.
        event_type = event.type()
        if event_type == QEvent.Type.ToolTip and isinstance(obj, QWidget):
            text = obj.toolTip()
            if text and obj.isEnabled() and obj.isVisible():
                self.show_tooltip(obj, text, event.globalPos())
            else:
                self.hide_tooltip()
            event.accept()
            return True

        if event_type in {
            QEvent.Type.Leave,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.Wheel,
            QEvent.Type.Hide,
            QEvent.Type.Close,
            QEvent.Type.WindowDeactivate,
        }:
            if obj is self._anchor or obj is self._tooltip:
                self.hide_tooltip()

        return super().eventFilter(obj, event)

    def show_tooltip(self, anchor: QWidget, text: str, global_pos: QPoint) -> None:
        window = anchor.window()
        if window is None:
            return

        frame = self._ensure_tooltip(window)
        assert self._tooltip_label is not None
        self._anchor = anchor
        self._tooltip_label.setText(text)
        self._tooltip_label.setMaximumWidth(
            max(TOOLTIP_MIN_WIDTH, min(TOOLTIP_MAX_WIDTH, window.width() - TOOLTIP_MARGIN * 4))
        )
        frame.adjustSize()

        width = frame.sizeHint().width()
        height = frame.sizeHint().height()
        max_x = max(TOOLTIP_MARGIN, window.width() - width - TOOLTIP_MARGIN)
        max_y = max(TOOLTIP_MARGIN, window.height() - height - TOOLTIP_MARGIN)
        local_pos = window.mapFromGlobal(global_pos)

        x = local_pos.x() + TOOLTIP_CURSOR_OFFSET.x()
        y = local_pos.y() + TOOLTIP_CURSOR_OFFSET.y()
        if x > max_x:
            x = local_pos.x() - width - TOOLTIP_CURSOR_OFFSET.x()
        if y > max_y:
            y = local_pos.y() - height - TOOLTIP_CURSOR_OFFSET.y()
        x = max(TOOLTIP_MARGIN, min(x, max_x))
        y = max(TOOLTIP_MARGIN, min(y, max_y))

        frame.move(x, y)
        frame.raise_()
        frame.show()
        duration = anchor.toolTipDuration()
        self._hide_timer.start(duration if duration > 0 else 3200)

    def hide_tooltip(self) -> None:
        self._hide_timer.stop()
        self._anchor = None
        if self._tooltip is not None:
            self._tooltip.hide()

    def current_tooltip(self) -> QFrame | None:
        return self._tooltip

    def _ensure_tooltip(self, window: QWidget) -> QFrame:
        if self._tooltip is not None and self._tooltip.parentWidget() is window:
            return self._tooltip

        if self._tooltip is not None:
            self._tooltip.deleteLater()

        frame = QFrame(window)
        frame.setObjectName("InAppToolTip")
        frame.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        frame.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(31, 44, 61, 34))
        frame.setGraphicsEffect(shadow)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        label = QLabel(frame)
        label.setObjectName("InAppToolTipText")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        layout.addWidget(label)
        self._tooltip = frame
        self._tooltip_label = label
        return frame


def install_in_app_tooltips(app: QApplication) -> InAppToolTipManager:
    """Install the bounded tooltip manager once per QApplication."""
    existing = getattr(app, "_translator_in_app_tooltips", None)
    if isinstance(existing, InAppToolTipManager):
        return existing
    manager = InAppToolTipManager(app)
    setattr(app, "_translator_in_app_tooltips", manager)
    return manager


class ScrollWheelFocusGuard(QObject):
    """Keep page scrolling from accidentally changing value controls."""

    def __init__(self, app: QApplication):
        super().__init__(app)
        app.installEventFilter(self)
        self.sync_existing_widgets(app)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API name.
        if isinstance(obj, QWidget) and event.type() in {
            QEvent.Type.Polish,
            QEvent.Type.Show,
        }:
            _apply_guarded_focus_policy(obj)

        if event.type() == QEvent.Type.ChildAdded and isinstance(event, QChildEvent):
            child = event.child()
            if isinstance(child, QWidget):
                _apply_guarded_focus_policy(child)

        if event.type() != QEvent.Type.Wheel or not isinstance(obj, QWidget):
            return super().eventFilter(obj, event)

        control = _guarded_wheel_control_for(obj)
        if control is None or _widget_contains_focus(control):
            return super().eventFilter(obj, event)

        _forward_wheel_to_scroll_parent(control, event)
        event.accept()
        return True

    def sync_existing_widgets(self, app: QApplication) -> None:
        for widget in app.allWidgets():
            _apply_guarded_focus_policy(widget)


def install_scroll_wheel_focus_guard(app: QApplication) -> ScrollWheelFocusGuard:
    """Install global wheel protection for spin boxes and combo boxes."""
    existing = getattr(app, "_translator_scroll_wheel_focus_guard", None)
    if isinstance(existing, ScrollWheelFocusGuard):
        return existing
    guard = ScrollWheelFocusGuard(app)
    setattr(app, "_translator_scroll_wheel_focus_guard", guard)
    return guard


def _guarded_wheel_control_for(widget: QWidget) -> QWidget | None:
    current: QWidget | None = widget
    while current is not None:
        if isinstance(current, QComboBox):
            if _is_combo_popup_part(current, widget):
                return None
            return current
        if isinstance(current, QAbstractSpinBox):
            return current
        current = current.parentWidget()
    return None


def _apply_guarded_focus_policy(widget: QWidget) -> None:
    if isinstance(widget, QComboBox):
        if widget.focusPolicy() in {
            Qt.FocusPolicy.TabFocus,
            Qt.FocusPolicy.WheelFocus,
            Qt.FocusPolicy.StrongFocus,
        }:
            widget.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
    elif isinstance(widget, QAbstractSpinBox):
        if widget.focusPolicy() in {
            Qt.FocusPolicy.WheelFocus,
            Qt.FocusPolicy.StrongFocus,
        }:
            widget.setFocusPolicy(Qt.FocusPolicy.ClickFocus)


def _is_combo_popup_part(combo: QComboBox, widget: QWidget) -> bool:
    view = combo.view()
    current: QWidget | None = widget
    while current is not None:
        if current is view:
            return True
        current = current.parentWidget()
    return False


def _widget_contains_focus(widget: QWidget) -> bool:
    current = QApplication.focusWidget()
    while current is not None:
        if current is widget:
            return True
        current = current.parentWidget()
    return False


def _forward_wheel_to_scroll_parent(source: QWidget, event: QWheelEvent) -> None:
    receiver = _nearest_scroll_viewport(source)
    if receiver is None:
        event.ignore()
        return

    local_pos = receiver.mapFromGlobal(event.globalPosition().toPoint())
    forwarded = QWheelEvent(
        QPointF(local_pos),
        event.globalPosition(),
        event.pixelDelta(),
        event.angleDelta(),
        event.buttons(),
        event.modifiers(),
        event.phase(),
        event.inverted(),
        event.source(),
        event.pointingDevice(),
    )
    QApplication.sendEvent(receiver, forwarded)


def _nearest_scroll_viewport(widget: QWidget) -> QWidget | None:
    current = widget.parentWidget()
    while current is not None:
        if isinstance(current, QAbstractScrollArea):
            return current.viewport()
        current = current.parentWidget()
    return None


class MiddleElideLabel(QLabel):
    """QLabel that keeps the beginning and end of long values visible."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._full_text = ""
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API name.
        self._full_text = str(text)
        self.setToolTip(self._full_text if self._full_text else "")
        self._apply_elide()

    def fullText(self) -> str:  # noqa: N802 - Qt-style companion for setText.
        return self._full_text

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = max(0, self.contentsRect().width())
        if width <= 0:
            super().setText(self._full_text)
            return
        text = self.fontMetrics().elidedText(
            self._full_text,
            Qt.TextElideMode.ElideMiddle,
            width,
        )
        super().setText(text)


class MiddleElideLineEdit(QLineEdit):
    """Line edit that middle-elides long values while keeping editable text intact."""

    def elided_text(self, width: int | None = None) -> str:
        text = self.text()
        if not text:
            return ""
        available_width = width if width is not None else self._text_rect_width()
        return self.fontMetrics().elidedText(
            text,
            Qt.TextElideMode.ElideMiddle,
            max(0, available_width),
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        if self._should_use_native_paint():
            super().paintEvent(event)
            return

        option = QStyleOptionFrame()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_PanelLineEdit, option, painter, self)

        text_rect = self._text_rect(option)
        painter.setPen(option.palette.text().color())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.elided_text(text_rect.width()),
        )

    def _should_use_native_paint(self) -> bool:
        return (
            self.hasFocus()
            or self.echoMode() != QLineEdit.EchoMode.Normal
            or bool(self.selectedText())
            or not self.text()
        )

    def _text_rect_width(self) -> int:
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        return self._text_rect(option).width()

    def _text_rect(self, option: QStyleOptionFrame) -> QRect:
        text_rect = self.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents,
            option,
            self,
        )
        margins = self.textMargins()
        text_rect.adjust(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        return text_rect


def configure_app_table(
    table: QTableWidget,
    *,
    editable: bool = False,
    row_height: int = DEFAULT_TABLE_ROW_HEIGHT,
    word_wrap: bool = False,
) -> QTableWidget:
    """Apply the shared native table contract without owning column layout."""
    table.setProperty("appTable", "true")
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(row_height)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setWordWrap(word_wrap)
    table.setEditTriggers(
        QTableWidget.EditTrigger.SelectedClicked
        | QTableWidget.EditTrigger.DoubleClicked
        | QTableWidget.EditTrigger.EditKeyPressed
        if editable
        else QTableWidget.EditTrigger.NoEditTriggers
    )
    return table


def configure_file_result_table(
    table: QTableWidget,
    *,
    status_width: int = 220,
    detail_width: int = 180,
) -> QTableWidget:
    """Use stable result-table columns: filename flexes, trailing columns stay visible."""
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(20)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
    table.setColumnWidth(1, status_width)
    if table.columnCount() > 2:
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, detail_width)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return table


def configure_file_selection_table(
    table: QTableWidget,
    *,
    fixed_column_widths: dict[int, int],
    checkbox_width: int = 58,
) -> QTableWidget:
    """Use stable task-list columns: filename flexes, metrics stay visible."""
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(20)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    table.setColumnWidth(0, checkbox_width)
    for column, width in fixed_column_widths.items():
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(column, width)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return table


def create_table_item(
    value: object,
    *,
    editable: bool = False,
    alignment: Qt.AlignmentFlag | Qt.Alignment = Qt.AlignmentFlag.AlignVCenter,
) -> QTableWidgetItem:
    item = QTableWidgetItem(str(value))
    flags = item.flags()
    if editable:
        flags |= Qt.ItemFlag.ItemIsEditable
    else:
        flags &= ~Qt.ItemFlag.ItemIsEditable
    item.setFlags(flags)
    item.setTextAlignment(alignment)
    return item


def create_elide_table_item(
    value: object,
    *,
    editable: bool = False,
    alignment: Qt.AlignmentFlag | Qt.Alignment = Qt.AlignmentFlag.AlignVCenter,
) -> QTableWidgetItem:
    """Create a table item that can middle-elide while keeping the full text in a tooltip."""
    item = create_table_item(value, editable=editable, alignment=alignment)
    text = str(value or "")
    if text:
        item.setToolTip(text)
    return item


def create_check_table_item(checked: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem()
    item.setFlags(
        Qt.ItemFlag.ItemIsUserCheckable
        | Qt.ItemFlag.ItemIsEnabled
        | Qt.ItemFlag.ItemIsSelectable
    )
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


class AlignedComboBox(QComboBox):
    """Combo box whose popup stays attached to the field edge."""

    def showPopup(self) -> None:  # noqa: N802 - Qt API name.
        super().showPopup()
        QTimer.singleShot(0, self.align_popup_to_field)

    def align_popup_to_field(self) -> None:
        popup = _popup_container_for_view(self.view())
        if popup is None or not popup.isVisible():
            return
        _align_popup_to_combo(self, popup)


class CenteredTextComboBox(AlignedComboBox):
    """Combo box that centers the selected value while keeping popup behavior."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        current_text = option.currentText
        option.currentText = ""

        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)

        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        arrow_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        edit_rect.setLeft(self.rect().left() + 8)
        edit_rect.setRight(arrow_rect.left() - 4)

        painter.setPen(option.palette.text().color())
        painter.drawText(
            edit_rect,
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
            current_text,
        )


class CurrentTextOverrideComboBox(AlignedComboBox):
    """Combo box that can paint a shorter closed-state label than its popup item."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._current_display_text_override = ""

    def setCurrentDisplayTextOverride(self, text: str) -> None:  # noqa: N802 - Qt-style setter.
        self._current_display_text_override = str(text or "")
        self.update()

    def currentDisplayTextOverride(self) -> str:  # noqa: N802 - Qt-style getter.
        return self._current_display_text_override

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        if not self._current_display_text_override:
            super().paintEvent(event)
            return

        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = ""

        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)

        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        arrow_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        edit_rect.setLeft(self.rect().left() + 9)
        edit_rect.setRight(arrow_rect.left() - 4)

        painter.setPen(option.palette.text().color())
        painter.drawText(
            edit_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._current_display_text_override,
        )


class AlignedCompleter(QCompleter):
    """Completer popup with the same edge-attached positioning as combo popups."""

    def __init__(self, combo: QComboBox):
        super().__init__(combo)
        self._combo = combo

    def complete(self, rect: QRect = QRect()) -> None:
        super().complete(rect)
        QTimer.singleShot(0, self.align_popup_to_field)

    def align_popup_to_field(self) -> None:
        popup = _popup_container_for_view(self.popup())
        if popup is None or not popup.isVisible():
            return
        _align_popup_to_combo(
            self._combo,
            popup,
            popup_view=self.popup(),
            item_count=max(0, self.completionCount()),
        )


def create_option_combo(*, max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS) -> QComboBox:
    """Create a standard app select control with the shared popup behavior."""
    combo = AlignedComboBox()
    configure_option_combo(combo, max_visible_items=max_visible_items)
    return combo


def create_centered_option_combo(
    *,
    max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
) -> QComboBox:
    """Create a select control with centered selected text."""
    combo = CenteredTextComboBox()
    configure_option_combo(combo, max_visible_items=max_visible_items)
    return combo


def create_current_text_override_combo(
    *,
    max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
) -> CurrentTextOverrideComboBox:
    """Create a select control whose closed label can be shorter than popup text."""
    combo = CurrentTextOverrideComboBox()
    configure_option_combo(combo, max_visible_items=max_visible_items)
    return combo


def create_editable_combo(*, max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS) -> QComboBox:
    """Create a combo that allows free text while keeping the app popup style."""
    combo = AlignedComboBox()
    configure_editable_combo(combo, max_visible_items=max_visible_items)
    return combo


def create_searchable_combo(*, max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS) -> QComboBox:
    """Create a searchable select that commits the closest listed option."""
    combo = AlignedComboBox()
    configure_searchable_combo(combo, max_visible_items=max_visible_items)
    return combo


def configure_option_combo(
    combo: QComboBox,
    *,
    max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
) -> None:
    """Apply the shared non-native dropdown sizing to app option controls."""
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.setMaxVisibleItems(max_visible_items)
    combo.view().setMaximumHeight(DEFAULT_COMBO_POPUP_MAX_HEIGHT)
    combo.view().setTextElideMode(Qt.TextElideMode.ElideRight)
    combo.setProperty("appOptionCombo", True)


def configure_editable_combo(
    combo: QComboBox,
    *,
    max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
    allow_free_text: bool = True,
) -> None:
    """Make a combo editable while keeping the last valid value available."""
    configure_option_combo(combo, max_visible_items=max_visible_items)
    combo.setEditable(True)
    combo.setProperty(_COMBO_ALLOW_FREE_TEXT_PROPERTY, allow_free_text)
    if not combo.property(_COMBO_VALIDATION_CONFIGURED_PROPERTY):
        combo.currentIndexChanged.connect(
            lambda _index=0, combo=combo: _remember_combo_current_valid_text(combo)
        )
        combo.currentIndexChanged.connect(
            lambda _index=0, combo=combo: _queue_combo_text_from_start(combo)
        )
        combo.currentTextChanged.connect(
            lambda text="", combo=combo: _remember_combo_free_text(combo, text)
        )
        if combo.lineEdit() is not None:
            combo.lineEdit().returnPressed.connect(
                lambda combo=combo: select_combo_text_match(combo)
            )
            combo.lineEdit().editingFinished.connect(
                lambda combo=combo: select_combo_text_match(combo)
            )
        combo.setProperty(_COMBO_VALIDATION_CONFIGURED_PROPERTY, True)
    refresh_combo_completer(combo)


def configure_searchable_combo(
    combo: QComboBox,
    *,
    max_visible_items: int = DEFAULT_COMBO_MAX_VISIBLE_ITEMS,
) -> None:
    """Make a combo box searchable without opening an oversized popup by default."""
    configure_editable_combo(
        combo,
        max_visible_items=max_visible_items,
        allow_free_text=False,
    )
    completer = combo.completer()
    if completer is None:
        completer = QCompleter(combo)
        combo.setCompleter(completer)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    refresh_combo_completer(combo)


def refresh_combo_completer(combo: QComboBox) -> None:
    completer = _ensure_aligned_completer(combo)
    completer.setModel(
        QStringListModel([combo.itemText(index) for index in range(combo.count())], combo)
    )
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.popup().setTextElideMode(Qt.TextElideMode.ElideRight)
    _remember_combo_current_valid_text(combo)
    _queue_combo_text_from_start(combo)


def _ensure_aligned_completer(combo: QComboBox) -> AlignedCompleter:
    completer = combo.completer()
    if isinstance(completer, AlignedCompleter):
        return completer
    aligned = AlignedCompleter(combo)
    combo.setCompleter(aligned)
    return aligned


def _popup_container_for_view(view: QWidget | None) -> QWidget | None:
    if view is None:
        return None
    parent = view.parentWidget()
    if parent is not None and parent.window() is not view.window():
        return parent
    window = view.window()
    return window if isinstance(window, QWidget) else view


def _align_popup_to_combo(
    combo: QComboBox,
    popup: QWidget,
    *,
    popup_view: QWidget | None = None,
    item_count: int | None = None,
) -> None:
    anchor = QRect(combo.mapToGlobal(QPoint(0, 0)), combo.size())
    screen = combo.window().windowHandle().screen() if combo.window().windowHandle() else None
    primary_screen = QApplication.primaryScreen()
    available = (
        screen.availableGeometry()
        if screen is not None
        else primary_screen.availableGeometry()
    )
    popup_height = _combo_popup_target_height(
        combo,
        popup,
        popup_view=popup_view or combo.view(),
        item_count=combo.count() if item_count is None else item_count,
    )
    popup_width = _combo_popup_target_width(combo, popup_view=popup_view or combo.view())
    geometry = _calculate_combo_popup_geometry(
        anchor,
        available,
        popup_height,
        requested_width=popup_width,
    )
    popup.setGeometry(geometry)


def _combo_popup_target_height(
    combo: QComboBox,
    popup: QWidget,
    *,
    popup_view: QWidget,
    item_count: int,
) -> int:
    visible_count = min(item_count, combo.maxVisibleItems())
    if visible_count <= 0:
        return max(1, min(popup.height(), DEFAULT_COMBO_POPUP_MAX_HEIGHT))

    default_row_height = popup_view.fontMetrics().height() + 10
    row_height = max(combo.view().sizeHintForRow(0), default_row_height)
    viewport = getattr(popup_view, "viewport", lambda: popup_view)()
    frame_extra = max(2, popup.height() - max(1, viewport.height()))
    content_height = visible_count * row_height + frame_extra
    return max(row_height + frame_extra, min(content_height, DEFAULT_COMBO_POPUP_MAX_HEIGHT))


def _combo_popup_target_width(
    combo: QComboBox,
    *,
    popup_view: QWidget,
) -> int | None:
    raw_minimum_width = combo.property("popupMinimumWidth")
    if raw_minimum_width in (None, False):
        return None
    try:
        minimum_width = int(raw_minimum_width)
    except (TypeError, ValueError):
        return None

    content_width = max(0, popup_view.sizeHintForColumn(0))
    viewport = getattr(popup_view, "viewport", lambda: popup_view)()
    frame_extra = max(0, popup_view.width() - max(1, viewport.width()))
    return max(combo.width(), minimum_width, content_width + frame_extra)


def _calculate_combo_popup_geometry(
    anchor: QRect,
    available: QRect,
    requested_height: int,
    *,
    requested_width: int | None = None,
) -> QRect:
    requested_height = max(1, requested_height)
    width = min(max(1, requested_width or anchor.width()), available.width())
    x = max(available.left(), min(anchor.left(), available.right() - width + 1))

    anchor_top = anchor.top()
    anchor_bottom = anchor.top() + anchor.height()
    available_top = available.top()
    available_bottom = available.top() + available.height()
    below_space = max(0, available_bottom - anchor_bottom)
    above_space = max(0, anchor_top - available_top)

    open_down = below_space >= requested_height or above_space <= below_space

    space = below_space if open_down else above_space
    if space <= 0:
        open_down = not open_down
        space = below_space if open_down else above_space
    height = max(1, min(requested_height, max(1, space)))
    y = anchor_bottom if open_down else anchor_top - height
    y = max(available_top, min(y, available_bottom - height))
    return QRect(x, y, width, height)


def _combo_allows_free_text(combo: QComboBox) -> bool:
    return bool(combo.property(_COMBO_ALLOW_FREE_TEXT_PROPERTY))


def _queue_combo_text_from_start(combo: QComboBox) -> None:
    _show_combo_text_from_start(combo)
    QTimer.singleShot(0, lambda combo=combo: _show_combo_text_from_start(combo))


def _show_combo_text_from_start(combo: QComboBox) -> None:
    if not is_live_widget(combo) or not combo.isEditable():
        return
    line_edit = combo.lineEdit()
    if line_edit is None or not is_live_widget(line_edit):
        return
    if line_edit.hasSelectedText():
        line_edit.deselect()
    line_edit.setCursorPosition(0)


def _combo_text_index(combo: QComboBox, text: str) -> int:
    lowered = str(text or "").strip().casefold()
    for index in range(combo.count()):
        if combo.itemText(index).strip().casefold() == lowered:
            return index
    return -1


def _combo_text_match_index(combo: QComboBox, text: str) -> int:
    lowered = str(text or "").strip().casefold()
    if not lowered:
        return _combo_text_index(combo, "")

    exact_index = _combo_text_index(combo, text)
    if exact_index >= 0:
        return exact_index

    for index in range(combo.count()):
        if combo.itemText(index).strip().casefold().startswith(lowered):
            return index
    for index in range(combo.count()):
        if lowered in combo.itemText(index).strip().casefold():
            return index
    return -1


def _set_combo_to_item_index(combo: QComboBox, index: int) -> None:
    text = combo.itemText(index)
    if index == combo.currentIndex():
        combo.setCurrentText(text)
    else:
        combo.setCurrentIndex(index)
    combo.setProperty(_COMBO_LAST_VALID_TEXT_PROPERTY, text)
    _queue_combo_text_from_start(combo)


def _remember_combo_current_valid_text(combo: QComboBox) -> None:
    if combo.currentIndex() >= 0:
        combo.setProperty(
            _COMBO_LAST_VALID_TEXT_PROPERTY,
            combo.itemText(combo.currentIndex()),
        )
        return

    text = combo.currentText().strip()
    if _combo_allows_free_text(combo) or _combo_text_index(combo, text) >= 0:
        combo.setProperty(_COMBO_LAST_VALID_TEXT_PROPERTY, text)


def _remember_combo_free_text(combo: QComboBox, text: str) -> None:
    if _combo_allows_free_text(combo):
        combo.setProperty(_COMBO_LAST_VALID_TEXT_PROPERTY, str(text or "").strip())


def _restore_combo_last_valid_text(combo: QComboBox) -> bool:
    fallback = combo.property(_COMBO_LAST_VALID_TEXT_PROPERTY)
    if fallback is None and combo.currentIndex() >= 0:
        fallback = combo.itemText(combo.currentIndex())
    if fallback is None and combo.count() > 0:
        fallback = combo.itemText(0)
    if fallback is None:
        return False

    text = str(fallback or "").strip()
    index = _combo_text_index(combo, text)
    if index >= 0:
        _set_combo_to_item_index(combo, index)
        return True
    if _combo_allows_free_text(combo):
        combo.setCurrentText(text)
        combo.setProperty(_COMBO_LAST_VALID_TEXT_PROPERTY, text)
        _queue_combo_text_from_start(combo)
        return True
    return False


def select_combo_text_match(combo: QComboBox) -> bool:
    """Select the closest item for typed combo text, or restore the last valid value."""
    text = combo.currentText().strip()
    index = _combo_text_match_index(combo, text)
    if index >= 0:
        _set_combo_to_item_index(combo, index)
        return True
    if _combo_allows_free_text(combo):
        combo.setCurrentText(text)
        combo.setProperty(_COMBO_LAST_VALID_TEXT_PROPERTY, text)
        _queue_combo_text_from_start(combo)
        return True
    _restore_combo_last_valid_text(combo)
    return False
