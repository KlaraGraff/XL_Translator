"""PDF「自动组合」里转 Word 翻译的部分：哪些页转 Word、连续的页合成一段转 Word 翻译
再转回 PDF、「同时输出 Word」。

PdfImageTranslationRunner 继承 PdfWordSegmentsMixin；这里用到的 self 上的状态
（_word_pages、_word_terms、_office_pdf_engine……）都在 runner 的 __init__ 里建好。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from core.output_metadata import build_translation_metadata, metadata_parameters, write_translation_metadata
from core.pdf_models import PDF_OUTPUT_STATE_FAILED, SOURCE_TYPE_IMAGE, PdfFileRecord, _PreparedPdfFile
from core.pdf_routes import KIND_BLANK, ROUTE_WORD, page_route
from core.task_runner import StatusMsg, user_facing_reason


class PdfWordSegmentsMixin:
    def _save_word_copy(self, record: PdfFileRecord, prepared: _PreparedPdfFile) -> None:
        """「同时输出 Word」：整份文件（空白页除外）都在同一段里转 Word 翻好时，把那份
        Word 放到译文 PDF 旁边。混合文件不出 Word：拆成几段的 Word 拼不回一份文件。"""
        if not getattr(self._settings.pdf, "also_save_word", False):
            return
        done = [item for item in record.word_segments if item.get("status") == "done" and item.get("docx_path")]
        kind = self._document_kinds.get(str(prepared.item.path))
        if len(done) != 1 or kind is None:
            return
        first, last = int(done[0]["first_page"]), int(done[0]["last_page"])
        if any(not first <= page.page_number <= last for page in kind.pages if page.kind != KIND_BLANK):
            return
        target = Path(prepared.translated_pdf_path).with_suffix(".docx")
        try:
            shutil.copy2(done[0]["docx_path"], target)
            write_translation_metadata(
                target,
                build_translation_metadata(
                    surface="pdf",
                    source_path=record.source_path,
                    target_lang=self._settings.pdf.target_lang,
                    source_lang=getattr(self._settings, "source_lang", "auto"),
                    parameters=metadata_parameters(self._settings, "pdf"),
                    output_mode=getattr(self._settings.pdf_output, "output_mode", "task_folder"),
                    fallback_used=getattr(getattr(self, "_output_resolution", None), "fallback_used", ""),
                ),
            )
        except OSError as exc:
            self._log("WARN", f"[{record.name}] 另存 Word 失败：" + user_facing_reason(exc, fallback="无法写入输出文件夹。"))
            return
        record.translated_docx_path = str(target)
        self._log("OK", f"[{record.name}] 另存了一份 Word：{target.name}")

    def _planned_word_pages(self, prepared: _PreparedPdfFile) -> list[int]:
        """自动组合里首选转 Word 的页（可提取文字的页，且设置成转 Word）。"""
        record = prepared.record
        if (getattr(self._settings.pdf, "translation_route", "image") != "auto"
                or record.source_type == SOURCE_TYPE_IMAGE
                or record.status == PDF_OUTPUT_STATE_FAILED):
            return []
        kind = self._document_kinds.get(str(prepared.item.path))
        if kind is None:
            return []
        # 「继续上次翻译」已经复用了译好页图的页不再转 Word：段首会把整段转回的 PDF
        # 拼进来，复用的页再单独装一次，内容就重复了，还白花一次翻译。段在这些页处断开。
        reused = {page.page_number for page in record.pages}
        planned: list[int] = []
        blanks: list[int] = []
        for item in sorted(kind.pages, key=lambda page: page.page_number):
            if item.page_number in reused:
                blanks = []
                planned.append(-1)  # 断开
                continue
            if item.kind == KIND_BLANK and not item.oversized:
                blanks.append(item.page_number)
                continue
            if page_route(self._settings.pdf, oversized_a3=False, oversized=item.oversized,
                          page_kind=item.kind) == ROUTE_WORD:
                # 夹在两页转 Word 的页中间的空白页随段一起转：一份文字文件中间插一页
                # 空白，不该被拆成两段（拆开就出不了「同时输出 Word」）。
                if planned and planned[-1] > 0:
                    planned.extend(blanks)
                planned.append(item.page_number)
            else:
                planned.append(-1)
            blanks = []
        return [number for number in planned if number > 0]

    def _collect_word_terms(self, terms: dict[str, Any], where: str) -> None:
        """一段转 Word 翻译的「记忆库」结果：位置前面补上文件名和页码。"""
        if not terms:
            return
        terms = dict(terms)
        not_applied = []
        for item in terms.get("not_applied") or []:
            if not isinstance(item, dict):
                continue
            locations = [str(value) for value in item.get("locations") or [] if value]
            item = {**item, "locations": [f"{where} {value}" for value in locations] or [where]}
            not_applied.append(item)
        terms["not_applied"] = not_applied
        self._word_terms.append(terms)

    def _run_word_segments(self, prepared_files: list[_PreparedPdfFile], *, total_pages: int = 0) -> None:
        """自动组合（Beta）：在页面翻译开始前，把每份文件里连续的正文页合成一段，转 Word
        翻译，再转回 PDF。转好的段在装配时按原页序拼进来；转不成的页随后按保底方式
        处理（见 _decide_page_route）。转出来的 Word 只留在过程文件夹里。

        这一步属于「翻译」阶段：转好的页按页数计入进度，正在翻的一段按 Word 流程真实
        返回的份额计（转不成的页之后还要按保底方式翻，不算）。"""
        from core.pdf_word_route import OfficePdfError, docx_to_pdf, translate_pdf_via_word

        plans = [(prepared, self._planned_word_pages(prepared)) for prepared in prepared_files]
        plans = [(prepared, pages) for prepared, pages in plans if pages]
        if not plans:
            return
        unavailable = self._word_segments_possible()
        hooks = self._inplace_hooks
        finished_pages = 0
        for prepared, page_numbers in plans:
            record = prepared.record
            key = str(prepared.item.path)
            segments: list[list[int]] = []
            for number in page_numbers:
                if segments and segments[-1][-1] == number - 1:
                    segments[-1].append(number)
                else:
                    segments.append([number])
            for index, seg in enumerate(segments, start=1):
                first, last = seg[0], seg[-1]
                label = f"第 {first} 页" if first == last else f"第 {first}–{last} 页"
                entry: dict[str, Any] = {"segment": index, "first_page": first, "last_page": last,
                                         "docx_path": "", "pdf_path": "", "status": "", "message": "",
                                         "issue_count": 0, "engine": ""}
                record.word_segments.append(entry)

                def mark(status: str, message: str = "", note: str = "") -> None:
                    entry.update(status=status, message=message)
                    for number in seg:
                        self._word_pages[(key, number)] = {"segment": index, "status": status,
                                                           "message": message, "note": note}

                if unavailable:
                    mark("failed", unavailable)
                    continue
                if not self._wait_while_paused():
                    mark("stopped", "任务已停止")
                    continue
                self._queue.put(StatusMsg(f"状态：正在把「{record.name}」{label}转成 Word 翻译…"))
                self._log("INFO", f"[{record.name}] {label}的文字可以提取，先转成 Word 翻译（Beta）。")
                work = Path(prepared.translated_pages_dir) / "word" / f"segment_{index:02d}"
                work.mkdir(parents=True, exist_ok=True)
                stem = Path(record.name).stem
                seg_pdf = work / f"{stem}_p{first}-{last}.pdf"
                base = finished_pages

                def segment_progress(done: int, total: int, *, base: int = base, size: int = len(seg)) -> None:
                    if total_pages:
                        units = base + size * min(1.0, max(0.0, done / max(1, total)))
                        self._send_translation_progress(units, total_pages)

                try:
                    _extract_pdf_pages(Path(record.source_copy_path), seg_pdf, first, last)
                    result = translate_pdf_via_word(
                        seg_pdf, work, work / f"{stem}_p{first}-{last}_translated.docx", self._settings,
                        source_lang=str(getattr(hooks, "source_lang", "") or self._settings.source_lang or "auto"),
                        should_stop=self._stop_event.is_set, log=self._log,
                        file_label=f"{record.name} {label}", key_overrides=self._key_overrides,
                        term_file_paths=[Path(record.source_path)], progress=segment_progress,
                        document_profile=self._shared_document_profile(record.source_path, seg),
                    )
                    self._collect_word_terms(result.terms, f"{record.name} {label}")
                    self._record_text_batch_counts(result.retry_count, result.rate_limit_reduction_count)
                    if not result.ok:
                        mark("stopped" if result.stopped else "failed",
                             "任务已停止" if result.stopped else "转成 Word 翻译失败")
                        if not result.stopped:
                            self._log("WARN", f"[{record.name}] {label}转成 Word 翻译失败：{result.error}")
                        continue
                    translated_pdf = work / f"{stem}_p{first}-{last}_translated.pdf"
                    engine = docx_to_pdf(Path(result.output_path), translated_pdf)
                except OfficePdfError as exc:
                    mark("failed", "译好的正文转回 PDF 失败")
                    self._log("WARN", f"[{record.name}] {label}译好的正文转回 PDF 失败：{str(exc)[:200]}")
                    continue
                except Exception as exc:  # noqa: BLE001 - one segment only costs that segment
                    logger.exception("[PDF] 正文段转 Word 翻译失败")
                    mark("failed", "转成 Word 翻译出错")
                    self._log("WARN", f"[{record.name}] {label}转成 Word 翻译出错："
                              + user_facing_reason(exc, fallback="处理这几页时出错。"))
                    continue
                entry.update(docx_path=result.output_path, pdf_path=str(translated_pdf),
                             issue_count=len(result.issues), engine=engine)
                record.word_issue_count += len(result.issues)
                mark("done", note=f"{label}的文字可以提取，转成 Word 翻译后转回 PDF 拼入")
                finished_pages += len(seg)
                segment_progress(1, 1)
                self._log("OK", f"[{record.name}] {label}已转成 Word 翻译，并转回 PDF，装配时按原页序拼入。")

    def _word_segments_possible(self) -> str:
        """可提取文字的页转 Word 要用的条件；全满足返回空串，否则返回原因。只查一次。"""
        if self._office_pdf_engine is None:
            from core.pdf_word_route import office_pdf_engine, word_route_available

            if not word_route_available():
                self._office_pdf_engine = ""
                self._office_pdf_message = "未安装「PDF 处理组件」"
            else:
                engine, message = office_pdf_engine()
                self._office_pdf_engine = engine
                self._office_pdf_message = "" if engine else "电脑上没有 Microsoft Word 或 LibreOffice，译好的正文转不回 PDF"
        return "" if self._office_pdf_engine else self._office_pdf_message



def _extract_pdf_pages(source: Path, target: Path, first: int, last: int) -> None:
    """把第 first–last 页（从 1 数）另存成一份 PDF，用随安装包分发的 pypdfium2。"""
    from core.pdf_image_translation import _load_pdfium

    pdfium = _load_pdfium()
    src = pdfium.PdfDocument(str(source))
    part = pdfium.PdfDocument.new()
    try:
        part.import_pages(src, pages=list(range(first - 1, last)))
        part.save(str(target))
    finally:
        part.close()
        src.close()
