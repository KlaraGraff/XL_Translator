"""钉住 CODE_AUDIT_2026-08-29 产品批次 1 三条拍板项的落地。

中-15  领域 Prompt 覆盖按预设名裸存，换目标语言后拿错语言的覆盖静默出错稿——
       改为「预设名 → {目标语言 → Prompt}」两层存取；旧扁平数据在加载校验时
       归入当时的目标语言名下，其他语言回内置默认。迁移必须幂等、绝不拒绝
       写入（V9.3.0 起的兼容硬约束）。
高-12  macOS 就地更新换掉正在运行的 sidecar 的 bundle 后，界面还说「期间可以
       继续用」——就绪横幅改为「请尽快重启完成更新，期间新任务可能失败」，
       且更新落地后第一次新建任务（工作区三种 + TM 深度清洗）弹一次「先重启」
       拦截，可选择仍要开始。
高-7   清洗建议确认弹窗默认全勾，一键确认就能用旧建议盖掉人工校对——默认改为
       全不勾（技术半边——乐观版本校验与建议失效——已在 A7 落地）。

中-15 的后端部分用 AppSettings 校验与 TestClient 行为测试；前端改动沿用
test_audit_settings_ui_fixes.py 的先例，读取 TS 源码做回归钉子（本仓库没有
JS/TS 测试运行时）。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core import diagnostics, tm_manager
from core.document_config import (
    DOCUMENT_CONFIG_EXPORT_TYPE,
    DOCUMENT_CONFIG_EXPORT_VERSION,
    apply_document_config_import,
    parse_document_config_import,
)
from core.engine_dispatcher import get_system_prompt
from core.language_registry import get_default_target_lang
from settings import AppSettings

_UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"


def _read_ui(relative: str) -> str:
    return (_UI_SRC / relative).read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """按「async function NAME(...) ... 列首 }」截取函数体（见 B2 集群先例）。"""
    match = re.search(rf"(?:async )?function {re.escape(name)}\(.*?\n\}}", source, re.S)
    assert match, f"在源码里找不到函数 {name}"
    return match.group(0)


OVERRIDE_TEXT = "工程覆盖：术语按项目词表来。"


class DomainOverrideLangMigrationTests(unittest.TestCase):
    """中-15：旧扁平覆盖在校验时归入当时的目标语言名下，新嵌套形态原样通过。"""

    def test_flat_excel_override_lands_under_excel_target_lang(self) -> None:
        settings = AppSettings.model_validate(
            {
                "excel_target_lang": "fr",
                "excel_domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT},
            }
        )
        self.assertEqual(
            settings.excel_domain_prompt_overrides,
            {"同步工程场景": {"fr": OVERRIDE_TEXT}},
        )

    def test_pre_page_split_flat_override_uses_each_surface_own_lang(self) -> None:
        # 页面拆分前的老配置：只有全局覆盖 + 全局 target_lang；word 另有自己的语言。
        settings = AppSettings.model_validate(
            {
                "target_lang": "ja",
                "word_target_lang": "en",
                "domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT},
            }
        )
        self.assertEqual(
            settings.domain_prompt_overrides, {"同步工程场景": {"ja": OVERRIDE_TEXT}}
        )
        self.assertEqual(
            settings.excel_domain_prompt_overrides,
            {"同步工程场景": {"ja": OVERRIDE_TEXT}},
            "excel 没有自己的目标语言时应回落到全局 target_lang",
        )
        self.assertEqual(
            settings.word_domain_prompt_overrides,
            {"同步工程场景": {"en": OVERRIDE_TEXT}},
            "word 有自己的目标语言时必须用它，不能吃全局的",
        )

    def test_flat_override_without_any_lang_uses_registry_default(self) -> None:
        settings = AppSettings.model_validate(
            {"excel_domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT}}
        )
        self.assertEqual(
            settings.excel_domain_prompt_overrides,
            {"同步工程场景": {get_default_target_lang(): OVERRIDE_TEXT}},
        )

    def test_nested_override_passes_through_unchanged(self) -> None:
        nested = {"同步工程场景": {"fr": OVERRIDE_TEXT, "en": "english override"}}
        settings = AppSettings.model_validate(
            {"excel_target_lang": "ja", "excel_domain_prompt_overrides": nested}
        )
        self.assertEqual(settings.excel_domain_prompt_overrides, nested)

    def test_mixed_flat_and_nested_values_both_survive(self) -> None:
        settings = AppSettings.model_validate(
            {
                "excel_target_lang": "fr",
                "excel_domain_prompt_overrides": {
                    "同步工程场景": OVERRIDE_TEXT,
                    "医疗器械": {"en": "nested one"},
                },
            }
        )
        self.assertEqual(
            settings.excel_domain_prompt_overrides,
            {
                "同步工程场景": {"fr": OVERRIDE_TEXT},
                "医疗器械": {"en": "nested one"},
            },
        )

    def test_flat_constructor_kwarg_never_refused(self) -> None:
        # 兼容硬约束：手工构造（CLI、老测试、导入的旧文档配置）给扁平字典
        # 必须照收，绝不允许校验报错「拒绝写入且不给出路」。
        settings = AppSettings(domain_prompt_overrides={"同步工程场景": OVERRIDE_TEXT})
        self.assertEqual(
            settings.domain_prompt_overrides,
            {"同步工程场景": {get_default_target_lang(): OVERRIDE_TEXT}},
        )

    def test_flat_override_lang_is_normalized_to_a_language_code(self) -> None:
        # 互审发现：语言字段随后会被 _normalize_target_lang_state 归一化成语言
        # 代码（「日语」→ ja），覆盖若按原始字符串落键，取用端永远查不到，
        # 用户的自定义 Prompt 无声失效。各种拼写都必须归到同一个代码名下。
        for spelling in ("日语", "JA", "Japanese"):
            with self.subTest(spelling=spelling):
                settings = AppSettings.model_validate(
                    {
                        "excel_target_lang": spelling,
                        "excel_domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT},
                    }
                )
                self.assertEqual(settings.excel_target_lang, "ja")
                self.assertEqual(
                    settings.excel_domain_prompt_overrides,
                    {"同步工程场景": {"ja": OVERRIDE_TEXT}},
                )

    def test_flat_override_with_unresolvable_lang_matches_field_fallback(self) -> None:
        # 解析不出来的语言（乱码、区域变体）：字段本身会被归一化成默认目标语言，
        # 覆盖必须跟着落到同一个键下，不能留在没人查的原始字符串键上。
        settings = AppSettings.model_validate(
            {
                "excel_target_lang": "ja-JP",
                "excel_domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT},
            }
        )
        self.assertEqual(settings.excel_target_lang, get_default_target_lang())
        self.assertEqual(
            settings.excel_domain_prompt_overrides,
            {"同步工程场景": {get_default_target_lang(): OVERRIDE_TEXT}},
        )

    def test_flat_override_lang_resolves_custom_language_names(self) -> None:
        settings = AppSettings.model_validate(
            {
                "custom_target_langs": [
                    {"name": "粤语", "description": "", "code": "x-custom-yue"}
                ],
                "excel_target_lang": "粤语",
                "excel_domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT},
            }
        )
        self.assertEqual(settings.excel_target_lang, "x-custom-yue")
        self.assertEqual(
            settings.excel_domain_prompt_overrides,
            {"同步工程场景": {"x-custom-yue": OVERRIDE_TEXT}},
        )


class DomainOverrideDispatchTests(unittest.TestCase):
    """中-15：取用端只认当前目标语言名下的覆盖，其他语言回内置默认。"""

    def _settings(self) -> AppSettings:
        return AppSettings.model_validate(
            {
                "excel_domain_preset": "同步工程场景",
                "excel_target_lang": "fr",
                "excel_domain_prompt_overrides": {
                    "同步工程场景": {"fr": OVERRIDE_TEXT}
                },
            }
        )

    def test_override_used_for_its_own_target_lang(self) -> None:
        prompt = get_system_prompt(self._settings(), target_lang="fr", page_key="excel")
        self.assertIn(OVERRIDE_TEXT, prompt)

    def test_other_target_lang_falls_back_to_builtin_not_wrong_lang(self) -> None:
        prompt = get_system_prompt(self._settings(), target_lang="en", page_key="excel")
        self.assertNotIn(OVERRIDE_TEXT, prompt, "en 不能拿走 fr 的覆盖")
        self.assertIn("工程", prompt, "应回落到内置预设文本")

    def test_unvalidated_flat_override_keeps_legacy_semantics(self) -> None:
        # 防御分支：绕过校验直接塞旧扁平值（pydantic 默认不校验赋值），
        # 保持旧语义不分语言直接用，而不是抛错或静默丢弃。
        settings = self._settings()
        settings.excel_domain_prompt_overrides = {"同步工程场景": OVERRIDE_TEXT}  # type: ignore[assignment]
        prompt = get_system_prompt(settings, target_lang="en", page_key="excel")
        self.assertIn(OVERRIDE_TEXT, prompt)


class _PatchedAppDataTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app_data = Path(temporary.name) / "app-data"
        self.app_data.mkdir(parents=True)
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=self.app_data,
                SETTINGS_PATH=self.app_data / "settings.json",
                KEYS_PATH=self.app_data / "keys.json",
                BACKUPS_DIR=self.app_data / "backups",
                RECOVERY_PATH=self.app_data / "recovery.json",
            ),
            patch.object(tm_manager, "DB_PATH", self.app_data / "tm.db"),
            patch.object(
                diagnostics,
                "DIAGNOSTIC_RECORDS_DIR",
                Path(temporary.name) / "diagnostics",
            ),
            patch.object(diagnostics, "LOG_PATH", self.app_data / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class DomainOverrideRoundTripTests(_PatchedAppDataTestCase):
    """中-15：磁盘上的旧扁平 settings.json 可读、可写、迁移结果落盘且幂等。"""

    def test_flat_settings_file_loads_saves_and_stays_nested(self) -> None:
        payload = AppSettings().model_dump(mode="json")
        payload["excel_target_lang"] = "fr"
        payload["excel_domain_prompt_overrides"] = {"同步工程场景": OVERRIDE_TEXT}
        settings_module.SETTINGS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        loaded = settings_module.load_settings()
        self.assertEqual(
            loaded.excel_domain_prompt_overrides,
            {"同步工程场景": {"fr": OVERRIDE_TEXT}},
        )

        # 任意一次保存都必须把迁移后的嵌套形态写回磁盘（save_settings 的合并
        # 基座是新鲜 load_settings() 的结果），不能留着扁平旧形态反复重迁移。
        settings_module.save_settings(loaded)
        on_disk = json.loads(
            settings_module.SETTINGS_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(
            on_disk["excel_domain_prompt_overrides"],
            {"同步工程场景": {"fr": OVERRIDE_TEXT}},
        )

        reloaded = settings_module.load_settings()
        self.assertEqual(
            reloaded.excel_domain_prompt_overrides,
            loaded.excel_domain_prompt_overrides,
            "迁移必须幂等：再次加载不得改变结果",
        )


class DomainPutEndpointTests(_PatchedAppDataTestCase):
    """中-15：PUT /api/domains/{surface} 收嵌套新形态，也收旧客户端的扁平写入。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app())

    def _put(self, overrides: dict) -> dict:
        response = self.client.put(
            "/api/domains/excel",
            json={
                "preset": "同步工程场景",
                "custom_prompt": "",
                "prompt_overrides": overrides,
                "name_overrides": {},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_nested_override_round_trips(self) -> None:
        body = self._put({"同步工程场景": {"fr": OVERRIDE_TEXT}})
        self.assertEqual(
            body["prompt_overrides"], {"同步工程场景": {"fr": OVERRIDE_TEXT}}
        )

    def test_flat_legacy_put_is_normalized_not_rejected(self) -> None:
        # 先把 excel 的目标语言定成 fr，再用旧形态写入——必须归到 fr 名下。
        response = self.client.put("/api/settings", json={"excel_target_lang": "fr"})
        self.assertEqual(response.status_code, 200, response.text)
        body = self._put({"同步工程场景": OVERRIDE_TEXT})
        self.assertEqual(
            body["prompt_overrides"], {"同步工程场景": {"fr": OVERRIDE_TEXT}}
        )

    def test_empty_lang_layer_is_dropped(self) -> None:
        body = self._put({"同步工程场景": {}})
        self.assertEqual(body["prompt_overrides"], {})


class DocumentConfigExportVersionTests(unittest.TestCase):
    """中-15 连带：导出文件里的覆盖结构变了，版本号必须跟着升。

    旧版应用校验不了嵌套覆盖，若版本号不动，它读新文件时报的是一段英文校验
    错误；升到 2 之后走版本闸门，得到的是「请先升级应用」的明白话。
    """

    def test_export_version_is_bumped(self) -> None:
        self.assertGreaterEqual(DOCUMENT_CONFIG_EXPORT_VERSION, 2)

    def test_v1_bundle_with_flat_overrides_still_imports(self) -> None:
        # 导入侧只拒绝比自己高的版本：老同事导出的 v1 扁平覆盖照常进来，
        # 并在校验时归入文件里的目标语言名下。
        merged = apply_document_config_import(
            AppSettings(),
            parse_document_config_import(
                {
                    "type": DOCUMENT_CONFIG_EXPORT_TYPE,
                    "version": 1,
                    "document": {
                        "excel_target_lang": "fr",
                        "excel_domain_prompt_overrides": {"同步工程场景": OVERRIDE_TEXT},
                    },
                }
            ),
        )
        self.assertEqual(
            merged.excel_domain_prompt_overrides,
            {"同步工程场景": {"fr": OVERRIDE_TEXT}},
        )


class DomainOverrideUiSourcePinTests(unittest.TestCase):
    """中-15 前端：设置页按「预设名＋目标语言」读写覆盖。"""

    def test_editor_reads_override_by_preset_and_target_lang(self) -> None:
        source = _read_ui("views/settings.ts")
        self.assertIn("current.promptOverrides[preset]?.[targetLang]", source)

    def test_save_writes_only_current_lang_layer(self) -> None:
        source = _read_ui("views/settings.ts")
        self.assertIn("langOverrides[targetLang] = promptArea.value", source)
        self.assertIn("delete langOverrides[targetLang]", source)


class UpdateRestartInterceptSourcePinTests(unittest.TestCase):
    """高-12：就绪横幅警示文案 + 更新落地后新建任务的一次性拦截。"""

    def test_ready_banner_warns_instead_of_reassuring(self) -> None:
        source = _read_ui("update-toast.ts")
        self.assertIn("请尽快重启完成更新，期间新任务可能失败。", source)
        self.assertNotIn("有任务在跑就先跑完，不急", source)
        self.assertNotIn("装完会告诉你，期间可以继续用", source)

    def test_toast_exports_the_intercept_modal(self) -> None:
        source = _read_ui("update-toast.ts")
        self.assertIn("export function openRestartBeforeTaskModal", source)
        self.assertIn("仍要开始", source)
        self.assertIn("立即重启", source)

    def test_intercept_modal_offers_a_way_out(self) -> None:
        # 互审发现：只有「仍要开始／立即重启」时，看完警告想先换文件的用户
        # 没有退路——要么开一个不想开的任务，要么重启应用。
        body = _function_body(_read_ui("update-toast.ts"), "openRestartBeforeTaskModal")
        self.assertIn('"取消"', body)

    def test_controller_arms_the_warning_once_per_ready_version(self) -> None:
        source = _read_ui("update-controller.ts")
        body = _function_body(source, "consumePendingRestartWarning")
        self.assertIn('state.flow.phase !== "ready"', body)
        self.assertIn("restartWarningShownFor = version", body)

    def test_workspace_start_task_consults_the_warning_first(self) -> None:
        source = _read_ui("views/workspace.ts")
        body = _function_body(source, "startTask")
        self.assertIn("consumePendingRestartWarning()", body)
        self.assertIn("openRestartBeforeTaskModal", body)
        self.assertLess(
            body.index("consumePendingRestartWarning"),
            body.index("preflightAndSubmit"),
            "拦截必须发生在预检/提交之前",
        )

    def test_tm_clean_is_guarded_too(self) -> None:
        source = _read_ui("views/library.ts")
        body = _function_body(source, "tmClean")
        self.assertIn("consumePendingRestartWarning()", body)
        self.assertIn("openRestartBeforeTaskModal", body)


class CleanSuggestionDefaultSourcePinTests(unittest.TestCase):
    """高-7 产品半边：清洗建议确认弹窗默认全不勾。"""

    def test_suggestion_checkbox_defaults_unchecked(self) -> None:
        source = _read_ui("views/library.ts")
        body = _function_body(source, "openCleanReviewModal")
        self.assertIn("check.checked = false;", body)
        self.assertNotIn("check.checked = true;", body)

    def test_write_button_refuses_an_all_unchecked_submit(self) -> None:
        # 互审发现：默认全不勾之后，误点「写入已勾选建议」会提交一个全 false
        # 的空单——后端不动一条，前端却把建议面板连同「查看建议」入口一起收走，
        # toast 还说「已写入 0 条」。空单必须在发请求之前拦下、弹窗留在原地。
        body = _function_body(_read_ui("views/library.ts"), "openCleanReviewModal")
        self.assertIn("if (!accepted)", body)
        self.assertIn("keepOpen: true", body)
        self.assertIn("handle.close()", body)
        self.assertLess(
            body.index("if (!accepted)"),
            body.index("client.request"),
            "空单必须在发请求之前拦下",
        )


if __name__ == "__main__":
    unittest.main()
