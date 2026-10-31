"""本机的模型服务接成云端连接时按本地模型发送（反馈 #24）。

用户把本机 Ollama 接成「自定义（OpenAI 兼容）」、地址填 http://127.0.0.1:11434/v1。以前按云端
处理：Word / PDF 转 Word 一次同时发 30 批，3 核、8GB 内存的电脑上每批等 2 到 6 分钟，等不到的
保留了原文，PDF 一页只翻了一部分。现在地址是本机就按本地模型：一次一批、按本地的每批大小。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.engine_dispatcher import build_engine, is_local_engine_name
from core.local_endpoint import (
    config_runs_locally,
    is_this_computer_url,
    runtime_mode,
    settings_runs_locally,
)
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from core.model_throughput import get_model_throughput
from core.surface_rate import resolve_surface_rate, role_mode, task_role_rate
from core.word_batching import WordBatchRunStats, translate_word_texts
from settings import AppSettings, WordBatchSettings, set_cloud_provider_config

OLLAMA_V1 = "http://127.0.0.1:11434/v1"


def _custom_openai(base_url: str) -> AppSettings:
    settings = AppSettings()
    settings.engine.mode = "cloud"
    settings.engine.source_role = "independent"
    settings.engine.cloud_provider = "custom_openai"
    set_cloud_provider_config(settings.engine, "custom_openai", cloud_model="qwen2.5:7b", cloud_base_url=base_url)
    return settings


class ThisComputerUrlTests(unittest.TestCase):
    def test_loopback_addresses(self) -> None:
        for url in (
            OLLAMA_V1,
            "http://localhost:1234/v1",
            "https://LOCALHOST/v1",
            "localhost:11434",
            "http://127.0.0.2:8000/v1",
            "http://[::1]:11434/v1",
            "http://0.0.0.0:11434/v1",
            "http://[::ffff:127.0.0.1]:11434/v1",
            "http://ollama.localhost/v1",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_this_computer_url(url))

    def test_other_machines_and_blank(self) -> None:
        for url in (
            "",
            None,
            "https://api.openai.com/v1",
            "http://192.168.1.20:11434/v1",
            "http://10.0.0.5/v1",
            "https://localhost.example.com/v1",
            "http://[not-an-ip/v1",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_this_computer_url(url))


class LoopbackConnectionPacingTests(unittest.TestCase):
    def test_engine_is_named_as_a_local_model(self) -> None:
        local = build_engine(_custom_openai(OLLAMA_V1))
        self.assertTrue(is_local_engine_name(local.engine_name))
        cloud = build_engine(_custom_openai("https://api.example.com/v1"))
        self.assertFalse(is_local_engine_name(cloud.engine_name))

    def test_settings_and_config_report_local(self) -> None:
        settings = _custom_openai(OLLAMA_V1)
        self.assertTrue(settings_runs_locally(settings))
        config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        self.assertEqual(config.mode, "cloud")  # 连接类型不变，只是按本地分档
        self.assertTrue(config_runs_locally(config))
        self.assertEqual(runtime_mode(config), "local")
        self.assertEqual(role_mode(settings), "local")

        remote = _custom_openai("https://api.example.com/v1")
        self.assertFalse(settings_runs_locally(remote))
        self.assertEqual(role_mode(remote), "cloud")

    def test_rates_use_the_local_defaults(self) -> None:
        settings = _custom_openai(OLLAMA_V1)
        for surface in ("word", "pdf_text", "excel", "cad"):
            with self.subTest(surface=surface):
                rate = resolve_surface_rate(settings, surface)
                self.assertEqual(rate.mode, "local")
                self.assertEqual((rate.concurrency, rate.batch_chars), (3, 800))
        config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        self.assertEqual(task_role_rate(settings, "excel_translate", config), (3, 10))
        self.assertEqual(get_model_throughput(settings, config).concurrency, 3)

        remote = _custom_openai("https://api.example.com/v1")
        self.assertEqual(resolve_surface_rate(remote, "word").concurrency, 30)


class _SlowSingleWorkerModel:
    """模拟本机模型服务：记下同一时刻最多有几个请求在算。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.requests = 0

    def answer(self, body: dict) -> dict:
        with self.lock:
            self.active += 1
            self.requests += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.05)
            texts = json.loads(body["messages"][-1]["content"])
            content = json.dumps([f"译:{text}" for text in texts], ensure_ascii=False)
            return {
                "id": "x",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            }
        finally:
            with self.lock:
                self.active -= 1


class LoopbackServerWordRouteTests(unittest.TestCase):
    """真实的 OpenAI 兼容引擎发到本机地址：Word 流程一次只发一批。"""

    def setUp(self) -> None:
        model = self.model = _SlowSingleWorkerModel()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                payload = model.answer(json.loads(self.rfile.read(length) or b"{}"))
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_word_batches_go_one_at_a_time(self) -> None:
        settings = _custom_openai(self.base_url)
        rate = resolve_surface_rate(settings, "word")
        engine = build_engine(settings)
        texts = [f"Paragraph number {index} of the report." for index in range(40)]
        stats = WordBatchRunStats()
        result = translate_word_texts(
            texts, engine, "zh", "p", rate.word_batch_settings(), rate.concurrency,
            source_lang="en", stats=stats,
        )
        self.assertEqual(result, {text: f"译:{text}" for text in texts})
        self.assertGreater(self.model.requests, 1)
        self.assertEqual(self.model.peak, 1)

    def test_a_remote_looking_connection_still_sends_in_parallel(self) -> None:
        # 对照：同一个服务按云端连接（并发 30）发，同一时刻会有多个请求。
        settings = _custom_openai(self.base_url)
        engine = build_engine(settings)
        engine._engine_name_prefix = "openai"  # 当成云端
        texts = [f"Paragraph number {index} of the report." for index in range(40)]
        translate_word_texts(
            texts, engine, "zh", "p", WordBatchSettings(max_paragraphs_per_batch=2), 30,
            source_lang="en", stats=WordBatchRunStats(),
        )
        self.assertGreater(self.model.peak, 1)


if __name__ == "__main__":
    unittest.main()
