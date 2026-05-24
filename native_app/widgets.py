"""Small Qt widget helpers shared by native pages."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QStringListModel
from PySide6.QtWidgets import QApplication, QComboBox, QCompleter, QFrame, QLabel, QVBoxLayout, QWidget


TOOLTIP_MARGIN = 12
TOOLTIP_CURSOR_OFFSET = QPoint(14, 18)
TOOLTIP_MAX_WIDTH = 320
TOOLTIP_MIN_WIDTH = 180


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


def configure_searchable_combo(combo: QComboBox, *, max_visible_items: int = 12) -> None:
    """Make a combo box searchable without opening an oversized popup by default."""
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.setMaxVisibleItems(max_visible_items)
    combo.view().setMaximumHeight(320)
    completer = combo.completer()
    if completer is None:
        completer = QCompleter(combo)
        combo.setCompleter(completer)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    if combo.lineEdit() is not None:
        combo.lineEdit().returnPressed.connect(lambda: select_combo_text_match(combo))
        combo.lineEdit().editingFinished.connect(lambda: select_combo_text_match(combo))
    refresh_combo_completer(combo)


def refresh_combo_completer(combo: QComboBox) -> None:
    completer = combo.completer()
    if completer is None:
        completer = QCompleter(combo)
        combo.setCompleter(completer)
    completer.setModel(
        QStringListModel([combo.itemText(index) for index in range(combo.count())], combo)
    )
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)


def select_combo_text_match(combo: QComboBox) -> bool:
    """Select the closest item for manually typed combo text."""
    text = combo.currentText().strip()
    if not text:
        return False

    lowered = text.casefold()
    candidates = [combo.itemText(index) for index in range(combo.count())]
    matched = next(
        (candidate for candidate in candidates if candidate.casefold() == lowered),
        "",
    )
    if not matched:
        matched = next(
            (candidate for candidate in candidates if candidate.casefold().startswith(lowered)),
            "",
        )
    if not matched:
        matched = next(
            (candidate for candidate in candidates if lowered in candidate.casefold()),
            "",
        )
    if not matched:
        return False

    index = combo.findText(matched)
    if index < 0 or index == combo.currentIndex():
        combo.setCurrentText(matched)
        return True
    combo.setCurrentIndex(index)
    return True
