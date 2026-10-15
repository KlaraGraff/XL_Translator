"""Installed adapters run in a child process (core/adapter_subprocess_host.py).

Each call starts ``launch_sidecar.py --adapter-host <dir> <action>`` and only
JSON crosses the boundary.  These pin that the child gives the same answers
as the in-process host, that a hung, failing or crashing adapter costs only
its own call, that the adapter's own prints never corrupt the result, and that
the child sees neither the app data directory nor any key.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from core.adapter_subprocess_host import SubprocessAdapterHost, child_environment
from core.format_adapter import AdapterError, InProcessAdapterHost, load_manifest
from translator_adapter import MODE_BILINGUAL, MODE_TRANSLATION_ONLY, TranslatedUnit
from tests.test_format_adapter import ECHO_DIR


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "notes.txt"
    path.write_text("Hello {{name}}\n#! raw\n\nWorld\n", encoding="utf-8")
    return path


def _adapter(tmp_path: Path, body: str) -> Path:
    """The echo manifest with a hand-written adapter.py."""
    directory = tmp_path / "adapter"
    directory.mkdir()
    shutil.copyfile(ECHO_DIR / "adapter.json", directory / "adapter.json")
    (directory / "adapter.py").write_text(body, encoding="utf-8")
    return directory


def test_the_child_answers_like_the_in_process_host(source: Path, tmp_path: Path) -> None:
    child = SubprocessAdapterHost(ECHO_DIR)
    local = InProcessAdapterHost(ECHO_DIR)
    assert child.scan(source) == local.scan(source)
    units = child.extract(source)
    assert units == local.extract(source)
    assert [(unit.id, unit.locked, unit.protected) for unit in units] == [
        ("L1", False, ["{{name}}"]),
        ("L2", True, []),
        ("L4", False, []),
    ]
    translated = [
        TranslatedUnit(id=unit.id, text=unit.text, translation=unit.text if unit.locked else f"[fr] {unit.text}")
        for unit in units
    ]
    for mode in (MODE_TRANSLATION_ONLY, MODE_BILINGUAL):
        out_child = tmp_path / f"child-{mode}.txt"
        out_local = tmp_path / f"local-{mode}.txt"
        result = child.write(source, translated, mode, out_child, {"upper": True})
        local.write(source, translated, mode, out_local, {"upper": True})
        assert result.path == str(out_child)
        assert out_child.read_text(encoding="utf-8") == out_local.read_text(encoding="utf-8")
    assert "[FR] HELLO {{NAME}}" in (tmp_path / "child-translation_only.txt").read_text(encoding="utf-8")


def test_a_hung_adapter_is_stopped_at_the_timeout(source: Path, tmp_path: Path) -> None:
    directory = _adapter(
        tmp_path,
        "import time\n\ndef extract(path):\n    time.sleep(30)\n    return []\n\ndef write(*args):\n    pass\n",
    )
    host = SubprocessAdapterHost(directory, file_timeout=2)
    started = time.monotonic()
    with pytest.raises(AdapterError, match="处理超时"):
        host.extract(source)
    assert time.monotonic() - started < 15


def test_an_exception_comes_back_as_a_message(source: Path, tmp_path: Path) -> None:
    directory = _adapter(
        tmp_path,
        "def extract(path):\n    raise ValueError('第 3 行格式不对')\n\ndef write(*args):\n    pass\n",
    )
    with pytest.raises(AdapterError, match="ValueError: 第 3 行格式不对"):
        SubprocessAdapterHost(directory).extract(source)


def test_a_crash_is_a_failed_call_not_a_dead_engine(source: Path, tmp_path: Path) -> None:
    directory = _adapter(
        tmp_path,
        "import os, sys\n\ndef extract(path):\n    sys.stderr.write('about to die\\n')\n    os._exit(3)\n\n"
        "def write(*args):\n    pass\n",
    )
    with pytest.raises(AdapterError, match="异常退出（代码 3）：about to die"):
        SubprocessAdapterHost(directory).extract(source)
    # The engine is still here and the next call works.
    assert SubprocessAdapterHost(ECHO_DIR).scan(source).unit_count == 2


def test_prints_do_not_corrupt_the_result(source: Path, tmp_path: Path) -> None:
    directory = _adapter(
        tmp_path,
        "from translator_adapter import TextUnit\n\n"
        "def extract(path):\n    print('{\"result\": \"fake\"}')\n    print('debug line')\n"
        "    return [TextUnit(id='a', text='x')]\n\ndef write(*args):\n    pass\n",
    )
    units = SubprocessAdapterHost(directory).extract(source)
    assert [unit.id for unit in units] == ["a"]


def test_bad_return_values_are_refused(source: Path, tmp_path: Path) -> None:
    directory = _adapter(
        tmp_path,
        "def extract(path):\n    return [{'id': 'a', 'text': 'x'}, {'id': 'a', 'text': 'y'}]\n\ndef write(*args):\n    pass\n",
    )
    with pytest.raises(AdapterError, match="重复"):
        SubprocessAdapterHost(directory).extract(source)


def test_the_child_sees_no_app_data_and_no_keys(source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "secret-data"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-not-leak")
    report = tmp_path / "env.json"
    directory = _adapter(
        tmp_path,
        "import json, os\nfrom translator_adapter import OutputFile\n\n"
        "def extract(path):\n    return []\n\n"
        "def write(path, units, mode, out_path, options):\n"
        "    with open(out_path, 'w', encoding='utf-8') as handle:\n"
        "        json.dump({'env': dict(os.environ), 'cwd': os.getcwd()}, handle)\n"
        "    return OutputFile(path=out_path)\n",
    )
    SubprocessAdapterHost(directory).write(source, [], MODE_TRANSLATION_ONLY, report, {})
    seen = json.loads(report.read_text(encoding="utf-8"))
    assert "TRANSLATOR_APP_DATA_DIR" not in seen["env"]
    assert not any("KEY" in name or "TOKEN" in name for name in seen["env"])
    assert "secret-data" not in json.dumps(seen)
    assert Path(seen["cwd"]).name.startswith("translator-adapter-")
    assert not Path(seen["cwd"]).exists()  # the working folder is removed afterwards


def test_child_environment_passes_only_what_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", "/data")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    env = child_environment("/tmp/work")
    assert env["PATH"] == "/usr/bin"
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == "/tmp/work"
    assert "TRANSLATOR_APP_DATA_DIR" not in env and "ANTHROPIC_API_KEY" not in env


def test_the_manifest_is_read_by_the_parent(tmp_path: Path) -> None:
    host = SubprocessAdapterHost(ECHO_DIR)
    assert host.manifest == load_manifest(ECHO_DIR)
    assert host.log_label == "[扩展 回声文本]"


def test_non_ascii_crosses_a_code_page_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The frozen sidecar ignores PYTHONUTF8 / PYTHONIOENCODING, so on Windows the
    child's pipes default to the system code page (V10.2.0 build: cp1252 could not
    write the result).  Force a code page here and send Chinese both ways."""
    import core.adapter_subprocess_host as host_module

    real_environment = host_module.child_environment

    def code_page_environment(temporary_dir: str) -> dict[str, str]:
        env = real_environment(temporary_dir)
        env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        return env

    monkeypatch.setattr(host_module, "child_environment", code_page_environment)
    folder = tmp_path / "图纸 说明"
    folder.mkdir()
    source = folder / "说明.txt"
    source.write_text("你好 {{name}}\n\n世界\n", encoding="utf-8")
    child = SubprocessAdapterHost(ECHO_DIR)
    assert child.scan(source) == InProcessAdapterHost(ECHO_DIR).scan(source)
    units = child.extract(source)
    assert [unit.text for unit in units] == ["你好 {{name}}", "世界"]
    out_path = folder / "译文.txt"
    translated = [TranslatedUnit(id=unit.id, text=unit.text, translation=f"译：{unit.text}") for unit in units]
    assert child.write(source, translated, MODE_TRANSLATION_ONLY, out_path, {}).path == str(out_path)
    assert "译：世界" in out_path.read_text(encoding="utf-8")
