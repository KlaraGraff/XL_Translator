"""Per-page settings through one accessor (settings v39).

Built-in pages keep their flat ``<key>_<name>`` fields; a page registered at run
time (``ext:<id>``) keeps its share in ``settings.extensions[<id>]``.  The
accessor must read the built-in fields exactly as the ``getattr`` calls it
replaced, so nothing about the four built-in pages changes.
"""

from __future__ import annotations

import json
import unittest

import pytest

import settings as settings_module
from config import CORE_TRANSLATION_PROMPT, NO_EXTRA_PROMPT, SETTINGS_SCHEMA_VERSION
from core.engine_dispatcher import (
    activate_translation_surface,
    core_prompt_for,
    get_system_prompt,
    smart_context_enabled,
)
from core.model_api_identity import task_model_roles_for_page
from core.model_roles import ROLE_TRANSLATION
from core.surface_settings import (
    EXTENSION_PAGE_KEY,
    extension_id,
    set_surface_setting,
    surface_setting,
)
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase

_NAMES = (
    "source_lang",
    "target_lang",
    "domain_preset",
    "smart_context",
    "custom_prompt",
    "domain_name_overrides",
    "domain_prompt_overrides",
    "domain_custom_prompts",
    "domain_disabled_presets",
)


def _tuned() -> AppSettings:
    settings = AppSettings(
        excel_target_lang="de",
        word_source_lang="en",
        cad_target_lang="fr",
        excel_smart_context=False,
        word_domain_preset="合同",
        word_domain_custom_prompts={"合同": {"_base": "按合同用语翻译"}},
    )
    settings.extensions["srt"] = ExtensionSettings(
        target_lang="en",
        source_lang="zh",
        domain_preset="字幕",
        smart_context=False,
        domain_custom_prompts={"字幕": {"_base": "每行不超过 42 个字符"}},
        options={"keep_timing": True},
    )
    return settings


@pytest.mark.parametrize("key", ["excel", "word", "pdf", "cad", "tm_clean", "EXCEL "])
@pytest.mark.parametrize("name", _NAMES)
def test_builtin_pages_read_exactly_like_getattr(key: str, name: str):
    settings = _tuned()
    attribute = f"{key.strip().lower()}_{name}"
    sentinel = object()
    assert surface_setting(settings, key, name, sentinel) is getattr(settings, attribute, sentinel)
    if hasattr(settings, attribute):
        assert surface_setting(settings, key, name) == getattr(settings, attribute)
    else:
        with pytest.raises(AttributeError):
            surface_setting(settings, key, name)


def test_extension_pages_read_their_own_entry():
    settings = _tuned()
    assert extension_id("ext:SRT") == "srt"
    assert extension_id("excel") is None
    assert extension_id("ext:") is None
    assert surface_setting(settings, "ext:srt", "target_lang") == "en"
    assert surface_setting(settings, "ext:srt", "source_lang") == "zh"
    assert surface_setting(settings, "ext:srt", "options") == {"keep_timing": True}
    # Not a field of an extension: the default, as getattr would give.
    assert surface_setting(settings, "ext:srt", "custom_prompt", "global") == "global"
    with pytest.raises(AttributeError):
        surface_setting(settings, "ext:srt", "custom_prompt")
    # Nothing stored yet: the defaults, and nothing is written back.
    assert surface_setting(settings, "ext:md", "source_lang") == "auto"
    assert surface_setting(settings, "ext:md", "pinned") is True
    assert "md" not in settings.extensions


def test_setting_a_value_creates_the_entry_and_rejects_unknown_names():
    settings = AppSettings()
    set_surface_setting(settings, "ext:md", "target_lang", "fr")
    assert settings.extensions["md"].target_lang == "fr"
    set_surface_setting(settings, "word", "domain_preset", "合同")
    assert settings.word_domain_preset == "合同"
    with pytest.raises(AttributeError):
        set_surface_setting(settings, "ext:md", "word_domain_preset", "x")
    with pytest.raises(AttributeError):
        set_surface_setting(settings, "pdf", "domain_preset", "x")


def test_prompt_and_auto_detect_follow_the_extension_entry():
    settings = _tuned()
    prompt = get_system_prompt(settings, target_lang="en", page_key="ext:srt")
    assert "每行不超过 42 个字符" in prompt
    assert smart_context_enabled(settings, "ext:srt") is False
    assert smart_context_enabled(settings, "ext:md") is True
    # A page without its own prompt settings keeps using the global ones.
    assert get_system_prompt(settings, target_lang="en", page_key="pdf") == get_system_prompt(
        settings, target_lang="en", page_key=""
    )

    activated = activate_translation_surface(settings.model_copy(deep=True), "ext:srt")
    assert activated.domain_preset == "字幕"
    assert activated.domain_smart_context is False
    assert activated.domain_custom_prompts == {"字幕": {"_base": "每行不超过 42 个字符"}}
    # Legacy fields an extension never had fall back to the global values.
    assert activated.custom_prompt == settings.custom_prompt
    assert activated.domain_disabled_presets == list(settings.domain_disabled_presets)

    untouched = activate_translation_surface(settings.model_copy(deep=True), "ext:md")
    assert untouched.domain_preset == NO_EXTRA_PROMPT
    assert untouched.domain_custom_prompts == {}


def test_extension_pages_can_replace_the_core_rules_and_built_in_pages_cannot():
    settings = _tuned()
    assert core_prompt_for(settings, "ext:srt") == CORE_TRANSLATION_PROMPT
    # The manifest's prompt is the default for its type.
    assert core_prompt_for(settings, "ext:srt", "只译台词。") == "只译台词。"
    set_surface_setting(settings, "ext:srt", "prompt", "  只译对白，不译音效说明。 ")
    assert core_prompt_for(settings, "ext:srt", "只译台词。") == "只译对白，不译音效说明。"
    prompt = get_system_prompt(settings, target_lang="en", page_key="ext:srt", default_core_prompt="只译台词。")
    assert prompt.startswith("只译对白，不译音效说明。")
    assert CORE_TRANSLATION_PROMPT not in prompt
    # The selected 补充要求 is still appended after the replaced rules.
    assert "每行不超过 42 个字符" in prompt
    # Other types keep their defaults; built-in pages ignore both.
    assert core_prompt_for(settings, "ext:md") == CORE_TRANSLATION_PROMPT
    assert core_prompt_for(settings, "excel", "只译台词。") == CORE_TRANSLATION_PROMPT
    assert get_system_prompt(settings, target_lang="en", page_key="excel").startswith(CORE_TRANSLATION_PROMPT)
    # Clearing the box goes back to the default.
    set_surface_setting(settings, "ext:srt", "prompt", "   ")
    assert core_prompt_for(settings, "ext:srt", "只译台词。") == "只译台词。"


def test_translation_types_keys_are_normalized():
    table = AppSettings.model_validate(
        {"translation_types": {
            "order": [" Excel", "ext:md", "excel", "", 3],
            "visible": {"ext:MD ": True, "word": False, "pdf": "yes", "": True},
        }}
    ).translation_types
    assert table.order == ["excel", "ext:md"]
    assert table.visible == {"ext:md": True, "word": False}


def test_built_ins_show_by_default_and_extension_types_do_not():
    table = AppSettings().translation_types
    assert [table.shows(key) for key in ("excel", "word", "pdf", "cad")] == [True] * 4
    assert table.shows("ext:text") is False
    table = AppSettings.model_validate(
        {"translation_types": {"visible": {"excel": False, "ext:text": True}}}
    ).translation_types
    assert table.shows("excel") is False and table.shows("EXT:text") is True and table.shows("ext:srt") is False


def test_extension_pages_occupy_only_the_translation_model():
    assert task_model_roles_for_page(AppSettings(), EXTENSION_PAGE_KEY) == (ROLE_TRANSLATION,)


class SettingsUpgradeTests(IsolatedAppDataTestCase):
    def test_a_v38_file_is_taken_over_unchanged_and_restamped_without_a_backup(self) -> None:
        payload = json.loads(_tuned().model_dump_json())
        payload.pop("extensions")
        payload.pop("translation_types")
        payload["settings_version"] = 38
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        path = self.app_data_dir / "settings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(settings_module.get_settings_schema_status()["state"], "adopted")
        loaded = settings_module.load_settings()
        self.assertEqual(loaded.extensions, {})
        self.assertEqual(loaded.excel_target_lang, "de")
        self.assertEqual(loaded.word_domain_custom_prompts, {"合同": {"_base": "按合同用语翻译"}})

        settings_module.save_settings(loaded)
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(SETTINGS_SCHEMA_VERSION, 40)
        self.assertEqual(stored.pop("extensions"), {})
        self.assertEqual(stored.pop("translation_types"), {"order": [], "visible": {}})
        stored.pop("settings_version")
        payload.pop("settings_version")
        self.assertEqual(stored, payload)
        # Additive: nothing was rewritten, so there is nothing to back up.
        self.assertFalse((self.app_data_dir / "backups").exists())
        self.assertEqual(settings_module.get_settings_schema_status()["state"], "current")

    def _write_v39(self, extensions: dict) -> None:
        payload = json.loads(_tuned().model_dump_json())
        payload.pop("translation_types")
        payload["extensions"] = extensions
        payload["settings_version"] = 39
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_a_v39_file_moves_the_order_into_one_table_and_extension_types_start_hidden(self) -> None:
        self._write_v39({
            "srt": {"domain_preset": "字幕", "pinned": False, "order": 1},
            "md": {"pinned": True, "order": 0},
            "csv": {"order": 2},
        })
        self.assertEqual(settings_module.get_settings_schema_status()["state"], "adopted")
        loaded = settings_module.load_settings()
        self.assertEqual(loaded.translation_types.visible, {})
        self.assertEqual(
            [loaded.translation_types.shows(key) for key in ("excel", "ext:srt", "ext:md", "ext:csv")],
            [True, False, False, False],
        )
        self.assertEqual(
            loaded.translation_types.order,
            ["excel", "word", "pdf", "cad", "ext:md", "ext:srt", "ext:csv"],
        )
        self.assertEqual(loaded.extensions["srt"].prompt, "")
        self.assertEqual(loaded.extensions["srt"].domain_preset, "字幕")
        # Saved once (any edit), the table stays and the old fields are left as they were.
        loaded.extensions["md"].target_lang = "de"
        settings_module.save_settings(loaded)
        stored = json.loads((self.app_data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertEqual(stored["translation_types"]["order"][-3:], ["ext:md", "ext:srt", "ext:csv"])
        self.assertIs(stored["extensions"]["srt"]["pinned"], False)
        self.assertFalse((self.app_data_dir / "backups").exists())
        reloaded = settings_module.load_settings()
        self.assertEqual(reloaded.translation_types.order, loaded.translation_types.order)
        self.assertEqual(settings_module.get_settings_schema_status()["state"], "current")

    def test_a_v39_file_with_default_positions_keeps_the_default_order(self) -> None:
        self._write_v39({"srt": {"order": 0}, "md": {}})
        loaded = settings_module.load_settings()
        self.assertEqual(loaded.translation_types.order, [])
        self.assertEqual(loaded.translation_types.visible, {})

    def test_extension_entries_survive_a_round_trip(self) -> None:
        settings = settings_module.load_settings()
        set_surface_setting(settings, "ext:srt", "options", {"keep_timing": False})
        settings_module.save_settings(settings)
        reloaded = settings_module.load_settings()
        self.assertEqual(reloaded.extensions["srt"].options, {"keep_timing": False})
        self.assertEqual(reloaded.extensions["srt"].target_lang, ExtensionSettings().target_lang)


if __name__ == "__main__":
    unittest.main()
