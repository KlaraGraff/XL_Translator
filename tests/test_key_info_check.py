"""译文定稿后的关键信息核对（core/key_info_check.py）和它在各条翻译流程里的接法。

句子取自 scripts/keyfield_shadow/cases.py：前一半是专门挑出来的「容易误判」的正常译文，
一条都不能报；后一半是带错的译文，每条都要报出来。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import REVIEW_MARK_KEY_INFO
from core.key_info_check import (
    KIND_DATE,
    KIND_ID,
    KIND_NAME_SPELLING,
    KIND_PINYIN_NAME,
    REVIEW_PROBLEM,
    KeyInfoIssue,
    check_key_info,
    name_hint_block,
    pinyin_name_parts,
    pinyin_names,
    review_detail,
    review_summary,
    titled_names,
)
from core.term_engine import TermAwareEngine
from core.term_session import TaskTermSession
from core.tm_terms import TermEntry, TermIndex
from core.word_task_runner import WordTaskRunner
from engines.base_engine import TranslationEngine
from tests.app_data_isolation import IsolatedAppDataTestCase

SCREENSHOT = (
    "Et Monsieur YU YUNCHONG, de nationalité chinoise, né le 07/09/1992 en Chine, "
    "titulaire du passeport chinois n° ED2914389,"
)

# (说明, 源语言, 目标语言, 原文, 译文)
CORRECT = [
    ("截图原句·拼音名保留", "fr", "zh", SCREENSHOT,
     "和 YU YUNCHONG 先生，中国国籍，1992年9月7日出生于中国，持有中国护照编号 ED2914389，"),
    ("外国人名音译加括注", "fr", "zh",
     "Madame SAIDA CHERGUI, titulaire de la CIN n° K19498, propriétaire de l'appartement, titre foncier n° 06/191635",
     "赛达·谢尔吉（SAIDA CHERGUI）女士，持有国家身份证（CIN）编号 K19498，系该公寓产权人，地籍登记号 06/191635"),
    ("外国人名只写音译", "fr", "zh", "Madame SAIDA CHERGUI déclare avoir reçu la somme de 4 500,00 dirhams.",
     "赛达·谢尔吉女士确认已收到 4,500.00 迪拉姆。"),
    ("名字像拼音的摩洛哥人", "fr", "zh", "Madame AMINA TAZI, gérante de la société", "阿米娜·塔齐（AMINA TAZI）女士，公司经理"),
    ("名字像拼音、不带间隔点", "fr", "zh", "Madame SANA, locataire", "萨娜（SANA）女士，承租人"),
    ("粤语拼法人名", "en", "zh", "Mr. CHAN TAI MAN, holder of HKID A1234567", "陈大文（CHAN TAI MAN）先生，香港身份证号码 A1234567"),
    ("称谓后不是名字", "fr", "zh", "Monsieur le Président du tribunal a rendu sa décision le 3 mars 2026.",
     "法院院长先生于2026年3月3日作出裁决。"),
    ("全大写标题", "fr", "zh", "CONTRAT DE BAIL A USAGE D'HABITATION", "住宅用途租赁合同"),
    ("日期换写法", "fr", "zh", "signé et légalisé le 29 septembre 2026", "已于2026年9月29日签署并合法化"),
    ("日期汉字数字", "fr", "zh", "fait à Tanger le 29 septembre 2026", "二〇二六年九月二十九日订于丹吉尔"),
    ("日期区间", "fr", "zh", "pour la période du 1er au 30 septembre 2026", "租期为2026年9月1日至30日"),
    ("英文日期有歧义", "en", "zh", "Signed on 07/09/2026 in London", "于2026年7月9日在伦敦签署"),
    ("英文日期另一种读法", "en", "zh", "Signed on 07/09/2026 in London", "于2026年9月7日在伦敦签署"),
    ("两个日期", "fr", "zh", "du 1er octobre 2026 au 30 septembre 2027", "自2026年10月1日起至2027年9月30日止"),
    ("楼层与户型", "fr", "zh", "appartement de type T3 situé au R+2, superficie 85 m2", "位于三层的三居室公寓，面积 85 平方米"),
    ("缩写编号", "fr", "zh", "conformément à l'article No.1234 du Code", "根据该法典第1234条"),
    ("编号整串原样", "fr", "zh", "référence du dossier : DOS2026001", "档案编号：DOS2026001"),
    ("编号中间加空格", "fr", "zh", "passeport n° ED2914389", "护照编号 ED 2914389"),
    ("名字重音", "fr", "zh", "Madame AÏCHA BENNANI", "艾莎·本纳尼（AICHA BENNANI）女士"),
    ("名字在后文只用音译", "fr", "zh", "Le Bailleur, Madame SAIDA CHERGUI, s'engage à ...", "出租人赛达·谢尔吉女士承诺……"),
    ("规范号", "zh", "fr", "执行 GB50300-2013 标准，混凝土 C30，钢筋 HRB400",
     "Conformément à la norme GB50300-2013, béton C30, armatures HRB400"),
    ("管径与长度", "zh", "fr", "DN1000 管道长 1500MM", "conduite DN1000 de 1500 mm de long"),
    ("功率", "zh", "fr", "变压器容量 2000KVA", "transformateur de 2000 kVA"),
    ("中译法日期", "zh", "fr", "2026年9月29日完成验收", "Réception achevée le 29 septembre 2026"),
    ("中译法日期区间", "zh", "fr", "工期：2026年9月1日至2026年12月30日", "Délai : du 1er septembre 2026 au 30 décembre 2026"),
    ("中译法数字日期", "zh", "fr", "日期：2026.09.29", "Date : 29/09/2026"),
    ("图号", "zh", "fr", "图号 JS-2026-001，版本 A1", "N° de plan JS-2026-001, version A1"),
    ("中文人名译拼音", "zh", "fr", "项目经理：于云聪先生", "Chef de projet : M. YU Yuncong"),
    ("段内格式标记", "fr", "zh", "<g1>Monsieur YU YUNCHONG</g1>, passeport n° <g2>ED2914389</g2>",
     "<g1>YU YUNCHONG 先生</g1>，护照编号 <g2>ED2914389</g2>"),
]

# (说明, 源语言, 目标语言, 原文, 译文, 应报的种类)
WRONG = [
    ("截图原句·拼音名配错汉字", "fr", "zh", SCREENSHOT,
     "和于云聪先生（YU YUNCHONG），中国国籍，1992年9月7日出生于中国，持有中国护照编号 ED2914389，", KIND_PINYIN_NAME),
    ("拼音名配了读音对的汉字", "fr", "zh", "Monsieur YU YUNCHONG reconnaît avoir reçu les clés.",
     "于云冲（YU YUNCHONG）先生确认已收到钥匙。", KIND_PINYIN_NAME),
    ("拼音名去掉拼音只写汉字", "fr", "zh", "Monsieur YU YUNCHONG reconnaît avoir reçu les clés.", "于云冲先生确认已收到钥匙。",
     KIND_PINYIN_NAME),
    ("名在前姓在后", "fr", "zh", "Monsieur YUNCHONG YU, locataire", "于云冲（YUNCHONG YU）先生，承租人", KIND_PINYIN_NAME),
    ("护照号字母被改", "fr", "zh", "passeport n° ED2914389", "护照编号 EB2914389", KIND_ID),
    ("身份证号字母被改", "fr", "zh", "CIN n° K19498", "身份证编号 R19498", KIND_ID),
    ("日月颠倒", "fr", "zh", "né le 07/09/1992", "1992年7月9日出生", KIND_DATE),
    ("月份错", "fr", "zh", "signé le 29 septembre 2026", "于2026年2月29日签署", KIND_DATE),
    ("人名拼写被改", "fr", "zh", "représentée par Madame MERYEM EL FARISSI",
     "由梅里耶姆·埃尔法里斯（MERYEM EL FARESSI）女士代表", KIND_NAME_SPELLING),
    ("中译法日期错", "zh", "fr", "2026年9月29日完成验收", "Réception achevée le 29 août 2026", KIND_DATE),
]


class RuleTests(unittest.TestCase):
    def test_correct_translations_are_never_flagged(self):
        for label, source_lang, target_lang, source, translation in CORRECT:
            with self.subTest(label):
                self.assertEqual(
                    check_key_info(source, translation, source_lang=source_lang, target_lang=target_lang), []
                )

    def test_each_wrong_translation_is_flagged_with_its_kind(self):
        for label, source_lang, target_lang, source, translation, kind in WRONG:
            with self.subTest(label):
                issues = check_key_info(source, translation, source_lang=source_lang, target_lang=target_lang)
                self.assertEqual([issue.kind for issue in issues], [kind])

    def test_messages_name_what_differs(self):
        [issue] = check_key_info("passeport n° ED2914389", "护照编号 EB2914389", target_lang="zh")
        self.assertEqual(issue.message, "编号 ED2914389 在译文中写成了 EB2914389")
        [issue] = check_key_info("né le 07/09/1992", "1992年7月9日出生", source_lang="fr", target_lang="zh")
        self.assertEqual(issue.message, "日期与原文不符：原文为 1992-09-07，译文为 1992-07-09")
        [issue] = check_key_info(SCREENSHOT, WRONG[0][4], source_lang="fr", target_lang="zh")
        self.assertEqual(issue.message, "原文人名 YU YUNCHONG 只有拼音，译文中的「于云聪」与拼音读音不符")

    def test_names_fixed_in_the_memory_are_not_guesses(self):
        source, translation = "Monsieur YU YUNCHONG reconnaît avoir reçu les clés.", "于云冲（YU YUNCHONG）先生确认已收到钥匙。"
        self.assertEqual(check_key_info(source, translation, target_lang="zh", fixed_names=["YU YUNCHONG"]), [])

    def test_missing_digits_belong_to_the_number_check(self):
        # 整串数字都不见了是现有数字核对的事，这里不重复报。
        self.assertEqual(check_key_info("passeport n° ED2914389", "护照编号见附件", target_lang="zh"), [])

    def test_unchanged_or_empty_translations_are_skipped(self):
        self.assertEqual(check_key_info("passeport n° ED2914389", "passeport n° ED2914389", target_lang="zh"), [])
        self.assertEqual(check_key_info("passeport n° ED2914389", "", target_lang="zh"), [])


class NameTests(unittest.TestCase):
    def test_titles_and_particles(self):
        self.assertEqual(
            titled_names("Et Monsieur YU YUNCHONG, M. WANG XIAOMING et Mme AMINA TAZI, Monsieur le Maire"),
            ["YU YUNCHONG", "WANG XIAOMING", "AMINA TAZI"],
        )
        self.assertEqual(titled_names("représentée par Madame MERYEM EL FARISSI"), ["MERYEM EL FARISSI"])

    def test_only_chinese_pinyin_names_count(self):
        self.assertEqual(pinyin_name_parts("YU YUNCHONG"), [["yu"], ["yun", "chong"]])
        self.assertIsNotNone(pinyin_name_parts("OUYANG XIU"))
        self.assertIsNotNone(pinyin_name_parts("LYU BIN"))
        for name in ("AMINA TAZI", "SANA", "CHAN TAI MAN", "KIM MINJUN", "TANAKA HIROSHI", "SAIDA CHERGUI"):
            with self.subTest(name):
                self.assertIsNone(pinyin_name_parts(name))

    def test_pinyin_names_leave_out_names_fixed_in_the_memory(self):
        text = "Et Monsieur YU YUNCHONG, M. WANG XIAOMING et Mme AMINA TAZI"
        self.assertEqual(pinyin_names(text), ["YU YUNCHONG", "WANG XIAOMING"])
        self.assertEqual(pinyin_names(text, fixed_names=["yu yunchong"]), ["WANG XIAOMING"])


class WordingTests(unittest.TestCase):
    def test_review_detail_adds_the_memory_hint_only_for_pinyin_names(self):
        date = KeyInfoIssue(KIND_DATE, "日期与原文不符：原文为 1992-09-07，译文为 1992-07-09")
        name = KeyInfoIssue(KIND_PINYIN_NAME, "原文人名 YU YUNCHONG 只有拼音，译文中的「于云冲」是推测的汉字")
        self.assertEqual(review_detail([date]), f"{date.message}。请对照原文核对。")
        self.assertEqual(
            review_detail([date, name]),
            f"{date.message}；{name.message}。请对照原文核对。如需固定人名的汉字写法，可在记忆库中为该人名设定译法。",
        )

    def test_summary_lists_the_first_reasons_and_counts_the_rest(self):
        found = {f"n° ED{index}": [KeyInfoIssue(KIND_ID, f"编号 ED{index} 在译文中写成了 EB{index}")] for index in range(7)}
        summary = review_summary(found, limit=2)
        self.assertEqual(
            summary,
            "有 7 条译文中的证件号、日期或人名与原文对不上，已标记需复核："
            "编号 ED0 在译文中写成了 EB0；编号 ED1 在译文中写成了 EB1；另有 5 处。请对照原文核对。",
        )

    def test_hint_block_lists_each_name_once(self):
        self.assertEqual(name_hint_block([]), "")
        block = name_hint_block(["YU YUNCHONG", "WANG XIAOMING", "YU YUNCHONG"])
        self.assertTrue(block.startswith("本批原文中的 YU YUNCHONG、WANG XIAOMING 是用拼音书写的中国人名"))
        self.assertIn("不要写成汉字", block)


class _Engine(TranslationEngine):
    engine_name = "test/mock"

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.prompts: list[str] = []

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.prompts.append(system_prompt)
        return {text: self.answers.get(text, f"译文：{len(text)}") for text in texts}


def _session(target_lang="zh", entries=(), logs=None) -> TaskTermSession:
    session = TaskTermSession(
        SimpleNamespace(apply_in_sentences=True, term_retry_attempts=2, term_batch_budget=20),
        target_lang=target_lang,
        smart_mode=False,
        log=(lambda level, message: logs.append((level, message))) if logs is not None else None,
    )
    if entries:
        session.index = TermIndex(list(entries))
    return session


class NameHintTests(unittest.TestCase):
    def test_batches_with_a_pinyin_name_get_the_hint(self):
        inner = _Engine()
        engine = _session().wrap(inner)
        self.assertIsInstance(engine, TermAwareEngine)
        engine.translate_batch(["Monsieur YU YUNCHONG, locataire", "Le loyer est payable"], "zh", "SYS", source_lang="fr")
        engine.translate_batch(["Le loyer est payable d'avance"], "zh", "SYS", source_lang="fr")
        self.assertIn("本批原文中的 YU YUNCHONG 是用拼音书写的中国人名", inner.prompts[0])
        self.assertEqual(inner.prompts[1], "SYS")

    def test_names_fixed_in_the_memory_get_no_hint(self):
        fixed = TermEntry(
            id=1, source="YU YUNCHONG", target="于云冲（YU YUNCHONG）", lang_pair="fr-zh", usage="always", origin="auto"
        )
        inner = _Engine()
        _session(entries=[fixed]).wrap(inner).translate_batch(["Monsieur YU YUNCHONG"], "zh", "SYS", source_lang="fr")
        self.assertNotIn("拼音书写的中国人名", inner.prompts[0])

    def test_other_targets_without_entries_are_not_wrapped(self):
        inner = _Engine()
        self.assertIs(_session(target_lang="fr").wrap(inner), inner)


class SessionCheckTests(unittest.TestCase):
    def test_check_keeps_the_issues_and_logs_once(self):
        logs: list[tuple[str, str]] = []
        session = _session(logs=logs)
        found = session.check_key_info(
            {
                "passeport n° ED2914389": "护照编号 EB2914389",
                "passeport n° ED2914390": "护照编号 ED2914390",
                "CIN n° K19498": "CIN n° K19498",
            },
            source_lang="fr",
        )
        self.assertEqual(list(found), ["passeport n° ED2914389"])
        self.assertEqual(session.key_info_detail("passeport n° ED2914389"),
                         "编号 ED2914389 在译文中写成了 EB2914389。请对照原文核对。")
        self.assertEqual([level for level, _message in logs], ["WARN"])
        self.assertIn("有 1 处译文中的证件号、日期或人名与原文对不上", logs[0][1])

    def test_a_broken_item_does_not_stop_the_others(self):
        session = _session()
        found = session.check_key_info({None: "x", "passeport n° ED2914389": "护照编号 EB2914389"})  # type: ignore[dict-item]
        self.assertEqual(list(found), ["passeport n° ED2914389"])


class RunnerHookTests(unittest.TestCase):
    """Excel 和 Word 流程里的接法：标需复核、写明原因、不进记忆库。"""

    def test_excel_marks_cells_and_summarises_the_reasons(self):
        from core.task_runner import TaskRunner

        session = _session()
        marks: dict[str, str] = {}
        issues: list[dict] = []
        TaskRunner._check_key_info(
            None,  # type: ignore[arg-type]
            session,
            global_translations={"CIN n° K19498": "身份证编号 R19498", "Loyer": "租金"},
            source_lang="fr",
            excel_review_marks=marks,
            quality_issues=issues,
        )
        self.assertEqual(marks, {"CIN n° K19498": REVIEW_MARK_KEY_INFO})
        [issue] = issues
        self.assertEqual((issue["type"], issue["severity"], issue["count"]), (REVIEW_MARK_KEY_INFO, "needs_review", 1))
        self.assertIn("编号 K19498 在译文中写成了 R19498", issue["message"])
        runner = SimpleNamespace(_term_session=session, _smart_mode=False)
        self.assertTrue(TaskRunner._smart_skips_tm(runner, "CIN n° K19498"))  # type: ignore[arg-type]
        self.assertFalse(TaskRunner._smart_skips_tm(runner, "Loyer"))  # type: ignore[arg-type]

    def test_word_lists_each_location_and_keeps_it_out_of_the_memory(self):
        session = _session()
        marks: dict[str, str] = {}
        issues: list[dict] = []
        no_tm: set[str] = set()
        source = "né le 07/09/1992"
        WordTaskRunner._check_key_info(
            None,  # type: ignore[arg-type]
            session,
            global_translations={source: "1992年7月9日出生", "Loyer": "租金"},
            source_lang="fr",
            review_marks=marks,
            quality_issues=issues,
            segment_locations={source: [{"file": "bail.docx", "location": "body.paragraph[3]",
                                         "location_label": "正文第 3 段", "snippet": source}]},
            no_tm_sources=no_tm,
        )
        self.assertEqual(set(marks), {source})
        self.assertEqual(no_tm, {source})
        [issue] = issues
        self.assertEqual(issue["problem"], REVIEW_PROBLEM)
        self.assertEqual(issue["severity"], "needs_review")
        self.assertEqual(issue["location_label"], "正文第 3 段")
        self.assertEqual(issue["status"], "日期与原文不符：原文为 1992-09-07，译文为 1992-07-09。请对照原文核对。")



class AdapterRunnerTests(IsolatedAppDataTestCase):
    """扩展格式（PPT 等）：对不上的保留译文、列进需复核清单，原因写在清单里。"""

    def test_listed_with_the_reason_and_the_name_hint_is_sent(self):
        from api.surfaces.adapter import AdapterSurface
        from api.surfaces.base import RunnerRequest
        from api.task_contracts import TaskOptions
        from core import tm_manager
        from core.format_adapter import InProcessAdapterHost, load_manifest
        from core.task_runner_messages import DoneMsg
        from settings import AppSettings, ExtensionSettings
        from tests.test_adapter_runner import _drain, _item
        from tests.test_format_adapter import ECHO_DIR

        tm_manager.init_db()
        source_dir = self.app_data_dir.parent / "docs"
        source_dir.mkdir(parents=True)
        path = source_dir / "bail.txt"
        path.write_text(f"{SCREENSHOT}\nLe loyer est payable d'avance.\n", encoding="utf-8")
        engine = _Engine({
            SCREENSHOT: "和于云聪先生（YU YUNCHONG），中国国籍，1992年9月7日出生于中国，持有中国护照编号 ED2914389，",
            "Le loyer est payable d'avance.": "租金须预付。",
        })
        manifest = load_manifest(ECHO_DIR)
        surface = AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest))
        settings = AppSettings(source_lang="fr", target_lang="zh")
        settings.extensions["echo"] = ExtensionSettings(smart_context=False)
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine), patch(
            "core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None
        ):
            runner = surface.build_runner(
                RunnerRequest(
                    files=[_item(path)],
                    settings=settings,
                    source_root=source_dir,
                    options=TaskOptions(options={}),
                    source_lang="fr",
                    key_overrides={},
                )
            )
            runner.start()
            messages = _drain(runner)
        done = next(message for message in reversed(messages) if isinstance(message, DoneMsg))
        result = done.file_results[0]
        self.assertEqual((result["status"], result["review_count"]), ("needs_review", 1))
        # 译文照样写出，不改、不重译。
        self.assertIn("于云聪先生", Path(result["output_path"]).read_text(encoding="utf-8"))
        [item] = done.review["items"]
        self.assertEqual(item["action"], "已保留译文")
        self.assertTrue(item["issue"].startswith(f"{REVIEW_PROBLEM}：原文人名 YU YUNCHONG 只有拼音"), item["issue"])
        self.assertTrue(any("本批原文中的 YU YUNCHONG 是用拼音书写的中国人名" in prompt for prompt in engine.prompts))


if __name__ == "__main__":
    unittest.main()
