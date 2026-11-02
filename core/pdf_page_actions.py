"""PDF 单页重译、换回上一版、取消单页操作。
PdfImageTranslationRunner 通过 PdfPageActionsMixin 用它。

本文件里用到的 core.pdf_image_translation 模块级名字都写成 `_pit.名字`，
运行时到原模块里取，和搬家前是同一个对象，测试对原模块的 patch 也照样生效。
"""

# self 标成 PdfImageTranslationRunner（真正用这些方法的类），类型检查才认得 runner 的属性；
# pyright 不许 self 标成子类，所以本文件关掉这一条。
# pyright: reportGeneralTypeIssues=false

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.pdf_image_translation import (  # noqa: F401
        PdfImageTranslationRunner,
        Any,
        Path,
        PdfPageRecord,
        WeightedApiScheduler,
        _PdfPageAction,
        _PreparedPdfFile,
    )


class PdfPageActionsMixin:
    def rerun_page(
        self: PdfImageTranslationRunner,
        *,
        relative_path: str,
        page_number: int,
        api_scheduler: WeightedApiScheduler | None = None,
        review_api_scheduler: WeightedApiScheduler | None = None,
    ) -> dict[str, Any]:
        """Start re-running one page of a finished task; overwrites its output."""
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise _pit.PdfPageActionError("页码必须是整数。") from exc
        if self.is_running():
            raise _pit.PdfPageActionError("任务还在运行，请暂停后再做单页操作。")
        if self._finished_output_dir is None:
            raise _pit.PdfPageActionError("此任务没有保留输出目录，无法重新生成单页。")
        # Checked before the per-page checks below, not only at the claim: while
        # a rerun is in flight the page it owns has left ``record.pages``, so the
        # eligibility checks would otherwise answer "this page has no result to
        # regenerate" to someone who simply clicked a second page.
        self._raise_if_rerun_active()
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
        if prepared is None:
            raise _pit.PdfPageActionError("该任务没有这个文件的逐页记录。")
        record = prepared.record
        if page_number < 1 or page_number > max(0, int(record.page_count)):
            raise _pit.PdfPageActionError(
                f"页码超出范围：{record.name} 共 {record.page_count} 页。"
            )
        # Regenerating drops the existing output file before it rebuilds one.
        # A file that never produced an output has nothing to rebuild from, so
        # the rerun would only delete evidence and then fail the same way.
        if record.status not in {_pit.PDF_OUTPUT_STATE_COMPLETED, _pit.PDF_OUTPUT_STATE_NEEDS_REVIEW}:
            raise _pit.PdfPageActionError(
                f"{record.name} 本次未生成输出文件，无法通过重新生成单页解决；请新建任务。"
            )
        page = next(
            (item for item in list(record.pages) if item.page_number == page_number),
            None,
        )
        if page is None or page.status not in _pit._PDF_PAGE_REGENERABLE_STATUSES:
            raise _pit.PdfPageActionError(
                f"{record.name} 第 {page_number} 页没有可以重新生成的结果。"
            )
        with self._rerun_lock:
            self._raise_if_rerun_active(locked=True)
            self._rerun_page_key = (record.relative_path, page_number)
            self._rerun_error = ""
            thread = _pit.threading.Thread(
                target=self._run_page_rerun,
                args=(prepared, page_number, api_scheduler, review_api_scheduler),
                daemon=True,
                name=f"pdf-page-rerun-{page_number}",
            )
            self._rerun_thread = thread
        thread.start()
        return {
            "action": _pit.PDF_PAGE_ACTION_REGENERATE,
            "relative_path": record.relative_path,
            "name": record.name,
            "page_number": page_number,
            "applies_on": "now",
        }

    def restore_previous_page(self: PdfImageTranslationRunner, *, relative_path: str, page_number: int) -> dict[str, Any]:
        """把一页换回上一版译文，并按新的页图重新生成输出文件。"""
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise _pit.PdfPageActionError("页码必须是整数。") from exc
        if self.is_running():
            raise _pit.PdfPageActionError("任务还在运行，请暂停后再做单页操作。")
        if self._finished_output_dir is None:
            raise _pit.PdfPageActionError("此任务没有保留输出目录，无法换回上一版。")
        self._raise_if_rerun_active()
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
        if prepared is None:
            raise _pit.PdfPageActionError("该任务没有这个文件的逐页记录。")
        record = prepared.record
        if page_number < 1 or page_number > max(0, int(record.page_count)):
            raise _pit.PdfPageActionError(
                f"页码超出范围：{record.name} 共 {record.page_count} 页。"
            )
        if record.status not in {_pit.PDF_OUTPUT_STATE_COMPLETED, _pit.PDF_OUTPUT_STATE_NEEDS_REVIEW}:
            raise _pit.PdfPageActionError(
                f"{record.name} 本次未生成输出文件，无法通过换回上一版解决；请新建任务。"
            )
        page = next(
            (item for item in record.pages if item.page_number == page_number),
            None,
        )
        if page is None or not self._page_image_file(
            prepared,
            page_number,
            page,
            _pit.PDF_PAGE_IMAGE_KIND_PREVIOUS,
        ):
            raise _pit.PdfPageActionError(
                f"{record.name} 第 {page_number} 页没有上一版译文，无法换回。"
            )
        if not page.translated_image_path or not _pit.Path(page.translated_image_path).is_file():
            raise _pit.PdfPageActionError(
                f"{record.name} 第 {page_number} 页的当前译文页图已丢失，无法互换；请重新生成此页。"
            )
        with self._rerun_lock:
            self._raise_if_rerun_active(locked=True)
            self._restore_page_key = (record.relative_path, page_number)
        try:
            self._execute_restore_previous(prepared, page)
        finally:
            with self._rerun_lock:
                self._restore_page_key = None
        return {
            "action": "restore_previous",
            "relative_path": record.relative_path,
            "name": record.name,
            "page_number": page_number,
        }

    def cancel_page_action(self: PdfImageTranslationRunner, *, relative_path: str, page_number: int) -> dict[str, Any]:
        """Withdraw a queued page action before the resume sends any request.

        Queued actions only take effect at the pause boundary in ``_run``, so
        while the task is still paused nothing has reached the model yet and
        dropping the entry leaves the page exactly as it was.  Cancelling a
        page with nothing queued is accepted and reports ``cancelled: ""``:
        a second click racing the snapshot refresh must not surface an error.
        """
        try:
            page_number = int(page_number)
        except (TypeError, ValueError) as exc:
            raise _pit.PdfPageActionError("页码必须是整数。") from exc
        with self._page_action_lock:
            prepared = self._find_prepared_file(relative_path)
            if prepared is None:
                raise _pit.PdfPageActionError("该任务没有这个文件的逐页记录。")
            record = prepared.record
            cancelled = ""
            remaining: list[_PdfPageAction] = []
            for action in self._pending_page_actions:
                if action.relative_path == record.relative_path and action.page_number == page_number:
                    cancelled = action.kind
                else:
                    remaining.append(action)
            self._pending_page_actions = remaining
            file_name = record.name
        return {
            "action": "cancel",
            "cancelled": cancelled,
            "relative_path": relative_path,
            "name": file_name,
            "page_number": page_number,
        }

    def _run_page_rerun(
        self: PdfImageTranslationRunner,
        prepared: _PreparedPdfFile,
        page_number: int,
        api_scheduler: WeightedApiScheduler | None,
        review_api_scheduler: WeightedApiScheduler | None,
    ) -> None:
        record = prepared.record
        error = ""
        try:
            with _pit.provider_key_overrides(self._key_overrides):
                self._execute_page_rerun(
                    prepared,
                    page_number,
                    api_scheduler,
                    review_api_scheduler,
                )
        except _pit.PdfPageActionError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - rerun failure becomes a panel message.
            _pit.logger.exception("PDF 单页重新生成失败")
            error = _pit.user_facing_reason(
                exc,
                fallback="重新生成这一页时出现了未预期的问题，输出文件未更新。",
            )
        if error:
            self._log(
                "ERROR",
                f"[{record.name}] 第 {page_number} 页重新生成失败：{error}",
            )
        with self._rerun_lock:
            self._rerun_page_key = None
            self._rerun_error = error
            self._rerun_thread = None

    def _execute_page_rerun(
        self: PdfImageTranslationRunner,
        prepared: _PreparedPdfFile,
        page_number: int,
        api_scheduler: WeightedApiScheduler | None,
        review_api_scheduler: WeightedApiScheduler | None,
    ) -> None:
        record = prepared.record
        rerun_started = _pit.time.monotonic()
        # A finished run may have left the stop flag set (user stop / end-paused).
        # Leaving it set would make ``_process_prepared_pages`` refuse to submit
        # the very page this rerun exists to produce.
        self._stop_event.clear()
        self._pause_event.clear()
        self._stop_reason = ""
        self._fatal_model_error = ""
        self._fatal_review_model_error = ""
        self._stop_wait_started_at = None
        self._stop_wait_last_emit = 0.0

        # Resolved fresh rather than cached from the run: settings may have been
        # fixed since (a corrected API key is the common case), and a retained
        # terminal runner should not sit on key material it does not need.
        try:
            model_config = _pit.resolve_effective_model_config(self._settings, _pit.ROLE_IMAGE)
        except Exception as exc:  # noqa: BLE001 - init converted to a panel message.
            _pit.logger.debug(f"[PDF] 单页重生成解析翻译模型配置失败原始错误：{exc!r}")
            raise _pit.PdfPageActionError(
                "图像翻译模型的设置不可用："
                + _pit.user_facing_reason(exc, fallback="请在设置中检查图像翻译模型的连接。")
            ) from exc
        if not _pit.provider_supports_capability(model_config.provider, "image"):
            raise _pit.PdfPageActionError(
                _pit.unsupported_capability_message(
                    "图像翻译模型", model_config.provider, "image"
                )
            )
        if not model_config.model:
            raise _pit.PdfPageActionError("图像翻译模型的名称不能为空。")

        review_model_config = None
        review_concurrency = 1
        if self._settings.pdf.review_enabled:
            try:
                review_model_config = _pit.resolve_effective_model_config(
                    self._settings,
                    _pit.ROLE_PDF_REVIEW,
                )
                review_concurrency = _pit.resolve_pdf_review_concurrency(
                    self._settings,
                    mode=_pit.runtime_mode(review_model_config),
                )
            except Exception as exc:  # noqa: BLE001 - init converted to a panel message.
                _pit.logger.debug(f"[PDF] 单页重生成解析审核模型配置失败原始错误：{exc!r}")
                raise _pit.PdfPageActionError(
                    "图像翻译审核模型的设置不可用："
                    + _pit.user_facing_reason(
                        exc,
                        fallback="请在设置中检查图像翻译审核模型的连接。",
                    )
                ) from exc
            if not _pit.provider_supports_capability(review_model_config.provider, "vision_text"):
                raise _pit.PdfPageActionError(
                    _pit.unsupported_capability_message(
                        "图像翻译审核模型", review_model_config.provider, "vision_text"
                    )
                )
            if not review_model_config.model:
                raise _pit.PdfPageActionError("图像翻译审核模型的名称不能为空。")

        max_attempts = _pit.max_page_generation_attempts(self._settings.pdf.page_retry_attempts)
        self._review_total = max_attempts
        scheduler = api_scheduler or _pit.WeightedApiScheduler(1)
        same_review_group = (
            review_model_config is not None
            and _pit._api_group_signature_from_config(model_config)
            == _pit._api_group_signature_from_config(review_model_config)
        )
        review_scheduler = review_api_scheduler or (
            scheduler if same_review_group else _pit.WeightedApiScheduler(review_concurrency)
        )

        page = next(
            (item for item in record.pages if item.page_number == page_number),
            None,
        )
        if page is None or page.status not in _pit._PDF_PAGE_REGENERABLE_STATUSES:
            raise _pit.PdfPageActionError(
                f"{record.name} 第 {page_number} 页的结果已发生变化，已取消本次重新生成。"
            )
        self._log(
            "INFO",
            f"[{record.name}] 开始重新生成第 {page_number} 页：仅在成功后替换译文页和输出文件。",
        )
        # 事务边界。这一步之前先把「回滚需要的一切」拍下来：页记录本身、这份文件
        # 的产物字段和状态、逐页计数。重生成失败（模型 Key 失效是最常见的一种）时
        # 全部还原，磁盘上的高清/压缩 PDF 也从未被动过——旧版本从头到尾还在。
        page_backup = _pit.copy.deepcopy(page)
        artifact_backup = _pit._file_artifact_snapshot(record)
        counters_backup = self._page_counters_snapshot()
        committed = False
        stashed = self._apply_page_regenerate(prepared, page, transactional=True)
        try:
            try:
                self._process_prepared_pages(
                    [prepared],
                    max_attempts=max_attempts,
                    scheduler=scheduler,
                    review_scheduler=review_scheduler,
                    model_config=model_config,
                    review_model_config=review_model_config,
                    concurrency=1,
                    total_pages=max(1, int(record.page_count)),
                )
            except (_pit.ImageModelUnavailableError, _pit.PdfReviewModelUnavailableError):
                # _process_prepared_pages 探测到模型/审核模型致命不可用时是直接把异常
                # raise 出来的（它自己在 finally 里说了「致命错误直接 raise 出去，收敛
                # 循环再也回不来」），不是正常返回后再让调用方去看 self._fatal_model_error
                # 标志位——下面 `if self._fatal_model_error:` 那行如果没有这个 except
                # 接住异常，永远走不到：能把这个标志位设成真的两处（本文件内）都紧跟着
                # raise。这里必须真的接住它，用户看到的 toast 才带得上「旧产物已保留」。
                pass
            if self._fatal_model_error:
                # finally 块里会把这一页和文件记录整体回滚（见下方 committed 分支），
                # 旧的译文页、高清/压缩 PDF 全都原样保留——异常消息本身要把这句话
                # 说出来，用户看到的是 toast，不是这段代码的回滚逻辑（低-旧产物提示）。
                raise _pit.PdfPageActionError(
                    f"{_pit._with_sentence_end(self._fatal_model_error)}"
                    "已保留上一版译文页和输出文件，本次改动未生效。"
                )
            self._finalize_file_record(prepared, should_assemble=True)
            if record.status == _pit.PDF_OUTPUT_STATE_FAILED:
                # 装配失败：旧产物还在磁盘上（新产物是先写临时文件再原子替换的），
                # 所以这里同样要整体回滚，而不是把这份文件留在「失败」上——同样要
                # 把「旧产物没丢」说进异常消息（低-旧产物提示），不能只在 record.error
                # 缺失时走默认文案才带这句话。
                reason = _pit._with_sentence_end(
                    record.error or "页面重新生成后，未能重新生成输出文件。"
                )
                raise _pit.PdfPageActionError(f"{reason}已保留上一版，输出文件未改动。")
            committed = True
        finally:
            if committed:
                self._commit_page_image_stash(prepared, stashed, page_backup=page_backup)
            else:
                self._restore_page_image_stash(stashed)
                self._restore_page_after_failed_rerun(
                    prepared,
                    page_number=page_number,
                    page_backup=page_backup,
                    artifact_backup=artifact_backup,
                    counters_backup=counters_backup,
                )
        self._rewrite_finished_report(extra_elapsed_sec=_pit.time.monotonic() - rerun_started)

    def _execute_restore_previous(self: PdfImageTranslationRunner, prepared: _PreparedPdfFile, page: PdfPageRecord) -> None:
        record = prepared.record
        page_number = int(page.page_number)
        started = _pit.time.monotonic()
        self._log(
            "INFO",
            f"[{record.name}] 第 {page_number} 页开始换回上一版：重新生成输出文件，不调用模型。",
        )
        # 和单页重生成同一条事务纪律：先把「回滚需要的一切」拍下来。区别只在于
        # 这里没有模型调用，失败的成因只剩装配本身。
        page_backup = _pit.copy.deepcopy(page)
        artifact_backup = _pit._file_artifact_snapshot(record)
        counters_backup = self._page_counters_snapshot()
        # 成功路径故意不动 runner 的全局逐页计数器（_completed_page_count 等）：
        # 任务已终态，进度消息不再发，终态界面读的是 record.pages 重算出的记录级
        # 计数（_refresh_file_record_counts），口径以 record 为准。失败路径回滚
        # 计数器只是把快照原样放回，不是「成功也该调」的另一半。
        # 高清版装配是原子替换，可压缩版在它之后才装：压缩版失败时高清版已经换成
        # 新内容了。先留一份高清版原件，回滚时放回去，换回才真的「输出文件不动」。
        try:
            hd_backup = self._backup_output_file(record.translated_pdf_path)
        except OSError as exc:
            reason = _pit._with_sentence_end(
                _pit.user_facing_reason(exc, fallback="无法在输出文件夹里留一份当前的高清 PDF。")
            )
            raise _pit.PdfPageActionError(f"{reason}本次换回未进行。") from exc
        moves = self._swap_page_versions(prepared, page)
        committed = False
        try:
            try:
                self._finalize_file_record(
                    prepared,
                    should_assemble=True,
                    keep_compressed_output=True,
                )
            except _pit._CompressedRebuildFailed as exc:
                reason = _pit._with_sentence_end(str(exc))
                raise _pit.PdfPageActionError(f"{reason}已保留当前版本，本次换回未生效。") from exc
            if record.status == _pit.PDF_OUTPUT_STATE_FAILED:
                reason = _pit._with_sentence_end(
                    record.error or "换回上一版后，未能重新生成输出文件。"
                )
                raise _pit.PdfPageActionError(f"{reason}已保留当前版本，本次换回未生效。")
            committed = True
        except _pit.PdfPageActionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败变成一条面板消息。
            _pit.logger.exception("PDF 单页换回上一版失败")
            reason = _pit._with_sentence_end(
                _pit.user_facing_reason(exc, fallback="换回上一版时出现了未预期的问题。")
            )
            raise _pit.PdfPageActionError(f"{reason}已保留当前版本，本次换回未生效。") from exc
        finally:
            if not committed:
                self._put_back_output_file(record.translated_pdf_path, hd_backup)
                self._undo_page_version_swap(moves)
                _pit._copy_page_record(page_backup, page)
                _pit._restore_file_artifacts(record, artifact_backup)
                self._refresh_file_record_counts(record)
                self._restore_page_counters(counters_backup)
                self._log(
                    "WARN",
                    f"[{record.name}] 第 {page_number} 页换回上一版失败，已保留当前版本，输出文件未改动。",
                )
            elif hd_backup is not None:
                hd_backup.unlink(missing_ok=True)
        self._rewrite_finished_report(extra_elapsed_sec=_pit.time.monotonic() - started)
        self._log(
            "OK",
            f"[{record.name}] 第 {page_number} 页已换回上一版，输出文件已重新装配。",
        )

    def _swap_page_versions(
        self: PdfImageTranslationRunner,
        prepared: _PreparedPdfFile,
        page: PdfPageRecord,
    ) -> list[tuple[Path, Path]]:
        """三步互换这一页的当前版与上一版（文件与页记录一起），返回已完成的改名。

        三步都是同目录 os.replace：当前版→中转名、上一版→当前版的位置、中转名→
        上一版的位置。中途失败按相反顺序还原，磁盘上不会留下半套。
        """
        base_dir = prepared.translated_pages_dir
        current = _pit.Path(page.translated_image_path)
        previous = _pit.Path(page.previous_image_path)
        # 上一版的文件名就是「当初那一版的页图名」加了前缀，去掉前缀正好换回它原来
        # 的名字——模型换过输出格式时后缀跟着变，这条也照样成立。
        restored_current = base_dir / previous.name.removeprefix(_pit.PDF_PAGE_IMAGE_PREVIOUS_PREFIX)
        swapped_previous = base_dir / f"{_pit.PDF_PAGE_IMAGE_PREVIOUS_PREFIX}{current.name}"
        temp = base_dir / f"{_pit.PDF_PAGE_IMAGE_SWAP_PREFIX}{current.name}"
        for path in (current, previous, restored_current, swapped_previous, temp):
            if not _pit._path_is_within(path, base_dir):
                raise _pit.PdfPageActionError("此页的译文页图不在任务的页面存档中，已取消换回。")
        done: list[tuple[Path, Path]] = []
        try:
            temp.unlink(missing_ok=True)
            for src, dst in ((current, temp), (previous, restored_current), (temp, swapped_previous)):
                _pit.os.replace(src, dst)
                done.append((src, dst))
        except Exception as exc:  # noqa: BLE001 - 半套互换比换不成危险得多。
            self._undo_page_version_swap(done)
            raise _pit.PdfPageActionError(
                "换回上一版时页图文件改名失败："
                + _pit.user_facing_reason(exc, fallback="无法修改页面存档中的文件。")
                + "已保留当前版本，本次换回未生效。"
            ) from exc
        previous_state = dict(page.previous_page_state or {})
        current_state = _pit._page_version_state(page)
        if previous_state:
            _pit._apply_page_version_state(page, previous_state)
        page.translated_image_path = str(restored_current)
        page.previous_image_path = str(swapped_previous)
        page.previous_page_state = current_state
        # 尺寸以磁盘上的那张图为准：更早的记录里可能根本没有这一版的结论
        # （previous_page_state 为空），装配和报告都要拿真实尺寸说话。
        measured = _pit._resume_pick_page_image([restored_current], verify=False)
        if measured is not None:
            _, page.output_width_px, page.output_height_px = measured
        return done

    def _undo_page_version_swap(self: PdfImageTranslationRunner, moves: list[tuple[Path, Path]]) -> None:
        """按相反顺序把互换过的页图搬回去。"""
        for src, dst in reversed(moves):
            try:
                _pit.os.replace(dst, src)
            except Exception as exc:  # noqa: BLE001 - 已经在失败路径上，不再抛新错。
                _pit.logger.debug(f"[PDF] 换回上一版回滚页图失败：{dst} → {src}：{exc!r}")

    @staticmethod
    def _backup_output_file(path_text: str) -> Path | None:
        if not path_text or not _pit.Path(path_text).is_file():
            return None
        path = _pit.Path(path_text)
        backup = path.with_name(f".{path.name}.before-restore")
        _pit.shutil.copy2(path, backup)
        return backup

    def _put_back_output_file(self: PdfImageTranslationRunner, path_text: str, backup: Path | None) -> None:
        if backup is None or not path_text:
            return
        try:
            _pit.os.replace(backup, path_text)
        except OSError as exc:
            _pit.logger.warning(f"[PDF] 换回失败后放回高清 PDF 失败：{backup} -> {path_text}：{exc!r}")

    def _commit_page_image_stash(
        self: PdfImageTranslationRunner,
        prepared: _PreparedPdfFile,
        stashed: list[tuple[Path, Path]],
        *,
        page_backup: PdfPageRecord,
    ) -> None:
        """重生成成功：把挪开的旧译图提升为这一页的「上一版」。

        只留页图、不留整份旧 PDF——输出文件每次都是从全部页图整份重装的，留住
        页图就等于留住了那一版的输出，而且不会随文件大小翻倍占盘。
        只留紧邻的一版：上一轮留下的那份在这里被顶替掉。

        提升失败（改名报错）时退回旧行为把它删掉：宁可这一页没有「上一版」，
        也不能让记录指着一个不存在的文件。
        """
        base_dir = prepared.translated_pages_dir
        page_number = int(page_backup.page_number)
        wanted = str(page_backup.translated_image_path or "")
        # 一页正常只有一份译图；真出现多份（模型换过输出格式）时，只有记录指着的
        # 那一份才是「上一版」，其余是残留，跟着删掉。
        keeper = next((moved for moved, original in stashed if str(original) == wanted), None)
        if keeper is None and len(stashed) == 1:
            keeper = stashed[0][0]
        promoted = ""
        for moved, original in stashed:
            if moved is not keeper:
                try:
                    moved.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001 - 残留文件不影响产物。
                    _pit.logger.debug(f"[PDF] 删除挪开的旧译图失败：{moved}：{exc!r}")
                continue
            target = base_dir / f"{_pit.PDF_PAGE_IMAGE_PREVIOUS_PREFIX}{original.name}"
            try:
                target.unlink(missing_ok=True)
                _pit.os.replace(moved, target)
                promoted = str(target)
            except Exception as exc:  # noqa: BLE001 - 留不住上一版不该让重生成失败。
                _pit.logger.debug(f"[PDF] 保留上一版译图失败：{moved}：{exc!r}")
                try:
                    moved.unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001
                    _pit.logger.debug(f"[PDF] 删除挪开的旧译图失败：{moved}：{cleanup_exc!r}")
        page = next(
            (item for item in prepared.record.pages if item.page_number == page_number),
            None,
        )
        if page is None:
            # 这一页的记录在重生成里被换走了：留下的「上一版」没有任何记录指着它，
            # 只会是页面存档里一份看不懂的隐藏文件。
            promoted = ""
        self._prune_previous_page_images(prepared, page_number, keep=promoted)
        if page is None:
            return
        page.previous_image_path = promoted
        page.previous_page_state = _pit._page_version_state(page_backup) if promoted else {}

    def _restore_page_image_stash(self: PdfImageTranslationRunner, stashed: list[tuple[Path, Path]]) -> None:
        """重生成失败：把挪开的旧译图原样搬回来。"""
        for stashed_path, original in stashed:
            try:
                if not stashed_path.is_file():
                    continue
                original.unlink(missing_ok=True)
                stashed_path.rename(original)
            except Exception as exc:  # noqa: BLE001 - 已经在失败路径上，不再抛新错。
                _pit.logger.debug(f"[PDF] 恢复旧译图失败：{stashed_path}：{exc!r}")

    def _prune_previous_page_images(
        self: PdfImageTranslationRunner,
        prepared: _PreparedPdfFile,
        page_number: int,
        *,
        keep: str,
    ) -> None:
        """删掉这一页除 ``keep`` 之外的所有「上一版」文件。

        后缀会变（模型换过输出格式：page_001.png → page_001.jpg），不清理的话
        同一页会留下两份「上一版」，而记录只认得其中一份，另一份成了孤儿。
        """
        base_dir = prepared.translated_pages_dir
        if not base_dir.is_dir():
            return
        page_count = max(1, int(prepared.record.page_count or 1))
        stem = _pit.page_image_name(page_number, page_count).removesuffix(".png")
        for candidate in sorted(base_dir.glob(f"{_pit.PDF_PAGE_IMAGE_PREVIOUS_PREFIX}{stem}*")):
            if str(candidate) == keep or not candidate.is_file():
                continue
            if not _pit._path_is_within(candidate, base_dir):
                continue
            try:
                candidate.unlink()
            except Exception as exc:  # noqa: BLE001 - 残留文件不影响产物。
                _pit.logger.debug(f"[PDF] 清除更早的上一版译图失败：{candidate}：{exc!r}")

    def _page_counters_snapshot(self: PdfImageTranslationRunner) -> tuple:
        with self._page_status_lock:
            return (
                self._completed_page_count,
                self._submitted_page_count,
                set(self._retrying_pages),
                set(self._retried_pages),
                set(self._recovered_pages),
                set(self._placeholder_pages),
            )

    def _restore_page_counters(self: PdfImageTranslationRunner, snapshot: tuple) -> None:
        with self._page_status_lock:
            (
                self._completed_page_count,
                self._submitted_page_count,
                retrying,
                retried,
                recovered,
                placeholder,
            ) = snapshot
            self._retrying_pages = set(retrying)
            self._retried_pages = set(retried)
            self._recovered_pages = set(recovered)
            self._placeholder_pages = set(placeholder)
            self._emit_page_status_locked()

    def _raise_if_rerun_active(self: PdfImageTranslationRunner, *, locked: bool = False) -> None:
        """Refuse a second rerun; one page at a time keeps the output coherent."""
        if locked:
            active = self._rerun_page_key
            restoring = self._restore_page_key
        else:
            with self._rerun_lock:
                active = self._rerun_page_key
                restoring = self._restore_page_key
        if active is not None:
            raise _pit.PdfPageActionError(
                f"第 {active[1]} 页正在重新生成，完成后才能操作其他页。"
            )
        if restoring is not None:
            raise _pit.PdfPageActionError(
                f"第 {restoring[1]} 页正在换回上一版，完成后才能操作其他页。"
            )

    def _restore_page_after_failed_rerun(
        self: PdfImageTranslationRunner,
        prepared: _PreparedPdfFile,
        *,
        page_number: int,
        page_backup: PdfPageRecord,
        artifact_backup: dict[str, Any],
        counters_backup: tuple,
    ) -> None:
        """单页重生成失败后把这份文件恢复到点「重新生成」之前的样子。"""
        record = prepared.record
        record.pages = [page for page in record.pages if page.page_number != page_number]
        record.pages.append(_pit.copy.deepcopy(page_backup))
        record.pages.sort(key=lambda item: item.page_number)
        _pit._restore_file_artifacts(record, artifact_backup)
        self._refresh_file_record_counts(record)
        self._restore_page_counters(counters_backup)
        self._log(
            "WARN",
            f"[{record.name}] 第 {page_number} 页重新生成失败，已保留上一版译文页和输出文件，"
            "任务结果未改动。",
        )

    def _rewrite_finished_report(self: PdfImageTranslationRunner, *, extra_elapsed_sec: float) -> None:
        """Rebuild the manifest, the report and the task-result payload."""
        output_dir = self._finished_output_dir
        if output_dir is None:
            return
        with self._page_action_lock:
            file_records = [prepared.record for prepared in self._prepared_files]
        # Wall-clock between the run ending and the user clicking 重新生成 is not
        # time this task spent working; only the rerun itself is added.
        self._finished_elapsed_sec += max(0.0, float(extra_elapsed_sec))
        summary = self._build_summary(
            output_dir=output_dir,
            started=self._finished_started_at or _pit.datetime.now(),
            file_records=file_records,
            stopped=self._finished_stopped,
            fatal_error=self._finished_fatal_error,
            elapsed_sec=self._finished_elapsed_sec,
            stop_reason=self._finished_stop_reason,
        )
        manifest_path, report_path = _pit.write_pdf_manifest_and_report(
            summary, artifact_dir=self._process_work_dir
        )
        _pit.logger.info(f"[PDF] 已更新进度记录：{manifest_path.name}")
        self._log("INFO", f"已更新翻译报告：{report_path.name}")
        self._result_patch = {
            "output_dir": str(output_dir),
            "report_path": str(report_path),
            "manifest_path": str(manifest_path),
            "file_results": [_pit._file_record_to_result(record) for record in file_records],
            "elapsed_sec": summary.elapsed_sec,
            "api_call_count": self._api_call_count,
            "issues": self._task_issues(file_records),
            "kpi": _pit._done_kpi(summary),
        }


# 放在文件末尾：先定义好上面的类，再取原模块，两边谁先被导入都不会循环导入失败。
import core.pdf_image_translation as _pit  # noqa: E402
