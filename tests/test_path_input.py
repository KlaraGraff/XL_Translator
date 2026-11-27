from __future__ import annotations

from pathlib import Path

import pytest

from api.app import CadScanRequest, ScanRequest, TaskStartRequest
from core.path_utils import normalize_user_path


def test_normalize_user_path_removes_common_matching_wrappers_only() -> None:
    path = "/tmp/plan 'revision'.pdf"
    assert normalize_user_path(f"'{path}'") == path
    assert normalize_user_path(f'"{path}"') == path
    assert normalize_user_path(f"“{path}”") == path
    assert normalize_user_path(f"`{path}`") == path
    assert normalize_user_path(f"'{path}") == f"'{path}"


def test_api_path_models_normalize_pasted_paths() -> None:
    source = "/Users/someone/Downloads/海关、开闭所图纸_法语原版&中文翻译件/法语原版/海关全套图纸/电气/应急照明/PDF/SNRS.DIA.DCE.PLAN.EDS.BAT DOUANE.pdf"
    scan = ScanRequest(
        path=f"'{source}'",
        paths=[f'“{source}”'],
        surface="pdf",
        preferred_resume_dir="`/tmp/previous-output`",
    )
    assert scan.path == source
    assert scan.paths == [source]
    assert scan.preferred_resume_dir == "/tmp/previous-output"

    cad = CadScanRequest(paths=[f'"{source}"'])
    assert cad.paths == [source]

    start = TaskStartRequest(
        surface="pdf",
        source_path=f"'{source}'",
        selected_paths=[f"‘{source}’"],
    )
    assert start.source_path == source
    assert start.selected_paths == [source]


def test_real_user_pdf_path_is_recovered_after_unwrapping() -> None:
    source = Path.home() / (
        "Downloads/海关、开闭所图纸_法语原版&中文翻译件/法语原版/海关全套图纸/电气/应急照明/PDF/SNRS.DIA.DCE.PLAN.EDS.BAT DOUANE.pdf"
    )
    if not source.is_file():
        # 这是维护者本机上一份真实的报错文件，换台机器（包括 CI）没有它就无从验证。
        pytest.skip("本机没有这份真实样本文件")
    assert Path(normalize_user_path(f"'{source}'")).is_file()


def test_normalize_user_path_accepts_file_urls() -> None:
    assert normalize_user_path("file:///Users/a/b%20c.xlsx") == "/Users/a/b c.xlsx"
    assert normalize_user_path("'file:///C:/x/y.docx'") == "C:/x/y.docx"
    assert normalize_user_path("file://localhost/tmp/a.pdf") == "/tmp/a.pdf"
    assert normalize_user_path("file://srv/share/a.xlsx") == "\\\\srv\\share\\a.xlsx"


def test_normalize_user_path_unescapes_paths_dragged_into_a_terminal() -> None:
    assert normalize_user_path("/Users/a/我的\\ 文件\\(1\\).xlsx") == "/Users/a/我的 文件(1).xlsx"
    assert normalize_user_path("~/x\\ y.pdf") == "~/x y.pdf"
    # Windows 路径的反斜杠是分隔符，不能动。
    assert normalize_user_path("C:\\Users\\a\\b c.xlsx") == "C:\\Users\\a\\b c.xlsx"
    assert normalize_user_path("\\\\srv\\share\\a.xlsx") == "\\\\srv\\share\\a.xlsx"


def test_ui_path_input_mirrors_backend_normalization() -> None:
    source = (Path(__file__).resolve().parents[1] / "ui/src/path-input.ts").read_text(encoding="utf-8")
    assert "fromFileUrl" in source and "unescapeShellPath" in source
    assert "return unescapeShellPath(fromFileUrl(normalized));" in source
