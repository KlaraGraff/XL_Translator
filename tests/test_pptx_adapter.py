"""The built-in 「PPT」 type (``adapters/pptx``) as the app runs it.

The manifest, the bundled-type check (``adapter test``) on its samples, the
page it registers, and a whole task on the shared executor with a fake
model: the output is a .pptx next to the others, the notes switch reaches
``write``, and what the writer flags lands in the review list.
"""

from __future__ import annotations

import re
import shutil
import zipfile
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lxml import etree

from api.surfaces import all_surfaces, get_surface, unregister_surface
from api.surfaces.adapter import AdapterSurface, register_bundled_adapters
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskOptions
from cli.adapter_commands import check_adapter
from config import CORE_TRANSLATION_PROMPT
from core import tm_manager
from core.format_adapter import BUNDLED_ADAPTERS_DIR, AdapterError, InProcessAdapterHost, load_manifest
from core.task_runner_messages import DoneMsg
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_adapter_runner import FakeEngine, _drain, _item
from translator_adapter import MODE_BILINGUAL, MODE_TRANSLATION_ONLY, TranslatedUnit

PPTX_DIR = BUNDLED_ADAPTERS_DIR / "pptx"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pptx"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


class FrenchEngine(FakeEngine):
    """Answers with Latin text only (the shared fake keeps the Chinese, which the
    residue check rightly flags), keeping the ``⟦n⟧`` tokens of protected parts."""

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        with self.lock:
            self.calls.append(list(texts))
            self.prompts.append(system_prompt)
        return {text: french(text) for text in texts}


C = "http://schemas.openxmlformats.org/drawingml/2006/chart"


def french(text: str) -> str:
    # Inline formatting marks (<g1>…</g1>) do not come back: the app then places
    # them itself or writes the paragraph whole, so the words match the plain text.
    text = re.sub(r"</?g\d+>", "", text)
    tokens = re.findall(r"⟦\d+⟧", text)
    # About as long as a real French line and with the same figures, so the
    # quality check takes it for a translation.
    numbers = re.findall(r"(?<!⟦)\d+(?:[.,]\d+)?%?", text)
    words = ["mot"] * (len(text) // 3)
    return " ".join([f"Texte {zlib.crc32(text.encode()) % 1000}", *words, *numbers, *tokens])


def _texts(path: Path, part: str) -> list[str]:
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read(part))
    return ["".join(t.text or "" for t in paragraph.iter(f"{{{A}}}t")) for paragraph in root.iter(f"{{{A}}}p")]


def test_manifest() -> None:
    manifest = load_manifest(PPTX_DIR)
    assert manifest.key == "ext:pptx"
    assert manifest.label == {"zh": "PPT", "en": "PowerPoint", "fr": "PowerPoint"}
    assert manifest.file_noun == {"zh": "演示文稿", "en": "presentations", "fr": "présentations"}
    assert manifest.extensions == ["pptx", "pptm", "ppsx", "potx"]
    assert manifest.capabilities.bilingual and manifest.capabilities.translation_only
    assert [(option.key, option.default) for option in manifest.options] == [
        ("translate_notes", True),
        ("fixed_layout", False),
    ]
    # The PPT rules come on top of the app's own, which must stay word for word.
    assert manifest.prompt.startswith(CORE_TRANSLATION_PROMPT + "\n")
    assert "不扩写" in manifest.prompt and "不加句号" in manifest.prompt


def test_the_bundled_check_passes_on_the_samples() -> None:
    report = check_adapter(PPTX_DIR, allow_reserved_id=True)
    assert report.ok, report.problems
    assert report.warnings == []
    assert [run.file for run in report.runs] == ["samples/安全培训讲义.pptx", "samples/项目进展汇报.pptx"]
    assert all(run.modes == {"translation_only": "ok", "bilingual": "ok"} for run in report.runs)


def test_readable_errors(tmp_path: Path) -> None:
    host = InProcessAdapterHost(PPTX_DIR)
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
    try:
        host.extract(broken)
    except AdapterError as exc:
        assert str(exc) == "此文件已加密，或实际是旧版 .ppt 格式，请在 PowerPoint 里另存为 .pptx 后再试。"
    else:  # pragma: no cover - the call must fail
        raise AssertionError("extract should fail")


def test_marks_reach_the_unit_when_the_protocol_has_them() -> None:
    units = {unit.text: unit for unit in InProcessAdapterHost(PPTX_DIR).extract(FIXTURES / "mixed.pptx")}
    unit = units["注意：所有图纸以最新版本为准。"]
    if hasattr(unit, "marked"):
        assert unit.marked == "<g1>注意：</g1>所有图纸以最新版本为准。"
    assert unit.context == "第 2 页 · Content Placeholder 2 · 第 2 段"
    assert units["第 3 页"].protected == ["3"]


def test_notes_switch_reaches_write(tmp_path: Path) -> None:
    host = InProcessAdapterHost(PPTX_DIR)
    source = FIXTURES / "training.pptx"
    units = host.extract(source)
    translated = [TranslatedUnit(unit.id, unit.text, f"FR {unit.text}") for unit in units]
    out = tmp_path / "no_notes.pptx"
    host.write(source, translated, MODE_TRANSLATION_ONLY, out, {"translate_notes": False, "target_lang": "fr"})
    assert _texts(out, "ppt/notesSlides/notesSlide1.xml") == ["开场先介绍培训目的。"]
    assert _texts(out, "ppt/slides/slide1.xml") == ["FR 安全生产培训", "FR 新员工入职第一课"]
    out = tmp_path / "bilingual.pptx"
    host.write(source, translated, MODE_BILINGUAL, out, {"target_lang": "fr"})
    assert _texts(out, "ppt/notesSlides/notesSlide1.xml") == ["开场先介绍培训目的。", "FR 开场先介绍培训目的。"]


def test_fixed_layout_switch_reaches_the_fit(tmp_path: Path) -> None:
    host = InProcessAdapterHost(PPTX_DIR)
    source = FIXTURES / "training.pptx"
    translated = [TranslatedUnit(unit.id, unit.text, f"FR {unit.text}") for unit in host.extract(source)]
    seen: list[dict[str, Any]] = []

    def fit(read_part, part_name, root, touched, **kwargs):
        seen.append(kwargs)
        return []

    with patch("core.pptx_text.fit_after_write", fit):
        host.write(source, translated, MODE_TRANSLATION_ONLY, tmp_path / "off.pptx", {"fixed_layout": False})
        assert seen and all(kwargs == {} for kwargs in seen)
        seen.clear()
        host.write(source, translated, MODE_TRANSLATION_ONLY, tmp_path / "on.pptx", {"fixed_layout": True})
        assert seen and all(kwargs == {"fixed_layout": True} for kwargs in seen)


class PptxTaskTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "decks"
        self.source.mkdir(parents=True)
        self.manifest = load_manifest(PPTX_DIR)
        self.surface = AdapterSurface(self.manifest, InProcessAdapterHost(PPTX_DIR, self.manifest))
        patcher = patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, engine: FakeEngine, paths: list[Path], *, options: dict[str, Any] | None = None) -> list[Any]:
        settings = AppSettings(target_lang="fr")
        settings.extensions["pptx"] = ExtensionSettings(smart_context=False)
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine):
            runner = self.surface.build_runner(
                RunnerRequest(
                    files=[_item(path) for path in paths],
                    settings=settings,
                    source_root=self.source,
                    options=TaskOptions(options=options or {}),
                    source_lang="zh",
                    key_overrides={},
                )
            )
            runner.start()
            return _drain(runner, timeout=60)

    def test_a_whole_task_writes_a_translated_deck(self) -> None:
        path = self.source / "项目进展汇报.pptx"
        shutil.copyfile(FIXTURES / "mixed.pptx", path)
        engine = FrenchEngine()
        messages = self._run(engine, [path])
        done = next(message for message in messages if isinstance(message, DoneMsg))
        result = done.file_results[0]
        out = Path(result["output_path"])
        assert out.name == "项目进展汇报_法文.pptx" and out.is_file()
        assert _texts(out, "ppt/slides/slide2.xml")[0] == french("项目概况")
        # The slide number is a field: the model gets a token for it and the field stays.
        assert french("第 ⟦1⟧ 页").replace("⟦1⟧", "3") in _texts(out, "ppt/slides/slide3.xml")
        assert engine.prompts[0].startswith(CORE_TRANSLATION_PROMPT)
        assert "不扩写" in engine.prompts[0]
        # The vertical text box on slide 7 is reported by write and listed for review.
        assert [(item["location"], item["issue"]) for item in done.review["items"]] == [
            ("第 7 页 · TextBox 1", "竖排文本框，译文可能需要改为横排")
        ]
        assert result["status"] == "needs_review" and result["review_count"] == 1, result

    def test_charts_and_smartart_are_translated_in_a_task(self) -> None:
        path = self.source / "季度产值.pptx"
        shutil.copyfile(FIXTURES / "graphics.pptx", path)
        for bilingual in (False, True):
            messages = self._run(FrenchEngine(), [path], options={"bilingual": bilingual})
            done = next(message for message in messages if isinstance(message, DoneMsg))
            result = done.file_results[0]
            assert result["status"] == "completed", result
            out = Path(result["output_path"])
            with zipfile.ZipFile(out) as package:
                cache = [v.text for v in etree.fromstring(package.read("ppt/charts/chart1.xml")).iter(f"{{{C}}}v")]
            quarter = f"第一季度\n{french('第一季度')}" if bilingual else french("第一季度")
            assert quarter in cache
            drawing = _texts(out, "ppt/diagrams/drawing1.xml")
            assert french("需求调研") in drawing
            assert ("需求调研" in drawing) is bilingual

    def test_bilingual_option_writes_both(self) -> None:
        path = self.source / "讲义.pptx"
        shutil.copyfile(FIXTURES / "training.pptx", path)
        messages = self._run(FrenchEngine(), [path], options={"bilingual": True, "translate_notes": False})
        done = next(message for message in messages if isinstance(message, DoneMsg))
        result = done.file_results[0]
        out = Path(result["output_path"])
        assert out.name == "讲义_法文_双语.pptx"
        assert result["status"] == "completed", result
        assert _texts(out, "ppt/slides/slide1.xml") == [
            "安全生产培训",
            french("安全生产培训"),
            "新员工入职第一课",
            french("新员工入职第一课"),
        ]
        assert _texts(out, "ppt/notesSlides/notesSlide1.xml") == ["开场先介绍培训目的。"]


def test_the_page_is_registered_with_the_app() -> None:
    key = "ext:pptx"
    already = any(surface.key == key for surface in all_surfaces())
    try:
        register_bundled_adapters()
        descriptor = get_surface(key).describe()
        assert descriptor["label"] == {"zh": "PPT", "en": "PowerPoint", "fr": "PowerPoint"}
        assert descriptor["icon"] == "ppt"
        assert descriptor["extensions"] == ["potx", "ppsx", "pptm", "pptx"]
        assert [option["key"] for option in descriptor["options"]] == ["bilingual", "translate_notes", "fixed_layout"]
    finally:
        if not already:
            unregister_surface(key)
