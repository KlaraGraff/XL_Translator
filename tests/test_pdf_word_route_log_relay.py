"""PDF「转 Word 翻译」：Word 流程的逐批日志要转到 PDF 任务的运行日志里。

以前只转警告和错误，这部分页翻译时日志停在「已转成 Word，开始按 Word 流程翻译」。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import core.pdf_word_route as route
from core.task_runner_messages import DoneMsg, LogMsg
from settings import AppSettings


class _FakeWordRunner:
    def __init__(self, items, settings, **kwargs) -> None:
        out = Path(settings.word_output.custom_output_dir) / "out.docx"
        out.write_bytes(b"docx")
        self._messages = [
            LogMsg("INFO", "读取文件：x.docx（1/1）"),
            LogMsg("INFO", "开始翻译 12 处内容（相同内容只翻译一次）：分 2 批发送，每次同时发送 2 批"),
            LogMsg("INFO", "正在等待 2 批请求返回，已等待 30 秒"),
            LogMsg("INFO", "第 1/2 批已返回：8 处，用时 3.1 秒"),
            LogMsg("WARN", "第 2/2 批已返回：4 处中 1 处未获得译文，用时 2.0 秒"),
            LogMsg("INFO", "第 9/9 批已返回：1 处，用时 0.1 秒", visible=False),
            LogMsg("OK", "文件完成：x.docx"),
            DoneMsg(output_dir="", file_results=[{"success": True, "output": str(out)}],
                    elapsed_sec=1.0, tm_hit_count=0, api_call_count=2),
        ]

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def needs_poll(self) -> bool:
        return bool(self._messages)

    def get_message(self, timeout: float = 0.0):
        return self._messages.pop(0) if self._messages else None


def test_word_route_relays_batch_lines(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    logged: list[tuple[str, str]] = []

    def fake_convert(_pdf, docx, **_kwargs):
        Path(docx).write_bytes(b"docx")

    with patch.object(route, "convert_pdf_to_docx", fake_convert), \
            patch("core.word_document.scan_word_path", return_value=[object()]), \
            patch("core.word_task_runner.WordTaskRunner", _FakeWordRunner):
        result = route.translate_pdf_via_word(
            pdf, tmp_path / "work", tmp_path / "final.docx", AppSettings(),
            source_lang="zh", should_stop=lambda: False,
            log=lambda level, message: logged.append((level, message)), file_label="doc.pdf",
        )

    assert result.ok
    assert logged == [
        ("INFO", "[doc.pdf] 已转成 Word，开始按 Word 流程翻译。"),
        ("INFO", "[doc.pdf] 开始翻译 12 处内容（相同内容只翻译一次）：分 2 批发送，每次同时发送 2 批"),
        ("INFO", "[doc.pdf] 正在等待 2 批请求返回，已等待 30 秒"),
        ("INFO", "[doc.pdf] 第 1/2 批已返回：8 处，用时 3.1 秒"),
        ("WARN", "[doc.pdf] 第 2/2 批已返回：4 处中 1 处未获得译文，用时 2.0 秒"),
    ]
