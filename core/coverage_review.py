"""补译复核的运行时接线：把 :mod:`core.coverage_arbitration` 的判定逻辑接到模型上。

``coverage_arbitration`` 是纯逻辑（不认识引擎、不认识调度器），本模块负责剩下那半：
读记忆库、按批发请求、把裁定回填、把结论写进日志。Word 和 Excel 走同一份实现——
同一份文档换个格式跑，不该有两种复核尺度。

调用方拿到 :class:`ArbitrationOutcome` 之后自己决定怎么报：Word 的报告条目按段落
定位，Excel 按 ``分表!坐标``，两边的 quality_issues 结构本来就不一样，硬合并只会
让两边都别扭。本模块只保证「判得一样」。
"""

from __future__ import annotations

import json
from typing import Callable

from loguru import logger

from core import tm_manager
from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.api_scheduler import API_REQUEST_CATEGORY_RECOVERY, WeightedApiScheduler
from core.coverage_arbitration import (
    RETRANSLATE_UNCERTAIN,
    ArbitrationOutcome,
    ArbitrationPair,
    apply_arbitration,
    collect_arbitration_candidates,
    review_coverage_pairs,
)
from core.engine_dispatcher import is_local_engine_name
from core.word_batching import estimate_api_request_weight
from core.translation_protocol import extract_replace_translation, is_replace_translation
from engines.base_engine import engine_supports_chat, strip_markdown_json

VERDICT_EQUIVALENT = "equivalent"
VERDICT_NOT_EQUIVALENT = "not_equivalent"
VERDICT_UNCERTAIN = "uncertain"

_VERDICTS = {VERDICT_EQUIVALENT, VERDICT_NOT_EQUIVALENT, VERDICT_UNCERTAIN}


def _candidate_text(candidate: str | None) -> str:
    value = str(candidate or "").strip()
    if is_replace_translation(value):
        return extract_replace_translation(value).strip()
    return value


def arbitrate_coverage_units(
    units,
    *,
    engine,
    api_scheduler: WeightedApiScheduler | None,
    target_lang: str,
    source_lang: str,
    lang_pair: str | None,
    concurrency: int,
    file_name: str,
    log: Callable[[str, str], None],
    stop_event=None,
    context_label: str = "补译复核",
    scheduler_label: str | None = None,
) -> ArbitrationOutcome | None:
    """复核「这一处的已有译文真的是它的译文吗」，判错的打回去重新翻译。

    补译模式靠启发式判断某处原文旁边那段文字是不是译文。判成"已有译文"而其实不是，
    那段原文就永远留在文档里，体检也发现不了（用的是同一套启发式）；判反了顶多多插
    一条译文，看得见。所以这里宁可多翻，不可漏翻。

    返回 ``None`` 表示这一趟没做任何事（没有候选，或者复核自己出错已按原判处理），
    调用方不必再做后续处理。改判已经写回 ``units``，写入器读的是同一批对象。
    """
    candidates = collect_arbitration_candidates(units)
    if not candidates:
        return None

    known_translations: dict[str, str] = {}
    if lang_pair:
        try:
            tm_result = tm_manager.lookup_batch(
                [unit.source_text.strip() for unit in candidates], lang_pair
            )
            known_translations = {
                text: str(value)
                for text, value in (tm_result or {}).items()
                if value
            }
        except Exception as exc:  # noqa: BLE001 - 记忆库只是省一次模型调用
            logger.debug(f"{context_label}读取记忆库失败：{exc!r}")

    arbitrate = None
    if engine_supports_chat(engine):
        def arbitrate(pairs) -> dict[str, str]:
            if stop_event is not None and stop_event.is_set():
                # 用户按了停止，剩下的一律保持原判——停止不该顺手把一份翻好的
                # 文档改判成"要重翻"。
                return {pair.id: VERDICT_EQUIVALENT for pair in pairs}
            return run_pair_arbitration_batch(
                engine,
                pairs,
                target_lang=target_lang,
                source_lang=source_lang,
                api_scheduler=api_scheduler,
                error_callback=lambda message: log("WARNING", message),
                context_label=context_label,
                scheduler_label=scheduler_label,
            )

    try:
        outcome = review_coverage_pairs(
            units,
            known_translations=known_translations,
            arbitrate=arbitrate,
            max_workers=max(1, int(concurrency or 4)),
            notify_model_checks=lambda count, batches: log(
                "INFO",
                (
                    f"  → {context_label}：{count} 对已有译文送模型判定，"
                    f"分 {batches} 批发出，请稍候。"
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # 复核只是给启发式加的一道保险。它自己出错（限流、网络、模型异常）不能连累
        # 整个文件——外层的 per-file except 会把这个文件当成"打不开"，直接不出译文，
        # 那比不复核严重得多。出错就退回原判：启发式说已覆盖就已覆盖。
        log(
            "WARNING",
            f"  → {file_name}：{context_label}未能完成（{exc}），本文件按原判处理。",
        )
        return None

    flipped = apply_arbitration(outcome)
    uncertain = sum(
        1
        for review in outcome.retranslated
        if review.reason == RETRANSLATE_UNCERTAIN
    )
    # 把"模型说不是译文"和"没问出结果"分开报：后者成批出现时说明接口在抖，
    # 不是文档里真有那么多配错的段落。
    detail = f"{len(flipped)} 对改为重新翻译"
    if uncertain:
        detail += f"（其中 {uncertain} 对因未取得判定结果而从严处理）"
    # 有候选就报一句，哪怕结论是"全都没问题"。只在有异常时才吭声的检查，用户
    # 无从分辨它是查过了没事，还是压根没跑。
    batch_note = (
        f"（分 {outcome.model_batch_count} 批）" if outcome.model_batch_count else ""
    )
    log(
        "INFO",
        (
            f"  → {context_label}：{len(candidates)} 对已有译文，"
            f"其中 {outcome.model_check_count} 对送模型判定{batch_note}，{detail}。"
        ),
    )
    if outcome.skipped_over_cap:
        log(
            "WARNING",
            (
                f"  → {context_label}：可疑对超过单文件上限，"
                f"{outcome.skipped_over_cap} 对未送模型，按原判保留为已有译文。"
            ),
        )
    if flipped:
        log(
            "INFO",
            f"  → {file_name}：{len(flipped)} 处原判「已有译文」经复核改为补译。",
        )
    return outcome


def run_pair_arbitration_batch(
    engine,
    pairs: list[ArbitrationPair],
    *,
    target_lang: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    error_callback: Callable[[str], None] | None = None,
    context_label: str = "补译复核",
    scheduler_label: str | None = None,
) -> dict[str, str]:
    """一次请求判一批「原文 ＋ 紧邻内容」，返回 {id: verdict}。

    补译复核取消长度比预筛之后，送判对数是原来的好几倍；一对一次请求会把请求数打爆，
    所以按批发。返回的 key 用调用方给的 id，不靠顺序对齐——模型漏返、多返、乱序都
    只会让对应的那几对落到 uncertain，不会把 A 的裁定安到 B 头上。

    出错一律返回空 dict（上游按 uncertain 处理，也就是重翻），只有
    ApiKeyTemporarilyUnavailableError 要往上抛——那是"这个 key 暂时不能用"的调度信号，
    吞掉会让整轮任务在错误的 key 上空转。
    """
    entries: list[tuple[str, str, str]] = []
    for pair in pairs:
        candidate_text = _candidate_text(pair.candidate)
        if not candidate_text:
            continue
        entries.append((pair.id, pair.source, candidate_text))
    if not entries:
        return {}

    system_prompt = build_pair_arbitration_prompt()
    user_payload = json.dumps(
        {
            "source_language": source_lang,
            "target_language": target_lang,
            "pairs": [
                {
                    "id": pair_id,
                    "source_text": source,
                    "existing_translation": candidate,
                }
                for pair_id, source, candidate in entries
            ],
        },
        ensure_ascii=False,
    )
    weight_texts: list[str] = []
    for _, source, candidate in entries:
        weight_texts.extend((source, candidate))
    weight = estimate_api_request_weight(weight_texts, system_prompt)

    request_generation: int | None = None
    try:
        if api_scheduler is None:
            raw = engine.chat(system_prompt, user_payload)
        else:
            with api_scheduler.slot(weight, category=API_REQUEST_CATEGORY_RECOVERY) as lease:
                request_generation = lease.generation
                raw = engine.chat(system_prompt, user_payload)
        payload = json.loads(strip_markdown_json(raw))
    except Exception as exc:  # noqa: BLE001 - 判不出就当拿不准，不能连累整个文件
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None and not is_local_engine_name(engine.engine_name):
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label=scheduler_label or context_label,
                error_callback=error_callback,
            )
            if decision is not None:
                return run_pair_arbitration_batch(
                    engine,
                    pairs,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    error_callback=error_callback,
                    context_label=context_label,
                    scheduler_label=scheduler_label,
                )
        if error_callback is not None:
            # 一批判不出就是一批内容被从严重翻，用户看到多出来的译文得知道是为什么。
            error_callback(
                f"  → {context_label}：一批 {len(entries)} 对未取得判定结果（{exc}），"
                "这一批按从严处理。"
            )
        return {}

    if not isinstance(payload, dict):
        return {}
    items = payload.get("results")
    if not isinstance(items, list):
        return {}

    verdicts: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        pair_id = str(item.get("id", "")).strip()
        if not pair_id:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        if verdict not in _VERDICTS:
            verdict = VERDICT_UNCERTAIN
        verdicts[pair_id] = verdict
    return verdicts


def build_pair_arbitration_prompt() -> str:
    """补译复核专用提示词——尺度比机器译文的质量仲裁宽得多。

    那一份判的是"机器刚翻出来的译文合不合格"，从严是对的。这一份判的是"文档里本来
    就有的这段文字，是不是上一段的译文"，写它的多半是人：单位的法定译名、合同上的
    签署译名跟字面翻译本来就对不齐，按机器译文的尺度去卡，会把大批好好的人工译文
    判成"不是译文"，然后在旁边再插一条机器译文——那是给用户添乱。
    所以这里只抓三种硬伤：说的是另一件事、整块内容缺失、数字对不上。
    """
    return (
        "你在核对一份双语文档：给你若干对文字，每一对是「文档里的一段原文」和"
        "「紧挨在它后面的那段文字」。你只判断后者是不是前者的译文。\n"
        "这些译文多半是人工写的，不是机器翻的。判定尺度要宽松，以下情形全部算 equivalent：\n"
        "1. 用了单位、公司、项目、机构的法定译名、官方译名或惯用译名，而不是字面直译；\n"
        "2. 用了合同、公文里约定俗成的签署译名、职务译名、文件名译法，与字面翻译对不上；\n"
        "3. 术语选词不同、用了同义词、表达习惯不同；\n"
        "4. 语序不同、句子被拆开或被合并、标点与分段不同；\n"
        "5. 使用了缩写、简称、代号或编号形式；\n"
        "6. 译文比原文简洁，省去了重复表述或不影响事实的修饰语。\n"
        "只有下面三种情形才判 not_equivalent：\n"
        "a. 两段讲的明显是另一件事（内容不相关，或者后者其实是另一段的译文）；\n"
        "b. 原文里有整块实质内容在译文里完全缺失——是整块没有，不是简写或省略修饰；\n"
        "c. 数字、日期、金额、期限、编号互相矛盾。\n"
        "以上三种都不符合就判 equivalent；确实拿不准才判 uncertain。\n"
        "输入是一个 JSON 对象，pairs 数组里每一项有 id、source_text、existing_translation。\n"
        "必须逐项判定：每个 id 各给一条结果，不能漏项、不能合并、不能自己编造 id。\n"
        "只输出一个 JSON 对象，不要输出 markdown 或解释文字。格式："
        '{"results":[{"id":"0","verdict":"equivalent|not_equivalent|uncertain"}]}'
    )
