"""Qt style sheet used by the native desktop shell."""

from __future__ import annotations

import sys
from pathlib import Path


def _asset_url(filename: str) -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return (root / "assets" / "ui" / filename).resolve().as_posix()


APP_QSS = """
QWidget {
    background: #F4F6F9;
    color: #1A2035;
    font-family: "Arial", "Helvetica", sans-serif;
    font-size: 13px;
}

QLabel,
QCheckBox,
QRadioButton {
    background: transparent;
}

QScrollArea {
    border: none;
    background: transparent;
}

QScrollArea > QWidget > QWidget {
    background: transparent;
}

QFrame#Sidebar {
    background: #FFFFFF;
    border-right: 1px solid #D9E2EC;
}

QLabel#BrandTitle {
    color: #0F172A;
    font-size: 20px;
    font-weight: 700;
}

QLabel#BrandMeta,
QLabel#MutedText,
QLabel#FieldHint {
    color: #667085;
}

QLabel#PageEyebrow {
    color: #3182CE;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0;
}

QLabel#PageTitle {
    color: #111827;
    font-size: 26px;
    font-weight: 700;
}

QLabel#SectionTitle {
    color: #111827;
    font-size: 14px;
    font-weight: 700;
}

QLabel#ResultSuccess {
    background: #ECFDF5;
    color: #047857;
    border: 1px solid #A7F3D0;
    border-radius: 7px;
    padding: 8px 10px;
    font-weight: 700;
}

QLabel#ResultWarning {
    background: #FFFBEB;
    color: #B45309;
    border: 1px solid #FDE68A;
    border-radius: 7px;
    padding: 8px 10px;
    font-weight: 700;
}

QFrame#Card,
QFrame#CommandBar,
QFrame#Workspace,
QFrame#Pill,
QFrame#KpiTile {
    background: #FFFFFF;
    border: 1px solid #D9E2EC;
    border-radius: 8px;
}

QFrame#QueueTaskCard {
    background: #FFFFFF;
    border: 1px solid #D9E2EC;
    border-radius: 8px;
}

QFrame#QueueTaskCard[selected="true"] {
    background: #F8FBFF;
    border: 1px solid #BAE6FD;
}

QFrame#QueueTaskCard[blocked="true"] {
    background: #FFFBEB;
    border: 1px solid #FDE68A;
}

QLabel#ReadonlyField {
    background: #F8FAFC;
    border: 1px solid #D9E2EC;
    border-radius: 7px;
    padding: 9px 10px;
    color: #334155;
}

QFrame#Pill {
    padding: 4px 8px;
}

QComboBox#ModelRoleCombo {
    background: #FFFFFF;
    border: 1px solid #7DD3FC;
    border-radius: 7px;
    color: #075985;
    font-weight: 700;
    padding: 7px 9px;
}

QComboBox#ModelRoleCombo:hover {
    border-color: #0EA5E9;
}

QLabel#PillLabel {
    color: #667085;
    font-size: 10px;
}

QLabel#PillValue {
    color: #111827;
    font-size: 11px;
    font-weight: 600;
}

QLabel#TmMetricLabel {
    color: #667085;
    font-size: 12px;
    font-weight: 600;
}

QLabel#TmMetricValue {
    color: #111827;
    font-size: 13px;
    font-weight: 700;
}

QWidget[tmMetricPair="true"] {
    background: transparent;
}

QFrame#PhaseBadge {
    background: #EAF6FD;
    border: 1px solid #BDE3F8;
    border-radius: 8px;
}

QLabel#PhaseBadgeText {
    color: #0B6FA4;
    font-size: 12px;
    font-weight: 700;
}

QFrame#RecoveryCard {
    background: #FFFFFF;
    border: 1px solid #D9E2EC;
    border-radius: 8px;
}

QLabel#RecoveryTitle {
    color: #111827;
    font-size: 12px;
    font-weight: 700;
}

QLabel#RecoveryBadge {
    color: #0B6FA4;
    background: #EAF6FD;
    border: 1px solid #BDE3F8;
    border-radius: 6px;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 700;
}

QFrame#RecoveryMetric {
    background: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 7px;
}

QLabel#RecoveryMetricLabel {
    color: #667085;
    font-size: 10px;
}

QLabel#RecoveryMetricValue_active {
    color: #0B6FA4;
    font-size: 14px;
    font-weight: 700;
}

QLabel#RecoveryMetricValue_success {
    color: #047857;
    font-size: 14px;
    font-weight: 700;
}

QLabel#RecoveryMetricValue_warn {
    color: #B45309;
    font-size: 14px;
    font-weight: 700;
}

QPushButton {
    background: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 7px;
    padding: 8px 12px;
    color: #1A2035;
    font-weight: 600;
}

QPushButton:hover {
    background: #F8FAFC;
    border-color: #94A3B8;
}

QPushButton:disabled {
    background: #EDF2F7;
    color: #98A2B3;
}

QPushButton[compact="true"] {
    padding: 6px 10px;
}

QPushButton#UpdateNoticeButton {
    background: #EAF6FD;
    border-color: #7DD3FC;
    color: #075985;
    padding: 5px 8px;
    font-size: 12px;
}

QPushButton#UpdateNoticeButton:hover {
    background: #DDF2FC;
    border-color: #38BDF8;
}

QPushButton[updateIgnored="true"] {
    background: #FFFBEB;
    border-color: #FDE68A;
    color: #92400E;
}

QPushButton[updateIgnored="true"]:hover {
    background: #FEF3C7;
    border-color: #FBBF24;
}

QPushButton#PrimaryButton {
    background: #0EA5E9;
    border-color: #0EA5E9;
    color: #FFFFFF;
}

QPushButton#PrimaryButton:hover {
    background: #0284C7;
    border-color: #0284C7;
}

QPushButton#DangerButton {
    background: #EF4444;
    border-color: #EF4444;
    color: #FFFFFF;
}

QPushButton#NavButton {
    text-align: left;
    padding: 10px 12px;
}

QPushButton#NavButton:checked {
    background: #EAF6FD;
    border-color: #7DD3FC;
    color: #075985;
}

QLineEdit,
QTextEdit,
QPlainTextEdit,
QComboBox,
QSpinBox {
    background: #FFFFFF;
    border: 1px solid #CBD5E1;
    border-radius: 7px;
    padding: 7px 9px;
    selection-background-color: #0EA5E9;
}

QTextEdit,
QPlainTextEdit {
    padding: 8px;
}

QComboBox {
    combobox-popup: 0;
    min-height: 22px;
}

QComboBox[compact="true"],
QSpinBox[compact="true"] {
    min-height: 18px;
    padding: 5px 8px;
}

QFrame[tmTopCard="true"] QLabel[tmFieldLabel="true"],
QFrame[tmTopCard="true"] QComboBox,
QFrame[tmTopCard="true"] QSpinBox,
QFrame[tmTopCard="true"] QPushButton,
QFrame[tmTopCard="true"] QCheckBox {
    font-size: 12px;
}

QFrame[tmTopCard="true"] QComboBox[compact="true"],
QFrame[tmTopCard="true"] QSpinBox[compact="true"] {
    min-height: 18px;
    padding: 4px 7px;
}

QFrame[tmTopCard="true"] QPushButton[compact="true"] {
    padding: 5px 8px;
}

QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #CBD5E1;
    outline: 0;
    selection-background-color: #EAF6FD;
    selection-color: #075985;
    padding: 4px;
}

QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 4px 8px;
}

QCheckBox,
QRadioButton {
    spacing: 8px;
}

QCheckBox::indicator,
QTableView::indicator,
QTableWidget::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #B8C7D9;
    border-radius: 4px;
    background: #FFFFFF;
}

QCheckBox::indicator:hover,
QTableView::indicator:hover,
QTableWidget::indicator:hover {
    border-color: #0EA5E9;
}

QCheckBox::indicator:checked,
QTableView::indicator:checked,
QTableWidget::indicator:checked {
    border-color: #0EA5E9;
    background: #0EA5E9;
    image: url("__CHECK_ICON_URL__");
}

QCheckBox::indicator:indeterminate,
QTableView::indicator:indeterminate,
QTableWidget::indicator:indeterminate {
    border-color: #0EA5E9;
    background: #BAE6FD;
}

QCheckBox::indicator:disabled,
QTableView::indicator:disabled,
QTableWidget::indicator:disabled {
    border-color: #CBD5E1;
    background: #F1F5F9;
}

QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    background: #FFFFFF;
}

QRadioButton::indicator:hover {
    border-color: #0EA5E9;
}

QRadioButton::indicator:checked {
    border: 1px solid #0EA5E9;
    border-radius: 8px;
    background: #0EA5E9;
    image: url("__RADIO_DOT_ICON_URL__");
}


QTableWidget {
    background: #FFFFFF;
    border: none;
    gridline-color: #E2E8F0;
    alternate-background-color: #F8FAFC;
}

QTableWidget[appTable="true"] {
    outline: 0;
    selection-background-color: #EAF6FD;
    selection-color: #0F172A;
}

QTableWidget[appTable="true"]::item {
    padding: 7px 8px;
}

QTableWidget[appTable="true"]::item:selected,
QTableWidget[appTable="true"]::item:selected:active,
QTableWidget[appTable="true"]::item:selected:!active {
    background: #EAF6FD;
    color: #0F172A;
}

QTableWidget[appTable="true"]::item:focus {
    background: #FFFFFF;
    color: #0F172A;
    border: 1px solid #7DD3FC;
}

QTableWidget[appTable="true"] QLineEdit {
    background: #FFFFFF;
    border: 1px solid #7DD3FC;
    border-radius: 6px;
    padding: 6px 8px;
    color: #0F172A;
    selection-background-color: #BAE6FD;
    selection-color: #0F172A;
}

QTableWidget[appTable="true"] QLineEdit:focus {
    border-color: #0EA5E9;
}

QHeaderView::section {
    background: #F1F5F9;
    color: #475467;
    border: none;
    border-bottom: 1px solid #D9E2EC;
    padding: 8px;
    font-weight: 700;
}

QProgressBar {
    background: #E2E8F0;
    border: none;
    border-radius: 6px;
    height: 16px;
    text-align: center;
}

QProgressBar::chunk {
    background: #0EA5E9;
    border-radius: 6px;
}

QToolTip {
    background: #111827;
    color: #FFFFFF;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px;
    font-size: 12px;
}

QFrame#InAppToolTip {
    background: #111827;
    border: 1px solid #334155;
    border-radius: 6px;
}

QLabel#InAppToolTipText {
    background: transparent;
    color: #FFFFFF;
    font-size: 12px;
}
""".replace("__CHECK_ICON_URL__", _asset_url("check-white.svg")).replace(
    "__RADIO_DOT_ICON_URL__",
    _asset_url("radio-dot-white.svg"),
)
