"""Editing a pool keeps the primary and the legacy fields in agreement."""

from __future__ import annotations

import unittest

from core.model_roles import (
    ROLE_TRANSLATION,
    ModelRoleConfigError,
    add_role_connection,
    remove_role_connection,
    reorder_role_connections,
    update_role_connection,
)
from settings import AppSettings


def _revalidated(app: AppSettings) -> AppSettings:
    """Round-trip through the validator, as saving and reloading would."""
    return AppSettings(**app.model_dump())


def _app() -> AppSettings:
    app = AppSettings()
    app.engine.mode = "cloud"
    app.engine.cloud_provider = "custom_openai"
    app.engine.cloud_base_url = "https://a.example/v1"
    app.engine.cloud_model = "model-a"
    return _revalidated(app)


class ConnectionPoolEditingTests(unittest.TestCase):
    def test_adding_a_connection_leaves_the_primary_alone(self) -> None:
        app = _app()
        add_role_connection(
            app, ROLE_TRANSLATION, label="B", base_url="https://b.example/v1"
        )
        app = _revalidated(app)
        self.assertEqual(
            [c.base_url for c in app.engine.connections],
            ["https://a.example/v1", "https://b.example/v1"],
        )
        self.assertEqual(app.engine.cloud_base_url, "https://a.example/v1")

    def test_reordering_promotes_the_new_primary_into_the_legacy_fields(self) -> None:
        app = _app()
        add_role_connection(
            app,
            ROLE_TRANSLATION,
            label="B",
            model="model-b",
            base_url="https://b.example/v1",
        )
        app = _revalidated(app)
        ids = [c.id for c in app.engine.connections]

        reorder_role_connections(app, ROLE_TRANSLATION, [ids[1], ids[0]])
        app = _revalidated(app)

        # Without pushing the new primary outwards first, the validator would
        # put the old values straight back.
        self.assertEqual(app.engine.connections[0].base_url, "https://b.example/v1")
        self.assertEqual(app.engine.cloud_base_url, "https://b.example/v1")
        self.assertEqual(app.engine.cloud_model, "model-b")

    def test_removing_the_primary_promotes_the_next_entry(self) -> None:
        app = _app()
        add_role_connection(
            app,
            ROLE_TRANSLATION,
            label="B",
            model="model-b",
            base_url="https://b.example/v1",
        )
        app = _revalidated(app)
        primary_id = app.engine.connections[0].id

        remove_role_connection(app, ROLE_TRANSLATION, primary_id)
        app = _revalidated(app)

        self.assertEqual(len(app.engine.connections), 1)
        self.assertEqual(app.engine.cloud_base_url, "https://b.example/v1")

    def test_the_last_connection_cannot_be_removed(self) -> None:
        app = _app()
        with self.assertRaises(ModelRoleConfigError):
            remove_role_connection(app, ROLE_TRANSLATION, app.engine.connections[0].id)

    def test_changing_an_endpoint_clears_its_test_result(self) -> None:
        app = _app()
        app.engine.connections[0].availability_status = "available"
        app.engine.connections[0].availability_message = "ok"
        update_role_connection(
            app,
            ROLE_TRANSLATION,
            app.engine.connections[0].id,
            base_url="https://moved.example/v1",
        )
        app = _revalidated(app)
        self.assertEqual(app.engine.connections[0].availability_status, "unknown")
        self.assertEqual(app.engine.cloud_base_url, "https://moved.example/v1")

    def test_renaming_a_connection_keeps_its_test_result(self) -> None:
        app = _app()
        app.engine.connections[0].availability_status = "available"
        renamed = update_role_connection(
            app, ROLE_TRANSLATION, app.engine.connections[0].id, label="主账号"
        )
        self.assertEqual(renamed.label, "主账号")
        self.assertEqual(renamed.availability_status, "available")

    def test_resubmitting_the_same_endpoint_keeps_the_test_result(self) -> None:
        """面板每次保存都整份提交这三个字段，值没变就不该判为「换了端点」。"""
        app = _app()
        connection = app.engine.connections[0]
        connection.availability_status = "available"
        connection.availability_message = "ok"

        update_role_connection(
            app,
            ROLE_TRANSLATION,
            connection.id,
            label="主账号",
            provider=connection.provider,
            model=connection.model,
            base_url=connection.base_url,
        )

        self.assertEqual(connection.label, "主账号")
        self.assertEqual(connection.availability_status, "available")
        self.assertEqual(connection.availability_message, "ok")

    def test_reorder_must_cover_exactly_the_current_entries(self) -> None:
        app = _app()
        with self.assertRaises(ModelRoleConfigError):
            reorder_role_connections(app, ROLE_TRANSLATION, ["nope"])

    def test_pools_of_different_roles_do_not_interfere(self) -> None:
        app = _app()
        add_role_connection(
            app, "cleaner", label="清洗备用", base_url="https://c.example/v1"
        )
        app = _revalidated(app)
        self.assertEqual(len(app.engine.connections), 1)
        self.assertEqual(len(app.cleaner_model_role.connections), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
