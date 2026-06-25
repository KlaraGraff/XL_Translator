"""Native PySide6 application entry point."""

from __future__ import annotations

import sys

from loguru import logger
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

from app_meta import APP_NAME
from core.data_migration import inspect_data_migration, migrate_non_conflicting_legacy_data
from core.tm_manager import init_db
from native_app.main_window import NativeMainWindow
from native_app.style import APP_QSS
from native_app.widgets import install_in_app_tooltips, install_scroll_wheel_focus_guard
from settings import load_settings, save_settings


class FastToolTipStyle(QProxyStyle):
    """Use shorter, snappier tooltips than the platform default."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return 220
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return 2400
        return super().styleHint(hint, option, widget, returnData)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle(FastToolTipStyle(app.style()))
    app.setStyleSheet(APP_QSS)
    install_scroll_wheel_focus_guard(app)
    install_in_app_tooltips(app)

    try:
        plan = inspect_data_migration()
        migrate_non_conflicting_legacy_data(plan)
    except Exception as exc:
        logger.warning(f"旧数据补迁移失败，已跳过并继续启动：{exc}")

    settings = load_settings()
    init_db()

    window = NativeMainWindow(settings)
    window.apply_initial_window_layout()
    window.show()

    exit_code = app.exec()
    save_settings(window.settings)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
