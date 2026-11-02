"""PDF 生图与审图的逐页尝试。
PdfImageTranslationRunner 通过 PdfImageGenerationMixin 用它。

本文件里用到的 core.pdf_image_translation 模块级名字都写成 `_pit.名字`，
运行时到原模块里取，和搬家前是同一个对象，测试对原模块的 patch 也照样生效。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.pdf_models import SOURCE_TYPE_PDF

if TYPE_CHECKING:
    from core.pdf_image_translation import (  # noqa: F401
        Any,
        PageQualityResult,
        Path,
        PdfPageRecord,
        WeightedApiScheduler,
    )


class PdfImageGenerationMixin:
    def _generate_page_attempts(
        self,
        page_record: PdfPageRecord,
        page_count: int,
        translated_pages_dir: Path,
        review_candidates_dir: Path,
        max_attempts: int,
        scheduler: WeightedApiScheduler,
        review_scheduler: WeightedApiScheduler,
        model_config,
        review_model_config,
        *,
        source_type: str = SOURCE_TYPE_PDF,
        document_context: str = "",
    ) -> PdfPageRecord:
        output_path = translated_pages_dir / _pit.page_image_name(page_record.page_number, page_count)
        preserve_model_format = source_type == _pit.SOURCE_TYPE_IMAGE
        last_error = ""
        last_quality: PageQualityResult | None = None
        last_image_bytes: bytes = b""
        last_review_failed = False
        review_feedback = ""
        target_language = _pit.get_target_lang_display(
            self._settings.pdf.target_lang,
            self._settings.custom_target_langs,
            include_optional=True,
        )
        # 自定义目标语言的说明（例如「用台湾用字」「用粤语口语」）也要交给图像模型，
        # 否则生图时只看到一个语言名。
        language_note = _pit.get_target_lang_description(
            self._settings.pdf.target_lang,
            self._settings.custom_target_langs,
        ).strip()
        if language_note:
            document_context = (
                f"Notes on the target language ({target_language}), written by the user:\n{language_note}"
                + (f"\n\n{document_context}" if document_context else "")
            )
        review_enabled = bool(self._settings.pdf.review_enabled and review_model_config is not None)
        page_record.review_enabled = review_enabled
        if review_enabled and self._review_switched_off:
            # 审核模型在这次任务里已经确认不收图：这一页不再送审，按「未审核」交付。
            review_enabled = False
        # 质检容差必须跟着「这一页会被请求成什么尺寸」走，否则对只支持固定
        # 尺寸的模型（gpt-image-1）来说这道门谁都过不去，每页都跑满重试。
        ratio_tolerance = _pit.pdf_page_ratio_tolerance(
            source_width=page_record.source_width_px,
            source_height=page_record.source_height_px,
            model=str(getattr(model_config, "model", "") or ""),
        )
        if ratio_tolerance > _pit.PDF_ASPECT_RATIO_TOLERANCE + 1e-9:
            self._log(
                "INFO",
                f"{self._page_log_prefix(page_record)}当前模型仅支持固定尺寸输出，比例不符时将补充白边以保持原比例，"
                "不再重试。",
                visible=False,
            )
        attempt = 1
        while attempt <= max_attempts:
            if self._image_model_rejected:
                return self._page_kept_after_image_model_rejected(page_record, source_type)
            try:
                lease = scheduler.acquire_lease(
                    1,
                    should_stop=self._stopping_page_attempts,
                )
            except _pit.ApiSchedulerAcquireCancelled:
                break
            self._note_page_request_started(page_record)
            try:
                if self._stopping_page_attempts():
                    break
                self._record_api_call()
                image_bytes = self._image_client.generate_page(
                    source_image_path=_pit.Path(page_record.source_image_path),
                    target_language=target_language,
                    target_lang_code=self._settings.pdf.target_lang,
                    model_config=model_config,
                    review_feedback=review_feedback or None,
                    # 只在有说明时才传，老的/第三方的生图客户端不必认识这个参数。
                    **({"document_context": document_context} if document_context else {}),
                )
            except Exception as exc:  # noqa: BLE001 - page/model classification.
                if isinstance(exc, _pit.ImageModelUnavailableError) or _pit.is_model_unavailable_error(exc):
                    # 这条消息会直接当成任务的终止提示显示出来，所以在抛出时就
                    # 换成人话；原文留给 debug 日志。
                    _pit.logger.debug(f"[PDF] 图像模型不可用原始错误：{exc!r}")
                    raise _pit.ImageModelUnavailableError(
                        _pit._model_unavailable_reason(
                            exc,
                            fallback=(
                                "图像翻译模型当前不可用，请在设置中检查此连接的 API 密钥、"
                                "模型名称和账户额度。"
                            ),
                        )
                    ) from exc
                decision = _pit.handle_api_concurrency_limit(
                    exc,
                    scheduler=scheduler,
                    request_generation=lease.generation,
                    context_label=f"PDF 第 {page_record.page_number} 页",
                    error_callback=lambda message: self._record_rate_limit_reduction(message),
                    # 限流退避里睡的是整整 30 秒。不把停止标志交给它，用户点了停止
                    # 之后每一页都要先把这一觉睡完才肯回来，停止响应被拖成几十秒。
                    should_stop=self._stop_event.is_set,
                )
                if decision is not None and decision.should_retry:
                    _pit.time.sleep(0.2)
                    continue
                # 原始异常文本（含接口地址、JSON 错误体）只进 debug 日志；
                # 用户看到的每一处都用一句中文说明。
                _pit.logger.debug(
                    f"[PDF] 第 {page_record.page_number} 页第 {attempt} 次生成失败原始错误：{exc!r}"
                )
                last_error = _pit.user_facing_reason(
                    exc,
                    fallback="图像翻译模型本次未返回可用结果，请稍后重试，或在设置中更换连接。",
                )
                self._record_page_retrying(page_record)
                self._log(
                    "WARN",
                    f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 次生成失败：{last_error}",
                )
                page_record.attempts = attempt
                if self._image_output_guard.failed(exc, key=self._page_status_key(page_record)):
                    # 还没成功过一页，又连续两页都是「这个模型生成不了图」：不再一页页地试。
                    # 整页图像翻译时整个任务停下；其他翻译方式里，剩下要整页图像翻译的页保留原样。
                    self._reject_image_model(model_config)
                attempt += 1
                continue
            finally:
                scheduler.release(lease)

            quality = _pit.check_page_quality(
                image_bytes,
                source_width=page_record.source_width_px,
                source_height=page_record.source_height_px,
                ratio_tolerance=ratio_tolerance,
                source_image_path=page_record.source_image_path or None,
            )
            page_record.attempts = attempt
            last_quality = quality
            self._apply_quality_flags(page_record, quality)
            last_image_bytes = image_bytes
            candidate_artifact: dict[str, Any] | None = None
            candidate_path: Path | None = None
            if review_enabled:
                candidate_dir = (
                    review_candidates_dir
                    / _pit.page_image_name(page_record.page_number, page_count).removesuffix(".png")
                )
                candidate_path = candidate_dir / f"attempt_{attempt:02d}.png"
                _pit._write_image_candidate(image_bytes, candidate_path)
                candidate_artifact = {
                    "attempt": attempt,
                    "candidate_image_path": str(candidate_path),
                    "review_path": "",
                    "quality_status": quality.status,
                    "review_status": "skipped",
                    "summary": "",
                }
                page_record.candidate_artifacts.append(candidate_artifact)
            if quality.ok:
                if self._review_switched_off:
                    # 别的页刚确认审核模型不收图：这一页（包括重试中的）不再送审。
                    review_enabled = False
                if review_enabled and candidate_path is not None and candidate_artifact is not None:
                    # 审核轮次必须自己计数，不能拿 attempt：attempt 是生成、质检、审核
                    # 共用的，生成失败或质检不过导致的重跑也会让它 +1，把它当成审核
                    # 轮次，报告里就出现「审核重试 1 次」而审核模型其实只跑了一次。
                    page_record.review_attempts += 1
                    self._begin_page_review(attempt)
                    review_request_error: Exception | None = None
                    try:
                        while True:
                            review_generation: int | None = None
                            try:
                                with review_scheduler.slot(
                                    1,
                                    category=_pit.API_REQUEST_CATEGORY_RECOVERY,
                                    should_stop=self._stop_event.is_set,
                                ) as review_lease:
                                    review_generation = review_lease.generation
                                    self._record_review_api_call()
                                    review_result = self._review_client.review_page(
                                        source_image_path=_pit.Path(page_record.source_image_path),
                                        translated_image_path=candidate_path,
                                        target_language=target_language,
                                        model_config=review_model_config,
                                    )
                                break
                            except _pit.ApiSchedulerAcquireCancelled:
                                raise
                            except Exception as review_exc:
                                if (
                                    isinstance(review_exc, _pit.PdfReviewModelUnavailableError)
                                    or _pit.is_model_unavailable_error(review_exc)
                                    or _pit.looks_like_no_image_input(review_exc)
                                ):
                                    raise
                                decision = _pit.handle_api_concurrency_limit(
                                    review_exc,
                                    scheduler=review_scheduler,
                                    request_generation=review_generation,
                                    context_label=f"PDF 第 {page_record.page_number} 页审核",
                                    error_callback=lambda message: self._record_rate_limit_reduction(message),
                                    should_stop=self._stop_event.is_set,
                                )
                                if decision is None or not decision.should_retry:
                                    raise
                    except _pit.ApiSchedulerAcquireCancelled:
                        self._finish_page_review()
                        break
                    except Exception as exc:  # noqa: BLE001 - review participates in page recovery.
                        self._finish_page_review()
                        refused_images = _pit.looks_like_no_image_input(exc)
                        if not refused_images and (
                            isinstance(exc, _pit.PdfReviewModelUnavailableError) or _pit.is_model_unavailable_error(exc)
                        ):
                            _pit.logger.debug(f"[PDF] 审核模型不可用原始错误：{exc!r}")
                            raise _pit.PdfReviewModelUnavailableError(
                                _pit._model_unavailable_reason(
                                    exc,
                                    fallback=(
                                        "图像翻译审核模型当前不可用，请在设置中检查此连接的 API 密钥、"
                                        "模型名称和账户额度。"
                                    ),
                                )
                            ) from exc
                        review_request_error = exc
                        _pit.logger.debug(f"[PDF] 审核请求失败原始错误：{exc!r}")
                        review_failure_reason = (
                            _pit.IMAGE_INPUT_NO_MESSAGE
                            if refused_images
                            else _pit.user_facing_reason(exc, fallback="审核模型本次未返回可用结果。")
                        )
                        if self._review_input_guard.failed(exc, key=self._page_status_key(page_record)):
                            self._switch_off_review(review_model_config)
                        review_result = _pit.PdfPageReviewResult(
                            passed=False,
                            blocking_issues=[
                                _pit.PdfReviewIssue(
                                    type="review_request_failed",
                                    problem=review_failure_reason,
                                    suggestion="请重试审核或更换图像翻译审核模型。",
                                )
                            ],
                            summary=f"审核请求失败：{review_failure_reason}",
                        )
                    else:
                        self._finish_page_review()
                        self._review_input_guard.succeeded()
                        # 只有审核真的回了结果才算「调用成功」；请求出错的那条路不算。
                        self._mark_pdf_review_model_success()

                    review_path = candidate_path.with_name(f"attempt_{attempt:02d}_review.json")
                    _pit._write_review_json(review_path, review_result)
                    candidate_artifact["review_path"] = str(review_path)
                    if review_request_error is not None:
                        candidate_artifact["review_status"] = "error"
                        candidate_artifact["summary"] = review_result.summary
                        page_record.review_status = "failed"
                        page_record.review_issues = [
                            issue.__dict__ for issue in review_result.blocking_issues
                        ]
                        # 这一轮的图就是交付图，上一轮的轻微建议不能跟着它交出去。
                        page_record.review_minor_suggestions = list(review_result.minor_suggestions)
                        page_record.error = review_result.summary
                        # 审核异常保留候选图时也要按真实格式定扩展名：这条分支此前
                        # 一直用循环开头那个写死的 .png，模型返回 JPEG/WebP 时就会
                        # 写出一张「名叫 png 的 jpg」，装订和预览都要另猜格式。
                        output_path = _pit._translated_page_output_path(
                            translated_pages_dir,
                            page_record=page_record,
                            page_count=page_count,
                            image_bytes=image_bytes,
                            preserve_model_format=preserve_model_format,
                        )
                        width, height = self._write_accepted_page_image(
                            image_bytes,
                            output_path,
                            page_record=page_record,
                            quality=quality,
                            preserve_model_format=preserve_model_format,
                        )
                        page_record.status = "success"
                        page_record.translated_image_path = str(output_path)
                        page_record.output_width_px = width
                        page_record.output_height_px = height
                        page_record.final_candidate_attempt = attempt
                        self._record_page_review_failed()
                        self._mark_image_model_success()
                        self._record_page_recovered(page_record)
                        self._log(
                            "WARN",
                            f"{self._page_log_prefix(page_record)}审核步骤出错，已保留本次译图并标记为需人工复核。",
                        )
                        return page_record
                    candidate_artifact["review_status"] = (
                        "passed" if review_result.passed else "failed"
                    )
                    candidate_artifact["summary"] = review_result.summary
                    # 轻微建议按轮次整体替换，不能跨轮累加：前几轮的候选图早就被丢掉了，
                    # 它们的建议若留在页记录上，就会被报告当成本次交付译文的建议展示出来，
                    # 「审核轻微建议」的条数也会被放大成重试轮数倍。与同一分支里
                    # review_issues 的赋值语义对齐；被丢弃那几轮的建议随候选图留在
                    # review_candidates/attempt_NN_review.json 里备查。
                    page_record.review_minor_suggestions = list(review_result.minor_suggestions)
                    if review_result.minor_suggestions:
                        self._log(
                            "INFO",
                            f"第 {page_record.page_number} 页审核轻微建议："
                            + "；".join(review_result.minor_suggestions[:3]),
                        )
                    if review_result.passed:
                        output_path = _pit._translated_page_output_path(
                            translated_pages_dir,
                            page_record=page_record,
                            page_count=page_count,
                            image_bytes=image_bytes,
                            preserve_model_format=preserve_model_format,
                        )
                        width, height = self._write_accepted_page_image(
                            image_bytes,
                            output_path,
                            page_record=page_record,
                            quality=quality,
                            preserve_model_format=preserve_model_format,
                        )
                        self._record_page_review_passed()
                        page_record.status = "success"
                        page_record.review_status = "passed"
                        # 上一轮没通过时留下的 blocking issue 必须清掉：面板的
                        # review_summary 优先读它，不清就会出现「通过」和上一轮那句
                        # 「编号标签误译」并排显示，同一行自相矛盾。
                        page_record.review_issues = []
                        page_record.translated_image_path = str(output_path)
                        page_record.output_width_px = width
                        page_record.output_height_px = height
                        page_record.final_candidate_attempt = attempt
                        self._mark_image_model_success()
                        self._log(
                            "OK",
                            self._page_success_log(
                                page_record,
                                attempt,
                                max_attempts,
                                review_checked=True,
                            ),
                        )
                        self._record_page_recovered(page_record)
                        return page_record

                    last_review_failed = True
                    page_record.review_status = "retrying"
                    page_record.review_issues = [
                        issue.__dict__ for issue in review_result.blocking_issues
                    ]
                    last_error = _pit._review_failure_summary(review_result)
                    review_feedback = _pit._review_feedback_text(review_result)
                    self._log(
                        "WARN",
                        f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 轮审核未通过：{last_error}",
                    )
                    self._record_page_retrying(page_record)
                    attempt += 1
                    continue

                output_path = _pit._translated_page_output_path(
                    translated_pages_dir,
                    page_record=page_record,
                    page_count=page_count,
                    image_bytes=image_bytes,
                    preserve_model_format=preserve_model_format,
                )
                width, height = self._write_accepted_page_image(
                    image_bytes,
                    output_path,
                    page_record=page_record,
                    quality=quality,
                    preserve_model_format=preserve_model_format,
                )
                self._mark_image_model_success()
                page_record.status = "success"
                page_record.translated_image_path = str(output_path)
                page_record.output_width_px = width
                page_record.output_height_px = height
                self._log(
                    "OK",
                    self._page_success_log(
                        page_record,
                        attempt,
                        max_attempts,
                        review_checked=False,
                    ),
                )
                self._record_page_recovered(page_record)
                return page_record

            last_review_failed = False
            last_error = quality.message
            self._record_page_retrying(page_record)
            self._log(
                "WARN",
                f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 次自动检查未通过：{quality.message}",
            )
            attempt += 1

        if (
            source_type != _pit.SOURCE_TYPE_IMAGE
            and
            not last_review_failed
            and last_quality is not None
            and last_quality.status == "ratio_error"
            and last_image_bytes
        ):
            width, height = _pit.normalize_image_to_source_ratio(
                last_image_bytes,
                source_width=page_record.source_width_px,
                source_height=page_record.source_height_px,
                output_path=output_path,
            )
            page_record.status = "emergency_normalized"
            page_record.emergency_ratio_normalized = True
            page_record.translated_image_path = str(output_path)
            page_record.output_width_px = width
            page_record.output_height_px = height
            page_record.error = last_error
            self._record_page_recovered(page_record)
            self._log("WARN", f"{self._page_log_prefix(page_record)}已自动修正页面比例{self._page_elapsed_text(page_record)}")
            return page_record

        if self._stopping_page_attempts():
            # 中止时这一页没跑出任何可用结果：它可能一次模型调用都没发生（在队列里
            # 等着，闸门直接取消），也可能跑到一半被截断。无论哪种，它都不是「试过
            # 了没成」，不该被烧成失败占位页塞进交付产物、更不该让整份文件被判成
            # 「全部页面已完成」。退回「未开始」，交给主循环撤掉这条页记录：报告如实
            # 报截断，续译时这一页还能重新排队。
            page_record.status = _pit.PDF_PAGE_STATUS_STOPPED_UNSTARTED
            page_record.placeholder = False
            page_record.translated_image_path = ""
            page_record.error = last_error or "任务已停止，此页尚未生成结果。"
            return page_record

        if source_type == _pit.SOURCE_TYPE_IMAGE:
            page_record.status = _pit.PDF_OUTPUT_STATE_FAILED
            page_record.placeholder = False
            if last_review_failed:
                page_record.review_status = "failed"
                self._record_page_review_failed()
            page_record.translated_image_path = ""
            page_record.error = last_error or "此页未能生成译图"
            self._record_page_failed(page_record)
            self._log_page_not_generated(page_record)
            return page_record

        page_record.status = "placeholder_pending"
        page_record.placeholder = True
        if last_review_failed:
            page_record.review_status = "failed"
            self._record_page_review_failed()
        page_record.translated_image_path = str(
            translated_pages_dir / _pit.page_image_name(page_record.page_number, page_count, failed=True)
        )
        page_record.error = last_error or "此页未能生成译图"
        self._record_page_placeholder(page_record)
        self._log_page_not_generated(page_record)
        return page_record

    def _apply_quality_flags(
        self,
        page_record: PdfPageRecord,
        quality: PageQualityResult,
    ) -> None:
        """Carry the content-QC verdict of the latest attempt onto the page."""
        page_record.quality_flags = list(quality.review_reasons)
        page_record.quality_message = quality.review_message
        if quality.review_reasons:
            self._log(
                "WARN",
                f"{self._page_log_prefix(page_record)}内容质检提示，已保留本页并标记人工复核："
                f"{quality.review_message}",
            )

    def _begin_page_review(self, attempt: int) -> None:
        with self._review_lock:
            self._latest_review_round = max(self._latest_review_round, attempt)
            self._review_processing_count += 1
            self._emit_review_status_locked()

    def _finish_page_review(self) -> None:
        with self._review_lock:
            self._review_processing_count = max(0, self._review_processing_count - 1)
            self._emit_review_status_locked()

    def _log_page_not_generated(self, page_record: PdfPageRecord) -> None:
        elapsed = self._page_elapsed_text(page_record)
        if elapsed:
            self._log("WARN", f"{self._page_log_prefix(page_record)}未能生成可用译图{elapsed}")

    def _mark_image_model_success(self) -> None:
        self._image_output_guard.succeeded()
        if self._settings.image_model_role.availability_status == "available":
            return
        _pit._save_image_model_availability(
            self._settings,
            ok=True,
            message="图像翻译模型调用成功。",
            checked_at=_pit.datetime.now().isoformat(timespec="seconds"),
        )

    def _mark_pdf_review_model_success(self) -> None:
        if self._settings.pdf_review_model_role.availability_status == "available":
            return
        _pit._save_pdf_review_model_availability(
            self._settings,
            ok=True,
            message="图像翻译审核模型调用成功。",
            checked_at=_pit.datetime.now().isoformat(timespec="seconds"),
        )

    def _note_page_request_started(self, page_record: PdfPageRecord) -> None:
        """The page got its first model slot: start its clock (later attempts keep it)."""
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            if key in self._page_started:
                return
            self._page_request_seq += 1
            index = self._page_request_seq
            self._page_started[key] = (index, _pit.time.monotonic())
        self._page_requests.on_event(_pit.BatchRequestEvent(_pit.BATCH_EVENT_SENT, index=index, items=1))

    def _page_elapsed_text(self, page_record: PdfPageRecord) -> str:
        """「，用时 N 秒」since the page's first request; empty before any request."""
        with self._page_status_lock:
            started = self._page_started.get(self._page_status_key(page_record))
        if started is None:
            return ""
        return f"，用时 {_pit.time.monotonic() - started[1]:.1f} 秒"

    def _page_kept_after_image_model_rejected(self, page_record: PdfPageRecord, source_type: str) -> PdfPageRecord:
        """图像模型已确认生成不了图时，还排着队的页：PDF 页保留原样，图片文件记为未能生成。"""
        page_record.placeholder = False
        page_record.translated_image_path = ""
        if source_type == _pit.SOURCE_TYPE_IMAGE:
            page_record.status = _pit.PDF_OUTPUT_STATE_FAILED
            page_record.error = _pit._with_sentence_end(self._image_model_rejected)
            self._record_page_failed(page_record)
            self._log_page_not_generated(page_record)
            return page_record
        page_record.status = _pit.PDF_PAGE_STATUS_SKIPPED_OVERSIZE
        page_record.skipped_oversize = True
        page_record.source_image_path = ""
        page_record.kept_reason = _pit._IMAGE_MODEL_REJECTED_KEPT_REASON
        page_record.error = ""
        # 前面失败过的这一页挂在「重试中」里，不撤掉的话进度上会一直显示它在重试。
        self._record_page_failed(page_record)
        self._log("WARN", f"{self._page_log_prefix(page_record)}{_pit._IMAGE_MODEL_REJECTED_KEPT_REASON}。")
        return page_record

    def _page_log_prefix(self, page_record: PdfPageRecord) -> str:
        if page_record.file_name:
            return f"[{page_record.file_name}] 第 {page_record.page_number} 页 "
        return f"第 {page_record.page_number} 页 "

    def _page_success_log(
        self,
        page_record: PdfPageRecord,
        attempt: int,
        max_attempts: int,
        *,
        review_checked: bool,
    ) -> str:
        suffix = ("生成成功，审核通过" if review_checked else "生成成功") + self._page_elapsed_text(page_record)
        if attempt <= 1:
            return f"{self._page_log_prefix(page_record)}{suffix}"
        return f"{self._page_log_prefix(page_record)}第 {attempt}/{max_attempts} 次{suffix}"

    def _record_api_call(self) -> None:
        """页面工作线程记一次图像生成调用（并发自增必须上锁）。"""
        with self._counter_lock:
            self._api_call_count += 1

    def _record_page_failed(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            self._retrying_pages.discard(key)
            self._emit_page_status_locked()

    def _record_page_recovered(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            if key in self._retried_pages:
                self._recovered_pages.add(key)
            self._retrying_pages.discard(key)
            self._emit_page_status_locked()

    def _record_page_retrying(self, page_record: PdfPageRecord) -> None:
        key = self._page_status_key(page_record)
        with self._page_status_lock:
            self._retrying_pages.add(key)
            self._retried_pages.add(key)
            self._emit_page_status_locked()

    def _record_page_review_failed(self) -> None:
        with self._review_lock:
            self._review_failed_count += 1
            self._emit_review_status_locked()

    def _record_page_review_passed(self) -> None:
        with self._review_lock:
            self._review_passed_count += 1
            self._emit_review_status_locked()

    def _record_rate_limit_reduction(self, message: str) -> None:
        with self._counter_lock:
            self._rate_limit_reduction_count += 1
        self._log("WARN", message)

    def _record_review_api_call(self) -> None:
        """页面工作线程记一次审核调用（并发自增必须上锁）。"""
        with self._counter_lock:
            self._review_api_call_count += 1

    def _reject_image_model(self, model_config) -> None:
        """图像翻译模型连续两页生成不了图：不再往它发页面。

        整页图像翻译时没有别的路可走，整个任务停下（走「图像模型不可用」那条路，
        已生成的页和报告照常保留）。其他翻译方式里，图像模型只是扫描页和保底用的：
        剩下要整页图像翻译的页保留原样，其余照常翻译。
        """
        message = f"图像翻译模型连续两页均未能生成图片，可能不是图像生成模型。{_pit.IMAGE_MODEL_ADVICE}"
        if model_config is not None:
            _pit.save_role_availability(model_config, _pit.ROLE_IMAGE, ok=False, message=f"图像翻译模型连接测试失败：{_pit.IMAGE_MODEL_ADVICE}")
        if getattr(self._settings.pdf, "translation_route", "image") == "image":
            # 主循环收到这个报错才会停下整个任务；在那之前，别的页别再向它发请求。
            self._image_model_fatal = message
            raise _pit.ImageModelUnavailableError(message)
        with self._review_lock:
            if self._image_model_rejected:
                return
            self._image_model_rejected = message
            self._image_model_missing = message
        self._log("WARN", f"{message}其余需要整页图像翻译的页面将保留原样。")

    def _stopping_page_attempts(self) -> bool:
        """这一页别再发新的生成请求：用户停止了，或者图像模型已确认生成不了图、整个
        任务正要停下（还在排队的页按「未开始」退回，续译时重新排队）。"""
        return self._stop_event.is_set() or bool(self._image_model_fatal)

    def _switch_off_review(self, review_model_config) -> None:
        """审核模型在这次任务里确认不收图：后面的页不再送审，结论存回设置。"""
        with self._review_lock:
            if self._review_switched_off:
                return
            self._review_switched_off = _pit.REVIEW_SWITCHED_OFF_MESSAGE
        self._log("WARN", _pit.REVIEW_SWITCHED_OFF_MESSAGE)
        if review_model_config is None:
            return
        _pit.save_image_input_verdict(
            review_model_config,
            _pit.IMAGE_INPUT_NO,
            _pit.IMAGE_INPUT_NO_MESSAGE,
            settings=self._settings,
            role=_pit.ROLE_PDF_REVIEW,
            availability_message=_pit.image_input_availability_message(
                review_model_config.label, _pit.IMAGE_INPUT_NO, _pit.IMAGE_INPUT_NO_MESSAGE
            ),
        )

    def _write_accepted_page_image(
        self,
        image_bytes: bytes,
        output_path: Path,
        *,
        page_record: PdfPageRecord,
        quality: PageQualityResult,
        preserve_model_format: bool,
    ) -> tuple[int, int]:
        """Write one accepted page, letterboxing it when the model's own size
        policy makes an exact aspect match impossible.

        Assembly scales the page image onto the PDF page rectangle, so an output
        whose shape differs from the source would be stretched.  Up to
        ``PDF_RATIO_LETTERBOX_THRESHOLD`` that stretch is invisible; beyond it
        (the fixed-size models are off by 5%~14%) the page is padded to the
        source shape instead.  This is a known, deterministic cost of that
        model — it does not consume a retry and does not mark the file for review.
        """
        source_ratio = _pit._safe_ratio(page_record.source_width_px, page_record.source_height_px)
        output_ratio = _pit._safe_ratio(quality.width, quality.height)
        needs_letterbox = (
            not preserve_model_format
            and source_ratio > 0
            and output_ratio > 0
            and abs(source_ratio - output_ratio) / source_ratio > _pit.PDF_RATIO_LETTERBOX_THRESHOLD
        )
        if not needs_letterbox:
            _pit._write_translated_page_image(
                image_bytes,
                output_path,
                preserve_model_format=preserve_model_format,
            )
            return quality.width, quality.height

        width, height = _pit.normalize_image_to_source_ratio(
            image_bytes,
            source_width=page_record.source_width_px,
            source_height=page_record.source_height_px,
            output_path=output_path,
        )
        page_record.ratio_letterboxed = True
        self._log(
            "INFO",
            f"{self._page_log_prefix(page_record)}模型仅支持固定尺寸输出，已为此页补充白边以保持原比例。",
            visible=False,
        )
        return width, height


# 放在文件末尾：先定义好上面的类，再取原模块，两边谁先被导入都不会循环导入失败。
import core.pdf_image_translation as _pit  # noqa: E402
