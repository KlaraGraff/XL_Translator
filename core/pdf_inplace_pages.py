"""PDF 原位替换模式的逐页流程。
PdfImageTranslationRunner 通过 PdfInplacePagesMixin 用它。

本文件里用到的 core.pdf_image_translation 模块级名字都写成 `_pit.名字`，
运行时到原模块里取，和搬家前是同一个对象，测试对原模块的 patch 也照样生效。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.pdf_image_translation import (  # noqa: F401
        Iterable,
        PageKind,
        Path,
        PdfFileRecord,
        PdfPageRecord,
        _PreparedPdfFile,
    )


class PdfInplacePagesMixin:
    def _run_inplace_pages(
        self,
        record: PdfFileRecord,
        prepared: _PreparedPdfFile,
        *,
        total_pages: int = 0,
    ) -> None:
        """「原位替换文字（Beta）」：逐页删字写回；失败或自检不过的页保留原页。

        ``total_pages`` 不为 0 时（任务的「翻译」阶段里），每页的识别、翻译、写回按真实
        完成的份额推进进度条；单页重新生成时为 0，进度另有说法。"""
        # 只处理还没试过的页。单页重新生成、「换回上一版」也会走到这里，它们不该
        # 顺带把别的原位替换页重跑一遍（又花一分钟、又调一次模型）。
        pages = [page for page in record.pages if page.inplace_requested and not page.inplace_status]
        if not pages or self._inplace_hooks is None:
            return
        if not _pit.inplace_target_supported(self._settings.pdf.target_lang):
            for page in pages:
                page.inplace_status = "kept_original"
                page.inplace_message = f"{_pit.INPLACE_TARGET_UNSUPPORTED}，这一页保留原样"
            self._log("WARN", f"[{record.name}] {_pit.INPLACE_TARGET_UNSUPPORTED}，相关页面保留原样。")
            return
        work_root = _pit.Path(prepared.translated_pages_dir) / "inplace"
        for page in sorted(pages, key=lambda item: item.page_number):
            if self._stop_event.is_set():
                page.inplace_status = "stopped"
                page.inplace_message = "任务已停止，这一页保留原样"
                continue
            self._log("INFO", f"[{record.name}] 第 {page.page_number} 页：原位替换文字（Beta）开始。")
            self._queue.put(_pit.StatusMsg(f"状态：正在原位替换「{record.name}」第 {page.page_number} 页的文字…"))
            key = (str(prepared.item.path), page.page_number)
            self._inplace_active_page = key
            try:
                self._run_one_inplace_page(record, prepared, page, work_root, total_pages=total_pages)
            finally:
                self._inplace_active_page = None
                self._inplace_page_fraction.pop(key, None)

    def _run_one_inplace_page(
        self,
        record: PdfFileRecord,
        prepared: _PreparedPdfFile,
        page: PdfPageRecord,
        work_root: Path,
        *,
        total_pages: int,
    ) -> None:
        from core.pdf_vector import translate_page_inplace

        hooks = self._inplace_hooks
        key = (str(prepared.item.path), page.page_number)
        head = f"[{record.name}] 第 {page.page_number} 页："
        progress = _pit._InplacePageProgress(
            self, key, total_pages=total_pages,
            log=lambda level, message: self._log(level, f"{head}{message}"),
            batch_label=f"{record.name} 第 {page.page_number} 页",
        )
        work = work_root / f"page_{page.page_number:04d}"
        # 字都在文字层里的页不用再跑文字识别；线条密的图纸、读不到文字的页照常识别
        # （core.pdf_routes.needs_ocr）。没判断过类型的页按尺寸：大幅面页照常识别。
        kind_info = self._page_kind_info(record.source_path, page.page_number - 1)
        native_only = (not _pit.needs_ocr(kind_info) if kind_info is not None
                       else not _pit.is_oversized_page(page.page_width_pt, page.page_height_pt))
        pdf_settings = self._settings.pdf
        target_lang = str(pdf_settings.target_lang or "zh")
        try:
            result = translate_page_inplace(
                _pit.Path(record.source_copy_path),
                page.page_number - 1,
                work,
                translate=_pit._with_stop(progress.wrap_translate(hooks.translate), self._stop_event.is_set),
                # 原位替换每一页单独写场景说明（一份图纸里常混着不同专业）；只有整份文件就是
                # 这一页时，沿用任务开始时写的那段（讲的是同一个内容）。
                profile=_pit._reuse_profile(self._shared_document_profile(record.source_path, [page.page_number]),
                                       hooks.profile),
                tesseract=hooks.tesseract,
                tesseract_languages=hooks.tesseract_languages,
                ocr_reader=getattr(hooks, "ocr_reader", None),
                source_lang=hooks.source_lang,
                log=self._log,
                native_only=native_only,
                memory=getattr(hooks, "memory", None),
                memory_file=_pit.Path(record.source_path),
                memory_location=f"{record.name} 第 {page.page_number} 页",
                report=progress.report,
                # 译文字体、放不下时缩到多小（「高级 → 原位替换」，与 CAD「译文写回」同一套规则）
                target_lang=target_lang,
                font_style=str(getattr(pdf_settings, "inplace_font", "sans") or "sans"),
                fit=pdf_settings.inplace_fit(target_lang),
                overflow_policy=str(getattr(pdf_settings, "inplace_overflow_policy", "write_translation") or "write_translation"),
                fallback_lang=self._document_source_lang(record, target_lang),
            )
        except Exception as exc:  # noqa: BLE001 - one page only costs that page
            _pit.logger.debug(f"[PDF] 原位替换失败：{exc!r}")
            page.inplace_status = "kept_original"
            page.inplace_message = "原位替换出错，这一页保留原样：" + _pit.user_facing_reason(exc, fallback="处理这一页时出错。")
            self._log("WARN", f"[{record.name}] 第 {page.page_number} 页{page.inplace_message}")
            return
        page.inplace_written = result.written
        page.inplace_kept = result.kept
        page.inplace_review = result.review + result.not_written
        page.inplace_overflow = int(getattr(result, "overflow_written", 0) or 0)
        page.inplace_untranslated = int(getattr(result, "untranslated", 0) or 0)
        page.inplace_term_review = int(getattr(result, "term_review", 0) or 0)
        page.inplace_review_items = result.review_items
        if result.ok and result.written > 0:
            page.inplace_status = "done"
            page.inplace_pdf_path = result.output_pdf
            note = "" if result.ocr_used or not hooks.ocr_wanted or native_only else "（没有安装文字识别组件，画成线条的字未识别）"
            if getattr(result, "ocr_skipped_cjk", False):
                note = _pit._inplace_cjk_ocr_note(result)
            elif getattr(result, "ocr_latin_only", False) and any(
                item.get("reason") == _pit._GARBAGE_REASON for item in result.review_items
            ):
                # 西文译好了，旁边画成线条的中文读成了乱码
                note = f"（{_pit._INPLACE_LATIN_OCR_HINT}）"
            page.inplace_message = (
                f"已替换 {result.written} 处，数字和编号等 {result.kept} 处不需要翻译，"
                f"{page.inplace_review} 处认不准保留原文{note}"
            )
            if page.inplace_overflow:
                page.inplace_message += f"；{page.inplace_overflow} 处空间不足仍写入译文，需复核"
            if page.inplace_untranslated:
                page.inplace_message += f"；{page.inplace_untranslated} 处未获得译文，需复核"
            if page.inplace_term_review:
                page.inplace_message += f"；{page.inplace_term_review} 处未采用记忆库中的设定译法，需复核"
            self._log("OK", f"[{record.name}] 第 {page.page_number} 页：{page.inplace_message}")
        else:
            page.inplace_status = "kept_original"
            page.inplace_message = result.error or _pit._inplace_nothing_written_message(
                result, ocr_missing=not (result.ocr_used or not hooks.ocr_wanted or native_only),
                cjk_ocr_skipped=bool(getattr(result, "ocr_skipped_cjk", False)),
                latin_ocr_only=bool(getattr(result, "ocr_latin_only", False)),
            )
            self._log("WARN", f"[{record.name}] 第 {page.page_number} 页：{page.inplace_message}")

    def _page_kind_info(self, source_path: str, page_index: int) -> PageKind | None:
        kind = self._document_kinds.get(source_path)
        if kind is None or page_index >= len(kind.pages):
            return None
        return kind.pages[page_index]

    def _document_source_lang(self, record: PdfFileRecord, target_lang: str) -> str:
        """整份文件的原文语言：页面自己读不到文字（全是线条）时按它决定识别哪种文字。

        读文字层的前 30 页，只读一次。整份都读不到文字时按法文（以前的做法），
        译成法文时按英文。"""
        key = str(record.source_path)
        if key not in self._document_langs:
            from core.pdf_vector.runner import guess_language

            fallback = "en" if target_lang.lower().startswith("fr") else "fr"
            try:
                lines = _pit._pdf_text_lines(_pit.Path(record.source_copy_path or record.source_path))
            except Exception as exc:  # noqa: BLE001 - only a hint for pages without text
                _pit.logger.debug(f"[PDF] 读取文字层判断原文语言失败：{exc!r}")
                lines = []
            self._document_langs[key] = guess_language(lines, fallback=fallback)
        return self._document_langs[key]

    def _shared_document_profile(self, source_path: str, pages: Iterable[int]) -> str | None:
        """原位替换页、转 Word 段能不能直接用任务开始时为整份 PDF 写的文档说明。

        只有这一部分就是整份文件（除空白页外每一页都在 ``pages`` 里：整份转 Word 的一段，
        或只有一页的图纸）时才沿用；其余转 Word 每段、原位替换每页各写一段，准确度优先
        （maple 2026-10-21）。
        "" 表示这次不用说明（「自动识别场景」关着）；None 表示由那一页、那一段自己写。
        """
        if self._profile_requester is None:
            return ""
        context = self._document_contexts.get(str(source_path), "")
        kind = self._document_kinds.get(str(source_path))
        if not context or kind is None or not kind.pages:
            return None
        covered = set(pages)
        if any(page.page_number not in covered for page in kind.pages if page.kind != _pit.KIND_BLANK):
            return None
        return context


# 放在文件末尾：先定义好上面的类，再取原模块，两边谁先被导入都不会循环导入失败。
import core.pdf_image_translation as _pit  # noqa: E402
