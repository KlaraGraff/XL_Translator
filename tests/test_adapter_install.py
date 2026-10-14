"""Adding a third-party format adapter from a folder or zip (core/adapter_install.py).

The package is the test ``echo`` adapter under another id (``ext:linetext``),
so nothing here collides with the fixture other tests register.  These pin
the whole path the 扩展 page takes: preview → confirm → install → the type is
listed by ``GET /api/surfaces`` → a task runs on it → uninstall; plus every
check that refuses a package, the upgrade in place, and a damaged registry
at start-up that must never stop the app.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import all_surfaces, unregister_surface
from api.surfaces.adapter import AdapterSurface
from core import adapter_install
from core.adapter_install import AdapterInstallError, inspect_package
from core.adapter_subprocess_host import SubprocessAdapterHost
from tests.test_format_adapter import ECHO_DIR

KEY = "ext:linetext"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import settings as settings_module

    data = tmp_path / "app-data"
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(data))
    for name, value in (
        ("APP_DATA_DIR", data),
        ("SETTINGS_PATH", data / "settings.json"),
        ("KEYS_PATH", data / "keys.json"),
        ("RECOVERY_PATH", data / "recovery.json"),
        ("BACKUPS_DIR", data / "backups"),
    ):
        monkeypatch.setattr(settings_module, name, value)
    unregister_surface(KEY)
    yield data
    unregister_surface(KEY)


def make_package(root: Path, *, version: str = "1.0.0", **manifest_overrides) -> Path:
    """The echo adapter as ``linetext``, with a sample and the usual clutter of a work folder."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ECHO_DIR / "adapter.json").read_text(encoding="utf-8"))
    manifest.update(
        id="linetext",
        version=version,
        label={"zh": "按行文本", "en": "Line text", "fr": "Texte par ligne"},
        **manifest_overrides,
    )
    (root / "adapter.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(ECHO_DIR / "adapter.py", root / "adapter.py")
    (root / "README.md").write_text("# linetext\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "samples").mkdir(exist_ok=True)
    (root / "samples" / "hello.txt").write_text("Hello\n", encoding="utf-8")
    # Not part of the package: hidden files, caches, build output, the author's scripts.
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: main\n", encoding="utf-8")
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "adapter.cpython-311.pyc").write_bytes(b"\x00" * 16)
    (root / "dist").mkdir(exist_ok=True)
    (root / "dist" / "old.zip").write_bytes(b"PK")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "release.py").write_text("import subprocess\n", encoding="utf-8")
    return root


def zip_package(source: Path, target: Path, *, prefix: str = "linetext-main/") -> Path:
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, prefix + path.relative_to(source).as_posix())
        archive.writestr("__MACOSX/._adapter.py", b"\x00\x05\x16\x07")
    return target


def _wait_install(client: TestClient) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        progress = client.get("/api/extensions/install-progress").json()
        if not progress["running"]:
            return progress
        time.sleep(0.02)
    raise TimeoutError("install")


def _install(client: TestClient, path: Path) -> dict:
    preview = client.post("/api/extensions/preview", json={"path": str(path)})
    assert preview.status_code == 200, preview.text
    started = client.post(
        "/api/extensions/install", json={"path": str(path), "confirmed": True, "sha256": preview.json()["sha256"]}
    )
    assert started.status_code == 200, started.text
    progress = _wait_install(client)
    assert progress["phase"] == "done", progress
    return progress


def _surface_keys(client: TestClient) -> list[str]:
    return [item["key"] for item in client.get("/api/surfaces").json()["surfaces"]]


# ── preview and install ─────────────────────────────────────────────────────


def test_preview_describes_the_package_and_changes_nothing(tmp_path: Path, isolated: Path) -> None:
    package = make_package(tmp_path / "linetext")
    summary = adapter_install.preview(package)
    assert summary["id"] == "linetext"
    assert summary["key"] == KEY
    assert summary["action"] == "install"
    assert summary["kind"] == "folder"
    # Samples are checked but not installed; clutter is left out entirely.
    assert summary["files"] == ["LICENSE", "README.md", "adapter.json", "adapter.py"]
    assert summary["sample_count"] == 1
    assert summary["label"]["zh"] == "按行文本"
    assert len(summary["sha256"]) == 64
    assert not (isolated / "plugins").exists()


def test_install_from_a_folder_registers_the_type(tmp_path: Path, isolated: Path) -> None:
    package = make_package(tmp_path / "linetext")
    with TestClient(create_app()) as client:
        progress = _install(client, package)
        assert progress["result"]["id"] == "linetext"
        surfaces = client.get("/api/surfaces").json()["surfaces"]
        installed = client.get("/api/extensions/installed").json()["extensions"]

    descriptor = next(item for item in surfaces if item["key"] == KEY)
    assert descriptor["adapter"]["origin"] == "installed"
    assert descriptor["label"]["en"] == "Line text"
    assert [(item["id"], item["ok"], item["registered"]) for item in installed] == [("linetext", True, True)]

    record = json.loads((isolated / "plugins" / "adapters" / "installed.json").read_text(encoding="utf-8"))
    entry = record["adapters"]["linetext"]
    assert entry["version"] == "1.0.0"
    assert entry["origin"] == "local"
    assert entry["source"] == str(package)
    target = isolated / "plugins" / "adapters" / entry["dir"]
    assert entry["dir"].startswith("linetext/1.0.0-")
    assert sorted(path.name for path in target.iterdir()) == ["LICENSE", "README.md", "adapter.json", "adapter.py"]
    # The user's folder is left as it was.
    assert (package / "samples" / "hello.txt").is_file()


def test_a_new_extension_shows_in_the_sidebar_unless_the_user_chose_otherwise(tmp_path: Path, isolated: Path) -> None:
    from settings import load_settings, save_settings

    with TestClient(create_app()) as client:
        _install(client, make_package(tmp_path / "v1"))
        assert load_settings().translation_types.visible[KEY] is True
        settings = load_settings()
        settings.translation_types.visible[KEY] = False  # the user hides it on the 「扩展」 page
        save_settings(settings)
        _install(client, make_package(tmp_path / "v2", version="1.1.0"))
        assert load_settings().translation_types.visible[KEY] is False  # an update keeps the choice
        assert client.post("/api/extensions/linetext/uninstall").status_code == 200
        _install(client, make_package(tmp_path / "v3"))
        assert load_settings().translation_types.visible[KEY] is False  # so does a reinstall


def test_install_from_a_zip_with_a_top_level_folder(tmp_path: Path, isolated: Path) -> None:
    archive = zip_package(make_package(tmp_path / "linetext"), tmp_path / "linetext.zip")
    summary = adapter_install.preview(archive)
    assert summary["kind"] == "zip"
    assert summary["files"] == ["LICENSE", "README.md", "adapter.json", "adapter.py"]
    with TestClient(create_app()) as client:
        _install(client, archive)
        assert KEY in _surface_keys(client)
    record = adapter_install.read_registry()["linetext"]
    assert record["sha256"] == adapter_install.file_sha256(archive)


def test_install_needs_confirmation(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    with pytest.raises(AdapterInstallError):
        adapter_install.install(package, confirmed=False)
    with TestClient(create_app()) as client:
        refused = client.post("/api/extensions/install", json={"path": str(package)})
        assert refused.status_code == 422
        assert refused.json()["reason"] == "extension_unconfirmed"
        assert KEY not in _surface_keys(client)
    assert adapter_install.read_registry() == {}


def test_changed_after_preview_is_refused(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    sha = adapter_install.preview(package)["sha256"]
    (package / "adapter.py").write_text((package / "adapter.py").read_text(encoding="utf-8") + "\n# changed\n")
    with pytest.raises(AdapterInstallError, match="发生了变化"):
        adapter_install.install(package, confirmed=True, expected_sha256=sha)


def test_upgrade_replaces_the_installed_version(tmp_path: Path, isolated: Path) -> None:
    with TestClient(create_app()) as client:
        _install(client, make_package(tmp_path / "v1"))
        newer = make_package(tmp_path / "v2", version="1.1.0")
        preview = client.post("/api/extensions/preview", json={"path": str(newer)}).json()
        assert preview["action"] == "upgrade"
        assert preview["installed_version"] == "1.0.0"
        assert "1.0.0" in preview["warnings"][0] and "1.1.0" in preview["warnings"][0]
        _install(client, newer)
        descriptor = next(item for item in client.get("/api/surfaces").json()["surfaces"] if item["key"] == KEY)
    assert descriptor["adapter"]["version"] == "1.1.0"
    versions = [path.name for path in (isolated / "plugins" / "adapters" / "linetext").iterdir()]
    assert len(versions) == 1 and versions[0].startswith("1.1.0-")
    assert adapter_install.read_registry()["linetext"]["version"] == "1.1.0"


def test_uninstall_removes_the_type_and_our_copy_but_keeps_its_settings(tmp_path: Path, isolated: Path) -> None:
    import settings as settings_module
    from core.surface_settings import set_surface_setting

    with TestClient(create_app()) as client:
        _install(client, make_package(tmp_path / "linetext"))
        stored = settings_module.load_settings()
        set_surface_setting(stored, KEY, "target_lang", "de")
        settings_module.save_settings(stored)

        removed = client.post("/api/extensions/linetext/uninstall")
        assert removed.status_code == 200, removed.text
        assert removed.json()["uninstalled"]["id"] == "linetext"
        assert KEY not in _surface_keys(client)
        assert client.get("/api/extensions/installed").json()["extensions"] == []
        again = client.post("/api/extensions/linetext/uninstall")
        assert again.status_code == 404
    assert not (isolated / "plugins" / "adapters" / "linetext").exists()
    assert settings_module.load_settings().extensions["linetext"].target_lang == "de"


def test_a_type_in_use_is_not_replaced_or_removed(tmp_path: Path) -> None:
    from api import extension_install_job as jobs

    jobs.install_and_activate(make_package(tmp_path / "v1"), active_surfaces=frozenset)
    busy = lambda: frozenset({KEY})  # noqa: E731
    with pytest.raises(AdapterInstallError, match="还有任务"):
        jobs.install_and_activate(make_package(tmp_path / "v2", version="2.0.0"), active_surfaces=busy)
    with pytest.raises(AdapterInstallError, match="还有任务"):
        jobs.uninstall_and_deactivate("linetext", active_surfaces=busy)
    # The installed version is still registered and still the one on record.
    surface = next(item for item in all_surfaces() if item.key == KEY)
    assert isinstance(surface, AdapterSurface) and surface.manifest.version == "1.0.0"
    assert adapter_install.read_registry()["linetext"]["version"] == "1.0.0"


# ── what a package may contain ──────────────────────────────────────────────


def _problems(package: Path, **kwargs) -> str:
    report = inspect_package(package, **kwargs)
    assert not report.ok
    return "；".join(report.problems)


def test_an_id_shipped_with_the_app_is_refused(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    assert "应用自带" in _problems(package, reserved_ids={"linetext"})


def test_oversized_package_is_refused(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "samples" / "big.txt").write_bytes(b"x" * (adapter_install.MAX_PACKAGE_BYTES + 1))
    assert "扩展包过大" in _problems(package)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("helper.so", b"\x7fELF\x02\x01"),
        ("samples/tool.txt", b"MZ\x90\x00"),  # an executable hiding behind a harmless name
        ("lib.dylib", b"\xcf\xfa\xed\xfe"),
    ],
)
def test_native_code_is_refused(tmp_path: Path, name: str, content: bytes) -> None:
    package = make_package(tmp_path / "linetext")
    (package / name).write_bytes(content)
    assert "原生库或可执行文件" in _problems(package)


def test_other_file_types_are_refused_outside_samples(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "data.csv").write_text("a,b\n", encoding="utf-8")
    (package / "samples" / "data.csv").write_text("a,b\n", encoding="utf-8")
    problems = _problems(package)
    assert "data.csv" in problems and "samples/data.csv" not in problems


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import socket\n", "导入了 socket（网络访问）"),
        ("from urllib.request import urlopen\n", "导入了 urllib.request（网络访问）"),
        ("import subprocess\n", "导入了 subprocess（启动其他程序）"),
        ("import ctypes\n", "导入了 ctypes（加载原生代码）"),
        ("import importlib\n", "导入了 importlib（动态加载代码）"),
        ("from os import system\n", "导入了 os.system（启动其他程序）"),
        ("import os\nos.execv('/bin/sh', [])\n", "使用了 os.execv（启动其他程序）"),
        ("x = eval('1')\n", "调用了 eval()（动态执行代码）"),
        ("def broken(:\n", "无法解析"),
    ],
)
def test_static_scan_stops_obvious_overreach(tmp_path: Path, code: str, expected: str) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "helper.py").write_text(code, encoding="utf-8")
    problems = _problems(package)
    assert "helper.py 第 " in problems and expected in problems


def test_the_scan_allows_ordinary_imports(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "helper.py").write_text(
        "import csv, json, os, re\nfrom pathlib import Path\nfrom . import x\nprint(os.path.join('a', 'b'))\n",
        encoding="utf-8",
    )
    assert inspect_package(package).ok


def test_unsupported_protocol_version_and_requirements_are_refused(tmp_path: Path) -> None:
    assert "协议版本" in _problems(make_package(tmp_path / "a", adapter_api_version=99))
    assert "requires" in _problems(make_package(tmp_path / "b", requires=["numpy"]))
    assert "需要应用 99.0.0" in _problems(make_package(tmp_path / "c", min_app_version="99.0.0"))


def test_missing_functions_and_manifest_are_reported(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "adapter.py").write_text("def extract(path):\n    return []\n", encoding="utf-8")
    assert "缺少函数 write()" in _problems(package)
    (package / "adapter.json").unlink()
    assert "没有 adapter.json" in _problems(package)


def test_zip_paths_outside_the_package_are_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("adapter.json", "{}")
        handle.writestr("../evil.py", "print('x')")
    with pytest.raises(AdapterInstallError, match="压缩包外"):
        adapter_install.preview(archive)

    archive = tmp_path / "evil2.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("adapter.json", "{}")
        handle.writestr("..\\..\\evil.py", "print('x')")
    with pytest.raises(AdapterInstallError, match="压缩包外"):
        adapter_install.preview(archive)


def test_zip_symlinks_are_refused(tmp_path: Path) -> None:
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        info = zipfile.ZipInfo("adapter.py")
        info.external_attr = (0o120777 << 16)
        handle.writestr(info, "/etc/passwd")
    with pytest.raises(AdapterInstallError, match="符号链接"):
        adapter_install.preview(archive)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_folder_symlinks_are_refused(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "notes.md").symlink_to(tmp_path / "elsewhere.md")
    assert "符号链接" in _problems(package)


def test_a_damaged_zip_is_a_message_not_a_crash(tmp_path: Path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(AdapterInstallError, match="无法读取 zip"):
        adapter_install.preview(archive)


def test_preview_route_lists_every_problem(tmp_path: Path) -> None:
    package = make_package(tmp_path / "linetext")
    (package / "helper.py").write_text("import socket\nimport subprocess\n", encoding="utf-8")
    with TestClient(create_app()) as client:
        refused = client.post("/api/extensions/preview", json={"path": str(package)})
    assert refused.status_code == 422
    body = refused.json()
    assert body["reason"] == "extension_invalid"
    assert len(body["problems"]) == 2
    assert "socket" in body["detail"] and "subprocess" in body["detail"]


# ── start-up ────────────────────────────────────────────────────────────────


def test_a_damaged_registry_is_backed_up_and_rebuilt(tmp_path: Path, isolated: Path) -> None:
    adapter_install.install(make_package(tmp_path / "linetext"), confirmed=True)
    registry = isolated / "plugins" / "adapters" / "installed.json"
    registry.write_text("{ not json", encoding="utf-8")
    unregister_surface(KEY)

    with TestClient(create_app()) as client:
        assert KEY in _surface_keys(client)
    backups = list(registry.parent.glob("installed.json.broken-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{ not json"
    assert adapter_install.read_registry()["linetext"]["version"] == "1.0.0"


def test_a_missing_folder_is_skipped_at_start_up(tmp_path: Path, isolated: Path) -> None:
    record = adapter_install.install(make_package(tmp_path / "linetext"), confirmed=True)
    shutil.rmtree(isolated / "plugins" / "adapters" / record["dir"])
    unregister_surface(KEY)
    with TestClient(create_app()) as client:
        assert KEY not in _surface_keys(client)
        listed = client.get("/api/extensions/installed").json()["extensions"]
    assert listed[0]["ok"] is False and "重新安装" in listed[0]["problem"]


# ── a task on an installed type ─────────────────────────────────────────────


def test_an_installed_type_translates_a_file(tmp_path: Path) -> None:
    from tests.test_adapter_runner import FakeEngine

    source = tmp_path / "notes"
    source.mkdir()
    (source / "a.txt").write_text("Hello\n#! raw\nWorld\n", encoding="utf-8")
    with (
        patch("core.engine_dispatcher.build_role_engine", return_value=FakeEngine()),
        patch("api.surfaces.adapter.require_translation_model", lambda settings: None),
        patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None),
        TestClient(create_app()) as client,
    ):
        _install(client, zip_package(make_package(tmp_path / "linetext"), tmp_path / "linetext.zip"))
        # Third-party code runs in a child process, never in the engine.
        surface = next(item for item in all_surfaces() if item.key == KEY)
        assert isinstance(surface.host, SubprocessAdapterHost)
        started = client.post(
            "/api/tasks",
            json={"surface": KEY, "source_path": str(source), "target_lang": "fr", "source_lang": "en"},
        )
        assert started.status_code == 202, started.text
        deadline = time.time() + 20
        while True:
            status = client.get(f"/api/tasks/{started.json()['task_id']}").json()
            if status.get("terminal") or time.time() > deadline:
                break
            time.sleep(0.05)
    assert status["state"] == "done", status
    assert status["task_snapshot"]["adapter"]["origin"] == "installed"
    assert status["task_snapshot"]["adapter"]["label"]["zh"] == "按行文本"
    output = Path(status["result"]["file_results"][0]["output_path"])
    assert output.read_text(encoding="utf-8") == "[fr] Hello\n#! raw\n[fr] World\n"
