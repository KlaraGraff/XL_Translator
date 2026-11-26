"""每一条都因为请求失败没拿到译文：任务（这张图纸）算失败，不再说「已生成」（好友反馈 2026-11-25）。

好友用 DeepSeek 翻图纸，53 条请求全部报错，日志却写「已生成翻译图纸」，输出其实是原图。
这里的假服务器回一个解不开的 gzip 正文（与 zstd 解不开同一种 DecodingError），
真引擎（OpenAIEngine → text_transport）→ 真批次调度 → CAD / Excel 任务全链路照常跑。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from openpyxl import load_workbook

import core.process_artifacts as process_artifacts
from core import engine_dispatcher
from core.cad_task_runner import CadTaskRunner
from core.cad_translation import CadPipelineOptions
from core.file_scanner import FileItem
from core.nothing_translated import NothingTranslatedError, raise_if_nothing_translated
from core.task_runner import ErrorMsg, TaskRunner
from engines.openai_engine import OpenAIEngine
from tests.test_excel_resume import _make_xlsx, _pipeline_patches, _run_and_get_done, _settings

_MESSAGE_START = "条内容的翻译请求全部失败，没有获得任何译文"


class _BrokenBody(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b"\x1f\x8b\x08\x00not really gzip"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _broken_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokenBody)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def broken_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(process_artifacts, "APP_DATA_DIR", tmp_path / "app-data")
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    with _broken_server() as base:
        yield OpenAIEngine(api_key="sk-test", model="deepseek-chat", base_url=base, api_mode="chat")


def _cad_translator(engine, *, errors: list[str]):
    """与 api/surfaces/cad.py 相同：真批次调度，请求全部失败时抛 NothingTranslatedError。"""

    def translate(texts, glossary, error_callback=None):
        stats = engine_dispatcher.TranslationBatchRunStats()
        results = engine_dispatcher.translate_texts(
            list(texts), engine, "en", "翻译", 20, 2,
            error_callback=lambda message: errors.append(message),
            stats=stats, transient_retries=0, review=False,
        )
        raise_if_nothing_translated(texts, stats)
        return {text: value for text, value in results.items() if text not in stats.untranslated_sources}

    return translate


def _run(runner: CadTaskRunner) -> list[object]:
    runner.start()
    messages: list[object] = []
    while runner.needs_poll():
        message = runner.get_message(timeout=1)
        if message is not None:
            messages.append(message)
    return messages


def _drawing(path: Path, *texts: str) -> Path:
    path.write_text("".join(f"0\nTEXT\n1\n{text}\n" for text in texts), encoding="utf-8")
    return path


def test_cad_drawing_fails_when_every_request_fails(tmp_path, broken_engine):
    source = _drawing(tmp_path / "plan.dxf", "门", "窗", "楼梯")
    errors: list[str] = []
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        translator=_cad_translator(broken_engine, errors=errors),
        options=CadPipelineOptions(source_lang="zh", target_lang="en"),
    )
    messages = _run(runner)
    done = next(message for message in messages if hasattr(message, "file_results"))
    result = done.file_results[0]
    assert errors, "每一批失败都写进运行日志"
    assert result["status"] == "failed"
    assert result["message"].startswith("3 " + _MESSAGE_START)
    assert "模型服务返回了错误" in result["message"]
    assert not list((tmp_path / "results").rglob("*.dxf"))
    logs = [getattr(message, "message", "") for message in messages]
    assert not any("已生成翻译图纸" in str(line) for line in logs)


def test_cad_drawing_with_memory_hits_still_writes_them(tmp_path, broken_engine):
    source = _drawing(tmp_path / "plan.dxf", "门", "窗")
    runner = CadTaskRunner(
        files=[SimpleNamespace(path=source, name=source.name, format="dxf")],
        source_root=tmp_path,
        output_dir=tmp_path / "results",
        memory_lookup=lambda texts, *args, **kwargs: {"门": "Door"} if "门" in texts else {},
        translator=_cad_translator(broken_engine, errors=[]),
        options=CadPipelineOptions(source_lang="zh", target_lang="en"),
    )
    done = next(message for message in _run(runner) if hasattr(message, "file_results"))
    result = done.file_results[0]
    # 记忆库给了一条：照旧写出已有译文，没翻到的标需复核（与以前相同）。
    assert result["status"] == "needs_review", result
    assert "Door" in Path(result["output_path"]).read_text(encoding="utf-8")


def test_excel_task_fails_when_every_request_fails(tmp_path, broken_engine):
    def real_dispatch(texts, _engine, *args, **kwargs):
        kwargs["transient_retries"] = 0
        return engine_dispatcher.translate_texts(texts, broken_engine, *args, **kwargs)

    proj = tmp_path / "proj"
    source = _make_xlsx(proj / "a.xlsx", {"A1": "你好", "A2": "再见"})
    with _pipeline_patches(translate_side_effect=real_dispatch):
        runner = TaskRunner([FileItem(path=source, name="a", size_kb=1.0)], _settings(), source_root=proj)
        runner._run()
    errors = [m for m in list(runner._queue.queue) if isinstance(m, ErrorMsg)]
    assert len(errors) == 1
    assert _MESSAGE_START in errors[0].message
    assert not list(proj.rglob("*_双语.xlsx"))


def test_excel_task_still_finishes_when_requests_work(tmp_path):
    def working(texts, *args, **kwargs):
        return {text: f"T:{text}" for text in texts}

    proj = tmp_path / "proj"
    source = _make_xlsx(proj / "a.xlsx", {"A1": "你好"})
    with _pipeline_patches(translate_side_effect=working):
        runner = TaskRunner([FileItem(path=source, name="a", size_kb=1.0)], _settings(), source_root=proj)
        done = _run_and_get_done(runner)
    wb = load_workbook(done.file_results[0]["output_path"])
    try:
        assert "T:你好" in wb.active["A1"].value
    finally:
        wb.close()


def test_helper_rules():
    stats = engine_dispatcher.TranslationBatchRunStats()
    stats.record_untranslated(["a", "b"], "cannot use a decompressobj multiple times")
    with pytest.raises(NothingTranslatedError) as caught:
        raise_if_nothing_translated(["a", "b", "a"], stats)
    assert str(caught.value) == (
        "2 条内容的翻译请求全部失败，没有获得任何译文：模型服务返回了错误。"
        "未生成仅含原文的文件。请稍后重试，或在「设置 → 模型服务」中检查此连接。"
    )
    # 有一条拿到了译文、用户按了停止、或根本没有要翻的：都不算。
    raise_if_nothing_translated(["a", "b", "c"], stats)
    raise_if_nothing_translated(["a", "b"], stats, should_stop=lambda: True)
    raise_if_nothing_translated([], stats)
    stopped = engine_dispatcher.TranslationBatchRunStats()
    stopped.record_untranslated(["a"])
    stopped.record_stopped(["b"])
    raise_if_nothing_translated(["a", "b"], stopped)


def test_helper_is_task_fatal_like_a_rejected_key():
    from core.api_concurrency_control import ApiCredentialRejectedError

    assert issubclass(NothingTranslatedError, ApiCredentialRejectedError)


@patch.dict("os.environ", {})
def test_text_service_flag_is_off_by_default():
    import inspect

    from core.text_translation_service import TextTranslationService

    for name in ("translate", "translate_with_sources"):
        parameter = inspect.signature(getattr(TextTranslationService, name)).parameters["fail_if_nothing_translated"]
        assert parameter.default is False


# ── 分几轮发请求的格式：Word（含 PDF 转 Word）、扩展格式（PPT 等）、PDF 原位替换 ──


def _word_run(tmp_path, engine, paragraphs):
    from contextlib import ExitStack

    from docx import Document

    from core.word_document import WordFileItem
    from core.word_task_runner import WordTaskRunner
    from settings import AppSettings
    from tests.test_word_resume import WordResumeTests

    path = tmp_path / "plan.docx"
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(path)
    settings = AppSettings(source_lang="zh", target_lang="en")
    settings.rate.word.request_retry = 0
    runner = WordTaskRunner([WordFileItem(path=path, name=path.name, size_kb=1.0)], settings,
                            source_root=tmp_path, source_lang="zh")
    with ExitStack() as stack:
        WordResumeTests._runner_patches(None, stack, root=tmp_path)
        stack.enter_context(patch("core.word_task_runner.build_engine", return_value=engine))
        stack.enter_context(patch("core.word_task_runner.tm_manager.lookup_batch", return_value={}))
        runner._run()
    return list(runner._queue.queue)


def test_word_task_fails_when_every_request_fails(tmp_path, broken_engine):
    from core.task_runner import DoneMsg, LogMsg

    messages = _word_run(tmp_path, broken_engine, ["第一段说明文字。", "第二段说明文字。", "第三段说明文字。"])
    errors = [m for m in messages if isinstance(m, ErrorMsg)]
    assert len(errors) == 1 and not any(isinstance(m, DoneMsg) for m in messages)
    assert errors[0].message.startswith("3 " + _MESSAGE_START), errors[0].message
    assert not list((tmp_path / "out").rglob("*.docx"))
    logs = [m.message for m in messages if isinstance(m, LogMsg)]
    assert not any("全部 Word 文件已生成" in line for line in logs)


def test_word_task_with_one_failing_paragraph_still_finishes(tmp_path):
    from core.task_runner import DoneMsg

    class OneBroken:
        engine_name = "fake/one-broken"

        def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
            if "坏段落。" in texts:
                raise ValueError("503 Service Unavailable")
            return {text: f"EN {text}" for text in texts}

    messages = _word_run(tmp_path, OneBroken(), ["好段落。", "坏段落。"])
    # 部分失败照旧：写出已翻的，没翻的保留原文、标需复核。
    assert any(isinstance(m, DoneMsg) for m in messages)
    assert not any(isinstance(m, ErrorMsg) for m in messages)


def test_pdf_via_word_reports_the_same_failure(tmp_path, broken_engine):
    from docx import Document

    from core.pdf_word_route import translate_pdf_via_word
    from settings import AppSettings

    def fake_convert(_pdf_path, docx, **_kwargs):
        doc = Document()
        doc.add_paragraph("第一段说明文字。")
        doc.add_paragraph("第二段说明文字。")
        doc.save(docx)

    settings = AppSettings(source_lang="zh", target_lang="en")
    settings.pdf.target_lang = "en"
    with patch("core.pdf_word_route.convert_pdf_to_docx", fake_convert), \
            patch("core.word_task_runner.build_engine", return_value=broken_engine), \
            patch("core.word_task_runner.check_translation_api_config") as check, \
            patch("core.word_task_runner.tm_manager.lookup_batch", return_value={}):
        check.return_value.ok = True
        result = translate_pdf_via_word(
            tmp_path / "plan.pdf", tmp_path / "work", tmp_path / "final.docx", settings,
            source_lang="zh", should_stop=lambda: False, log=lambda *_args: None,
        )
    assert not result.ok
    assert result.error.startswith("2 " + _MESSAGE_START), result.error
    assert not (tmp_path / "final.docx").exists()
