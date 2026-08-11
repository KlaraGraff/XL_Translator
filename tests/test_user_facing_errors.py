from __future__ import annotations

import unittest

from core.user_facing_errors import humanize_error, strip_error_noise


class HumanizeErrorTests(unittest.TestCase):
    """Every case here is a message a real run put in front of a user."""

    def test_applescript_removed_verb_reads_as_unsupported_automation(self) -> None:
        raw = "407:413: syntax error: Expected end of line but found identifier. (-2741)"
        self.assertEqual(
            humanize_error(raw),
            "本机 Office 不支持这项自动化操作，已改用内置方式处理，排版可能与 Office 略有差异。",
        )

    def test_automation_permission_denied_points_at_system_settings(self) -> None:
        raw = "execution error: Not authorized to send Apple events to Microsoft Word. (-1743)"
        self.assertIn("系统设置", humanize_error(raw))

    def test_dns_failure_reads_as_no_network(self) -> None:
        raw = "[Errno 8] nodename nor servname provided, or not known"
        self.assertTrue(humanize_error(raw).startswith("连不上网络"))

    def test_corrupt_docx_reads_as_broken_file(self) -> None:
        raw = "Package not found at '/tmp/fixtures/broken.docx'"
        self.assertEqual(humanize_error(raw), "这个文件打不开，可能已损坏，或者后缀名与真实格式不符。")

    def test_openpyxl_zip_complaint_reads_as_broken_file(self) -> None:
        self.assertEqual(
            humanize_error("File is not a zip file"),
            "这个文件打不开，可能已损坏，或者后缀名与真实格式不符。",
        )

    def test_service_unavailable_drops_status_line_and_mdn_link(self) -> None:
        raw = (
            "Server error '503 Service Unavailable' for url 'https://api.example.com/v1/images/edits'\n"
            "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503"
        )
        sentence = humanize_error(raw)
        self.assertEqual(sentence, "接口所在的服务暂时不可用，请稍后重试，或在设置里换一条连接。")
        self.assertNotIn("developer.mozilla.org", sentence)

    def test_unknown_message_survives_untouched(self) -> None:
        """An unrecognized failure keeps its text: a vague sentence would erase the only clue."""
        self.assertEqual(humanize_error("段落 18 的译文长度异常"), "段落 18 的译文长度异常")

    def test_unknown_message_uses_fallback_when_caller_supplies_one(self) -> None:
        self.assertEqual(humanize_error("weird internal state 0x1", "转换失败。"), "转换失败。")

    def test_empty_value_returns_fallback(self) -> None:
        self.assertEqual(humanize_error(None, "转换失败。"), "转换失败。")
        self.assertEqual(humanize_error(""), "")

    def test_exception_instances_are_accepted(self) -> None:
        self.assertTrue(humanize_error(FileNotFoundError("No such file or directory: 'a.docx'")).startswith("文件不存在"))

    def test_status_code_rules_do_not_fire_on_ordinary_numbers(self) -> None:
        """`\\b503\\b` must not match a paragraph count or a byte size."""
        self.assertEqual(humanize_error("已处理 1503 段"), "已处理 1503 段")
        self.assertEqual(humanize_error("写出 5030 字节"), "写出 5030 字节")

    def test_strip_error_noise_collapses_whitespace(self) -> None:
        self.assertEqual(strip_error_noise("  a\n  b \t c "), "a b c")


if __name__ == "__main__":
    unittest.main()
