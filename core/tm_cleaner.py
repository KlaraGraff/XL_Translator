"""
TM 深度清洗模块。
将 TM 库中的词条批量送入高质量 LLM（默认 Claude Opus），
对专业术语进行校正。清洗策略固定为“先生成建议、后由用户确认写入”，
不存在后台直接覆写模式。

并发策略与翻译流程保持一致：
  - 云端引擎：ThreadPoolExecutor（可配置 workers）并发提交所有批次
  - 本地引擎（Ollama）：asyncio.gather 并发（内部已实现）
"""
import asyncio
import json
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from loguru import logger

from core import tm_manager
from core.language_registry import (
    append_prompt_block,
    build_target_lang_note_block_from_lang_pair,
    get_target_lang_display_from_lang_pair,
)
from core.engine_dispatcher import is_local_engine_name
from core.tm_text import normalize_tm_text_for_compare, normalize_tm_text_for_storage
from engines.base_engine import TranslationEngine, strip_markdown_json


@dataclass
class CleanSuggestion:
    entry_id:    int
    source_text: str
    old_target:  str
    new_target:  str
    accepted:    bool = True   # UI 中用户可逐条切换
    lang_pair:   str = ""
    expected_version: str = ""


class TmCleaningBatchError(RuntimeError):
    """One or more TM cleaning batches failed and must not look successful."""

    def __init__(self, failed_batches: int, total_batches: int, first_error: str):
        self.failed_batches = max(1, int(failed_batches))
        self.total_batches = max(self.failed_batches, int(total_batches))
        self.first_error = str(first_error or "未知错误")
        super().__init__(
            f"{self.failed_batches}/{self.total_batches} 个清洗批次失败；"
            f"首个错误：{self.first_error}"
        )


DEFAULT_CLEAN_SYSTEM_PROMPT_TEMPLATE = (
    "你是一名土木工程与建筑工程术语词库清洗助手。"
    "输入为 JSON 数组，每项包含 id、source（原文）和 current（当前译文）。\n"
    "任务：基于土木工程、建筑工程、机电安装等常见工程语境，对 current 做保守清洗与必要校正；核心目标是提升工程术语准确性、规范性和可复用性。\n"
    "处理原则：\n"
    "1) 准确性优先。若 current 已符合工程语境、术语准确且可直接复用，则保持不变。\n"
    "2) 保留并正确传达等级、规范编号、尺寸、单位、型号、代号、楼层/分区/构件标识，以及括号中的有效工程信息，例如 (A1.5)、(A2.0)、50×20×0.6mm、DN50、C30。\n"
    "3) 只做最小必要修正：清理首尾噪声、明显多余的引号、冒号、markdown 包裹、空格与格式问题；不要删减有效括号信息。\n"
    "4) 不臆测未给出的项目背景，不补充解释，不扩写，不输出多个版本；若无法在工程语境下确定更优译法，则保持 current 不变。\n"
    "5) 不改动原文事实，不擅自替换专业等级、材料、部位、构件名称或技术参数。\n"
    "6) 若 source 本身看起来已经是“中文 + {target_lang_name}”的双语单元格内容，或 source 混入了现成译文而不是干净原文，则该项直接返回空字符串 suggested。\n"
    "7) 输出的 suggested 必须是单一最终译文，不要包含备注、理由、说明、操作指令或“无需修改”等评语。\n"
    "严格输出 JSON 数组，不要附加任何解释：\n"
    "[{{\"id\": <id>, \"suggested\": \"<清洗后译文>\"}}]"
)
DEFAULT_CLEAN_SYSTEM_PROMPT = DEFAULT_CLEAN_SYSTEM_PROMPT_TEMPLATE.format(
    target_lang_name="目标语言"
)

# A full user override may replace the built-in terminology/judgement rules,
# but it cannot remove the machine-readable response contract or the
# suggestion-only safety boundary.  This block is deliberately appended after
# user text so an override cannot supersede it by ordering alone.
_CLEAN_IMMUTABLE_PROTOCOL = (
    "[程序固定约束]\n"
    "严格输出 JSON 数组，不要附加任何解释。每一项必须包含与输入对应的 id 和 suggested 字段；"
    "suggested 只能是单一清洗建议，禁止输出备注、理由或多版本。\n"
    "这是建议确认流程：只生成建议，不得直接写入、覆盖或确认 TM 词条。"
    "必须保护原文事实、术语等级、编号、规格参数、单位、括号和其他工程标识；无法确定时返回空字符串。"
)


def get_clean_system_prompt(
    custom_prompt: str = "",
    lang_pair: str = "",
    custom_target_langs=None,
) -> str:
    """Return the user override when provided, otherwise the built-in cleaner prompt."""
    prompt = str(custom_prompt or "").strip()
    if prompt:
        return append_prompt_block(
            prompt,
            build_target_lang_note_block_from_lang_pair(lang_pair, custom_target_langs),
        )
    return build_clean_system_prompt(
        lang_pair=lang_pair,
        custom_target_langs=custom_target_langs,
    )


def get_clean_target_lang_name(lang_pair: str = "", custom_target_langs=None) -> str:
    return get_target_lang_display_from_lang_pair(lang_pair, custom_target_langs)


def get_clean_builtin_system_prompt(lang_pair: str = "", custom_target_langs=None) -> str:
    return DEFAULT_CLEAN_SYSTEM_PROMPT_TEMPLATE.format(
        target_lang_name=get_clean_target_lang_name(lang_pair, custom_target_langs)
    )


def build_clean_system_prompt(
    lang_pair: str = "",
    extra_prompt: str = "",
    full_override_prompt: str = "",
    custom_target_langs=None,
) -> str:
    target_lang_note_block = build_target_lang_note_block_from_lang_pair(
        lang_pair,
        custom_target_langs,
    )
    full_override = str(full_override_prompt or "").strip()
    if full_override:
        prompt = append_prompt_block(full_override, target_lang_note_block)
        return append_prompt_block(prompt, _CLEAN_IMMUTABLE_PROTOCOL)

    builtin_prompt = get_clean_builtin_system_prompt(
        lang_pair,
        custom_target_langs,
    )
    builtin_prompt = append_prompt_block(builtin_prompt, target_lang_note_block)
    extra = str(extra_prompt or "").strip()
    if not extra:
        return builtin_prompt

    return (
        f"{builtin_prompt}\n\n"
        "[当前语言补充要求]\n"
        "以下内容用于细化当前语言的清洗偏好；如与上述核心规则冲突，以上述核心规则为准。\n"
        f"{extra}"
    )


def _emit_clean_progress(
    progress_callback,
    *,
    stage: str,
    total_entries: int,
    total_batches: int,
    completed_entries: int = 0,
    completed_batches: int = 0,
    submitted_batches: int | None = None,
) -> None:
    """Emit a structured cleaning progress event for the UI."""
    if not progress_callback:
        return

    total_entries = max(0, int(total_entries))
    total_batches = max(0, int(total_batches))
    completed_entries = min(max(0, int(completed_entries)), total_entries)
    completed_batches = min(max(0, int(completed_batches)), total_batches)
    submitted_batches = completed_batches if submitted_batches is None else int(submitted_batches)
    submitted_batches = min(max(0, submitted_batches), total_batches)

    progress_callback(
        {
            "stage": stage,
            "done": completed_entries,
            "total": total_entries,
            "completed_entries": completed_entries,
            "total_entries": total_entries,
            "completed_batches": completed_batches,
            "submitted_batches": submitted_batches,
            "total_batches": total_batches,
        }
    )


_HIGH_CONFIDENCE_OUTER_WRAPPERS = (
    ("**", "**"),
    ("__", "__"),
    ("*", "*"),
    ("_", "_"),
    ("`", "`"),
    ('"', '"'),
    ("“", "”"),
    ("‘", "’"),
    ("«", "»"),
    ("‹", "›"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
)
_OUTER_COLON_WRAPPER_RE = re.compile(r"^[:：]+\s*(.+?)\s*[:：]+$")
_MULTISPACE_RE = re.compile(r"\s+")
_META_CHINESE_RE = re.compile(
    r"^(无需修改|无须修改|保持不变|保持原译(?:文)?|沿用当前译文|使用当前译文|无需调整)(?:[，。；:：].*)?$"
)
_BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


def _normalize_clean_target(text: str) -> str:
    """保守清洗：去除高置信外层噪声，保留可能承载工程信息的括号类内容。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    while True:
        updated = _strip_outer_noise_once(cleaned)
        if updated == cleaned:
            break
        cleaned = updated
        if not cleaned:
            break
    return _MULTISPACE_RE.sub(" ", cleaned).strip()


def _strip_outer_noise_once(text: str) -> str:
    """仅剥离一层高置信外层噪声，不触碰括号/方括号/大括号。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    for prefix, suffix in _HIGH_CONFIDENCE_OUTER_WRAPPERS:
        min_len = len(prefix) + len(suffix)
        if len(cleaned) <= min_len:
            continue
        if cleaned.startswith(prefix) and cleaned.endswith(suffix):
            return cleaned[len(prefix) : len(cleaned) - len(suffix)].strip()

    match = _OUTER_COLON_WRAPPER_RE.match(cleaned)
    if match:
        return match.group(1).strip()

    return cleaned


def _looks_like_meta_output(text: str) -> bool:
    """识别“无需修改”“按当前译文执行”一类评语/指令式输出。"""
    normalized = _MULTISPACE_RE.sub(" ", str(text or "")).strip()
    if not normalized:
        return False

    lowered = normalized.lower()
    if "proceed according" in lowered or "proceed accordingly" in lowered:
        return True
    if "keep as is" in lowered or "leave unchanged" in lowered:
        return True
    if "use current translation" in lowered or "keep current translation" in lowered:
        return True
    if ("no change" in lowered or "no changes" in lowered) and any(
        hint in lowered for hint in ("proceed", "translation", "keep", "leave", "use", "needed", "required")
    ):
        return True

    return bool(_META_CHINESE_RE.fullmatch(normalized))


def _has_unbalanced_brackets(text: str) -> bool:
    """简单检查首尾保护括号是否失衡；失衡时宁可丢弃建议，避免误伤工程标识。"""
    return any(text.count(left) != text.count(right) for left, right in _BRACKET_PAIRS)


def _sanitize_clean_suggestion(suggested_raw: str, current_target: str) -> str:
    """标准化并过滤不可信的清洗结果。"""
    suggested = _normalize_clean_target(suggested_raw)
    if not suggested:
        return ""
    suggested = normalize_tm_text_for_storage(suggested)
    if not suggested:
        return ""
    if _looks_like_meta_output(suggested):
        return ""
    if not _has_unbalanced_brackets(current_target) and _has_unbalanced_brackets(suggested):
        return ""
    return suggested


def _build_clean_suggestion(item: dict, id_to_entry: dict[int, dict]) -> CleanSuggestion | None:
    """把模型返回的单项结果转成可写入的清洗建议。"""
    eid = item.get("id")
    entry = id_to_entry.get(eid)
    if not entry:
        return None

    suggested_value = item.get("suggested", "")
    suggested_raw = "" if suggested_value is None else str(suggested_value).strip()
    suggested = _sanitize_clean_suggestion(suggested_raw, entry["target_text"])
    current_target = str(entry["target_text"] or "")
    if not suggested:
        return None
    if normalize_tm_text_for_compare(suggested) == normalize_tm_text_for_compare(current_target):
        return None

    return CleanSuggestion(
        entry_id=eid,
        source_text=entry["source_text"],
        old_target=current_target,
        new_target=suggested,
        lang_pair=str(entry.get("lang_pair") or ""),
        expected_version=str(entry.get("version") or ""),
    )


def run_cleaning(
    lang_pair: str,
    engine: TranslationEngine,
    batch_size: int = 20,
    concurrency: int = 5,
    progress_callback=None,
    extra_prompt: str = "",
    full_override_prompt: str = "",
    custom_target_langs=None,
    cancel_event: threading.Event | None = None,
) -> list[CleanSuggestion]:
    """
    对指定语言对的所有 TM 词条发起清洗请求。
    返回建议修改列表（仅包含与当前译文不同的项）。

    并发策略与翻译流程保持一致：
      - 云端引擎：ThreadPoolExecutor（可配置 workers）并发提交所有批次
      - 本地引擎（Ollama）：asyncio.gather 并发（内部已实现）
    """
    all_entries = tm_manager.get_all_entries_for_cleaning(lang_pair)
    if not all_entries:
        return []

    clean_system_prompt = build_clean_system_prompt(
        lang_pair=lang_pair,
        extra_prompt=extra_prompt,
        full_override_prompt=full_override_prompt,
        custom_target_langs=custom_target_langs,
    )
    batch_size = max(1, int(batch_size))
    concurrency = max(1, int(concurrency))

    total = len(all_entries)
    batches: list[list[dict]] = [
        all_entries[i : i + batch_size] for i in range(0, total, batch_size)
    ]
    total_batches = len(batches)

    _emit_clean_progress(
        progress_callback,
        stage="prepared",
        total_entries=total,
        total_batches=total_batches,
    )

    # 判断引擎类型，选择对应的并发策略
    is_local = is_local_engine_name(engine.engine_name)

    if is_local:
        # 本地引擎：使用 asyncio 并发（与翻译流程一致）
        return _run_cleaning_async(
            batches,
            engine,
            progress_callback,
            clean_system_prompt,
            cancel_event=cancel_event,
        )
    else:
        # 云端引擎：使用 ThreadPoolExecutor 并发（与翻译流程一致）
        return _run_cleaning_threaded(
            batches,
            engine,
            progress_callback,
            concurrency=concurrency,
            system_prompt=clean_system_prompt,
            cancel_event=cancel_event,
        )


def _run_cleaning_async(
    batches: list[list[dict]],
    engine: TranslationEngine,
    progress_callback=None,
    system_prompt: str = "",
    cancel_event: threading.Event | None = None,
) -> list[CleanSuggestion]:
    """
    本地引擎（Ollama）的异步并发清洗。
    使用 asyncio.gather 并发处理所有批次。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _clean_async_impl(
                batches,
                engine,
                progress_callback,
                system_prompt,
                cancel_event=cancel_event,
            )
        )
    finally:
        loop.close()


async def _clean_async_impl(
    batches: list[list[dict]],
    engine: TranslationEngine,
    progress_callback=None,
    system_prompt: str = "",
    cancel_event: threading.Event | None = None,
) -> list[CleanSuggestion]:
    """异步清洗实现，使用 asyncio.gather 并发。"""
    total = sum(len(b) for b in batches)
    total_batches = len(batches)
    done_count = [0]  # 使用列表以便在闭包中修改
    done_batches = [0]
    batch_errors: list[str] = []
    lock = asyncio.Lock()

    # 获取引擎的并发数（Ollama 引擎有 _concurrency 属性）
    concurrency = getattr(engine, "_concurrency", 4)
    semaphore = asyncio.Semaphore(concurrency)

    _emit_clean_progress(
        progress_callback,
        stage="waiting_first_result",
        total_entries=total,
        total_batches=total_batches,
        submitted_batches=total_batches,
    )

    async def _process_batch(batch: list[dict]) -> list[CleanSuggestion]:
        """处理单个批次，返回建议列表。"""
        if cancel_event and cancel_event.is_set():
            return []
        async with semaphore:
            if cancel_event and cancel_event.is_set():
                return []
            try:
                partial = await _clean_batch_async(batch, engine, system_prompt)
            except Exception as e:
                logger.error(f"清洗批次失败（{len(batch)} 条）：{e}")
                batch_errors.append(str(e))
                partial = []

            async with lock:
                done_count[0] += len(batch)
                done_batches[0] += 1
                if progress_callback:
                    _emit_clean_progress(
                        progress_callback,
                        stage="processing",
                        total_entries=total,
                        total_batches=total_batches,
                        completed_entries=done_count[0],
                        completed_batches=done_batches[0],
                        submitted_batches=total_batches,
                    )

            return partial

    # 并发处理所有批次
    results = await asyncio.gather(*[_process_batch(batch) for batch in batches])

    # 汇总所有建议
    suggestions: list[CleanSuggestion] = []
    for partial in results:
        suggestions.extend(partial)

    if batch_errors:
        raise TmCleaningBatchError(
            len(batch_errors),
            total_batches,
            batch_errors[0],
        )

    _emit_clean_progress(
        progress_callback,
        stage="completed",
        total_entries=total,
        total_batches=total_batches,
        completed_entries=total,
        completed_batches=total_batches,
        submitted_batches=total_batches,
    )
    tm_manager.persist_cleaning_suggestions(
        [
            {
                "entry_id": item.entry_id,
                "source_text": item.source_text,
                "old_target": item.old_target,
                "new_target": item.new_target,
                "lang_pair": item.lang_pair,
                "version": item.expected_version,
            }
            for item in suggestions
        ]
    )
    logger.info(f"清洗完成，发现 {len(suggestions)} 处建议修改")
    return suggestions


async def _clean_batch_async(
    entries: list[dict],
    engine: TranslationEngine,
    system_prompt: str,
) -> list[CleanSuggestion]:
    """
    异步清洗单个批次。
    对于 Ollama 引擎，调用其异步方法；对于其他引擎，在线程池中运行同步方法。
    """
    # 检查引擎是否有异步方法
    if hasattr(engine, "_call_ollama"):
        # Ollama 引擎：直接调用异步方法
        return await _clean_batch_ollama_async(entries, engine, system_prompt)
    else:
        # 其他本地引擎：在线程池中运行同步方法
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _clean_batch_sync, entries, engine, system_prompt
        )


async def _clean_batch_ollama_async(
    entries: list[dict],
    engine: TranslationEngine,
    system_prompt: str,
) -> list[CleanSuggestion]:
    """Ollama 引擎的异步批次清洗。"""
    payload = [
        {"id": e["id"], "source": e["source_text"], "current": e["target_text"]}
        for e in entries
    ]
    user_msg = json.dumps(payload, ensure_ascii=False)

    try:
        raw = await engine._call_ollama(system_prompt, user_msg)
    except Exception as e:
        logger.error(f"Ollama 清洗调用失败：{e}")
        return []

    raw = strip_markdown_json(raw)

    try:
        results = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"清洗响应解析失败：{raw[:300]}")
        return []

    id_to_entry = {e["id"]: e for e in entries}
    suggestions: list[CleanSuggestion] = []

    for item in results:
        suggestion = _build_clean_suggestion(item, id_to_entry)
        if suggestion is not None:
            suggestions.append(suggestion)

    return suggestions


def _run_cleaning_threaded(
    batches: list[list[dict]],
    engine: TranslationEngine,
    progress_callback=None,
    concurrency: int = 5,
    system_prompt: str = "",
    cancel_event: threading.Event | None = None,
) -> list[CleanSuggestion]:
    """
    云端引擎的线程池并发清洗。
    使用 ThreadPoolExecutor（可配置 workers）并发处理所有批次。
    与翻译流程的并发策略保持一致。
    """
    total = sum(len(b) for b in batches)
    total_batches = len(batches)
    suggestions: list[CleanSuggestion] = []
    done_count = 0
    done_batches = 0
    batch_errors: list[str] = []
    lock = threading.Lock()
    max_workers = max(1, int(concurrency))

    def _submit_batch(batch: list[dict]) -> list[CleanSuggestion]:
        """单批次执行，异常由汇总层转换为清晰的任务失败。"""
        return _clean_batch_sync(batch, engine, system_prompt)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map: dict = {}
        batch_iter = iter(batches)

        def _submit_next() -> bool:
            if cancel_event and cancel_event.is_set():
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

        _emit_clean_progress(
            progress_callback,
            stage="waiting_first_result",
            total_entries=total,
            total_batches=total_batches,
            submitted_batches=len(future_map),
        )

        while future_map:
            done_futures, _ = wait(tuple(future_map.keys()), return_when=FIRST_COMPLETED)
            for future in done_futures:
                batch = future_map.pop(future)
                try:
                    partial = future.result()
                except Exception as exc:  # noqa: BLE001 - aggregate batch failures.
                    logger.error(f"清洗批次失败（{len(batch)} 条）：{exc}")
                    batch_errors.append(str(exc))
                    partial = []
                with lock:
                    suggestions.extend(partial)
                    done_count += len(batch)
                    done_batches += 1
                    _emit_clean_progress(
                        progress_callback,
                        stage="processing",
                        total_entries=total,
                        total_batches=total_batches,
                        completed_entries=min(done_count, total),
                        completed_batches=done_batches,
                        submitted_batches=done_batches + len(future_map),
                    )
                if not (cancel_event and cancel_event.is_set()):
                    _submit_next()

    if batch_errors:
        raise TmCleaningBatchError(
            len(batch_errors),
            total_batches,
            batch_errors[0],
        )

    _emit_clean_progress(
        progress_callback,
        stage="completed",
        total_entries=total,
        total_batches=total_batches,
        completed_entries=total,
        completed_batches=total_batches,
        submitted_batches=total_batches,
    )
    tm_manager.persist_cleaning_suggestions(
        [
            {
                "entry_id": item.entry_id,
                "source_text": item.source_text,
                "old_target": item.old_target,
                "new_target": item.new_target,
                "lang_pair": item.lang_pair,
                "version": item.expected_version,
            }
            for item in suggestions
        ]
    )
    logger.info(f"清洗完成，发现 {len(suggestions)} 处建议修改")
    return suggestions


def _clean_batch_sync(
    entries: list[dict],
    engine: TranslationEngine,
    system_prompt: str,
) -> list[CleanSuggestion]:
    """
    同步清洗单个批次（云端引擎或线程池中的本地引擎）。
    返回有差异的建议列表。
    """
    payload = [
        {"id": e["id"], "source": e["source_text"], "current": e["target_text"]}
        for e in entries
    ]
    user_msg = json.dumps(payload, ensure_ascii=False)

    # 使用 chat() 直接调用 API，不注入翻译格式指令
    raw = engine.chat(system=system_prompt, user=user_msg).strip()

    # 去除可能的 markdown 代码块（复用 base_engine 中的健壮实现）
    raw = strip_markdown_json(raw)

    try:
        results = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"清洗响应解析失败：{raw[:300]}")
        return []

    id_to_entry = {e["id"]: e for e in entries}
    suggestions: list[CleanSuggestion] = []

    for item in results:
        suggestion = _build_clean_suggestion(item, id_to_entry)
        if suggestion is not None:
            suggestions.append(suggestion)

    return suggestions


def apply_suggestions(
    suggestions: list[CleanSuggestion],
    auto_pin: bool = False,
    *,
    sync_reverse: bool = False,
) -> int:
    """
    将用户接受的建议写入 TM 数据库。
    若 auto_pin=True，写入后同时固定这些词条（防止重复清洗）。
    返回实际写入条数。
    """
    accepted_suggestions = [s for s in suggestions if s.accepted]
    accepted = [(s.entry_id, s.new_target) for s in accepted_suggestions]
    if not accepted:
        return 0
    expected_versions = {
        s.entry_id: s.expected_version
        for s in accepted_suggestions
        if s.expected_version
    }
    count = tm_manager.bulk_update(
        accepted,
        sync_reverse=sync_reverse,
        expected_versions=expected_versions or None,
        word_type=(
            tm_manager.CLEANING_LOCKED_WORD_TYPE
            if auto_pin
            else tm_manager.REVIEWED_AUTO_WORD_TYPE
        ),
    )
    if expected_versions:
        stale_ids = set(expected_versions)
        applied_ids = {
            s.entry_id
            for s in accepted_suggestions
            if not expected_versions.get(s.entry_id)
            or tm_manager.lookup_batch([s.source_text], s.lang_pair).get(s.source_text) == s.new_target
        }
        stale_ids -= applied_ids
        if stale_ids:
            logger.warning(f"清洗建议已过期并跳过 {len(stale_ids)} 条")
    if count:
        logger.info(
            f"清洗确认写入 {count} 条，状态升级为 "
            f"{'cleaning_locked' if auto_pin else 'reviewed_auto'}"
        )
    return count
