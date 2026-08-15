"""页眉页脚专用通道接进 Word 主流程的那一段接线。

通道本身的规则在 test_header_footer_channel.py，这里只管接线：哪些行进通道、
记忆库怎么查、结果怎么并回 global_translations、补不上的怎么进报告。
"""

from __future__ import annotations

import json
import threading
import unittest

from core import word_task_runner
from core.header_footer_channel import build_header_footer_payload
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX
from core.word_document import WordSegment

REAL_HEADER = (
    "贝特瑞地中海负极项目总包一标段PROJETBTRANODEMÉDITERRANÉE(Lot1)"
    "                  地面裂缝修复施工方案"
)
REAL_ADDED = "Plan de réparation des fissures du sol"


def _segment(source: str, index: int = 0) -> WordSegment:
    return WordSegment(
        source=source,
        kind="header",
        location=f"header[header1].paragraph[{index}]",
        section_path="第 1 节 页眉",
    )


class _Engine:
    """带真 chat() 的假引擎——engine_supports_chat 看的是类上的方法。"""

    engine_name = "fake"

    def __init__(self, reply):
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    def chat(self, system_prompt: str, user_payload: str) -> str:
        self.calls.append((system_prompt, user_payload))
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


class _NoChatEngine:
    engine_name = "local"


class _TmManager:
    def __init__(self, entries: dict[str, str] | None = None):
        self.entries = entries or {}
        self.queries: list[list[str]] = []

    def lookup_batch(self, texts, lang_pair):  # noqa: ANN001 - 只是个假件
        self.queries.append(list(texts))
        return {text: self.entries.get(text) for text in texts}


def _reply(lines: list[str], added: str = REAL_ADDED) -> str:
    return json.dumps(
        [
            {"id": index, "line": f"{line} {added}", "added": added}
            for index, line in enumerate(lines)
        ],
        ensure_ascii=False,
    )


class ChannelGlueTests(unittest.TestCase):
    def _runner(self, logs: list[tuple[str, str]]):
        runner = word_task_runner.WordTaskRunner.__new__(word_task_runner.WordTaskRunner)
        runner._log = lambda level, message: logs.append((level, message))
        runner._stop_event = threading.Event()
        return runner

    def _resolve(self, runner, entries, **kwargs):
        params = {
            "engine": _NoChatEngine(),
            "api_scheduler": None,
            "tm_manager": _TmManager(),
            "tm_language_pairs": ["zh-fr"],
            "target_lang": "fr",
            "source_lang": "zh",
            "global_translations": {},
            "body_texts": set(),
            "quality_issues": [],
        }
        params.update(kwargs)
        runner._resolve_header_footer_lines(entries, **params)
        return params

    def test_a_model_answer_lands_as_a_replace_marked_whole_line(self) -> None:
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        issues: list[dict] = []
        engine = _Engine(_reply([REAL_HEADER]))

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            global_translations=translations,
            quality_issues=issues,
        )

        value = translations[REAL_HEADER]
        self.assertTrue(value.startswith(REPLACE_TRANSLATION_PREFIX))
        # 原行逐字保留，新译文接在后面——这是整套逻辑唯一的验收标准。
        self.assertTrue(value.endswith(f"{REAL_HEADER} {REAL_ADDED}"))
        self.assertEqual(issues, [])
        self.assertEqual(len(engine.calls), 1)
        self.assertIn(REAL_HEADER, engine.calls[0][1])

    def test_a_line_that_also_exists_in_the_body_is_left_to_the_normal_path(
        self,
    ) -> None:
        """通道的成品是整行替换，拿去替换正文段落会把那一段的排版一起改掉。"""
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        engine = _Engine(_reply([REAL_HEADER]))

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            global_translations=translations,
            body_texts={REAL_HEADER},
        )

        self.assertEqual(engine.calls, [])
        self.assertEqual(translations, {})
        self.assertTrue(
            any("与正文内容相同" in message for _level, message in logs), logs
        )

    def test_the_body_translation_of_a_residual_fragment_skips_the_model(self) -> None:
        """整行只剩一处中文残片、而正文刚好翻过它——这一趟模型钱不用花。"""
        logs: list[tuple[str, str]] = []
        line = "PROJET BTR ANODE MÉDITERRANÉE 地面裂缝修复施工方案"
        translations = {"地面裂缝修复施工方案": REAL_ADDED}
        engine = _Engine(_reply([line]))

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(line))],
            engine=engine,
            global_translations=translations,
        )

        self.assertEqual(engine.calls, [])
        self.assertEqual(translations[line], REAL_ADDED)

    def test_a_replace_marked_body_value_is_never_reused_as_a_fragment(self) -> None:
        """整体替换标记是「那一行的成品」，不是这段原文的译文，拼过去就是一行乱码。"""
        logs: list[tuple[str, str]] = []
        line = "PROJET BTR ANODE MÉDITERRANÉE 地面裂缝修复施工方案"
        translations = {
            "地面裂缝修复施工方案": f"{REPLACE_TRANSLATION_PREFIX}某一整行成品"
        }
        engine = _Engine(_reply([line]))

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(line))],
            engine=engine,
            global_translations=translations,
        )

        self.assertEqual(len(engine.calls), 1)
        self.assertTrue(translations[line].startswith(REPLACE_TRANSLATION_PREFIX))
        self.assertIn(REAL_ADDED, translations[line])

    def test_the_memory_bank_is_consulted_before_the_model(self) -> None:
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        tm = _TmManager({REAL_HEADER: REAL_ADDED})
        engine = _Engine(_reply([REAL_HEADER]))

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            tm_manager=tm,
            global_translations=translations,
        )

        self.assertEqual(engine.calls, [])
        # 整行命中走追加，不带整体替换标记——追加不动原行一个字。
        self.assertEqual(translations, {REAL_HEADER: REAL_ADDED})
        self.assertTrue(tm.queries)

    def test_a_rewritten_line_falls_back_to_appending_and_says_so(self) -> None:
        """模型把已有的法定译名改写了：整行成品拒收，只把新译文接在行尾。"""
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        issues: list[dict] = []
        rewritten = (
            "贝特瑞地中海负极项目总包一标段 PROJET BTR ANODE MÉDITERRANÉE (Lot 1) "
            f"地面裂缝修复施工方案 {REAL_ADDED}"
        )
        engine = _Engine(
            json.dumps(
                [{"id": 0, "line": rewritten, "added": REAL_ADDED}], ensure_ascii=False
            )
        )

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            global_translations=translations,
            quality_issues=issues,
        )

        self.assertEqual(translations, {REAL_HEADER: REAL_ADDED})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["problem"], "页眉页脚整行改写未通过校验")
        self.assertEqual(issues[0]["severity"], "resolved")

    def test_a_line_that_cannot_be_completed_is_reported_for_a_human(self) -> None:
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        issues: list[dict] = []
        engine = _Engine(json.dumps([{"id": 0, "line": "完全另一行", "added": ""}]))

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            global_translations=translations,
            quality_issues=issues,
        )

        self.assertEqual(translations, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["problem"], "页眉页脚未能补上译文")
        self.assertEqual(issues[0]["severity"], "needs_review")
        self.assertEqual(issues[0]["location"], "header[header1].paragraph[0]")

    def test_a_line_the_model_says_is_already_complete_is_not_a_problem(self) -> None:
        """原样退回 = 这一行本来就全译好了，报告里不该多一条待办。"""
        logs: list[tuple[str, str]] = []
        issues: list[dict] = []
        engine = _Engine(
            json.dumps([{"id": 0, "line": REAL_HEADER, "added": ""}], ensure_ascii=False)
        )

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            quality_issues=issues,
        )

        self.assertEqual(issues, [])

    def test_an_engine_without_chat_leaves_every_line_untouched(self) -> None:
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        issues: list[dict] = []

        self._resolve(
            self._runner(logs),
            [("方案.docx", _segment(REAL_HEADER))],
            global_translations=translations,
            quality_issues=issues,
        )

        self.assertEqual(translations, {})
        self.assertEqual(issues[0]["problem"], "页眉页脚未能补上译文")

    def test_stopping_the_task_writes_nothing_instead_of_half_a_line(self) -> None:
        logs: list[tuple[str, str]] = []
        translations: dict[str, str] = {}
        engine = _Engine(_reply([REAL_HEADER]))
        runner = self._runner(logs)
        runner._stop_event.set()

        self._resolve(
            runner,
            [("方案.docx", _segment(REAL_HEADER))],
            engine=engine,
            global_translations=translations,
        )

        self.assertEqual(engine.calls, [])
        self.assertEqual(translations, {})

    def test_the_same_line_in_two_files_is_asked_once_and_reported_twice(self) -> None:
        logs: list[tuple[str, str]] = []
        issues: list[dict] = []
        engine = _Engine(json.dumps([{"id": 0, "line": "完全另一行", "added": ""}]))

        self._resolve(
            self._runner(logs),
            [
                ("甲.docx", _segment(REAL_HEADER)),
                ("乙.docx", _segment(REAL_HEADER, index=1)),
            ],
            engine=engine,
            quality_issues=issues,
        )

        self.assertEqual(len(engine.calls), 1)
        self.assertEqual([issue["file"] for issue in issues], ["甲.docx", "乙.docx"])


class BatchRequestTests(unittest.TestCase):
    def test_a_good_reply_is_parsed_into_line_and_added(self) -> None:
        engine = _Engine(_reply([REAL_HEADER]))

        answers = word_task_runner._run_header_footer_batch(
            engine,
            [REAL_HEADER],
            source_lang="zh",
            target_lang="fr",
            api_scheduler=None,
        )

        self.assertEqual(answers, [(f"{REAL_HEADER} {REAL_ADDED}", REAL_ADDED)])
        self.assertEqual(engine.calls[0][1], build_header_footer_payload([REAL_HEADER]))

    def test_a_failed_request_returns_nothing_and_warns(self) -> None:
        """页眉补不上不该掀掉整批任务——正文译文这时候已经全部付费拿到手了。"""
        engine = _Engine(RuntimeError("网络中断"))
        warnings: list[str] = []

        answers = word_task_runner._run_header_footer_batch(
            engine,
            [REAL_HEADER],
            source_lang="zh",
            target_lang="fr",
            api_scheduler=None,
            error_callback=warnings.append,
        )

        self.assertEqual(answers, [])
        self.assertTrue(any("网络中断" in message for message in warnings), warnings)

    def test_an_unusable_key_does_not_take_the_task_down_either(self) -> None:
        engine = _Engine(
            word_task_runner.ApiKeyTemporarilyUnavailableError("key 暂不可用")
        )
        warnings: list[str] = []

        answers = word_task_runner._run_header_footer_batch(
            engine,
            [REAL_HEADER],
            source_lang="zh",
            target_lang="fr",
            api_scheduler=None,
            error_callback=warnings.append,
        )

        self.assertEqual(answers, [])
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
