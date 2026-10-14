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
from config import NO_EXTRA_PROMPT, SETTINGS_SCHEMA_VERSION
from core.engine_dispatcher import activate_translation_surface, get_system_prompt, smart_context_enabled
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


def test_extension_pages_occupy_only_the_translation_model():
    assert task_model_roles_for_page(AppSettings(), EXTENSION_PAGE_KEY) == (ROLE_TRANSLATION,)


class SettingsV39UpgradeTests(IsolatedAppDataTestCase):
    def test_a_v38_file_is_taken_over_unchanged_and_restamped_without_a_backup(self) -> None:
        payload = json.loads(_tuned().model_dump_json())
        payload.pop("extensions")
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
        self.assertEqual(SETTINGS_SCHEMA_VERSION, 39)
        self.assertEqual(stored.pop("extensions"), {})
        stored.pop("settings_version")
        payload.pop("settings_version")
        self.assertEqual(stored, payload)
        # Additive: nothing was rewritten, so there is nothing to back up.
        self.assertFalse((self.app_data_dir / "backups").exists())
        self.assertEqual(settings_module.get_settings_schema_status()["state"], "current")

    def test_extension_entries_survive_a_round_trip(self) -> None:
        settings = settings_module.load_settings()
        set_surface_setting(settings, "ext:srt", "options", {"keep_timing": False})
        settings_module.save_settings(settings)
        reloaded = settings_module.load_settings()
        self.assertEqual(reloaded.extensions["srt"].options, {"keep_timing": False})
        self.assertEqual(reloaded.extensions["srt"].target_lang, ExtensionSettings().target_lang)


if __name__ == "__main__":
    unittest.main()
