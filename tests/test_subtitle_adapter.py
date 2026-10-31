"""The built-in 「字幕」 type (``adapters/subtitle``): .srt, .vtt, .ass / .ssa.

The samples in ``adapters/subtitle/samples`` must come back byte for byte when
every translation equals its source.  Numbers, timings, cue settings, styles
and formatting codes never change; a sentence cut across subtitles goes out as
one unit with one ``<gN>`` mark per subtitle and is split back by the marks, or
by length (listed for review) when the marks did not come back.
"""

from __future__ import annotations

import codecs
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from api.surfaces.adapter import AdapterSurface
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskOptions
from config import CORE_TRANSLATION_PROMPT
from core import tm_manager
from core.format_adapter import BUNDLED_ADAPTERS_DIR, InProcessAdapterHost, load_manifest
from core.task_runner_messages import DoneMsg
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_adapter_runner import FakeEngine, _drain, _item
from translator_adapter import MODE_BILINGUAL, MODE_TRANSLATION_ONLY, TextUnit, TranslatedUnit

SUBTITLE_DIR = BUNDLED_ADAPTERS_DIR / "subtitle"
SAMPLES = sorted((SUBTITLE_DIR / "samples").iterdir())
HOST = InProcessAdapterHost(SUBTITLE_DIR)
ROOT = Path(__file__).resolve().parents[1]
_MARK = re.compile(r"</?g\d+>")


def _units(path: Path) -> list[TextUnit]:
    return HOST.extract(path, {})


def _write(
    path: Path,
    translate: Any,
    mode: str = MODE_TRANSLATION_ONLY,
    *,
    target_lang: str = "fr",
    out: Path | None = None,
) -> tuple[str, list[Any]]:
    """``translate(unit)`` returns the translation, or ``(translation, marked_translation)``."""
    units = []
    for unit in _units(path):
        value = translate(unit)
        translation, marked = value if isinstance(value, tuple) else (value, "")
        units.append(TranslatedUnit(id=unit.id, text=unit.text, translation=translation, marked_translation=marked))
    target = out or path.with_name(f"out_{path.name}")
    result = HOST.write(path, units, mode, target, {"target_lang": target_lang})
    return target.read_bytes().decode("utf-8-sig"), result.review_items


def _file(tmp_path: Path, name: str, body: str, *, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(body.encode(encoding))
    return path


def _keep_marks(unit: TextUnit) -> tuple[str, str]:
    """Each piece becomes ``T<n>``, the marks kept: the expected split is obvious."""
    if not unit.marked:
        return f"T {unit.id}", ""
    marked = re.sub(r"<g(\d+)>.*?</g\1>", lambda m: f"<g{m.group(1)}>T{m.group(1)}</g{m.group(1)}>", unit.marked)
    return _MARK.sub("", marked), marked


SRT = """1
00:00:01,000 --> 00:00:03,000
今天我们主要讲一下

2
00:00:03,000 --> 00:00:06,000
防火分区应该怎么划分。

3
00:00:07,000 --> 00:00:09,000
下一个问题
"""


# ── the type ────────────────────────────────────────────────────────────────


def test_manifest_describes_the_subtitle_page() -> None:
    manifest = load_manifest(SUBTITLE_DIR)
    assert manifest.key == "ext:subtitle"
    assert manifest.label["zh"] == "字幕"
    assert manifest.extensions == ["srt", "vtt", "ass", "ssa"]
    assert manifest.capabilities.bilingual and manifest.capabilities.translation_only
    # Hidden until the user turns it on (or says yes when dropping a subtitle file).
    assert manifest.visible_by_default is False
    assert manifest.prompt.startswith(CORE_TRANSLATION_PROMPT)
    assert "<g1>" in manifest.prompt
    assert manifest.icon == "subtitle"
    icons = (ROOT / "ui" / "src" / "icons.ts").read_text(encoding="utf-8")
    assert '"subtitle",' in icons and "subtitle: {" in icons


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda path: path.name)
@pytest.mark.parametrize("mode", [MODE_TRANSLATION_ONLY, MODE_BILINGUAL])
def test_samples_come_back_byte_for_byte(tmp_path: Path, sample: Path, mode: str) -> None:
    out = tmp_path / sample.name
    units = [TranslatedUnit(id=u.id, text=u.text, translation=u.text) for u in _units(sample)]
    HOST.write(sample, units, mode, out, {"target_lang": "fr"})
    assert out.read_bytes() == sample.read_bytes()


def test_sample_units() -> None:
    by_name = {sample.name: _units(sample) for sample in SAMPLES}
    srt = by_name["培训视频.srt"]
    assert [unit.id for unit in srt] == ["c1", "c2-3", "c4", "c5.1", "c5.2"]  # the ♪ line has no words
    joined = srt[1]
    assert joined.text == "今天我们主要讲一下防火分区应该怎么划分。"
    assert joined.marked == "<g1>今天我们主要讲一下</g1><g2>防火分区应该怎么划分。</g2>"
    assert joined.context == "第 2–3 条 · 00:01:05"
    # The opening <i> stays out of the words; the closing one inside is protected.
    assert srt[2].text == "注意：</i>每个分区面积不超过 2500 m²。" and srt[2].protected == ["</i>"]
    assert [unit.text for unit in srt[3:]] == ["有问题吗？", "没有。"]
    assert srt[3].context == "第 5 条第 1 行 · 00:01:14"

    vtt = by_name["lecture.vtt"]
    # Speaker tags stay out; header, NOTE, STYLE and cue settings are never units.
    assert vtt[0].text == "欢迎来到第二讲我们从疏散通道的宽度说起。"
    assert vtt[1].text == "好的&amp;明白" and vtt[1].protected == ["&amp;"]
    assert "NOTE" not in "".join(unit.text for unit in vtt)

    ass = by_name["特效字幕.ass"]
    # Comment lines and drawings are skipped; another speaker is never joined.
    assert [unit.id for unit in ass] == ["c1", "c2-3"]
    assert ass[0].text == "第一章 设备检查"
    assert ass[1].protected == ["{\\i1}", "{\\i0}"]
    assert ass[1].text == "检查灭火器时，{\\i1}先看压力表{\\i0}再看有效期，最后确认铅封完好。"


def test_scan_counts_units_and_warns_about_files_without_subtitles(tmp_path: Path) -> None:
    assert HOST.scan(SAMPLES[1], {}).unit_count == 5
    empty = HOST.scan(_file(tmp_path, "notes.srt", "只是一些文字\n"), {})
    assert empty.unit_count == 0 and empty.warnings


# ── grouping ────────────────────────────────────────────────────────────────


def test_a_sentence_end_a_long_gap_or_four_subtitles_close_a_group(tmp_path: Path) -> None:
    units = _units(_file(tmp_path, "a.srt", SRT))
    # 2 ends with 。; 3 starts after a gap, and stands alone.
    assert [unit.id for unit in units] == ["c1-2", "c3"]

    cues = "\n".join(
        f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},900\nword{n}\n" for n in range(1, 7)
    )
    units = _units(_file(tmp_path, "b.srt", cues))
    assert [unit.id for unit in units] == ["c1-4", "c5-6"]
    assert units[0].text == "word1 word2 word3 word4"
    assert units[0].marked == "<g1>word1</g1> <g2>word2</g2> <g3>word3</g3> <g4>word4</g4>"

    ellipsis = "1\n00:00:01,000 --> 00:00:02,000\nI was going to...\n\n2\n00:00:02,000 --> 00:00:03,000\ntell you.\n\n3\n00:00:03,000 --> 00:00:04,000\nmore\n"
    assert [unit.id for unit in _units(_file(tmp_path, "c.srt", ellipsis))] == ["c1-2", "c3"]


def test_two_lines_of_one_subtitle_are_one_sentence(tmp_path: Path) -> None:
    body = "1\n00:00:01,000 --> 00:00:04,000\nThe exits must stay\nclear at all times.\n"
    units = _units(_file(tmp_path, "a.srt", body))
    assert [unit.text for unit in units] == ["The exits must stay clear at all times."]


# ── writing ─────────────────────────────────────────────────────────────────


def test_marks_split_the_translation_over_the_subtitles(tmp_path: Path) -> None:
    text, review = _write(_file(tmp_path, "a.srt", SRT), _keep_marks)
    assert text == (
        "1\n00:00:01,000 --> 00:00:03,000\nT1\n\n"
        "2\n00:00:03,000 --> 00:00:06,000\nT2\n\n"
        "3\n00:00:07,000 --> 00:00:09,000\nT c3\n"
    )
    assert review == []


def test_without_marks_the_translation_is_split_by_length_and_listed(tmp_path: Path) -> None:
    def translate(unit: TextUnit) -> str:
        return "Aujourd'hui, nous allons voir comment découper les compartiments." if unit.marked else "Suivant"

    text, review = _write(_file(tmp_path, "a.srt", SRT), translate)
    assert "\nAujourd'hui, nous allons voir\n\n2\n" in text
    assert "\ncomment découper les compartiments.\n\n3\n" in text
    assert [(item.id, item.context) for item in review] == [("c1-2", "第 1–2 条 · 00:00:01")]
    assert "按长短比例" in review[0].reason


@pytest.mark.parametrize("marked", ["<g1>Un</g1> <g1>deux</g1>", "<g1>Un deux</g1> <g2></g2>", "<g2>Un</g2> <g1>deux"])
def test_broken_marks_fall_back_to_the_length_split(tmp_path: Path, marked: str) -> None:
    def translate(unit: TextUnit) -> Any:
        return ("Un deux", marked) if unit.marked else "Suivant"

    _text, review = _write(_file(tmp_path, "a.srt", SRT), translate)
    assert [item.id for item in review] == ["c1-2"]


def test_reordered_marks_and_words_outside_marks(tmp_path: Path) -> None:
    def translate(unit: TextUnit) -> Any:
        return ("Alors deux, un", "Alors <g2>deux</g2>, <g1>un</g1>") if unit.marked else "Suivant"

    text, review = _write(_file(tmp_path, "a.srt", SRT), translate)
    # Each subtitle keeps the words of its own mark; loose words join the mark they follow.
    assert "00:00:03,000\nun\n" in text
    assert "00:00:06,000\nAlors deux,\n" in text
    assert review == []


def test_long_translations_wrap_on_two_lines_and_fast_ones_are_listed(tmp_path: Path) -> None:
    body = "1\n00:00:01,000 --> 00:00:06,000\n每个分区面积不超过两千五百平方米。\n\n2\n00:00:07,000 --> 00:00:08,000\n快\n"
    long = "Chaque compartiment ne doit pas dépasser deux mille cinq cents mètres carrés."

    def translate(unit: TextUnit) -> str:
        return long if unit.id == "c1" else "Une phrase bien trop longue pour une seule seconde"

    text, review = _write(_file(tmp_path, "a.srt", body), translate)
    lines = text.split("\n")
    first = lines[2:4]
    assert " ".join(first) == long and all(len(line) <= 42 for line in first)
    assert lines[4] == ""
    # The timings never change.
    assert "00:00:07,000 --> 00:00:08,000" in text
    assert [(item.id, item.context) for item in review] == [("c2", "第 2 条 · 00:00:07")]
    assert "读不完" in review[0].reason


def test_wrapping_into_chinese(tmp_path: Path) -> None:
    body = "1\n00:00:01,000 --> 00:00:09,000\nEvery compartment must stay below two thousand five hundred square metres.\n"
    zh = "每个防火分区的面积，都必须小于两千五百平方米。"
    text, review = _write(_file(tmp_path, "a.srt", body), lambda unit: zh, target_lang="zh")
    lines = text.split("\n")[2:4]
    assert "".join(lines) == zh and all(len(line) <= 18 for line in lines)
    assert lines[0].endswith("，")  # cut after the comma
    assert review == []


def test_never_start_a_line_with_french_punctuation(tmp_path: Path) -> None:
    body = "1\n00:00:01,000 --> 00:00:09,000\n你知道防火分区是什么意思吗\n"
    fr = "Est-ce que vous savez ce que veut dire un compartiment ?"
    text, _review = _write(_file(tmp_path, "a.srt", body), lambda unit: fr)
    for line in text.split("\n"):
        assert not line.startswith("?")


def test_dialogue_lines_keep_their_dashes(tmp_path: Path) -> None:
    body = "1\n00:00:01,000 --> 00:00:04,000\n- 有问题吗？\n- 没有。\n"
    answers = {"有问题吗？": "Des questions ?", "没有。": "Non."}
    text, _review = _write(_file(tmp_path, "a.srt", body), lambda unit: answers[unit.text])
    assert text == "1\n00:00:01,000 --> 00:00:04,000\n- Des questions ?\n- Non.\n"
    text, _review = _write(
        _file(tmp_path, "a.srt", body), lambda unit: answers[unit.text], MODE_BILINGUAL
    )
    assert text.endswith("- 有问题吗？\n- 没有。\n- Des questions ?\n- Non.\n")


def test_formatting_codes_stay_around_the_translation(tmp_path: Path) -> None:
    body = "1\n00:00:01,000 --> 00:00:04,000\n{\\an8}<i>注意安全</i>\n"
    text, _review = _write(_file(tmp_path, "a.srt", body), lambda unit: "Attention")
    assert text.endswith("\n{\\an8}<i>Attention</i>\n")


def test_bilingual_srt_and_vtt_keep_source_lines_above(tmp_path: Path) -> None:
    text, _review = _write(_file(tmp_path, "a.srt", SRT), _keep_marks, MODE_BILINGUAL)
    assert "00:00:03,000\n今天我们主要讲一下\nT1\n\n2\n" in text
    vtt = SAMPLES[0]
    assert vtt.suffix == ".vtt"
    text, _review = _write(vtt, _keep_marks, MODE_BILINGUAL, out=tmp_path / "b.vtt")
    assert "\r\n<v 讲师>欢迎来到第二讲\r\n<v 讲师>T1\r\n\r\n" in text
    # CRLF everywhere, header and cue settings as they were.
    assert text.count("\r\n") == text.count("\n")
    assert text.startswith("WEBVTT\r\nKind: captions\r\n")
    assert "00:01.000 --> 00:03.000 align:start position:10%\r\n" in text


def test_ass_text_field_only(tmp_path: Path) -> None:
    ass = next(sample for sample in SAMPLES if sample.suffix == ".ass")
    long = "Pour un extincteur, {\\i1}regardez le manomètre{\\i0}, puis la date de validité,"

    def translate(unit: TextUnit) -> Any:
        if unit.id == "c1":
            return "Chapitre 1"
        return _MARK.sub("", f"<g1>{long}</g1> <g2>et le plomb.</g2>"), f"<g1>{long}</g1> <g2>et le plomb.</g2>"

    text, _review = _write(ass, translate, out=tmp_path / "a.ass")
    lines = text.split("\n")
    assert "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}Chapitre 1" in lines
    second = next(line for line in lines if line.startswith("Dialogue: 0,0:00:03.50"))
    assert second.startswith("Dialogue: 0,0:00:03.50,0:00:06.00,Default,旁白,0,0,0,,Pour un extincteur,")
    assert second.count("\\N") == 1 and "{\\i1}" in second and "{\\i0}" in second
    assert "Dialogue: 0,0:00:06.00,0:00:08.00,Default,旁白,0,0,0,,et le plomb." in lines
    assert "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,这一行是注释" in lines
    assert any(line.startswith("Style: Default,Microsoft YaHei,60") for line in lines)

    text, _review = _write(ass, translate, MODE_BILINGUAL, out=tmp_path / "b.ass")
    # The source stays; the position code is not repeated under it.
    assert "Default,,0,0,0,,{\\an8}第一章 设备检查\\NChapitre 1" in text


def test_a_unit_whose_source_changed_keeps_the_file_text(tmp_path: Path) -> None:
    path = _file(tmp_path, "a.srt", SRT)
    units = [TranslatedUnit(id="c3", text="别的文字", translation="Autre")]
    HOST.write(path, units, MODE_TRANSLATION_ONLY, tmp_path / "out.srt", {"target_lang": "fr"})
    assert (tmp_path / "out.srt").read_bytes() == path.read_bytes()


def test_gb18030_files_are_written_as_utf8_with_bom(tmp_path: Path) -> None:
    path = _file(tmp_path, "a.srt", SRT, encoding="gb18030")
    out = tmp_path / "out.srt"
    units = [TranslatedUnit(id=u.id, text=u.text, translation="Été") for u in _units(path)]
    HOST.write(path, units, MODE_TRANSLATION_ONLY, out, {"target_lang": "fr"})
    raw = out.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8) and "Été".encode() in raw


# ── a whole task ────────────────────────────────────────────────────────────

_NON_ASCII = re.compile(r"[^\x00-\x7f⟦⟧]")


class _AsciiEngine(FakeEngine):
    """Translates into ASCII and keeps marks and placeholders, so no check fires."""

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append(list(texts))
        self.prompts.append(system_prompt)
        return {text: "FR " + _NON_ASCII.sub("x", text) for text in texts}

    def chat(self, system, user):
        # 仅译文模式下「中文 + 外文」的内容（「2500 m²」）走结构化的那一路（core/mixed_language.py）。
        try:
            payload = json.loads(user)
        except ValueError:
            payload = None
        if isinstance(payload, list) and payload and all(isinstance(item, dict) and "text" in item for item in payload):
            self.calls.append([item["text"] for item in payload])
            return json.dumps(
                [
                    {"id": item["id"], "action": "translate", "translation": "FR " + _NON_ASCII.sub("x", item["text"])}
                    for item in payload
                ]
            )
        return json.dumps([])


class SubtitleTypeRunTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "videos"
        self.source.mkdir(parents=True)
        for sample in SAMPLES:
            (self.source / sample.name).write_bytes(sample.read_bytes())
        self.surface = AdapterSurface(load_manifest(SUBTITLE_DIR), HOST)

    def _run(self, options: dict[str, bool]) -> tuple[DoneMsg, _AsciiEngine]:
        settings = AppSettings(target_lang="fr")
        settings.extensions["subtitle"] = ExtensionSettings(smart_context=False)
        engine = _AsciiEngine()
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine):
            runner = self.surface.build_runner(
                RunnerRequest(
                    files=[_item(self.source / sample.name) for sample in SAMPLES],
                    settings=settings,
                    source_root=self.source,
                    options=TaskOptions(options=options),
                    source_lang="zh",
                    key_overrides={},
                )
            )
            runner.start()
            messages = _drain(runner)
        done = messages[-1]
        assert isinstance(done, DoneMsg), done
        return done, engine

    def test_samples_translate_with_timings_in_place(self) -> None:
        done, engine = self._run({})
        assert [result["status"] for result in done.file_results] == ["completed"] * len(SAMPLES)
        sent = "\n".join(engine.sent)
        # Joined sentences go out with one mark per subtitle; codes never reach the model.
        assert "<g1>" in sent and "<g2>" in sent
        for secret in ("{\\i1}", "&amp;", "<v ", "-->", "Dialogue", "align:start"):
            assert secret not in sent
        assert engine.prompts and all("<g1>" in prompt for prompt in engine.prompts)
        outputs = {result["name"]: Path(result["output_path"]) for result in done.file_results}
        srt = outputs["培训视频.srt"].read_text(encoding="utf-8")
        before = (self.source / "培训视频.srt").read_text(encoding="utf-8")
        timings = re.compile(r"^\d\d:\d\d:\d\d,\d{3} --> .*$", re.M)
        assert timings.findall(srt) == timings.findall(before)
        # The marks came back: each half lands in its own subtitle, nothing to review for it.
        assert "00:01:07,800\nFR xxxxxxxxx\n\n3\n00:01:07,800 --> 00:01:10,500\nxxxxxxxxxxx\n" in srt
        assert "c2-3" not in json.dumps(done.review, ensure_ascii=False)
        ass = outputs["特效字幕.ass"].read_text(encoding="utf-8")
        assert "{\\i1}" in ass and "{\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}" in ass

    def test_bilingual_switch(self) -> None:
        done, _engine = self._run({"bilingual": True})
        names = sorted(Path(result["output_path"]).name for result in done.file_results)
        assert all("_双语" in name for name in names)
        srt = Path(next(r["output_path"] for r in done.file_results if r["name"] == "培训视频.srt")).read_text(encoding="utf-8")
        assert "00:01:05,200 --> 00:01:07,800\n今天我们主要讲一下\nFR " in srt
