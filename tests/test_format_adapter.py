"""Format adapters: the protocol package, the manifest, the in-process host and the page.

``tests/fixtures/adapters/echo`` is a test-only adapter (one unit per line,
written back as is).  These checks pin what a manifest must say, that the
protocol types survive a JSON round trip (third-party adapters will only
exchange JSON), that ``translator_adapter`` never pulls in the app, and that
a page made from a manifest goes through the same routes as any other.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import all_surfaces, get_surface, unregister_surface
from api.surfaces.adapter import BILINGUAL_OPTION, AdapterSurface, register_bundled_adapters
from core.format_adapter import (
    BUNDLED_ADAPTERS_DIR,
    AdapterError,
    AdapterManifestError,
    InProcessAdapterHost,
    discover_adapters,
    load_manifest,
    parse_manifest,
)
from tests.app_data_isolation import IsolatedAppDataTestCase
from translator_adapter import (
    ADAPTER_API_VERSION,
    STATUS_UNTRANSLATED,
    OptionSpec,
    OutputFile,
    ScanItem,
    TextUnit,
    TranslatedUnit,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters"
ECHO_DIR = FIXTURES / "echo"
ECHO_KEY = "ext:echo"


def _manifest(**overrides) -> dict:
    data = json.loads((ECHO_DIR / "adapter.json").read_text(encoding="utf-8"))
    data.update(overrides)
    return data


def register_echo() -> AdapterSurface:
    """Register ``ext:echo`` once; later calls return the registered one."""
    registered = {surface.key for surface in all_surfaces()}
    if ECHO_KEY not in registered:
        register_bundled_adapters(FIXTURES)
    surface = get_surface(ECHO_KEY)
    assert isinstance(surface, AdapterSurface)
    return surface


# ── protocol package ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        ScanItem(unit_count=3, warnings=["空文件"]),
        TextUnit(id="p1", text="见 `npm install`", context="第 1 段", kind="heading", locked=False, protected=["`npm install`"]),
        TextUnit(id="c1", text="print(1)", locked=True),
        TranslatedUnit(id="p1", text="原文", translation="source", status="translated"),
        TranslatedUnit(id="p2", text="原文", translation="原文", status=STATUS_UNTRANSLATED),
        OutputFile(path="/tmp/out.md", review_count=2),
        OptionSpec(key="keep", label={"zh": "保留", "en": "Keep", "fr": "Garder"}, default=True, help={"zh": "说明"}),
    ],
    ids=lambda value: type(value).__name__,
)
def test_protocol_types_survive_a_json_round_trip(value):
    payload = json.loads(json.dumps(value.to_dict(), ensure_ascii=False))
    assert type(value).from_dict(payload) == value


def test_from_dict_fills_defaults_and_rejects_non_objects():
    assert TextUnit.from_dict({"id": "a", "text": "x"}) == TextUnit(id="a", text="x")
    # A missing translation means the source is written back.
    assert TranslatedUnit.from_dict({"id": "a", "text": "x"}).translation == "x"
    with pytest.raises(TypeError):
        TextUnit.from_dict(["a", "x"])


def test_the_protocol_package_never_imports_the_app():
    code = (
        "import sys, translator_adapter\n"
        "app = [m for m in sys.modules if m.split('.')[0] in {'core', 'api', 'settings', 'config', 'engines'}]\n"
        "assert not app, app\n"
        "print(translator_adapter.ADAPTER_API_VERSION)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(ADAPTER_API_VERSION)


# ── manifest ─────────────────────────────────────────────────────────────────


def test_the_echo_manifest_loads():
    manifest = load_manifest(ECHO_DIR)
    assert manifest.key == ECHO_KEY
    assert manifest.extensions == ["txt"]
    assert manifest.capabilities.bilingual and manifest.capabilities.translation_only
    assert [option.key for option in manifest.options] == ["upper"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"extensions": None}, "extensions"),
        ({"extensions": []}, "extensions"),
        ({"label": {"zh": "回声", "en": "Echo"}}, "label"),
        ({"id": "Echo!"}, "id"),
        ({"id": "ext:echo"}, "id"),
        ({"adapter_api_version": 99}, "adapter_api_version"),
        ({"adapter_api_version": 0}, "adapter_api_version"),
        ({"capabilities": {"bilingual": False, "translation_only": False}}, "capabilities"),
        ({"options": [{"key": "bilingual", "label": {"zh": "双语", "en": "Bi", "fr": "Bi"}}]}, "options"),
        ({"surprise": True}, "surprise"),
    ],
)
def test_a_bad_manifest_is_refused_with_the_field_named(overrides, message):
    data = _manifest(**overrides)
    for key, value in overrides.items():
        if value is None:
            data.pop(key)
    with pytest.raises(AdapterManifestError) as caught:
        parse_manifest(data)
    assert message in str(caught.value)


def test_a_missing_field_is_named():
    data = _manifest()
    data.pop("version")
    with pytest.raises(AdapterManifestError, match="version"):
        parse_manifest(data)


def test_a_manifest_for_a_newer_app_is_refused():
    assert parse_manifest(_manifest(min_app_version="1.0"), app_version="10.1.0")
    with pytest.raises(AdapterManifestError, match="99.0"):
        parse_manifest(_manifest(min_app_version="99.0"), app_version="10.1.0")


def test_extensions_are_normalized():
    manifest = parse_manifest(_manifest(extensions=[".MD", "md", "Markdown"]))
    assert manifest.extensions == ["md", "markdown"]


def test_discovery_skips_broken_adapters_and_reports_why():
    found, problems = discover_adapters(FIXTURES)
    assert [manifest.id for _path, manifest in found] == ["echo"]
    assert len(problems) == 1 and "broken" in problems[0]
    assert discover_adapters(FIXTURES / "missing") == ([], [])


def test_every_bundled_adapter_is_valid():
    found, problems = discover_adapters(BUNDLED_ADAPTERS_DIR)
    assert not problems
    for directory, manifest in found:
        assert directory.name == manifest.id


# ── in-process host ──────────────────────────────────────────────────────────


def test_the_host_calls_the_adapter_and_copies_its_results(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("#! keep\nHello {{name}}\n\nBye\n", encoding="utf-8")
    host = InProcessAdapterHost(ECHO_DIR)
    assert host.scan(source) == ScanItem(unit_count=2, warnings=[])
    units = host.extract(source)
    assert [(unit.id, unit.locked, unit.protected) for unit in units] == [
        ("L1", True, []),
        ("L2", False, ["{{name}}"]),
        ("L4", False, []),
    ]
    out = tmp_path / "out.txt"
    result = host.write(
        source,
        [TranslatedUnit(unit.id, unit.text, unit.text.replace("Bye", "Au revoir")) for unit in units],
        "translation_only",
        out,
        {},
    )
    assert result == OutputFile(path=str(out))
    assert out.read_text(encoding="utf-8") == "#! keep\nHello {{name}}\n\nAu revoir\n"


def _write_adapter(directory: Path, body: str, *, adapter_id: str = "probe") -> InProcessAdapterHost:
    directory.mkdir(parents=True)
    (directory / "adapter.json").write_text(
        json.dumps(_manifest(id=adapter_id, options=[]), ensure_ascii=False), encoding="utf-8"
    )
    (directory / "adapter.py").write_text(body, encoding="utf-8")
    return InProcessAdapterHost(directory)


def test_the_host_accepts_plain_dicts_and_counts_without_scan(tmp_path):
    host = _write_adapter(
        tmp_path / "probe",
        "def extract(path):\n"
        "    return [{'id': 'a', 'text': 'one', 'protected': ['missing', 'one']}, {'id': 'b', 'text': 'x', 'locked': True}]\n"
        "def write(path, units, mode, out_path, options):\n"
        "    return None\n",
    )
    units = host.extract(Path("anything"))
    assert units[0] == TextUnit(id="a", text="one", protected=["one"])
    assert host.scan(Path("anything")).unit_count == 1
    assert host.write(Path("x"), [], "bilingual", Path("/tmp/o"), {}).path == "/tmp/o"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("def extract(path):\n    return [{'id': 'a', 'text': '1'}, {'id': 'a', 'text': '2'}]\ndef write(*a):\n    pass\n", "重复"),
        ("def extract(path):\n    return [{'text': 'no id'}]\ndef write(*a):\n    pass\n", "id"),
        ("def extract(path):\n    return 'text'\ndef write(*a):\n    pass\n", "列表"),
        ("def extract(path):\n    raise UnicodeDecodeError('utf-8', b'x', 0, 1, 'bad')\ndef write(*a):\n    pass\n", "UnicodeDecodeError"),
        ("def write(*a):\n    pass\n", "extract"),
        ("raise ImportError('needs numpy')\n", "numpy"),
    ],
)
def test_adapter_failures_become_adapter_errors(tmp_path, body, message):
    host = _write_adapter(tmp_path / "probe", body)
    with pytest.raises(AdapterError, match=message):
        host.extract(Path("anything"))


# ── the page made from a manifest ────────────────────────────────────────────


def test_the_descriptor_comes_from_the_manifest():
    manifest = load_manifest(ECHO_DIR)
    surface = AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest))
    descriptor = surface.describe()
    assert descriptor["key"] == ECHO_KEY
    assert descriptor["builtin"] is False
    assert descriptor["label"] == manifest.label
    assert descriptor["file_noun"] == manifest.file_noun
    assert descriptor["icon"] == "doc-file"
    assert descriptor["extensions"] == ["txt"]
    assert descriptor["capabilities"]["bilingual"] and descriptor["capabilities"]["translation_only"]
    assert descriptor["capabilities"]["can_pause"] and descriptor["capabilities"]["can_resume"]
    # Both outputs: the app adds its own 双语对照 switch before the adapter's.
    assert [option["key"] for option in descriptor["options"]] == ["bilingual", "upper"]
    assert descriptor["options"][0] == BILINGUAL_OPTION.describe()
    assert len(descriptor["phases"]) == len(surface.phase_weights)
    assert descriptor["adapter"] == {"id": "echo", "version": "1.0.0", "origin": "bundled", "license": "MIT", "repo": ""}


def test_one_output_means_no_bilingual_switch():
    manifest = parse_manifest(_manifest(capabilities={"bilingual": False, "translation_only": True}))
    surface = AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest))
    assert [option.key for option in surface.options] == ["upper"]


def test_switch_values_prefer_the_request_then_the_saved_value():
    from api.task_contracts import TaskOptions
    from core.surface_settings import set_surface_setting
    from settings import AppSettings

    manifest = load_manifest(ECHO_DIR)
    surface = AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest))
    settings = AppSettings()
    assert surface.resolve_options(settings, TaskOptions()) == {"bilingual": False, "upper": False}
    set_surface_setting(settings, ECHO_KEY, "options", {"bilingual": True, "stale": True})
    assert surface.resolve_options(settings, TaskOptions()) == {"bilingual": True, "upper": False}
    assert surface.resolve_options(settings, TaskOptions(options={"bilingual": False, "upper": True})) == {
        "bilingual": False,
        "upper": True,
    }


def test_registration_is_idempotent_and_skips_broken_adapters():
    try:
        first = register_bundled_adapters(FIXTURES)
        again = register_bundled_adapters(FIXTURES)
        assert first == [ECHO_KEY]
        assert again == []
        assert isinstance(get_surface(ECHO_KEY), AdapterSurface)
    finally:
        unregister_surface(ECHO_KEY)


class AdapterRouteTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        register_echo()
        self.addCleanup(unregister_surface, ECHO_KEY)
        self.source = self.app_data_dir.parent / "notes"
        (self.source / "sub").mkdir(parents=True)
        (self.source / "a.txt").write_text("Hello\nWorld\n", encoding="utf-8")
        (self.source / "sub" / "b.txt").write_text("#! only locked\n", encoding="utf-8")
        (self.source / "skip.md").write_text("not mine", encoding="utf-8")
        # Earlier output and hidden folders are never listed.
        (self.source / "notes_翻译输出_20261014_120000").mkdir()
        (self.source / "notes_翻译输出_20261014_120000" / "a_法语.txt").write_text("x", encoding="utf-8")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "c.txt").write_text("x", encoding="utf-8")
        (self.source / "bad.txt").write_bytes(b"\xff\xfe\x00bad")

    def test_the_descriptor_lists_the_page(self) -> None:
        with TestClient(create_app()) as client:
            surfaces = client.get("/api/surfaces").json()["surfaces"]
        echo = next(item for item in surfaces if item["key"] == ECHO_KEY)
        assert echo["label"]["zh"] == "回声文本"
        assert echo["adapter"]["origin"] == "bundled"

    def test_scan_lists_files_with_their_unit_counts(self) -> None:
        with TestClient(create_app()) as client:
            scanned = client.post("/api/sources/scan", json={"surface": ECHO_KEY, "path": str(self.source)})
        assert scanned.status_code == 200, scanned.text
        payload = scanned.json()
        assert [(item["relative_path"], item["unit_count"]) for item in payload["items"]] == [
            ("a.txt", 2),
            (str(Path("sub") / "b.txt"), 0),
        ]
        assert payload["items"][1]["warnings"] == ["no text"]
        assert all(item["file_id"] for item in payload["items"])
        # A file the adapter cannot read is listed as skipped, with the reason.
        assert [item["name"] for item in payload["skipped"]] == ["bad.txt"]
        assert payload["skipped"][0]["reason"].startswith("无法读取")
        assert payload["summary"] == {"scanned_count": 3, "selected_count": 2, "skipped_count": 1, "unit_count": 2}
        assert payload["previous_output"] is None
