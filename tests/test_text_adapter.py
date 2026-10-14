"""The built-in 「文本」 type (``adapters/text``): .txt and Markdown, written back in place.

The samples in ``adapters/text/samples`` must come back byte for byte when
every translation equals its source; a translation must land exactly where
its source was, with markers, indentation, line endings and the byte-order
mark around it untouched; code, front matter, HTML and link addresses never
reach the model.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from api.surfaces import get_surface
from config import CORE_TRANSLATION_PROMPT
from api.surfaces.adapter import AdapterSurface, register_bundled_adapters
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskOptions
from core import tm_manager
from core.format_adapter import BUNDLED_ADAPTERS_DIR, AdapterError, InProcessAdapterHost, load_manifest
from core.task_runner_messages import DoneMsg
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_adapter_runner import FakeEngine, _drain, _item
from translator_adapter import MODE_BILINGUAL, MODE_TRANSLATION_ONLY, TextUnit, TranslatedUnit

TEXT_DIR = BUNDLED_ADAPTERS_DIR / "text"
SAMPLES = sorted((TEXT_DIR / "samples").iterdir())
HOST = InProcessAdapterHost(TEXT_DIR)
ROOT = Path(__file__).resolve().parents[1]


def _units(path: Path) -> list[TextUnit]:
    return HOST.extract(path)


def _write(path: Path, translate: Any, mode: str = MODE_TRANSLATION_ONLY) -> bytes:
    """Write ``path`` back with ``translate(unit)`` for every unit; return the bytes."""
    out = path.with_name(f"out_{mode}{path.suffix}")
    units = [TranslatedUnit(unit.id, unit.text, unit.text if unit.locked else translate(unit)) for unit in _units(path)]
    HOST.write(path, units, mode, out, {})
    return out.read_bytes()


def _text(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_bytes(body.encode("utf-8"))
    return path


def _upper(unit: TextUnit) -> str:
    return unit.text.upper() if unit.text.isascii() else f"T<{unit.text}>"


# ── manifest ────────────────────────────────────────────────────────────────


def test_manifest_describes_the_text_page() -> None:
    manifest = load_manifest(TEXT_DIR)
    assert manifest.key == "ext:text"
    assert manifest.label == {"zh": "文本", "en": "Text", "fr": "Texte"}
    assert manifest.extensions == ["txt", "md", "markdown"]
    assert manifest.capabilities.bilingual and manifest.capabilities.translation_only
    # The page's 「?」: what it handles and what stays untranslated.
    assert "代码块、链接地址不翻译" in manifest.description["zh"]
    assert set(manifest.description) == {"zh", "en", "fr"}
    icons = (ROOT / "ui" / "src" / "icons.ts").read_text(encoding="utf-8")
    assert f'"{manifest.icon}"' in icons


def test_shipped_with_the_app() -> None:
    register_bundled_adapters()
    surface = get_surface("ext:text")
    assert isinstance(surface, AdapterSurface)
    descriptor = surface.describe()
    assert sorted(descriptor["extensions"]) == ["markdown", "md", "txt"]
    assert [option["key"] for option in descriptor["options"]] == ["bilingual"]
    assert descriptor["options"][0]["default"] is False
    assert descriptor["description"]["en"].startswith("Translates plain text")
    spec = (ROOT / "packaging" / "sidecar" / "translator_sidecar.spec").read_text(encoding="utf-8")
    assert 'glob("*/adapter.*")' in spec and '"adapters"' in spec


# ── round trip ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", [MODE_TRANSLATION_ONLY, MODE_BILINGUAL])
@pytest.mark.parametrize("sample", SAMPLES, ids=lambda path: path.name)
def test_samples_come_back_byte_for_byte(tmp_path: Path, sample: Path, mode: str) -> None:
    copy = tmp_path / sample.name
    copy.write_bytes(sample.read_bytes())
    assert _write(copy, lambda unit: unit.text, mode) == sample.read_bytes()


def test_samples_cover_crlf_bom_code_and_tables() -> None:
    names = {path.name for path in SAMPLES}
    assert {"技术说明.md", "code_and_table.md", "windows_newlines.txt"} <= names
    raw = (TEXT_DIR / "samples" / "windows_newlines.txt").read_bytes()
    assert raw.startswith(codecs.BOM_UTF8) and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


# ── what is read ────────────────────────────────────────────────────────────


def test_markdown_units_of_the_chinese_sample() -> None:
    units = _units(TEXT_DIR / "samples" / "技术说明.md")
    by_kind: dict[str, list[TextUnit]] = {}
    for unit in units:
        by_kind.setdefault(unit.kind, []).append(unit)
    # Headings without their #, ATX and setext alike.
    assert [unit.text for unit in by_kind["heading"]] == [
        "设备监控服务部署说明",
        "一、准备工作",
        "二、安装步骤",
        "安装完成",
        "三、端口说明",
        "常见问题",
    ]
    # List items without their markers; a task box stays with the marker.
    items = [unit.text for unit in by_kind["list_item"]]
    assert items[:3] == ["准备一台 Linux 服务器（建议 4 核 8 GB 内存）。", "安装 Docker 20.10 或更高版本，执行 `docker --version` 确认。", "申请数据库账号："]
    assert "读写账号用于写入采集数据；" in items
    assert "已开通防火墙 8080 端口" in items
    # Code, front matter, HTML and table separator rows are locked.
    locked = {unit.kind for unit in units if unit.locked}
    assert locked == {"front_matter", "code", "html", "table_separator", "reference"}
    assert all(not unit.locked for unit in by_kind["table_cell"])
    assert all("tar -xzf" not in unit.text for unit in units if not unit.locked)
    # Inline code, links and addresses are protected.
    install = next(unit for unit in units if unit.text.startswith("安装脚本"))
    assert install.protected == ["`monitor`", "`config.yaml`", "`data_dir`"]
    browse = next(unit for unit in units if unit.text.startswith("服务启动后"))
    assert browse.protected == ["<http://localhost:8080>", "https://example.com/docs", "[^1]"]
    download = next(unit for unit in units if unit.text.startswith("下载安装包"))
    assert download.protected == ['(https://example.com/releases "发布页面")']
    footnote = next(unit for unit in units if unit.kind == "footnote")
    assert footnote.text == "文档每周更新一次。"
    # Where each unit is, for the review list.
    assert by_kind["heading"][0].context == "第 7 行"
    cell = next(unit for unit in units if unit.text == "用途")
    assert (cell.id, cell.context) == ("L49C2", "第 49 行 · 表格第 2 列")


def test_txt_is_split_at_blank_lines() -> None:
    units = _units(TEXT_DIR / "samples" / "windows_newlines.txt")
    assert [unit.text for unit in units] == [
        "设备巡检记录说明",
        "本记录用于每日设备巡检。巡检人员应在每班开始后一小时内完成，\n并将结果上传至 https://example.com/inspection 。",
        "注意：发现异常时，请立即电话通知值班主管。",
        "联系邮箱：maintenance@example.com\n最后更新：2026 年 10 月",
    ]
    assert units[1].protected == ["https://example.com/inspection"]
    assert HOST.scan(TEXT_DIR / "samples" / "windows_newlines.txt").unit_count == 4


def test_a_long_paragraph_is_cut_at_line_boundaries(tmp_path: Path) -> None:
    body = "".join(f"Line {index} of a very long paragraph that goes on and on.\n" for index in range(120))
    path = _text(tmp_path, "long.txt", body)
    units = _units(path)
    assert len(units) > 1
    assert all(len(unit.text) <= 1600 for unit in units)
    assert "\n".join(unit.text for unit in units) == body.rstrip("\n")
    assert _write(path, lambda unit: unit.text) == body.encode()


def test_empty_and_code_only_files_have_nothing_to_translate(tmp_path: Path) -> None:
    empty = _text(tmp_path, "empty.md", "")
    code = _text(tmp_path, "code.md", "```\nprint('hi')\n```\n")
    assert HOST.scan(empty).unit_count == 0
    scanned = HOST.scan(code)
    assert scanned.unit_count == 0 and scanned.warnings == ["没有找到需要翻译的文字。"]


# ── what is written ─────────────────────────────────────────────────────────

DOC = """# 标题

段落第一行
段落第二行

- 项目一
- [ ] 待办

> 引用的话

```
不要翻译
```

| 名称 | 说明 |
|------|------|
| 甲 | 第一项 |
"""


def test_translations_land_where_their_source_was(tmp_path: Path) -> None:
    path = _text(tmp_path, "doc.md", DOC)
    out = _write(path, lambda unit: f"T<{unit.text}>").decode("utf-8")
    assert out == """# T<标题>

T<段落第一行
段落第二行>

- T<项目一>
- [ ] T<待办>

> T<引用的话>

```
不要翻译
```

| T<名称> | T<说明> |
|------|------|
| T<甲> | T<第一项> |
"""


def test_bilingual_puts_each_translation_after_its_source(tmp_path: Path) -> None:
    path = _text(tmp_path, "doc.md", DOC)
    out = _write(path, lambda unit: f"T<{unit.text}>", MODE_BILINGUAL).decode("utf-8")
    assert out == """# 标题 / T<标题>

段落第一行
段落第二行

T<段落第一行
段落第二行>

- 项目一

  T<项目一>
- [ ] 待办

  T<待办>

> 引用的话
>
> T<引用的话>

```
不要翻译
```

| 名称 / T<名称> | 说明 / T<说明> |
|------|------|
| 甲 / T<甲> | 第一项 / T<第一项> |
"""


def test_a_translation_with_more_or_fewer_lines_keeps_the_block(tmp_path: Path) -> None:
    path = _text(tmp_path, "list.md", "1. 第一行\n   第二行\n\n# 标题\n")
    translations = {"第一行\n第二行": "One\nTwo\nThree", "标题": "Title\nsplit"}
    out = _write(path, lambda unit: translations[unit.text]).decode("utf-8")
    # Extra lines get the item's indentation; a heading stays on one line.
    assert out == "1. One\n   Two\n   Three\n\n# Title split\n"


def test_crlf_and_bom_are_kept_on_added_lines(tmp_path: Path) -> None:
    path = tmp_path / "win.txt"
    path.write_bytes(codecs.BOM_UTF8 + "  第一段\r\n\r\n第二段\r\n".encode("utf-8"))
    out = _write(path, lambda unit: "One\nTwo" if unit.text == "第一段" else "Three")
    assert out == codecs.BOM_UTF8 + b"  One\r\n  Two\r\n\r\nThree\r\n"
    bilingual = _write(path, lambda unit: "One" if unit.text == "第一段" else "Three", MODE_BILINGUAL)
    assert bilingual == codecs.BOM_UTF8 + "  第一段\r\n\r\n  One\r\n\r\n第二段\r\n\r\nThree\r\n".encode("utf-8")


def test_table_pipes_are_escaped_and_link_gaps_closed(tmp_path: Path) -> None:
    path = _text(tmp_path, "t.md", "| 甲 |\n|---|\n| 见[文档](https://x.example/a) |\n")
    translations = {"甲": "A | B", "见[文档](https://x.example/a)": "See [docs] (https://x.example/a)"}
    out = _write(path, lambda unit: translations[unit.text]).decode("utf-8")
    assert out == "| A \\| B |\n|---|\n| See [docs](https://x.example/a) |\n"


def test_a_unit_whose_source_changed_keeps_the_file_text(tmp_path: Path) -> None:
    path = _text(tmp_path, "a.md", "旧的\n")
    out = tmp_path / "out.md"
    HOST.write(path, [TranslatedUnit("L1", "别的", "Other")], MODE_TRANSLATION_ONLY, out, {})
    assert out.read_text(encoding="utf-8") == "旧的\n"


def test_gb18030_files_are_read_and_written_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "gbk.txt"
    path.write_bytes("设备清单\r\n".encode("gb18030"))
    assert [unit.text for unit in _units(path)] == ["设备清单"]
    assert _write(path, lambda unit: "Liste des équipements") == codecs.BOM_UTF8 + "Liste des équipements\r\n".encode("utf-8")


def test_an_unknown_encoding_is_refused_with_a_sentence(tmp_path: Path) -> None:
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\x80\xff\x00 data")
    with pytest.raises(AdapterError, match="无法识别文件编码"):
        HOST.extract(path)


# ── through the shared executor ─────────────────────────────────────────────


_NON_ASCII = re.compile(r"[^\x00-\x7f⟦⟧]")


class _AsciiEngine(FakeEngine):
    """Translates line by line into ASCII so the residual check stays quiet."""

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append(list(texts))
        self.prompts.append(system_prompt)
        return {text: "\n".join("FR " + _NON_ASCII.sub("x", line) for line in text.split("\n")) for text in texts}


class TextTypeRunTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "docs"
        self.source.mkdir(parents=True)
        for sample in SAMPLES:
            (self.source / sample.name).write_bytes(sample.read_bytes())
        self.surface = AdapterSurface(load_manifest(TEXT_DIR), HOST)

    def _run(self, options: dict[str, bool], *, prompt: str = "") -> tuple[DoneMsg, _AsciiEngine]:
        settings = AppSettings(target_lang="fr")
        settings.extensions["text"] = ExtensionSettings(smart_context=False, prompt=prompt)
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

    def test_samples_translate_with_structure_in_place(self) -> None:
        done, engine = self._run({})
        assert done.review == {"total_count": 0}
        assert [result["status"] for result in done.file_results] == ["completed"] * len(SAMPLES)
        sent = "\n".join(engine.sent)
        # Nothing locked or protected reaches the model.
        for secret in ("tar -xzf", "data_dir", "https://example.com/releases", "title: 设备", "HTML 块", "PYTHONHASHSEED"):
            assert secret not in sent
        outputs = {Path(result["output_path"]).name: Path(result["output_path"]) for result in done.file_results}
        assert set(outputs) == {"技术说明_法文.md", "code_and_table_法文.md", "windows_newlines_法文.txt"}

        source = (self.source / "技术说明.md").read_text(encoding="utf-8").split("\n")
        output = outputs["技术说明_法文.md"].read_text(encoding="utf-8").split("\n")
        # The engine keeps the line count, so every line can be compared in place.
        assert len(output) == len(source)
        for before, after in zip(source, output):
            heading = re.match(r"#{1,6} ", before)
            item = re.match(r"\s*(?:[-*+]|\d+\.) (?:\[[ x]\] )?", before)
            if heading:
                assert after.startswith(heading.group(0) + "FR ")
            elif item:
                assert after.startswith(item.group(0) + "FR ")
            elif before.startswith(("```", "    ", "|:", "<div", "</div>", "title:", "[docs]:")):
                assert after == before
        assert "| FR xx | FR xx | FR xxxx |" in output
        assert "    systemctl start monitor" in output
        assert "安装脚本会自动创建" not in "\n".join(output)
        assert re.search(r"FR x+ `monitor` x+", "\n".join(output))

        windows = outputs["windows_newlines_法文.txt"].read_bytes()
        assert windows.startswith(codecs.BOM_UTF8) and windows.count(b"\r\n") == windows.count(b"\n")

    def test_bilingual_switch(self) -> None:
        done, _engine = self._run({"bilingual": True})
        names = sorted(Path(result["output_path"]).name for result in done.file_results)
        assert names == ["code_and_table_法文_双语.md", "windows_newlines_法文_双语.txt", "技术说明_法文_双语.md"]
        output = Path(next(r["output_path"] for r in done.file_results if r["name"] == "技术说明.md")).read_text(encoding="utf-8")
        assert "## 一、准备工作 / FR xxxxxx\n" in output
        assert "本文说明如何在一台新的服务器上部署设备监控服务，\n并完成首次配置。" in output

    def test_edited_core_rules_lead_every_request(self) -> None:
        _done, engine = self._run({})
        assert engine.prompts and all(prompt.startswith(CORE_TRANSLATION_PROMPT) for prompt in engine.prompts)

        _done, engine = self._run({}, prompt="只译正文，保留 Markdown 标记。")
        assert engine.prompts
        for prompt in engine.prompts:
            assert prompt.startswith("只译正文，保留 Markdown 标记。")
            assert CORE_TRANSLATION_PROMPT not in prompt
