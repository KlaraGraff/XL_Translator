"""记忆库在 CAD、PDF 原位替换、PDF 转 Word 这几条文字翻译路径上的规则（与 Excel / Word 相同）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import tm_manager
from core.cad_review import _rows
from core.cad_translation import CadPipelineOptions, CadTranslationPipeline
from core.engine_dispatcher import translate_texts
from core.term_session import DocumentTermMemory, merge_term_results
from engines.base_engine import TranslationEngine

TM_SETTINGS = SimpleNamespace(
    apply_in_sentences=True,
    term_retry_attempts=2,
    term_batch_budget=20,
    auto_save_short_scene=True,
    local_scene_candidates=False,
)


def _dxf(*texts: str) -> str:
    body = "".join(f"0\nTEXT\n1\n{text}\n" for text in texts)
    return f"0\nSECTION\n2\nENTITIES\n{body}0\nENDSEC\n0\nEOF\n"


class FakeEngine(TranslationEngine):
    """「预埋件」只有在提示词里看到设定译法（或改正说明）时才照用；``comply`` 为假时一直不用。"""

    engine_name = "test/mock"

    def __init__(self, answers: dict[str, str], *, comply: bool = True) -> None:
        self.answers = answers
        self.comply = comply
        self.prompts: list[str] = []
        self.batches: list[list[str]] = []
        self.chats: list[str] = []

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.prompts.append(system_prompt)
        self.batches.append(list(texts))
        out = {}
        for text in texts:
            if "预埋件" in text:
                told = "预埋件 → pièce noyée" in system_prompt
                out[text] = "fixé à la pièce noyée" if (told and self.comply) else "fixé à la pièce à sceller"
            else:
                out[text] = self.answers.get(text, f"FR {len(text)}")
        return out

    def chat(self, system, user):
        self.chats.append(user)
        return json.dumps({"甲": "防火门耐火等级"}, ensure_ascii=False)


class _IsolatedTm(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (
            patch.object(tm_manager, "DB_PATH", self.root / "tm.db"),
            patch.object(tm_manager, "BACKUPS_DIR", self.root / "backups"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tm_manager.init_db()
        tm_manager.insert_manual_entry_detailed("预埋件", "pièce noyée", "zh-fr")
        tm_manager.insert_manual_entry_detailed("说明", "Notes", "zh-fr")


class CadTermMemoryTests(_IsolatedTm):
    def _run(self, engine: FakeEngine, *, smart_mode=False, profile=""):
        source = self.root / "plan.dxf"
        source.write_text(_dxf("门框与预埋件连接", "说明", "设备基础", "甲"), encoding="utf-8")
        memory = DocumentTermMemory(
            TM_SETTINGS,
            target_lang="fr",
            lang_pairs=["zh-fr"],
            smart_mode=smart_mode,
            engine_provider=lambda: engine,
            marked_in="cad_review",
        )

        # 与 api/surfaces/cad.py 的翻译器同一写法：会话把引擎包一层再交给调度。
        def translator(texts, glossary, context="", term_session=None):
            wrapped = term_session.wrap(engine) if term_session is not None else engine
            return translate_texts(texts, wrapped, "fr", "SYS" + context, 20, 1, source_lang="zh")

        pipeline = CadTranslationPipeline(
            converter=None,
            translator=translator,
            term_memory=memory,
            profile_requester=(lambda texts, log, neighbor_rows=(): profile) if profile else None,
        )
        result = pipeline.translate_file(
            source, self.root / "plan_fr.dxf", options=CadPipelineOptions(source_lang="zh", target_lang="fr")
        )
        return result, memory

    def test_manual_term_is_applied_in_a_sentence_and_whole_entry_is_reused(self):
        engine = FakeEngine({"设备基础": "Fondation d'équipement", "甲": "A"})
        result, memory = self._run(engine)
        # 「说明」整段复用手动维护的译法，不送模型；含「预埋件」的那句带着设定译法翻译。
        self.assertEqual(result.stats.memory_hits, 1)
        self.assertNotIn("说明", [text for batch in engine.batches for text in batch])
        self.assertIn("预埋件 → pièce noyée", engine.prompts[0])
        self.assertEqual(result.term_review, [])
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        statuses = {unit["source"]: (unit["status"], unit["translation"]) for unit in manifest["units"]}
        self.assertEqual(statuses["门框与预埋件连接"], ("translated", "fixé à la pièce noyée"))
        self.assertEqual(statuses["说明"], ("translated", "Notes"))
        report = memory.finish()
        self.assertEqual(report["not_applied"], [])
        self.assertEqual(report["manual_terms_hit"], 2)

    def test_not_applied_after_two_retries_is_written_and_marked_for_review(self):
        engine = FakeEngine({"设备基础": "Fondation d'équipement", "甲": "A"}, comply=False)
        result, memory = self._run(engine)
        term_prompts = [p for p, batch in zip(engine.prompts, engine.batches) if "门框与预埋件连接" in batch]
        self.assertEqual(len(term_prompts), 3)  # 首次 + 重译 2 次
        self.assertEqual([item["source"] for item in result.term_review], ["门框与预埋件连接"])
        self.assertIn("重译 2 次后仍未采用", result.term_review[0]["reason"])
        self.assertEqual(result.stats.term_not_applied, 1)
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        unit = next(unit for unit in manifest["units"] if unit["source"] == "门框与预埋件连接")
        self.assertEqual((unit["status"], unit["translation"]), ("review", "fixé à la pièce à sceller"))
        # 译文对照表里这一行是「需复核」，带着原因。
        row = next(row for row in _rows(manifest, {}) if row["source"] == "门框与预埋件连接")
        self.assertEqual(row["status"], "review")
        self.assertIn("pièce noyée", row["reason"])
        # 译文照常写进图纸（DXF 里非 ASCII 字符写成 \U+XXXX）。
        self.assertIn("la pi\\U+00E8ce \\U+00E0 sceller", (self.root / "plan_fr.dxf").read_text(encoding="utf-8"))
        report = memory.finish()
        self.assertEqual(report["marked_in"], "cad_review")
        self.assertEqual(report["not_applied"][0]["locations"], ["plan.dxf"])
        self.assertEqual(report["not_applied"][0]["terms"], [{"term": "预埋件", "expected": "pièce noyée"}])
        # 收尾只记一次使用次数，再调一次结果不变。
        self.assertEqual(memory.finish(), report)

    def test_short_words_are_auto_saved_with_a_scene_note_in_smart_mode(self):
        engine = FakeEngine({"设备基础": "Fondation d'équipement", "甲": "classe A"})
        _result, memory = self._run(engine, smart_mode=True, profile="住宅楼防火门图纸")
        report = memory.finish()
        self.assertEqual([(item["term"], item["target"], item["note"]) for item in report["auto_saved"]],
                         [("甲", "classe A", "防火门耐火等级")])
        rows = [row for row in tm_manager.list_entries_for_target("fr", ["zh-fr"]) if row["source_text"] == "甲"]
        self.assertEqual([(row["target_text"], row["note"]) for row in rows], [("classe A", "防火门耐火等级")])

    def test_scope_follows_the_drawing_path(self):
        tm_manager.insert_manual_entry_detailed("设备基础", "socle", "zh-fr", scope="住宅")
        engine = FakeEngine({"设备基础": "Fondation d'équipement", "甲": "A"})
        result, _memory = self._run(engine)
        manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
        unit = next(unit for unit in manifest["units"] if unit["source"] == "设备基础")
        # 图纸路径里没有「住宅」：这条不生效，也不会被旧式整段查找带回来。
        self.assertEqual(unit["translation"], "Fondation d'équipement")


class CadRunnerTermTests(_IsolatedTm):
    def test_runner_marks_the_file_and_reports_terms(self):
        import core.process_artifacts as process_artifacts
        from core.cad_task_runner import CadTaskRunner

        engine = FakeEngine({"设备基础": "Fondation d'équipement"}, comply=False)
        source = self.root / "drawing.dxf"
        source.write_text(_dxf("门框与预埋件连接", "设备基础"), encoding="utf-8")
        memory = DocumentTermMemory(
            TM_SETTINGS, target_lang="fr", lang_pairs=["zh-fr"], smart_mode=False,
            engine_provider=lambda: engine, marked_in="cad_review",
        )

        def translator(texts, glossary, term_session=None):
            return translate_texts(texts, term_session.wrap(engine) if term_session else engine, "fr", "SYS", 20, 1)

        with patch.object(process_artifacts, "APP_DATA_DIR", self.root / "app-data"):
            runner = CadTaskRunner(
                files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
                source_root=self.root,
                output_dir=self.root / "results",
                translator=translator,
                term_memory=memory,
                options=CadPipelineOptions(source_lang="zh", target_lang="fr"),
            )
            runner.start()
            messages = []
            while runner.needs_poll():
                message = runner.get_message(timeout=1)
                if message is not None:
                    messages.append(message)
        done = next(message for message in messages if hasattr(message, "file_results"))
        self.assertEqual(done.file_results[0]["status"], "needs_review")
        self.assertEqual(done.file_results[0]["stats"]["term_not_applied"], 1)
        self.assertTrue(any(issue.get("reason") == "term_not_applied" for issue in done.issues))
        self.assertEqual(len(done.terms["not_applied"]), 1)
        self.assertEqual(done.terms["marked_in"], "cad_review")


class InplaceTermMemoryTests(_IsolatedTm):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.insert_manual_entry_detailed("béton armé", "钢筋混凝土", "fr-zh")
        tm_manager.insert_manual_entry_detailed("Coupe A-A", "A-A 剖面", "fr-zh")

    def _rows(self):
        return [
            {"id": 1, "source_text": "Coupe A-A", "origin": "native"},
            {"id": 2, "source_text": "Mur en béton armé", "origin": "native"},
            {"id": 3, "source_text": "Fondation superficielle", "origin": "native"},
        ]

    def _run(self, *, comply: bool):
        from core.pdf_vector.classify import classify_and_translate

        prompts: list[str] = []

        class Engine(TranslationEngine):
            engine_name = "test/mock"

            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                prompts.append(system_prompt)
                told = "béton armé → 钢筋混凝土" in system_prompt
                answers = {"Mur en béton armé": "钢筋混凝土墙" if (told and comply) else "加筋混凝土墙",
                           "Fondation superficielle": "浅基础"}
                return {text: answers.get(text, text) for text in texts}

        engine = Engine()
        memory = DocumentTermMemory(TM_SETTINGS, target_lang="zh", lang_pairs=(), smart_mode=False,
                                    engine_provider=lambda: engine, marked_in="pdf_pages")

        # 与 api/surfaces/pdf.py 的 translate 同一写法。
        def translate(texts, prompt, should_stop=None, term_session=None):
            wrapped = term_session.wrap(engine) if term_session is not None else engine
            return translate_texts(texts, wrapped, "zh", prompt, 20, 1, source_lang="auto")

        rows = self._rows()
        summary = classify_and_translate(rows, translate=translate, source_lang="fr", memory=memory,
                                         memory_file=self.root / "plan.pdf", memory_location="plan.pdf 第 1 页")
        return {row["source_text"]: row for row in rows}, summary, memory, prompts

    def test_whole_label_comes_from_memory_and_term_is_applied(self):
        rows, summary, memory, prompts = self._run(comply=True)
        self.assertEqual((rows["Coupe A-A"]["translation"], rows["Coupe A-A"]["translation_source"]),
                         ("A-A 剖面", "memory"))
        self.assertNotIn("Coupe A-A", "".join(prompts))
        self.assertEqual(rows["Mur en béton armé"]["translation"], "钢筋混凝土墙")
        self.assertEqual(summary["memory_hits"], 1)
        self.assertEqual(summary["term_review"], 0)
        self.assertEqual(memory.finish()["manual_terms_hit"], 2)

    def test_not_applied_is_written_and_listed(self):
        rows, summary, memory, prompts = self._run(comply=False)
        row = rows["Mur en béton armé"]
        self.assertEqual((row["decision"], row["translation"]), ("write", "加筋混凝土墙"))
        self.assertIn("重译 2 次后仍未采用", row["term_review"])
        self.assertEqual(summary["term_review"], 1)
        self.assertEqual(len(prompts), 3)
        report = memory.finish()
        self.assertEqual(report["not_applied"][0]["locations"], ["plan.pdf 第 1 页"])
        self.assertEqual(report["marked_in"], "pdf_pages")


class PdfWordRouteScopeTests(unittest.TestCase):
    def test_word_route_judges_scope_by_the_original_pdf(self):
        from core import pdf_word_route
        from core.task_runner import DoneMsg

        captured = {}

        class FakeRunner:
            def __init__(self, items, settings, **kwargs):
                captured.update(kwargs)
                self._sent = False

            def start(self):
                pass

            def needs_poll(self):
                return not self._sent

            def stop(self):
                pass

            def get_message(self, timeout=0.2):
                self._sent = True
                return DoneMsg(output_dir="", file_results=[], elapsed_sec=0, tm_hit_count=0, api_call_count=0,
                               terms={"not_applied": [{"snippet": "x", "locations": ["第 2 段"]}]})

        settings = SimpleNamespace(
            pdf=SimpleNamespace(target_lang="fr"), target_lang="fr",
            model_copy=lambda deep=True: SimpleNamespace(
                pdf=SimpleNamespace(target_lang="fr"), target_lang="fr",
                word_output=SimpleNamespace(),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(pdf_word_route, "convert_pdf_to_docx", lambda *a, **k: None), \
                    patch("core.word_document.scan_word_path", lambda path: [SimpleNamespace(path=path)]), \
                    patch("core.word_task_runner.WordTaskRunner", FakeRunner):
                result = pdf_word_route.translate_pdf_via_word(
                    root / "seg.pdf", root / "work", root / "out.docx", settings,
                    source_lang="zh", should_stop=lambda: False, log=lambda *_: None,
                    term_file_paths=[Path("/项目/住宅/原图.pdf")],
                )
        self.assertEqual(captured["term_file_paths"], [Path("/项目/住宅/原图.pdf")])
        self.assertEqual(result.terms["not_applied"][0]["locations"], ["第 2 段"])


class MergeTermResultsTests(unittest.TestCase):
    def test_parts_are_summed_and_manual_ids_deduplicated(self):
        first = {
            "profile": "图纸说明", "hits": {"always": 1, "scene": 0, "segment": 2},
            "applied_direct": 3, "applied_after_retry": 1, "not_applied": [{"snippet": "a"}],
            "auto_saved": [], "scene_choices": [{"term": "甲", "candidates": [], "chosen": [{"target": "A", "count": 1}], "count": 1}],
            "manual_terms_total": 5, "manual_terms_hit": 2, "manual_hit_ids": [1, 2], "retry_attempts": 2,
        }
        second = {
            "profile": "", "hits": {"always": 2, "scene": 1, "segment": 0},
            "applied_direct": 1, "applied_after_retry": 0, "not_applied": [{"snippet": "b"}],
            "auto_saved": [{"id": 9}], "scene_choices": [{"term": "甲", "candidates": [], "chosen": [{"target": "A", "count": 2}], "count": 2}],
            "manual_terms_total": 5, "manual_terms_hit": 2, "manual_hit_ids": [2, 3], "retry_attempts": 2,
            "marked_in": "pdf_pages",
        }
        merged = merge_term_results([first, None, second])
        self.assertEqual(merged["hits"], {"always": 3, "scene": 1, "segment": 2})
        self.assertEqual((merged["applied_direct"], merged["applied_after_retry"]), (4, 1))
        self.assertEqual([item["snippet"] for item in merged["not_applied"]], ["a", "b"])
        self.assertEqual(merged["manual_terms_hit"], 3)
        self.assertEqual(merged["scene_choices"][0]["chosen"], [{"target": "A", "count": 3}])
        self.assertEqual(merged["profile"], "图纸说明")
        # 转 Word 的段标在文件里、原位替换的页列在逐页结果：说法笼统一些。
        self.assertEqual(merged["marked_in"], "mixed")
        self.assertEqual(merge_term_results([]), {})


if __name__ == "__main__":
    unittest.main()
