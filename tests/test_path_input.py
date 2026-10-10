from __future__ import annotations

from pathlib import Path

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
    source = "/Users/lijianwei/Downloads/海关、开闭所图纸_法语原版&中文翻译件/法语原版/海关全套图纸/电气/应急照明/PDF/SNRS.DIA.DCE.PLAN.EDS.BAT DOUANE.pdf"
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
    source = Path(
        "/Users/lijianwei/Downloads/海关、开闭所图纸_法语原版&中文翻译件/法语原版/海关全套图纸/电气/应急照明/PDF/SNRS.DIA.DCE.PLAN.EDS.BAT DOUANE.pdf"
    )
    assert source.is_file()
    assert Path(normalize_user_path(f"'{source}'")).is_file()
