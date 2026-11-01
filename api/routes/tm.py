"""Translation-memory library: entries, import/export, conflicts and cleaning."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import FastAPI, HTTPException, Response

from api.task_manager import (
    TaskOptions,
)
from core import tm_manager
from core.tm_import_parse import build_template_xlsx, parse_import_file
from core.language_registry import (
    CustomTargetLang,
    is_custom_target_lang,
    normalize_custom_target_langs,
)
from core.tm_cleaner import CleanSuggestion, apply_suggestions_detailed
from settings import (
    load_settings,
    save_settings,
)
from api.schemas import (
    TmEntryPayload,
    TmEntryUpdatePayload,
    TmPinPayload,
    TmBulkPinPayload,
    TmBulkDeletePayload,
    TmImportPayload,
    TmImportParsePayload,
    TmFullImportPayload,
    TmApplySuggestionsPayload,
    TmCleanRequest,
)


def register(app: FastAPI) -> None:
    """Register the tm routes on ``app``."""

    @app.get("/api/tm/entries")
    def list_tm_entries(
        lang_pair: str,
        keyword: str = "",
        page: int = 1,
        page_size: int = 50,
        entry_filter: str = "all",
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        """``pinned`` 是记忆库页「只看固定」的筛选条件；不传时按固定状态不过滤。"""
        tm_manager.init_db()
        rows, total = tm_manager.search_entries(
            lang_pair,
            keyword,
            page=max(1, page),
            page_size=min(max(1, page_size), 200),
            entry_filter=entry_filter,
            pinned=pinned,
        )
        return {
            "entries": rows,
            "total": total,
            "stats": tm_manager.get_stats(lang_pair),
            "pin_count": tm_manager.get_pin_count(lang_pair, keyword),
            "filter_counts": tm_manager.get_filter_counts(lang_pair, keyword),
        }

    @app.get("/api/tm/entries/by-ids")
    def get_tm_entries_by_ids(ids: str = "") -> dict[str, Any]:
        """按 id 取词条当前内容；已删除的不在结果里。"""
        tm_manager.init_db()
        wanted = [int(part) for part in ids.split(",") if part.strip().isdigit()]
        return {"entries": tm_manager.get_entries_by_ids(wanted)}

    @app.post("/api/tm/entries", status_code=201)
    def create_tm_entry(payload: TmEntryPayload) -> dict[str, Any]:
        """如实回报这次手工新增的去向，不再一律当成写入成功。

        被固定词条挡下（只登记了待裁决冲突）、库里本来就是同一条译文、
        数据库出错，这三种情况以前和真正写入返回同一个形状，界面于是
        统一弹「已保存」，用户以为改动生效了。
        """
        tm_manager.init_db()
        result = tm_manager.insert_manual_entry_detailed(
            payload.source_text,
            payload.target_text,
            payload.lang_pair,
            sync_reverse=payload.sync_reverse,
            note=payload.note,
            usage=payload.usage or None,
            scope=payload.scope or None,
        )
        if result.status == "invalid":
            raise HTTPException(422, result.message)
        if result.status == "error":
            raise HTTPException(500, result.message)
        if result.status == "layout_pending":
            raise HTTPException(409, result.message)
        return {
            "changed": result.changed,
            "status": result.status,
            "message": result.message,
        }

    # Literal segments must be declared before the parameterised sibling.
    # Starlette matches routes in declaration order, so ``/entries/bulk/pin``
    # placed after ``/entries/{entry_id}/pin`` never runs: the router tries to
    # parse "bulk" as an int and answers 422 instead.
    @app.post("/api/tm/entries/bulk/pin")
    def bulk_pin_tm_entries(payload: TmBulkPinPayload) -> dict[str, int]:
        tm_manager.init_db()
        tm_manager.bulk_pin_entries(payload.ids, payload.pinned)
        return {"count": len(payload.ids)}

    @app.post("/api/tm/entries/bulk/delete")
    def bulk_delete_tm_entries(payload: TmBulkDeletePayload) -> dict[str, int]:
        tm_manager.init_db()
        return tm_manager.delete_entries(payload.ids, allow_pinned=payload.allow_pinned)

    @app.put("/api/tm/entries/{entry_id}")
    def update_tm_entry(entry_id: int, payload: TmEntryUpdatePayload) -> dict[str, Any]:
        """按真实原因回报编辑失败，不再把所有拒绝都说成「与现有原文冲突」。"""
        tm_manager.init_db()
        result = tm_manager.update_entry_full_detailed(
            entry_id,
            payload.source_text,
            payload.target_text,
            sync_reverse=payload.sync_reverse,
            note=payload.note,
            usage=payload.usage,
            scope=payload.scope,
            allow_pinned=payload.allow_pinned,
        )
        if result.status == "invalid":
            raise HTTPException(422, result.message)
        if result.status == "error":
            raise HTTPException(500, result.message)
        if result.status == "missing":
            raise HTTPException(404, result.message)
        if not result.changed:
            raise HTTPException(409, result.message)
        return {"changed": True, "status": result.status, "message": result.message}

    @app.delete("/api/tm/entries/{entry_id}")
    def delete_tm_entry(entry_id: int, allow_pinned: bool = False) -> dict[str, bool]:
        tm_manager.init_db()
        deleted = tm_manager.delete_entry(entry_id, allow_pinned=allow_pinned)
        if not deleted:
            raise HTTPException(409, "固定或不存在的词条不能删除；请先解除固定。")
        return {"deleted": True}

    @app.post("/api/tm/entries/{entry_id}/pin")
    def pin_tm_entry(entry_id: int, payload: TmPinPayload) -> dict[str, bool]:
        tm_manager.init_db()
        tm_manager.pin_entry(entry_id, payload.pinned)
        return {"changed": True}

    @app.get("/api/tm/export")
    def export_tm_entries(lang_pair: str) -> dict[str, Any]:
        tm_manager.init_db()
        return {"lang_pair": lang_pair, "entries": tm_manager.get_all_entries_for_export(lang_pair)}

    @app.get("/api/tm/export/full")
    def export_full_tm() -> dict[str, Any]:
        tm_manager.init_db()
        settings = load_settings()
        return tm_manager.get_full_export(settings.custom_target_langs)

    @app.post("/api/tm/import")
    def import_tm_entries(payload: TmImportPayload) -> dict[str, int]:
        tm_manager.init_db()
        return tm_manager.import_entries(
            payload.entries,
            payload.lang_pair,
            payload.mode,
            sync_reverse=payload.sync_reverse,
        )

    @app.post("/api/tm/import/parse")
    def parse_tm_import_file(payload: TmImportParsePayload) -> dict[str, Any]:
        try:
            data = base64.b64decode(payload.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, "无法读取导入文件。") from exc
        try:
            return parse_import_file(payload.file_name, data, payload.lang_pair)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/tm/import/template")
    def tm_import_template(locale: str = "zh") -> Response:
        return Response(
            build_template_xlsx(locale),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.post("/api/tm/import/full")
    def import_full_tm(payload: TmFullImportPayload) -> dict[str, int]:
        tm_manager.init_db()
        settings = load_settings()
        current = normalize_custom_target_langs(settings.custom_target_langs)
        by_code = {entry.code: entry for entry in current}
        code_map = {str(key).strip(): str(value).strip() for key, value in payload.code_map.items()}

        for raw in payload.custom_target_langs:
            incoming = CustomTargetLang.model_validate(raw)
            source_code = incoming.code.strip()
            if not source_code:
                raise HTTPException(422, "此记忆库备份中的自定义语言信息不完整，无法导入。")
            target_code = code_map.get(source_code, source_code)
            if not is_custom_target_lang(source_code) or not is_custom_target_lang(target_code):
                raise HTTPException(
                    422,
                    "此记忆库备份中的自定义语言信息不完整，无法导入。",
                )
            existing = by_code.get(target_code)
            if existing is not None:
                mapped_explicitly = source_code in code_map
                if not mapped_explicitly and (
                    existing.name != incoming.name or existing.description != incoming.description
                ):
                    raise HTTPException(
                        409,
                        f"备份中的自定义语言（代码 {source_code}）与本机同代码语言的设置不同；请在「自定义代码映射」中为其指定新代码后重试。",
                    )
                continue
            if target_code != source_code:
                incoming = incoming.model_copy(update={"code": target_code})
            by_code[target_code] = incoming
            current.append(incoming)

        custom_codes = set(by_code)

        def remap_pair(pair: object) -> str:
            parsed = tm_manager.split_lang_pair(str(pair or ""))
            if parsed is None:
                raise HTTPException(422, f"此记忆库备份包含无法识别的语言方向：{pair}")
            source, target = parsed
            source = code_map.get(source, source)
            target = code_map.get(target, target)
            if source.startswith("x-custom-"):
                raise HTTPException(422, "此记忆库备份将自定义语言用作源语言，无法导入（自定义语言只能作为目标语言）。")
            if target.startswith("x-custom-") and target not in custom_codes:
                raise HTTPException(422, "此记忆库备份中的自定义语言信息不完整，无法导入。")
            return f"{source}-{target}"

        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in payload.entries:
            mapped = dict(entry)
            mapped["lang_pair"] = remap_pair(mapped.get("lang_pair"))
            grouped.setdefault(mapped["lang_pair"], []).append(mapped)
        mapped_conflicts = []
        for candidate in payload.conflict_candidates:
            mapped = dict(candidate)
            mapped["lang_pair"] = remap_pair(mapped.get("lang_pair"))
            mapped_conflicts.append(mapped)

        settings.custom_target_langs = current
        save_settings(settings)
        inserted = updated = skipped = duplicates = 0
        for pair, entries in grouped.items():
            result = tm_manager.import_entries(
                entries,
                pair,
                payload.mode,
                sync_reverse=payload.sync_reverse and not pair.split("-", 1)[1].startswith("x-custom-"),
                preserve_status=True,
            )
            inserted += result.get("inserted", 0)
            # overwrite 模式下有多少条是盖掉了库里已有的词条。恢复备份时
            # 用户就是靠这个数字判断到底恢复了没有。
            updated += result.get("updated", 0)
            skipped += result.get("skipped", 0)
            duplicates += result.get("duplicates", 0)
        restored_conflicts = tm_manager.import_conflict_candidates(
            mapped_conflicts,
        )
        return {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "duplicates": duplicates,
            "custom_languages": len(current),
            "conflicts": restored_conflicts,
        }

    @app.get("/api/tm/conflicts")
    def list_tm_conflicts(lang_pair: str | None = None) -> dict[str, Any]:
        tm_manager.init_db()
        return {"conflicts": tm_manager.list_conflict_candidates(lang_pair)}

    @app.post("/api/tm/conflicts/{candidate_id}/resolve")
    def resolve_tm_conflict(candidate_id: int, payload: dict[str, str]) -> dict[str, bool]:
        action = str(payload.get("action") or "").strip()
        try:
            resolved = tm_manager.resolve_conflict_candidate(candidate_id, action)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not resolved:
            raise HTTPException(409, "冲突候选不存在、已处理或当前词条已发生变化。")
        return {"resolved": True}

    @app.get("/api/tm/clean/suggestions")
    def list_tm_clean_suggestions(lang_pair: str | None = None) -> dict[str, Any]:
        tm_manager.init_db()
        # 先淘汰对不上当前词条的旧建议：对应词条已被改过/固定/删除的建议
        # 再展示出来只会让用户勾一次、失败一次。
        tm_manager.expire_stale_cleaning_suggestions(lang_pair)
        # stale_count 只数「当前待审窗口」内失效的条数（现存最早 pending 之后
        # 失效的那些）：建议表没有批次列，全量数 stale 会无限累积，面板会永久
        # 顶着一句说陈年旧账的「另有 N 条已失效」。窗口口径下 pending 清零时
        # 计数自动归零。前端拿它在复核面板顶部渲染失效汇总。
        return {
            "suggestions": tm_manager.list_cleaning_suggestions(lang_pair),
            "stale_count": tm_manager.count_stale_suggestions_in_review_window(lang_pair),
        }

    @app.post("/api/tm/clean", status_code=202)
    def clean_tm_entries(payload: TmCleanRequest) -> dict[str, Any]:
        """Compatibility wrapper for the unified scheduled TM-clean task.

        The former synchronous endpoint could bypass resource risk checks.  It
        now has identical token semantics to the task-center route.
        """
        return app.state.task_manager.start_task(
            surface="tm_clean",
            source_path=payload.lang_pair,
            options=TaskOptions(lang_pair=payload.lang_pair),
            confirmation_token=payload.confirmation_token,
        )

    @app.post("/api/tm/clean/apply")
    def apply_tm_suggestions(payload: TmApplySuggestionsPayload) -> dict[str, Any]:
        tm_manager.init_db()
        suggestions = [
            CleanSuggestion(
                entry_id=item.entry_id,
                source_text=item.source_text,
                old_target=item.old_target,
                new_target=item.new_target,
                accepted=item.accepted,
                lang_pair=item.lang_pair,
                expected_version=item.expected_version,
                suggestion_id=item.suggestion_id,
            )
            for item in payload.suggestions
        ]
        # 三个聚合数字都回：unchanged（库里本来就一样）与 skipped（被并发拦下 /
        # 已固定 / 已删除）是两回事，界面混着说会让用户以为词条被人动过。
        # outcomes 是逐条去向（旧前端只读聚合数字，字段是增量的，照样能工作）：
        # 界面靠它在写入后原地标出每一行到底进了哪一桶，不必再靠总数瞎猜。
        return apply_suggestions_detailed(
            suggestions,
            auto_pin=payload.auto_pin,
            sync_reverse=payload.sync_reverse,
        )
