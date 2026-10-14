"""「扩展」页的安装任务，以及把装进来的扩展注册成翻译类型（``ext:<id>``）。

文件层面的检查、拷贝和登记在 ``core/adapter_install.py``；这里接上两件 ``core`` 做不了的事：

- 注册 / 注销页面：装好后立即出现在 ``GET /api/surfaces``（侧栏、任务中心、命令行、MCP 都认），
  卸载后立即消失；应用启动时 ``register_installed_adapters()`` 把已装的逐个注册，坏的跳过并记日志。
- 有任务的类型不能换：同一类型还有任务在跑或暂停时，拒绝更新和卸载（旧目录会被删掉）。

安装走与插件页相同的后台任务样式（``InstallJob``：进度、取消、结果轮询），同一时间只装一个。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from api.component_install_job import InstallJob
from api.surfaces import all_surfaces, register_surface, unregister_surface
from api.surfaces.adapter import AdapterSurface
from core import adapter_install
from core.adapter_install import AdapterInstallError
from core.format_adapter import AdapterHost, AdapterManifest, InProcessAdapterHost

#: ``AdapterSurface.origin`` of a type installed by the user.
INSTALLED_ORIGIN = "installed"

ProgressFn = Callable[[str, int, int], None]


def make_host(directory: Path, manifest: AdapterManifest) -> AdapterHost:
    """装进来的扩展用哪种宿主（只在这一处决定）。"""
    return InProcessAdapterHost(directory, manifest)


def _registered() -> dict[str, Any]:
    return {surface.key: surface for surface in all_surfaces()}


def reserved_ids() -> set[str]:
    """装进来的扩展不能占用的 id：随应用提供的类型，以及其他非安装来源已注册的类型。"""
    ids = adapter_install.bundled_ids()
    for surface in all_surfaces():
        if isinstance(surface, AdapterSurface) and surface.origin != INSTALLED_ORIGIN:
            ids.add(surface.manifest.id)
    return ids


def activate(directory: Path, manifest: AdapterManifest) -> AdapterSurface:
    """把装好的扩展注册成翻译类型；同一 id 之前装的那一份先注销。"""
    existing = _registered().get(manifest.key)
    if existing is not None:
        if not (isinstance(existing, AdapterSurface) and existing.origin == INSTALLED_ORIGIN):
            raise AdapterInstallError(f"id「{manifest.id}」与应用自带的翻译类型相同，请换一个 id")
        unregister_surface(manifest.key)
    surface = AdapterSurface(manifest, make_host(directory, manifest), origin=INSTALLED_ORIGIN)
    register_surface(surface)
    return surface


def deactivate(adapter_id: str) -> None:
    key = f"ext:{adapter_id}"
    surface = _registered().get(key)
    if isinstance(surface, AdapterSurface) and surface.origin == INSTALLED_ORIGIN:
        unregister_surface(key)


def register_installed_adapters() -> list[str]:
    """应用启动时注册所有已装的扩展（在随应用提供的类型之后）；返回这次注册的页面键。

    装坏的（目录丢了、清单读不动、与本应用不兼容）和与已注册类型重名的跳过并记日志，
    不影响启动。
    """
    added: list[str] = []
    for item in adapter_install.installed_adapters():
        if item.manifest is None:
            logger.warning(f"[扩展] 跳过已安装的扩展 {item.id}：{item.problem}")
            continue
        existing = _registered().get(item.manifest.key)
        if existing is not None:
            if not (isinstance(existing, AdapterSurface) and existing.origin == INSTALLED_ORIGIN):
                logger.warning(f"[扩展] 跳过已安装的扩展 {item.id}：与应用自带的翻译类型同名")
            continue
        try:
            activate(item.directory, item.manifest)
        except (AdapterInstallError, ValueError) as exc:
            logger.warning(f"[扩展] 无法注册已安装的扩展 {item.id}：{exc}")
            continue
        added.append(item.manifest.key)
    if added:
        logger.info(f"[扩展] 已注册已安装的翻译类型：{', '.join(added)}")
    return added


def _reactivate(adapter_id: str) -> None:
    """更新失败时把仍登记着的那一份重新注册回来（旧目录没删时它还能用）。"""
    for item in adapter_install.installed_adapters():
        if item.id == adapter_id and item.manifest is not None:
            try:
                activate(item.directory, item.manifest)
            except (AdapterInstallError, ValueError) as exc:
                logger.warning(f"[扩展] 无法恢复 {adapter_id}：{exc}")
            return


def _busy_check(active_surfaces: Callable[[], frozenset[str]]) -> Callable[[AdapterManifest], None]:
    """换上新目录之前：先注销旧的（不再接新任务），再看还有没有任务在用它。"""

    def check(manifest: AdapterManifest) -> None:
        was_registered = manifest.key in _registered()
        deactivate(manifest.id)
        if manifest.key in active_surfaces():
            if was_registered:
                _reactivate(manifest.id)
            label = manifest.label.get("zh") or manifest.id
            raise AdapterInstallError(f"「{label}」还有任务在进行或已暂停，请等任务结束后再更新")

    return check


def install_and_activate(
    source: str | Path,
    *,
    active_surfaces: Callable[[], frozenset[str]],
    expected_sha256: str = "",
    origin: str = adapter_install.ORIGIN_LOCAL,
    source_label: str = "",
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """安装（调用方已取得用户确认）并注册；返回登记项。"""
    if progress:
        progress("install", 0, 1)
    adapter_id = ""

    def before_replace(manifest: AdapterManifest) -> None:
        nonlocal adapter_id
        adapter_id = manifest.id
        _busy_check(active_surfaces)(manifest)

    try:
        record = adapter_install.install(
            source,
            confirmed=True,
            expected_sha256=expected_sha256,
            reserved_ids=reserved_ids(),
            origin=origin,
            source_label=source_label,
            cancel=cancel,
            before_replace=before_replace,
        )
    except BaseException:
        if adapter_id and f"ext:{adapter_id}" not in _registered():
            _reactivate(adapter_id)
        raise
    for item in adapter_install.installed_adapters():
        if item.id == record["id"]:
            if item.manifest is None:
                raise AdapterInstallError(item.problem or "扩展已复制，但无法读取")
            activate(item.directory, item.manifest)
            break
    if progress:
        progress("done", 1, 1)
    return record


def uninstall_and_deactivate(adapter_id: str, *, active_surfaces: Callable[[], frozenset[str]]) -> dict[str, Any]:
    """注销并删掉我们的拷贝；类型还有任务时拒绝。没装过时抛 ``AdapterInstallError``。"""
    adapter_id = str(adapter_id or "").strip().lower()
    record = adapter_install.read_registry().get(adapter_id)
    if record is None:
        raise AdapterInstallError("没有找到这个扩展，可能已被卸载")
    key = f"ext:{adapter_id}"
    was_registered = key in _registered()
    deactivate(adapter_id)
    if key in active_surfaces():
        if was_registered:
            _reactivate(adapter_id)
        label = (record.get("label") or {}).get("zh") or adapter_id
        raise AdapterInstallError(f"「{label}」还有任务在进行或已暂停，请等任务结束后再卸载")
    adapter_install.uninstall(adapter_id)
    return record


class ExtensionInstallJob(InstallJob):
    """同一时间只装一个扩展；进度里带 ``result``（装好的登记项）供界面提示。"""

    def __init__(self) -> None:
        self._work: Callable[..., dict[str, Any]] | None = None
        self._begin_lock = threading.Lock()
        super().__init__("扩展", lambda: self._run_work, lambda: AdapterInstallError)

    def _run_work(self, *, progress: ProgressFn, cancel: threading.Event) -> None:
        work = self._work
        if work is None:
            raise AdapterInstallError("扩展安装失败。")
        try:
            result = work(progress=progress, cancel=cancel)
        except AdapterInstallError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure becomes a message on the page
            logger.exception("[扩展] 安装失败")
            raise AdapterInstallError("扩展安装失败。") from exc
        self._set(result=result)

    def begin(self, work: Callable[..., dict[str, Any]], *, label: str = "", total: int = 1) -> dict[str, Any]:
        with self._begin_lock:
            if self.running():
                raise AdapterInstallError("正在安装另一个扩展，请稍候")
            self._work = work
            self._set(result=None, label=label)
            return self.start(total)


JOB = ExtensionInstallJob()


__all__ = [
    "INSTALLED_ORIGIN",
    "JOB",
    "ExtensionInstallJob",
    "activate",
    "deactivate",
    "install_and_activate",
    "make_host",
    "register_installed_adapters",
    "reserved_ids",
    "uninstall_and_deactivate",
]
