"""
翻译引擎调度层（统一入口）。
根据用户配置实例化对应引擎，对外暴露统一的 translate_batch() 接口。
云端引擎使用 ThreadPoolExecutor 并发发送批次，Ollama 保持原有 asyncio 逻辑不变。
"""
import math
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from loguru import logger

from config import (
    DOMAIN_PRESETS,
    CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX,
    CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX,
    LM_STUDIO_BASE_URL,
    OLLAMA_BASE_URL,
    normalize_cloud_base_url,
)
from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    API_REQUEST_CATEGORY_NORMAL,
    ApiConcurrencyLimitDecision,
    WeightedApiScheduler,
)
from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.language_registry import append_prompt_block, build_target_lang_note_block
from core.translation_protocol import should_apply_quality_filter
from engines.base_engine import TranslationEngine
from settings import AppSettings, get_cloud_provider_config, get_key
from core.translation_filter import is_translation_redundant  # 质量闭环拦截

_EXCEL_CLOUD_BATCH_CHAR_BUDGET = 3200
_EXCEL_LOCAL_BATCH_CHAR_BUDGET = 2400
_API_WEIGHT_CHARS_PER_SLOT = 4000
_API_WEIGHT_PROMPT_CHAR_CAP = 900
_API_WEIGHT_OUTPUT_MULTIPLIER = 1.15


@dataclass
class TranslationBatchRunStats:
    original_count: int = 0
    batch_count: int = 0
    retry_count: int = 0
    failed_batch_count: int = 0
    failed_items: list[dict[str, str]] = field(default_factory=list)
    max_request_weight: int = 1
    weighted_scheduler_used: bool = False
    adaptive_concurrency_reductions: int = 0
    adaptive_lowest_concurrency: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def record_request_weight(self, request_weight: int) -> None:
        with self._lock:
            self.max_request_weight = max(self.max_request_weight, request_weight)

    def record_retry(self) -> None:
        with self._lock:
            self.retry_count += 1

    def record_failed_batch(self, source_text: str = "", error: str = "") -> None:
        with self._lock:
            self.failed_batch_count += 1
            if source_text:
                self.failed_items.append(
                    {
                        "source": source_text,
                        "error": error,
                    }
                )

    def record_adaptive_concurrency_decision(
        self,
        decision: ApiConcurrencyLimitDecision,
    ) -> None:
        if decision.action != API_CONCURRENCY_ACTION_REDUCED:
            return
        with self._lock:
            self.adaptive_concurrency_reductions += 1
            if self.adaptive_lowest_concurrency <= 0:
                self.adaptive_lowest_concurrency = decision.current_capacity
            else:
                self.adaptive_lowest_concurrency = min(
                    self.adaptive_lowest_concurrency,
                    decision.current_capacity,
                )


def is_local_engine_name(engine_name: str) -> bool:
    return str(engine_name or "").startswith(("ollama/", "local_openai/"))


def build_engine(settings: AppSettings) -> TranslationEngine:
    """根据当前配置构建并返回翻译引擎实例。"""
    s = settings.engine

    if s.mode == "local":
        provider = str(s.local_provider or "ollama").strip()
        model = str(s.local_model or s.ollama_model or "").strip()
        base_url = str(s.local_base_url or "").strip()
        if provider == "ollama":
            from engines.ollama_engine import OllamaEngine
            return OllamaEngine(
                model=model,
                concurrency=s.ollama_concurrency,
                base_url=base_url or OLLAMA_BASE_URL,
            )
        if provider in {"lm_studio", "custom_local"}:
            from engines.openai_engine import OpenAIEngine
            return OpenAIEngine(
                api_key=get_key(provider) or "local-model",
                model=model,
                base_url=base_url or (LM_STUDIO_BASE_URL if provider == "lm_studio" else ""),
                engine_name_prefix=f"local_openai/{provider}",
            )
        raise ValueError(f"未知本地模型服务：{provider}")

    # 云端模式
    provider = str(s.cloud_provider or "").strip()
    provider_config = get_cloud_provider_config(s, provider)
    cloud_model = provider_config.cloud_model or s.cloud_model
    cloud_base_url = normalize_cloud_base_url(provider, provider_config.cloud_base_url)
    api_key = get_key(provider, cloud_base_url)

    if provider == "claude":
        from engines.claude_engine import ClaudeEngine
        return ClaudeEngine(
            api_key=api_key,
            model=cloud_model,
            base_url=cloud_base_url,
        )

    if provider in ("openai", "siliconflow", "custom_openai", "lanyi"):
        from engines.openai_engine import OpenAIEngine
        return OpenAIEngine(
            api_key=api_key,
            model=cloud_model,
            base_url=cloud_base_url,
        )

    if provider == "zhipu":
        from engines.zhipu_engine import ZhipuEngine
        return ZhipuEngine(api_key=api_key, model=cloud_model)

    if provider == "dashscope":
        from engines.dashscope_engine import DashscopeEngine
        return DashscopeEngine(api_key=api_key, model=cloud_model)

    raise ValueError(f"未知翻译引擎：{provider}")


def get_system_prompt(
    settings: AppSettings,
    target_lang: str = "",
    source_lang: str = "zh",
) -> str:
    """
    根据领域预设和目标语言生成最终 System Prompt。

    DOMAIN_PRESETS 支持两种格式：
      - dict[str, str]：单语言（旧格式，向下兼容）
      - dict[str, dict[str, str]]：多语言，内层 key 为 lang 代码 或 "_base"
    """
    if settings.domain_preset == "自定义":
        prompt = settings.custom_prompt
        return append_prompt_block(
            prompt,
            build_target_lang_note_block(target_lang, settings.custom_target_langs),
        )
    # 用户自定义覆盖优先于内置预设
    if settings.domain_preset in settings.domain_prompt_overrides:
        prompt = settings.domain_prompt_overrides[settings.domain_preset]
        return append_prompt_block(
            prompt,
            build_target_lang_note_block(target_lang, settings.custom_target_langs),
        )
    preset = DOMAIN_PRESETS.get(settings.domain_preset, "")
    prompt = ""
    if isinstance(preset, dict):
        prompt = preset.get(target_lang) or preset.get("_base", "")
    else:
        prompt = preset
    return append_prompt_block(
        prompt,
        build_target_lang_note_block(target_lang, settings.custom_target_langs),
    )


def get_batch_size(settings: AppSettings) -> int:
    """获取当前引擎对应的批次大小（UI 已按模式锁定区间，此处直接透传）。"""
    return settings.engine.batch_size


def translate_texts(
    texts: list[str],
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    batch_size: int,
    concurrency: int,
    progress_callback=None,
    error_callback=None,
    should_stop=None,
    source_lang: str = "zh",
    api_scheduler: WeightedApiScheduler | None = None,
    request_category: str = API_REQUEST_CATEGORY_NORMAL,
    stats: TranslationBatchRunStats | None = None,
) -> dict[str, str]:
    """
    将 texts 分批送入 engine，汇总返回 {原文: 译文}。

    - Ollama（mode=local）：engine.translate_batch 内部已有 asyncio 并发，
      此处保持串行逐批调用，避免与 asyncio.run() 冲突。
    - 云端引擎：使用 ThreadPoolExecutor 并发提交所有批次，
      progress_callback 通过 Lock 保证线程安全。
    """
    if not texts:
        return {}

    # 最后一道参数钳位：强制将 batch_size 限制在合规区间，
    # 防止配置脏数据或 UI 传参异常导致越界请求。
    is_local = is_local_engine_name(engine.engine_name)
    if is_local:
        chunk = max(CHUNK_LOCAL_MIN, min(CHUNK_LOCAL_MAX, batch_size))
    else:
        chunk = max(CHUNK_CLOUD_MIN, min(CHUNK_CLOUD_MAX, batch_size))

    char_budget = (
        _EXCEL_LOCAL_BATCH_CHAR_BUDGET
        if is_local
        else _EXCEL_CLOUD_BATCH_CHAR_BUDGET
    )
    batches = _build_text_batches(texts, max_items=chunk, max_chars=char_budget)
    total = len(texts)
    run_stats = stats or TranslationBatchRunStats()
    run_stats.original_count = total
    run_stats.batch_count = len(batches)

    # Ollama 走原有串行路径，不引入线程池（内部已有 asyncio 并发）
    if is_local:
        results: dict[str, str] = {}
        done = 0
        for batch in batches:
            if should_stop and should_stop():
                logger.info("翻译任务收到停止信号，停止提交后续本地批次。")
                break
            partial = _translate_batch_with_fallback(
                batch,
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=None,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=run_stats,
            )
            results.update(partial)
            done += len(batch)
            if progress_callback:
                progress_callback(done, total)
        _apply_quality_filter(results, target_lang, source_lang=source_lang)
        return results

    # 云端引擎：ThreadPoolExecutor 并发提交所有批次
    results: dict[str, str] = {}
    done_count = 0
    lock = threading.Lock()
    max_workers = max(1, int(concurrency))
    scheduler = api_scheduler or WeightedApiScheduler(max_workers)
    run_stats.weighted_scheduler_used = True

    def _submit_batch(batch: list[str]) -> tuple[list[str], dict[str, str]]:
        """单批次执行，返回 (原始词条列表, 翻译结果)。失败时会缩小批次重试。"""
        return batch, _translate_batch_with_fallback(
            batch,
            engine=engine,
            target_lang=target_lang,
            system_prompt=system_prompt,
            source_lang=source_lang,
            api_scheduler=scheduler,
            request_category=request_category,
            should_stop=should_stop,
            error_callback=error_callback,
            stats=run_stats,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map: dict = {}
        batch_iter = iter(batches)

        def _submit_next() -> bool:
            if should_stop and should_stop():
                return False
            try:
                batch = next(batch_iter)
            except StopIteration:
                return False
            future = executor.submit(_submit_batch, batch)
            future_map[future] = batch
            return True

        for _ in range(min(max_workers, len(batches))):
            if not _submit_next():
                break

        while future_map:
            done_futures, _ = wait(tuple(future_map.keys()), return_when=FIRST_COMPLETED)
            for future in done_futures:
                batch, partial = future.result()
                future_map.pop(future, None)
                with lock:
                    results.update(partial)
                    done_count += len(batch)
                    if progress_callback:
                        progress_callback(min(done_count, total), total)
                if not (should_stop and should_stop()):
                    _submit_next()

    _apply_quality_filter(results, target_lang, source_lang=source_lang)
    return results


def _build_text_batches(
    texts: list[str],
    *,
    max_items: int,
    max_chars: int,
) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    item_limit = max(1, int(max_items or 1))
    char_limit = max(1, int(max_chars or 1))

    def flush() -> None:
        nonlocal current, current_chars
        if current:
            batches.append(current)
        current = []
        current_chars = 0

    for text in texts:
        text_chars = len(str(text or ""))
        if text_chars >= char_limit:
            flush()
            batches.append([text])
            continue

        would_exceed_items = len(current) >= item_limit
        would_exceed_chars = current and (current_chars + text_chars > char_limit)
        if would_exceed_items or would_exceed_chars:
            flush()

        current.append(text)
        current_chars += text_chars

    flush()
    return batches


def _translate_batch_with_fallback(
    batch: list[str],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    request_category: str,
    should_stop,
    error_callback,
    stats: TranslationBatchRunStats,
) -> dict[str, str]:
    if not batch:
        return {}
    if should_stop and should_stop():
        return {text: text for text in batch}

    request_generation: int | None = None
    try:
        request_weight = _estimate_api_request_weight(batch, system_prompt)
        stats.record_request_weight(request_weight)
        if api_scheduler is None:
            partial = engine.translate_batch(
                batch,
                target_lang,
                system_prompt,
                source_lang=source_lang,
            )
        else:
            with api_scheduler.slot(request_weight, category=request_category) as lease:
                request_generation = lease.generation
                partial = engine.translate_batch(
                    batch,
                    target_lang,
                    system_prompt,
                    source_lang=source_lang,
                )
        _validate_batch_integrity(batch, partial)
        return partial
    except Exception as exc:  # noqa: BLE001 - fallback decides the safest degradation
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None:
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label="Excel",
                error_callback=error_callback,
            )
            if decision is not None:
                stats.record_adaptive_concurrency_decision(decision)
                if should_stop and should_stop():
                    return {text: text for text in batch}
                return _translate_batch_with_fallback(
                    batch,
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    request_category=request_category,
                    should_stop=should_stop,
                    error_callback=error_callback,
                    stats=stats,
                )

        if len(batch) > 1 and not (should_stop and should_stop()):
            midpoint = max(1, len(batch) // 2)
            stats.record_retry()
            message = (
                "Excel 批次翻译失败，已缩小批次重试"
                f"（{len(batch)} -> {midpoint}+{len(batch) - midpoint}）：{exc}"
            )
            logger.warning(message)
            if error_callback:
                error_callback(message)
            left = _translate_batch_with_fallback(
                batch[:midpoint],
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=api_scheduler,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            right = _translate_batch_with_fallback(
                batch[midpoint:],
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=api_scheduler,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            return {**left, **right}

        stats.record_failed_batch(str(batch[0] if batch else ""), str(exc))
        head = str(batch[0] if batch else "")[:40]
        err_msg = f"Excel 单条翻译仍失败，已保留原文：{head}... | {exc}"
        logger.error(err_msg)
        if error_callback:
            error_callback(err_msg)
        return {text: text for text in batch}


def _validate_batch_integrity(batch: list[str], results: dict[str, str]) -> None:
    missing = [text for text in batch if text not in results]
    if missing:
        raise ValueError(f"缺少 {len(missing)} 条译文")
    if len(results) < len(set(batch)):
        raise ValueError(f"返回条数不足：输入 {len(set(batch))} 条，返回 {len(results)} 条")


def _estimate_api_request_weight(
    texts: list[str],
    system_prompt: str = "",
    *,
    chars_per_slot: int = _API_WEIGHT_CHARS_PER_SLOT,
) -> int:
    input_chars = sum(len(str(text or "")) for text in texts)
    prompt_chars = min(len(str(system_prompt or "")), _API_WEIGHT_PROMPT_CHAR_CAP)
    estimated_output_chars = int(math.ceil(input_chars * _API_WEIGHT_OUTPUT_MULTIPLIER))
    total_chars = max(1, input_chars + prompt_chars + estimated_output_chars)
    return max(1, int(math.ceil(total_chars / max(1, int(chars_per_slot)))))


def _apply_quality_filter(
    results: dict[str, str],
    target_lang: str,
    *,
    source_lang: str = "zh",
) -> None:
    """
    检测-拦截-重置闭环：
    对每条译文调用 is_translation_redundant()，若判定为无效，
    强制将译文重置为原文（Source Text），阻止损坏数据写回 Excel。
    """
    reset_count = 0
    for src in list(results.keys()):
        if not should_apply_quality_filter(results[src]):
            continue
        if is_translation_redundant(
            src,
            results[src],
            target_lang,
            source_lang=source_lang,
        ):
            results[src] = src
            reset_count += 1
    if reset_count > 0:
        logger.warning(f"因质量校验未通过，已强制保留 {reset_count} 条原文")
