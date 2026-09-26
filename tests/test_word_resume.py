"""core/word_task_runner.py 的 Word 续译路径（resume_output_dir）契约测试。

覆盖 :372-406（_resolve_resume_process_path）与 :788-865（换底稿决策落点，含分歧核查）
一带：换底稿、找不到产物时降级源文件、auto 源语言下照样换底稿（Word 的补译计划本就按
get_default_source_lang 建，与常规 auto+补译一致）、底稿损坏降级、
resume 目录只读、不带 resume_output_dir 时行为不变。

另覆盖分歧核查这条护栏：换底稿前 runner 会调用
baseline_missing_source_texts(surface="word", ...) 核对源文件是否比上次翻译时长出了
底稿不认识的内容——source_only 且底稿全集里找不到（含 Word 特有的编号前缀容差）。
一旦源文件"变大"，底稿即被判定不可信，日志打出含「续译核对」的 WARNING，本文件退回
按源文件整篇处理（自动/非自动源语言两条路径都要覆盖，见下方两组分歧测试）。

relative_path 的取值同样是契约的一部分：生产环境里 WordFileItem.relative_path 是
_relative_word_path 填的、【含文件名】的相对路径（"report.docx"、"sub/inner.docx"），
_resolve_resume_process_path 必须先取其 .parent 才能拼出正确的底稿目录——本文件里
每一处 WordFileItem 构造都按这个真实形状给 relative_path，并有一个子目录场景专门
钉住这条修复（Fix #0）。

翻译全靠 tm_manager.lookup_batch 一次性命中挡在 TM 层——不触发任何真实模型调用，
镜像 tests/test_phase5_word_contracts.py 与 tests/test_word_defect_fixes.py 里
WordTaskRunner 全流程测试的既有替身写法（同一批 core.word_task_runner.* 补丁点）。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from docx import Document

from core.api_config_check import ApiConfigCheckResult
from core.bilingual_writer import bilingual_output_name
from core.language_preflight import LanguagePreflightResult
from core.language_registry import get_target_lang_display
from core.model_throughput import EffectiveModelThroughput
from core.task_runner import DoneMsg, LogMsg
from core.word_document import WordFileItem, _normalize_word_output_name
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


def _tm_translation_stub(texts, _pair):
    """把送查的每个文本都命中成同一个占位译文——不含中文，绕开残留中文体检。"""
    return {text: "TM translation" for text in texts}


def _auto_zh_preflight_stub(files, _detector, *, target_lang):  # noqa: ARG001
    """auto 源语言路径的语言预检替身：每个文件都判定成中文源。"""
    return {
        str(path): LanguagePreflightResult(source_langs=("zh",), candidates=tuple(texts))
        for path, texts in files.items()
    }


def _passthrough_prepare(path: Path, **_kwargs):
    """跳过真实的 doc→docx 转换/编号预处理：直接把传入路径当成处理结果。"""
    return SimpleNamespace(
        path=path,
        method="test-passthrough",
        temp_paths=(),
        fallback_messages=(),
        labels_seen=0,
        labels_prepended=0,
        conversion_method="not_required",
        conversion_fidelity="not_required",
        numbering_method="python_conservative",
        numbering_fallback_messages=(),
    )


class WordResumeTests(IsolatedAppDataTestCase):
    @staticmethod
    def _settings(*, target_lang: str = "en") -> AppSettings:
        return AppSettings(source_lang="zh", target_lang=target_lang)

    @staticmethod
    def _terminal_message(runner: WordTaskRunner, message_type):
        matches = [
            message
            for message in list(runner._queue.queue)
            if isinstance(message, message_type)
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one {message_type.__name__}, got {len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _log_messages(runner: WordTaskRunner, level: str | None = None) -> list[str]:
        return [
            message.message
            for message in list(runner._queue.queue)
            if isinstance(message, LogMsg) and (level is None or message.level == level)
        ]

    def _runner_patches(self, stack: ExitStack, *, root: Path) -> None:
        stack.enter_context(
            patch("core.word_task_runner.TaskLogger", return_value=MagicMock(task_id="word-resume"))
        )
        stack.enter_context(
            patch(
                "core.word_task_runner.check_translation_api_config",
                return_value=ApiConfigCheckResult(ok=True),
            )
        )
        stack.enter_context(
            patch(
                "core.word_task_runner.build_engine",
                return_value=SimpleNamespace(engine_name="resume/mock"),
            )
        )
        stack.enter_context(patch("core.word_task_runner.get_system_prompt", return_value="system"))
        stack.enter_context(
            patch("core.word_task_runner.resolve_effective_model_config", return_value=object())
        )
        stack.enter_context(
            patch(
                "core.word_task_runner.get_model_throughput",
                return_value=EffectiveModelThroughput(
                    profile_key="resume", batch_size=10, concurrency=1
                ),
            )
        )
        stack.enter_context(
            patch("core.word_task_runner.build_word_output_dir", return_value=root / "out")
        )
        stack.enter_context(
            patch("core.word_task_runner._append_post_write_coverage_issues", return_value=0)
        )
        stack.enter_context(
            patch(
                "core.word_task_runner._write_word_quality_report",
                return_value=root / "out" / "word_translation_report.md",
            )
        )
        stack.enter_context(
            patch("core.word_task_runner.tm_manager.lookup_batch", side_effect=_tm_translation_stub)
        )
        stack.enter_context(patch("core.word_task_runner.tm_manager.insert_batch", return_value=0))
        stack.enter_context(
            patch(
                "core.word_task_runner._prepare_word_source_for_translation",
                side_effect=_passthrough_prepare,
            )
        )

    @staticmethod
    def _make_docx(path: Path, paragraphs: list[str]) -> None:
        doc = Document()
        for text in paragraphs:
            doc.add_paragraph(text)
        doc.save(path)

    @staticmethod
    def _bilingual_name(source_name: str, target_lang: str) -> str:
        lang_display = get_target_lang_display(target_lang, include_optional=True)
        return bilingual_output_name(_normalize_word_output_name(source_name), lang_display)

    def _run_success(self, runner: WordTaskRunner, root: Path, **extra_patches) -> DoneMsg:
        with ExitStack() as stack:
            self._runner_patches(stack, root=root)
            for target, kwargs in extra_patches.items():
                stack.enter_context(patch(target, **kwargs))
            runner._run()
        done = self._terminal_message(runner, DoneMsg)
        self.assertTrue(done.file_results and done.file_results[0]["success"], done.file_results)
        return done

    def test_post_write_audit_failure_does_not_publish_output(self) -> None:
        for existing_output in (False, True):
            with self.subTest(existing_output=existing_output), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "report.docx"
                self._make_docx(source, ["项目名称"])
                final_dir = root / "out"
                final_dir.mkdir()
                final = final_dir / self._bilingual_name(source.name, "en")
                if existing_output:
                    self._make_docx(final, ["之前的译文"])
                previous = final.read_bytes() if existing_output else None
                runner = WordTaskRunner(
                    [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                    self._settings(),
                    source_root=root,
                )

                with ExitStack() as stack:
                    self._runner_patches(stack, root=root)
                    stack.enter_context(
                        patch(
                            "core.word_task_runner._append_post_write_coverage_issues",
                            side_effect=RuntimeError("audit failed"),
                        )
                    )
                    runner._run()

                done = self._terminal_message(runner, DoneMsg)
                self.assertFalse(done.file_results[0]["success"])
                self.assertNotIn("output", done.file_results[0])
                self.assertEqual(final.read_bytes() if final.exists() else None, previous)
                self.assertEqual(list(final_dir.iterdir()), [final] if existing_output else [])

    def test_task_statistics_failure_after_commit_keeps_successful_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "report.docx"
            self._make_docx(source, ["项目名称"])
            runner = WordTaskRunner(
                [WordFileItem(path=source, name=source.name, size_kb=1.0)],
                self._settings(),
                source_root=root,
            )
            with ExitStack() as stack:
                self._runner_patches(stack, root=root)
                stack.enter_context(
                    patch.object(
                        runner._task_logger,
                        "file_done",
                        side_effect=RuntimeError("statistics failed"),
                    )
                )
                runner._run()

            done = self._terminal_message(runner, DoneMsg)
            self.assertTrue(done.file_results[0]["success"])
            final = Path(done.file_results[0]["output"])
            self.assertTrue(final.is_file())
            self.assertEqual(final.parent, root / "out")

    # ------------------------------------------------------------------
    # 1) resume + untranslated_only：换底稿，只补未译段落，输出名无叠加后缀
    # ------------------------------------------------------------------

    def test_resume_swaps_baseline_and_only_backfills_untranslated_paragraphs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()
            source = root / "report.docx"
            # 源文件是底稿两个源文半边的原样重现：分歧核查（baseline_missing_
            # source_texts）不会因为源文件"比底稿多"而拒用底稿。
            self._make_docx(source, ["已翻译标题", "未翻译内容"])

            baseline_path = resume_dir / self._bilingual_name("report.docx", "en")
            self._make_docx(
                baseline_path,
                ["已翻译标题", "Translated Title", "未翻译内容"],
            )

            item = WordFileItem(
                path=source, name="report.docx", size_kb=1.0, relative_path="report.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = self._run_success(runner, root)
            out_path = Path(done.file_results[0]["output"])

            # 输出名仍按原始源文件名计算：不产生 _双语_双语 叠加。
            self.assertEqual(out_path.name, self._bilingual_name("report.docx", "en"))
            self.assertEqual(out_path.name.count("双语"), 1)

            out_texts = [p.text for p in Document(str(out_path)).paragraphs]
            # 已有的译文对原样保留（底稿换成了上次的双语产物）。
            self.assertIn("已翻译标题", out_texts)
            self.assertIn("Translated Title", out_texts)
            # 只补未译段落。
            self.assertIn("未翻译内容", out_texts)
            self.assertIn("TM translation", out_texts)

            log_text = "\n".join(self._log_messages(runner))
            self.assertIn("复用上次双语产物为底稿", log_text)
            # 源文件没有比底稿多出内容，分歧核查不应触发降级。
            self.assertNotIn("续译核对", log_text)

    def test_translated_output_filename_applies_to_full_and_untranslated_runs(self) -> None:
        for untranslated_only in (False, True):
            with self.subTest(untranslated_only=untranslated_only), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "报告.docx"
                self._make_docx(source, ["待翻译内容"])
                settings = self._settings()
                settings.word_output.translate_output_filename = True
                item = WordFileItem(
                    path=source,
                    name=source.name,
                    size_kb=1.0,
                    relative_path=source.name,
                )
                runner = WordTaskRunner(
                    [item],
                    settings,
                    source_root=root,
                    untranslated_only=untranslated_only,
                )
                done = self._run_success(
                    runner,
                    root,
                    **{
                        "core.word_task_runner.translate_output_stem": {
                            "return_value": "Translated report"
                        }
                    },
                )
                output_path = Path(done.file_results[0]["output"])
                self.assertEqual(
                    output_path.name,
                    self._bilingual_name("Translated report.docx", "en"),
                )

    # ------------------------------------------------------------------
    # 2) 匹配不到 → 源文件全量翻
    # ------------------------------------------------------------------

    def test_no_match_falls_back_to_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()  # 存在，但没有这份文件的产物
            source = root / "fresh.docx"
            self._make_docx(source, ["全新未译内容"])

            item = WordFileItem(
                path=source, name="fresh.docx", size_kb=1.0, relative_path="fresh.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = self._run_success(runner, root)
            out_path = Path(done.file_results[0]["output"])
            out_texts = [p.text for p in Document(str(out_path)).paragraphs]
            self.assertIn("全新未译内容", out_texts)
            self.assertIn("TM translation", out_texts)

            log_text = "\n".join(self._log_messages(runner))
            self.assertNotIn("复用上次双语产物为底稿", log_text)

    # ------------------------------------------------------------------
    # 3) auto 源语言 → 照样换底稿，续译对默认配置也要兑现
    # ------------------------------------------------------------------

    def test_auto_source_lang_still_swaps_baseline(self) -> None:
        """「自动识别」是源语言的默认值，续译必须对它兑现。

        Word 与 Excel 结构不同：Excel 的补译清单在语言预检后重算，所以底稿要延迟
        换（task_runner._apply_deferred_resume_baselines）；Word 没有预检后的重算，
        auto 下补译计划一律按 get_default_source_lang() 建一次（word_task_runner.py
        :809-811 的 resume_source_lang 就是同一条约定），底稿因此可以在前期直接换，
        与 Word 常规 auto+补译 的处理保持一致。

        源文件是底稿两个源文半边的原样重现（与场景 1 同一套底稿覆盖关系），确保
        走的是「换底稿」这条主路径，而不是分歧核查降级路径——降级路径由下面
        test_auto_source_lang_divergent_baseline_falls_back_to_source 单独钉住。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()
            source = root / "auto.docx"
            self._make_docx(source, ["已翻译标题", "未翻译内容"])

            baseline_path = resume_dir / self._bilingual_name("auto.docx", "en")
            self._make_docx(
                baseline_path,
                ["已翻译标题", "Translated Title", "未翻译内容"],
            )

            item = WordFileItem(
                path=source, name="auto.docx", size_kb=1.0, relative_path="auto.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                source_lang="auto",
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )

            done = self._run_success(
                runner,
                root,
                **{
                    "core.word_task_runner.preflight_files": {
                        "side_effect": _auto_zh_preflight_stub
                    }
                },
            )
            out_path = Path(done.file_results[0]["output"])
            out_texts = [p.text for p in Document(str(out_path)).paragraphs]

            # 底稿换成了上次产物：已有译文对保留，未译段落补齐。
            self.assertIn("已翻译标题", out_texts)
            self.assertIn("Translated Title", out_texts)
            self.assertIn("未翻译内容", out_texts)
            self.assertIn("TM translation", out_texts)

            log_text = "\n".join(self._log_messages(runner))
            self.assertIn("复用上次双语产物为底稿", log_text)
            self.assertNotIn("续译核对", log_text)

    # ------------------------------------------------------------------
    # 3b) auto 源语言 + 源文件比底稿多出内容 → 分歧核查降级，不吞新增段落
    # ------------------------------------------------------------------

    def test_auto_source_lang_divergent_baseline_falls_back_to_source(self) -> None:
        """auto 路径下，源文件比上次翻译时长出的内容必须原样进产物，不能被底稿吞掉。

        对照 word_task_runner.py :836-858：换底稿前会用
        baseline_missing_source_texts(surface="word", ...) 核对源文件里每条待译
        文本是否都在底稿全集里。这里源文件比底稿多了一段底稿完全不认识的内容，
        核查必须判定「不可信」，日志打出含「续译核对」的 WARNING，本文件退回按
        源文件整篇处理——底稿的旧译文（"Translated Title"）不能残留进产物。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()
            source = root / "auto_grown.docx"
            self._make_docx(
                source,
                ["已翻译标题", "未翻译内容", "源文件新增的段落"],
            )

            baseline_path = resume_dir / self._bilingual_name("auto_grown.docx", "en")
            self._make_docx(
                baseline_path,
                ["已翻译标题", "Translated Title", "未翻译内容"],
            )

            item = WordFileItem(
                path=source,
                name="auto_grown.docx",
                size_kb=1.0,
                relative_path="auto_grown.docx",
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                source_lang="auto",
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )

            done = self._run_success(
                runner,
                root,
                **{
                    "core.word_task_runner.preflight_files": {
                        "side_effect": _auto_zh_preflight_stub
                    }
                },
            )
            out_path = Path(done.file_results[0]["output"])
            out_texts = [p.text for p in Document(str(out_path)).paragraphs]

            # 源文件新增的段落照常进产物、照常翻译——按源文件整篇处理，不是补译。
            self.assertIn("源文件新增的段落", out_texts)
            self.assertIn("TM translation", out_texts)
            # 底稿的旧译文不能残留：底稿已经整份被判定不可信、彻底不用。
            self.assertNotIn("Translated Title", out_texts)

            warning_logs = self._log_messages(runner, level="WARNING")
            self.assertTrue(
                any("续译核对" in message for message in warning_logs),
                warning_logs,
            )

    # ------------------------------------------------------------------
    # 3c) 非 auto（显式源语言）+ 源文件比底稿多出内容 → 同一条分歧核查护栏
    # ------------------------------------------------------------------

    def test_explicit_source_lang_divergent_baseline_falls_back_to_source(self) -> None:
        """分歧核查护栏不是 auto 专属：显式指定源语言时同样要拦住会漏译的换底稿。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()
            source = root / "grown.docx"
            self._make_docx(
                source,
                ["已翻译标题", "未翻译内容", "源文件新增的段落"],
            )

            baseline_path = resume_dir / self._bilingual_name("grown.docx", "en")
            self._make_docx(
                baseline_path,
                ["已翻译标题", "Translated Title", "未翻译内容"],
            )

            item = WordFileItem(
                path=source, name="grown.docx", size_kb=1.0, relative_path="grown.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = self._run_success(runner, root)
            out_path = Path(done.file_results[0]["output"])
            out_texts = [p.text for p in Document(str(out_path)).paragraphs]

            self.assertIn("源文件新增的段落", out_texts)
            self.assertIn("TM translation", out_texts)
            self.assertNotIn("Translated Title", out_texts)

            warning_logs = self._log_messages(runner, level="WARNING")
            self.assertTrue(
                any("续译核对" in message for message in warning_logs),
                warning_logs,
            )

    # ------------------------------------------------------------------
    # 3d) relative_path 含文件名（生产真实形状）+ 子目录 → 续译仍能定位到底稿
    # ------------------------------------------------------------------

    def test_resume_matches_baseline_in_subdirectory(self) -> None:
        """Fix #0 钉子测试：relative_path 必须含文件名，_resolve_resume_process_path

        要先取 .parent 才能拼出正确目录。旧测试全用 relative_path="" 的 dataclass
        默认值绕开了这个 bug——这里显式用子目录场景（"sub/inner.docx"）復现生产环境
        WordFileItem._relative_word_path 的真实形状，确保匹配逻辑真的按父目录去找。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            (resume_dir / "sub").mkdir(parents=True)
            source_dir = root / "sub"
            source_dir.mkdir()
            source = source_dir / "inner.docx"
            self._make_docx(source, ["已翻译标题", "未翻译内容"])

            baseline_path = (resume_dir / "sub") / self._bilingual_name("inner.docx", "en")
            self._make_docx(
                baseline_path,
                ["已翻译标题", "Translated Title", "未翻译内容"],
            )

            item = WordFileItem(
                path=source,
                name="inner.docx",
                size_kb=1.0,
                relative_path="sub/inner.docx",
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = self._run_success(runner, root)
            out_path = Path(done.file_results[0]["output"])

            self.assertEqual(out_path.parent.name, "sub")
            self.assertEqual(out_path.name, self._bilingual_name("inner.docx", "en"))

            out_texts = [p.text for p in Document(str(out_path)).paragraphs]
            self.assertIn("已翻译标题", out_texts)
            self.assertIn("Translated Title", out_texts)
            self.assertIn("未翻译内容", out_texts)
            self.assertIn("TM translation", out_texts)

            log_text = "\n".join(self._log_messages(runner))
            self.assertIn("复用上次双语产物为底稿", log_text)

    # ------------------------------------------------------------------
    # 4) 底稿损坏 → 降级源文件，且有日志
    # ------------------------------------------------------------------

    def test_corrupted_previous_output_falls_back_to_source_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()
            source = root / "report.docx"
            self._make_docx(source, ["源文件的正常内容"])

            baseline_path = resume_dir / self._bilingual_name("report.docx", "en")
            baseline_path.write_bytes(b"not a real docx file")  # 损坏的底稿

            item = WordFileItem(
                path=source, name="report.docx", size_kb=1.0, relative_path="report.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = self._run_success(runner, root)
            out_path = Path(done.file_results[0]["output"])
            out_texts = [p.text for p in Document(str(out_path)).paragraphs]
            self.assertIn("源文件的正常内容", out_texts)

            warning_logs = self._log_messages(runner, level="WARNING")
            self.assertTrue(
                any(
                    "续译底稿无法建立补译计划" in message
                    and "已按源文件正常处理" in message
                    for message in warning_logs
                ),
                warning_logs,
            )

    # ------------------------------------------------------------------
    # 5) resume 目录只读
    # ------------------------------------------------------------------

    def test_resume_dir_is_never_written_to(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_dir = root / "prev_output"
            resume_dir.mkdir()
            source = root / "report.docx"
            # 源文件与底稿的源文半边一致，走的是真正换底稿这条路径（而不是分歧
            # 核查降级）——这样「resume 目录只读」测的才是底稿真的被读取、复用
            # 时不写入，不是降级路径下底稿根本没被摸到的假阳性。
            self._make_docx(source, ["已翻译标题", "未翻译内容"])

            baseline_name = self._bilingual_name("report.docx", "en")
            baseline_path = resume_dir / baseline_name
            self._make_docx(
                baseline_path,
                ["已翻译标题", "Translated Title", "未翻译内容"],
            )

            def _fingerprint() -> dict[str, tuple[int, bytes]]:
                return {
                    str(entry.relative_to(resume_dir)): (
                        entry.stat().st_mtime_ns,
                        entry.read_bytes(),
                    )
                    for entry in sorted(resume_dir.rglob("*"))
                    if entry.is_file()
                }

            before = _fingerprint()

            item = WordFileItem(
                path=source, name="report.docx", size_kb=1.0, relative_path="report.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            self._run_success(runner, root)

            after = _fingerprint()
            self.assertEqual(before, after, "resume 目录必须只读，任何写入都是契约违反")
            self.assertEqual(
                sorted(entry.name for entry in resume_dir.iterdir()),
                [baseline_name],
                "resume 目录下不该冒出任何新文件",
            )

    # ------------------------------------------------------------------
    # 6) 不带 resume_output_dir：行为不变
    # ------------------------------------------------------------------

    def test_without_resume_output_dir_behavior_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plain.docx"
            self._make_docx(source, ["普通未译段落"])

            item = WordFileItem(
                path=source, name="plain.docx", size_kb=1.0, relative_path="plain.docx"
            )
            runner = WordTaskRunner(
                [item],
                self._settings(),
                source_root=root,
                untranslated_only=True,
            )
            self.assertIsNone(runner._resume_output_dir)

            with ExitStack() as stack:
                self._runner_patches(stack, root=root)
                match_mock = stack.enter_context(
                    patch("core.word_task_runner.match_previous_output")
                )
                runner._run()

            match_mock.assert_not_called()
            done = self._terminal_message(runner, DoneMsg)
            self.assertTrue(done.file_results[0]["success"], done.file_results)
            out_path = Path(done.file_results[0]["output"])
            out_texts = [p.text for p in Document(str(out_path)).paragraphs]
            self.assertIn("普通未译段落", out_texts)
            self.assertIn("TM translation", out_texts)


if __name__ == "__main__":
    unittest.main()
