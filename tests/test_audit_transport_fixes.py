"""Regression tests for the 2026-08-29 审计 A5-transport 集群。

覆盖三条:
- 高-9: Responses 流式路由遇到非 2xx 时,限流分类器读 .text 被
  httpx.ResponseNotRead 击穿,穿透批次二分/降级阶梯崩掉整个任务。
- 中-9: failover 候选连接构建失败被当成整个 endpoint 宕机,同网关全部
  连接被标耗尽,可用候选一次没试。（该条的回归测试放在
  tests/test_failover_engine.py，与既有 failover 测试共用夹具。）
- 中-23: update_checker 的校验和兜底请求不跟随 302，GitHub 下载链接必
  302，走到即整次检查报废且 CI 的假 client 从不模拟重定向,永绿。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from core.api_concurrency_control import is_api_concurrency_limit_error
from core.text_transport import clear_protocol_cache
from core.update_checker import check_for_updates
from engines.openai_engine import OpenAIEngine


SHA256 = "a" * 64

_REAL_HTTPX_CLIENT = httpx.AsyncClient

class _LazyByteStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body that only yields chunks when actually iterated/read.

    A real HTTP response streamed over the wire never has its body sitting
    in memory ahead of time; httpx.Response(..., content=b"...") does not
    reproduce that (it marks the body pre-read). Only stream= does.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self):
        yield from self._chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        return None

    def close(self) -> None:
        return None


def _error_transport(*, status_code: int, body: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, stream=_LazyByteStream([body]))

    return httpx.MockTransport(handler)


class ResponsesStreamErrorBodyTests(unittest.TestCase):
    """高-9: 流式错误路径必须先读出 body,分类器读 .text 不能被击穿。"""

    def setUp(self):
        clear_protocol_cache()

    def test_a_non_2xx_streaming_response_is_read_before_raising(self) -> None:
        transport = _error_transport(
            status_code=429,
            body=b'{"error": {"message": "rate limit exceeded, please retry"}}',
        )

        def build_client(**kwargs) -> httpx.Client:
            # 生产代码传 timeout=CLOUD_REQUEST_TIMEOUT；这里换上 MockTransport
            # 但保留一个真实、未提前读取的流式响应体。
            return _REAL_HTTPX_CLIENT(transport=transport, **kwargs)

        engine = OpenAIEngine(
            api_key="test-key",
            model="gpt-x",
            base_url="https://api.example.test/v1",
            api_mode="codex_responses",
        )

        with patch("core.text_transport.httpx.AsyncClient", side_effect=build_client):
            with self.assertRaises(httpx.HTTPStatusError) as ctx:
                engine._call_responses_api("system", "user")

        response = ctx.exception.response
        self.assertEqual(response.status_code, 429)
        # 修复前:未读的流式响应体在这里访问 .text 会抛
        # httpx.ResponseNotRead,而不是返回内容。
        self.assertIn("rate limit", response.text)

    def test_the_concurrency_classifier_survives_an_unread_streaming_error(self) -> None:
        """分类器自身也要有兜底:即便拿到未读流式响应也不能崩。"""
        transport = _error_transport(
            status_code=429,
            body=b"Too Many Requests",
        )
        with httpx.Client(transport=transport) as client:
            with client.stream("GET", "https://api.example.test/probe") as response:
                try:
                    response.raise_for_status()
                    self.fail("expected raise_for_status to raise on 429")
                except httpx.HTTPStatusError as exc:
                    # 分类器读取异常时不应传播 ResponseNotRead。
                    result = is_api_concurrency_limit_error(exc)

        self.assertTrue(result)

    def test_end_to_end_engine_error_is_classified_without_crashing(self) -> None:
        """高-9 端到端复现:流式 429 经引擎抛出后,分类器仍能正常识别限流。"""
        transport = _error_transport(
            status_code=429,
            body=b'{"error": {"message": "too many requests, slow down"}}',
        )

        def build_client(**kwargs) -> httpx.Client:
            return _REAL_HTTPX_CLIENT(transport=transport, **kwargs)

        engine = OpenAIEngine(
            api_key="test-key",
            model="gpt-x",
            base_url="https://api.example.test/v1",
            api_mode="codex_responses",
        )

        with patch("core.text_transport.httpx.AsyncClient", side_effect=build_client):
            with self.assertRaises(httpx.HTTPStatusError) as ctx:
                engine._call_responses_api("system", "user")

        # 修复前这一行本身就会被 ResponseNotRead 打断,穿透批次二分和
        # 降级阶梯直接崩掉整个任务；修复后应正常判定为限流,走降并发路径
        # 而不是把任务判死。
        self.assertTrue(is_api_concurrency_limit_error(ctx.exception))


def _release_payload(*, version: str = "8.1.0") -> dict:
    def asset(name: str, url: str) -> dict:
        return {"name": name, "browser_download_url": url}

    return {
        "tag_name": f"v{version}",
        "html_url": f"https://example.test/releases/v{version}",
        "published_at": "2026-07-24T12:00:00Z",
        "body": "- release notes",
        "assets": [
            asset(
                f"Translator_macOS_arm64_{version}.dmg",
                f"https://example.test/{version}/arm64.dmg",
            ),
            asset(
                f"Translator_macOS_arm64_{version}.dmg.sha256",
                f"https://example.test/{version}/arm64.dmg.sha256",
            ),
        ],
    }


class _Response:
    def __init__(self, *, payload: dict | None = None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        assert self._payload is not None
        return self._payload


class _RedirectAwareClient:
    """模拟 httpx.Client 对 302 的真实处理方式。

    GitHub 的 asset 下载链接（校验和兜底请求命中的 checksum_url）一律
    302 到 objects.githubusercontent.com。httpx.Client 只有在构造时传了
    follow_redirects=True 才会替调用方把重定向走完；不传时拿到的是 302
    本身的空/占位正文，而不是真正的校验和文件。这个假 client 把这一步
    行为差异做成显式判断，而不是像旧假 client 那样对任何 URL 都直接返回
    解析好的内容——那样写会让「忘记 follow_redirects」这种回归在 CI 上
    永远测不出来（审计中-23 指出的问题）。
    """

    def __init__(self, release: dict, checksum: str) -> None:
        self.release = release
        self.checksum = checksum
        self.follow_redirects = False
        self.request_urls: list[str] = []

    def __call__(self, *, follow_redirects: bool = False, **_kwargs) -> "_RedirectAwareClient":
        self.follow_redirects = follow_redirects
        return self

    def __enter__(self) -> "_RedirectAwareClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.request_urls.append(url)
        if url.endswith(".sha256"):
            if not self.follow_redirects:
                # 302 本身没有正文可读；用空串代表「没跟过去」。
                return _Response(text="")
            return _Response(text=self.checksum)
        return _Response(payload=self.release)


class UpdateCheckerRedirectTests(unittest.TestCase):
    """中-23: 校验和兜底请求必须跟随 GitHub 的 302。"""

    def test_checksum_request_follows_the_redirect_github_always_sends(self) -> None:
        release = _release_payload()
        checksum_text = f"{SHA256}  Translator_macOS_arm64_8.1.0.dmg\n"
        fake_client = _RedirectAwareClient(release, checksum_text)

        with patch("core.update_checker.httpx.Client", side_effect=fake_client):
            result = check_for_updates(
                current_version="8.0.0",
                platform_name="Darwin",
                machine="arm64",
            )

        # 修复前:httpx.Client 不传 follow_redirects,fake_client 停在 302
        # 上返回空正文,checksum 解析失败,整次检查被误判为
        # "release_not_ready"(正式发布包尚未就绪)。
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.sha256, SHA256)


if __name__ == "__main__":
    unittest.main(verbosity=2)
