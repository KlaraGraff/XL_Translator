"""What the shared format-adapter executor adds for formatted text and mixed languages.

``tests/fixtures/mark_adapters/marks`` hands ``*bold*`` stretches over as paired
marks (``TextUnit.marked``) and writes ``marked_translation`` back as ``*…*``.
The three paths: the model brings the marks back whole; it loses them and the
app asks it to place them on the final translation; a translation memory hit
gets its marks the same way.  When that fails too the unit is written in one
piece, never refused.

The mixed-language rule is the Excel / Word one (#87, #97): in translation-only
mode, Chinese + foreign text is translated whole into the target language, and
what cannot be is kept and listed for review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from api.surfaces.adapter import AdapterSurface
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskOptions
from core import tm_manager
from core.adapter_runner import REASON_MIXED_KEPT, REASON_MIXED_LEFTOVER
from core.format_adapter import InProcessAdapterHost, load_manifest, validate_units
from core.task_runner_messages import DoneMsg
from core.word_inline_marks import MARK_PROMPT_RULE, PLACE_MARKS_PROMPT
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_adapter_runner import ECHO_DIR, FakeEngine, _drain, _item
from translator_adapter import TextUnit, TranslatedUnit

MARKS_DIR = Path(__file__).resolve().parent / "fixtures" / "mark_adapters" / "marks"


class MarkEngine(FakeEngine):
    """Keeps the marks unless told to drop them; places marks on request."""

    def __init__(self, *, drop_marks: bool = False, place: dict[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.drop_marks = drop_marks
        #: final translation → the same with marks, answered to the place-marks request.
        self.place = dict(place or {})

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        out = super().translate_batch(texts, target_lang, system_prompt, source_lang)
        if self.drop_marks:
            out = {text: value.replace("<g1>", "").replace("</g1>", "") for text, value in out.items()}
        return out

    def chat(self, system, user):
        if system == PLACE_MARKS_PROMPT:
            with self.lock:
                self.chats.append(system)
            return json.dumps([self.place.get(item["译文"], "?") for item in json.loads(user)], ensure_ascii=False)
        return super().chat(system, user)


class MarksRunnerTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "docs"
        self.source.mkdir(parents=True)
        manifest = load_manifest(MARKS_DIR)
        self.surface = AdapterSurface(manifest, InProcessAdapterHost(MARKS_DIR, manifest))

    def _run(self, engine: FakeEngine, body: str, *, settings: AppSettings | None = None) -> tuple[DoneMsg, str]:
        path = self.source / "a.mk"
        path.write_text(body, encoding="utf-8")
        if settings is None:
            settings = AppSettings(target_lang="fr")
            settings.extensions["marks"] = ExtensionSettings(smart_context=False)
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine):
            runner = self.surface.build_runner(
                RunnerRequest(
                    files=[_item(path)],
                    settings=settings,
                    source_root=self.source,
                    options=TaskOptions(options={}),
                    source_lang="en",
                    key_overrides={},
                )
            )
            runner.start()
            messages = _drain(runner)
        done = messages[-1]
        assert isinstance(done, DoneMsg), done
        return done, Path(done.file_results[0]["output_path"]).read_text(encoding="utf-8")

    def test_marks_that_come_back_whole_keep_the_bold_stretch(self) -> None:
        engine = MarkEngine()
        done, output = self._run(engine, "Press *Start* now\nPlain line\n")
        # The model sees the marks and the rule for them; the plain line goes as it is.
        assert engine.sent == ["Press <g1>Start</g1> now", "Plain line"]
        assert MARK_PROMPT_RULE in engine.prompts[0]
        assert output == "[fr] Press *Start* now\n[fr] Plain line\n"
        assert done.review == {"total_count": 0}
        assert PLACE_MARKS_PROMPT not in engine.chats

    def test_lost_marks_are_placed_on_the_final_translation(self) -> None:
        engine = MarkEngine(drop_marks=True, place={"[fr] Press Start now": "[fr] Press <g1>Start</g1> now"})
        done, output = self._run(engine, "Press *Start* now\n")
        assert engine.chats.count(PLACE_MARKS_PROMPT) == 1
        assert output == "[fr] Press *Start* now\n"
        assert done.review == {"total_count": 0}

    def test_marks_that_cannot_be_placed_fall_back_to_the_whole_paragraph(self) -> None:
        # The answer changes the translation: refused, the unit is written without marks.
        engine = MarkEngine(drop_marks=True, place={"[fr] Press Start now": "[fr] Press <g1>Go</g1> now"})
        done, output = self._run(engine, "Press *Start* now\n")
        assert output == "[fr] Press Start now\n"
        assert done.review == {"total_count": 0}

    def test_a_memory_hit_gets_its_marks_placed(self) -> None:
        tm_manager.insert_batch([("Press Start now", "Appuyez sur Démarrer")], "en-fr", 500, "test")
        engine = MarkEngine(place={"Appuyez sur Démarrer": "Appuyez sur <g1>Démarrer</g1>"})
        done, output = self._run(engine, "Press *Start* now\n")
        assert engine.sent == []
        assert output == "Appuyez sur *Démarrer*\n"
        assert done.tm_hit_count == 1

    def test_an_untranslated_unit_keeps_its_marked_source(self) -> None:
        engine = MarkEngine(fail={"Press <g1>Start</g1> now"})
        done, output = self._run(engine, "Press *Start* now\n")
        assert output == "Press *Start* now\n"
        assert done.review["total_count"] == 1


def test_marked_fields_survive_the_json_round_trip_and_default_to_empty() -> None:
    unit = TextUnit(id="1", text="a b", marked="a <g1>b</g1>")
    assert TextUnit.from_dict(unit.to_dict()) == unit
    assert TextUnit.from_dict({"id": "1", "text": "a"}).marked == ""
    done = TranslatedUnit(id="1", text="a b", translation="x y", marked_translation="x <g1>y</g1>")
    assert TranslatedUnit.from_dict(done.to_dict()) == done
    assert TranslatedUnit.from_dict({"id": "1", "text": "a"}).marked_translation == ""


def test_marks_that_do_not_match_the_text_are_ignored() -> None:
    units = validate_units(
        [
            TextUnit(id="ok", text="a b", marked="a <g1>b</g1>"),
            TextUnit(id="other text", text="a b", marked="a <g1>c</g1>"),
            TextUnit(id="unpaired", text="a b", marked="a <g1>b"),
            TextUnit(id="nested", text="a b c", marked="<g1>a <g2>b</g2></g1> c"),
            TextUnit(id="no marks", text="a b", marked="a b"),
        ]
    )
    assert [unit.marked for unit in units] == ["a <g1>b</g1>", "", "", "", ""]


# ── 仅译文：夹杂多种语言的内容 ─────────────────────────────────────────────


class MixedEngine(FakeEngine):
    """Answers the structured mixed-language request with a fixed action per text."""

    def __init__(self, actions: dict[str, tuple[str, str]], translations: dict[str, str]) -> None:
        super().__init__()
        self.actions = actions
        self.translations = translations
        self.mixed_sent: list[str] = []

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        with self.lock:
            self.calls.append(list(texts))
            self.prompts.append(system_prompt)
        return {text: self.translations.get(text, text) for text in texts}

    def chat(self, system, user):
        try:
            payload = json.loads(user)
        except ValueError:
            return super().chat(system, user)
        if not isinstance(payload, list):
            return super().chat(system, user)
        answers = []
        for item in payload:
            self.mixed_sent.append(item["text"])
            action, translation = self.actions[item["text"]]
            answers.append({"id": item["id"], "action": action, "translation": translation})
        return json.dumps(answers, ensure_ascii=False)


class MixedLanguageRuleTests(IsolatedAppDataTestCase):
    MIXED = "土石方工程合计 Total of Earthworks"
    NOISE = "钢筋 Rebar 规格"

    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "docs"
        self.source.mkdir(parents=True)
        manifest = load_manifest(ECHO_DIR)
        self.surface = AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest))

    def _run(self, engine: FakeEngine, body: str, *, bilingual: bool = False, source_lang: str = "zh") -> tuple[DoneMsg, str]:
        path = self.source / "a.txt"
        path.write_text(body, encoding="utf-8")
        settings = AppSettings(target_lang="fr")
        settings.extensions["echo"] = ExtensionSettings(smart_context=False)
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine), patch(
            "core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None
        ):
            runner = self.surface.build_runner(
                RunnerRequest(
                    files=[_item(path)],
                    settings=settings,
                    source_root=self.source,
                    options=TaskOptions(options={"bilingual": bilingual}),
                    source_lang=source_lang,
                    key_overrides={},
                )
            )
            runner.start()
            messages = _drain(runner)
        done = messages[-1]
        assert isinstance(done, DoneMsg), done
        return done, Path(done.file_results[0]["output_path"]).read_text(encoding="utf-8")

    def test_existing_bilingual_is_translated_whole_in_translation_only_mode(self) -> None:
        engine = MixedEngine(
            {self.MIXED: ("existing_bilingual", "")},
            {self.MIXED: "Total Total des terrassements", "设备清单": "Liste des équipements"},
        )
        done, output = self._run(engine, f"{self.MIXED}\n设备清单\n")
        # Asked again on the mixed path first (translation only refuses 「already bilingual」).
        assert set(engine.mixed_sent) == {self.MIXED}
        # The ordinary line never goes the mixed way; the mixed one is re-sent as a whole, echo collapsed.
        assert engine.sent == ["设备清单", self.MIXED]
        assert output == "Total des terrassements\nListe des équipements\n"
        assert done.review == {"total_count": 0}

    def test_existing_bilingual_that_stays_untranslated_keeps_the_source_for_review(self) -> None:
        engine = MixedEngine({self.MIXED: ("existing_bilingual", "")}, {})
        done, output = self._run(engine, f"{self.MIXED}\n")
        assert output == f"{self.MIXED}\n"
        assert [item["issue"] for item in done.review["items"]] == [REASON_MIXED_KEPT]
        assert done.review["items"][0]["action"] == "已保留原文"

    def test_foreign_words_left_in_the_translation_are_listed(self) -> None:
        engine = MixedEngine({self.MIXED: ("translate", "Total of Earthworks")}, {})
        done, output = self._run(engine, f"{self.MIXED}\n")
        assert output == "Total of Earthworks\n"
        assert [item["issue"] for item in done.review["items"]] == [REASON_MIXED_LEFTOVER]
        assert done.review["items"][0]["action"] == "已保留译文"

    def test_bilingual_mode_and_non_chinese_sources_keep_the_ordinary_path(self) -> None:
        engine = MixedEngine({}, {self.MIXED: "Total des terrassements"})
        _done, output = self._run(engine, f"{self.MIXED}\n", bilingual=True)
        assert engine.mixed_sent == [] and engine.sent == [self.MIXED]
        assert output == f"{self.MIXED}\nTotal des terrassements\n"

        engine = MixedEngine({}, {self.MIXED: "Total des terrassements"})
        _done, output = self._run(engine, f"{self.MIXED}\n", source_lang="en")
        assert engine.mixed_sent == []
