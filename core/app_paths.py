"""Application data paths for the current Translator baseline."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from app_meta import APP_NAME

APP_DATA_DIR_ENV = "TRANSLATOR_APP_DATA_DIR"
def _home_dir(home: str | Path | None = None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def get_app_data_dir(
    *,
    system: str | None = None,
    home: str | Path | None = None,
    local_app_data: str | Path | None = None,
    xdg_data_home: str | Path | None = None,
) -> Path:
    """Return the platform-native data directory for the current app name."""
    override = os.environ.get(APP_DATA_DIR_ENV)
    if override:
        return Path(override).expanduser()

    current_system = system or platform.system()
    home_path = _home_dir(home)

    if current_system == "Windows":
        base = (
            Path(local_app_data).expanduser()
            if local_app_data is not None
            else Path(os.environ.get("LOCALAPPDATA") or home_path / "AppData" / "Local")
        )
        return base / APP_NAME

    if current_system == "Darwin":
        return home_path / "Library" / "Application Support" / APP_NAME

    base = (
        Path(xdg_data_home).expanduser()
        if xdg_data_home is not None
        else Path(os.environ.get("XDG_DATA_HOME") or home_path / ".local" / "share")
    )
    return base / APP_NAME
