"""
Ollama 本地 LLM 翻译引擎。
支持 asyncio.gather 并发批次（M5 Pro / 高性能本地机器优化）。
文件级串行，批次级并发。
"""
import asyncio
import json
import math

import httpx
from loguru import logger

from config import OLLAMA_BASE_URL, OLLAMA_TIMEOUT, RETRY_MAX_ATTEMPTS
from core.translation_protocol import REPLACE_TRANSLATION_PREFIX
from engines.base_engine import (
    TASK_INSTRUCTION,
    TranslationEngine,
    get_source_lang_name,
    get_target_lang_name,
    parse_response,
)


class OllamaEngine(TranslationEngine):

    def __init__(
        self,
        model: str = "qwen2.5:14b",
        concurrency: int = 4,
        base_url: str = OLLAMA_BASE_URL,
    ):
        self._model       = model
        self._concurrency = concurrency
        self._base_url    = base_url.rstrip("/")

    @property
    def engine_name(self) -> str:
        return f"ollama/{self._model}"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        """
        同步入口，内部用独立 event loop 运行异步并发子批次。
        使用 new_event_loop() 而非 asyncio.run()，
        避免在已有 event loop 的环境中引发 RuntimeError。
        """
        if not texts:
            return {}
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._translate_async(texts, target_lang, system_prompt, source_lang)
            )
        finally:
            loop.close()

    async def _translate_async(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        source_lang_name = get_source_lang_name(source_lang)
        target_lang_name = get_target_lang_name(target_lang)
        instruction = TASK_INSTRUCTION.format(
            source_lang_name=source_lang_name,
            target_lang_name=target_lang_name,
            replace_prefix=REPLACE_TRANSLATION_PREFIX,
        )
        full_system = f"{system_prompt}\n\n{instruction}".strip()

        # 将 texts 拆成最多 concurrency 份并发请求（向上取整确保 chunk 数不超过并发上限）
        chunk_size = max(1, math.ceil(len(texts) / self._concurrency))
        chunks = [texts[i:i + chunk_size] for i in range(0, len(texts), chunk_size)]

        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = [
            self._translate_chunk(chunk, target_lang, full_system, semaphore)
            for chunk in chunks
        ]
        results = await asyncio.gather(*tasks)

        merged: dict[str, str] = {}
        for partial in results:
            merged.update(partial)
        return merged

    async def _translate_chunk(
        self,
        texts: list[str],
        target_lang: str,
        full_system: str,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, str]:
        user_msg = json.dumps(texts, ensure_ascii=False)
        async with semaphore:
            for attempt in range(RETRY_MAX_ATTEMPTS):
                try:
                    raw = await self._call_ollama(full_system, user_msg)
                    return parse_response(texts, raw, "Ollama")
                except Exception as e:
                    logger.warning(f"Ollama 第 {attempt + 1} 次重试：{e}")
                    await asyncio.sleep(1.5 ** attempt)
        logger.error("Ollama 重试耗尽，降级返回原文")
        return {t: t for t in texts}

    async def _call_ollama(self, system: str, user_msg: str) -> str:
        url = f"{self._base_url}/api/chat"
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system",  "content": system},
                {"role": "user",    "content": user_msg},
            ],
        }
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]

    def chat(self, system: str, user: str) -> str:
        """Direct chat call used by structured review/translation helpers."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self._call_ollama(system, user))
        finally:
            loop.close()
