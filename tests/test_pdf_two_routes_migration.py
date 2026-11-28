"""PDF 只剩两种翻译方式（v42，2026-11-28）：老设置文件怎样升级。

改前：「翻译方式」有图像还原 / 原位替换 / 转 Word / 自动组合，自动组合下还有「扫描页」「文字页」
「图纸和大图」「保底方式」四个子选项。
改后：只有「按页自动选择」（新用户默认）和「整页图像翻译」；按页自动选择逐页怎么选、用不了时怎么办都由
程序定，没有子选项。

老用户：选着图像的照旧是整页图像翻译；原位替换、转 Word、自动组合都改为按页自动选择。改过子选项的人
升级时先备份旧文件（子选项会被去掉），没改过的不备份。
"""
from __future__ import annotations

import json

from config import SETTINGS_SCHEMA_VERSION
import settings as settings_module
from tests.app_data_isolation import IsolatedAppDataTestCase


class PdfTwoRoutesMigrationTests(IsolatedAppDataTestCase):
    def _write(self, pdf: dict, version: int = 41) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"settings_version": version, "target_lang": "zh", "pdf": pdf}
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def _stored_pdf(self) -> dict:
        stored = json.loads((self.app_data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["settings_version"], SETTINGS_SCHEMA_VERSION)
        return stored["pdf"]

    def _backups(self) -> list:
        folder = self.app_data_dir / "backups" / "settings"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_schema_is_v42(self) -> None:
        self.assertEqual(SETTINGS_SCHEMA_VERSION, 42)

    def test_new_install_defaults_to_smart_mix(self) -> None:
        self.assertEqual(settings_module.load_settings().pdf.translation_route, "auto")

    def test_image_users_stay_on_full_page_image(self) -> None:
        # 存着 image 的，和从没改过翻译方式（旧文件里没有这一项，那时的默认是 image）的，都照旧
        for pdf in ({"translation_route": "image"}, {}):
            with self.subTest(pdf=pdf):
                self._write(pdf)
                loaded = settings_module.load_settings()
                self.assertEqual(loaded.pdf.translation_route, "image")
                settings_module.save_settings(loaded)
                self.assertEqual(self._stored_pdf()["translation_route"], "image")
        self.assertEqual(self._backups(), [])

    def test_every_other_route_becomes_smart_mix_and_the_sub_options_go(self) -> None:
        for route in ("inplace", "word", "auto"):
            with self.subTest(route=route):
                self._write({"translation_route": route})
                loaded = settings_module.load_settings()
                self.assertEqual(loaded.pdf.translation_route, "auto")
                settings_module.save_settings(loaded)
                pdf = self._stored_pdf()
                self.assertEqual(pdf["translation_route"], "auto")
                for gone in ("auto_scan_route", "auto_text_route", "auto_drawing_route", "auto_fallback"):
                    self.assertNotIn(gone, pdf)

    def test_untouched_smart_mix_is_not_backed_up(self) -> None:
        self._write({"translation_route": "auto", "auto_scan_route": "image", "auto_text_route": "word",
                     "auto_drawing_route": "inplace", "auto_fallback": "image"})
        settings_module.save_settings(settings_module.load_settings())
        self.assertEqual(self._backups(), [])

    def test_a_choice_that_is_dropped_backs_up_the_old_file_first(self) -> None:
        old = {"translation_route": "auto", "auto_drawing_route": "skip", "auto_fallback": "skip"}
        self._write(old)
        settings_module.save_settings(settings_module.load_settings())
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["pdf"], old)
        self.assertEqual(self._stored_pdf()["translation_route"], "auto")

    def test_a_single_route_that_is_merged_is_backed_up(self) -> None:
        # 选着「原位替换」或「转 Word」的人换成了按页自动选择：留一份旧文件。
        self._write({"translation_route": "word"})
        settings_module.save_settings(settings_module.load_settings())
        self.assertEqual(len(self._backups()), 1)

    def test_a_v42_file_is_left_alone(self) -> None:
        self._write({"translation_route": "image"}, version=42)
        self.assertEqual(settings_module.load_settings().pdf.translation_route, "image")
