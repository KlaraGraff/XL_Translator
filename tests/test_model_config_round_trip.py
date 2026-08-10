"""Exporting then importing a model configuration must not lose a role's state.

Translation can follow another role and the cleaner can run against a local
runner.  Both live in fields the exporter writes for all four roles, so the
reader has to pick them up for all four or a round-trip quietly resets them.
"""

from __future__ import annotations

import unittest

from core.model_config import (
    MODEL_CONFIG_EXPORT_VERSION,
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


class ExportVersionTests(unittest.TestCase):
    """旧版本导出的文件必须读得进来，新版本导出的才该被拒。

    这些文件常常是用户唯一的一份配置备份，一次格式升级不能把它们全作废。
    """

    def test_an_older_bundle_still_imports(self) -> None:
        payload = build_model_config_export_payload(
            AppSettings(), get_api_key=lambda *_: ""
        )
        payload["version"] = MODEL_CONFIG_EXPORT_VERSION - 1

        imported = parse_model_config_import(payload)

        self.assertTrue(imported.model_config)

    def test_a_newer_bundle_is_refused_and_says_why(self) -> None:
        payload = build_model_config_export_payload(
            AppSettings(), get_api_key=lambda *_: ""
        )
        payload["version"] = MODEL_CONFIG_EXPORT_VERSION + 1

        with self.assertRaises(ValueError) as raised:
            parse_model_config_import(payload)

        self.assertIn("更新版本", str(raised.exception))

    def test_a_file_without_a_version_is_still_rejected(self) -> None:
        payload = build_model_config_export_payload(
            AppSettings(), get_api_key=lambda *_: ""
        )
        del payload["version"]

        with self.assertRaises(ValueError):
            parse_model_config_import(payload)


class WithKeysRoundTripTests(unittest.TestCase):
    """「导出含 Key」再导入，主用连接必须还是拿自己的 Key。"""

    def test_a_backup_on_the_same_endpoint_does_not_take_over_the_primary_key(
        self,
    ) -> None:
        settings = AppSettings()
        settings.engine.cloud_provider = "custom_openai"
        settings.engine.cloud_base_url = "https://vendor.example/v1"
        settings.engine.cloud_model = "vendor-model"
        settings = AppSettings(**settings.model_dump())
        add_role_connection(
            settings,
            ROLE_TRANSLATION,
            label="备用账号",
            provider="custom_openai",
            model="vendor-model",
            base_url="https://vendor.example/v1",
        )
        settings = AppSettings(**settings.model_dump())
        ids = [conn.id for conn in settings.engine.connections]
        scoped = {ids[0]: "sk-PRIMARY", ids[1]: "sk-BACKUP"}

        payload = build_model_config_export_payload(
            settings,
            get_api_key=lambda *_: "",
            get_scoped_api_key=lambda connection_id: scoped.get(connection_id, ""),
            include_api_key=True,
        )
        saved: dict[tuple[str, str], str] = {}

        def _save(provider: str, api_key: str, base_url: str = "") -> None:
            saved[(provider, base_url)] = api_key

        apply_model_config_import(
            AppSettings(),
            parse_model_config_import(payload),
            save_api_key=_save,
        )

        # 两条连接共用一个 provider + Base URL 作用域，文件里没有 id 能把它们分开，
        # 所以只有一把 Key 存得下——那必须是主用连接的。
        self.assertEqual(
            saved.get(("custom_openai", "https://vendor.example/v1")),
            "sk-PRIMARY",
        )


class ReplacedConnectionKeyCleanupTests(unittest.TestCase):
    """只有真正失去主人的 ``conn::<id>`` 密钥才该被删。

    池是整份替换的，导出文件里也不带 id。要是每次导入都给每条连接换一个新 id，
    本机存着的密钥就全成了没人指得到的孤儿——「导出（不含 Key）再原样导入回来」
    会把每条连接的 Key 都清空。反过来，槽位真的消失了、或者换成了另一个端点，
    那条 id 就再没有任何界面能看见、能改、能删，留着只会随每次导入越堆越多。
    """

    def _import_with_a_pool(self, *, include_pool: bool) -> list[str]:
        reader = AppSettings()
        add_role_connection(reader, ROLE_TRANSLATION, label="本机备用")
        reader = AppSettings(**reader.model_dump())
        self.old_ids = [
            conn.id
            for owner in (
                reader.engine,
                reader.cleaner_model_role,
                reader.image_model_role,
                reader.pdf_review_model_role,
            )
            for conn in owner.connections
        ]
        # 文件只描述每个角色一条连接，所以本机多出来的那条备用没有对应槽位。
        self.orphan_id = reader.engine.connections[1].id

        payload = build_model_config_export_payload(
            AppSettings(), get_api_key=lambda *_: ""
        )
        if not include_pool:
            for profile in payload["model_profiles"].values():
                del profile["connections"]
        deleted: list[str] = []
        self.restored = apply_model_config_import(
            reader,
            parse_model_config_import(payload),
            save_api_key=lambda *_args, **_kwargs: None,
            delete_scoped_api_key=deleted.append,
        )
        return deleted

    def test_only_the_slot_that_disappeared_is_purged(self) -> None:
        deleted = self._import_with_a_pool(include_pool=True)

        self.assertEqual(deleted, [self.orphan_id])

    def test_an_unchanged_endpoint_keeps_its_connection_id(self) -> None:
        """自家导出原样导回来，密钥必须还在原地。"""
        self._import_with_a_pool(include_pool=True)

        self.assertEqual(
            self.restored.engine.connections[0].id,
            self.old_ids[0],
        )

    def test_a_changed_endpoint_gives_up_its_key(self) -> None:
        reader = AppSettings()
        reader.engine.cloud_provider = "custom_openai"
        reader.engine.cloud_base_url = "https://old-vendor.example/v1"
        reader = AppSettings(**reader.model_dump())
        old_id = reader.engine.connections[0].id

        donor = AppSettings()
        donor.engine.cloud_provider = "custom_openai"
        donor.engine.cloud_base_url = "https://new-vendor.example/v1"
        donor = AppSettings(**donor.model_dump())
        payload = build_model_config_export_payload(donor, get_api_key=lambda *_: "")

        deleted: list[str] = []
        restored = apply_model_config_import(
            reader,
            parse_model_config_import(payload),
            save_api_key=lambda *_args, **_kwargs: None,
            delete_scoped_api_key=deleted.append,
        )

        self.assertIn(old_id, deleted)
        self.assertNotEqual(restored.engine.connections[0].id, old_id)

    def test_nothing_is_purged_when_the_pool_is_left_alone(self) -> None:
        # 老文件不带 connections，池原封不动，密钥当然一个都不能动。
        deleted = self._import_with_a_pool(include_pool=False)

        self.assertEqual(deleted, [])


if __name__ == "__main__":
    unittest.main()
