"""Exporting then importing a model configuration must not lose a role's state.

Translation can follow another role and the cleaner can run against a local
runner.  Both live in fields the exporter writes for all four roles, so the
reader has to pick them up for all four or a round-trip quietly resets them.
"""

from __future__ import annotations

import unittest

from core.model_config import (
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from core.model_roles import ROLE_TRANSLATION, add_role_connection
from settings import AppSettings


def _round_trip(settings: AppSettings) -> AppSettings:
    payload = build_model_config_export_payload(settings, get_api_key=lambda *_: "")
    imported = parse_model_config_import(payload)
    return apply_model_config_import(
        AppSettings(),
        imported,
        save_api_key=lambda *_args, **_kwargs: None,
    )


class ModelConfigRoundTripTests(unittest.TestCase):
    def test_translation_following_another_role_survives(self) -> None:
        settings = AppSettings()
        # Cleaner has to become independent first: following a follower is the
        # one shape the graph still rejects.
        settings.cleaner_model_role.source_role = "independent"
        settings.image_model_role.source_role = "independent"
        settings.pdf_review_model_role.source_role = "independent"
        settings.engine.source_role = "cleaner"

        restored = _round_trip(settings)
        assert restored.engine.source_role == "cleaner"

    def test_a_local_cleaner_survives(self) -> None:
        settings = AppSettings()
        settings.cleaner_model_role.source_role = "independent"
        settings.cleaner_model_role.mode = "local"
        settings.cleaner_model_role.local_provider = "lm_studio"
        settings.cleaner_model_role.local_model = "qwen2.5:7b"
        settings.cleaner_model_role.local_base_url = "http://127.0.0.1:1234/v1"

        restored = _round_trip(settings)
        cleaner = restored.cleaner_model_role
        assert cleaner.mode == "local"
        assert cleaner.local_provider == "lm_studio"
        assert cleaner.local_model == "qwen2.5:7b"
        assert cleaner.local_base_url == "http://127.0.0.1:1234/v1"

    def test_a_local_translation_still_survives(self) -> None:
        """The pre-existing translation-only behaviour must not regress."""
        settings = AppSettings()
        # The image and review roles follow translation by default and are
        # cloud-only, so they have to leave first for a local translation to be
        # a legal graph at all.  That rule predates this change.
        settings.image_model_role.source_role = "independent"
        settings.pdf_review_model_role.source_role = "independent"
        settings.engine.mode = "local"
        settings.engine.local_provider = "ollama"
        settings.engine.local_model = "qwen2.5:7b"

        restored = _round_trip(settings)
        assert restored.engine.mode == "local"
        assert restored.engine.local_model == "qwen2.5:7b"
        # The pre-rename field tracks local_model for the engine only.
        assert restored.engine.ollama_model == "qwen2.5:7b"

    def test_cloud_roles_stay_cloud(self) -> None:
        restored = _round_trip(AppSettings())
        assert restored.engine.mode == "cloud"
        assert restored.cleaner_model_role.mode == "cloud"
        assert restored.image_model_role.mode == "cloud"
        assert restored.pdf_review_model_role.mode == "cloud"

    def test_a_sparse_file_does_not_reset_an_unmentioned_mode(self) -> None:
        """A profile that names no mode must leave a configured local role alone."""
        settings = AppSettings()
        settings.cleaner_model_role.source_role = "independent"
        settings.cleaner_model_role.mode = "local"
        settings.cleaner_model_role.local_model = "qwen2.5:7b"

        payload = build_model_config_export_payload(
            settings, get_api_key=lambda *_: ""
        )
        del payload["model_profiles"]["cleaner"]["mode"]
        imported = parse_model_config_import(payload)
        restored = apply_model_config_import(
            settings,
            imported,
            save_api_key=lambda *_args, **_kwargs: None,
        )
        assert restored.cleaner_model_role.mode == "local"

    def test_every_connection_in_a_pool_travels_with_the_bundle(self) -> None:
        """The bundle promises the whole model service, pools included."""
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_base_url = "https://primary.example/v1"
        settings.engine.cloud_model = "model-primary"
        settings = AppSettings(**settings.model_dump())
        add_role_connection(
            settings,
            ROLE_TRANSLATION,
            label="备用",
            provider="custom_openai",
            model="model-second",
            base_url="https://second.example/v1",
        )
        settings = AppSettings(**settings.model_dump())

        restored = _round_trip(settings)

        pool = restored.engine.connections
        assert [conn.base_url for conn in pool] == [
            "https://primary.example/v1",
            "https://second.example/v1",
        ]
        assert pool[1].label == "备用"
        assert pool[1].model == "model-second"
        # Ids are machine-local key scopes and must not be carried over.
        assert pool[1].id != settings.engine.connections[1].id
        # Nothing in the file was ever tested on this machine.
        assert all(conn.availability_status == "unknown" for conn in pool)

    def test_a_file_without_a_pool_leaves_the_readers_own_pool_alone(self) -> None:
        """Bundles written before pools travelled must not collapse one."""
        reader = AppSettings()
        add_role_connection(reader, ROLE_TRANSLATION, label="本机备用")
        reader = AppSettings(**reader.model_dump())

        payload = build_model_config_export_payload(
            AppSettings(), get_api_key=lambda *_: ""
        )
        for profile in payload["model_profiles"].values():
            del profile["connections"]

        restored = apply_model_config_import(
            reader,
            parse_model_config_import(payload),
            save_api_key=lambda *_args, **_kwargs: None,
        )

        assert [conn.label for conn in restored.engine.connections] == ["", "本机备用"]


class ReplacedConnectionKeyCleanupTests(unittest.TestCase):
    """被整份替换掉的连接，它们的 ``conn::<id>`` 密钥不能留在密钥文件里。

    池是整份替换的，本机原来那几条连接的 id 在导入后不复存在。没有任何界面还
    能看见、能改、能删这些作用域下的密钥，留着就是一堆随每次导入不断增长、且
    属于别人的凭据。
    """

    def _import_with_a_pool(self, *, include_pool: bool) -> list[str]:
        reader = AppSettings()
        add_role_connection(reader, ROLE_TRANSLATION, label="本机备用")
        reader = AppSettings(**reader.model_dump())
        # 文件带着全部四个角色的池，所以四个角色的旧连接都会被替换掉。
        old_ids = [
            conn.id
            for owner in (
                reader.engine,
                reader.cleaner_model_role,
                reader.image_model_role,
                reader.pdf_review_model_role,
            )
            for conn in owner.connections
        ]

        payload = build_model_config_export_payload(
            AppSettings(), get_api_key=lambda *_: ""
        )
        if not include_pool:
            for profile in payload["model_profiles"].values():
                del profile["connections"]
        deleted: list[str] = []
        apply_model_config_import(
            reader,
            parse_model_config_import(payload),
            save_api_key=lambda *_args, **_kwargs: None,
            delete_scoped_api_key=deleted.append,
        )
        self.old_ids = old_ids
        return deleted

    def test_keys_of_replaced_connections_are_purged(self) -> None:
        deleted = self._import_with_a_pool(include_pool=True)

        self.assertEqual(sorted(deleted), sorted(self.old_ids))

    def test_nothing_is_purged_when_the_pool_is_left_alone(self) -> None:
        # 老文件不带 connections，池原封不动，密钥当然一个都不能动。
        deleted = self._import_with_a_pool(include_pool=False)

        self.assertEqual(deleted, [])


if __name__ == "__main__":
    unittest.main()
