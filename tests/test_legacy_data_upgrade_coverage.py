"""V7.4 用户数据目录升级的离线演练：settings / keys / TM 三份数据一起升。

验收记录 `docs/validation/2026-07-16_route_b_tauri_final_acceptance.md:17` 当时把
「V8 数据兼容」记为 PASS（模拟 V7.4 数据），证据是 `tests/test_v8_upgrade_migration.py`
在隔离目录里构造 schema v24 的 settings、keys 和 TM SQLite，验证迁移后语言、模型、密钥、
TM 数据及 settings 备份都还在。那个文件后来不在了，仓库里没有第二个用例把三份数据放进
同一个目录、按真实启动顺序（读设置 → 读密钥 → 开记忆库 → 写回）一起升一遍。

真实 V7.4 用户数据目录本身只有用户手上有（见同文件 :31 的外部验收项），所以这里用离线
构造的等价数据，断言的是**四项数据都还在**，而不是「版本号变成了 41」：语言、模型选择与
Base URL、每把密钥、每条 TM 词条。V7.4 的 ``deepseek-chat`` 已在 v27 停用，读盘时按既定
规则换成继任的 ``deepseek-flash``——这是升级，不是丢数据。

单独成文件而不是塞进 `tests/test_data_schema_recovery.py`：那条演练只碰 `settings` 和
`core.tm_manager`，不该被 API 层（`api.app`）的导入问题挡在门外。

测试结尾还钉住 AGENTS.md 的两条硬规矩：第一次写回前必须留一份 settings 原文件；迁移后
任何一份文件都必须可写（不能出现「读得出来但不能保存」的死局）。
"""

from __future__ import annotations

import json
import sqlite3
import unittest

import settings as settings_module
from core import tm_manager
from tests.app_data_isolation import IsolatedAppDataTestCase

# V7.4 的 settings.json：字段名与当时一致，版本 24。
_V74_SETTINGS = {
    "settings_version": 24,
    "source_lang": "auto",
    "target_lang": "fr",
    "custom_prompt": "保留这句 V7.4 自定义要求",
    "domain_preset": "同步工程场景",
    "excel_domain_preset": "无",
    "word_domain_preset": "同步工程场景",
    "cad_domain_preset": "同步工程场景",
    "appearance": {"theme": "system", "ui_language": "fr"},
    "engine": {
        "cloud_provider": "deepseek",
        "cloud_model": "deepseek-chat",
        "cloud_base_url": "https://v74.invalid/v1",
        "concurrency": 3,
    },
    "last_source_folder": "/Users/v74/Documents/待翻译",
}

_V74_KEYS = {
    "openai": "sk-v74-openai",
    "deepseek": "sk-v74-deepseek",
    "claude": "sk-v74-claude",
    "zhipu": "sk-v74-zhipu",
}

# TM v2：就是 v3 之前的那张表，后面几列由升级补上。
_V2_TM_DDL = """
CREATE TABLE tm_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text   TEXT    NOT NULL,
    source_hash   TEXT    NOT NULL DEFAULT '',
    target_text   TEXT    NOT NULL,
    lang_pair     TEXT    NOT NULL,
    word_type     TEXT    NOT NULL,
    source_engine TEXT    DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_text, lang_pair)
);
CREATE TABLE tm_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
"""


class LegacyV74DataDirectoryDrillTests(IsolatedAppDataTestCase):
    def _write_settings(self) -> dict:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(
            json.dumps(_V74_SETTINGS, ensure_ascii=False), encoding="utf-8"
        )
        return dict(_V74_SETTINGS)

    def _write_keys(self) -> dict[str, str]:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "keys.json").write_text(
            json.dumps(_V74_KEYS, ensure_ascii=False), encoding="utf-8"
        )
        return dict(_V74_KEYS)

    def _write_v2_memory(self, entries: int) -> None:
        """V7.4 时代的记忆库：schema v2，词条是在有哈希之前写下的。

        ``source_hash`` 停在列默认值 ''——真实老库就是这样，升级必须先补哈希再
        建唯一索引，否则查不到、也写不进。
        """
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(tm_manager.DB_PATH))
        try:
            conn.executescript(_V2_TM_DDL)
            conn.execute(
                "INSERT INTO tm_meta (meta_key, meta_value) VALUES (?, ?)",
                [tm_manager.TM_SCHEMA_VERSION_KEY, "2"],
            )
            for index in range(entries):
                conn.execute(
                    "INSERT INTO tm_entries "
                    "(source_text, source_hash, target_text, lang_pair, word_type) "
                    "VALUES (?, '', ?, 'en-zh', 'auto')",
                    [f"term-{index}", f"词-{index}"],
                )
            conn.commit()
        finally:
            conn.close()

    def _settings_backups(self) -> list:
        return sorted(self.app_data_dir.rglob("settings_v24_before_upgrade_*.json"))

    def test_v74_directory_upgrade_keeps_settings_keys_and_memory(self) -> None:
        original_settings = self._write_settings()
        original_keys = self._write_keys()
        self._write_v2_memory(entries=4)

        # ① 升级前先能看出「这是 v24 老数据、可读可写」。
        before = settings_module.get_settings_schema_status()
        self.assertEqual(before["state"], "adopted")
        self.assertTrue(before["can_write"])
        self.assertEqual(before["stored_version"], 24)
        loaded = settings_module.load_settings()

        # ② 设置：语言、Base URL、上次打开的目录、界面语言全部原样。
        self.assertEqual(loaded.source_lang, "auto")
        self.assertEqual(loaded.target_lang, "fr")
        self.assertEqual(loaded.engine.cloud_provider, "deepseek")
        self.assertEqual(loaded.engine.cloud_base_url, "https://v74.invalid/v1")
        self.assertEqual(loaded.engine.concurrency, 3)
        self.assertEqual(
            loaded.last_source_folder, original_settings["last_source_folder"]
        )
        self.assertEqual(loaded.appearance.ui_language, "fr")
        # 停用的模型按既定规则换成继任模型，不是丢成默认值。
        self.assertEqual(loaded.engine.cloud_model, "deepseek-flash")
        # v30 起默认主题改成浅色，v24 文件里的默认 "system" 跟着改成浅色。
        self.assertEqual(loaded.appearance.theme, "light")
        # v41 起默认配色是「流光」，旧文件没有这两个字段，按新默认补齐。
        self.assertEqual(
            (loaded.appearance.palette_light, loaded.appearance.palette_dark),
            ("liuguang", "liuguang"),
        )

        # ③ V7.4 的单条自定义 Prompt 变成「补充要求」，正文一个字不丢。
        prompt_name = settings_module.LEGACY_CUSTOM_PROMPT_NAME
        self.assertEqual(
            loaded.domain_custom_prompts[prompt_name]["_base"],
            "保留这句 V7.4 自定义要求",
        )
        for page_prompts in (
            loaded.excel_domain_custom_prompts,
            loaded.word_domain_custom_prompts,
            loaded.cad_domain_custom_prompts,
        ):
            self.assertEqual(set(page_prompts), {prompt_name})
        # v33 起旧默认（全局/Word/CAD 的「同步工程场景」、Excel 的「无」）一律改为
        # 自动识别场景，所以四页的开关都是开的、选中的补充要求都是「无」。
        self.assertTrue(loaded.domain_smart_context)
        self.assertTrue(loaded.excel_smart_context)
        self.assertTrue(loaded.word_smart_context)
        self.assertEqual(loaded.excel_domain_preset, settings_module.NO_EXTRA_PROMPT)

        # ④ 密钥：四把都在，作用域不变。
        self.assertEqual(settings_module.load_keys(), original_keys)

        # ⑤ 记忆库：v2 的四条无哈希词条升到当前版本后仍可查、可写。
        tm_status = tm_manager.get_schema_status()
        self.assertEqual(tm_status["state"], "upgraded")
        self.assertTrue(tm_status["can_write"])
        self.assertEqual(tm_status["stored_version"], 2)
        tm_manager.init_db()
        self.assertEqual(tm_manager.count_entries(), 4)
        self.assertEqual(
            tm_manager.lookup_batch(["term-0", "term-3"], "en-zh"),
            {"term-0": "词-0", "term-3": "词-3"},
        )
        self.assertTrue(
            tm_manager.insert_manual_entry("term-v74-new", "新词", "en-zh")
        )
        self.assertEqual(tm_manager.count_entries(), 5)

        # ⑥ 第一次写回：先备份原文件，再盖成当前版本；不产生「恢复」告示。
        settings_module.save_settings(loaded)
        persisted = json.loads(
            (self.app_data_dir / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            persisted["settings_version"], settings_module.SETTINGS_SCHEMA_VERSION
        )
        self.assertEqual(persisted["target_lang"], "fr")
        self.assertEqual(persisted["engine"]["cloud_model"], "deepseek-flash")
        backups = self._settings_backups()
        self.assertEqual(len(backups), 1, list(self.app_data_dir.rglob("*")))
        self.assertEqual(
            json.loads(backups[0].read_text(encoding="utf-8")), original_settings
        )
        self.assertEqual(settings_module.read_recovery_record(), {})

        # ⑦ 三项都必须是「可写」的，任何一项都不能停在「读得出来但存不进去」。
        self.assertEqual(
            settings_module.get_settings_schema_status()["state"], "current"
        )
        self.assertTrue(settings_module.get_settings_schema_status()["can_write"])
        self.assertEqual(tm_manager.get_schema_status()["state"], "current")
        self.assertTrue(tm_manager.get_schema_status()["can_write"])
        # 再存一次不再重复备份（同一次升级只留一份原件）。
        settings_module.save_settings(settings_module.load_settings())
        self.assertEqual(len(self._settings_backups()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
