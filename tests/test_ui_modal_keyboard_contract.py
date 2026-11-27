"""弹窗的键盘行为、停止横幅认得出「用户自己停的」——源码约定。

两处都在真浏览器里验过（Playwright）；这里用便宜的源码检查守住，免得以后改丢：

- ``openModal`` 打开后焦点要进弹窗：以前留在后面的按钮上，再按回车同一个弹窗会叠出好几个。
  Esc 等于点「取消 / 关闭 / 知道了」（或标了 ``dismiss`` 的按钮），只有最上面一层响应。
- 工作区收到「stopping」事件要记下 ``stop_requested``：任务状态是开始时拿到的那份，不记的话
  结束横幅认不出是用户停的，会把剩余数量说两遍。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _open_modal_source() -> str:
    components = (ROOT / "ui" / "src" / "components.ts").read_text(encoding="utf-8")
    start = components.index("export function openModal(")
    return components[start : components.index("\n}\n", start)]


def test_open_modal_moves_focus_in_and_closes_on_escape() -> None:
    body = _open_modal_source()
    assert 'modal.setAttribute("role", "dialog")' in body
    assert "(confirmInputEl ?? modal).focus()" in body
    assert 'event.key !== "Escape"' in body
    # 只认最上面一层弹窗，且关掉时要摘掉监听、把焦点还给打开它的控件。
    assert "overlays[overlays.length - 1] !== overlay" in body
    assert 'document.removeEventListener("keydown", onKey)' in body
    assert "opener.focus?.()" in body


def test_escape_never_triggers_a_primary_or_danger_action_by_default() -> None:
    body = _open_modal_source()
    assert 'action.dismiss ?? (index === 0 && (!action.variant || action.variant === "default") && !action.keepOpen' in body
    assert 'plainDismiss = [t("取消"), t("关闭"), t("知道了")]' in body


def test_stop_confirm_dialogs_mark_keep_running_as_the_dismiss_action() -> None:
    for name in ("workspace.ts", "tasks.ts"):
        source = (ROOT / "ui" / "src" / "views" / name).read_text(encoding="utf-8")
        line = next(line for line in source.splitlines() if 'label: t("继续执行")' in line)
        assert "dismiss: true" in line, f"{name}: {line.strip()}"


def test_workspace_records_user_stop_from_the_stopping_event() -> None:
    workspace = (ROOT / "ui" / "src" / "views" / "workspace.ts").read_text(encoding="utf-8")
    start = workspace.index('case "stopping":\n      syncEventState(local, data, "stopping");')
    assert "stop_requested: true" in workspace[start : start + 600]
    # 横幅那头仍然靠这个标记决定只说一句话。
    assert "const userStopped = task.stop_requested !== false;" in workspace
