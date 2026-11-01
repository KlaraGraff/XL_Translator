"""Item 107（第 2 项）: ``model_throughput`` 的裸共享状态——两次保存互相覆盖。

docs/CODE_AUDIT_2026-08-31.md:1131 所列 5 项「未及验证」的第 2 项：审计说
``set_model_throughput`` 直接改 ``AppSettings`` 上的字典、``get_model_throughput``
会被 worker 线程同时读，「看着像典型的裸共享状态」，但没构造出具体后果，所以没报。

后果可以构造，而且不需要真的并发：``PUT /api/models/throughput/{role}`` 是
「load_settings → set_model_throughput → save_settings」，而
``model_throughput_profiles`` 在 ``save_settings`` 的 delta 里是**整份字典替换**。
两个请求各自基于自己那份旧快照写回时，后写的把先写的那一格整份抹掉：

    用户把每批条数改成 25，紧接着把同时请求数改成 3；
    结果并发 3 生效，每批条数悄悄回到 20（默认值）。

下面按接口的真实顺序串行复现（并发只是把这个窗口变成必然，串行已经足够）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import settings as settings_module
from core.model_roles import EffectiveModelConfig
from core.model_throughput import set_model_throughput
from settings import AppSettings

_KEY = "translation|cloud|custom_openai|https://api.example/v1|m1"


def _config() -> EffectiveModelConfig:
    return EffectiveModelConfig(
        role="translation",
        label="翻译",
        capability="text",
        mode="cloud",
        provider="custom_openai",
        model="m1",
        base_url="https://api.example/v1",
        api_key="sk-test",
    )


class ThroughputProfileLostUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for patcher in (
            patch.object(settings_module, "APP_DATA_DIR", root),
            patch.object(settings_module, "SETTINGS_PATH", root / "settings.json"),
            patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        settings_module.save_settings(AppSettings())

    def test_two_requests_tuning_the_same_model_keep_both_values(self) -> None:
        """两个请求各自 load → 改一项 → save，正是「测试连接」那种重叠的窗形。

        FastAPI 把同步端点丢进线程池，两次 PUT 可以完全重叠：B 的
        ``load_settings()`` 发生在 A 落盘之前，于是 B 手里那份快照里这一格还是
        默认值；``save_settings`` 对 ``model_throughput_profiles`` 按整份字典替换，
        B 一落盘就把 A 改的数值抹掉了。
        """
        config = _config()

        # 两个请求都先读到「改之前」的那份快照。
        first = settings_module.load_settings()
        second = settings_module.load_settings()

        set_model_throughput(first, config, batch_size=25)   # 请求 A：每批条数
        set_model_throughput(second, config, concurrency=3)  # 请求 B：同时请求数

        settings_module.save_settings(first)
        settings_module.save_settings(second)

        profile = settings_module.load_settings().model_throughput_profiles.get(_KEY)
        self.assertIsNotNone(profile, f"档案 {_KEY} 整个不见了")
        self.assertEqual(
            (profile.batch_size, profile.concurrency),
            (25, 3),
            "两次保存互相覆盖：后一次把同一档案里前一次改的数值整份抹掉了"
            "（model_throughput 的读—改—写没有锁，delta 又按整份字典替换）",
        )

    def test_a_single_load_mutate_save_round_trip_is_fine(self) -> None:
        """对照：同一次请求里改两项不会丢，问题只出在两次请求之间。"""
        config = _config()
        one = settings_module.load_settings()
        set_model_throughput(one, config, batch_size=25)
        set_model_throughput(one, config, concurrency=3)
        settings_module.save_settings(one)

        profile = settings_module.load_settings().model_throughput_profiles.get(_KEY)
        self.assertEqual((profile.batch_size, profile.concurrency), (25, 3))


if __name__ == "__main__":
    unittest.main()
