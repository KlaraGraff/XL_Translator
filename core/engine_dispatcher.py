"""
翻译引擎调度层（统一入口）。
根据用户配置实例化对应引擎，对外暴露统一的 translate_batch() 接口。
云端引擎使用 ThreadPoolExecutor 并发发送批次，Ollama 保持原有 asyncio 逻辑不变。
"""
import math
import random
import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from loguru import logger

from core.text_transport import FAILED_REQUEST_CACHE_SECONDS, cancellable_text_operation
from core.request_progress import (
    BATCH_EVENT_DONE,
    BATCH_EVENT_PLAN,
    BATCH_EVENT_SENT,
    BatchRequestEvent,
    notify_batch_event,
)
from config import (
    CORE_TRANSLATION_PROMPT,
    EXTRA_PROMPT_HEADER,
    NO_EXTRA_PROMPT,
    CHUNK_CLOUD_MAX,
    CHUNK_LOCAL_MAX,
    LM_STUDIO_BASE_URL,
    OLLAMA_BASE_URL,
    REQUEST_RETRY_ATTEMPTS_DEFAULT,
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
    credential_rejection,
    handle_api_concurrency_limit,
    is_api_concurrency_limit_error,
)
from core.language_registry import append_prompt_block, build_target_lang_note_block
from core.language_preflight import TranslationLanguageResult
from core.prompt_cache import carry_meter
from core.surface_settings import is_extension_key, surface_setting
from core.translation_protocol import should_apply_quality_filter
from core.user_facing_errors import humanize_error
from engines.base_engine import TranslationEngine
from settings import AppSettings, get_cloud_provider_config, get_key
from core.translation_filter import is_translation_redundant  # 质量闭环拦截

# 调用方没给每批字符上限时的兜底（v38 起 Excel、CAD、PDF 原位替换都按「翻译参数」
# 传入，见 core/surface_rate.py）。
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
# A 5xx, a timeout or a connection dropped mid-answer is usually gone a few
# seconds later.  The text transport never resends on its own (it marks those
# errors no_replay), so this layer resends the same batch after a pause, as
# many times as 翻译参数 → 失败重试次数 says.  Splitting would not help a
# server that is down.
DEFAULT_TRANSIENT_RETRIES = REQUEST_RETRY_ATTEMPTS_DEFAULT
# The first wait must clear the transport's failed-request cache even at the
# low end of the jitter (x0.75), or the first resend never leaves the machine.
_TRANSIENT_RETRY_BASE_DELAY = FAILED_REQUEST_CACHE_SECONDS * 1.5
_TRANSIENT_RETRY_MAX_DELAY = 8.0
_TRANSIENT_TRANSPORT_KINDS = frozenset({
    "connect_timeout", "read_timeout", "total_timeout", "incomplete",
})
_SLOW_ANSWER_KINDS = frozenset({"read_timeout", "total_timeout"})
# Failed source samples are attached to the task result for the UI; the counts
# stay exact while the sample list stays bounded.
_FAILED_ITEM_SAMPLE_LIMIT = 200


def _run_batch_with_events(
    run, batch: list[str], *, index: int, batch_total: int, callback, stats: "TranslationBatchRunStats"
) -> dict[str, str]:
    """Wrap one top-level batch with ``sent`` / ``done`` events."""
    if callback is None:
        return run(batch)
    notify_batch_event(
        callback, BatchRequestEvent(BATCH_EVENT_SENT, index=index, batch_total=batch_total, items=len(batch))
    )
    started = time.monotonic()
    partial = run(batch)
    stopped = sum(1 for text in batch if text in stats.stopped_sources)
    failed = sum(1 for text in batch if text in stats.untranslated_sources)
    notify_batch_event(
        callback,
        BatchRequestEvent(
            BATCH_EVENT_DONE,
            index=index,
            batch_total=batch_total,
            items=len(batch),
            elapsed=time.monotonic() - started,
            failed=failed,
            stopped=stopped,
        ),
    )
    return partial


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
    # Whole-batch resends after a 5xx / timeout; retry_count stays splits only.
    transient_retry_count: int = 0
    # 拆到最小仍没译出来、逐条单独重发的请求数（翻译参数 → 单段重试次数）。
    item_retry_count: int = 0
    failed_batch_count: int = 0
    untranslated_count: int = 0
    failed_items: list[dict[str, str]] = field(default_factory=list)
    quality_reset_count: int = 0
    quality_reset_items: list[str] = field(default_factory=list)
    # API 失败回退（译文=原文）的全量源文集合。failed_items 是限量采样，
    # 质量校验去重必须用全集，否则超出采样上限的条目仍会被二次计数。
    untranslated_sources: set[str] = field(default_factory=set)
    # 用户按停止时还没翻、按原文占位交回的条目。它们不是接口出错（不进
    # untranslated_count / failed_items），调用方据此把占位从译文表里摘掉，
    # 写盘时保留原文、续译时重新排队，也不会被当成「API 没给译文」去吓用户。
    stopped_sources: set[str] = field(default_factory=set)
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

    def record_transient_retry(self) -> None:
        with self._lock:
            self.transient_retry_count += 1

    def record_item_retry(self) -> None:
        with self._lock:
            self.item_retry_count += 1

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

    def record_stopped(self, texts: Sequence[str]) -> None:
        """停止时整批没翻、原文占位交回——停止不是失败，不计入未翻译条数。"""
        with self._lock:
            self.stopped_sources.update(str(text or "") for text in texts)

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
    cloud_base_url = provider_config.cloud_base_url
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
            api_mode=provider_config.api_mode,
            connection_id=s.connections[0].id if s.connections else "",
            model_role=getattr(settings, "_runtime_model_role", "translation"),
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
    """拼出发给文档翻译模型的 System Prompt（不含返回格式，那段由引擎追加）。

    内置规则（CORE_TRANSLATION_PROMPT）始终在最前面、用户改不了；用户选了「补充要求」
    时接在后面，只追加、不替换。「自动识别场景」的文档说明由各任务在拿到之后用
    core.document_profile.append_profile 接上，不在这里。
    """
    normalized_page = str(page_key or "").strip().lower()
    # 有自己提示词设置的页面：Excel、Word、CAD 和运行时注册的类型；其余用全局那份。
    page = normalized_page if normalized_page in {"excel", "word", "cad"} or is_extension_key(normalized_page) else ""
    selected = str(
        (surface_setting(settings, page, "domain_preset", settings.domain_preset) if page else settings.domain_preset)
        or NO_EXTRA_PROMPT
    ).strip()
    extras = dict(
        (surface_setting(settings, page, "domain_custom_prompts", None) if page else settings.domain_custom_prompts)
        or {}
    )
    extra = ""
    entry = extras.get(selected) if selected != NO_EXTRA_PROMPT else None
    if isinstance(entry, dict):
        # 正常数据只有一份 ``_base``；旧版按目标语言存的映射仍按当前语言读。
        extra = str(entry.get("_base") or entry.get(target_lang) or "").strip()
        if not extra:
            extra = next(
                (str(value).strip() for value in entry.values() if isinstance(value, str) and value.strip()),
                "",
            )
    elif isinstance(entry, str):
        extra = entry.strip()
    elif selected == "自定义":
        # 没经过 v35 迁移的旧调用方（CLI 手工构造的设置）还可能用 V9 的单条自定义提示词。
        extra = str(
            (surface_setting(settings, page, "custom_prompt", settings.custom_prompt) if page else settings.custom_prompt)
            or ""
        ).strip()
    prompt = CORE_TRANSLATION_PROMPT
    if extra:
        prompt = f"{prompt}\n\n{EXTRA_PROMPT_HEADER}\n{extra}"
    return append_prompt_block(
        prompt,
        build_target_lang_note_block(target_lang, settings.custom_target_langs),
    )


def smart_context_enabled(settings: AppSettings, page_key: str) -> bool:
    """「自动识别场景」开关：这一页翻译前要不要先通读文档、写一段文档说明。"""
    normalized_page = str(page_key or "").strip().lower()
    if normalized_page in {"excel", "word", "cad"} or is_extension_key(normalized_page):
        return bool(surface_setting(settings, normalized_page, "smart_context", True))
    return bool(getattr(settings, "domain_smart_context", True))


def activate_translation_surface(settings: AppSettings, surface: str) -> AppSettings:
    """Select the page-owned domain/Prompt state for a frozen task copy."""
    normalized = str(surface or "").strip().lower()
    if normalized not in {"excel", "word"} and not is_extension_key(normalized):
        return settings
    settings.domain_preset = str(
        surface_setting(settings, normalized, "domain_preset", settings.domain_preset)
        or NO_EXTRA_PROMPT
    ).strip()
    settings.domain_smart_context = bool(
        surface_setting(settings, normalized, "smart_context", settings.domain_smart_context)
    )
    settings.custom_prompt = str(
        surface_setting(settings, normalized, "custom_prompt", settings.custom_prompt) or ""
    )
    settings.domain_name_overrides = dict(
        surface_setting(settings, normalized, "domain_name_overrides", settings.domain_name_overrides)
        or {}
    )
    settings.domain_prompt_overrides = dict(
        surface_setting(settings, normalized, "domain_prompt_overrides", settings.domain_prompt_overrides)
        or {}
    )
    settings.domain_custom_prompts = dict(
        surface_setting(settings, normalized, "domain_custom_prompts", {})
        or {}
    )
    settings.domain_disabled_presets = list(
        surface_setting(settings, normalized, "domain_disabled_presets", []) or []
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
    throughput_unlocked: bool = False,
    request_callback=None,
    transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
    max_chars: int | None = None,
    item_retries: int = 0,
) -> dict[str, str]:
    """
    将 texts 分批送入 engine，汇总返回 {原文: 译文}。

    - max_chars：每批原文字符数之和的上限（翻译参数 → 每批字符上限）；不给时按
      云端 3200 / 本地 2400 兜底。
    - item_retries：拆到最小仍没译出来的条目逐条单独重发的次数（单段重试次数）。

    - Ollama（mode=local）：engine.translate_batch 内部已有 asyncio 并发，
      此处保持串行逐批调用，避免与 asyncio.run() 冲突。
    - 云端引擎：使用 ThreadPoolExecutor 并发提交所有批次，
      progress_callback 通过 Lock 保证线程安全。
    - request_callback（可选）收到每一批的 BatchRequestEvent：开始前一次 plan，
      每批发出时 sent、返回时 done（在执行该批的线程里调用，需自行线程安全）。
    """
    if not texts:
        return {}

    # 最后一道参数钳位：强制将 batch_size 限制在合规区间，
    # 防止配置脏数据或 UI 传参异常导致越界请求。
    is_local = is_local_engine_name(engine.engine_name)
    if is_local:
        maximum = 2**31 - 1 if throughput_unlocked else CHUNK_LOCAL_MAX
        chunk = max(1, min(maximum, int(batch_size)))
    else:
        maximum = 2**31 - 1 if throughput_unlocked else CHUNK_CLOUD_MAX
        chunk = max(1, min(maximum, int(batch_size)))

    char_budget = _batch_char_budget(max_chars, is_local)
    batches = _build_text_batches(texts, max_items=chunk, max_chars=char_budget)
    total = len(texts)
    run_stats = stats or TranslationBatchRunStats()
    run_stats.original_count = total
    run_stats.batch_count = len(batches)

    # Ollama 走原有串行路径，不引入线程池（内部已有 asyncio 并发）
    if is_local:
        notify_batch_event(
            request_callback,
            BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=len(batches), items=total, concurrency=1),
        )
        results: dict[str, str] = {}
        done = 0
        for batch_index, batch in enumerate(batches, start=1):
            if should_stop and should_stop():
                logger.info("翻译任务收到停止信号，停止提交后续本地批次。")
                break
            partial = _run_batch_with_events(
                lambda current: _translate_batch_with_fallback(
                    current,
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    source_lang=source_lang,
                    api_scheduler=None,
                    request_category=request_category,
                    should_stop=should_stop,
                    error_callback=error_callback,
                    stats=run_stats,
                    max_transient_retries=transient_retries,
                    item_retries=item_retries,
                ),
                batch,
                index=batch_index,
                batch_total=len(batches),
                callback=request_callback,
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

    def _submit_batch(batch_index: int, batch: list[str]) -> tuple[list[str], dict[str, str]]:
        """单批次执行，返回 (原始词条列表, 翻译结果)。失败时会缩小批次重试。"""
        return batch, _run_batch_with_events(
            lambda current: _translate_batch_with_fallback(
                current,
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=scheduler,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=run_stats,
                max_transient_retries=transient_retries,
                item_retries=item_retries,
            ),
            batch,
            index=batch_index,
            batch_total=len(batches),
            callback=request_callback,
            stats=run_stats,
        )

    notify_batch_event(
        request_callback,
        BatchRequestEvent(
            BATCH_EVENT_PLAN,
            batch_total=len(batches),
            items=total,
            concurrency=min(max_workers, len(batches)),
        ),
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map: dict = {}
        batch_iter = iter(enumerate(batches, start=1))

        def _submit_next() -> bool:
            if should_stop and should_stop():
                return False
            try:
                batch_index, batch = next(batch_iter)
            except StopIteration:
                return False
            future = executor.submit(carry_meter(_submit_batch), batch_index, batch)
            future_map[future] = batch
            return True

        for _ in range(min(max_workers, len(batches))):
            if not _submit_next():
                break

        # key 被判「暂时不能用」时整轮任务会以致命错误收场，但在那之前已经有批次
        # 翻好、付过钱了。以前第一个异常直接从这里抛出去，这些译文跟着局部变量
        # 一起丢掉（审计 高-6）。现在不再提交新批次，等已经在路上的批次落定，
        # 把拿到的译文挂在异常上带出去，由调用方决定怎么保住它们。
        fatal: ApiKeyTemporarilyUnavailableError | None = None
        while future_map:
            done_futures, _ = wait(tuple(future_map.keys()), return_when=FIRST_COMPLETED)
            for future in done_futures:
                future_map.pop(future, None)
                try:
                    batch, partial = future.result()
                except ApiKeyTemporarilyUnavailableError as exc:
                    fatal = fatal or exc
                    continue
                with lock:
                    results.update(partial)
                    done_count += len(batch)
                    if progress_callback:
                        progress_callback(min(done_count, total), total)
                if fatal is None and not (should_stop and should_stop()):
                    _submit_next()

    _apply_quality_filter(
        results, target_lang, source_lang=source_lang, stats=run_stats
    )
    if fatal is not None:
        _attach_partial_results(fatal, results)
        raise fatal
    return results


def _attach_partial_results(exc: BaseException, results: dict) -> None:
    """Hand already-paid translations to the caller on a task-fatal API error."""
    try:
        exc.partial_results = dict(results)  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - exceptions with __slots__
        pass


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
    throughput_unlocked: bool = False,
    transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
    request_callback=None,
    max_chars: int | None = None,
    item_retries: int = 0,
) -> dict[str, TranslationLanguageResult]:
    """Translate bounded batches while retaining model-reported source codes.

    This path is deliberately used only by automatic-language workflows.  It
    keeps the normal legacy string protocol intact and gives automatic TM
    insertion a real per-item language rather than an inferred ``auto-*``.
    ``request_callback`` gets the same per-batch events as ``translate_texts``.
    """
    if not texts:
        return {}
    is_local = is_local_engine_name(engine.engine_name)
    item_limit = (
        max(1, min(2**31 - 1 if throughput_unlocked else CHUNK_LOCAL_MAX, batch_size))
        if is_local
        else max(1, min(2**31 - 1 if throughput_unlocked else CHUNK_CLOUD_MAX, batch_size))
    )
    char_budget = _batch_char_budget(max_chars, is_local)
    batches = _build_text_batches(texts, max_items=item_limit, max_chars=char_budget)
    run_stats = stats or TranslationBatchRunStats()
    run_stats.original_count = len(texts)
    run_stats.batch_count = len(batches)
    scheduler = None if is_local else (api_scheduler or WeightedApiScheduler(max(1, concurrency)))
    results: dict[str, TranslationLanguageResult] = {}
    done = 0

    def _run_batch(batch_index: int, batch: list[str]) -> dict[str, TranslationLanguageResult]:
        return _run_batch_with_events(
            lambda current: _translate_batch_with_sources_fallback(
                current,
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                api_scheduler=scheduler,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=run_stats,
                max_transient_retries=transient_retries,
                item_retries=item_retries,
            ),
            batch,
            index=batch_index,
            batch_total=len(batches),
            callback=request_callback,
            stats=run_stats,
        )

    def _record(batch: list[str], partial: dict[str, TranslationLanguageResult]) -> None:
        nonlocal done
        results.update(partial)
        done += len(batch)
        if progress_callback:
            progress_callback(min(done, len(texts)), len(texts))

    if is_local:
        notify_batch_event(
            request_callback,
            BatchRequestEvent(BATCH_EVENT_PLAN, batch_total=len(batches), items=len(texts), concurrency=1),
        )
        # 本地引擎同 translate_texts：串行提交，并发交给引擎自己。
        for batch_index, batch in enumerate(batches, start=1):
            if should_stop and should_stop():
                break
            try:
                partial = _run_batch(batch_index, batch)
            except ApiKeyTemporarilyUnavailableError as exc:
                # 同 translate_texts：前面已付费的批次随异常带出去（审计 高-6）。
                _attach_partial_results(exc, results)
                raise
            _record(batch, partial)
        return results

    # 云端引擎和 translate_texts 一样按并发数同时提交。这里以前是逐批串行：
    # 源语言选「自动识别」（Excel 的默认）时，并发设置形同虚设，一份 30 批的
    # 表格要排队等 30 次请求，比指定源语言慢好几倍。
    max_workers = max(1, int(concurrency))
    notify_batch_event(
        request_callback,
        BatchRequestEvent(
            BATCH_EVENT_PLAN,
            batch_total=len(batches),
            items=len(texts),
            concurrency=min(max_workers, len(batches)),
        ),
    )
    fatal: ApiKeyTemporarilyUnavailableError | None = None
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map: dict = {}
        batch_iter = iter(enumerate(batches, start=1))

        def _submit_next() -> bool:
            if should_stop and should_stop():
                return False
            try:
                batch_index, batch = next(batch_iter)
            except StopIteration:
                return False
            future_map[executor.submit(carry_meter(_run_batch), batch_index, batch)] = batch
            return True

        for _ in range(min(max_workers, len(batches))):
            if not _submit_next():
                break
        while future_map:
            done_futures, _ = wait(tuple(future_map.keys()), return_when=FIRST_COMPLETED)
            for future in done_futures:
                batch = future_map.pop(future)
                try:
                    partial = future.result()
                except ApiKeyTemporarilyUnavailableError as exc:
                    # 不再提交新批次，等在途的落定，已付费的译文随异常带出去（审计 高-6）。
                    fatal = fatal or exc
                    continue
                _record(batch, partial)
                if fatal is None and not (should_stop and should_stop()):
                    _submit_next()
    if fatal is not None:
        _attach_partial_results(fatal, results)
        raise fatal
    return results


def _batch_char_budget(max_chars: int | None, is_local: bool) -> int:
    if max_chars is not None:
        try:
            return max(1, int(max_chars))
        except (TypeError, ValueError):
            pass
    return _EXCEL_LOCAL_BATCH_CHAR_BUDGET if is_local else _EXCEL_CLOUD_BATCH_CHAR_BUDGET


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


@cancellable_text_operation
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
    transient_round: int = 0,
    max_transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
    item_retries: int = 0,
) -> dict[str, str]:
    if not batch:
        return {}
    if should_stop and should_stop():
        stats.record_stopped(batch)
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
                    stats.record_stopped(batch)
                    return {text: text for text in batch}
                partial = engine.translate_batch(
                    batch, target_lang, system_prompt, source_lang=source_lang
                )
        _validate_batch_integrity(batch, partial)
        return partial
    except Exception as exc:  # noqa: BLE001 - fallback decides the safest degradation
        if isinstance(exc, ApiSchedulerAcquireCancelled):
            stats.record_stopped(batch)
            return {text: text for text in batch}
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if should_stop and should_stop():
            # 停止时在途请求被打断（连接被掐、读超时……）报出来的错不是接口坏了：
            # 记成「停止时尚未翻译」，不拆批重试、不计入未翻译条数，免得任务详情
            # 让用户去查一个没坏的 API 配置。
            logger.debug(f"停止期间批次请求中断：{exc!r}")
            stats.record_stopped(batch)
            return {text: text for text in batch}
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
                    stats.record_stopped(batch)
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
                    stats.record_stopped(batch)
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
                    transient_round=transient_round,
                    max_transient_retries=max_transient_retries,
                    item_retries=item_retries,
                )
        rejected = credential_rejection(exc)
        if rejected is not None:
            # 401/403、Key 无效、没额度：后面每一批都会同样被拒，整个任务直接失败。
            raise rejected from exc
        if _should_retry_transient(exc, transient_round, max_transient_retries):
            if not _wait_before_transient_retry(
                exc, transient_round, max_transient_retries,
                stats=stats, error_callback=error_callback, should_stop=should_stop,
            ):
                stats.record_stopped(batch)
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
                concurrency_round=concurrency_round,
                transient_round=transient_round + 1,
                max_transient_retries=max_transient_retries,
                item_retries=item_retries,
            )
        if _is_permanent_request_error(exc) and not _split_after_slow_answer(exc, batch, split_depth):
            logger.error(f"Excel 翻译请求不再重发，已停止拆分批次：{exc}")
            if error_callback:
                error_callback(_unrecoverable_batch_message(exc, len(batch), transient_round))
            stats.record_untranslated(batch, str(exc))
            return {text: text for text in batch}

        can_split = (
            len(batch) > 1
            and split_depth < _MAX_BATCH_SPLIT_DEPTH
            and not (should_stop and should_stop())
            and not _is_rate_limit_error(exc)
        )
        if can_split:
            midpoint = max(1, len(batch) // 2)
            stats.record_retry()
            logger.warning(
                "Excel 批次翻译失败，已缩小批次重试"
                f"（{len(batch)} -> {midpoint}+{len(batch) - midpoint}）：{exc}"
            )
            message = f"一批翻译出错，已拆分为更小的批次重试：{_batch_failure_reason(exc)}"
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
                max_transient_retries=_split_retries(exc, max_transient_retries),
                item_retries=item_retries,
            )
            right = _translate_batch_with_fallback(
                batch[midpoint:], engine=engine, target_lang=target_lang,
                system_prompt=system_prompt, source_lang=source_lang,
                api_scheduler=api_scheduler, request_category=request_category,
                should_stop=should_stop, error_callback=error_callback, stats=stats,
                split_depth=split_depth + 1,
                max_transient_retries=_split_retries(exc, max_transient_retries),
                item_retries=item_retries,
            )
            return {**left, **right}
        if item_retries > 0 and not _is_rate_limit_error(exc) and not (should_stop and should_stop()):
            def _send_one(text: str) -> str:
                partial = engine.translate_batch(
                    [text], target_lang, system_prompt, source_lang=source_lang
                )
                _validate_batch_integrity([text], partial)
                return partial[text]

            recovered, failed, stopped, exc = _retry_items_individually(
                batch,
                exc,
                attempts=item_retries,
                send=_send_one,
                system_prompt=system_prompt,
                api_scheduler=api_scheduler,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            results: dict[str, str] = dict(recovered)
            if stopped:
                stats.record_stopped(stopped)
                results.update({text: text for text in stopped})
            if not failed:
                return results
            batch = failed
        else:
            results = {}
        stats.record_untranslated(batch, str(exc))
        head = str(batch[0] if batch else "")[:40]
        logger.error(
            f"Excel 有 {len(batch)} 条未能翻译，已原样保留并计入未翻译条数："
            f"{head}... | {exc}"
        )
        err_msg = (
            f"有 {len(batch)} 条内容未能翻译，已保留原文（如「{head}…」）："
            f"{_batch_failure_reason(exc)}"
        )
        if error_callback:
            error_callback(err_msg)
        return {**results, **{text: text for text in batch}}


def _retry_items_individually(
    batch: list[str],
    first_error: BaseException,
    *,
    attempts: int,
    send,
    system_prompt: str,
    api_scheduler: WeightedApiScheduler | None,
    request_category: str,
    should_stop,
    error_callback,
    stats: TranslationBatchRunStats,
):
    """拆到最小仍没译出来的条目逐条单独重发（翻译参数 → 单段重试次数）。

    走到这里的是内容出错（返回条数不对、缺译文、格式坏了）：限流和接口拒绝在上面
    已经各有去处。一条单独发，模型更容易答对。重发中又遇到限流或接口拒绝，后面的
    条目不再重发，免得把一个已经在拒绝的端点再撞几十次。

    返回 (译好的, 仍失败的, 停止时没发的, 最后一个错误)。
    """
    recovered: dict = {}
    failed: list[str] = []
    stopped: list[str] = []
    last_error = first_error
    if error_callback:
        error_callback(
            f"有 {len(batch)} 条内容拆分后仍未译出，正在逐条重试（每条最多 {attempts} 次）："
            f"{_batch_failure_reason(first_error)}"
        )
    abort = False
    for index, text in enumerate(batch):
        if abort:
            failed.extend(batch[index:])
            break
        done = False
        for attempt in range(attempts):
            if should_stop and should_stop():
                break
            if attempt:
                _backoff_sleep(
                    attempt - 1,
                    base=_SPLIT_RETRY_BASE_DELAY,
                    cap=_SPLIT_RETRY_MAX_DELAY,
                    should_stop=should_stop,
                )
                if should_stop and should_stop():
                    break
            stats.record_item_retry()
            try:
                if api_scheduler is None:
                    recovered[text] = send(text)
                else:
                    with api_scheduler.slot(
                        _estimate_api_request_weight([text], system_prompt),
                        category=request_category,
                        should_stop=should_stop,
                    ):
                        if should_stop and should_stop():
                            break
                        recovered[text] = send(text)
                done = True
                break
            except ApiSchedulerAcquireCancelled:
                break
            except ApiKeyTemporarilyUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - the item keeps its source text
                rejected = credential_rejection(exc)
                if rejected is not None:
                    raise rejected from exc
                last_error = exc
                logger.debug(f"逐条重试第 {attempt + 1}/{attempts} 次失败：{exc!r}")
                if _is_rate_limit_error(exc) or (
                    _is_permanent_request_error(exc) and not _is_transient_request_error(exc)
                ):
                    abort = True
                    break
        if done:
            continue
        if should_stop and should_stop():
            stopped.append(text)
        else:
            failed.append(text)
    return recovered, failed, stopped, last_error


_REASON_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_REASON_URL_RE = re.compile(r"\b(?:https?|wss?)://", re.IGNORECASE)
_REASON_JSON_RE = re.compile(r"\{\s*[\"']")


def user_facing_batch_reason(value: object, *, fallback: str = "模型服务返回了错误。") -> str:
    """One plain sentence for the run log; the raw error only goes to the debug log.

    这些句子会经 error_callback 进用户的运行日志。原始异常里常有英文报文、请求
    地址和 JSON 错误体，对用户没用：humanize_error 认得出的换成中文原因，本来就是
    中文的照常显示，其余一律用兜底句（与 task_runner.user_facing_reason 同一标准，
    这里另写一份是因为 task_runner 反向依赖本模块）。
    """
    logger.debug(f"批次请求失败原始错误：{value!r}")
    sentence = humanize_error(value)
    if (
        not _REASON_CJK_RE.search(sentence)
        or _REASON_URL_RE.search(sentence)
        or _REASON_JSON_RE.search(sentence)
    ):
        return fallback
    return sentence


def _batch_failure_reason(exc: BaseException) -> str:
    return user_facing_batch_reason(exc)


@cancellable_text_operation
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
    transient_round: int = 0,
    max_transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
    item_retries: int = 0,
) -> dict[str, TranslationLanguageResult]:
    if not batch:
        return {}
    if should_stop and should_stop():
        stats.record_stopped(batch)
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
                    stats.record_stopped(batch)
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
            stats.record_stopped(batch)
            return {
                text: TranslationLanguageResult(text, text, source_lang="und", target_lang=target_lang)
                for text in batch
            }
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if should_stop and should_stop():
            # 同 _translate_batch_with_fallback：停止打断的请求不算接口出错。
            logger.debug(f"停止期间批次请求中断：{exc!r}")
            stats.record_stopped(batch)
            return _untranslated_language_results(batch, target_lang)
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
                    stats.record_stopped(batch)
                    return _untranslated_language_results(batch, target_lang)
                _backoff_sleep(
                    concurrency_round,
                    base=_CONCURRENCY_RETRY_BASE_DELAY,
                    cap=_CONCURRENCY_RETRY_MAX_DELAY,
                    should_stop=should_stop,
                )
                if should_stop and should_stop():
                    stats.record_stopped(batch)
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
                    transient_round=transient_round,
                    max_transient_retries=max_transient_retries,
                    item_retries=item_retries,
                )
        # Splitting a batch cannot fix a rejected key or a missing model, and
        # the sources variant used to keep bisecting anyway — mirror the plain
        # path so a permanent failure costs one request, not fifteen.
        rejected = credential_rejection(exc)
        if rejected is not None:
            # 401/403、Key 无效、没额度：后面每一批都会同样被拒，整个任务直接失败。
            raise rejected from exc
        if _should_retry_transient(exc, transient_round, max_transient_retries):
            if not _wait_before_transient_retry(
                exc, transient_round, max_transient_retries,
                stats=stats, error_callback=error_callback, should_stop=should_stop,
            ):
                stats.record_stopped(batch)
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
                concurrency_round=concurrency_round,
                transient_round=transient_round + 1,
                max_transient_retries=max_transient_retries,
                item_retries=item_retries,
            )
        if _is_permanent_request_error(exc) and not _split_after_slow_answer(exc, batch, split_depth):
            logger.error(f"Excel 语言回报请求不再重发，已停止拆分批次：{exc}")
            if error_callback:
                error_callback(_unrecoverable_batch_message(exc, len(batch), transient_round))
            stats.record_untranslated(batch, str(exc))
            return _untranslated_language_results(batch, target_lang)

        can_split = (
            len(batch) > 1
            and split_depth < _MAX_BATCH_SPLIT_DEPTH
            and not (should_stop and should_stop())
            and not _is_rate_limit_error(exc)
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
                max_transient_retries=_split_retries(exc, max_transient_retries),
                item_retries=item_retries,
            )
            right = _translate_batch_with_sources_fallback(
                batch[midpoint:], engine=engine, target_lang=target_lang,
                system_prompt=system_prompt, api_scheduler=api_scheduler,
                request_category=request_category, should_stop=should_stop,
                error_callback=error_callback, stats=stats,
                split_depth=split_depth + 1,
                max_transient_retries=_split_retries(exc, max_transient_retries),
                item_retries=item_retries,
            )
            return {**left, **right}
        recovered_items: dict[str, TranslationLanguageResult] = {}
        if item_retries > 0 and not _is_rate_limit_error(exc) and not (should_stop and should_stop()):
            def _send_one(text: str) -> TranslationLanguageResult:
                items = engine.translate_batch_with_sources(
                    [text], target_lang, system_prompt, source_lang="auto"
                )
                if len(items) != 1:
                    raise ValueError(f"返回条数不足：输入 1，返回 {len(items)}")
                return items[0]

            recovered, failed, stopped, exc = _retry_items_individually(
                batch,
                exc,
                attempts=item_retries,
                send=_send_one,
                system_prompt=system_prompt,
                api_scheduler=api_scheduler,
                request_category=request_category,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            recovered_items.update(recovered)
            if stopped:
                stats.record_stopped(stopped)
                recovered_items.update(_untranslated_language_results(stopped, target_lang))
            if not failed:
                return recovered_items
            batch = failed
        stats.record_untranslated(batch, str(exc))
        logger.error(f"Excel 有 {len(batch)} 条未能翻译，已原样保留并计入未翻译条数：{exc}")
        err_msg = (
            f"有 {len(batch)} 条内容未能翻译，已保留原文："
            f"{_batch_failure_reason(exc)}"
        )
        if error_callback:
            error_callback(err_msg)
        return {**recovered_items, **_untranslated_language_results(batch, target_lang)}


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


def _is_transient_request_error(exc: BaseException) -> bool:
    """A failure that the same request is likely to get past a few seconds later.

    5xx, 408 and timeouts / answers cut off mid-stream.  429 is not here: the
    adaptive concurrency path above owns rate limits.  Any other 4xx means the
    request itself was refused, which resending cannot change.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 0
    if status:
        return status >= 500 or status == 408
    if getattr(exc, "kind", "") in _TRANSIENT_TRANSPORT_KINDS:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def _split_after_slow_answer(exc: BaseException, batch: Sequence[str], split_depth: int) -> bool:
    """A batch that still times out after its resends gets halved once.

    Half the items is half the answer to wait for.  Resending the same batch a
    third time would most likely time out again, and the batch was then kept
    as source text (feedback FB-20261013-UVW95F).
    """
    return (
        split_depth == 0
        and len(batch) > 1
        and getattr(exc, "kind", "") in _SLOW_ANSWER_KINDS
    )


def _split_retries(exc: BaseException, limit: int) -> int:
    # The halves of a slow batch do not resend on their own: if the server
    # does not answer at all, each batch costs two more requests, not six.
    return 0 if getattr(exc, "kind", "") in _SLOW_ANSWER_KINDS else limit


def _should_retry_transient(exc: BaseException, transient_round: int, limit: int) -> bool:
    # Only errors the layer below did not already resend: the text transport
    # marks every failure no_replay, and the failover engine marks the one
    # that ended a whole connection chain.  Engines with their own retry loop
    # (Claude, Ollama) raise unmarked errors and keep their old path.
    return (
        transient_round < limit
        and bool(getattr(exc, "no_replay", False))
        and _is_transient_request_error(exc)
    )


def _wait_before_transient_retry(
    exc: BaseException,
    transient_round: int,
    limit: int,
    *,
    stats: TranslationBatchRunStats,
    error_callback,
    should_stop,
) -> bool:
    """Log the retry, back off, and return False if the task was stopped meanwhile."""
    stats.record_transient_retry()
    logger.warning(
        f"批次请求暂时失败，第 {transient_round + 1}/{limit} 次重发同一批：{exc!r}"
    )
    if error_callback:
        error_callback(
            f"一批翻译请求暂时失败，稍后自动重试（第 {transient_round + 1}/{limit} 次）："
            f"{_batch_failure_reason(exc)}"
        )
    _backoff_sleep(
        transient_round,
        base=_TRANSIENT_RETRY_BASE_DELAY,
        cap=_TRANSIENT_RETRY_MAX_DELAY,
        should_stop=should_stop,
    )
    return not (should_stop and should_stop())


def _unrecoverable_batch_message(exc: BaseException, count: int, transient_round: int) -> str:
    reason = _batch_failure_reason(exc)
    if transient_round:
        return f"一批翻译请求重试 {transient_round} 次后仍失败，该批 {count} 条内容已保留原文：{reason}"
    return f"一批翻译请求未成功，该批 {count} 条内容已保留原文：{reason}"


def _is_rate_limit_error(exc: BaseException) -> bool:
    """限流（429 / 并发上限）拆小批次救不了，只会把请求数放大好几倍（审计 高-7）。

    这类错误已经按退避节奏把同一批重试过了；重试用完仍被限流，这一批就记为未翻译，
    不再拆成 2、4、8 个请求去撞同一个正在限流的端点。
    """
    if is_api_concurrency_limit_error(exc):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return str(status_code or "") == "429"


def _is_permanent_request_error(exc: BaseException) -> bool:
    """Return True when shrinking a batch cannot change the upstream outcome."""
    if getattr(exc, "no_replay", False):
        return True
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
        stats.untranslated_sources | stats.stopped_sources
        if stats is not None
        else frozenset()
    )
    for src in list(results.keys()):
        if src in already_untranslated:
            # API 失败回退的条目已按「未翻译」上报过。这里译文必然等于原文，
            # 再计一次「质量校验回退」会把服务故障误报成译文质量问题，
            # 同一格子在报告里出现两条互相矛盾的 needs_action。
            # 停止时按原文占位交回的条目同理：那是还没翻，不是译坏了。
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
