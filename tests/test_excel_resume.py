"""Excel 续译执行层（core/task_runner.py 的 resume_output_dir 路径）契约测试。

背景：untranslated_only 补译时，若给了 resume_output_dir，程序应当把「上次输出的
双语文件」当底稿，只补那份底稿里还没翻的格子——而不是把这次的源文件从头翻一遍
（会把已经有的译文再翻一遍），也不是把双语底稿原样复制了事（新格子没人补）。换
底稿这件事只在补译识别计划真的会跑的前提下才安全：auto_source_lang 时语言预检
必须从【源文件】取样（底稿里混着译文，取样会污染语言判定），所以换底稿动作延迟
到语言定下来之后（_apply_deferred_resume_baselines），再在底稿上重建覆盖率计划
补译——底稿全程受覆盖率计划保护，绝不会被当普通源文件整份重翻。

覆盖场景（对应简报 I4 的验收清单，1:1 对应下面的测试函数）：
  1. 有匹配底稿 + untranslated_only → 换底稿，只补底稿里未译的格子；输出文件名
     仍按【原始源文件名】计算，不出现「_双语_双语」叠加；顺带验证镜像相对路径
     （源文件在子目录下）能正确匹配到 resume 目录同名子目录里的产物。
  2. resume 目录里没有这个文件对应的产物（只有别的文件的产物）→ 照常用源文件
     全量翻译。
  3. auto_source_lang（源语言=自动识别）→ 延迟换底稿：语言预检从源文件取样，
     语言定下来后换上底稿重建覆盖率计划，只补底稿里未译的格子；已译内容绝不
     被当成普通源文件重新整份送翻。
  4. 底稿文件损坏打不开 → 降级为源文件全量翻译，并留下 WARN 日志。
  5. resume 目录全程只读：跑完后目录内每个文件的指纹（相对路径+大小+mtime）
     必须与跑之前完全一致。
  6. 不传 resume_output_dir 时，行为与没有这个功能之前完全一致——不触发任何
     换底稿分支，日志里没有"续译"字样。

不真调翻译 API：翻译入口 core.task_runner.translate_texts 整个换成受控假函数，
其余引擎/配置检查按 tests/test_excel_untranslated_auto_lang.py 的既有替身方式打桩。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook, load_workbook

from core.api_config_check import ApiConfigCheckResult
from core.bilingual_writer import bilingual_output_name
from core.excel_coverage import build_excel_coverage_plan, write_untranslated_excel_file
from core.file_scanner import FileItem
from core.language_preflight import LanguagePreflightResult
from core.language_registry import get_target_lang_display
from core.language_preflight import TranslationLanguageResult
from core.model_throughput import EffectiveModelThroughput
from core.task_runner import DoneMsg, LogMsg, TaskRunner
from settings import AppSettings, EngineSettings

TARGET_LANG = "en"
SOURCE_LANG = "zh"


# ── 构造辅助 ────────────────────────────────────────────────────────────────

def _make_xlsx(path: Path, cells: dict[str, str], *, sheet_name: str = "Sheet") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for coordinate, text in cells.items():
        ws[coordinate] = text
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    wb.close()
    return path


def _make_baseline(
    source_path: Path,
    output_dir: Path,
    *,
    translations: dict[str, str],
    target_lang: str = TARGET_LANG,
    source_lang: str = SOURCE_LANG,
    keep_original_sheets: bool = True,
) -> Path:
    """用真实的覆盖率计划 + 补丁写入器，造一份「上次输出」的双语产物。

    直接复用生产代码本身的写入链路（build_excel_coverage_plan +
    write_untranslated_excel_file），而不是手写 xlsx 字节——这样产物的命名
    规则、单元格合并格式（"原文\\n译文"）天然和生产代码一致，不会因为测试
    自己的假设跟实现脱节。``translations`` 里没给的 source-only 格子会原样
    留在产物里，模拟「上次任务翻到一半」。
    """
    plan = build_excel_coverage_plan(
        source_path, target_lang=target_lang, source_lang=source_lang
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    return write_untranslated_excel_file(
        source_path=source_path,
        output_dir=output_dir,
        plan=plan,
        translations=translations,
        target_lang=target_lang,
        source_lang=source_lang,
        keep_original_sheets=keep_original_sheets,
    )


def _expected_bilingual_name(source_name: str, target_lang: str = TARGET_LANG) -> str:
    lang_display = get_target_lang_display(target_lang, include_optional=True)
    return bilingual_output_name(source_name, lang_display)


def _settings(*, source_lang: str = SOURCE_LANG, target_lang: str = TARGET_LANG) -> AppSettings:
    return AppSettings(
        engine=EngineSettings(
            mode="cloud",
            cloud_provider="custom_openai",
            cloud_model="fake-model",
            cloud_base_url="https://example.invalid/v1",
            concurrency=1,
            batch_size=20,
        ),
        target_lang=target_lang,
        source_lang=source_lang,
    )


def _pipeline_patches(
    *,
    translate_side_effect,
    preflight_result: dict[str, LanguagePreflightResult] | None = None,
    translate_with_sources_side_effect=None,
) -> ExitStack:
    """跑通 TaskRunner._run() 全链路所需的最小打桩集合。

    照抄 tests/test_excel_untranslated_auto_lang.py 的替身方式：真实引擎/网络
    一律不碰，只把 translate_texts（以及 auto 场景下的 preflight_files）换成
    受控假函数，其余全部走生产代码本身（含真实的覆盖率计划、真实的 xlsx 补丁
    写入）。
    """
    stack = ExitStack()
    stack.enter_context(
        patch("core.task_runner.TaskLogger", return_value=MagicMock(task_id="resume-test"))
    )
    stack.enter_context(
        patch(
            "core.task_runner.check_translation_api_config",
            return_value=ApiConfigCheckResult(ok=True),
        )
    )
    stack.enter_context(patch("core.task_runner.build_engine", return_value=MagicMock()))
    stack.enter_context(patch("core.task_runner.get_system_prompt", return_value="system"))
    stack.enter_context(
        patch("core.task_runner.resolve_effective_model_config", return_value=object())
    )
    stack.enter_context(
        patch(
            "core.task_runner.get_model_throughput",
            return_value=EffectiveModelThroughput(
                profile_key="test", batch_size=20, concurrency=1
            ),
        )
    )
    stack.enter_context(patch("core.task_runner.tm_manager.lookup_batch", return_value={}))
    stack.enter_context(patch("core.task_runner.tm_manager.insert_batch", return_value=0))
    stack.enter_context(
        patch("core.task_runner.translate_texts", side_effect=translate_side_effect)
    )
    if preflight_result is not None:
        stack.enter_context(
            patch("core.task_runner.preflight_files", return_value=preflight_result)
        )
    if translate_with_sources_side_effect is not None:
        stack.enter_context(
            patch(
                "core.task_runner.translate_texts_with_sources",
                side_effect=translate_with_sources_side_effect,
            )
        )
    return stack


def _fake_translate(recorded_calls: list[list[str]]):
    def _translate(texts, *args, **kwargs):
        recorded_calls.append(list(texts))
        return {t: f"T:{t}" for t in texts}

    return _translate


def _fake_translate_with_sources(recorded_calls: list[list[str]], *, source_lang: str = "zh"):
    """auto_source_lang 场景专用：这条路径不走 core.task_runner.translate_texts，
    而是 translate_texts_with_sources（直接调用 engine.chat 解析模型自报语种），
    所以要单独打桩，不能只换 translate_texts。
    """

    def _translate(texts, engine, target_lang, *args, **kwargs):
        recorded_calls.append(list(texts))
        return {
            t: TranslationLanguageResult(
                t, f"T:{t}", source_lang=source_lang, target_lang=target_lang
            )
            for t in texts
        }

    return _translate


def _run_and_get_done(runner: TaskRunner) -> DoneMsg:
    runner._run()
    messages = list(runner._queue.queue)
    done = [m for m in messages if isinstance(m, DoneMsg)]
    assert done, f"任务没有产出 DoneMsg，队列内容：{messages}"
    return done[0]


def _log_texts(runner: TaskRunner) -> list[str]:
    return [m.message for m in list(runner._queue.queue) if isinstance(m, LogMsg)]


def _fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """目录只读指纹：相对路径 -> (size, mtime_ns)。目录不存在时返回空字典。"""
    out: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            full = Path(dirpath) / filename
            rel = str(full.relative_to(root))
            st = full.stat()
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


class ExcelResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    # ── 场景 1：命中底稿，只补未译格子；顺带验证镜像相对路径 + 只读 ──────────

    def test_matched_baseline_fills_only_untranslated_cells_and_keeps_naming(self) -> None:
        proj = self.tmp / "proj"
        # 源文件在子目录下，验证 resume 目录里镜像同一层子目录也能匹配到。
        source = _make_xlsx(proj / "sub" / "doc.xlsx", {"A1": "你好", "A2": "再见"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        baseline = _make_baseline(
            source, resume_dir / "sub", translations={"你好": "Hello"}
        )
        wb = load_workbook(baseline)
        self.assertEqual(wb["Sheet"]["A1"].value, "你好\nHello")
        self.assertEqual(wb["Sheet"]["A2"].value, "再见")
        wb.close()
        self.assertEqual(baseline.name, _expected_bilingual_name("doc.xlsx"))

        fingerprint_before = _fingerprint(resume_dir)

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="doc", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        # 只有底稿里还没翻的 "再见" 被送去 API；已经翻过的 "你好" 绝不重翻。
        all_misses = [t for batch in calls for t in batch]
        self.assertIn("再见", all_misses)
        self.assertNotIn("你好", all_misses)

        self.assertEqual(len(done.file_results), 1)
        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])

        # 输出名必须按【原始源文件名】计算——不能在已经带过一次"双语"后缀的
        # 底稿文件名上再叠一层。
        self.assertEqual(out_path.name, _expected_bilingual_name("doc.xlsx"))
        self.assertNotIn("双语_双语", out_path.name)

        wb_out = load_workbook(out_path)
        try:
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nHello")  # 保留，未重翻
            self.assertEqual(wb_out["Sheet"]["A2"].value, "再见\nT:再见")  # 补齐
        finally:
            wb_out.close()

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("续译：doc 以上次产物为底稿", log_texts)

        # resume 目录全程只读。
        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 2：resume 目录里没有这个文件的产物 → 源文件全量翻译 ─────────────

    def test_unmatched_file_falls_back_to_full_source_translation(self) -> None:
        proj = self.tmp / "proj"
        # resume 目录里只有另一个文件（other.xlsx）的产物，没有 b.xlsx 的。
        other_source = _make_xlsx(proj / "other.xlsx", {"A1": "你好"})
        source = _make_xlsx(proj / "b.xlsx", {"A1": "你好"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        _make_baseline(other_source, resume_dir, translations={"你好": "Hello"})

        fingerprint_before = _fingerprint(resume_dir)

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="b", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        wb_out = load_workbook(out_path)
        try:
            # 源文件唯一的格子被正常整格翻译——没有底稿可换，走的是常规全量补译。
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
        finally:
            wb_out.close()

        all_misses = [t for batch in calls for t in batch]
        self.assertIn("你好", all_misses)

        log_texts = "\n".join(_log_texts(runner))
        self.assertNotIn("续译：", log_texts)  # 没有底稿可换，不应出现"换底稿"日志

        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 3：auto_source_lang → 语言预检之后再换底稿，续译照样兑现 ────────

    def test_auto_source_lang_defers_baseline_swap_until_language_settles(self) -> None:
        """「自动识别」是源语言的默认值，续译必须对它兑现，而不是静默退成全量重翻。

        约束是取样顺序：阶段 1 的词条要当语言预检的样本，双语底稿会把译文混进
        样本、把源语言判花，所以底稿只能等预检定出语言、补译清单重算之前换上
        （task_runner._apply_deferred_resume_baselines）。这里验证的就是这条链：
        预检样本来自源文件，补译清单来自底稿，已翻内容一格不重翻。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "c.xlsx", {"A1": "你好", "A2": "再见"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        # 底稿翻了一半："你好" 已有译文，"再见" 还没有。
        baseline = _make_baseline(source, resume_dir, translations={"你好": "Hello"})
        wb = load_workbook(baseline)
        self.assertEqual(wb["Sheet"]["A1"].value, "你好\nHello")
        self.assertEqual(wb["Sheet"]["A2"].value, "再见")
        wb.close()

        fingerprint_before = _fingerprint(resume_dir)

        calls: list[list[str]] = []
        with_sources_calls: list[list[str]] = []
        preflight_result = {
            str(source): LanguagePreflightResult(source_langs=("zh",), requested=True)
        }
        with _pipeline_patches(
            translate_side_effect=_fake_translate(calls),
            preflight_result=preflight_result,
            translate_with_sources_side_effect=_fake_translate_with_sources(with_sources_calls),
        ):
            runner = TaskRunner(
                [FileItem(path=source, name="c", size_kb=1.0)],
                _settings(source_lang="auto"),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        # 只有底稿里还没翻的 "再见" 被送去翻译；"你好" 已有译文，绝不重翻。
        sent = [t for batch in (calls + with_sources_calls) for t in batch]
        self.assertIn("再见", sent)
        self.assertNotIn("你好", sent)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        self.assertEqual(out_path.name, _expected_bilingual_name("c.xlsx"))
        wb_out = load_workbook(out_path)
        try:
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nHello")  # 底稿保留
            self.assertEqual(wb_out["Sheet"]["A2"].value, "再见\nT:再见")  # 本次补齐
        finally:
            wb_out.close()

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("等识别出源语言后再换为底稿", log_texts)
        self.assertIn("续译：c 以上次产物为底稿", log_texts)

        # resume 目录全程只读。
        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 4：底稿损坏 → 降级源文件全量翻译 + WARN 日志 ────────────────────

    def test_corrupted_baseline_downgrades_to_source_with_warning(self) -> None:
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "d.xlsx", {"A1": "你好"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        resume_dir.mkdir(parents=True)
        corrupted = resume_dir / _expected_bilingual_name("d.xlsx")
        corrupted.write_bytes(b"not a real xlsx file")

        fingerprint_before = _fingerprint(resume_dir)

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="d", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        wb_out = load_workbook(out_path)
        try:
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
        finally:
            wb_out.close()

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("续译底稿已损坏或无法打开，改用源文件全量翻译", log_texts)

        # 损坏的底稿文件本身不该被删除或改写。
        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 7：源文件比底稿多了新内容（非 auto）→ 拒用底稿，整份重翻 ────────

    def test_baseline_divergence_falls_back_to_full_source_translation(self) -> None:
        """底稿是「上一版」源文件翻的；这一版源文件多了一格底稿里没有的内容。

        换底稿只看底稿自身的补译清单——源文件比上次翻译时新增的内容根本不在
        底稿里，硬换会把它静默丢掉（既不送翻译也不出现在产物里）。核对
        （baseline_missing_source_texts）发现分歧后必须整份拒用底稿，回退成
        普通的源文件全量补译，新增格子和已有格子都要重新过一遍翻译。
        """
        old_version_dir = self.tmp / "old_version"
        old_source = _make_xlsx(old_version_dir / "j.xlsx", {"A1": "你好"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        _make_baseline(old_source, resume_dir, translations={"你好": "Hello"})

        proj = self.tmp / "proj"
        # 当前源文件比上次翻译时多了 A2 这一格新内容。
        source = _make_xlsx(proj / "j.xlsx", {"A1": "你好", "A2": "新增条款"})

        fingerprint_before = _fingerprint(resume_dir)

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="j", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("续译核对", log_texts)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        # 输出名按【这一版】源文件算，不是底稿名字。
        self.assertEqual(out_path.name, _expected_bilingual_name("j.xlsx"))

        wb_out = load_workbook(out_path)
        try:
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
            self.assertEqual(wb_out["Sheet"]["A2"].value, "新增条款\nT:新增条款")
        finally:
            wb_out.close()

        # 拒用底稿就是整份重翻：新增格子和已有格子都要送去 API。
        all_misses = [t for batch in calls for t in batch]
        self.assertIn("你好", all_misses)
        self.assertIn("新增条款", all_misses)

        # 底稿文件本身只读——核对失败也不该碰它。
        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 8：源文件比底稿多了新内容（auto）→ 延迟核对同样拒用底稿 ─────────

    def test_baseline_divergence_falls_back_to_full_source_translation_auto_lang(self) -> None:
        """场景 7 的 auto_source_lang 版本：核对推迟到 _apply_deferred_resume_baselines，

        换底稿的动作本身推迟到语言预检之后，但分歧核对的结论必须一致——不能因为
        换成了"延迟路径"就漏查，把新增内容悄悄漏掉。
        """
        old_version_dir = self.tmp / "old_version"
        old_source = _make_xlsx(old_version_dir / "k.xlsx", {"A1": "你好"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        _make_baseline(old_source, resume_dir, translations={"你好": "Hello"})

        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "k.xlsx", {"A1": "你好", "A2": "新增条款"})

        fingerprint_before = _fingerprint(resume_dir)

        calls: list[list[str]] = []
        with_sources_calls: list[list[str]] = []
        preflight_result = {
            str(source): LanguagePreflightResult(source_langs=("zh",), requested=True)
        }
        with _pipeline_patches(
            translate_side_effect=_fake_translate(calls),
            preflight_result=preflight_result,
            translate_with_sources_side_effect=_fake_translate_with_sources(with_sources_calls),
        ):
            runner = TaskRunner(
                [FileItem(path=source, name="k", size_kb=1.0)],
                _settings(source_lang="auto"),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("续译核对", log_texts)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        self.assertEqual(out_path.name, _expected_bilingual_name("k.xlsx"))

        wb_out = load_workbook(out_path)
        try:
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
            self.assertEqual(wb_out["Sheet"]["A2"].value, "新增条款\nT:新增条款")
        finally:
            wb_out.close()

        sent = [t for batch in (calls + with_sources_calls) for t in batch]
        self.assertIn("你好", sent)
        self.assertIn("新增条款", sent)

        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 9：sheet 集合跨续译轮次稳定，不叠加克隆 ─────────────────────────

    def test_sheet_set_stable_across_resume_rounds(self) -> None:
        """finding #12：keep_original_sheets 的「_原文」克隆分表不能每续译一轮翻倍。

        第一轮续译换上的底稿本身已经带着上一次生成的「Sheet_原文」分表；这一轮
        写盘时不能对它再克隆一层变成「Sheet_原文_原文」，也不能把还没克隆过的
        「Sheet」再错误地跳过。往后每多续一轮，sheet 集合都要保持和第一次产物
        完全一样。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "g.xlsx", {"A1": "你好", "A2": "再见"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        baseline = _make_baseline(
            source, resume_dir, translations={"你好": "Hello", "再见": "Bye"}
        )
        wb = load_workbook(baseline)
        baseline_sheetnames = list(wb.sheetnames)
        wb.close()
        # 前提核实：keep_original_sheets 默认开启，首次产物本该是「原文表 + 克隆的
        # 原文备份表」这两张。
        self.assertEqual(baseline_sheetnames, ["Sheet", "Sheet_原文"])

        calls1: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls1)):
            runner1 = TaskRunner(
                [FileItem(path=source, name="g", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done1 = _run_and_get_done(runner1)
        result1 = done1.file_results[0]
        self.assertTrue(result1.get("success"), result1)
        round1_out = Path(result1["output_path"])

        wb1 = load_workbook(round1_out)
        round1_sheetnames = list(wb1.sheetnames)
        wb1.close()
        self.assertEqual(round1_sheetnames, baseline_sheetnames)

        # 第二轮：以第一轮续译自己的产物目录为新的 resume_output_dir，再续译一次。
        calls2: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls2)):
            runner2 = TaskRunner(
                [FileItem(path=source, name="g", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(round1_out.parent),
            )
            done2 = _run_and_get_done(runner2)
        result2 = done2.file_results[0]
        self.assertTrue(result2.get("success"), result2)
        round2_out = Path(result2["output_path"])

        wb2 = load_workbook(round2_out)
        round2_sheetnames = list(wb2.sheetnames)
        wb2.close()
        # 两轮之后 sheet 集合仍然只有这两张——没有 "Sheet1_原文_2"，
        # 也没有 "Sheet1_原文_原文"。
        self.assertEqual(round2_sheetnames, baseline_sheetnames)

    # ── 场景 10：底稿探活通过但建计划失败（非 auto）→ 降级源文件 + WARN ──────

    def test_baseline_coverage_plan_failure_downgrades_to_source_with_warning(self) -> None:
        """finding #3：底稿文件本身能被 openpyxl 打开（探活通过），但建立补译
        覆盖率计划时才炸——这和「文件彻底打不开」不是一回事，必须单独降级、
        单独留痕，不能被 test_corrupted_baseline 那条路径误吞。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "h.xlsx", {"A1": "你好"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        baseline = _make_baseline(source, resume_dir, translations={"你好": "Hello"})

        fingerprint_before = _fingerprint(resume_dir)

        real_build_plan = build_excel_coverage_plan

        def _fail_only_for_baseline(path, *args, **kwargs):
            if Path(path) == baseline:
                raise ValueError("模拟：底稿建补译计划失败")
            return real_build_plan(path, *args, **kwargs)

        calls: list[list[str]] = []
        with (
            _pipeline_patches(translate_side_effect=_fake_translate(calls)),
            patch(
                "core.task_runner.build_excel_coverage_plan",
                side_effect=_fail_only_for_baseline,
            ),
        ):
            runner = TaskRunner(
                [FileItem(path=source, name="h", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        wb_out = load_workbook(out_path)
        try:
            # 降级为源文件全量翻译：底稿里已经有的 "Hello" 不作数，重新翻一遍。
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
        finally:
            wb_out.close()

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("续译底稿无法建立补译计划", log_texts)

        # 底稿本身只读——建计划失败也不该碰它。
        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 11：底稿探活通过但建计划失败（auto）→ 延迟路径同样降级 + WARN ───

    def test_baseline_coverage_plan_failure_downgrades_to_source_with_warning_auto_lang(
        self,
    ) -> None:
        """场景 10 的 auto_source_lang 版本：失败点在 _rebuild_coverage_plans_after_preflight

        （语言预检之后才重建补译清单），而不是阶段 1；patch 目标是同一个
        core.task_runner.build_excel_coverage_plan 模块级引用，只对换上的底稿路径
        生效，验证两条路径都会走到同一套降级 + WARN 兜底。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "i.xlsx", {"A1": "你好", "A2": "再见"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        baseline = _make_baseline(
            source, resume_dir, translations={"你好": "Hello", "再见": "Bye"}
        )

        fingerprint_before = _fingerprint(resume_dir)

        real_build_plan = build_excel_coverage_plan

        def _fail_only_for_baseline(path, *args, **kwargs):
            if Path(path) == baseline:
                raise ValueError("模拟：底稿建补译计划失败")
            return real_build_plan(path, *args, **kwargs)

        calls: list[list[str]] = []
        with_sources_calls: list[list[str]] = []
        preflight_result = {
            str(source): LanguagePreflightResult(source_langs=("zh",), requested=True)
        }
        with (
            _pipeline_patches(
                translate_side_effect=_fake_translate(calls),
                preflight_result=preflight_result,
                translate_with_sources_side_effect=_fake_translate_with_sources(
                    with_sources_calls
                ),
            ),
            patch(
                "core.task_runner.build_excel_coverage_plan",
                side_effect=_fail_only_for_baseline,
            ),
        ):
            runner = TaskRunner(
                [FileItem(path=source, name="i", size_kb=1.0)],
                _settings(source_lang="auto"),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        wb_out = load_workbook(out_path)
        try:
            # 降级为源文件全量翻译：底稿里已经翻好的 "Hello"/"Bye" 不作数。
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
            self.assertEqual(wb_out["Sheet"]["A2"].value, "再见\nT:再见")
        finally:
            wb_out.close()

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("续译底稿无法建立补译计划", log_texts)

        self.assertEqual(_fingerprint(resume_dir), fingerprint_before)

    # ── 场景 12：底稿全覆盖时日志是 INFO 报喜，不是 WARN 报警 ────────────────

    def test_resume_full_coverage_logs_info_not_warn(self) -> None:
        """finding #1：底稿已经把源文件翻完时（没有可补的内容），这是"续译成功、
        无需再补"的好消息，日志级别和措辞都得是 INFO「输出与上次产物一致」——
        绝不能沿用普通补译"没找到需要补译的内容"那句面向"本来就没翻译过"场景的
        WARN 措辞，两种情况对用户的含义完全相反。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "m.xlsx", {"A1": "你好"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        _make_baseline(source, resume_dir, translations={"你好": "Hello"})

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="m", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)

        log_texts = "\n".join(_log_texts(runner))
        self.assertNotIn("输出文件会和原文一致", log_texts)
        self.assertIn("输出与上次产物一致", log_texts)

    # ── 场景 6：不带 resume_output_dir → 行为与旧版完全一致 ──────────────────

    def test_without_resume_output_dir_behaves_like_before(self) -> None:
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "e.xlsx", {"A1": "你好"})

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="e", size_kb=1.0)],
                _settings(),
                source_root=proj,
                untranslated_only=True,
                # resume_output_dir 未传，默认 None。
            )
            done = _run_and_get_done(runner)

        result = done.file_results[0]
        self.assertTrue(result.get("success"), result)
        out_path = Path(result["output_path"])
        wb_out = load_workbook(out_path)
        try:
            self.assertEqual(wb_out["Sheet"]["A1"].value, "你好\nT:你好")
        finally:
            wb_out.close()

        # 没有 resume_output_dir，任何"续译换底稿"相关分支都不该被触发。
        log_texts = "\n".join(_log_texts(runner))
        self.assertNotIn("续译", log_texts)

    # ── 场景 7：续译 + 「保留原文副本」开启 → 不从双语底稿硬克隆 _原文 分表 ──

    def test_resume_never_clones_original_sheets_from_bilingual_baseline(self) -> None:
        """上次任务关着「保留原文副本」，这次续译前用户把它打开了。

        续译的写入源是上次的双语底稿——从「原文\\n译文」的内容里克隆不出
        「未翻译的原始副本」，硬克隆出来的「Sheet_原文」名字在撒谎、体积还翻倍。
        换底稿的文件必须压掉克隆并说明原因；同一批里没匹配到底稿、按源文件全量
        翻译的文件不受影响，照常生成货真价实的原文副本。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "doc.xlsx", {"A1": "你好", "A2": "再见"})
        extra = _make_xlsx(proj / "extra.xlsx", {"A1": "早安"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        # 上次任务没开「保留原文副本」——底稿里只有双语正表，没有 _原文 分表。
        baseline = _make_baseline(
            source,
            resume_dir,
            translations={"你好": "Hello"},
            keep_original_sheets=False,
        )
        wb = load_workbook(baseline)
        try:
            self.assertEqual(wb.sheetnames, ["Sheet"])
        finally:
            wb.close()

        settings = _settings()
        settings.excel_output.keep_original_sheets = True

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [
                    FileItem(path=source, name="doc", size_kb=1.0),
                    FileItem(path=extra, name="extra", size_kb=1.0),
                ],
                settings,
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        results = {Path(r["output_path"]).name: r for r in done.file_results if r.get("success")}
        self.assertEqual(len(results), 2, done.file_results)

        doc_out = Path(results[_expected_bilingual_name("doc.xlsx")]["output_path"])
        wb_doc = load_workbook(doc_out)
        try:
            # 换了底稿的文件：绝不新增 _原文 分表——那只能克隆自双语内容。
            self.assertEqual(wb_doc.sheetnames, ["Sheet"])
        finally:
            wb_doc.close()

        extra_out = Path(results[_expected_bilingual_name("extra.xlsx")]["output_path"])
        wb_extra = load_workbook(extra_out)
        try:
            # 没底稿、按源文件翻的文件不受影响，原文副本照常生成。
            self.assertEqual(wb_extra.sheetnames, ["Sheet", "Sheet_原文"])
            self.assertEqual(wb_extra["Sheet_原文"]["A1"].value, "早安")
        finally:
            wb_extra.close()

        log_texts = "\n".join(_log_texts(runner))
        self.assertIn("不新增「_原文」分表", log_texts)

    def test_resume_preserves_existing_original_sheets_without_recloning(self) -> None:
        """上次和这次都开着「保留原文副本」：底稿里已有的 _原文 分表原样保留。

        底稿自带上一轮克隆的货真价实的「Sheet_原文」。续译时它作为普通分表随
        产物复制走；写入层被压掉克隆后不能再叠一份（既不覆盖也不冒出
        「Sheet_原文1」之类的重名变体）。
        """
        proj = self.tmp / "proj"
        source = _make_xlsx(proj / "doc.xlsx", {"A1": "你好", "A2": "再见"})

        resume_dir = self.tmp / "proj_翻译输出_20260101_000000"
        baseline = _make_baseline(
            source,
            resume_dir,
            translations={"你好": "Hello"},
            keep_original_sheets=True,
        )
        wb = load_workbook(baseline)
        try:
            self.assertEqual(wb.sheetnames, ["Sheet", "Sheet_原文"])
            self.assertEqual(wb["Sheet_原文"]["A1"].value, "你好")
        finally:
            wb.close()

        settings = _settings()
        settings.excel_output.keep_original_sheets = True

        calls: list[list[str]] = []
        with _pipeline_patches(translate_side_effect=_fake_translate(calls)):
            runner = TaskRunner(
                [FileItem(path=source, name="doc", size_kb=1.0)],
                settings,
                source_root=proj,
                untranslated_only=True,
                resume_output_dir=str(resume_dir),
            )
            done = _run_and_get_done(runner)

        results = {Path(r["output_path"]).name: r for r in done.file_results if r.get("success")}
        doc_out = Path(results[_expected_bilingual_name("doc.xlsx")]["output_path"])
        wb_doc = load_workbook(doc_out)
        try:
            self.assertEqual(wb_doc.sheetnames, ["Sheet", "Sheet_原文"])
            # 上一轮克隆的原文副本原样保留，仍是纯原文而不是双语内容。
            self.assertEqual(wb_doc["Sheet_原文"]["A1"].value, "你好")
            self.assertEqual(wb_doc["Sheet_原文"]["A2"].value, "再见")
        finally:
            wb_doc.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
