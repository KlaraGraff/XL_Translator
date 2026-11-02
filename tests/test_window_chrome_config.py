"""窗口顶部（ui/src/window-chrome.ts）依赖的 Tauri 配置。

Windows 的配置文件按 JSON Merge Patch 合并进 tauri.conf.json，数组整个替换，所以
tauri.windows.conf.json 里的主窗口是一份完整拷贝。这里确保两份除了平台专属的几项外一致，
改了窗口尺寸之类只改一处时会报出来。
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src-tauri"
MAC_ONLY = {"titleBarStyle", "hiddenTitle", "trafficLightPosition"}


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _main_window(config: dict) -> dict:
    windows = config["app"]["windows"]
    assert len(windows) == 1
    return windows[0]


def test_mac_window_hides_the_title_bar_and_keeps_traffic_lights():
    window = _main_window(_load("tauri.conf.json"))
    assert window["titleBarStyle"] == "Overlay"
    assert window["hiddenTitle"] is True
    assert set(window["trafficLightPosition"]) == {"x", "y"}
    assert window.get("decorations", True) is True


def test_windows_window_is_the_same_window_without_system_title_bar():
    base = _main_window(_load("tauri.conf.json"))
    win = _main_window(_load("tauri.windows.conf.json"))
    assert win.pop("decorations") is False
    expected = {k: v for k, v in base.items() if k not in MAC_ONLY}
    assert win == expected


def test_main_window_may_drag_and_use_its_own_caption_buttons():
    permissions = set(_load("capabilities/default.json")["permissions"])
    for needed in (
        "core:window:allow-start-dragging",
        "core:window:allow-internal-toggle-maximize",
        "core:window:allow-minimize",
        "core:window:allow-toggle-maximize",
        "core:window:allow-close",
        "core:window:allow-is-maximized",
    ):
        assert needed in permissions
