"""任务中心「查看与修改译文」（CAD）背后的服务：对照表、保存修改、Excel 导出导入、重新写回。

对照表的数据在任务自己留下的批次清单里（见 ``core.cad_review``）；这里负责从任务记录里
找到它，把修改顺手存进记忆库，并在后台线程里重新写回图纸。重新写回不调用模型、不花钱，
所以不占任务资源组，只是同一个任务同时只能有一次写回。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from api.cad_plugin import probe_status
from api.task_contracts import TaskConflictError, TaskInputError
from core import tm_manager
from core.cad_cache import default_cad_cache
from core.cad_language import latin_language_scores
from core.cad_review import (
    CadReviewError,
    clear_applied_edits,
    export_workbook,
    load_review,
    parse_workbook,
    rewrite_files,
    save_edits,
)
from core.cad_translation import SubprocessCadConverter

_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


def memory_pair(source_text: str, source_lang: str, target_lang: str) -> str | None:
    """这条修改存进记忆库时用的语言对；原文语种判断不出来时不存（返回 None）。"""
    target = str(target_lang or "").strip()
    source = str(source_lang or "").strip()
    if not target:
        return None
    if not source or source == "auto":
        if _CJK_RE.search(source_text):
            source = "zh"
        else:
            scores = {lang: score for lang, score in latin_language_scores(source_text).items() if score > 0}
            if not scores:
                return None
            source = max(scores, key=scores.get)
    if source.split("-", 1)[0].lower() == target.split("-", 1)[0].lower():
        return None
    return f"{source}-{target}"


class CadReviewService:
    def __init__(self, task_manager: Any) -> None:
        self._tasks = task_manager
        self._lock = threading.Lock()
        self._rewrites: dict[str, dict[str, Any]] = {}

    # ── 找到这次任务的批次清单 ─────────────────────────────

    def _task(self, task_id: str) -> tuple[dict[str, Any], Path]:
        status = self._tasks.task_status(task_id)
        if status.get("surface") != "cad":
            raise TaskInputError("只有 CAD 图纸翻译任务有译文对照表。")
        result = status.get("result") if isinstance(status.get("result"), dict) else {}
        manifest = str(result.get("manifest_path") or "").strip()
        if not manifest:
            raise TaskInputError("这次任务没有留下译文记录，可能还没有译完任何一张图纸。")
        return status, Path(manifest)

    def _load(self, manifest: Path) -> dict[str, Any]:
        try:
            return load_review(manifest)
        except CadReviewError as exc:
            raise TaskInputError(str(exc)) from exc

    def review(self, task_id: str) -> dict[str, Any]:
        status, manifest = self._task(task_id)
        review = self._load(manifest)
        with self._lock:
            rewrite = dict(self._rewrites.get(task_id) or {"active": False})
        return {
            "task_id": task_id,
            "terminal": bool(status.get("terminal")),
            "rewrite": rewrite,
            **review,
        }

    # ── 保存修改 ─────────────────────────────────────────

    def save(self, task_id: str, key: str, edits: dict[str, str]) -> dict[str, Any]:
        _status, manifest = self._task(task_id)
        self._ensure_idle(task_id)
        try:
            saved = save_edits(manifest, key, edits)
        except CadReviewError as exc:
            raise TaskInputError(str(exc)) from exc
        remembered = self._remember(manifest, saved)
        return {"saved": len(saved), "remembered": remembered}

    def _remember(self, manifest: Path, saved: dict[str, str]) -> int:
        """改过的译文存进记忆库，下次翻到同样的文字直接用。存不进去不影响这次保存。"""
        if not saved:
            return 0
        try:
            review = load_review(manifest)
            options = self._options_of(manifest)
        except (CadReviewError, OSError):
            return 0
        target = str(options.get("target_lang") or review.get("target_lang") or "")
        source_lang = str(options.get("source_lang") or "auto")
        count = 0
        try:
            tm_manager.init_db()
            for source, target_text in saved.items():
                pair = memory_pair(source, source_lang, target)
                if pair is None:
                    continue
                result = tm_manager.insert_manual_entry_detailed(source, target_text, pair)
                if result.status in {"written", "unchanged"}:
                    count += 1
        except Exception as exc:  # 记忆库出错不能让「保存修改」失败
            logger.warning(f"[CAD 对照表] 修改未能存进记忆库：{exc!r}")
        return count

    @staticmethod
    def _options_of(manifest: Path) -> dict[str, Any]:
        import json

        try:
            batch = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        options = batch.get("pipeline_options") if isinstance(batch, dict) else None
        return options if isinstance(options, dict) else {}

    # ── Excel ────────────────────────────────────────────

    def export(self, task_id: str) -> bytes:
        _status, manifest = self._task(task_id)
        return export_workbook(self._load(manifest))

    def import_workbook(self, task_id: str, payload: bytes) -> dict[str, Any]:
        _status, manifest = self._task(task_id)
        self._ensure_idle(task_id)
        review = self._load(manifest)
        try:
            changes = parse_workbook(review, payload)
        except CadReviewError as exc:
            raise TaskInputError(str(exc)) from exc
        rows = 0
        remembered = 0
        for key, edits in changes.items():
            try:
                saved = save_edits(manifest, key, edits)
            except CadReviewError as exc:
                raise TaskInputError(str(exc)) from exc
            rows += len(saved)
            remembered += self._remember(manifest, saved)
        return {"files": len(changes), "rows": rows, "remembered": remembered}

    # ── 重新写回 ─────────────────────────────────────────

    def _ensure_idle(self, task_id: str) -> None:
        with self._lock:
            if (self._rewrites.get(task_id) or {}).get("active"):
                raise TaskConflictError("正在按对照表重新写回图纸，请等它完成后再修改。", reason="cad_rewrite_active")

    def rewrite(self, task_id: str, keys: list[str]) -> dict[str, Any]:
        status, manifest = self._task(task_id)
        if not status.get("terminal"):
            raise TaskConflictError("任务还没有结束，请等它结束后再重新写回。", reason="task_not_terminal")
        review = self._load(manifest)
        rewritable = {str(file["key"]) for file in review["files"] if file.get("rewritable")}
        wanted = [key for key in dict.fromkeys(keys) if key in rewritable]
        if not wanted:
            reasons = [str(file.get("reason") or "") for file in review["files"] if str(file["key"]) in keys]
            raise TaskInputError(next((reason for reason in reasons if reason), "没有可以重新写回的图纸。"))
        converter_path = str(probe_status().get("converter") or "").strip()
        converter = SubprocessCadConverter(converter_path) if converter_path else None
        with self._lock:
            if (self._rewrites.get(task_id) or {}).get("active"):
                raise TaskConflictError("这批图纸正在重新写回，请等它完成。", reason="cad_rewrite_active")
            state = {"active": True, "done": 0, "total": len(wanted), "current": "", "results": []}
            self._rewrites[task_id] = state
        threading.Thread(
            target=self._run_rewrite,
            args=(task_id, manifest, wanted, converter),
            daemon=True,
            name=f"cad-rewrite-{task_id[:8]}",
        ).start()
        return {"accepted": len(wanted), "rewrite": dict(state)}

    def _run_rewrite(self, task_id: str, manifest: Path, keys: list[str], converter: Any) -> None:
        def progress(name: str, done: int, total: int) -> None:
            with self._lock:
                state = self._rewrites.get(task_id)
                if state is not None:
                    state.update(current=name, done=done, total=total)

        results: list[dict[str, Any]] = []
        try:
            results = rewrite_files(manifest, keys, converter=converter, dxf_cache=default_cad_cache(), progress=progress)
            clear_applied_edits(manifest, results)
        except Exception as exc:
            logger.opt(exception=exc).warning(f"[CAD 对照表] 重新写回失败：{exc!r}")
            message = str(exc) if isinstance(exc, CadReviewError) else "重新写回时出错，请导出诊断信息反馈。"
            results.append({"key": "", "name": "", "ok": False, "message": message})
        with self._lock:
            self._rewrites[task_id] = {
                "active": False,
                "done": len(keys),
                "total": len(keys),
                "current": "",
                "results": [
                    {key: value for key, value in item.items() if key != "key"} | {"file": item.get("key", "")}
                    for item in results
                ],
            }
