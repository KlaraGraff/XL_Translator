"""A following role's connection list belongs to the role it follows.

Reusing another role's credentials but listing your own idle pool made the
panel name a connection nothing was dialing, so these tests pin the pool a
following role reports and the fact that it cannot be edited from there.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from core.model_api_identity import task_api_context_for_page
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    add_role_connection,
)
from settings import AppSettings

TOKEN = "test-token"


class RoleApiTestCase(unittest.TestCase):
    """Isolated app data plus the role helpers both suites below share."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for item in (
            patch.object(settings_module, "APP_DATA_DIR", root),
            patch.object(settings_module, "KEYS_PATH", root / "keys.json"),
            patch.object(settings_module, "SETTINGS_PATH", root / "settings.json"),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(create_app(auth_token=TOKEN))
        self.client.headers.update({"X-Translator-Token": TOKEN})
        self.addCleanup(self.client.close)

    def _role(self, role: str) -> dict:
        response = self.client.get("/api/models/roles")
        self.assertEqual(response.status_code, 200)
        return response.json()["roles"][role]

    def _name_translation_primary(self, label: str) -> str:
        """Give translation's primary a recognisable label and return its id."""
        primary = self._role("translation")["connections"][0]
        response = self.client.put(
            f"/api/models/roles/translation/connections/{primary['id']}",
            json={"label": label},
        )
        self.assertEqual(response.status_code, 200)
        return str(primary["id"])

    def _write_stored_settings(self, **role_sources: str) -> None:
        """Put a settings.json on disk that names the given follow sources."""
        stored = AppSettings().model_dump(mode="json")
        for setting_key, source_role in role_sources.items():
            stored[setting_key]["source_role"] = source_role
        settings_module.SETTINGS_PATH.write_text(
            json.dumps(stored, ensure_ascii=False),
            encoding="utf-8",
        )

    def _follow_translation(self, role: str) -> None:
        response = self.client.put(
            f"/api/models/roles/{role}",
            json={"source_role": "translation"},
        )
        self.assertEqual(response.status_code, 200, response.text)


class FollowedConnectionPoolTests(RoleApiTestCase):
    def test_a_following_role_lists_the_connection_it_actually_dials(self) -> None:
        connection_id = self._name_translation_primary("DeepSeek")
        self._follow_translation("pdf_review")

        review = self._role("pdf_review")
        assert review["follows"] is True
        assert review["connection_pool_role"] == "translation"
        assert [conn["id"] for conn in review["connections"]] == [connection_id]
        # The panel used to fall back to the role's own provider name here.
        assert review["connections"][0]["display_label"] == "DeepSeek"

    def test_an_independent_role_still_lists_its_own_pool(self) -> None:
        self._name_translation_primary("DeepSeek")
        response = self.client.put(
            "/api/models/roles/image",
            json={"source_role": "independent"},
        )
        self.assertEqual(response.status_code, 200, response.text)

        image = self._role("image")
        assert image["connection_pool_role"] == "image"
        translation_ids = {
            conn["id"] for conn in self._role("translation")["connections"]
        }
        assert not translation_ids & {conn["id"] for conn in image["connections"]}

    def test_a_following_role_cannot_edit_the_borrowed_pool(self) -> None:
        connection_id = self._name_translation_primary("DeepSeek")
        self._follow_translation("pdf_review")

        attempts = (
            self.client.post(
                "/api/models/roles/pdf_review/connections",
                json={"label": "新连接"},
            ),
            self.client.put(
                f"/api/models/roles/pdf_review/connections/{connection_id}",
                json={"label": "改名"},
            ),
            self.client.delete(
                f"/api/models/roles/pdf_review/connections/{connection_id}",
            ),
            self.client.post(
                "/api/models/roles/pdf_review/connections/reorder",
                json={"ordered_ids": [connection_id]},
            ),
        )
        for response in attempts:
            assert response.status_code == 422, response.text
            assert "跟随" in response.json()["detail"]

        # The borrowed pool survived every rejected edit.
        assert [
            conn["display_label"] for conn in self._role("translation")["connections"]
        ] == ["DeepSeek"]

    def test_following_reports_the_source_primary_after_a_promotion(self) -> None:
        self._name_translation_primary("DeepSeek")
        added = self.client.post(
            "/api/models/roles/translation/connections",
            json={
                "label": "备用厂商",
                "provider": "custom_openai",
                "base_url": "https://vendor-b.example/v1",
            },
        )
        self.assertEqual(added.status_code, 200, added.text)
        pool = added.json()["connections"]
        self._follow_translation("pdf_review")

        reordered = self.client.post(
            "/api/models/roles/translation/connections/reorder",
            json={"ordered_ids": [pool[1]["id"], pool[0]["id"]]},
        )
        self.assertEqual(reordered.status_code, 200, reordered.text)

        review = self._role("pdf_review")
        assert [conn["id"] for conn in review["connections"]] == [
            pool[1]["id"],
            pool[0]["id"],
        ]
        assert review["connections"][0]["display_label"] == "备用厂商"


class AccessModeApiTests(RoleApiTestCase):
    """The 连接方式 control: cloud, local, or following another role."""

    def test_only_text_roles_advertise_a_local_option(self) -> None:
        roles = self.client.get("/api/models/roles").json()["roles"]
        assert roles["translation"]["supports_local"] is True
        assert roles["cleaner"]["supports_local"] is True
        # Image generation and image understanding are cloud-only here, so the
        # panel must not offer a local runner it cannot use.
        assert roles["image"]["supports_local"] is False
        assert roles["pdf_review"]["supports_local"] is False

    def test_follow_options_exclude_self_and_existing_followers(self) -> None:
        roles = self.client.get("/api/models/roles").json()["roles"]
        # Out of the box the secondary roles follow translation, so it is the
        # only legal source and translation itself has none.
        assert roles["pdf_review"]["source_role_options"] == [
            "independent",
            "translation",
        ]
        assert roles["translation"]["source_role_options"] == ["independent"]
        # 图像角色两个方向都不参与跟随。
        assert roles["image"]["source_role_options"] == ["independent"]
        for options in roles.values():
            assert "image" not in options["source_role_options"]

        freed = self.client.put(
            "/api/models/roles/pdf_review", json={"source_role": "independent"}
        )
        self.assertEqual(freed.status_code, 200, freed.text)
        roles = self.client.get("/api/models/roles").json()["roles"]
        assert roles["translation"]["source_role_options"] == [
            "independent",
            "pdf_review",
        ]
        for role, options in roles.items():
            assert role not in options["source_role_options"]

    def test_translation_can_follow_a_role_that_is_independent(self) -> None:
        for role in ("cleaner", "image", "pdf_review"):
            self.client.put(
                f"/api/models/roles/{role}", json={"source_role": "independent"}
            )
        cleaner = self.client.put(
            "/api/models/roles/cleaner",
            json={
                "mode": "cloud",
                "provider": "custom_openai",
                "base_url": "https://cleaner.example/v1",
                "model": "cleaner-model",
            },
        )
        self.assertEqual(cleaner.status_code, 200, cleaner.text)

        response = self.client.put(
            "/api/models/roles/translation", json={"source_role": "cleaner"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        assert response.json()["follows"] is True
        assert response.json()["source_role"] == "cleaner"
        assert response.json()["base_url"] == "https://cleaner.example/v1"
        assert response.json()["connection_pool_role"] == "cleaner"

    def test_following_a_role_that_already_follows_is_rejected(self) -> None:
        # cleaner follows translation by default, so translation following
        # cleaner would be a chain (and a cycle).
        response = self.client.put(
            "/api/models/roles/translation", json={"source_role": "cleaner"}
        )
        assert response.status_code == 422, response.text

    def test_the_image_role_cannot_follow_another_role_over_http(self) -> None:
        response = self.client.put(
            "/api/models/roles/image", json={"source_role": "translation"}
        )
        assert response.status_code == 422, response.text
        assert "只能独立配置" in response.json()["detail"]

    def test_no_role_can_follow_the_image_role_over_http(self) -> None:
        freed = self.client.put(
            "/api/models/roles/image", json={"source_role": "independent"}
        )
        self.assertEqual(freed.status_code, 200, freed.text)
        for role in ("translation", "cleaner", "pdf_review"):
            with self.subTest(role=role):
                response = self.client.put(
                    f"/api/models/roles/{role}", json={"source_role": "image"}
                )
                assert response.status_code == 422, response.text
                assert "只能独立配置" in response.json()["detail"]

    def test_the_settings_endpoint_rejects_an_illegal_follow_choice(self) -> None:
        response = self.client.put(
            "/api/settings",
            json={"pdf_review_model_role": {"source_role": "image"}},
        )
        assert response.status_code == 422, response.text
        assert "只能独立配置" in response.json()["detail"]

    def test_a_role_cannot_follow_itself_over_http(self) -> None:
        response = self.client.put(
            "/api/models/roles/image", json={"source_role": "image"}
        )
        assert response.status_code == 422, response.text
        assert "自己" in response.json()["detail"]

    def test_the_cleaner_role_can_run_against_a_local_runner(self) -> None:
        response = self.client.put(
            "/api/models/roles/cleaner",
            json={
                "source_role": "independent",
                "mode": "local",
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen2.5:7b",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        assert payload["mode"] == "local"
        assert payload["provider"] == "ollama"
        assert payload["model"] == "qwen2.5:7b"
        assert payload["has_api_key"] is False

    def test_an_image_role_still_cannot_run_locally(self) -> None:
        response = self.client.put(
            "/api/models/roles/image",
            json={"source_role": "independent", "mode": "local", "provider": "ollama"},
        )
        assert response.status_code == 422, response.text


class StoredIllegalFollowTests(RoleApiTestCase):
    """磁盘上已经存在的非法跟随必须读得出来。

    图像角色被移出跟随机制之前存下的 settings.json，两个方向都可能有跟随。读的时候
    再报错，用户打开的就是一个连设置面板都进不去的应用。
    """

    def test_a_stored_image_follow_still_loads_and_degrades(self) -> None:
        self._write_stored_settings(image_model_role="translation")

        response = self.client.get("/api/models/roles")

        self.assertEqual(response.status_code, 200, response.text)
        image = response.json()["roles"]["image"]
        assert image["follows"] is False
        assert image["source_role"] == "independent"
        assert image["source_role_options"] == ["independent"]

    def test_a_stored_follow_of_the_image_role_still_loads(self) -> None:
        self._write_stored_settings(
            image_model_role="independent",
            pdf_review_model_role="image",
        )

        response = self.client.get("/api/models/roles")

        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()["roles"]["pdf_review"]
        assert review["follows"] is False
        assert review["source_role"] == "independent"

    def test_both_illegal_directions_at_once_still_load(self) -> None:
        self._write_stored_settings(
            image_model_role="translation",
            pdf_review_model_role="image",
        )

        response = self.client.get("/api/models/roles")

        self.assertEqual(response.status_code, 200, response.text)
        roles = response.json()["roles"]
        assert roles["image"]["source_role"] == "independent"
        assert roles["pdf_review"]["source_role"] == "independent"

    def test_an_unrelated_save_is_not_blocked_by_a_stored_illegal_follow(self) -> None:
        """降级是为了不挡路：旧配置在磁盘上，也不能让别的设置存不下去。"""
        self._write_stored_settings(image_model_role="translation")

        response = self.client.put("/api/settings", json={"target_lang": "en"})

        self.assertEqual(response.status_code, 200, response.text)


class FollowedConnectionAllocationTests(unittest.TestCase):
    """A task's recorded connection must come from the pool it really dials."""

    def _settings(self) -> AppSettings:
        settings = AppSettings()
        settings.engine.connections[0].label = "DeepSeek"
        # 图像角色不参与跟随，PDF 页上只有审核角色会去借翻译模型的连接池。
        settings.image_model_role.source_role = "independent"
        settings.pdf_review_model_role.source_role = "translation"
        settings.pdf.review_enabled = True
        return settings

    def test_a_following_role_is_allocated_from_the_source_pool(self) -> None:
        settings = self._settings()
        source_ids = {conn.id for conn in settings.engine.connections}

        with patch("core.model_roles.get_key", return_value="secret"):
            context = task_api_context_for_page(settings, "pdf_translate")

        # This id is what the panel matches its "occupied" markers against, so
        # it has to name a connection the panel actually lists.
        assert context.role_connection_ids[ROLE_PDF_REVIEW] in source_ids
        # 独立配置的角色仍然拨自己那份池子。
        assert context.role_connection_ids[ROLE_IMAGE] not in source_ids

    def test_spreading_moves_a_follower_off_a_busy_source_connection(self) -> None:
        settings = self._settings()
        add_role_connection(
            settings,
            ROLE_TRANSLATION,
            label="备用厂商",
            provider="custom_openai",
            base_url="https://vendor-b.example/v1",
        )
        source_ids = [conn.id for conn in settings.engine.connections]
        assert len(source_ids) == 2

        with patch("core.model_roles.get_key", return_value="secret"):
            context = task_api_context_for_page(
                settings,
                "pdf_translate",
                busy_connection_ids=frozenset({source_ids[0]}),
                spread=True,
            )

        # The follower borrows the source's pool, so spreading has to move it
        # inside that pool rather than fall back to its own idle entry.
        assert context.role_connection_ids[ROLE_PDF_REVIEW] == source_ids[1]


if __name__ == "__main__":
    unittest.main()
