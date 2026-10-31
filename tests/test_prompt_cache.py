"""提示词缓存：排列、按模型名加的官方标识、被退回时去掉重发、用量读回。"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import core.text_transport as transport
from core.document_profile import append_profile
from core.prompt_cache import (
    CacheUsageMeter,
    UsageTotals,
    append_batch_block,
    carry_meter,
    compose_system,
    current_meter,
    format_usage_line,
    model_cache_family,
    parse_usage,
    plain_system,
    split_system,
    usage_metering,
)
from core.request_log import BatchRequestLog
from core.term_engine import TermAwareEngine
from core.tm_terms import TermIndex
from engines.base_engine import TASK_INSTRUCTION
from engines.claude_engine import ClaudeEngine
from engines.openai_engine import OpenAIEngine

MARKS = ("", "")


def _no_marks(text: str) -> bool:
    return not any(mark in text for mark in MARKS)


# ── 排列 ────────────────────────────────────────────────────────────────────


def test_fixed_parts_come_first_and_batch_parts_last() -> None:
    prompt = append_profile("内置规则", "这是一份消防图纸。")
    prompt = append_batch_block(prompt, "本批文字中出现的记忆库词条：甲 → A")
    composed = compose_system(prompt, "返回格式要求")
    stable, volatile = split_system(composed)
    assert stable.index("内置规则") < stable.index("返回格式要求") < stable.index("本文档说明")
    assert volatile == "本批文字中出现的记忆库词条：甲 → A"
    assert _no_marks(stable) and _no_marks(volatile)
    assert plain_system(composed) == f"{stable}\n\n{volatile}"


def test_two_batches_of_one_document_share_the_whole_fixed_part() -> None:
    base = append_profile("内置规则", "这是一份消防图纸。")
    first = split_system(compose_system(append_batch_block(base, "词条 1"), "格式"))
    second = split_system(compose_system(append_batch_block(base, "词条 2"), "格式"))
    assert first[0] == second[0]
    assert first[1] != second[1]


def test_prompt_without_marks_is_unchanged() -> None:
    assert compose_system("规则", "格式") == "规则\n\n格式"
    assert split_system("规则") == ("规则", "")


# ── 按模型名认厂家 ──────────────────────────────────────────────────────────


def test_model_family_from_model_name_not_from_address() -> None:
    cases = {
        "claude-haiku-4-5-20251001": "claude",
        "anthropic/claude-sonnet-5.5": "claude",
        "gpt-5.6-luna": "openai",
        "openai/gpt-4o-mini": "openai",
        "o3-mini": "openai",
        "o4": "openai",
        "chatgpt-4o-latest": "openai",
        "grok-4.7": "grok",
        "x-ai/grok-4.5-fast": "grok",
        # 官方自动缓存的厂家，哪个平台上都不加东西。
        "deepseek-chat": "",
        "deepseek-ai/DeepSeek-V4": "",
        "qwen-plus": "",
        "glm-5.3": "",
        "MiniMax-M3": "",
        "kimi-k3": "",
        "moonshot-v1-8k": "",
        "mimo-v2.6-pro": "",
        "hunyuan-t1": "",
        "doubao-1.5-pro": "",
        # 开放权重，由各平台自己部署。
        "openai/gpt-oss-120b": "",
    }
    for model, family in cases.items():
        assert model_cache_family(model) == family, model


# ── 请求体 ──────────────────────────────────────────────────────────────────


def _chat_route(omitted=frozenset()) -> transport.Route:
    return transport.Route("chat", "https://relay.example/v1/chat/completions", frozenset(omitted))


SYSTEM = compose_system(append_batch_block("规则", "本批词条"), "格式")


def test_claude_on_any_platform_marks_the_fixed_part() -> None:
    body = transport._payload(_chat_route(), "anthropic/claude-sonnet-5.5", SYSTEM, "[]")
    content = body["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "规则\n\n格式", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "本批词条"},
    ]
    assert "prompt_cache_key" not in body


def test_gpt_gets_prompt_cache_key_that_ignores_the_batch_part() -> None:
    other = compose_system(append_batch_block("规则", "另一批词条"), "格式")
    first = transport._payload(_chat_route(), "gpt-5.6-luna", SYSTEM, "[1]")
    second = transport._payload(_chat_route(), "gpt-5.6-luna", other, "[2]")
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert first["messages"][0]["content"] == "规则\n\n格式\n\n本批词条"


def test_grok_gets_conversation_header_not_body_field() -> None:
    body = transport._payload(_chat_route(), "grok-4.7", SYSTEM, "[]")
    headers = body[transport._HEADERS_KEY]
    assert set(headers) == {"x-grok-conv-id"}
    assert "prompt_cache_key" not in body


def test_auto_caching_models_get_nothing_extra() -> None:
    body = transport._payload(_chat_route(), "deepseek-chat", SYSTEM, "[]")
    assert body == {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "规则\n\n格式\n\n本批词条"},
            {"role": "user", "content": "[]"},
        ],
    }


def test_responses_route_sends_plain_instructions() -> None:
    route = transport.Route("responses", "https://api.openai.com/v1/responses")
    body = transport._payload(route, "gpt-5.6-luna", SYSTEM, "[]")
    assert body["instructions"] == "规则\n\n格式\n\n本批词条"
    assert "prompt_cache_key" in body


def _reject(route, status=400, message="Unrecognized request argument supplied: prompt_cache_key"):
    request = httpx.Request("POST", route.url)
    response = httpx.Response(status, json={"error": {"message": message}}, request=request)
    raise httpx.HTTPStatusError(message, request=request, response=response)


def test_platform_rejecting_hints_gets_one_resend_without_them_and_it_is_remembered() -> None:
    transport.clear_protocol_cache()
    payloads: list[dict] = []

    def send(route, api_key, payload, budget):
        payloads.append(payload)
        if "prompt_cache_key" in payload or isinstance(payload["messages"][0]["content"], list):
            _reject(route)
        return "OK"

    route = transport.Route("chat", "https://relay.example/v1/chat/completions")
    for model in ("gpt-5.6-luna", "claude-sonnet-5.5"):
        payloads.clear()
        with (
            patch.object(transport, "_send", side_effect=send),
            patch.object(transport, "candidate_routes", return_value=[route]),
        ):
            text, used = transport.request_text(
                base_url="https://relay.example/v1", api_key="k", model=model,
                system=SYSTEM, user="[]", connection_id=f"relay-{model}",
            )
            assert text == "OK"
            assert len(payloads) == 2
            # 后续请求沿用学到的路线，不再带标识。
            transport.request_text(
                base_url="https://relay.example/v1", api_key="k", model=model,
                system=SYSTEM, user="[]", connection_id=f"relay-{model}",
            )
        assert len(payloads) == 3
        assert payloads[-1]["messages"][0]["content"] == "规则\n\n格式\n\n本批词条"
        assert "prompt_cache_key" not in payloads[-1]
        assert used.omitted
    transport.clear_protocol_cache()


def test_other_rejections_are_not_resent() -> None:
    transport.clear_protocol_cache()
    calls: list[dict] = []

    def send(route, api_key, payload, budget):
        calls.append(payload)
        _reject(route, status=401, message="invalid api key")

    route = transport.Route("chat", "https://relay.example/v1/chat/completions")
    with (
        patch.object(transport, "_send", side_effect=send),
        patch.object(transport, "candidate_routes", return_value=[route]),
    ):
        try:
            transport.request_text(
                base_url="https://relay.example/v1", api_key="k", model="gpt-5.6-luna",
                system=SYSTEM, user="[]", connection_id="bad-key",
            )
        except httpx.HTTPStatusError:
            pass
    assert len(calls) == 1
    transport.clear_protocol_cache()


# ── 引擎全链路：记忆库词条排在最后，记号不外泄 ───────────────────────────────


def test_openai_engine_with_terms_sends_fixed_prefix_then_batch_terms() -> None:
    transport.clear_protocol_cache()
    seen: list[dict] = []

    def send(route, api_key, payload, budget):
        seen.append(payload)
        texts = json.loads(payload["messages"][1]["content"])
        return json.dumps([f"T{index}" for index, _ in enumerate(texts)])

    engine = OpenAIEngine(api_key="k", model="deepseek-chat", base_url="https://api.deepseek.com/v1")
    prompt = append_profile("内置规则", "这是一份消防图纸。")
    route = transport.Route("chat", "https://api.deepseek.com/v1/chat/completions")
    with (
        patch.object(transport, "_send", side_effect=send),
        patch.object(transport, "candidate_routes", return_value=[route]),
    ):
        engine.translate_batch(["甲级防火门"], "fr", append_batch_block(prompt, "本批文字中出现的记忆库词条：X"))
        engine.translate_batch(["乙级防火门"], "fr", append_batch_block(prompt, "本批文字中出现的记忆库词条：Y"))
    first, second = (payload["messages"][0]["content"] for payload in seen)
    assert _no_marks(first) and _no_marks(second)
    instruction_head = TASK_INSTRUCTION.split("{", 1)[0]
    assert first.index("内置规则") < first.index(instruction_head) < first.index("本文档说明") < first.index("记忆库词条：X")
    shared = first[: first.index("本批文字中出现")]
    assert second.startswith(shared)
    transport.clear_protocol_cache()


def test_term_engine_puts_term_block_after_task_instruction() -> None:
    captured: list[str] = []

    class Inner(OpenAIEngine):
        def _call_api(self, system, user_msg):
            captured.append(system)
            return json.dumps(["porte coupe-feu de classe A"])

    hit = SimpleNamespace(usage="scene", key="甲级防火门")
    engine = TermAwareEngine(Inner(api_key="k", model="m"), TermIndex.__new__(TermIndex), target_lang="fr")
    engine._scan = lambda texts: [[hit] for _ in texts]  # type: ignore[method-assign]
    engine._block = lambda hits: "本批文字中出现的记忆库词条：甲级防火门"  # type: ignore[method-assign]
    engine.translate_batch(["甲级防火门"], "fr", "内置规则")
    stable, volatile = split_system(captured[0])
    assert stable.startswith("内置规则\n\n")
    assert volatile == "本批文字中出现的记忆库词条：甲级防火门"


def test_claude_engine_marks_fixed_part_and_falls_back_when_rejected() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if isinstance(body["system"], list) and len(bodies) == 1 and reject["on"]:
            return httpx.Response(400, json={"error": {"message": "system: Input should be a valid string"}})
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "[\"A\"]"}],
            "usage": {"input_tokens": 50, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0},
        })

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    system = append_batch_block("规则", "本批词条")
    for rejected in (False, True):
        reject = {"on": rejected}
        bodies.clear()
        engine = ClaudeEngine(api_key="k", model="claude-sonnet-5.5", base_url="https://relay.example/v1")
        with patch("httpx.Client", side_effect=client_factory), usage_metering() as meter:
            assert engine.translate_batch(["甲"], "fr", system) == {"甲": "A"}
        if not rejected:
            assert len(bodies) == 1
            blocks = bodies[0]["system"]
            assert blocks[0]["cache_control"] == {"type": "ephemeral"}
            assert blocks[0]["text"].startswith("规则\n\n")
            assert blocks[1] == {"type": "text", "text": "本批词条"}
            assert meter.totals() == UsageTotals(1, 950, 900)
        else:
            assert len(bodies) == 2
            assert isinstance(bodies[1]["system"], str) and _no_marks(bodies[1]["system"])


# ── 用量 ────────────────────────────────────────────────────────────────────


def test_parse_usage_reads_each_providers_field() -> None:
    assert parse_usage({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 768}}).cached_tokens == 768
    assert parse_usage({"input_tokens": 900, "input_tokens_details": {"cached_tokens": 512}}).cached_tokens == 512
    deepseek = parse_usage({"prompt_tokens": 800, "prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 160})
    assert (deepseek.input_tokens, deepseek.cached_tokens) == (800, 640)
    assert parse_usage({"prompt_tokens": 800, "cached_tokens": 300}).cached_tokens == 300
    anthropic = parse_usage({"input_tokens": 40, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 0})
    assert (anthropic.input_tokens, anthropic.cached_tokens) == (740, 700)
    # 没有命中字段的服务不算进去，免得写出「命中 0」的假话。
    assert parse_usage({"prompt_tokens": 800, "completion_tokens": 10}) is None
    assert parse_usage(None) is None


def test_usage_line_wording() -> None:
    assert format_usage_line(UsageTotals(3, 12340, 8200)) == "模型输入共 12,340 tokens，其中缓存命中 8,200（66%）"
    assert format_usage_line(UsageTotals()) == ""


def test_meter_reaches_pool_threads_and_request_log_writes_one_line() -> None:
    lines: list[tuple[str, str]] = []
    with usage_metering() as meter:
        log = BatchRequestLog(lambda level, message: lines.append((level, message)), tick_seconds=0.01)
        with log:
            def work():
                assert current_meter() is meter
                transport.record_usage({"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 600}})

            worker = threading.Thread(target=carry_meter(work))
            worker.start()
            worker.join()
            orphan = threading.Thread(target=lambda: transport.record_usage({"prompt_tokens": 5, "cached_tokens": 5}))
            orphan.start()
            orphan.join()
    assert lines == [("INFO", "模型输入共 1,000 tokens，其中缓存命中 600（60%）")]
    assert isinstance(meter, CacheUsageMeter)


def test_no_line_when_service_reports_no_cache_numbers() -> None:
    lines: list[tuple[str, str]] = []
    with usage_metering():
        with BatchRequestLog(lambda level, message: lines.append((level, message)), tick_seconds=0.01):
            transport.record_usage({"prompt_tokens": 1000})
    assert lines == []
