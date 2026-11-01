"""Word：内容控件里的文字照常翻译；未接受的修订在扫描期数出来、开始前由用户选处理方式。

V10.5 之前，``w:sdt``（内容控件）和 ``w:ins`` / ``w:del``（未接受的修订）里的文字
python-docx 都看不见：它们既不进待译清单，只在任务跑起来之后报一句「存在无法读取的
内容」。这一版：

- 内容控件的段落当成普通正文段落，随正文一起翻，用户没有选择；
- 修订在扫描时数进 ``tracked_change_count``，开始翻译时弹窗让用户选 ``skip``
  （默认，保持原文）或 ``translate``（连修订一起翻，作者/日期/id 不变）。

这里造的是真 .docx：python-docx 建骨架，lxml 塞 ``w:sdt`` / ``w:ins`` / ``w:del``。
"""

from __future__ import annotations

# 本仓库的开发副本里 core/_embedded_key.py 只有 EMBEDDED_KEY_ID，缺发布期注入的
# EMBEDDED_PRIVATE_KEY_B64：api.app 在这份副本上根本导不进来，整份 API 测试会
# 收在 collection 阶段。导入时就用开发兜底密钥补上，接口契约才测得到。
import base64 as _base64
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.task_runner import DoneMsg
from core.word_coverage import build_word_coverage_plan, write_untranslated_docx
from core.word_document import (
    detect_hidden_word_content,
    extract_word_segments,
    extract_word_story_segments,
    scan_word_sources,
    write_bilingual_docx,
)
from core.word_revisions import (
    REVISION_MODE_SKIP,
    REVISION_MODE_TRANSLATE,
    RevisionWriteStats,
    count_tracked_changes,
    normalize_revision_mode,
)
from core.word_document import WordFileItem
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from tests import test_word_resume as word_helpers
from tests.app_data_isolation import IsolatedAppDataTestCase

try:  # pragma: no cover - 只有缺少发布期注入的副本才走这里
    import core._embedded_key as _embedded_key

    if not hasattr(_embedded_key, "EMBEDDED_PRIVATE_KEY_B64"):
        from cryptography.hazmat.primitives import serialization as _serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        _embedded_key.EMBEDDED_PRIVATE_KEY_B64 = _base64.b64encode(
            X25519PrivateKey.generate().private_bytes(
                encoding=_serialization.Encoding.Raw,
                format=_serialization.PrivateFormat.Raw,
                encryption_algorithm=_serialization.NoEncryption(),
            )
        ).decode()
except Exception:  # noqa: BLE001 - 拿不到就按原样跑，相关用例会自己跳过
    pass

from fastapi.testclient import TestClient

CONTROL_TEXT = "内容控件里的字"
INSERTED_TEXT = "插入的一句话"
DELETED_TEXT = "删掉的一句话"
BODY_TEXT = "正文原文"
TRANSLATIONS = {
    BODY_TEXT: "Body text",
    CONTROL_TEXT: "Text inside the control",
    INSERTED_TEXT: "An inserted sentence",
    DELETED_TEXT: "A deleted sentence",
}


def _append_xml(doc: Document, xml: str) -> None:
    """插到 sectPr 之前——Word 的正文块都在那里，body 尾部只能有 sectPr。"""
    doc.element.body.find(qn("w:sectPr")).addprevious(parse_xml(xml))


def _content_control_xml(text: str, *, toc: bool = False) -> str:
    marker = (
        "<w:sdtPr><w:docPartObj>"
        '<w:docPartGallery w:val="Table of Contents"/>'
        "</w:docPartObj></w:sdtPr>"
        if toc
        else "<w:sdtPr/>"
    )
    body = f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
    if toc:
        body += (
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve">TOC \\o "1-3" \\h \\u </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>一、变更概况</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        )
    return f'<w:sdt {nsdecls("w")}>{marker}<w:sdtContent>{body}</w:sdtContent></w:sdt>'


def _revision_xml(*, inserted: str, deleted: str) -> str:
    return (
        f'<w:p {nsdecls("w")}><w:r><w:t xml:space="preserve">{BODY_TEXT} </w:t></w:r>'
        f'<w:ins w:id="11" w:author="张三" w:date="2026-01-02T03:04:05Z">'
        f"<w:r><w:t>{inserted}</w:t></w:r></w:ins>"
        f'<w:del w:id="12" w:author="张三" w:date="2026-01-02T03:04:05Z">'
        f"<w:r><w:delText>{deleted}</w:delText></w:r></w:del></w:p>"
    )


def build_document(
    path: Path,
    *,
    control: str | None = None,
    control_is_toc: bool = False,
    inserted: str | None = None,
    deleted: str | None = None,
    body: bool = True,
) -> None:
    doc = Document()
    if body:
        doc.add_paragraph(BODY_TEXT)
    if control:
        _append_xml(doc, _content_control_xml(control, toc=control_is_toc))
    if inserted or deleted:
        _append_xml(doc, _revision_xml(inserted=inserted or "", deleted=deleted or ""))
    doc.save(path)


def _document_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _control_texts(path: Path) -> list[str]:
    """每个内容控件里各段的文字（按文档顺序）。"""
    import re

    xml = _document_xml(path)
    controls = re.findall(r"<w:sdt>.*?</w:sdt>", xml, re.S)
    return [
        [
            "".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", paragraph))
            for paragraph in re.findall(r"<w:p[ >].*?</w:p>", control, re.S)
        ]
        for control in controls
    ]


class ContentControlTests(unittest.TestCase):
    """A. 内容控件里的文字永远翻译，不由用户选择。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_control_paragraphs_are_extracted_like_body_paragraphs(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        segments = extract_word_segments(source, target_lang="en")

        by_source = {segment.source: segment for segment in segments}
        self.assertIn(CONTROL_TEXT, by_source)
        self.assertEqual(by_source[CONTROL_TEXT].kind, "content_control")
        self.assertEqual(
            by_source[CONTROL_TEXT].location, "content_control[0].paragraph[0]"
        )

    def test_control_text_is_translated_in_bilingual_output(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "out",
            translations=TRANSLATIONS,
            target_lang="en",
        )

        self.assertEqual(_control_texts(output), [[CONTROL_TEXT, TRANSLATIONS[CONTROL_TEXT]]])
        # 控件本身还在：用户仍然在同一个表单格子里，内容控件没有被拆掉。
        self.assertIn("<w:sdt>", _document_xml(output))
        Document(str(output))  # 输出还能被 Word 打开

    def test_control_text_replaces_in_translation_only_mode(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "only",
            translations=TRANSLATIONS,
            target_lang="en",
            output_translation_only=True,
        )

        self.assertEqual(_control_texts(output), [[TRANSLATIONS[CONTROL_TEXT]]])

    def test_toc_control_is_still_left_untouched(self) -> None:
        source = self.root / "toc.docx"
        build_document(source, control="目录", control_is_toc=True)

        segments = extract_word_segments(source, target_lang="en")
        self.assertNotIn("目录", [segment.source for segment in segments])

        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "toc-out",
            translations={"目录": "Contents", BODY_TEXT: TRANSLATIONS[BODY_TEXT]},
            target_lang="en",
        )
        self.assertIn("<w:instrText", _document_xml(output))
        # 目录控件里的每段文字都没动过（gallery 属性值里也有 "Contents"，不能按整份 XML 判）。
        self.assertEqual(
            [text.strip() for text in _control_texts(output)[0]],
            ["目录", "一、变更概况"],
        )

    def test_control_text_does_not_trigger_the_hidden_content_warning(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        report = detect_hidden_word_content(source)

        self.assertEqual(report.content_control_count, 1)
        self.assertFalse(report.found)
        self.assertEqual(report.describe(), "")

    def test_untreated_only_fills_in_control_paragraphs(self) -> None:
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        plan = build_word_coverage_plan(source, target_lang="en")
        self.assertIn(
            ("content_control", CONTROL_TEXT),
            [(unit.kind, unit.source_text) for unit in plan.source_units],
        )

        output = write_untranslated_docx(
            source_path=source,
            output_dir=self.root / "fill",
            plan=plan,
            translations=TRANSLATIONS,
            target_lang="en",
        )
        self.assertEqual(_control_texts(output)[0][-1], TRANSLATIONS[CONTROL_TEXT])

    def test_control_paragraphs_are_not_part_of_story_segments(self) -> None:
        """文本框/脚注那一路不能把控件段落再抽一遍，否则同一句会被翻两次。"""
        source = self.root / "control.docx"
        build_document(source, control=CONTROL_TEXT)

        self.assertEqual(extract_word_story_segments(source, target_lang="en"), [])


class TrackedChangeScanTests(unittest.TestCase):
    """B. 扫描期就把「有几处未接受的修订」数出来。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_scan_counts_insertions_and_deletions(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        result = scan_word_sources(self.root)

        item = next(item for item in result.items if item.path == source)
        self.assertEqual(item.tracked_change_count, 2)
        self.assertEqual(result.summary["tracked_change_count"], 2)
        self.assertEqual(result.summary["tracked_change_file_count"], 1)

    def test_file_without_revisions_reports_zero(self) -> None:
        source = self.root / "plain.docx"
        build_document(source, body=True)

        result = scan_word_sources(self.root)

        item = next(item for item in result.items if item.path == source)
        self.assertEqual(item.tracked_change_count, 0)
        self.assertEqual(result.summary["tracked_change_file_count"], 0)

    def test_paragraph_mark_insertions_do_not_count(self) -> None:
        """``w:pPr/w:rPr/w:ins`` 只标记段落标记本身，没有文字，不算一处修订。"""
        source = self.root / "mark.docx"
        doc = Document()
        doc.add_paragraph("Ordinary paragraph.")
        doc.paragraphs[0]._p.get_or_add_pPr().append(
            parse_xml(
                f'<w:rPr {nsdecls("w")}>'
                '<w:ins w:id="7" w:author="tester" w:date="2026-01-01T00:00:00Z"/>'
                "</w:rPr>"
            )
        )
        doc.save(source)

        result = scan_word_sources(self.root)
        self.assertEqual(result.items[0].tracked_change_count, 0)

    def test_revisions_inside_a_toc_control_do_not_count(self) -> None:
        source = self.root / "toc.docx"
        doc = Document()
        doc.add_paragraph(BODY_TEXT)
        _append_xml(
            doc,
            f'<w:sdt {nsdecls("w")}><w:sdtPr><w:docPartObj>'
            '<w:docPartGallery w:val="Table of Contents"/>'
            "</w:docPartObj></w:sdtPr><w:sdtContent>"
            "<w:p><w:r><w:t>目录</w:t></w:r></w:p>"
            f'<w:p><w:ins w:id="21" w:author="tester" w:date="2026-01-01T00:00:00Z">'
            "<w:r><w:t>目录条目</w:t></w:r></w:ins></w:p>"
            "</w:sdtContent></w:sdt>",
        )
        doc.save(source)

        result = scan_word_sources(self.root)
        self.assertEqual(result.items[0].tracked_change_count, 0)
        self.assertEqual(count_tracked_changes(Document(str(source)).element.body), 1)


class RevisionModeTests(unittest.TestCase):
    """C/D. skip 保原文、translate 换文字但保留修订标记。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, source: Path, *, mode: str, name: str) -> Path:
        return write_bilingual_docx(
            source_path=source,
            output_dir=self.root / name,
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode=mode,
            output_translation_only=True,
        )

    def test_default_mode_is_skip(self) -> None:
        self.assertEqual(normalize_revision_mode(None), REVISION_MODE_SKIP)
        self.assertEqual(normalize_revision_mode("bogus"), REVISION_MODE_SKIP)
        self.assertEqual(normalize_revision_mode("TRANSLATE"), REVISION_MODE_TRANSLATE)
        stats = RevisionWriteStats()
        self.assertEqual(stats.describe(), "跳过了 0 处修订（保持原文）")

    def test_skip_leaves_revision_text_untouched(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        output = self._write(source, mode="skip", name="skip")
        xml = _document_xml(output)

        self.assertIn(INSERTED_TEXT, xml)
        self.assertIn(DELETED_TEXT, xml)
        self.assertNotIn(TRANSLATIONS[INSERTED_TEXT], xml)
        self.assertNotIn(TRANSLATIONS[DELETED_TEXT], xml)
        self.assertIn(TRANSLATIONS[BODY_TEXT], xml)

    def test_translate_replaces_revision_text_and_keeps_the_marks(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        seen: list[RevisionWriteStats] = []
        output = write_bilingual_docx(
            source_path=source,
            output_dir=self.root / "translate",
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
            output_translation_only=True,
            revision_callback=seen.append,
        )
        xml = _document_xml(output)

        self.assertNotIn(INSERTED_TEXT, xml)
        self.assertNotIn(DELETED_TEXT, xml)
        self.assertIn(TRANSLATIONS[INSERTED_TEXT], xml)
        self.assertIn(TRANSLATIONS[DELETED_TEXT], xml)
        # 修订元素和它们的作者/日期/id 一个都不能少：对方打开译文还得看得出改了哪。
        self.assertIn('<w:ins w:id="11" w:author="张三"', xml)
        self.assertIn('<w:del w:id="12" w:author="张三"', xml)
        # 删除的还是删除：译文写在 w:delText 里，不能被写成正文 w:t。
        self.assertRegex(xml, r'<w:del[^>]*>.*?<w:delText[^>]*>A deleted sentence</w:delText>')
        self.assertEqual(seen[0].mode, "translate")
        self.assertEqual(seen[0].translated_count, 2)
        self.assertEqual(seen[0].describe(), "翻译了 2 处修订（保留修订标记）")
        Document(str(output))

    def test_translate_mode_only_extracts_revisions_in_translate_mode(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        skip_sources = [
            segment.source for segment in extract_word_segments(source, target_lang="en")
        ]
        translated = [
            segment.source
            for segment in extract_word_segments(
                source, target_lang="en", revision_mode="translate"
            )
        ]

        self.assertNotIn(INSERTED_TEXT, skip_sources)
        self.assertNotIn(DELETED_TEXT, skip_sources)
        self.assertIn(INSERTED_TEXT, translated)
        self.assertIn(DELETED_TEXT, translated)
        self.assertTrue(
            any(
                segment.kind == "tracked_change"
                for segment in extract_word_segments(
                    source, target_lang="en", revision_mode="translate"
                )
            )
        )

    def test_both_control_and_revisions_together(self) -> None:
        source = self.root / "both.docx"
        build_document(source, control=CONTROL_TEXT, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

        output = self._write(source, mode="translate", name="both")
        xml = _document_xml(output)

        self.assertEqual(
            _control_texts(output), [[TRANSLATIONS[CONTROL_TEXT]]]
        )
        self.assertIn(TRANSLATIONS[INSERTED_TEXT], xml)
        self.assertIn(TRANSLATIONS[DELETED_TEXT], xml)
        Document(str(output))

    def test_translate_mode_also_works_for_fill_in_tasks(self) -> None:
        source = self.root / "revisions.docx"
        build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)
        plan = build_word_coverage_plan(source, target_lang="en")

        output = write_untranslated_docx(
            source_path=source,
            output_dir=self.root / "fill",
            plan=plan,
            translations=TRANSLATIONS,
            target_lang="en",
            revision_mode="translate",
        )
        xml = _document_xml(output)

        self.assertIn(TRANSLATIONS[INSERTED_TEXT], xml)
        self.assertIn(TRANSLATIONS[DELETED_TEXT], xml)
        self.assertIn('<w:ins w:id="11"', xml)
        self.assertIn('<w:del w:id="12"', xml)


class RunnerRevisionRecordTests(IsolatedAppDataTestCase):
    """D + 状态⑤：任务记录里写明这份文件对修订是怎么处理的。"""

    def _run(self, source: Path, root: Path, *, revision_mode: str) -> DoneMsg:
        runner = WordTaskRunner(
            [WordFileItem(path=source, name=source.name, size_kb=1.0)],
            AppSettings(source_lang="zh", target_lang="en"),
            source_root=root,
            revision_mode=revision_mode,
        )
        with ExitStack() as stack:
            word_helpers.WordResumeTests._runner_patches(self, stack, root=root)
            runner._run()
        return next(
            message for message in runner._queue.queue if isinstance(message, DoneMsg)
        )

    def test_skip_is_recorded_on_the_file_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "revisions.docx"
            build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

            done = self._run(source, root, revision_mode="skip")

            result = done.file_results[0]
            self.assertTrue(result["success"], result)
            self.assertEqual(result["revisions"]["mode"], "skip")
            self.assertEqual(result["revisions"]["count"], 2)
            self.assertEqual(result["revisions"]["label"], "跳过了 2 处修订（保持原文）")

    def test_translate_is_recorded_on_the_file_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "revisions.docx"
            build_document(source, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)

            done = self._run(source, root, revision_mode="translate")

            result = done.file_results[0]
            self.assertTrue(result["success"], result)
            self.assertEqual(result["revisions"]["mode"], "translate")
            self.assertEqual(result["revisions"]["count"], 2)
            self.assertEqual(
                result["revisions"]["label"], "翻译了 2 处修订（保留修订标记）"
            )
            # 这里走的是记忆库替身（每句都给 "TM translation"）：验证的是修订里的字
            # 换成了译文、修订标记还在，具体译法由别的用例覆盖。
            xml = _document_xml(Path(result["output"]))
            self.assertIn('<w:ins w:id="11" w:author="张三"', xml)
            self.assertIn('<w:del w:id="12" w:author="张三"', xml)
            self.assertNotIn(INSERTED_TEXT, xml)
            self.assertNotIn(DELETED_TEXT, xml)


if __name__ == "__main__":
    unittest.main(verbosity=2)


def _app_factory():
    """按需导入 FastAPI 应用。

    仓库里的开发版 ``core/_embedded_key.py`` 只有 ``EMBEDDED_KEY_ID``，发布期才会由
    scripts/inject_embedded_key.py 补上 ``EMBEDDED_PRIVATE_KEY_B64``；缺这个常量时
    ``api.app`` 根本导不进来，整个 API 层都测不了。那种环境里跳过，而不是让这一组
    契约测试在开发副本上永远红着。
    """
    try:
        from api.app import create_app
    except ImportError as exc:  # pragma: no cover - 只发生在缺少发布期注入的开发副本
        raise unittest.SkipTest(f"api.app 在当前副本里导入不了：{exc}")
    return create_app


class RevisionModeApiTests(IsolatedAppDataTestCase):
    """API 契约：扫描结果带 tracked_change_count，启动载荷带可选的 revision_mode。"""

    def test_scan_reports_tracked_change_count_per_file_and_in_summary(self) -> None:
        create_app = _app_factory()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with_revisions = root / "revisions.docx"
            plain = root / "plain.docx"
            build_document(with_revisions, inserted=INSERTED_TEXT, deleted=DELETED_TEXT)
            build_document(plain)

            with TestClient(create_app()) as client:
                response = client.post(
                    "/api/sources/scan", json={"surface": "word", "path": str(root)}
                )

            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            by_name = {item["name"]: item for item in payload["items"]}
            self.assertEqual(by_name["revisions"]["tracked_change_count"], 2)
            self.assertEqual(by_name["plain"]["tracked_change_count"], 0)
            self.assertEqual(payload["summary"]["tracked_change_file_count"], 1)
            self.assertEqual(payload["summary"]["tracked_change_count"], 2)

    def test_start_payload_carries_revision_mode_and_defaults_to_skip(self) -> None:
        from api.task_manager import TaskOptions
        from api.schemas import TaskStartRequest

        # 请求字段 → TaskOptions 的那一步是纯数据搬运（api/routes/tasks.py 的
        # _task_options 按同名字段取值），这里直接钉住两端的契约，不启动真任务。
        explicit = TaskStartRequest(
            surface="word", source_path="/tmp/x.docx", revision_mode="translate"
        )
        self.assertEqual(explicit.revision_mode, "translate")
        self.assertEqual(TaskOptions(revision_mode=explicit.revision_mode).revision_mode, "translate")

        default = TaskStartRequest(surface="word", source_path="/tmp/x.docx")
        self.assertEqual(default.revision_mode, "skip")
        self.assertNotIn("revision_mode", default.model_fields_set)
        self.assertEqual(TaskOptions().revision_mode, "skip")

    def test_surface_forwards_revision_mode_to_the_runner(self) -> None:
        from api.surfaces import get_surface
        from api.surfaces.base import RunnerRequest
        from api.task_contracts import TaskOptions

        WordSurface = type(get_surface("word"))
        from settings import AppSettings

        captured: dict[str, object] = {}

        class _Recorder:
            def __init__(self, *args, **kwargs):  # noqa: ARG002 - 只记 keyword
                captured.update(kwargs)

        with patch("api.surfaces.word.WordTaskRunner", _Recorder):
            surface = WordSurface()
            surface.build_runner(
                RunnerRequest(
                    files=[],
                    settings=AppSettings(),
                    source_root=Path("/tmp"),
                    source_lang="zh",
                    key_overrides={},
                    api_schedulers={},
                    options=TaskOptions(revision_mode="translate"),
                )
            )

        self.assertEqual(captured["revision_mode"], "translate")

    def test_unknown_revision_mode_is_rejected(self) -> None:
        """取值只认 skip / translate：拼错的值不该被悄悄当成默认值。"""
        from api.schemas import TaskStartRequest
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TaskStartRequest(
                surface="word", source_path="/tmp/x.docx", revision_mode="delete"
            )
