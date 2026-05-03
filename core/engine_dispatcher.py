"""
翻译引擎调度层（统一入口）。
根据用户配置实例化对应引擎，对外暴露统一的 translate_batch() 接口。
云端引擎使用 ThreadPoolExecutor 并发发送批次，Ollama 保持原有 asyncio 逻辑不变。
"""
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from loguru import logger

from config import (
    DOMAIN_PRESETS,
    CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX,
    CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX,
)
from core.language_registry import append_prompt_block, build_target_lang_note_block
from core.translation_protocol import should_apply_quality_filter
from engines.base_engine import TranslationEngine
from settings import AppSettings, get_key
from core.translation_filter import is_translation_redundant  # 质量闭环拦截


def build_engine(settings: AppSettings) -> TranslationEngine:
    """根据当前配置构建并返回翻译引擎实例。"""
    s = settings.engine

    if s.mode == "local":
        from engines.ollama_engine import OllamaEngine
        return OllamaEngine(
            model=s.ollama_model,
            concurrency=s.ollama_concurrency,
        )

    # 云端模式
    provider = s.cloud_provider
    api_key  = get_key(provider)

    if provider == "hermes":
        from engines.hermes_engine import HermesEngine
        return HermesEngine()

    if provider == "claude":
        from engines.claude_engine import ClaudeEngine
        return ClaudeEngine(
            api_key=api_key,
            model=s.cloud_model,
            base_url=s.cloud_base_url,
        )

    if provider in ("openai", "siliconflow", "custom_openai", "lanyi"):
        from engines.openai_engine import OpenAIEngine
        return OpenAIEngine(
            api_key=api_key,
            model=s.cloud_model,
            base_url=s.cloud_base_url,
        )

    if provider == "zhipu":
        from engines.zhipu_engine import ZhipuEngine
        return ZhipuEngine(api_key=api_key, model=s.cloud_model)

    if provider == "dashscope":
        from engines.dashscope_engine import DashscopeEngine
        return DashscopeEngine(api_key=api_key, model=s.cloud_model)

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
    is_local = engine.engine_name.startswith("ollama/")
    if is_local:
        chunk = max(CHUNK_LOCAL_MIN, min(CHUNK_LOCAL_MAX, batch_size))
    else:
        chunk = max(CHUNK_CLOUD_MIN, min(CHUNK_CLOUD_MAX, batch_size))

    batches: list[list[str]] = [
        texts[i: i + chunk] for i in range(0, len(texts), chunk)
    ]
    total = len(texts)

    # Ollama 走原有串行路径，不引入线程池（内部已有 asyncio 并发）
    if is_local:
        results: dict[str, str] = {}
        done = 0
        for batch in batches:
            if should_stop and should_stop():
                logger.info("翻译任务收到停止信号，停止提交后续本地批次。")
                break
            try:
                partial = engine.translate_batch(
                    batch,
                    target_lang,
                    system_prompt,
                    source_lang=source_lang,
                )
                results.update(partial)
            except Exception as e:
                err_msg = f"批次翻译失败（{len(results)+1}~{len(results)+len(batch)} 条）：{e}"
                logger.error(err_msg)
                if error_callback:
                    error_callback(err_msg)
                results.update({t: t for t in batch})
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

    def _submit_batch(batch: list[str]) -> tuple[list[str], dict[str, str]]:
        """单批次执行，返回 (原始词条列表, 翻译结果)，异常时降级为原文。"""
        try:
            return batch, engine.translate_batch(
                batch,
                target_lang,
                system_prompt,
                source_lang=source_lang,
            )
        except Exception as e:
            err_msg = f"批次翻译失败（{batch[0][:20]}… 等 {len(batch)} 条）：{e}"
            logger.error(err_msg)
            if error_callback:
                error_callback(err_msg)
            return batch, {t: t for t in batch}

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
                    # 用实际返回译文条数累加，真实反映已完成量，防止虚高超出 total
                    done_count += len(partial)
                    if progress_callback:
                        progress_callback(min(done_count, total), total)
                if not (should_stop and should_stop()):
                    _submit_next()

    _apply_quality_filter(results, target_lang, source_lang=source_lang)
    return results


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
