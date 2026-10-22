"""给用户看的几处文字不再露出内部标识或英文原始报错（界面走查 S6、S8）。"""

from __future__ import annotations

import unittest

import httpx

from core.connectivity_check import _user_error
from core.user_facing_errors import humanize_error
from settings import ModelConnection


class ConnectionTestMessageTests(unittest.TestCase):
    def test_httpx_connect_error_becomes_one_chinese_sentence(self) -> None:
        message = _user_error(httpx.ConnectError("All connection attempts failed"))
        self.assertNotIn("All connection attempts failed", message)
        self.assertNotIn("测试失败", message)
        self.assertEqual(message, humanize_error("connection refused"))

    def test_api_key_is_still_masked_in_unrecognized_errors(self) -> None:
        message = _user_error(RuntimeError("weird failure sk-secret-123456"), secret="sk-secret-123456")
        self.assertNotIn("sk-secret-123456", message)


class ConnectionDisplayLabelTests(unittest.TestCase):
    def test_unnamed_connection_shows_provider_name_and_host(self) -> None:
        connection = ModelConnection(provider="custom_openai", base_url="https://api.example.com/v1")
        self.assertEqual(connection.display_label, "自定义配置 · api.example.com")

    def test_unnamed_connection_without_address_shows_provider_name(self) -> None:
        self.assertEqual(ModelConnection(provider="deepseek").display_label, "DeepSeek")

    def test_user_given_name_wins(self) -> None:
        self.assertEqual(ModelConnection(provider="deepseek", label="主力").display_label, "主力")

    def test_old_default_follow_names_show_the_renamed_uses(self) -> None:
        old = ModelConnection(provider="deepseek", label="跟随文档翻译（Excel / Word / CAD）")
        self.assertEqual(old.display_label, "跟随文本翻译（Excel / Word / CAD）")
        self.assertEqual(old.label, "跟随文档翻译（Excel / Word / CAD）")
        self.assertEqual(ModelConnection(label="跟随PDF 与图片翻译").display_label, "跟随图像翻译")


if __name__ == "__main__":
    unittest.main()
