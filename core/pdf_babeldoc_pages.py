"""PDF「自动组合」里段落重排（BabelDOC）的部分：哪些页交给它、译完逐页检查、不过的页改走转 Word。

可提取文字的页选了「段落重排」时，在转 Word 之前先把这些页一次交给 BabelDOC（子进程，见
core/babeldoc_component.py），它在原 PDF 上按段落排入译文。每一页再拿原文页比一遍
（core/pdf_babeldoc_check.py：数字、缩字、残留标记、列表、重叠、漏翻……），通过的页另存成单页
PDF，装订时导入；没通过的页，以及没装组件、模型不支持、整份出错时的全部页，照旧转 Word
（转不成再原位替换、保底，见 _decide_page_route）。

翻译走用户设置的文本翻译模型：BabelDOC 拼好的提示词原样作为用户消息，系统消息是内置规则 +
补充要求 + 目标语言说明；数字、日期、代码原样照抄的要求另外写进 BabelDOC 的提示词；记忆库里
「始终采用」的词条作为 BabelDOC 的术语表。

PdfImageTranslationRunner 继承 PdfBabelDocPagesMixin；这里用到的 self 上的状态（_babeldoc_pages、
_babeldoc_reason、_pretranslated_pages）都在 runner 的 __init__ 里建好。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from core.pdf_models import PDF_OUTPUT_STATE_FAILED, SOURCE_TYPE_IMAGE, _PreparedPdfFile
from core.pdf_routes import KIND_TEXT, ROUTE_BABELDOC, inplace_target_supported, page_route
from core.task_runner import StatusMsg, user_facing_reason

#: 没通过检查的页写在页面说明里的开头
BABELDOC_CHECK_FAILED = "段落重排没通过检查"
BABELDOC_DONE_NOTE = "这一页用段落重排翻译：在原 PDF 上按段落排入译文，公式、图和表格线原样保留"
# 原文语言认不出时告诉 BabelDOC 的（只影响它提示词里的「从……译成」）
_FALLBACK_SOURCE = "en"


class PdfBabelDocPagesMixin:
    # PdfImageTranslationRunner 提供（这里只写给类型检查看）
    _settings: Any
    _babeldoc_pages: dict[tuple[str, int], dict[str, Any]]
    _babeldoc_reason: str | None
    _pretranslated_pages: int
    _document_kinds: dict[str, Any]
    _inplace_hooks: Any
    _stop_event: Any
    _queue: Any
    _log: Any
    _send_translation_progress: Any
    _wait_while_paused: Any

    # ── 哪些页 ───────────────────────────────────────────────────────────

    def _babeldoc_route_chosen(self) -> bool:
        pdf = self._settings.pdf
        return (getattr(pdf, "translation_route", "image") == "auto"
                and getattr(pdf, "auto_text_route", "") == ROUTE_BABELDOC)

    def _planned_babeldoc_pages(self, prepared: _PreparedPdfFile) -> list[int]:
        """首选段落重排的页：可提取文字、放得进 Word 的页（大页面按图纸处理），不含「整页大图上
        叠着表格线」的页（字在图片里）和「继续上次翻译」已经复用的页。"""
        record = prepared.record
        if (not self._babeldoc_route_chosen() or record.source_type == SOURCE_TYPE_IMAGE
                or record.status == PDF_OUTPUT_STATE_FAILED):
            return []
        kind = self._document_kinds.get(str(prepared.item.path))
        if kind is None:
            return []
        reused = {page.page_number for page in record.pages}
        return [item.page_number for item in sorted(kind.pages, key=lambda page: page.page_number)
                if item.kind == KIND_TEXT and not item.image_grid and item.page_number not in reused
                and page_route(self._settings.pdf, oversized_a3=False, oversized=item.oversized,
                               page_kind=item.kind) == ROUTE_BABELDOC]

    def _babeldoc_unavailable(self) -> str:
        """段落重排用不了的原因（这台电脑、这次任务的设置）；能用返回空串。只查一次。"""
        if self._babeldoc_reason is None:
            self._babeldoc_reason = self._find_babeldoc_reason()
        return self._babeldoc_reason

    def _find_babeldoc_reason(self) -> str:
        from core.babeldoc_component import component_paths

        hooks = self._inplace_hooks
        if hooks is None or getattr(hooks, "chat", None) is None:
            return "所选的文本翻译模型不支持段落重排"
        supported = getattr(hooks, "chat_supported", None)
        try:
            if supported is not None and not supported():
                return "所选的文本翻译模型不支持段落重排"
        except Exception as exc:  # noqa: BLE001 - no engine: Word reports the real error for these pages
            logger.debug(f"[PDF] 段落重排：文本翻译引擎建不起来：{exc!r}")
            return "文本翻译模型不可用"
        if not inplace_target_supported(self._settings.pdf.target_lang):
            # 与原位替换同一条：要连写排版的文字（阿拉伯文、泰文……）BabelDOC 也排不好。
            return "段落重排暂不支持该目标语言（文字需连写排版）"
        if component_paths() is None:
            return "未安装「段落重排组件」"
        return ""

    # ── 翻译 ─────────────────────────────────────────────────────────────

    def _babeldoc_translate_fn(self):
        """BabelDOC 每次要译文时调它：(kind, text) -> 译文。"""
        from core.babeldoc_component import LITERAL_RULES
        from core.pdf_vector.classify import target_language_name

        hooks = self._inplace_hooks
        chat = hooks.chat
        system = str(getattr(hooks, "chat_system_prompt", "") or "")
        retries = max(0, int(getattr(hooks, "chat_retries", 2) or 0))
        language = target_language_name(self._settings.pdf.target_lang)
        stop = self._stop_event

        def translate(kind: str, text: str) -> str:
            if kind == "plain":
                # BabelDOC 整批的回答解析不了时，改成一段一段问。
                user = (f"Translate the following text into {language}. Keep every placeholder such as "
                        "{v1}, <style id='1'> and </style> unchanged and in place. "
                        f"{LITERAL_RULES} Output only the translation, without any explanation.\n\n{text}")
            else:
                user = text
            for attempt in range(retries + 1):
                if stop.is_set():
                    raise RuntimeError("任务已停止")
                try:
                    reply = chat(system, user)
                except Exception as exc:  # noqa: BLE001 - retried, then BabelDOC decides
                    if attempt >= retries:
                        raise
                    logger.debug(f"[PDF] 段落重排的一次请求失败，稍后重试：{exc!r}")
                    time.sleep(min(8.0, 2.0 * (attempt + 1)))
                    continue
                if str(reply or "").strip():
                    return str(reply)
                if attempt >= retries:
                    raise RuntimeError("模型返回了空内容")
            raise RuntimeError("模型没有返回译文")

        return translate

    def _babeldoc_source_lang(self, source: Path, pages: list[int]) -> str:
        """原文语言：设置里选了就用它，否则从前几页的文字认。"""
        chosen = str(getattr(self._settings, "source_lang", "") or "").strip()
        if chosen and chosen != "auto":
            return chosen
        try:
            from core.pdf_component import require_fitz
            from core.pdf_vector.runner import guess_language

            fitz = require_fitz()
            with fitz.open(str(source)) as doc:
                texts = [doc[number - 1].get_text() for number in pages[:5] if 0 < number <= doc.page_count]
            return guess_language(texts, fallback=_FALLBACK_SOURCE)
        except Exception as exc:  # noqa: BLE001 - only the wording of BabelDOC's prompt depends on it
            logger.debug(f"[PDF] 段落重排认原文语言失败，按英文：{exc!r}")
            return _FALLBACK_SOURCE

    def _run_babeldoc_pages(self, prepared_files: list[_PreparedPdfFile], *, total_pages: int = 0) -> None:
        """自动组合选了段落重排时：在转 Word 之前，每份文件的文字页一次交给 BabelDOC，逐页检查。

        结果记在 ``_babeldoc_pages``：(源文件路径, 页码) → {status: done|failed|stopped, message,
        pdf_path}。转 Word 只转没 done 的页（PdfWordSegmentsMixin._planned_word_pages）。"""
        from core.babeldoc_component import LITERAL_RULES, translate_pdf
        from core.pdf_babeldoc_check import check_pages
        from core.pdf_word_segments import _extract_pdf_pages

        plans = [(prepared, self._planned_babeldoc_pages(prepared)) for prepared in prepared_files]
        plans = [(prepared, pages) for prepared, pages in plans if pages]
        if not plans:
            return
        reason = self._babeldoc_unavailable()
        if reason:
            for prepared, pages in plans:
                self._mark_babeldoc_pages(str(prepared.item.path), pages, "failed", reason)
            count = sum(len(pages) for _prepared, pages in plans)
            self._log("WARN", f"{reason}：{count} 页可提取文字的页改为转成 Word 翻译。")
            return
        hooks = self._inplace_hooks
        translate = self._babeldoc_translate_fn()
        workers = max(1, int(getattr(hooks, "chat_concurrency", 1) or 1))
        target = str(self._settings.pdf.target_lang or "zh")
        finished = 0
        for prepared, pages in plans:
            record = prepared.record
            key = str(prepared.item.path)
            if not self._wait_while_paused():
                self._mark_babeldoc_pages(key, pages, "stopped", "任务已停止")
                continue
            label = f"{len(pages)} 页"
            self._queue.put(StatusMsg(f"状态：正在用段落重排翻译「{record.name}」的 {label}…"))
            self._log("INFO", f"[{record.name}] {label}可提取文字，先用段落重排在原 PDF 上排入译文（Beta）。")
            source = Path(record.source_copy_path)
            work = Path(prepared.translated_pages_dir) / "babeldoc"
            base = finished

            def progress(percent: float, *, base: int = base, size: int = len(pages)) -> None:
                if total_pages:
                    # 检查通过前不算完成：译完的份额只记九成，余下的等检查过了再算。
                    units = base + size * 0.9 * min(1.0, max(0.0, percent / 100.0))
                    self._send_translation_progress(units, total_pages)

            terms_fn = getattr(hooks, "fixed_terms", None)
            try:
                glossary = terms_fn(record.source_path) if terms_fn is not None else []
            except Exception as exc:  # noqa: BLE001 - translate without the glossary rather than not at all
                logger.opt(exception=exc).warning("[PDF] 读取记忆库「始终采用」的词条失败")
                glossary = []
            try:
                result = translate_pdf(
                    source, work, pages=pages, lang_in=self._babeldoc_source_lang(source, pages),
                    lang_out=target, translate=translate, workers=workers, custom_prompt=LITERAL_RULES,
                    glossary=glossary, progress=progress, should_stop=self._stop_event.is_set,
                )
            except Exception as exc:  # noqa: BLE001 - one file only costs its pages a fallback
                logger.exception("[PDF] 段落重排出错")
                self._mark_babeldoc_pages(key, pages, "failed", "段落重排出错")
                self._log("WARN", f"[{record.name}] 段落重排出错，这些页改为转成 Word 翻译："
                          + user_facing_reason(exc, fallback="处理这份文件时出错。"))
                continue
            if result.requests:
                logger.info(f"[PDF] [{record.name}] 段落重排共请求模型 {result.requests} 次，"
                            f"失败 {result.failed_requests} 次（BabelDOC {result.version}）")
            if not result.ok:
                status = "stopped" if result.stopped else "failed"
                self._mark_babeldoc_pages(key, pages, status, result.error or "段落重排没有完成")
                if not result.stopped:
                    self._log("WARN", f"[{record.name}] {result.error or '段落重排没有完成'}，这些页改为转成 Word 翻译。")
                continue
            try:
                verdicts = check_pages(source, Path(result.output_pdf), pages, target_lang=target)
            except Exception:  # noqa: BLE001 - an unchecked page is never used (logger.exception keeps the traceback)
                logger.exception("[PDF] 段落重排的逐页检查出错")
                self._mark_babeldoc_pages(key, pages, "failed", f"{BABELDOC_CHECK_FAILED}（检查出错）")
                self._log("WARN", f"[{record.name}] 段落重排的结果没能检查，这些页改为转成 Word 翻译。")
                continue
            passed: list[int] = []
            failed: dict[str, list[int]] = {}
            for number in pages:
                verdict = verdicts[number]
                if not verdict.ok:
                    message = f"{BABELDOC_CHECK_FAILED}（{'、'.join(verdict.reasons)}）"
                    self._mark_babeldoc_pages(key, [number], "failed", message)
                    failed.setdefault("、".join(verdict.reasons), []).append(number)
                    logger.info(f"[PDF] [{record.name}] 第 {number} 页段落重排没通过检查：{'; '.join(verdict.details)}")
                    continue
                page_pdf = work / "pages" / f"page_{number:04d}.pdf"
                try:
                    page_pdf.parent.mkdir(parents=True, exist_ok=True)
                    _extract_pdf_pages(Path(result.output_pdf), page_pdf, number, number)
                except Exception as exc:  # noqa: BLE001 - that page falls back
                    logger.opt(exception=exc).warning(f"[PDF] 段落重排第 {number} 页另存失败")
                    self._mark_babeldoc_pages(key, [number], "failed", "段落重排的结果没能保存")
                    failed.setdefault("结果没能保存", []).append(number)
                    continue
                self._babeldoc_pages[(key, number)] = {"status": "done", "message": "",
                                                       "note": BABELDOC_DONE_NOTE, "pdf_path": str(page_pdf)}
                passed.append(number)
            finished += len(passed)
            if total_pages:
                self._send_translation_progress(finished, total_pages)
            if passed:
                self._log("OK", f"[{record.name}] 段落重排译好 {len(passed)} 页，装配时按原页序放入。")
            for reasons, numbers in failed.items():
                self._log("WARN", f"[{record.name}] 第 {_page_list(numbers)} 页{BABELDOC_CHECK_FAILED}（{reasons}），"
                          "改为转成 Word 翻译。")
        self._pretranslated_pages = finished

    def _mark_babeldoc_pages(self, key: str, pages: list[int], status: str, message: str) -> None:
        for number in pages:
            self._babeldoc_pages[(key, number)] = {"status": status, "message": message, "note": "", "pdf_path": ""}

    def _babeldoc_done_entry(self, key: str, page_number: int) -> dict[str, Any] | None:
        """这一页段落重排译好、通过了检查、单页 PDF 还在：返回记录，否则 None。"""
        entry = self._babeldoc_pages.get((key, page_number))
        if entry and entry.get("status") == "done" and Path(str(entry.get("pdf_path") or "")).is_file():
            return entry
        return None

    def _babeldoc_miss_reason(self, key: str, page_number: int) -> str:
        """首选段落重排、却没用上的原因（写进页面说明）；这一页没交给它（字在图片里的页）时为空。"""
        return str((self._babeldoc_pages.get((key, page_number)) or {}).get("message") or "")


def _page_list(numbers: list[int]) -> str:
    return "、".join(str(number) for number in numbers)


def fixed_glossary(target_lang: str, lang_pairs: list[str], file_path: str) -> list[tuple[str, str]]:
    """记忆库里对这份文件「始终采用」的词条 (原文, 译文)，交给 BabelDOC 当术语表。

    与 Excel / Word 句中采用设定译法同一条线（core.tm_terms）：同一原文的词条全是「始终采用」、
    译法只有一种才算；生效范围不含这份文件的不算。BabelDOC 只把段落里真出现的词条放进提示词。"""
    from collections import defaultdict

    from core.tm_terms import USAGE_ALWAYS, TermIndex, whole_key

    index = TermIndex.load(target_lang, lang_pairs=lang_pairs, file_paths=[file_path])
    groups: dict[str, list[Any]] = defaultdict(list)
    for entry in index.entries:
        groups[whole_key(entry.source)].append(entry)
    pairs: list[tuple[str, str]] = []
    for rows in groups.values():
        targets = {row.target.strip() for row in rows}
        if all(row.usage == USAGE_ALWAYS for row in rows) and len(targets) == 1:
            pairs.append((rows[0].source.strip(), targets.pop()))
    return pairs
