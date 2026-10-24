"""原位替换译成中文以外的语言，以及读中文原文（2026-10-21）。

规则与 CAD「译文写回」一致：放不下先压窄、再缩小（初始比例 → 每档 0.1 → 最小比例），
最小比例仍放不下才往下方空白处换行；译文字体按目标语言挑电脑自带的字体。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest

from core.cad_layout import DEFAULT_FIT_SETTINGS, DEFAULT_FIT_SETTINGS_ZH, FitSettings
from core.pdf_vector import classify as ct
from core.pdf_vector.common import norm_text, numbers
from core.pdf_vector.fonts import FontSet, find_cjk_font, looks_bold, looks_serif, needs_shaping, target_supported
from core.pdf_vector.runner import guess_language
from settings import PdfSettings

fitz = pytest.importorskip("fitz")

FR_EN = {
    "Plan de toiture": "Roof plan",
    "Mur coupe feu 1 heure": "1-hour fire wall",
    "Détection automatique d'incendie": "Automatic fire detection",
}
ZH_FR = {
    "设备基坑": "Fosse d'équipement",
    "坑底标高": "Cote du fond de fosse",
    "一层平面图": "Plan du rez-de-chaussée",
}


def _latin_font() -> Path | None:
    return FontSet("en").pick("Roof plan é") and FontSet("en").pick("Roof plan é").path


def _fake(mapping, prompts=None):
    table = {norm_text(k): v for k, v in mapping.items()}

    def translate(texts, prompt, **_extra):
        if prompts is not None:
            prompts.append(prompt)
        return {t: table.get(norm_text(t), t) for t in texts}

    return translate


class _PageCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _run(self, build, mapping, target, **kwargs):
        from core.pdf_vector import translate_page_inplace

        source = self.root / "source.pdf"
        doc = fitz.open()
        page = doc.new_page(width=842, height=595)
        build(page)
        doc.save(source)
        prompts: list[str] = []
        result = translate_page_inplace(source, 0, self.root / "work", translate=_fake(mapping, prompts),
                                        source_lang="auto", target_lang=target, **kwargs)
        return result, prompts


@unittest.skipUnless(_latin_font(), "no Latin font on this machine")
class FrenchToEnglishTests(_PageCase):
    def _build(self, page):
        page.insert_text((60, 60), "Plan de toiture", fontsize=20)
        page.draw_rect(fitz.Rect(55, 90, 230, 130), color=(0, 0, 0), width=0.8)
        page.insert_text((60, 115), "Mur coupe feu 1 heure", fontsize=11)
        page.insert_text((60, 170), "Détection automatique d'incendie", fontsize=11)
        page.draw_line((40, 200), (800, 200), color=(1, 0, 0), width=1)

    def test_english_replaces_french_and_keeps_the_drawing(self):
        result, prompts = self._run(self._build, FR_EN, "en")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.written, 3)
        self.assertEqual(result.source_lang, "fr")
        self.assertIn("把每条法语原文逐条翻译成英文工程术语", prompts[0])
        with fitz.open(result.output_pdf) as out:
            text = out[0].get_text()
            fonts = {f[3] for f in out[0].get_fonts(full=True)}
            self.assertEqual(len(out[0].get_drawings()), 2)   # the frame and the red line survive
        for english in FR_EN.values():
            self.assertIn(english, text)
        self.assertNotIn("Plan de toiture", text)
        # 西文译文用西文字体写，不用中文黑体
        self.assertFalse(any("Hei" in name or "CJK" in name for name in fonts), fonts)

    def test_lines_already_in_the_target_language_are_kept(self):
        def build(page):
            page.insert_text((60, 60), "Plan de toiture", fontsize=20)
            page.insert_text((60, 120), "Coupe A-A", fontsize=11)

        result, _prompts = self._run(build, FR_EN, "fr")
        # 译成法文：法文标注本来就是目标语言，一处不换，这一页保留原样
        self.assertEqual(result.written, 0)
        self.assertEqual(result.kept, 2)

    def test_a_label_packed_against_a_code_leaves_the_code_whole(self):
        # 行距 8 磅、字号 10 磅：两行的字框（Helvetica 高 1.37 个字号）叠进对方三分之一，
        # 换掉标注时型号曾整行被删（macOS 上 Arial Unicode 的中文标注同样如此）
        mapping = {"Capacité 500 portes": "Capacity 500 doors"}
        for above, below in (("5*TN3000", "Capacité 500 portes"), ("Capacité 500 portes", "5*TN3000")):
            with self.subTest(above=above):
                def build(page, above=above, below=below):
                    page.insert_text((100, 100), above, fontsize=10)
                    page.insert_text((100, 108), below, fontsize=10)

                result, _prompts = self._run(build, mapping, "en")
                self.assertEqual(result.written, 1, result.review_items)
                with fitz.open(result.output_pdf) as out:
                    text = out[0].get_text()
                self.assertIn("5*TN3000", text)
                self.assertIn("Capacity 500 doors", text)
                self.assertNotIn("Capacité", text)

    def test_text_layer_reads_plain_spaces_and_hyphens(self):
        # Arial on macOS draws the space and U+00A0, the hyphen and U+00AD with one glyph each;
        # MuPDF's map then read 「1\xadhour\xa0fire\xa0wall」.  Same map on the font here.
        from core.pdf_vector.writeback import set_to_unicode

        path, text = _latin_font(), "1-hour fire wall"
        font = fitz.Font(fontfile=str(path))
        doc = fitz.open()
        page = doc.new_page()
        page.insert_font(fontname="XLT0", fontfile=str(path))
        page.insert_text((50, 100), text, fontname="XLT0", fontsize=12)
        xref = next(f[0] for f in page.get_fonts(full=True) if f[4] == "XLT0")
        mac = {" ": "\xa0", "-": "\xad"}
        pairs = "\n".join(f"<{font.has_glyph(ord(c)):04x}> <{ord(mac.get(c, c)):04x}>" for c in sorted(set(text)))
        bad = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /Adobe-Identity-UCS def\n"
               "/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
               f"{len(set(text))} beginbfchar\n{pairs}\nendbfchar\nendcmap\n"
               "CMapName currentdict /CMap defineresource pop\nend\nend\n")
        doc.update_stream(int(doc.xref_get_key(xref, "ToUnicode")[1].split()[0]), bad.encode())

        def read(d):
            with fitz.open("pdf", d.tobytes()) as copy:
                return copy[0].get_text().strip()

        self.assertEqual(read(doc), "1\xadhour\xa0fire\xa0wall")
        set_to_unicode(doc, page, {"XLT0": (font, set(text))})
        self.assertEqual(read(doc), text)


@unittest.skipUnless(find_cjk_font() and _latin_font(), "no CJK or Latin font on this machine")
class ChineseToFrenchTests(_PageCase):
    def _build(self, page):
        page.insert_font(fontname="zh", fontfile=str(find_cjk_font()))
        for x in (60, 200, 340):
            page.draw_line((x, 80), (x, 160), color=(0, 0, 0), width=0.8)
        for y in (80, 120, 160):
            page.draw_line((60, y), (340, y), color=(0, 0, 0), width=0.8)
        page.insert_text((70, 105), "设备基坑", fontname="zh", fontsize=12)
        page.insert_text((210, 105), "坑底标高", fontname="zh", fontsize=12)
        page.insert_text((70, 145), "-0.090", fontsize=10)
        page.insert_text((400, 60), "一层平面图", fontname="zh", fontsize=20)

    def test_chinese_text_layer_is_translated_into_french(self):
        result, prompts = self._run(self._build, ZH_FR, "fr", tesseract="/no/tesseract/here")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.written, 3)
        self.assertEqual(result.kept, 1)                     # -0.090
        self.assertEqual(result.source_lang, "zh")
        self.assertFalse(result.ocr_used)                    # no Latin OCR over Chinese strokes
        self.assertTrue(result.ocr_skipped_cjk)
        self.assertIn("把每条中文原文逐条翻译成法文工程术语", prompts[0])
        with fitz.open(result.output_pdf) as out:
            text = out[0].get_text()
        self.assertIn("Fosse d'équipement", text)
        self.assertIn("Plan du rez-de-chaussée", text)
        self.assertNotIn("设备基坑", text)

    def test_a_translation_still_holding_chinese_is_not_written(self):
        result, _prompts = self._run(self._build, {**ZH_FR, "设备基坑": "Fosse 设备"}, "fr")
        self.assertEqual(result.written, 2)
        self.assertEqual(result.review, 1)
        self.assertEqual(result.review_items[0]["reason"], "译文中仍有原文")

    def test_removing_a_label_leaves_the_code_right_above_it(self):
        # 型号和容量叠得很紧（行距小于字框高）：换掉「500门」不能把上一行型号的字一起删掉
        def build(page):
            page.insert_font(fontname="zh", fontfile=str(find_cjk_font()))
            page.insert_text((100, 100), "5*TN3000", fontsize=10)
            page.insert_text((100, 108), "500门", fontname="zh", fontsize=10)

        result, _prompts = self._run(build, {"500门": "Capacité : 500"}, "fr")
        self.assertEqual(result.written, 1, result.review_items)
        with fitz.open(result.output_pdf) as out:
            text = out[0].get_text()
        self.assertIn("5*TN3000", text)
        self.assertIn("Capacité : 500", text)
        self.assertNotIn("门", text)

    def test_dates_written_with_month_names_are_written(self):
        def build(page):
            page.insert_font(fontname="zh", fontfile=str(find_cjk_font()))
            page.insert_text((60, 100), "2026年3月24日", fontname="zh", fontsize=10)
            page.insert_text((60, 160), "2026年4月1日", fontname="zh", fontsize=10)

        result, _prompts = self._run(build, {"2026年3月24日": "24 mars 2026", "2026年4月1日": "1er avril 2026"}, "fr")
        self.assertEqual(result.written, 2, result.review_items)

    def test_chinese_target_keeps_chinese_labels(self):
        result, _prompts = self._run(self._build, ZH_FR, "zh")
        self.assertEqual(result.written, 0)
        self.assertEqual(result.kept, 4)


@unittest.skipUnless(find_cjk_font() and _latin_font(), "no CJK or Latin font on this machine")
class FitTests(_PageCase):
    LONG = {"配电室": "Local de distribution électrique basse tension", "水泵房": "Salle des pompes"}

    def _build(self, page):
        page.insert_font(fontname="zh", fontfile=str(find_cjk_font()))
        for x in (60, 150, 240):
            page.draw_line((x, 80), (x, 200), color=(0, 0, 0), width=0.8)
        for y in (80, 200):
            page.draw_line((60, y), (240, y), color=(0, 0, 0), width=0.8)
        page.insert_text((70, 110), "配电室", fontname="zh", fontsize=12)
        page.insert_text((160, 110), "水泵房", fontname="zh", fontsize=12)

    def _written(self):
        import json

        return {w["source_text"]: w for w in json.loads((self.root / "work" / "writeback.json").read_text("utf-8"))["written"]}

    def test_shrinks_by_cad_tiers_then_wraps(self):
        result, _ = self._run(self._build, self.LONG, "fr", fit=DEFAULT_FIT_SETTINGS)
        self.assertTrue(result.ok, result.error)
        written = self._written()
        pumps = written["水泵房"]
        # 一行放不下：直接缩到初始比例 70%，先只压窄（字高不变）
        self.assertEqual(pumps["fit"], "shrunk")
        self.assertEqual(pumps["scale"], 0.7)
        self.assertEqual(pumps["width_factor"], 0.7)
        self.assertEqual(pumps["fontsize"], pumps["target_fontsize"])
        power = written["配电室"]
        # 缩到最小比例一行仍放不下：往格子里下方的空白处换行，取放得下的最大一档
        self.assertEqual(power["fit"], "wrapped_into_blank")
        self.assertGreater(len(power["lines"]), 1)
        self.assertGreaterEqual(power["scale"], DEFAULT_FIT_SETTINGS.min_scale)

    def test_minimum_scale_is_respected(self):
        # 最小比例设成 100%：放不下的不缩，只换行；换行也放不下的保留原文、列进复核
        result, _ = self._run(self._build, {"配电室": "Local de distribution électrique basse tension "
                                                     "et salle des transformateurs principaux du bâtiment"},
                              "fr", fit=FitSettings(1.0, 1.0))
        self.assertEqual(result.written, 0)
        self.assertEqual(result.not_written, 1)
        self.assertEqual(result.review_items[0]["reason"], "空间不够，放不下译文")


class ClassifyTests(unittest.TestCase):
    def _row(self, text, origin="native"):
        return {"source_text": text, "origin": origin}

    def test_chinese_rows_are_translated_unless_the_target_is_chinese(self):
        self.assertEqual(ct.classify(self._row("设备基坑"), 0.45, "zh", target_lang="fr"), ("translate", "candidate"))
        self.assertEqual(ct.classify(self._row("设备基坑"), 0.45, "zh", target_lang="zh"), ("keep_source", "already_target"))
        self.assertEqual(ct.classify(self._row("Q235钢板"), 0.45, "zh", target_lang="zh"), ("keep_source", "already_target"))
        self.assertEqual(ct.classify(self._row("PLAN DE MASSE"), 0.45, "fr", target_lang="fr"),
                         ("keep_source", "already_target"))
        self.assertEqual(ct.classify(self._row("Door schedule"), 0.45, "en", target_lang="fr"), ("translate", "candidate"))
        self.assertEqual(ct.classify(self._row("KL1"), 0.45, "zh", target_lang="fr"), ("keep_source", "retained_identifier"))

    def test_two_chinese_labels_one_character_apart_stay_separate(self):
        rows = [self._row("一层平面图（东区）给排水"), self._row("一层平面图（西区）给排水")]
        canon = ct.canonicalize(rows)
        self.assertEqual(len(set(canon.values())), 2)
        # OCR 读出来的近似写法照旧合并
        ocr = [self._row("Réservation pour gaine technique", "ocr"), self._row("Reservation pour gaine techniqu", "ocr")]
        self.assertEqual(len(set(ct.canonicalize(ocr).values())), 1)

    def test_translation_checks_follow_the_target(self):
        self.assertEqual(ct.translation_problem("Plan", "平面图", "zh"), "")
        self.assertEqual(ct.translation_problem("Plan", "Plan", "zh"), "retained_by_model")
        self.assertEqual(ct.translation_problem("Plan", "Floor plan", "zh"), "retained_by_model")
        self.assertEqual(ct.translation_problem("设备基坑", "Fosse d'équipement", "fr"), "")
        self.assertEqual(ct.translation_problem("设备基坑", "Fosse 设备", "fr"), "residual_source")
        self.assertEqual(ct.translation_problem("Plan", "План", "ru"), "")
        self.assertEqual(ct.translation_problem("Plan", "Plan view", "ru"), "retained_by_model")

    def test_missing_chinese_translation_gets_another_try(self):
        calls = []

        def translate(texts, _prompt):
            calls.append(list(texts))
            return {} if len(calls) == 1 else {t: "Fosse" for t in texts}

        res, retried = ct._translate_with_retry(["设备基坑"], "p", translate, [])
        self.assertEqual(retried, 1)
        self.assertEqual(res, {"设备基坑": "Fosse"})

    def test_the_page_source_language_reaches_the_engine(self):
        seen = []

        def translate(texts, _prompt, source_lang=None):
            seen.append(source_lang)
            return {t: "Fosse d'équipement" for t in texts}

        ct.classify_and_translate([self._row("设备基坑")], translate=translate, source_lang="zh", target_lang="fr")
        self.assertEqual(seen, ["zh"])   # the engine is told "from Chinese", not "from auto"
        rows = [self._row("设备基坑")]
        ct.classify_and_translate(rows, translate=lambda texts, _prompt: {t: "Fosse" for t in texts},
                                  source_lang="zh", target_lang="fr")
        self.assertEqual(rows[0]["translation"], "Fosse")   # hooks without the keyword still work

    def test_dates_count_as_day_month_year_however_written(self):
        def outcome(source, translation):
            rows = [self._row(source)]
            ct.classify_and_translate(rows, translate=lambda texts, _p: {t: translation for t in texts},
                                      source_lang="zh", target_lang="fr")
            return rows[0]["decision"], rows[0]["decision_reason"]

        for translation in ("24 mars 2026", "24/03/2026", "24.03.2026", "March 24, 2026", "2026-03-24"):
            self.assertEqual(outcome("2026年3月24日", translation), ("write", "model_translation"), translation)
        # a wrong day or month is still caught
        self.assertEqual(outcome("2026年3月24日", "25 mars 2026"), ("review", "numeric_mismatch"))
        self.assertEqual(outcome("2026年3月24日", "24 avril 2026"), ("review", "numeric_mismatch"))
        # a word that only looks like a month counts only next to a number
        self.assertEqual(numbers("Le mur mai", dates=True), [])

    def test_target_name_in_the_prompt(self):
        self.assertEqual(ct.target_language_name("zh"), "简体中文")
        self.assertEqual(ct.target_language_name("fr"), "法文")
        self.assertEqual(ct.target_language_name("en"), "英文")


class LanguageAndFontTests(unittest.TestCase):
    def test_source_language_by_script(self):
        self.assertEqual(guess_language(["一层平面图 设备基坑 KL1 C30"]), "zh")
        self.assertEqual(guess_language(["Plan de masse des bâtiments 设备"]), "fr")
        self.assertEqual(guess_language([""], fallback="en"), "en")

    def test_targets_that_need_shaping_are_not_supported(self):
        for code in ("ar", "he", "th", "hi"):
            self.assertFalse(target_supported(code), code)
        for code in ("zh", "en", "fr", "ru", "ja", "ko", "vi", "el"):
            self.assertTrue(target_supported(code), code)
        self.assertTrue(needs_shaping("مخطط"))
        self.assertFalse(needs_shaping("Plan du rez-de-chaussée"))

    def test_font_style_names(self):
        self.assertTrue(looks_serif("ABCDEF+TimesNewRomanPSMT"))
        self.assertTrue(looks_serif("SimSun"))
        self.assertFalse(looks_serif("ArialMT"))
        self.assertFalse(looks_serif("romans"))       # AutoCAD's single-stroke Roman Simplex is a sans
        # subset fonts named only 「CIDFont+F1」 go by the font's serif flag; a known name wins
        self.assertTrue(looks_serif("CIDFont+F1", 4))
        self.assertFalse(looks_serif("CIDFont+F1", 0))
        self.assertFalse(looks_serif("ArialMT", 4))
        self.assertTrue(looks_bold("Arial-BoldMT"))
        self.assertTrue(looks_bold("SimHei", 16))
        self.assertFalse(looks_bold("SimHei"))

    @unittest.skipUnless(_latin_font(), "no Latin font on this machine")
    def test_a_font_with_every_glyph_is_picked(self):
        fonts = FontSet("fr")
        choice = fonts.pick("Œuvre côté façade")
        self.assertIsNotNone(choice)
        font = fitz.Font(fontfile=str(choice.path))
        self.assertTrue(all(font.has_glyph(ord(c)) for c in "Œôç"))
        self.assertIsNone(FontSet("fr", forced=choice.path).pick("\U0001d400\U0001d401"))  # math letters: nothing fits

    def test_decimal_comma_is_the_same_number(self):
        self.assertEqual(numbers("épaisseur 0,5 m"), numbers("厚 0.5 m"))


class SettingsTests(unittest.TestCase):
    def test_defaults_follow_cad_per_target(self):
        pdf = PdfSettings()
        self.assertEqual(pdf.inplace_font, "sans")
        self.assertEqual(pdf.inplace_fit("zh"), DEFAULT_FIT_SETTINGS_ZH)
        self.assertEqual(pdf.inplace_fit("fr"), DEFAULT_FIT_SETTINGS)

    def test_out_of_range_values_are_clamped_and_unknown_font_reset(self):
        pdf = PdfSettings.model_validate({"inplace_font": "comic", "inplace_fit_default_scale": 3,
                                          "inplace_fit_min_scale": 0.05, "inplace_fit_min_scale_zh": 0.9,
                                          "inplace_fit_default_scale_zh": 0.6})
        self.assertEqual(pdf.inplace_font, "sans")
        self.assertEqual((pdf.inplace_fit_default_scale, pdf.inplace_fit_min_scale), (1.0, 0.3))
        self.assertEqual((pdf.inplace_fit_default_scale_zh, pdf.inplace_fit_min_scale_zh), (0.6, 0.6))

    def test_old_settings_files_get_the_defaults(self):
        pdf = PdfSettings.model_validate({"target_lang": "fr", "translation_route": "auto"})
        self.assertEqual(pdf.inplace_fit(), DEFAULT_FIT_SETTINGS)
