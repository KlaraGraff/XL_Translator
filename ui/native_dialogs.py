"""Native file/folder picker helpers for the local desktop app."""

from __future__ import annotations

import subprocess
from pathlib import Path

_MACOS_FOLDER_PICKER_SCRIPT = r"""
on run argv
    set initialDir to ""
    if (count of argv) > 0 then
        set initialDir to item 1 of argv
    end if
    if initialDir is not "" then
        set chosenItem to choose folder with prompt "选择源文件夹" default location ((POSIX file initialDir) as alias)
    else
        set chosenItem to choose folder with prompt "选择源文件夹"
    end if
    return POSIX path of chosenItem
end run
"""

_MACOS_EXCEL_FILE_PICKER_SCRIPT = r"""
on run argv
    set initialDir to ""
    set promptText to "选择 Excel 文件（.xlsx / .xls）"
    if (count of argv) > 0 then
        set initialDir to item 1 of argv
    end if
    if initialDir is not "" then
        set chosenItem to choose file with prompt promptText default location ((POSIX file initialDir) as alias)
    else
        set chosenItem to choose file with prompt promptText
    end if
    return POSIX path of chosenItem
end run
"""

_MACOS_WORD_FILE_PICKER_SCRIPT = r"""
on run argv
    set initialDir to ""
    set promptText to "选择 Word 文件（.docx）"
    if (count of argv) > 0 then
        set initialDir to item 1 of argv
    end if
    if initialDir is not "" then
        set chosenItem to choose file with prompt promptText default location ((POSIX file initialDir) as alias)
    else
        set chosenItem to choose file with prompt promptText
    end if
    return POSIX path of chosenItem
end run
"""


def pick_folder(initial_path: str | Path | None = None) -> str | None:
    """Open a native folder picker and return the chosen path."""
    return _run_picker(script=_MACOS_FOLDER_PICKER_SCRIPT, initial_path=initial_path)


def pick_excel_file(initial_path: str | Path | None = None) -> str | None:
    """Open a native Excel file picker and return the chosen path."""
    return _run_picker(script=_MACOS_EXCEL_FILE_PICKER_SCRIPT, initial_path=initial_path)


def pick_word_file(initial_path: str | Path | None = None) -> str | None:
    """Open a native Word file picker and return the chosen path."""
    return _run_picker(script=_MACOS_WORD_FILE_PICKER_SCRIPT, initial_path=initial_path)


def _run_picker(*, script: str, initial_path: str | Path | None) -> str | None:
    initial_dir = _resolve_initial_directory(initial_path)
    return _run_macos_picker(script, initial_dir)


def _run_macos_picker(script: str, initial_dir: str) -> str | None:
    command = ["osascript", "-"]
    if initial_dir:
        command.append(initial_dir)
    result = subprocess.run(
        command,
        input=script,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        if _is_user_cancelled(details):
            return None
        raise RuntimeError(details or "无法打开系统选择窗口。")

    selected_path = (result.stdout or "").strip()
    return selected_path or None


def _is_user_cancelled(details: str) -> bool:
    normalized = (details or "").lower()
    return "-128" in normalized or "user canceled" in normalized or "cancelled" in normalized


def _resolve_initial_directory(initial_path: str | Path | None) -> str:
    if not initial_path:
        return ""

    candidate = Path(str(initial_path).strip().strip('"')).expanduser()
    if candidate.is_file():
        candidate = candidate.parent

    current = candidate
    while True:
        if current.exists() and current.is_dir():
            return str(current.resolve())
        if current.parent == current:
            return ""
        current = current.parent
