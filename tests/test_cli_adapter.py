"""``translator-sidecar adapter new | test | install | uninstall | pack``.

``new`` must write the template exactly (it is also the public template
repository), ``test`` must catch an adapter that cannot write its own file
back, ``pack`` must give the same zip as the template's ``scripts/pack.py``
run on its own, and ``install`` / ``uninstall`` go through a real engine
(``tests/fixtures/cli_engine.py``) with an isolated app data folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from cli import adapter_commands
from cli.adapter_commands import TEMPLATE_DIR, check_adapter, new_adapter, pack_adapter, template_files
from cli.main import main as cli_main
from core.adapter_install import inspect_package
from core.format_adapter import BUNDLED_ADAPTERS_DIR
from tests.fixtures.cli_engine import AppEngine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _outside_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # pack fills the author and the home page from these on GitHub Actions.
    for name in ("GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER", "GITHUB_SERVER_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))

    def invoke(*argv: str) -> tuple[int, str, str]:
        code = cli_main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


def _new(tmp_path: Path, adapter_id: str = "csv-lite") -> Path:
    target = tmp_path / adapter_id
    new_adapter(adapter_id, target)
    return target


def _rewrite(directory: Path, old: str, new: str) -> None:
    path = directory / "adapter.py"
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new), encoding="utf-8")


# ── new ──────────────────────────────────────────────────────────────────


def test_new_writes_the_template_byte_for_byte(run, tmp_path: Path) -> None:
    code, out, _err = run("adapter", "new", "csv-lite", "--dir", str(tmp_path / "work"), "--json")
    assert code == 0
    files = json.loads(out)["files"]
    assert files == template_files()
    assert {"adapter.json", "adapter.py", "AGENTS.md", "README.md", "README.en.md", "LICENSE",
            "scripts/pack.py", ".github/workflows/release.yml", ".gitignore"} <= set(files)
    assert len([name for name in files if name.startswith("samples/")]) == 2
    for relative in files:
        written = (tmp_path / "work" / relative).read_bytes()
        template = (TEMPLATE_DIR / relative).read_bytes()
        if relative == "adapter.json":
            assert written == template.replace(b'"id": "my-format"', b'"id": "csv-lite"')
        else:
            assert written == template, relative
    manifest = json.loads((tmp_path / "work" / "adapter.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "csv-lite"


@pytest.mark.skipif(not os.environ.get("TRANSLATOR_TEMPLATE_REPO"), reason="no checkout of the template repository")
def test_the_template_repository_matches_the_cli_template() -> None:
    """KlaraGraff/XL_Translator-extension-template is cli/adapter_template, byte for byte (PR checks clone it)."""
    repository = Path(os.environ["TRANSLATOR_TEMPLATE_REPO"]).resolve()
    published = sorted(
        path.relative_to(repository).as_posix()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    )
    assert published == template_files()
    for relative in published:
        assert (repository / relative).read_bytes() == (TEMPLATE_DIR / relative).read_bytes(), relative


def test_new_refuses_a_bad_id_a_used_folder_and_a_bundled_id(run, tmp_path: Path) -> None:
    assert run("adapter", "new", "Bad Id", "--dir", str(tmp_path / "a"))[0] == 2
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "notes.txt").write_text("x", encoding="utf-8")
    code, _out, err = run("adapter", "new", "fine", "--dir", str(tmp_path / "used"))
    assert code == 2 and "不是空文件夹" in err
    if (BUNDLED_ADAPTERS_DIR / "text").is_dir():
        assert run("adapter", "new", "text", "--dir", str(tmp_path / "b"))[0] == 2


def test_the_template_passes_its_own_test(run, tmp_path: Path) -> None:
    directory = _new(tmp_path)
    code, out, _err = run("adapter", "test", str(directory), "--json")
    report = json.loads(out)
    assert code == 0, report
    assert report["ok"] and report["id"] == "csv-lite" and report["problems"] == []
    assert [item["file"] for item in report["runs"]] == ["samples/hello.myfmt", "samples/inventory.myfmt"]
    first = report["runs"][0]
    assert (first["units"], first["locked"], first["scan_units"]) == (6, 1, 5)
    assert first["modes"] == {"translation_only": "ok", "bilingual": "ok"}
    # The scripts/, .github/ and README files are not installed; the samples are checked, not installed.
    assert "scripts/pack.py" not in report["files"] and "adapter.py" in report["files"]
    code, out, _err = run("adapter", "test", str(directory))
    assert code == 0 and "结果：通过。" in out and "samples/hello.myfmt：取出 6 段（其中 1 段不翻译）" in out


def test_the_bundled_text_type_passes(tmp_path: Path) -> None:
    text = BUNDLED_ADAPTERS_DIR / "text"
    if not text.is_dir():
        pytest.skip("no bundled text type in this checkout")
    report = check_adapter(text, allow_reserved_id=True)
    assert report.ok, report.problems
    assert all(run.modes == {"translation_only": "ok", "bilingual": "ok"} for run in report.runs)
    assert not check_adapter(text).ok  # its id is taken by the app itself


# ── test catches broken adapters ─────────────────────────────────────────


def test_an_adapter_that_ignores_the_translations_fails(tmp_path: Path) -> None:
    directory = _new(tmp_path)
    _rewrite(directory, "translation = \" \".join(unit.translation.splitlines())", "translation = unit.text")
    report = check_adapter(directory)
    assert not report.ok
    assert any("写回时没有用上译文" in problem for problem in report.problems)


def test_an_adapter_that_damages_the_text_fails(tmp_path: Path) -> None:
    directory = _new(tmp_path)
    _rewrite(directory, "translation = \" \".join(unit.translation.splitlines())", "translation = unit.translation.upper()")
    report = check_adapter(directory)
    assert not report.ok
    assert any("以原文作译文写回后再取字，与原文不一致" in problem for problem in report.problems)


def test_an_adapter_that_crashes_is_reported_per_sample(tmp_path: Path) -> None:
    directory = _new(tmp_path)
    _rewrite(directory, "    by_id = {unit.id: unit for unit in units}\n", "    raise ValueError('写不了')\n")
    report = check_adapter(directory)
    assert not report.ok
    assert [problem for problem in report.problems if "write 出错" in problem and "ValueError: 写不了" in problem]
    assert all(run.modes.get("translation_only") == "failed" for run in report.runs)


def test_static_problems_skip_the_sample_run(tmp_path: Path) -> None:
    directory = _new(tmp_path)
    _rewrite(directory, "import re\n", "import re\nimport socket\n")
    report = check_adapter(directory)
    assert not report.ok and report.runs == []
    assert any("socket" in problem for problem in report.problems)
    assert "未试运行样例：先修好上面列出的问题" in report.warnings


def test_missing_samples_and_texts_are_reported(tmp_path: Path) -> None:
    directory = _new(tmp_path)
    shutil.rmtree(directory / "samples")
    manifest = json.loads((directory / "adapter.json").read_text(encoding="utf-8"))
    manifest["description"] = {"zh": "只有中文"}
    (directory / "adapter.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    report = check_adapter(directory)
    assert "samples/ 里没有 .myfmt 样例，无法试运行" in report.problems
    assert "description 缺少 en, fr" in report.warnings


# ── pack ─────────────────────────────────────────────────────────────────


def test_pack_matches_the_template_script(run, tmp_path: Path) -> None:
    directory = _new(tmp_path)
    code, out, _err = run(
        "adapter", "pack", str(directory), "--out", str(tmp_path / "cli-dist"),
        "--repo", "https://github.com/someone/csv-lite", "--tag", "v0.1.0", "--json",
    )
    assert code == 0
    result = json.loads(out)
    script = subprocess.run(
        [sys.executable, str(directory / "scripts" / "pack.py"), str(directory), "--out", str(tmp_path / "script-dist"),
         "--repo", "https://github.com/someone/csv-lite", "--tag", "v0.1.0"],
        capture_output=True, text=True, encoding="utf-8", cwd=tmp_path,
        env=dict(os.environ), check=True,
    )
    cli_zip = Path(result["zip"])
    script_zip = tmp_path / "script-dist" / cli_zip.name
    assert cli_zip.name == "csv-lite-0.1.0.zip"
    assert cli_zip.read_bytes() == script_zip.read_bytes()
    assert json.loads(script.stdout) == result["entry"]
    entry = result["entry"]
    assert entry["sha256"] == hashlib.sha256(cli_zip.read_bytes()).hexdigest() and entry["size"] == cli_zip.stat().st_size
    assert entry["download"] == ["https://github.com/someone/csv-lite/releases/download/v0.1.0/csv-lite-0.1.0.zip"]
    assert entry["author"] == "someone" and entry["name"]["en"] == "My format" and entry["summary"]["fr"]
    assert (tmp_path / "cli-dist" / "csv-lite-0.1.0.zip.sha256").read_text(encoding="utf-8") == (
        f"{entry['sha256']}  csv-lite-0.1.0.zip\n"
    )
    # The zip holds exactly what an install keeps plus the samples.
    package = inspect_package(directory)
    with zipfile.ZipFile(cli_zip) as archive:
        assert archive.namelist() == sorted(package.files + package.samples)


def test_test_takes_the_packed_zip(run, tmp_path: Path) -> None:
    directory = _new(tmp_path)
    archive = pack_adapter(directory, out=str(tmp_path / "dist"))["zip"]
    code, out, _err = run("adapter", "test", archive, "--json")
    report = json.loads(out)
    assert code == 0 and report["ok"] and report["directory"] == archive
    assert len(report["runs"]) == 2
    code, _out, err = run("adapter", "test", str(tmp_path / "missing.zip"))
    assert code == 2 and "找不到" in err


def test_pack_is_repeatable_and_checks_the_tag(tmp_path: Path) -> None:
    directory = _new(tmp_path)
    first = pack_adapter(directory, out=str(tmp_path / "one"))
    second = pack_adapter(directory, out=str(tmp_path / "two"))
    assert first["entry"]["sha256"] == second["entry"]["sha256"]
    with pytest.raises(adapter_commands.AdapterCommandError, match="与 adapter.json 的 version 0.1.0 不一致"):
        pack_adapter(directory, tag="v0.2.0")
    _rewrite(directory, "import re\n", "import re\nimport subprocess\n")
    with pytest.raises(adapter_commands.AdapterCommandError, match="检查未通过"):
        pack_adapter(directory)


# ── install / uninstall through the engine ───────────────────────────────


def test_install_and_uninstall_through_the_engine(run, tmp_path: Path) -> None:
    directory = _new(tmp_path)
    data = tmp_path / "app-data"
    engine = AppEngine(data)
    try:
        code, out, err = run("adapter", "install", str(directory))
        assert code == 4
        assert "将添加扩展「我的格式」（ext:csv-lite）0.1.0" in out and "只安装你信任的来源" in out
        assert "--yes" in err
        assert "ext:csv-lite" not in [item["key"] for item in engine.http.get("/api/surfaces").json()["surfaces"]]

        code, out, _err = run("adapter", "install", str(directory), "--yes", "--json")
        assert code == 0, out
        assert json.loads(out)["record"]["id"] == "csv-lite"
        assert "ext:csv-lite" in [item["key"] for item in engine.http.get("/api/surfaces").json()["surfaces"]]

        code, out, _err = run("adapter", "uninstall", "csv-lite")
        assert code == 0 and "已卸载「我的格式」" in out
        assert "ext:csv-lite" not in [item["key"] for item in engine.http.get("/api/surfaces").json()["surfaces"]]
        code, _out, err = run("adapter", "uninstall", "csv-lite")
        assert code == 2 and "没有找到这个扩展" in err
    finally:
        engine.close()


def test_install_lists_every_problem(run, tmp_path: Path) -> None:
    directory = _new(tmp_path)
    _rewrite(directory, "import re\n", "import re\nimport socket\nimport ctypes\n")
    engine = AppEngine(tmp_path / "app-data")
    try:
        code, _out, err = run("adapter", "install", str(directory), "--yes")
    finally:
        engine.close()
    assert code == 2
    assert "socket" in err and "ctypes" in err and "检查未通过，未安装。" in err
