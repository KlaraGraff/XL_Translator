from __future__ import annotations

import unittest

from core.task_runner import user_facing_reason
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


class UserFacingReasonTests(unittest.TestCase):
    """The gate all three runners pass a raw cause through before showing it."""

    def test_upstream_503_blob_becomes_one_chinese_sentence(self) -> None:
        raw = (
            "Server error '503 Service Unavailable' for url "
            "'https://api.ai-pixel.online/v1/images/edits'\n"
            "For more information check: "
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503\n"
            '接口返回：{"error":{"message":"Service temporarily unavailable"}}'
        )
        reason = user_facing_reason(RuntimeError(raw), fallback="接口没有返回结果。")
        self.assertIn("暂时不可用", reason)
        self.assertNotIn("http", reason)
        self.assertNotIn('{"error"', reason)
        self.assertNotIn("Server error", reason)

    def test_missing_package_error_drops_the_absolute_path(self) -> None:
        raw = "Package not found at '/Users/somebody/工程量清单.xlsx'"
        reason = user_facing_reason(ValueError(raw), fallback="文件打不开。")
        self.assertNotIn("/Users/", reason)
        self.assertNotIn("Package not found", reason)

    def test_a_message_we_wrote_ourselves_survives_untouched(self) -> None:
        """Our own Chinese sentences are better than any fallback — keep them."""
        ours = "第 3 页的译文比原文短很多，已标记待复核。"
        self.assertEqual(user_facing_reason(RuntimeError(ours), fallback="出错了。"), ours)

    def test_unrecognized_english_is_replaced_by_the_caller_fallback(self) -> None:
        reason = user_facing_reason(
            RuntimeError("weird internal state 0x1"),
            fallback="出现了未预期的问题。",
        )
        self.assertEqual(reason, "出现了未预期的问题。")

    def test_a_chinese_sentence_carrying_a_url_is_still_replaced(self) -> None:
        reason = user_facing_reason(
            RuntimeError("请求失败：https://api.example/v1/chat"),
            fallback="接口没有响应。",
        )
        self.assertEqual(reason, "接口没有响应。")


if __name__ == "__main__":
    unittest.main()
