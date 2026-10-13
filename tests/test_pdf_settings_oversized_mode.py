from __future__ import annotations

import unittest

from settings import PdfSettings


class PdfSettingsOversizedModeMigrationTests(unittest.TestCase):
    """PdfSettings 的大幅面页设置迁移：旧字段 skip_oversized_pages 推出新字段
    oversized_page_mode（没有新字段的旧 settings.json 读进来时），以及新字段落地后
    旧字段怎么跟着同步——这两段逻辑都在 PdfSettings 的 before/after 两个
    model_validator 里（见 settings.py 对应注释），这里直接单测，不经过磁盘。
    """

    def test_missing_new_field_with_skip_false_migrates_to_image_mode(self) -> None:
        # 旧版本只存过 skip_oversized_pages=False（用户当年关掉了「跳过」，也就是
        # 「整页图像翻译」）；新字段完全不存在时，必须按这个旧值推出 "image"，不能
        # 悄悄回落成新默认值 "skip"（那会让已经关掉跳过的用户的大幅面页突然被跳过）。
        settings = PdfSettings(skip_oversized_pages=False)
        self.assertEqual(settings.oversized_page_mode, "image")
        # 新旧字段必须互相一致：after-validator 按新字段重新派生旧字段。
        self.assertFalse(settings.skip_oversized_pages)

    def test_all_fields_missing_defaults_to_skip_mode(self) -> None:
        # 全新安装（两个字段都没有）：默认行为必须和升级前完全一样——跳过大幅面页。
        settings = PdfSettings()
        self.assertEqual(settings.oversized_page_mode, "skip")
        self.assertTrue(settings.skip_oversized_pages)
        self.assertEqual(settings.oversized_page_threshold, "A3")
        self.assertEqual(settings.normal_page_mode, "image")

    def test_inplace_mode_keeps_legacy_skip_flag_true(self) -> None:
        # "原位替换"：大幅面页仍然不送图像模型重新生成（只是换一种不送的方式），
        # 所以旧的 skip_oversized_pages 读出来必须还是 True——任何还在看这个旧字段的
        # 代码路径不能把「原位替换」误当成「整页图像翻译」处理。
        settings = PdfSettings(oversized_page_mode="inplace")
        self.assertEqual(settings.oversized_page_mode, "inplace")
        self.assertTrue(settings.skip_oversized_pages)

        # 显式传入互相矛盾的旧字段（False）也不能把新字段 "inplace" 冲掉；
        # after-validator 以新字段为准重新派生旧字段。
        settings_conflicting = PdfSettings(oversized_page_mode="inplace", skip_oversized_pages=False)
        self.assertEqual(settings_conflicting.oversized_page_mode, "inplace")
        self.assertTrue(settings_conflicting.skip_oversized_pages)


if __name__ == "__main__":
    unittest.main()


class PdfSettingsSavedThroughTheSettingsApiTests(unittest.TestCase):
    """The settings endpoint merges a patch into the current settings and
    validates the whole thing; the stale legacy flag must not undo the choice."""

    def _save(self, current: dict, patch: dict) -> PdfSettings:
        from api.routes.settings import _deep_merge
        from settings import AppSettings

        base = AppSettings.model_validate(current).model_dump(mode="json")
        merged = _deep_merge(base, {"pdf": patch})
        return AppSettings.model_validate(merged).pdf

    def test_every_mode_change_round_trips(self) -> None:
        for start in ("skip", "inplace", "image"):
            for target in ("skip", "inplace", "image"):
                with self.subTest(start=start, target=target):
                    pdf = self._save({"pdf": {"oversized_page_mode": start}}, {"oversized_page_mode": target})
                    self.assertEqual(pdf.oversized_page_mode, target)
                    self.assertEqual(pdf.skip_oversized_pages, target != "image")

    def test_old_file_with_skip_off_can_switch_back_to_skip(self) -> None:
        pdf = self._save({"pdf": {"skip_oversized_pages": False}}, {"oversized_page_mode": "skip"})
        self.assertEqual(pdf.oversized_page_mode, "skip")
