from __future__ import annotations

import os
import platform
import sys
import webbrowser
from collections.abc import Callable

from app_meta import APP_NAME

BROWSER_FALLBACK_ENV = "XL_TRANSLATOR_OPEN_BROWSER"
WEBVIEW_DEBUG_ENV = "XL_TRANSLATOR_WEBVIEW_DEBUG"
MACOS_BROWSER_FALLBACK_MAX_MAJOR = 12

WINDOW_WIDTH = 1320
WINDOW_HEIGHT = 880
WINDOW_MIN_WIDTH = 1040
WINDOW_MIN_HEIGHT = 700


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _log(log_callback: Callable[[str], None] | None, message: str) -> None:
    if log_callback is not None:
        log_callback(message)


def _parse_macos_major_version(version: str) -> int | None:
    normalized = str(version or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized.split(".", 1)[0])
    except ValueError:
        return None


def should_use_system_browser_for_macos(
    *,
    platform_name: str | None = None,
    macos_version: str | None = None,
) -> bool:
    platform_name = platform_name or sys.platform
    if platform_name != "darwin":
        return False

    version = macos_version if macos_version is not None else platform.mac_ver()[0]
    major_version = _parse_macos_major_version(version)
    return (
        major_version is not None
        and major_version <= MACOS_BROWSER_FALLBACK_MAX_MAJOR
    )


def open_system_browser(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    webbrowser.open(url)


def _create_window(webview_module, url: str):
    try:
        return webview_module.create_window(
            APP_NAME,
            url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        )
    except TypeError:
        return webview_module.create_window(
            APP_NAME,
            url,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
        )


def open_app_window(
    url: str,
    *,
    log_callback: Callable[[str], None] | None = None,
) -> bool:
    """Open the local Streamlit URL in an in-app WebView window.

    Returns True when the call blocks until the WebView window closes. Returns
    False when falling back to the external browser, whose lifecycle is not
    controlled by this process.
    """
    if _env_truthy(BROWSER_FALLBACK_ENV):
        _log(
            log_callback,
            f"{BROWSER_FALLBACK_ENV}=1 detected. Opening system browser: {url}",
        )
        open_system_browser(url)
        return False

    if should_use_system_browser_for_macos():
        _log(
            log_callback,
            "macOS 12 or earlier detected. Opening system browser "
            f"instead of WebView: {url}",
        )
        open_system_browser(url)
        return False

    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        _log(
            log_callback,
            f"PyWebView is unavailable ({exc}). Falling back to system browser: {url}",
        )
        open_system_browser(url)
        return False

    try:
        _log(log_callback, f"Opening app window: {url}")
        _create_window(webview, url)
        if _env_truthy(WEBVIEW_DEBUG_ENV):
            webview.start(debug=True)
        else:
            webview.start()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(
            log_callback,
            f"Unable to open app window ({exc}). Falling back to system browser: {url}",
        )
        open_system_browser(url)
        return False
