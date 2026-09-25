"""钉住 CODE_AUDIT_2026-08-29 里 B2-settings-ui 集群三条缺陷的修复。

高-14  前端内置领域 Prompt 是过期硬编码文本，缺规则、缺 en 键，用户一编辑保存
       就把后端权威文本永久顶掉——改成前端向后端只读端点要内置文本，
       config.py 的 DOMAIN_PRESETS 单一事实来源。
中-14  五个数值字段前端上下限比后端 config.py 宽，越界提交 422 英文报错且
       catch 分支不重绘表单——对齐边界、加前端夹值、失败时重绘并给中文提示。
中-17  主题先写 localStorage 再落后端，后端失败则本地/后端永久分叉——
       落盘成功后才写 localStorage，失败时回滚可见主题。

三条里高-14 有真正的后端改动（新增只读端点），用 TestClient 做行为测试；
中-14 / 中-17 是纯前端改动，本仓库没有 JS/TS 测试运行时（package.json 只有
tsc --noEmit），沿用 test_config_crypto.py::CorruptDetailContractTests 已有的
先例——读取 ui/src/views/settings.ts 源码文本做回归钉子。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
import settings as settings_module
from api.app import create_app
from core import diagnostics, tm_manager

SETTINGS_TS_PATH = Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "settings.ts"


def _read_settings_ts() -> str:
    return SETTINGS_TS_PATH.read_text(encoding="utf-8")


class BuiltinDomainPromptsEndpointTests(unittest.TestCase):
    """高-14：GET /api/domains/builtin-prompts 是内置领域 Prompt 的唯一事实来源。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        app_data = Path(temporary.name) / "app-data"
        for patcher in (
            patch.multiple(
                settings_module,
                APP_DATA_DIR=app_data,
                SETTINGS_PATH=app_data / "settings.json",
                KEYS_PATH=app_data / "keys.json",
            ),
            patch.object(tm_manager, "DB_PATH", app_data / "tm.db"),
            patch.object(diagnostics, "DIAGNOSTIC_RECORDS_DIR", Path(temporary.name) / "diagnostics"),
            patch.object(diagnostics, "LOG_PATH", app_data / "app.log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(create_app())

    def test_endpoint_returns_the_full_config_presets_verbatim(self) -> None:
        """回归签名：整份 dict 必须原样吐出，不能是精简/摘要过的版本。"""
        response = self.client.get("/api/domains/builtin-prompts")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"presets": config.DOMAIN_PRESETS})

    def test_route_is_registered_ahead_of_the_surface_route_and_not_shadowed(self) -> None:
        """`/api/domains/{surface}` 声明在后面就会把这条路由吃掉，变成
        404「Unknown translation surface.」——回归 test_api_route_ordering.py
        里同一类问题的钉法。"""
        response = self.client.get("/api/domains/builtin-prompts")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json().get("detail"), "Unknown translation surface.")

    def test_includes_the_previously_missing_en_key_and_the_two_dropped_rules(self) -> None:
        """旧的前端硬编码文本缺 en 键，也缺「一个字符都不能改动」和「中文计量
        用语换成目标语言书写习惯」这两条规则。走后端端点之后，这些必须都在。"""
        preset = config.DOMAIN_PRESETS["同步工程场景"]
        self.assertIn("en", preset, "config.py 的内置预设应当带 en 键")
        self.assertIn("一个字符都不能改动", preset["en"])
        self.assertIn("中文计量用语", preset["en"])

        response = self.client.get("/api/domains/builtin-prompts")
        served = response.json()["presets"]["同步工程场景"]
        self.assertIn("en", served)
        self.assertIn("一个字符都不能改动", served["en"])
        self.assertIn("中文计量用语", served["en"])

    def test_unknown_surface_still_404s_after_adding_the_sibling_route(self) -> None:
        """确认新路由没有反过来抢占 `/api/domains/{surface}` 的合法请求。"""
        response = self.client.get("/api/domains/excel")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/domains/nonsense")
        self.assertEqual(response.status_code, 404)


class DomainPromptFrontendSourceTests(unittest.TestCase):
    """高-14：前端不得再自带一份内置领域 Prompt 的硬拷贝。"""

    def test_the_stale_hardcoded_copy_is_gone(self) -> None:
        source = _read_settings_ts()
        # 这是旧硬编码文本里「同步工程场景」_base 的原句——缺「一个字符都不能改动」，
        # 一旦这句话重新出现在文件里，说明有人把硬拷贝加回来了。
        self.assertNotIn(
            "原文中的编号、日期、计量单位、规格参数、版本号与符号必须原样保留。",
            source,
            "settings.ts 里又出现了过期的内置领域 Prompt 硬拷贝（高-14 回归）",
        )

    def test_frontend_fetches_the_builtin_prompts_from_the_backend(self) -> None:
        source = _read_settings_ts()
        self.assertIn("/api/domains/builtin-prompts", source)
        self.assertIn("refreshDomainBuiltinPrompts", source)
        # 必须挂在每次刷新设置的路径上，否则领域预设保存后内置文本就没法跟着刷新。
        refresh_settings_match = re.search(
            r"async function refreshSettings\(\):[^{]*\{(.*?)\n\}", source, re.S,
        )
        self.assertIsNotNone(refresh_settings_match, "找不到 refreshSettings() 函数体")
        self.assertIn("refreshDomainBuiltinPrompts()", refresh_settings_match.group(1))


class NumericBoundsAlignmentTests(unittest.TestCase):
    """中-14：未解锁时前端上限与 config.py 一致；解锁后只保留正整数限制。"""

    @staticmethod
    def _numberfield_bounds(source: str, setting_path: str) -> tuple[int, int]:
        pattern = (
            r'saveSettingPath\("' + re.escape(setting_path) + r'", v\)'
            # reRenderAfter 的第二个参数（{ rerenderOnError: true } 之类的 opts）是可选的——
            # REG-1 之后数值框都会带上它，但这条断言只关心 min/max，不关心 opts 长什么样。
            r'(?:,\s*\{[^{}]*\})?\)'
            r'[^)]*?\{\s*min:\s*(?:throughputUnlocked\s*\?\s*1\s*:\s*)?(-?\d+),\s*'
            r'max:\s*throughputUnlocked\s*\?\s*undefined\s*:\s*(-?\d+)'
        )
        match = re.search(pattern, source)
        assert match, f"没找到 {setting_path} 的 numberField min/max 声明"
        return int(match.group(1)), int(match.group(2))

    def test_word_batch_chars_bounds_match_config(self) -> None:
        lo, hi = self._numberfield_bounds(_read_settings_ts(), "word_batch.max_chars_per_batch")
        self.assertEqual((lo, hi), (config.WORD_BATCH_CHARS_MIN, config.WORD_BATCH_CHARS_MAX))

    def test_word_page_shows_only_character_budget_and_retry(self) -> None:
        source = _read_settings_ts()
        self.assertNotIn('numberField("每批最大段落数"', source)
        self.assertNotIn('numberField("长段拆分阈值"', source)
        self.assertIn('numberField("每批字符上限"', source)

    def test_pdf_page_retry_bounds_match_config(self) -> None:
        source = _read_settings_ts()
        match = re.search(
            r'saveSettingPath\("pdf\.page_retry_attempts", v\).*?\{\s*min:\s*(\d+),\s*max:\s*(\d+)',
            source,
        )
        self.assertIsNotNone(match)
        lo, hi = int(match.group(1)), int(match.group(2))
        self.assertEqual((lo, hi), (config.PDF_PAGE_RETRY_ATTEMPTS_MIN, config.PDF_PAGE_RETRY_ATTEMPTS_MAX))

    def test_pdf_page_concurrency_bounds_match_config(self) -> None:
        source = _read_settings_ts()
        self.assertIn('concurrencyInput.min = "1";', source)
        self.assertIn(
            f'if (!throughputUnlocked) concurrencyInput.max = "{config.PDF_PAGE_CONCURRENCY_SAFETY_CAP}";',
            source,
        )
        # 未解锁时继续夹到安全上限；解锁后可使用更大的正整数。
        self.assertIn(
            f"parsed = Math.min(throughputUnlocked ? 2**31 - 1 : {config.PDF_PAGE_CONCURRENCY_SAFETY_CAP}, Math.max(1, parsed));",
            source,
        )

    def test_numberfield_clamps_before_committing(self) -> None:
        """光对齐 min/max 属性不够——HTML 的 min/max 拦不住手动输入的越界值，
        真正防住 422 的是提交前的 clamp。"""
        source = _read_settings_ts()
        number_field_match = re.search(
            r"function numberField\(.*?\n\}", source, re.S,
        )
        self.assertIsNotNone(number_field_match)
        body = number_field_match.group(0)
        self.assertIn("Math.max(opts.min", body)
        self.assertIn("Math.min(opts.max", body)


class ReRenderAfterRollbackTests(unittest.TestCase):
    """中-14：保存失败（含 422）必须重绘表单并给中文提示，不能留着没存的值。"""

    def test_catch_branch_rerenders_and_uses_friendly_message(self) -> None:
        source = _read_settings_ts()
        match = re.search(r"async function reRenderAfter<T>\(.*?\n\}", source, re.S)
        self.assertIsNotNone(match, "找不到 reRenderAfter 函数体")
        body = match.group(0)
        catch_match = re.search(r"catch \(error\) \{(.*?)\n  \}\n\}", body, re.S)
        self.assertIsNotNone(catch_match, "reRenderAfter 里找不到 catch 分支")
        catch_body = catch_match.group(1)
        self.assertIn("renderBody()", catch_body, "catch 分支必须重绘表单，否则表单值和磁盘不一致")
        self.assertIn("friendlyErrorMessage(error)", catch_body, "catch 分支必须给中文提示而不是原样甩英文报错")

    def test_friendly_error_message_translates_pydantic_validation_errors(self) -> None:
        source = _read_settings_ts()
        match = re.search(r"function friendlyErrorMessage\(.*?\n\}", source, re.S)
        self.assertIsNotNone(match, "找不到 friendlyErrorMessage 函数")
        body = match.group(0)
        self.assertIn("validation error", body.lower())
        self.assertIn("超出允许范围", body)


class ThemePersistenceRollbackTests(unittest.TestCase):
    """中-17：主题必须先落后端成功，才能写 localStorage；失败要回滚可见主题。"""

    def test_local_storage_write_is_a_separate_post_success_step(self) -> None:
        source = _read_settings_ts()
        self.assertIn("function commitThemeToLocalStorage", source)
        self.assertIn("function applyThemeVisual", source)
        # 旧的错误实现：一个函数里又切可见主题又写 localStorage，在落盘之前调用。
        self.assertNotIn("function applyThemePreference", source, "旧的先写 localStorage 再落盘的实现应已被拆分/删除")

    def test_click_handler_only_commits_local_storage_after_persist_succeeds(self) -> None:
        source = _read_settings_ts()
        handler_match = re.search(
            r'segc\.addEventListener\("click", \(\) => void reRenderAfter\(async \(\) => \{(.*?)\n    \}\)\);',
            source, re.S,
        )
        self.assertIsNotNone(handler_match, "找不到主题分段控件的点击处理函数")
        body = handler_match.group(1)

        persist_idx = body.find("await persistSettings({ appearance:")
        commit_idx = body.find("commitThemeToLocalStorage(option.value)")
        self.assertGreater(persist_idx, -1, "点击处理函数里没有落盘调用")
        self.assertGreater(commit_idx, -1, "点击处理函数里没有提交 localStorage 的调用")
        self.assertLess(
            persist_idx, commit_idx,
            "localStorage 必须在 persistSettings 成功之后才写，否则失败时本地/后端永久分叉（中-17）",
        )

        # 失败路径：必须捕获落盘异常、把可见主题回滚到点击前的值，再往外抛
        # （交给 reRenderAfter 统一重绘 + toast），不能吞掉异常导致失败也当成功处理。
        catch_match = re.search(r"catch \(error\) \{(.*?)\n\s*\}", body, re.S)
        self.assertIsNotNone(catch_match, "点击处理函数缺少落盘失败的回滚分支")
        catch_body = catch_match.group(1)
        self.assertIn("applyThemeVisual(previous)", catch_body)
        self.assertIn("throw error", catch_body)


class DomainPromptDraftNotWipedOnSaveErrorTests(unittest.TestCase):
    """REG-1（第二轮复审 mustFix）：中-14 给 reRenderAfter 的 catch 分支加了
    「失败也 renderBody()」，但这是无差别生效的——领域 Prompt 文本框
    （promptArea）不在 MODEL_FORM_DRAFT_FIELD_IDS 草稿名单里，用户敲了几百字
    自定义 Prompt、保存时后端短暂不可用，catch 一重绘文本就被 current.customPrompt
    重建，全文当场消失。修复：给 reRenderAfter 加 opts.rerenderOnError（默认
    false），只在数值框 / PDF 并发框的调用点显式传 true；领域 Prompt 的
    doSave / 恢复内置默认两个调用点必须保持默认（不重绘）。"""

    def test_rerender_after_catch_only_rerenders_when_explicitly_opted_in(self) -> None:
        source = _read_settings_ts()
        match = re.search(r"async function reRenderAfter<T>\(.*?\n\}", source, re.S)
        self.assertIsNotNone(match, "找不到 reRenderAfter 函数体")
        body = match.group(0)

        # 签名必须新增 rerenderOnError 开关。
        self.assertIn("rerenderOnError", body, "reRenderAfter 必须提供按调用点开启失败重绘的开关")

        catch_match = re.search(r"catch \(error\) \{(.*?)\n  \}\n\}", body, re.S)
        self.assertIsNotNone(catch_match, "reRenderAfter 里找不到 catch 分支")
        catch_body = catch_match.group(1)
        # 失败提示必须始终给出（这条不受开关影响）。
        self.assertIn("friendlyErrorMessage(error)", catch_body)
        # renderBody() 必须收在 `if (opts.rerenderOnError)` 之类的条件分支里。
        guarded_call = re.search(r"if\s*\(opts\.rerenderOnError\)\s*\{[^}]*renderBody\(\);", catch_body, re.S)
        self.assertIsNotNone(guarded_call, "renderBody() 必须由 opts.rerenderOnError 守卫")
        # 回归旧写法：renderBody() 紧跟在 showToast(...) 之后、不经任何 if 判断——
        # 用「守卫出现在 renderBody() 调用之前」代替整段结构匹配，避免正则本身太脆。
        renderbody_idx = catch_body.index("renderBody();")
        guard_idx = catch_body.index("if (opts.rerenderOnError)")
        self.assertLess(
            guard_idx, renderbody_idx,
            "catch 分支里 renderBody() 不能无条件执行，必须由 opts.rerenderOnError 显式开启（REG-1）",
        )

    def test_numeric_setting_paths_opt_into_rerender_on_error(self) -> None:
        """数值框保存失败后仍要被拉回磁盘值。"""
        source = _read_settings_ts()
        for path in (
            "word_batch.max_chars_per_batch",
            "word_batch.strict_retry_attempts",
            "pdf.page_retry_attempts",
            "pdf.page_generation_concurrency",
        ):
            call_match = re.search(
                r'reRenderAfter\([^;]*?saveSettingPath\("' + re.escape(path) + r'"[^;]*?\);',
                source, re.S,
            )
            self.assertIsNotNone(call_match, f"找不到 {path} 的 reRenderAfter 调用")
            self.assertIn(
                "rerenderOnError: true", call_match.group(0),
                f"{path} 的保存调用必须显式传 rerenderOnError: true，否则失败后表单值和磁盘不一致（中-14）",
            )

    def test_domain_prompt_save_does_not_opt_into_rerender_on_error(self) -> None:
        """回归钉子：领域 Prompt 的「保存」「恢复内置默认」两个按钮绝不能传
        rerenderOnError:true，否则用户未提交的草稿会在后端短暂不可用时被吞掉。"""
        source = _read_settings_ts()
        card_match = re.search(r"function renderDomainPromptCard\(.*?\n\}", source, re.S)
        self.assertIsNotNone(card_match, "找不到 renderDomainPromptCard")
        card_body = card_match.group(0)
        self.assertIn("promptArea", card_body)
        self.assertNotIn(
            "rerenderOnError: true", card_body,
            "领域 Prompt 卡片内的 reRenderAfter 调用不能开启失败重绘（REG-1）",
        )


class ThemeRealignsOnRefreshSettingsTests(unittest.TestCase):
    """中-17-残留（第二轮复审 mustFix）：「重置设置」等不经过主题分段控件、
    直接改后端 theme 的路径不会触发 applyThemeVisual/commitThemeToLocalStorage，
    界面和 localStorage 会永久停留在旧主题上，历史分叉的安装也永远修不回来。
    修复：refreshSettings() 末尾用刚拿到的后端权威值把可见主题和 localStorage
    对齐一次。"""

    def test_refresh_settings_realigns_visual_theme_and_local_storage(self) -> None:
        source = _read_settings_ts()
        match = re.search(r"async function refreshSettings\(\): Promise<void> \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match, "找不到 refreshSettings 函数体")
        body = match.group(0)
        self.assertIn(
            "applyThemeVisual(", body,
            "refreshSettings 必须用后端权威值刷新可见主题，否则重置设置之类的路径会留下本地/后端分叉",
        )
        self.assertIn(
            "commitThemeToLocalStorage(", body,
            "refreshSettings 必须同步刷新 localStorage 缓存，否则重启后仍按旧主题显示",
        )

    def test_reset_settings_maintenance_path_calls_refresh_settings(self) -> None:
        """维护页「重置设置」按钮必须走 refreshSettings()（而不是只读设置不刷主题），
        这是让上面那条对齐逻辑生效的前提。"""
        source = _read_settings_ts()
        match = re.search(
            r'category === "settings"\)\s*await refreshSettings\(\);',
            source,
        )
        self.assertIsNotNone(match, "重置设置的成功回调必须调用 refreshSettings()")


class ScopedDataHealthDismissSourceTests(unittest.TestCase):
    """W3 mustFix A8-R5 的前端半边（源码钉子）。

    两个成对的缺陷：
    1. 「知道了」曾整份清除 recovery.json——keys 的恢复事件是惰性补录的，横幅
       已在屏时新写入的事件从未展示过就被顺手抹掉，用户永远见不到通知。
       修复：横幅把「真正展示过的作用域」随 DELETE 一起送后端，按清单清除。
    2. 界面里写 Key 的三个入口（saveModel、deleteConnection、runModelConfigImport）
       成功后都要刷新 data health。模型连接现在逐条保存，saveModel 只有一个
       成功出口；惰性补录的 keys 事件不能等下一次整页刷新才露面。
    """

    @staticmethod
    def _function_body(source: str, name: str) -> str:
        match = re.search(
            r"async function " + re.escape(name) + r"\(.*?\n\}", source, re.S
        )
        assert match is not None, f"找不到 {name} 函数体"
        return match.group(0)

    def test_dismiss_sends_only_the_scopes_actually_rendered(self) -> None:
        source = _read_settings_ts()
        banner_match = re.search(
            r"function renderDataHealthBanner\(\).*?\n\}", source, re.S
        )
        self.assertIsNotNone(banner_match, "找不到 renderDataHealthBanner")
        body = banner_match.group(0)
        # blocked 在屏时 recreated 一条都没露面 → 一个作用域都不能清。
        self.assertIn(
            "const shownScopes = blocked.length ? [] : recreated.map((item) => item.scope);",
            body,
            "shownScopes 必须在 blocked 占屏时退化为空数组，否则未展示的事件会被清掉",
        )
        self.assertIn(
            'body: JSON.stringify({ scopes: shownScopes })',
            body,
            "「知道了」的 DELETE 必须带上真正展示过的作用域清单（A8-R5）",
        )

    def test_every_ui_key_write_path_refreshes_data_health(self) -> None:
        source = _read_settings_ts()
        # 模型连接逐条保存后，saveModel 只有一个成功出口。
        save_model = self._function_body(source, "saveModel")
        self.assertIn(
            "void refreshDataHealth(mountToken);", save_model,
            "saveModel 成功后必须刷新 data health（复审实测漏刷即丢通知）",
        )
        for name in ("deleteConnection", "runModelConfigImport"):
            body = self._function_body(source, name)
            self.assertIn(
                "void refreshDataHealth(mountToken);", body,
                f"{name} 成功后必须刷新 data health，keys 恢复事件是写 Key 时才惰性补录的",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
