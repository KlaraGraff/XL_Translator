from pathlib import Path

from docx import Document
from openpyxl import Workbook

from api.surfaces.adapter import list_files
from core.cad_translation import list_cad_files
from core.bilingual_writer import bilingual_output_name
from core.file_scanner import scan_excel_sources
from core.output_metadata import build_translation_metadata, write_translation_metadata
from core.pdf_image_translation import scan_pdf_sources
from core.resume_detection import match_source_folder_output
from core.word_document import scan_word_sources


def _mark_output(path: Path, *, surface: str, source: Path) -> None:
    path.write_bytes(b"generated")
    assert write_translation_metadata(
        path,
        build_translation_metadata(
            surface=surface,
            source_path=source,
            target_lang="en",
            output_mode="source_folder",
        ),
    )


def test_source_folder_metadata_outputs_are_excluded_from_recursive_scans(tmp_path: Path):
    root = tmp_path / "documents"
    root.mkdir()

    workbook = root / "source.xlsx"
    book = Workbook()
    book.active["A1"] = "源文"
    book.save(workbook)
    word = root / "source.docx"
    Document().save(word)
    pdf = root / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    excel_output = root / "source_English_双语.xlsx"
    word_output = root / "source_English_双语.docx"
    pdf_output = root / "source_English.pdf"
    _mark_output(excel_output, surface="excel", source=workbook)
    _mark_output(word_output, surface="word", source=word)
    _mark_output(pdf_output, surface="pdf", source=pdf)

    excel_paths = {item.path for item in scan_excel_sources(root).items}
    word_paths = {item.path for item in scan_word_sources(root).items}
    pdf_paths = {item.path for item in scan_pdf_sources(root).items}
    assert excel_output not in excel_paths and workbook in excel_paths
    assert word_output not in word_paths and word in word_paths
    assert pdf_output not in pdf_paths


def test_metadata_outputs_are_excluded_from_adapter_and_cad_lists(tmp_path: Path):
    root = tmp_path / "documents"
    root.mkdir()
    adapter_source = root / "source.txt"
    adapter_output = root / "source_English.txt"
    adapter_source.write_text("source", encoding="utf-8")
    _mark_output(adapter_output, surface="echo", source=adapter_source)

    cad_source = root / "source.dxf"
    cad_output = root / "source_English.dxf"
    cad_source.write_text("0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
    _mark_output(cad_output, surface="cad", source=cad_source)

    assert [path for path, _ in list_files(root, ("txt",))] == [adapter_source]
    assert list_cad_files([root]) == [cad_source]


def test_source_folder_resume_rejects_a_same_named_output_from_another_source(tmp_path: Path):
    source = tmp_path / "source.xlsx"
    other_source = tmp_path / "other" / "source.xlsx"
    other_source.parent.mkdir()
    output = tmp_path / bilingual_output_name(source.name, "英文")
    _mark_output(output, surface="excel", source=source)
    assert match_source_folder_output(source, "en", "excel") == output
    assert match_source_folder_output(other_source, "en", "excel") is None
