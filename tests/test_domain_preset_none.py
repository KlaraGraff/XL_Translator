"""专业领域「无」预设：验证 get_system_prompt() 在选中「无」时不注入任何领域
提示词，具体领域与未知领域名的既有行为不受影响。

对应需求：ui/src/views/workspace.ts 的「专业领域」下拉新增「无」选项，选中后
不向模型注入任何领域提示词（不是空字符串占位，也不是「无特殊领域」话术，而
是那一段整个不出现）。
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from api.app import create_app
from config import DOMAIN_PRESETS
from core.engine_dispatcher import get_system_prompt
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


class DomainPresetNoneTests(unittest.TestCase):
    def test_none_preset_listed_and_empty(self) -> None:
        # 「无」必须是 DOMAIN_PRESETS 里的合法 key（否则 /api/domains 的校验会
        # 把它当未知预设拒绝），且内容为空——不携带任何领域文本。
        self.assertIn("无", DOMAIN_PRESETS)
        self.assertEqual(DOMAIN_PRESETS["无"], {})

    def test_none_preset_injects_nothing(self) -> None:
        settings = AppSettings(domain_preset="无")
        # 不给 target_lang，连「目标语言识别补充」都不会被拼接，此时整段
        # system prompt 必须是空字符串——领域 Prompt 那一段完全不出现。
        prompt = get_system_prompt(settings, target_lang="", source_lang="zh")
        self.assertEqual(prompt, "")

    def test_none_preset_does_not_leak_other_domain_text(self) -> None:
        settings = AppSettings(domain_preset="无")
        prompt = get_system_prompt(settings, target_lang="en", source_lang="zh")
        # 允许出现「目标语言识别补充」这类与领域无关的通用说明，但不能出现
        # 任何内置领域预设的专属措辞，也不能出现「无特殊领域」之类的占位话术。
        for keyword in ("工程同步", "资料管理", "行政与日常办公", "无特殊领域"):
            self.assertNotIn(keyword, prompt)

    def test_named_domain_presets_still_work(self) -> None:
        settings = AppSettings(domain_preset="资料管理场景")
        prompt = get_system_prompt(settings, target_lang="en", source_lang="zh")
        self.assertIn("资料管理", prompt)

    def test_excel_word_independent_none_state(self) -> None:
        # Excel 选「无」不影响 Word 仍保留原来的领域设定，反之亦然。
        settings = AppSettings(
            excel_domain_preset="无",
            word_domain_preset="行政生活化场景",
        )
        excel_prompt = get_system_prompt(settings, target_lang="en", page_key="excel")
        word_prompt = get_system_prompt(settings, target_lang="en", page_key="word")
        self.assertNotIn("行政", excel_prompt)
        self.assertIn("行政", word_prompt)

    def test_unknown_domain_name_does_not_raise(self) -> None:
        # 旧 settings.json 里存着一个当前版本不认识的领域名时，不许抛异常
        # 卡死——现有 DOMAIN_PRESETS.get(name, "") 兜底已经保证了这一点，这里
        # 补一条测试把它锁定为契约。
        settings = AppSettings(domain_preset="某个已下线的旧领域名")
        prompt = get_system_prompt(settings, target_lang="", source_lang="zh")
        self.assertEqual(prompt, "")

    def test_none_preset_is_not_overridable_via_custom_dict_membership(self) -> None:
        # domain_prompt_overrides 里没有「无」这个 key 时，选「无」必须仍然
        # 是空提示词，不会意外命中别的覆盖项。
        settings = AppSettings(
            domain_preset="无",
            domain_prompt_overrides={"同步工程场景": "覆盖文本"},
        )
        prompt = get_system_prompt(settings, target_lang="", source_lang="zh")
        self.assertEqual(prompt, "")


class DomainPresetOrderTests(IsolatedAppDataTestCase):
    """设置页的领域下拉直接照抄这个顺序，所以顺序本身是契约。

    路由会读设置文件，因此跑在隔离的数据目录里。
    """

    def test_the_api_lists_none_first(self) -> None:
        # 排序会把「无」按拼音丢到中间，和 config.py 里「放在字典首位、排在第一项」
        # 的约定打架；下拉里选不中当前值时，浏览器会静默改选第 0 项。
        client = TestClient(create_app())
        for surface in ("excel", "word"):
            presets = client.get(f"/api/domains/{surface}").json()["presets"]
            self.assertEqual(presets[0], "无")
            self.assertEqual(presets, list(DOMAIN_PRESETS))


if __name__ == "__main__":
    unittest.main()
