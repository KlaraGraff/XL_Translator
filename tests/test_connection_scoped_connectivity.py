"""A connectivity test belongs to the connection the panel is showing.

Before this, ``check_connectivity`` always dialled the role's primary and wrote
the verdict onto the role itself.  Two visible defects followed: a freshly added
connection displayed the primary's 「测试通过」 without ever having been tested,
and pressing 测试连接 on that connection reported on the primary instead.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.connectivity_check as connectivity_module
from api.app import create_app
from core.connectivity_check import ConnectivityResult, check_connectivity
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_TRANSLATION,
    add_role_connection,
    record_model_role_availability,
    resolve_effective_model_config,
)
from settings import (
    AppSettings,
    current_key_overrides,
    save_connection_key,
    save_key,
)
from tests.app_data_isolation import IsolatedAppDataTestCase


def _revalidated(app: AppSettings) -> AppSettings:
    return AppSettings(**app.model_dump())


def _app_with_two_connections() -> tuple[AppSettings, str, str]:
    app = AppSettings()
    app.engine.mode = "cloud"
    app.engine.cloud_provider = "custom_openai"
    app.engine.cloud_base_url = "https://primary.example/v1"
    app.engine.cloud_model = "model-primary"
    app = _revalidated(app)
    add_role_connection(
        app,
        ROLE_TRANSLATION,
        label="第二条",
        model="model-second",
        base_url="https://second.example/v1",
    )
    app = _revalidated(app)
    ids = [conn.id for conn in app.engine.connections]
    return app, ids[0], ids[1]


class ConnectionScopedAvailabilityTests(unittest.TestCase):
    def test_a_second_connection_does_not_inherit_the_primary_verdict(self) -> None:
        app, _primary_id, second_id = _app_with_two_connections()
        record_model_role_availability(
            app,
            ROLE_TRANSLATION,
            ok=True,
            message="主连接可用。",
        )
        app = _revalidated(app)

        primary = resolve_effective_model_config(app, ROLE_TRANSLATION)
        second = resolve_effective_model_config(
            app,
            ROLE_TRANSLATION,
            connection_id=second_id,
        )

        self.assertEqual(primary.availability_status, "available")
        self.assertEqual(second.availability_status, "unknown")
        self.assertEqual(app.engine.connections[1].availability_status, "unknown")

    def test_testing_a_second_connection_leaves_the_primary_untouched(self) -> None:
        app, _primary_id, second_id = _app_with_two_connections()
        record_model_role_availability(
            app,
            ROLE_TRANSLATION,
            ok=True,
            message="主连接可用。",
        )
        app = _revalidated(app)

        record_model_role_availability(
            app,
            ROLE_TRANSLATION,
            ok=False,
            message="第二条不可用。",
            connection_id=second_id,
        )
        app = _revalidated(app)

        self.assertEqual(app.engine.connections[0].availability_status, "available")
        self.assertEqual(app.engine.connections[1].availability_status, "unavailable")
        # 角色本身的状态镜像的是主连接，不能被第二条的失败带崩。
        self.assertEqual(app.engine.availability_status, "available")

    def test_check_connectivity_dials_the_requested_connection(self) -> None:
        app, _primary_id, second_id = _app_with_two_connections()
        dialled: list[str] = []

        def _fake_check(settings, *, timeout_seconds=0.0):
            dialled.append(settings.engine.cloud_base_url)
            return ConnectivityResult(
                ok=True,
                status="ok",
                message="ok",
                provider=settings.engine.cloud_provider,
                model=settings.engine.cloud_model,
            )

        with patch.object(connectivity_module, "_check_connectivity", _fake_check):
            result = check_connectivity(app, connection_id=second_id)

        self.assertTrue(result.ok)
        self.assertEqual(dialled, ["https://second.example/v1"])
        self.assertEqual(result.model, "model-second")
        app = _revalidated(app)
        self.assertEqual(app.engine.connections[1].availability_status, "available")
        self.assertEqual(app.engine.connections[0].availability_status, "unknown")


class FollowingRoleAvailabilityTests(unittest.TestCase):
    """A follower dials the source's endpoint but verifies its own model name.

    Three of the four roles follow translation out of the box, so writing a
    follower's verdict onto the source's pool entry would be the common case,
    not an edge one: it would overwrite the translation connection's result and
    leave the follower's own state (which the follow branch of
    ``resolve_effective_model_config`` reads off the role itself) permanently
    stale.
    """

    def test_testing_a_follower_does_not_overwrite_the_source_verdict(self) -> None:
        app, _primary_id, second_id = _app_with_two_connections()
        record_model_role_availability(
            app, ROLE_TRANSLATION, ok=True, message="翻译主连接可用。"
        )
        record_model_role_availability(
            app,
            ROLE_TRANSLATION,
            ok=True,
            message="翻译第二条可用。",
            connection_id=second_id,
        )
        app = _revalidated(app)
        self.assertEqual(app.cleaner_model_role.source_role, ROLE_TRANSLATION)

        record_model_role_availability(
            app,
            ROLE_CLEANER,
            ok=False,
            message="清洗模型名不可用。",
            connection_id=second_id,
        )
        app = _revalidated(app)

        self.assertEqual(app.engine.connections[1].availability_status, "available")
        self.assertEqual(app.engine.connections[1].availability_message, "翻译第二条可用。")
        self.assertEqual(app.cleaner_model_role.availability_status, "unavailable")

    def test_a_followers_verdict_is_the_one_it_reads_back(self) -> None:
        app, _primary_id, second_id = _app_with_two_connections()

        record_model_role_availability(
            app,
            ROLE_CLEANER,
            ok=True,
            message="清洗可用。",
            connection_id=second_id,
        )
        app = _revalidated(app)
        resolved = resolve_effective_model_config(
            app, ROLE_CLEANER, connection_id=second_id
        )

        self.assertTrue(resolved.follows)
        self.assertEqual(resolved.availability_status, "available")


class ConnectionKeyDialledTests(IsolatedAppDataTestCase):
    """测试必须用**被测那条连接自己的**密钥去拨号。

    连接的密钥存在 ``conn::<id>`` 作用域下，而底层探测只认 provider + Base URL，
    它调的 ``get_key`` 永远看不到这个作用域。不把密钥钉回去的话：同一家两个账号会
    拿主用连接的密钥去测第二条（测通了也证明不了第二条能用），换一家网关则直接报
    「缺少 API Key」，而这条连接真跑任务时是好的。密钥读写要落盘，所以跑在隔离目录里。
    """

    def _dial_second_connection(self) -> dict[str, str]:
        app, _primary_id, second_id = _app_with_two_connections()
        save_key("custom_openai", "K1-PRIMARY", "https://primary.example/v1")
        save_connection_key(second_id, "K2-SECOND")
        seen: dict[str, str] = {}

        def _fake(*, provider, api_key, model, base_url, **_kwargs):
            seen["api_key"] = api_key
            seen["base_url"] = base_url
            return ConnectivityResult(
                ok=True, status="ok", message="ok", provider=provider, model=model
            )

        with patch.object(connectivity_module, "_check_openai_compatible", _fake):
            check_connectivity(app, connection_id=second_id)
        return seen

    def test_a_second_connection_is_dialled_with_its_own_key(self) -> None:
        seen = self._dial_second_connection()

        self.assertEqual(seen["api_key"], "K2-SECOND")
        self.assertEqual(seen["base_url"], "https://second.example/v1")

    def test_the_override_does_not_outlive_the_test_call(self) -> None:
        self._dial_second_connection()

        # 覆盖是线程局部的，拨完必须还回去：留着它会让同一线程后面的任何取密钥
        # 都拿到这条连接的密钥。
        self.assertIsNone(current_key_overrides())

    def test_the_primary_still_uses_the_provider_scoped_key(self) -> None:
        app, _primary_id, _second_id = _app_with_two_connections()
        save_key("custom_openai", "K1-PRIMARY", "https://primary.example/v1")
        seen: dict[str, str] = {}

        def _fake(*, provider, api_key, model, base_url, **_kwargs):
            seen["api_key"] = api_key
            return ConnectivityResult(
                ok=True, status="ok", message="ok", provider=provider, model=model
            )

        with patch.object(connectivity_module, "_check_openai_compatible", _fake):
            check_connectivity(app)

        self.assertEqual(seen["api_key"], "K1-PRIMARY")


class ConnectivityRouteTests(IsolatedAppDataTestCase):
    """The route has to forward the panel's connection id, or none of it helps.

    The route ends in ``save_settings``, so it runs against an isolated app-data
    directory — a test must never rewrite the developer's own settings file.
    """

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app())

    def test_route_forwards_the_connection_id(self) -> None:
        seen: dict[str, str] = {}

        def _fake(settings, *, role=ROLE_TRANSLATION, connection_id="", **_kwargs):
            seen["connection_id"] = connection_id
            return ConnectivityResult(ok=True, status="ok", message="ok")

        with patch("api.app.check_connectivity", _fake):
            response = self.client.post(
                "/api/models/connectivity/translation",
                json={"connection_id": "conn-42"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["connection_id"], "conn-42")

    def test_route_still_tests_the_primary_without_a_connection_id(self) -> None:
        seen: dict[str, str] = {}

        def _fake(settings, *, role=ROLE_TRANSLATION, connection_id="", **_kwargs):
            seen["connection_id"] = connection_id
            return ConnectivityResult(ok=True, status="ok", message="ok")

        with patch("api.app.check_connectivity", _fake):
            response = self.client.post("/api/models/connectivity/translation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["connection_id"], "")


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
