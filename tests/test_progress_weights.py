"""Overall progress folding for the task progress bar (api/task_manager.py)."""

from api.task_manager import overall_progress_percent
from core.pdf_image_translation import PdfFileRecord, PdfImageTranslationRunner


def test_pdf_page_phase_covers_most_of_the_bar():
    # 翻页阶段做完一半时，进度条应该过半，而不是停在「4 段平均分」的 37.5%。
    assert overall_progress_percent("pdf", 2, 4, 50.0) > 45
    # 页全部译完时进度条已接近满格，剩下的只有装订和写报告。
    assert overall_progress_percent("pdf", 2, 4, 100.0) >= 85
    assert overall_progress_percent("pdf", 4, 4, 100.0) == 100


def test_progress_is_monotonic_across_phases():
    for surface, total in (("excel", 4), ("excel", 3), ("word", 3), ("pdf", 4)):
        points = [
            overall_progress_percent(surface, phase, total, pct)
            for phase in range(1, total + 1)
            for pct in (0.0, 50.0, 100.0)
        ]
        assert points == sorted(points), surface
        assert points[-1] == 100


def test_unknown_phase_layout_falls_back_to_equal_weights():
    assert overall_progress_percent("pdf", 2, 5, 0.0) == 20
    assert overall_progress_percent("unknown", 1, 2, 100.0) == 50


def test_batch_level_progress_passes_through():
    # CAD 不分阶段，直接上报整批进度。
    assert overall_progress_percent("cad", 1, 0, 42.0) == 42


def test_pdf_file_chip_only_says_rendering_for_the_first_page():
    record = PdfFileRecord(name="a.pdf", source_path="a.pdf", relative_path="a.pdf", page_count=3)
    assert PdfImageTranslationRunner._page_render_phase(record) == "render"
    record.pages.append(object())  # type: ignore[arg-type]
    assert PdfImageTranslationRunner._page_render_phase(record) == "translate"
