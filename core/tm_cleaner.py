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
from dataclasses import dataclass, replace

from loguru import logger

from core import tm_manager
from core.language_registry import (
    append_prompt_block,
    build_target_lang_note_block_from_lang_pair,
    get_target_lang_display_from_lang_pair,
)
from core.engine_dispatcher import is_local_engine_name
from core.api_concurrency_control import handle_api_concurrency_limit
from core.api_scheduler import API_REQUEST_CATEGORY_NORMAL
from core.residual_classifier import check_heading_consistency, is_section_heading_source
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
    # 建议表主键：确认写入后据此把这一条标成 applied/stale，
    # 否则旧建议永远挂在 pending，重复出现在待审列表里
    suggestion_id: int = 0


class TmCleaningBatchError(RuntimeError):
    """One or more TM cleaning batches failed and must not look successful."""

    def __init__(self, failed_batches: int, total_batches: int, first_error: str):
        self.failed_batches = max(1, int(failed_batches))
        self.total_batches = max(self.failed_batches, int(total_batches))
        self.first_error = str(first_error or "未知错误")
        # 批次失败前已算好的 0 API 惯例归一建议由 run_cleaning 挂在这里，
        # 调用方可以只丢模型建议、保住确定性建议，不必整批重来
        self.partial_suggestions: list = []
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


def _wraps_whole_text(cleaned: str, prefix: str, suffix: str) -> bool:
    """判断首尾这一对定界符是否真的包住了整段文本。

    只看「开头是它、结尾也是它」会把正文当噪声吃掉：`「甲」 与 「乙」`
    首尾恰好是一对引号，按字面剥一层就变成 `甲」 与 「乙`，正文被毁，
    而且这类建议默认勾选，会直接写进记忆库。
    """
    inner = cleaned[len(prefix) : len(cleaned) - len(suffix)]
    if prefix == suffix:
        # 对称定界符（引号、`**`、`` ` ``）无法靠配对深度判断：
        # 正文里再出现同一个定界符，就说明首尾两个不是一对，保守不剥。
        return prefix not in inner
    # 非对称定界符按配对深度扫描：深度在末尾之前归零，
    # 说明开头那个的配对不在结尾，是「A」与「B」这类并列，不能剥。
    depth = 1
    index = len(prefix)
    last = len(cleaned) - len(suffix)
    while index < last:
        if cleaned.startswith(prefix, index):
            depth += 1
            index += len(prefix)
            continue
        if cleaned.startswith(suffix, index):
            depth -= 1
            if depth == 0:
                return False
            index += len(suffix)
            continue
        index += 1
    return depth == 1


def _strip_outer_noise_once(text: str) -> str:
    """仅剥离一层高置信外层噪声，不触碰括号/方括号/大括号。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    for prefix, suffix in _HIGH_CONFIDENCE_OUTER_WRAPPERS:
        min_len = len(prefix) + len(suffix)
        if len(cleaned) <= min_len:
            continue
        if not (cleaned.startswith(prefix) and cleaned.endswith(suffix)):
            continue
        if not _wraps_whole_text(cleaned, prefix, suffix):
            continue
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


def build_convention_suggestions(
    lang_pair: str,
    entries: list[dict] | None = None,
) -> list[CleanSuggestion]:
    """
    惯例归一建议（确定性规则，0 API）：库内「第X节/章」条目按多数派写法
    聚类，离群者生成仅改前缀的建议，沿用「先建议、用户确认后写入」流程。

    只在多数派是「Section N」系写法时给建议——归一到序数词写法需要词形
    变化知识，不做确定性改写。
    """
    if entries is None:
        entries = tm_manager.get_all_entries_for_cleaning(lang_pair)
    heading_entries = [
        e for e in entries if is_section_heading_source(e.get("source_text", ""))
    ]
    if len(heading_entries) < 2:
        return []
    target_lang = lang_pair.split("-", 1)[1] if "-" in lang_pair else ""
    result = check_heading_consistency(
        (
            (e["source_text"], e["target_text"], e["id"])
            for e in heading_entries
        ),
        target_lang=target_lang,
    )
    id_to_entry = {e["id"]: e for e in heading_entries}
    suggestions: list[CleanSuggestion] = []
    for entry_id, fixed in result.fixes.items():
        entry = id_to_entry[entry_id]
        current_target = str(entry.get("target_text") or "")
        if normalize_tm_text_for_compare(fixed) == normalize_tm_text_for_compare(
            current_target
        ):
            continue
        suggestions.append(
            CleanSuggestion(
                entry_id=entry_id,
                source_text=entry["source_text"],
                old_target=current_target,
                new_target=fixed,
                lang_pair=str(entry.get("lang_pair") or ""),
                expected_version=str(entry.get("version") or ""),
            )
        )
    return suggestions


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
    api_scheduler=None,
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

    # 确定性惯例归一先行（0 API）；同一条目以确定性建议为准，模型建议让位
    convention_suggestions = build_convention_suggestions(lang_pair, entries=all_entries)
    if convention_suggestions:
        logger.info(
            f"TM 清洗：节标题惯例归一规则命中 {len(convention_suggestions)} 条（0 API）"
        )
        # 算完立刻入库：模型批次失败也不能让这批建议只活在日志里——
        # 建议列表读的是 tm_cleaning_suggestions 表，不入库用户永远看不到。
        # 建议是确定性的，重跑会原样再算一遍，按 (entry_id, 归一后译文)
        # 与库里 pending 的去重，避免重复条目堆积。
        existing_pending = {
            (
                int(row.get("entry_id") or 0),
                normalize_tm_text_for_compare(str(row.get("new_target") or "")),
            )
            for row in tm_manager.list_cleaning_suggestions(lang_pair, status="pending")
        }
        fresh = [
            s
            for s in convention_suggestions
            if (s.entry_id, normalize_tm_text_for_compare(s.new_target))
            not in existing_pending
        ]
        if fresh:
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
                    for item in fresh
                ]
            )
    convention_covered = {s.entry_id for s in convention_suggestions}

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

    try:
        if is_local:
            # 本地引擎：使用 asyncio 并发（与翻译流程一致）
            model_suggestions = _run_cleaning_async(
                batches,
                engine,
                progress_callback,
                clean_system_prompt,
                cancel_event=cancel_event,
            )
        else:
            # 云端引擎：使用 ThreadPoolExecutor 并发（与翻译流程一致）
            model_suggestions = _run_cleaning_threaded(
                batches,
                engine,
                progress_callback,
                concurrency=concurrency,
                system_prompt=clean_system_prompt,
                cancel_event=cancel_event,
                api_scheduler=api_scheduler,
            )
    except TmCleaningBatchError as batch_error:
        # 模型批次失败不该连累 0 API 的确定性建议：挂在异常上带出去
        batch_error.partial_suggestions = list(convention_suggestions)
        raise
    return convention_suggestions + [
        s for s in model_suggestions if s.entry_id not in convention_covered
    ]


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

    # Let engine failures propagate: _process_batch aggregates them into
    # batch_errors and the task ends in TmCleaningBatchError. Swallowing them
    # here reported a half-failed run as a clean completion, exactly like the
    # sync path would not.
    raw = await engine._call_ollama(system_prompt, user_msg)

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
    api_scheduler=None,
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
        while True:
            generation = None
            try:
                if api_scheduler is None:
                    return _clean_batch_sync(batch, engine, system_prompt)
                with api_scheduler.slot(
                    1,
                    category=API_REQUEST_CATEGORY_NORMAL,
                    should_stop=(
                        (lambda: bool(cancel_event and cancel_event.is_set()))
                        if cancel_event is not None
                        else None
                    ),
                ) as lease:
                    generation = lease.generation
                    return _clean_batch_sync(batch, engine, system_prompt)
            except Exception as exc:
                if api_scheduler is None:
                    raise
                decision = handle_api_concurrency_limit(
                    exc,
                    scheduler=api_scheduler,
                    request_generation=generation,
                    context_label="TM 清洗",
                )
                if decision is None or not decision.should_retry:
                    raise

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


def _resolve_pending_suggestions(
    suggestions: list[CleanSuggestion],
) -> list[CleanSuggestion]:
    """给建议补上建议表主键与乐观并发版本。

    前端可能只回传内容（旧版本客户端就是如此）。这时按
    (entry_id, 新译文) 去 pending 建议里找回对应行：没有 expected_version
    就等于关掉乐观并发检查，别人刚改过的译文会被这次确认无声覆盖。
    """
    lang_pairs = {str(item.lang_pair or "") for item in suggestions}
    pending: list[dict] = []
    for lang_pair in lang_pairs:
        pending.extend(tm_manager.list_cleaning_suggestions(lang_pair or None, status="pending"))
    by_content: dict[tuple[int, str], dict] = {}
    by_id: dict[int, dict] = {}
    for row in pending:
        key = (
            int(row.get("entry_id") or 0),
            normalize_tm_text_for_compare(str(row.get("new_target") or "")),
        )
        by_content.setdefault(key, row)
        by_id[int(row.get("id") or 0)] = row

    resolved: list[CleanSuggestion] = []
    for item in suggestions:
        row = by_id.get(int(item.suggestion_id or 0))
        if row is None:
            row = by_content.get(
                (item.entry_id, normalize_tm_text_for_compare(item.new_target))
            )
        if row is None:
            resolved.append(item)
            continue
        resolved.append(
            replace(
                item,
                suggestion_id=int(row.get("id") or 0),
                expected_version=item.expected_version
                or str(row.get("expected_version") or ""),
            )
        )
    return resolved


def _settle_suggestion_rows(
    paired: list[tuple[CleanSuggestion, str]],
) -> None:
    """按逐行写入结果把建议表里的对应行标成 applied / stale。

    必须按提交行序配对，不能按 entry_id 归并：同一词条挂两条建议时，先到的
    写入（updated）、后到的被乐观并发拦下（stale），entry_id 会同时出现在两个
    桶里——按 id 反查会把两条一起标错。invalid（译文为空、没落库）的行保持
    pending：什么都没发生，用户下次打开还应该原样看到它。
    """
    applied_ids = [
        s.suggestion_id
        for s, bucket in paired
        if s.suggestion_id and bucket in ("updated", "unchanged")
    ]
    stale_ids = [
        s.suggestion_id
        for s, bucket in paired
        if s.suggestion_id and bucket in ("stale", "pinned", "missing")
    ]
    if applied_ids:
        tm_manager.mark_cleaning_suggestions(applied_ids, "applied")
    if stale_ids:
        tm_manager.mark_cleaning_suggestions(stale_ids, "stale")


def apply_suggestions(
    suggestions: list[CleanSuggestion],
    auto_pin: bool = False,
    *,
    sync_reverse: bool = False,
) -> int:
    """将用户接受的建议写入 TM 数据库，返回实际写入条数。

    保留为薄包装：老调用方只关心写入条数，明细走
    :func:`apply_suggestions_detailed`。
    """
    return apply_suggestions_detailed(
        suggestions, auto_pin=auto_pin, sync_reverse=sync_reverse
    )["applied"]


def _build_suggestion_outcomes(
    paired: list[tuple[CleanSuggestion, str]],
) -> list[dict[str, object]]:
    """把逐行去向（updated/unchanged/pinned/stale/missing/invalid）摊回逐条建议。

    界面拿这份明细在弹窗里原地标出每一行到底进了哪一桶，不用再靠 applied/
    unchanged/skipped 三个总数猜某一条具体是被固定拦下还是已经过期。
    配对必须按提交行序（bulk_update_detailed 的 "rows"），不能按 entry_id
    归并——同一词条挂两条建议时 entry_id 会同时出现在多个桶里，反查会把
    没写入的那条也标成已写入。建议表本身的 status 词汇表不因此扩充（仍只有
    pending/applied/stale/rejected 四个，兼容旧记录）——这份细分只走 API
    响应，不落库。
    """
    return [
        {"suggestion_id": s.suggestion_id, "outcome": bucket}
        for s, bucket in paired
        if s.suggestion_id
    ]


def apply_suggestions_detailed(
    suggestions: list[CleanSuggestion],
    auto_pin: bool = False,
    *,
    sync_reverse: bool = False,
) -> dict[str, object]:
    """
    将用户接受的建议写入 TM 数据库，逐类给出去向。
    若 auto_pin=True，写入后同时固定这些词条（防止重复清洗）。

    返回 {applied, unchanged, skipped, outcomes}：
      applied   真正改写了译文的条数；
      unchanged 库里译文本来就与建议一致、无需改动的条数；
      skipped   被乐观并发拦下、词条已固定、已删除，或建议译文为空没法写的条数；
      outcomes  逐条 {suggestion_id, outcome}，outcome 属于
                updated/unchanged/pinned/stale/missing/invalid，只覆盖本次
                实际提交（accepted=true）的建议——未勾选的不在其中。
                invalid＝译文规整后为空、这一行没落库，建议保持 pending。
    界面要靠 applied/unchanged/skipped 这三个数字如实汇报——把 unchanged
    混进 skipped，用户会以为自己的词条被别人改过或被固定了，其实什么问题都没有。

    写入后按逐条结果结算建议表：写进去的（含译文本来就一样的）标 applied，
    被乐观并发拦下、词条已固定或已删除的标 stale。少了这一步，建议永远
    停在 pending，下次打开审阅列表还会看到同一批已经处理过的旧建议。

    accepted=false 的建议从头到尾不参与这个函数：既不写入、也不结算，
    在建议表里保持原样（多半是 pending）——用户没做决定的东西，下次打开
    复核面板还应该原样看到，不能因为「这一轮没勾」就被悄悄销账。
    """
    empty: dict[str, object] = {"applied": 0, "unchanged": 0, "skipped": 0, "outcomes": []}
    accepted_suggestions = [s for s in suggestions if s.accepted]
    if not accepted_suggestions:
        return dict(empty)
    accepted_suggestions = _resolve_pending_suggestions(accepted_suggestions)
    accepted = [(s.entry_id, s.new_target) for s in accepted_suggestions]
    expected_versions = {
        s.entry_id: s.expected_version
        for s in accepted_suggestions
        if s.expected_version
    }
    outcome = tm_manager.bulk_update_detailed(
        accepted,
        sync_reverse=sync_reverse,
        expected_versions=expected_versions or None,
        word_type=(
            tm_manager.CLEANING_LOCKED_WORD_TYPE
            if auto_pin
            else tm_manager.REVIEWED_AUTO_WORD_TYPE
        ),
    )
    count = len(outcome["updated"])
    unchanged = len(outcome["unchanged"])
    # 逐条去向按提交行序配对（bulk_update_detailed 的 "rows" 与 accepted 一一
    # 对齐）。zip 在两边长度不符时静默截断，宁可让多出来的建议留在 pending，
    # 也不给它们编造去向。
    paired = list(zip(accepted_suggestions, outcome.get("rows") or []))
    _settle_suggestion_rows(paired)
    invalid = sum(1 for _, bucket in paired if bucket == "invalid")
    skipped = (
        len(outcome["stale"]) + len(outcome["pinned"]) + len(outcome["missing"]) + invalid
    )
    outcomes = _build_suggestion_outcomes(paired)
    if skipped:
        logger.warning(f"清洗建议已过期并跳过 {skipped} 条")
    if count:
        logger.info(
            f"清洗确认写入 {count} 条，状态升级为 "
            f"{'cleaning_locked' if auto_pin else 'reviewed_auto'}"
        )
    return {"applied": count, "unchanged": unchanged, "skipped": skipped, "outcomes": outcomes}
