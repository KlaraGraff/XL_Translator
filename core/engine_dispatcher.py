"""
翻译引擎调度层（统一入口）。
根据用户配置实例化对应引擎，对外暴露统一的 translate_batch() 接口。
云端引擎使用 ThreadPoolExecutor 并发发送批次，Ollama 保持原有 asyncio 逻辑不变。
"""
import math
import random
import threading
import time
from collections.abc import Sequence
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
    ApiSchedulerAcquireCancelled,
    WeightedApiScheduler,
)
from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.language_registry import append_prompt_block, build_target_lang_note_block
from core.language_preflight import TranslationLanguageResult
from core.translation_protocol import should_apply_quality_filter
from engines.base_engine import TranslationEngine
from settings import AppSettings, get_cloud_provider_config, get_key
from core.translation_filter import is_translation_redundant  # 质量闭环拦截

_EXCEL_CLOUD_BATCH_CHAR_BUDGET = 3200
_EXCEL_LOCAL_BATCH_CHAR_BUDGET = 2400
_API_WEIGHT_CHARS_PER_SLOT = 4000
_API_WEIGHT_PROMPT_CHAR_CAP = 900
_API_WEIGHT_OUTPUT_MULTIPLIER = 1.15

# A batch of 30 used to bisect all the way down to singletons: 59 nodes, each
# with its own tenacity budget, so one upstream wobble turned into ~177
# requests.  Three levels isolate a poison item well enough while capping the
# tree at 15 nodes, and every retry now waits instead of hammering.
_MAX_BATCH_SPLIT_DEPTH = 3
_SPLIT_RETRY_BASE_DELAY = 0.5
_SPLIT_RETRY_MAX_DELAY = 8.0
# Retries triggered by adaptive concurrency reduction: bounded so a key that
# keeps answering 429 cannot spin the same batch forever.
_MAX_CONCURRENCY_RETRY_ROUNDS = 8
_CONCURRENCY_RETRY_BASE_DELAY = 0.5
_CONCURRENCY_RETRY_MAX_DELAY = 8.0
# Failed source samples are attached to the task result for the UI; the counts
# stay exact while the sample list stays bounded.
_FAILED_ITEM_SAMPLE_LIMIT = 200


def _backoff_sleep(
    attempt: int,
    *,
    base: float,
    cap: float,
    should_stop=None,
) -> None:
    """Sleep for an exponentially growing, jittered delay.

    The wait is sliced so a stop request is honoured promptly instead of after
    the full backoff.
    """
    delay = min(cap, base * (2 ** max(0, int(attempt))))
    delay *= 0.75 + random.random() * 0.5
    deadline = time.monotonic() + delay
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if should_stop and should_stop():
            return
        time.sleep(min(0.25, remaining))


@dataclass
class TranslationBatchRunStats:
    original_count: int = 0
    batch_count: int = 0
    retry_count: int = 0
    failed_batch_count: int = 0
    untranslated_count: int = 0
    failed_items: list[dict[str, str]] = field(default_factory=list)
    quality_reset_count: int = 0
    quality_reset_items: list[str] = field(default_factory=list)
    # API 失败回退（译文=原文）的全量源文集合。failed_items 是限量采样，
    # 质量校验去重必须用全集，否则超出采样上限的条目仍会被二次计数。
    untranslated_sources: set[str] = field(default_factory=set)
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
            if source_text and len(self.failed_items) < _FAILED_ITEM_SAMPLE_LIMIT:
                self.failed_items.append(
                    {
                        "source": source_text,
                        "error": error,
                    }
                )

    def record_quality_reset(self, source_text: str) -> None:
        """质量校验把译文重置回原文时留痕：条目必须能被任务层看见并上报。"""
        with self._lock:
            self.quality_reset_count += 1
            if source_text and len(self.quality_reset_items) < _FAILED_ITEM_SAMPLE_LIMIT:
                self.quality_reset_items.append(source_text)

    def record_untranslated(self, texts: Sequence[str], error: str = "") -> None:
        """Mark every entry of a batch as returned without a translation.

        The fallback still hands the source text back so the rest of the run
        can finish and the file gets written, but the run must never present
        that as a translation: these counts drive the task-level "N 条未翻译"
        warning, so a batch that silently degraded is impossible.
        """
        items = list(texts)
        for text in items:
            self.record_failed_batch(str(text or ""), error)
        with self._lock:
            self.untranslated_count += len(items)
            self.untranslated_sources.update(str(text or "") for text in items)

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

    if provider in ("openai", "siliconflow", "custom_openai", "lanyi", "deepseek"):
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


def build_role_engine(
    settings: AppSettings,
    role: str,
    *,
    connection_ids: Sequence[str] = (),
    on_switch=None,
) -> TranslationEngine:
    """Build an engine for one role, with failover when a chain is supplied.

    With fewer than two usable connections this returns the plain engine, so
    the common single-connection setup keeps exactly the behaviour it had.
    """
    from core.model_roles import (
        connection_key_overrides,
        list_effective_role_connections,
        resolve_effective_model_config,
        settings_for_text_role,
    )
    from settings import current_key_overrides, provider_key_overrides

    # A runtime switch builds the next engine on a pool worker thread, where
    # the task's thread-local key snapshot is invisible; capture it here and
    # re-enter it around every build so a running task keeps the credentials
    # it started with.
    key_overrides = current_key_overrides()

    def _engine_for(connection) -> TranslationEngine:
        role_settings = settings_for_text_role(
            settings,
            role,
            connection_id=connection.id,
        )
        with provider_key_overrides(key_overrides):
            # Resolve inside the snapshot so a running task keeps the credentials
            # it started with; the connection's own ``conn::`` key wins there too.
            config = resolve_effective_model_config(
                settings,
                role,
                connection_id=connection.id,
            )
        # ``build_engine`` only ever asks ``get_key(provider, base_url)``, and the
        # snapshot pins that scope to the connection the task started on.  Two pool
        # entries on one provider and Base URL — the same service, two accounts —
        # therefore both dialled the first entry's key, so failing over after a
        # quota rejection replayed the credential that had just been rejected.
        # Layer this candidate's own key over that scope for its own build.
        merged = dict(key_overrides or {})
        merged.update(connection_key_overrides(config))
        with provider_key_overrides(merged):
            return build_engine(role_settings)

    # The chain ids were generated from the effective (follow-resolved) pool,
    # so resolve them there too: a following role's own pool holds ids nothing
    # ever dialed, and resolving against it degraded every entry to the
    # primary, silently disabling failover.  An id that has since been removed
    # from the pool still degrades to the primary.
    pool = list_effective_role_connections(settings, role)
    by_id = {connection.id: connection for connection in pool}
    primary = pool[0] if pool else None
    chain = [
        by_id.get(str(connection_id or "").strip()) or primary
        for connection_id in connection_ids
        if str(connection_id or "").strip()
    ]
    # Drop repeats caused by ids that no longer exist and fell back to primary.
    unique: list = []
    seen: set[str] = set()
    for connection in chain:
        if connection is not None and connection.id not in seen:
            seen.add(connection.id)
            unique.append(connection)

    if len(unique) < 2:
        return build_engine(settings_for_text_role(settings, role))

    from core.failover_engine import FailoverTranslationEngine

    return FailoverTranslationEngine(
        build_engine_for=_engine_for,
        candidates=unique,
        on_switch=on_switch,
    )


def get_system_prompt(
    settings: AppSettings,
    target_lang: str = "",
    source_lang: str = "zh",
    page_key: str = "",
) -> str:
    """
    根据领域预设和目标语言生成最终 System Prompt。

    DOMAIN_PRESETS 支持两种格式：
      - dict[str, str]：单语言（旧格式，向下兼容）
      - dict[str, dict[str, str]]：多语言，内层 key 为 lang 代码 或 "_base"
    """
    normalized_page = str(page_key or "").strip().lower()
    if normalized_page in {"excel", "word"}:
        prefix = f"{normalized_page}_"
        domain_preset = str(
            getattr(settings, f"{prefix}domain_preset", settings.domain_preset)
            or settings.domain_preset
        ).strip()
        custom_prompt = str(
            getattr(settings, f"{prefix}custom_prompt", settings.custom_prompt)
            or ""
        )
        domain_prompt_overrides = dict(
            getattr(
                settings,
                f"{prefix}domain_prompt_overrides",
                settings.domain_prompt_overrides,
            )
            or {}
        )
    else:
        domain_preset = str(settings.domain_preset or "").strip()
        custom_prompt = str(settings.custom_prompt or "")
        domain_prompt_overrides = dict(settings.domain_prompt_overrides or {})

    if domain_preset == "自定义":
        if not custom_prompt.strip():
            raise ValueError("自定义领域 Prompt 不能为空")
        prompt = custom_prompt
        return append_prompt_block(
            prompt,
            build_target_lang_note_block(target_lang, settings.custom_target_langs),
        )
    # 用户自定义覆盖优先于内置预设
    if domain_preset in domain_prompt_overrides:
        prompt = domain_prompt_overrides[domain_preset]
        return append_prompt_block(
            prompt,
            build_target_lang_note_block(target_lang, settings.custom_target_langs),
        )
    preset = DOMAIN_PRESETS.get(domain_preset, "")
    prompt = ""
    if isinstance(preset, dict):
        prompt = preset.get(target_lang) or preset.get("_base", "")
    else:
        prompt = preset
    return append_prompt_block(
        prompt,
        build_target_lang_note_block(target_lang, settings.custom_target_langs),
    )


def activate_translation_surface(settings: AppSettings, surface: str) -> AppSettings:
    """Select the page-owned domain/Prompt state for a frozen task copy."""
    normalized = str(surface or "").strip().lower()
    if normalized not in {"excel", "word"}:
        return settings
    prefix = f"{normalized}_"
    settings.domain_preset = str(
        getattr(settings, f"{prefix}domain_preset", settings.domain_preset)
        or "同步工程场景"
    ).strip()
    settings.custom_prompt = str(
        getattr(settings, f"{prefix}custom_prompt", settings.custom_prompt) or ""
    )
    settings.domain_name_overrides = dict(
        getattr(settings, f"{prefix}domain_name_overrides", settings.domain_name_overrides)
        or {}
    )
    settings.domain_prompt_overrides = dict(
        getattr(settings, f"{prefix}domain_prompt_overrides", settings.domain_prompt_overrides)
        or {}
    )
    return settings


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
        _apply_quality_filter(
            results, target_lang, source_lang=source_lang, stats=run_stats
        )
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

    _apply_quality_filter(
        results, target_lang, source_lang=source_lang, stats=run_stats
    )
    return results


def translate_texts_with_sources(
    texts: list[str],
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    batch_size: int,
    concurrency: int,
    progress_callback=None,
    error_callback=None,
    should_stop=None,
    api_scheduler: WeightedApiScheduler | None = None,
    request_category: str = API_REQUEST_CATEGORY_NORMAL,
    stats: TranslationBatchRunStats | None = None,
) -> dict[str, TranslationLanguageResult]:
    """Translate bounded batches while retaining model-reported source codes.

    This path is deliberately used only by automatic-language workflows.  It
    keeps the normal legacy string protocol intact and gives automatic TM
    insertion a real per-item language rather than an inferred ``auto-*``.
    """
    if not texts:
        return {}
    is_local = is_local_engine_name(engine.engine_name)
    item_limit = (
        max(CHUNK_LOCAL_MIN, min(CHUNK_LOCAL_MAX, batch_size))
        if is_local
        else max(CHUNK_CLOUD_MIN, min(CHUNK_CLOUD_MAX, batch_size))
    )
    char_budget = _EXCEL_LOCAL_BATCH_CHAR_BUDGET if is_local else _EXCEL_CLOUD_BATCH_CHAR_BUDGET
    batches = _build_text_batches(texts, max_items=item_limit, max_chars=char_budget)
    run_stats = stats or TranslationBatchRunStats()
    run_stats.original_count = len(texts)
    run_stats.batch_count = len(batches)
    scheduler = None if is_local else (api_scheduler or WeightedApiScheduler(max(1, concurrency)))
    results: dict[str, TranslationLanguageResult] = {}
    done = 0
    for batch in batches:
        if should_stop and should_stop():
            break
        partial = _translate_batch_with_sources_fallback(
            batch,
            engine=engine,
            target_lang=target_lang,
            system_prompt=system_prompt,
            api_scheduler=scheduler,
            request_category=request_category,
            should_stop=should_stop,
            error_callback=error_callback,
            stats=run_stats,
        )
        results.update(partial)
        done += len(batch)
        if progress_callback:
            progress_callback(min(done, len(texts)), len(texts))
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
    split_depth: int = 0,
    concurrency_round: int = 0,
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
                batch, target_lang, system_prompt, source_lang=source_lang
            )
        else:
            with api_scheduler.slot(
                request_weight,
                category=request_category,
                should_stop=should_stop,
            ) as lease:
                request_generation = lease.generation
                if should_stop and should_stop():
                    return {text: text for text in batch}
                partial = engine.translate_batch(
                    batch, target_lang, system_prompt, source_lang=source_lang
                )
        _validate_batch_integrity(batch, partial)
        return partial
    except Exception as exc:  # noqa: BLE001 - fallback decides the safest degradation
        if isinstance(exc, ApiSchedulerAcquireCancelled):
            return {text: text for text in batch}
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None and concurrency_round < _MAX_CONCURRENCY_RETRY_ROUNDS:
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label="Excel",
                error_callback=error_callback,
                should_stop=should_stop,
            )
            if decision is not None:
                stats.record_adaptive_concurrency_decision(decision)
                if should_stop and should_stop():
                    return {text: text for text in batch}
                # Retrying the identical batch the instant the cap dropped is
                # what turned upstream 429 jitter into a local request storm.
                _backoff_sleep(
                    concurrency_round,
                    base=_CONCURRENCY_RETRY_BASE_DELAY,
                    cap=_CONCURRENCY_RETRY_MAX_DELAY,
                    should_stop=should_stop,
                )
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
                    split_depth=split_depth,
                    concurrency_round=concurrency_round + 1,
                )
        if _is_permanent_request_error(exc):
            message = f"Excel 翻译请求不可重试，已停止拆分批次：{exc}"
            logger.error(message)
            if error_callback:
                error_callback(message)
            stats.record_untranslated(batch, str(exc))
            return {text: text for text in batch}

        can_split = (
            len(batch) > 1
            and split_depth < _MAX_BATCH_SPLIT_DEPTH
            and not (should_stop and should_stop())
        )
        if can_split:
            midpoint = max(1, len(batch) // 2)
            stats.record_retry()
            message = (
                "Excel 批次翻译失败，已缩小批次重试"
                f"（{len(batch)} -> {midpoint}+{len(batch) - midpoint}）：{exc}"
            )
            logger.warning(message)
            if error_callback:
                error_callback(message)
            _backoff_sleep(
                split_depth,
                base=_SPLIT_RETRY_BASE_DELAY,
                cap=_SPLIT_RETRY_MAX_DELAY,
                should_stop=should_stop,
            )
            left = _translate_batch_with_fallback(
                batch[:midpoint], engine=engine, target_lang=target_lang,
                system_prompt=system_prompt, source_lang=source_lang,
                api_scheduler=api_scheduler, request_category=request_category,
                should_stop=should_stop, error_callback=error_callback, stats=stats,
                split_depth=split_depth + 1,
            )
            right = _translate_batch_with_fallback(
                batch[midpoint:], engine=engine, target_lang=target_lang,
                system_prompt=system_prompt, source_lang=source_lang,
                api_scheduler=api_scheduler, request_category=request_category,
                should_stop=should_stop, error_callback=error_callback, stats=stats,
                split_depth=split_depth + 1,
            )
            return {**left, **right}
        stats.record_untranslated(batch, str(exc))
        head = str(batch[0] if batch else "")[:40]
        err_msg = (
            f"Excel 有 {len(batch)} 条未能翻译，已原样保留并计入未翻译条数："
            f"{head}... | {exc}"
        )
        logger.error(err_msg)
        if error_callback:
            error_callback(err_msg)
        return {text: text for text in batch}


def _translate_batch_with_sources_fallback(
    batch: list[str],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    api_scheduler: WeightedApiScheduler | None,
    request_category: str,
    should_stop,
    error_callback,
    stats: TranslationBatchRunStats,
    split_depth: int = 0,
    concurrency_round: int = 0,
) -> dict[str, TranslationLanguageResult]:
    if not batch:
        return {}
    if should_stop and should_stop():
        return {
            text: TranslationLanguageResult(text, text, source_lang="und", target_lang=target_lang)
            for text in batch
        }
    request_generation: int | None = None
    try:
        weight = _estimate_api_request_weight(batch, system_prompt)
        stats.record_request_weight(weight)
        if api_scheduler is None:
            items = engine.translate_batch_with_sources(
                batch, target_lang, system_prompt, source_lang="auto"
            )
        else:
            with api_scheduler.slot(
                weight,
                category=request_category,
                should_stop=should_stop,
            ) as lease:
                request_generation = lease.generation
                if should_stop and should_stop():
                    return {
                        text: TranslationLanguageResult(text, text, source_lang="und", target_lang=target_lang)
                        for text in batch
                    }
                items = engine.translate_batch_with_sources(
                    batch, target_lang, system_prompt, source_lang="auto"
                )
        if len(items) != len(batch):
            raise ValueError(f"返回条数不足：输入 {len(batch)}，返回 {len(items)}")
        return {item.source_text: item for item in items}
    except Exception as exc:  # noqa: BLE001 - retain a safe non-TM fallback
        if isinstance(exc, ApiSchedulerAcquireCancelled):
            return {
                text: TranslationLanguageResult(text, text, source_lang="und", target_lang=target_lang)
                for text in batch
            }
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None and concurrency_round < _MAX_CONCURRENCY_RETRY_ROUNDS:
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label="Excel",
                error_callback=error_callback,
                should_stop=should_stop,
            )
            if decision is not None:
                stats.record_adaptive_concurrency_decision(decision)
                if should_stop and should_stop():
                    return _untranslated_language_results(batch, target_lang)
                _backoff_sleep(
                    concurrency_round,
                    base=_CONCURRENCY_RETRY_BASE_DELAY,
                    cap=_CONCURRENCY_RETRY_MAX_DELAY,
                    should_stop=should_stop,
                )
                if should_stop and should_stop():
                    return _untranslated_language_results(batch, target_lang)
                return _translate_batch_with_sources_fallback(
                    batch,
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    api_scheduler=api_scheduler,
                    request_category=request_category,
                    should_stop=should_stop,
                    error_callback=error_callback,
                    stats=stats,
                    split_depth=split_depth,
                    concurrency_round=concurrency_round + 1,
                )
        # Splitting a batch cannot fix a rejected key or a missing model, and
        # the sources variant used to keep bisecting anyway — mirror the plain
        # path so a permanent failure costs one request, not fifteen.
        if _is_permanent_request_error(exc):
            message = f"Excel 语言回报请求不可重试，已停止拆分批次：{exc}"
            logger.error(message)
            if error_callback:
                error_callback(message)
            stats.record_untranslated(batch, str(exc))
            return _untranslated_language_results(batch, target_lang)

        can_split = (
            len(batch) > 1
            and split_depth < _MAX_BATCH_SPLIT_DEPTH
            and not (should_stop and should_stop())
        )
        if can_split:
            stats.record_retry()
            midpoint = max(1, len(batch) // 2)
            _backoff_sleep(
                split_depth,
                base=_SPLIT_RETRY_BASE_DELAY,
                cap=_SPLIT_RETRY_MAX_DELAY,
                should_stop=should_stop,
            )
            left = _translate_batch_with_sources_fallback(
                batch[:midpoint], engine=engine, target_lang=target_lang,
                system_prompt=system_prompt, api_scheduler=api_scheduler,
                request_category=request_category, should_stop=should_stop,
                error_callback=error_callback, stats=stats,
                split_depth=split_depth + 1,
            )
            right = _translate_batch_with_sources_fallback(
                batch[midpoint:], engine=engine, target_lang=target_lang,
                system_prompt=system_prompt, api_scheduler=api_scheduler,
                request_category=request_category, should_stop=should_stop,
                error_callback=error_callback, stats=stats,
                split_depth=split_depth + 1,
            )
            return {**left, **right}
        stats.record_untranslated(batch, str(exc))
        err_msg = f"Excel 有 {len(batch)} 条未能翻译，已原样保留并计入未翻译条数：{exc}"
        logger.error(err_msg)
        if error_callback:
            error_callback(err_msg)
        return _untranslated_language_results(batch, target_lang)


def _untranslated_language_results(
    batch: Sequence[str],
    target_lang: str,
) -> dict[str, TranslationLanguageResult]:
    """Hand the source text back, tagged so nothing downstream calls it a translation.

    ``source_lang="und"`` keeps these entries out of TM, and the run stats
    carry the matching untranslated count for the task result.
    """
    return {
        text: TranslationLanguageResult(
            text,
            text,
            source_lang="und",
            target_lang=target_lang,
        )
        for text in batch
    }


def _is_permanent_request_error(exc: BaseException) -> bool:
    """Return True when shrinking a batch cannot change the upstream outcome."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 0
    if status in {400, 401, 402, 403, 404, 405, 410, 422}:
        return True

    message = str(exc or "").casefold()
    permanent_markers = (
        "invalid api key",
        "incorrect api key",
        "unauthorized",
        "forbidden",
        "insufficient_quota",
        "billing hard limit",
        "model not found",
        "does not exist",
        "余额不足",
        "额度不足",
        "未授权",
        "模型不存在",
    )
    return any(marker in message for marker in permanent_markers)


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
    stats: TranslationBatchRunStats | None = None,
) -> None:
    """
    检测-拦截-重置闭环：
    对每条译文调用 is_translation_redundant()，若判定为无效，
    强制将译文重置为原文（Source Text），阻止损坏数据写回 Excel。

    重置不允许静默：每条被重置的原文都记入 stats.quality_reset_items，
    由任务层汇入结果报告——文件里保留原文的格子必须在报告里有对应条目。
    """
    reset_count = 0
    already_untranslated = (
        stats.untranslated_sources if stats is not None else frozenset()
    )
    for src in list(results.keys()):
        if src in already_untranslated:
            # API 失败回退的条目已按「未翻译」上报过。这里译文必然等于原文，
            # 再计一次「质量校验回退」会把服务故障误报成译文质量问题，
            # 同一格子在报告里出现两条互相矛盾的 needs_action。
            continue
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
            if stats is not None:
                stats.record_quality_reset(src)
    if reset_count > 0:
        logger.warning(f"因质量校验未通过，已强制保留 {reset_count} 条原文")
