"""换文档翻译的服务商时，跟随它的用途变得用不了，不能连带把这次保存拒掉。

新装状态下「图像翻译审核模型」默认跟随文档翻译模型。以前把文档翻译换成智谱、
阿里百炼（本应用里不能看图）或本地模型，保存时会连带校验审核角色，校验不过就整单 422，
文档翻译怎么也存不进去。现在文档翻译照常保存，审核角色在面板上标为不可用并写明原因；
用到它的 PDF 任务开始前给出同一句原因。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import settings as settings_module
from api.app import create_app
from api.surfaces import get_surface
from api.task_contracts import REASON_PDF_REVIEW_MODEL_MISSING, TaskInputError, TaskOptions
from settings import load_settings, save_settings, set_cloud_provider_config

TOKEN = "test-token"


class FollowerCapabilitySaveTests(unittest.TestCase):
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

    def _roles(self) -> dict:
        response = self.client.get("/api/models/roles")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["roles"]

    def _save_translation(self, body: dict) -> dict:
        primary = self._roles()["translation"]["connections"][0]
        response = self.client.put(
            f"/api/models/roles/translation/connections/{primary['id']}",
            json={"source_role": "independent", "label": "", **body},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _switch_translation_to(self, provider: str, model: str) -> None:
        self._save_translation(
            {"connection_mode": "cloud", "provider": provider, "model": model, "api_mode": "auto"}
        )

    def test_fresh_install_review_follows_translation(self) -> None:
        review = self._roles()["pdf_review"]
        self.assertEqual(review["connections"][0]["connection_mode"], "follow")
        self.assertEqual(review["connections"][0]["source_role"], "translation")
        self.assertEqual(review["config_problem"], "")

    def test_text_only_provider_saves_and_marks_review_unavailable(self) -> None:
        for provider, model, name in (
            ("claude", "claude-text-model", "Anthropic"),
        ):
            with self.subTest(provider=provider):
                self._switch_translation_to(provider, model)
                stored = load_settings()
                self.assertEqual(stored.engine.cloud_provider, provider)
                self.assertEqual(stored.engine.connections[0].provider, provider)

                roles = self._roles()
                self.assertEqual(roles["translation"]["provider"], provider)
                self.assertEqual(roles["translation"]["config_problem"], "")
                self.assertEqual(roles["cleaner"]["config_problem"], "")
                review = roles["pdf_review"]
                expected = f"{name}的看图审核功能尚未接入本软件"
                self.assertIn(expected, review["config_problem"])
                self.assertEqual(review["availability_status"], "unavailable")
                self.assertIn(expected, review["availability_message"])
                row = review["connections"][0]
                self.assertIn(expected, row["config_problem"])
                self.assertEqual(row["availability_status"], "unavailable")

    def test_local_translation_saves_and_marks_review_unavailable(self) -> None:
        self._save_translation(
            {
                "connection_mode": "local",
                "provider": "ollama",
                "model": "qwen2.5:7b",
                "base_url": "http://127.0.0.1:11434/v1",
            }
        )
        stored = load_settings()
        self.assertEqual(stored.engine.connections[0].connection_mode, "local")
        roles = self._roles()
        self.assertEqual(roles["translation"]["mode"], "local")
        # 记忆库清洗是文字用途，跟随本地模型没有问题。
        self.assertEqual(roles["cleaner"]["config_problem"], "")
        self.assertIn("跟随来源不可用", roles["pdf_review"]["config_problem"])

    def test_other_saves_keep_working_while_review_is_unavailable(self) -> None:
        self._switch_translation_to("claude", "claude-text-model")
        response = self.client.put("/api/settings", json={"theme": "dark"})
        self.assertEqual(response.status_code, 200, response.text)
        self._switch_translation_to("claude", "claude-text-model-2")
        self.assertEqual(load_settings().engine.cloud_model, "claude-text-model-2")
        throughput = self.client.get("/api/models/throughput/pdf_review")
        self.assertEqual(throughput.status_code, 200, throughput.text)

    def test_review_row_gets_its_own_way_out(self) -> None:
        self._switch_translation_to("claude", "claude-text-model")
        review_row = self._roles()["pdf_review"]["connections"][0]
        url = f"/api/models/roles/pdf_review/connections/{review_row['id']}"

        # 直接编辑这条跟随行（例如改模型名）：它是用户正在改的连接，按自己的能力把关。
        response = self.client.put(
            url,
            json={"connection_mode": "follow", "source_role": "translation", "model": "x"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Anthropic的看图审核功能尚未接入本软件", response.json()["detail"])

        # 改成独立的云端连接就恢复可用。
        response = self.client.put(
            url,
            json={
                "connection_mode": "cloud",
                "source_role": "independent",
                "provider": "custom_openai",
                "model": "vision-model",
                "base_url": "https://vision.example/v1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["config_problem"], "")
        self.assertEqual(self._roles()["translation"]["provider"], "claude")

    def test_deepseek_translation_keeps_the_following_review_usable(self) -> None:
        # DeepSeek 的 API 已能读图片（deepseek-flash），跟随它的审核用途不再标为不可用（反馈 #16）。
        self._switch_translation_to("deepseek", "deepseek-flash")
        review = self._roles()["pdf_review"]
        self.assertEqual(review["config_problem"], "")
        self.assertEqual(review["connections"][0]["connection_mode"], "follow")

    def test_zhipu_and_dashscope_translation_keep_the_following_review_usable(self) -> None:
        # 智谱、阿里百炼是 OpenAI 兼容接口，看图请求走统一的图片请求模块，审核跟随它们不再标为不可用；
        # 所选模型能否看图由「测试连接」实际发图判断。
        for provider, model in (("zhipu", "glm-vision"), ("dashscope", "qwen-vl")):
            with self.subTest(provider=provider):
                self._switch_translation_to(provider, model)
                review = self._roles()["pdf_review"]
                self.assertEqual(review["config_problem"], "")

    def test_switching_translation_back_clears_the_problem(self) -> None:
        self._switch_translation_to("claude", "claude-text-model")
        self.assertTrue(self._roles()["pdf_review"]["config_problem"])
        self._switch_translation_to("custom_openai", "gpt-like")
        review = self._roles()["pdf_review"]
        self.assertEqual(review["config_problem"], "")
        self.assertEqual(review["connections"][0]["connection_mode"], "follow")

    def test_review_itself_still_rejects_a_provider_it_cannot_use(self) -> None:
        review_row = self._roles()["pdf_review"]["connections"][0]
        response = self.client.put(
            f"/api/models/roles/pdf_review/connections/{review_row['id']}",
            json={
                "connection_mode": "cloud",
                "source_role": "independent",
                "provider": "claude",
                "model": "claude-text-model",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Anthropic的看图审核功能尚未接入本软件", response.json()["detail"])

    def test_deleting_a_connection_is_not_blocked_by_an_unavailable_follower(self) -> None:
        self._switch_translation_to("claude", "claude-text-model")
        added = self.client.post(
            "/api/models/roles/translation/connections",
            json={"label": "备用", "provider": "claude", "model": "claude-text-model"},
        )
        self.assertEqual(added.status_code, 200, added.text)
        backup = added.json()["connections"][1]["id"]
        response = self.client.delete(f"/api/models/roles/translation/connections/{backup}")
        self.assertEqual(response.status_code, 200, response.text)

    def _pdf_ready_settings(self):
        settings = load_settings()
        settings.pdf.target_lang = "en"
        image_role = settings.image_model_role
        image_role.cloud_provider = "custom_openai"
        set_cloud_provider_config(
            image_role,
            "custom_openai",
            cloud_model="mock-image-model",
            cloud_base_url="https://image.example/v1",
        )
        return settings

    def test_pdf_task_explains_the_unavailable_review_model(self) -> None:
        self._switch_translation_to("claude", "claude-text-model")
        settings = self._pdf_ready_settings()
        settings.pdf.review_enabled = True
        with patch("core.model_roles.get_key", return_value="secret"), self.assertRaises(
            TaskInputError
        ) as caught:
            get_surface("pdf").validate_preflight(
                files=[object()], settings=settings, options=TaskOptions()
            )
        self.assertEqual(caught.exception.reason, REASON_PDF_REVIEW_MODEL_MISSING)
        self.assertIn("Anthropic的看图审核功能尚未接入本软件", str(caught.exception))

    def test_pdf_task_without_review_is_not_affected(self) -> None:
        self._switch_translation_to("claude", "claude-text-model")
        settings = self._pdf_ready_settings()
        settings.pdf.review_enabled = False
        save_settings(settings)
        with patch("core.model_roles.get_key", return_value="secret"):
            get_surface("pdf").validate_preflight(
                files=[object()], settings=load_settings(), options=TaskOptions()
            )


if __name__ == "__main__":
    unittest.main()
