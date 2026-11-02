"""#283 复查：一批多个 Word 文件，写出和成品体检按各文件自己认出的原文语言判。

英文件 A、日文件 B 同批译中文时，以前写出一律用全批第一个认出的语言（英文）。
B 里只有汉字的段落按「未知/日文」付费翻了，写出时却按英文判成已是中文、被筛掉。
"""
from __future__ import annotations

import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document

from core.language_preflight import TranslationLanguageResult
from core.task_runner import DoneMsg
from core.word_document import WordFileItem, WordSegment
from core.word_task_runner import WordTaskRunner
from tests import test_phase5_word_contracts as _contracts
from tests.app_data_isolation import IsolatedAppDataTestCase

ENGLISH = "Pump room layout drawing."
KANJI = "安全注意事項"


class PerFileSourceLanguageTests(IsolatedAppDataTestCase):
    def _run(self, replies: dict[str, str]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "a_en.docx", root / "b_ja.docx"]
            texts = {paths[0]: ENGLISH, paths[1]: KANJI}
            prepared = {}
            for path in paths:
                Document().save(path)
                prepared[path] = SimpleNamespace(
                    path=path, method="编号预处理：Python 兜底", temp_paths=(),
                    fallback_messages=(), labels_seen=0, labels_prepended=0,
                    conversion_method="not_required", conversion_fidelity="not_required",
                    numbering_method="python_conservative", numbering_fallback_messages=(),
                )
            settings = _contracts.WordTaskResultContractTests._settings()
            settings.target_lang = "zh"
            runner = WordTaskRunner(
                [WordFileItem(path=path, name=path.name, size_kb=1.0) for path in paths],
                settings, source_root=root, source_lang="auto",
            )

            class _PreflightEngine:
                engine_name = "mock"

                def chat(self, system: str, user: str) -> str:
                    sample = json.loads(user)["samples"][0]
                    return replies[sample]

            def translate(batch, _engine, target_lang, *_args, **kwargs):
                for text in batch:
                    kwargs["source_result_callback"](
                        text,
                        TranslationLanguageResult(
                            source_text=text, translation=f"译：{text}",
                            source_lang="ja" if text == KANJI else "en", target_lang=target_lang,
                        ),
                    )
                    kwargs["candidate_callback"](text, f"译：{text}")
                return {text: f"译：{text}" for text in batch}

            def segments(path, **kwargs):
                return [WordSegment(source=texts[Path(path)], kind="paragraph", location="正文第 1 段")]

            with ExitStack() as stack:
                writer = _contracts.WordTaskResultContractTests._runner_patches(
                    self, stack, root=root, prepared_by_path=prepared
                )
                coverage = stack.enter_context(
                    patch("core.word_task_runner._append_post_write_coverage_issues", return_value=0)
                )
                stack.enter_context(patch("core.word_task_runner._WordRecoveryPool.add_candidate"))
                stack.enter_context(patch("core.word_task_runner.build_engine", return_value=_PreflightEngine()))
                stack.enter_context(patch("core.word_task_runner.extract_word_segments", side_effect=segments))
                stack.enter_context(patch("core.word_task_runner.tm_manager.lookup_batch", return_value={}))
                stack.enter_context(patch("core.word_task_runner.translate_word_texts", side_effect=translate))
                runner._run()

            _contracts.WordTaskResultContractTests._terminal_message(runner, DoneMsg)
            by_source = {
                Path(call.kwargs["source_path"]).name: call.kwargs["source_lang"]
                for call in writer.call_args_list
            }
            checked = [call.kwargs["source_lang"] for call in coverage.call_args_list]
            return by_source, checked

    def test_each_file_is_written_with_its_own_detected_language(self) -> None:
        by_source, checked = self._run({
            ENGLISH: '{"source_langs":["en"]}',
            KANJI: '{"source_langs":["ja"]}',
        })
        self.assertEqual(by_source, {"a_en.docx": "en", "b_ja.docx": "ja"})
        self.assertEqual(sorted(checked), ["en", "ja"])

    def test_undetected_file_is_written_as_unknown_not_with_another_files_language(self) -> None:
        by_source, _checked = self._run({
            ENGLISH: '{"source_langs":["en"]}',
            KANJI: "???",
        })
        self.assertEqual(by_source, {"a_en.docx": "en", "b_ja.docx": "auto"})
