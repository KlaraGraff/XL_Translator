"""2026-08-29 代码审计 A4-pdf 集群（高-5 / 中-27 / 低-PDF×5）的回归测试。

所有图像与审核模型调用都走假客户端，任何一条用例都不会真的打到收费 API。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from core.api_concurrency_control import handle_api_concurrency_limit
from core.api_scheduler import WeightedApiScheduler
from core.image_generation import ImageModelUnavailableError
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    SOURCE_INDEPENDENT,
    resolve_effective_model_config,
)
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    PDF_OUTPUT_STATE_COMPLETED,
    PDF_OUTPUT_STATE_STOPPED,
    PDF_PAGE_IMAGE_STASH_PREFIX,
    PDF_PAGE_STATUS_STOPPED_UNSTARTED,
    PDF_REPORT_FILENAME,
    SOURCE_TYPE_IMAGE,
    PdfFileItem,
    PdfImageTranslationRunner,
    _unstarted_page_count,
    max_page_generation_attempts,
)
from core.task_runner import DoneMsg, StoppedMsg
from settings import AppSettings
from tests.test_pdf_image_translation import (
    _FailThenPassReviewClient,
    _FakeImageClient,
    _ReviewRequestErrorClient,
    _drain_all_messages,
    _fake_pdfium_module,
    _fake_pdfium_module_by_page_count,
    _jpeg_bytes,
    _page_review_settings,
    _png_bytes,
)


class _WorkingThenUnavailableImageClient(_FakeImageClient):
    """前 ``ok_calls`` 次正常返回，之后一律「模型不可用」。

    用来还原「跑批成功交付、事后点单页重新生成时 Key 已经失效」这一幕。
    """

    def __init__(self, image_bytes: bytes, *, ok_calls: int) -> None:
        super().__init__(image_bytes)
        self.ok_calls = ok_calls
        self.calls = 0

    def generate_page(self, **kwargs):
        self.calls += 1
        if self.calls > self.ok_calls:
            raise ImageModelUnavailableError("invalid api key")
        return super().generate_page(**kwargs)


class _StopOnFirstPageImageClient(_FakeImageClient):
    """第一页跑到一半用户按下停止；后面的页已经排在队列里，一次都没开跑。

    先等到所有页都提交进执行器再按停止——不等的话「后面的页根本没入队」和
    「入队了但被中止」两种局面会随机出现，这条用例要钉的恰恰是后者。
    """

    def __init__(self, image_bytes: bytes, *, expected_submitted: int) -> None:
        super().__init__(image_bytes)
        self.calls = 0
        self.expected_submitted = expected_submitted
        self.submitted_at_stop = 0
        self.runner: PdfImageTranslationRunner | None = None

    def generate_page(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and self.runner is not None:
            deadline = time.monotonic() + 10.0
            while (
                self.runner._submitted_page_count < self.expected_submitted
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.submitted_at_stop = self.runner._submitted_page_count
            self.runner.stop()
        return super().generate_page(**kwargs)


class _RateLimitedImageClient(_FakeImageClient):
    """每一次调用都以上游限流失败，用来把限流退避那条分支跑起来。"""

    def __init__(self, image_bytes: bytes) -> None:
        super().__init__(image_bytes)
        self.calls = 0
        self.runner: PdfImageTranslationRunner | None = None

    def generate_page(self, **_kwargs):
        self.calls += 1
        if self.runner is not None:
            self.runner.stop()
        raise RuntimeError("429 Too Many Requests: rate limit reached")


class _FatalThenSlowFailureImageClient(_FakeImageClient):
    """第一次调用抛「模型不可用」，其余调用慢一拍后普通失败。

    还原「跑到一半 API Key 失效，另外几页正在飞」这一幕：致命错误是直接从
    ``_process_prepared_pages`` 抛出去的，在飞的页由执行器 ``__exit__`` 等回来，
    收敛循环再也回不到它们身上。
    """

    def __init__(self, image_bytes: bytes) -> None:
        super().__init__(image_bytes)
        self._lock = threading.Lock()
        self.calls = 0

    def generate_page(self, **_kwargs):
        with self._lock:
            self.calls += 1
            index = self.calls
        if index == 1:
            time.sleep(0.15)
            raise ImageModelUnavailableError("invalid api key")
        time.sleep(1.0)
        raise RuntimeError("上游临时抖动")


class _FormatSwitchingImageClient(_FakeImageClient):
    """前 ``png_calls`` 次返回 PNG，之后改吐 JPEG。

    还原「单页重新生成时模型换了输出格式」：新译图落成 ``xxx_en.jpg``，
    上一版 ``xxx_en.png`` 如果没人管就会变成孤儿留在用户的输出目录里。
    """

    def __init__(self, png_bytes: bytes, jpeg_bytes: bytes, *, png_calls: int) -> None:
        super().__init__(png_bytes)
        self.jpeg_bytes = jpeg_bytes
        self.png_calls = png_calls
        self.calls = 0

    def generate_page(self, **kwargs):
        self.calls += 1
        if self.calls > self.png_calls:
            self.image_bytes = self.jpeg_bytes
        return super().generate_page(**kwargs)


class _CountingLock:
    """记录被进入次数的锁，用来钉住「计数器是在锁里改的」。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enter_count = 0

    def __enter__(self):
        self._lock.acquire()
        self.enter_count += 1
        return self

    def __exit__(self, *_exc_info) -> None:
        self._lock.release()


def _await_rerun(test: unittest.TestCase, runner: PdfImageTranslationRunner) -> None:
    deadline = time.monotonic() + 20.0
    while runner.page_rerun_state()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    test.assertFalse(
        runner.page_rerun_state()["active"],
        "单页重新生成没有在预期时间内结束。",
    )


class AuditPdfHighAndMediumTests(unittest.TestCase):
    def test_failed_page_rerun_keeps_the_delivered_pdf_and_the_old_page_image(self) -> None:
        """高-5：单页重生成失败，已交付的 PDF 一个字节都不许动。

        旧实现是「先删旧产物再重建」，重建被 ``ImageModelUnavailableError``
        跳过之后，整份高清/压缩 PDF 就从磁盘上消失了，任务中心还指着已删文件。
        现在的约定是「新产物写成功了才原子替换」，失败时整份文件原样不动。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            image_client = _WorkingThenUnavailableImageClient(
                _png_bytes(1200, 1600),
                ok_calls=2,
            )
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 2})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

                record = runner._prepared_files[0].record
                self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
                translated_pdf = Path(record.translated_pdf_path)
                self.assertTrue(translated_pdf.is_file())
                pdf_bytes_before = translated_pdf.read_bytes()
                compressed_pdf = (
                    Path(record.compressed_pdf_path) if record.compressed_pdf_path else None
                )
                compressed_bytes_before = (
                    compressed_pdf.read_bytes()
                    if compressed_pdf is not None and compressed_pdf.is_file()
                    else None
                )
                page = next(item for item in record.pages if item.page_number == 1)
                page_image = Path(page.translated_image_path)
                page_bytes_before = page_image.read_bytes()
                pages_before = len(record.pages)
                _drain_all_messages(runner)

                self.assertTrue(runner.can_rerun_pages())
                runner.rerun_page(relative_path="source.pdf", page_number=1)
                _await_rerun(self, runner)

            state = runner.page_rerun_state()
            # 失败要如实说出来，而不是悄悄留下一份被删空的输出目录。
            self.assertTrue(state["error"])
            self.assertIn("API Key", state["error"])

            # 交付产物：还在原地，内容一致。
            self.assertTrue(translated_pdf.is_file())
            self.assertEqual(translated_pdf.read_bytes(), pdf_bytes_before)
            self.assertEqual(record.translated_pdf_path, str(translated_pdf))
            if compressed_bytes_before is not None:
                self.assertTrue(compressed_pdf.is_file())
                self.assertEqual(compressed_pdf.read_bytes(), compressed_bytes_before)

            # 记录：状态、页数、这一页的译图都回到点「重新生成」之前。
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertEqual(len(record.pages), pages_before)
            restored = next(item for item in record.pages if item.page_number == 1)
            self.assertEqual(restored.status, "success")
            self.assertEqual(restored.translated_image_path, str(page_image))
            self.assertTrue(page_image.is_file())
            self.assertEqual(page_image.read_bytes(), page_bytes_before)

            # 挪开旧译图用的临时名不许留在页面存档里：续译的 glob 认不出它，
            # 但用户打开目录会看到一堆看不懂的隐藏文件。
            stash_leftovers = [
                item.name
                for item in page_image.parent.iterdir()
                if item.name.startswith(PDF_PAGE_IMAGE_STASH_PREFIX)
            ]
            self.assertEqual(stash_leftovers, [])

    def test_stopped_queued_pages_do_not_become_placeholder_pages(self) -> None:
        """中-27：中止时排着队没开跑的页，不许被烧成「图像生成失败」占位页。

        旧实现把这些零模型调用的页写成占位页塞进交付 PDF，任务还报「已完成、
        未截断」。现在它们退回「未开始」：不进交付产物，报告如实报截断。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            image_client = _StopOnFirstPageImageClient(
                _png_bytes(1200, 1600),
                expected_submitted=3,
            )
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=3)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 3})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            # 前提：三页全部提交进了执行器，中止时另外两页确实「已入队未开跑」。
            self.assertEqual(image_client.submitted_at_stop, 3)
            # 只有第一页真的调用了模型，另外两页在队列里被取消。
            self.assertEqual(image_client.calls, 1)

            record = runner._prepared_files[0].record
            self.assertEqual(len(record.pages), 1)
            self.assertEqual(record.pages[0].page_number, 1)
            self.assertFalse(any(page.placeholder for page in record.pages))
            self.assertEqual(record.placeholder_page_count, 0)
            self.assertEqual(_unstarted_page_count(record), 2)

            # 三页里只跑出一页，交付产物一个都不该有。
            self.assertEqual(record.status, PDF_OUTPUT_STATE_STOPPED)
            self.assertEqual(record.translated_pdf_path, "")
            self.assertEqual(record.compressed_pdf_path, "")
            output_dir = runner._finished_output_dir
            self.assertIsNotNone(output_dir)
            # 目录里只有原样复制过去的源文件，没有任何译文产物。
            self.assertEqual(
                sorted(path.name for path in output_dir.glob("*.pdf")),
                ["source.pdf"],
            )

            # 任务级结论：是「已停止」，不是「已完成」。
            messages = _drain_all_messages(runner)
            self.assertTrue(any(isinstance(msg, StoppedMsg) for msg in messages))
            self.assertFalse(any(isinstance(msg, DoneMsg) for msg in messages))

            manifest = json.loads(
                (output_dir / PDF_MANIFEST_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["placeholder_page_count"], 0)
            report = (output_dir / PDF_REPORT_FILENAME).read_text(encoding="utf-8")
            self.assertIn("未开始页面：2", report)


class AuditPdfLowSeverityTests(unittest.TestCase):
    def test_review_error_keeps_the_model_image_format_in_the_page_name(self) -> None:
        """低-PDF(a)：审核异常保留候选图时，扩展名要跟着实际格式走。

        这条分支此前一直沿用循环开头写死的 ``.png``，模型返回 JPEG 时就会写出
        一张「名叫 png 的 jpg」，装订和预览都得另猜格式。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "images"
            source_dir.mkdir()
            source_image = source_dir / "diagram.png"
            source_image.write_bytes(_png_bytes(1200, 1600))
            output_dir = root / "out"
            settings = AppSettings(target_lang="en")
            settings.pdf.target_lang = "en"
            settings.pdf.review_enabled = True
            settings.image_model_role.source_role = SOURCE_INDEPENDENT
            settings.image_model_role.cloud_provider = "custom_openai"
            settings.image_model_role.cloud_model = "image-model"
            settings.image_model_role.cloud_base_url = "https://images.example/v1"
            settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
            settings.pdf_review_model_role.cloud_provider = "custom_openai"
            settings.pdf_review_model_role.cloud_model = "vision-review-model"
            settings.pdf_review_model_role.cloud_base_url = "https://images.example/v1"
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(
                        path=source_image,
                        name="diagram",
                        size_kb=1.0,
                        page_count=1,
                        source_type=SOURCE_TYPE_IMAGE,
                    )
                ],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_jpeg_bytes(1200, 1600)),
                review_client=_ReviewRequestErrorClient(),
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                prepared = runner._prepare_pdf_files(output_dir=output_dir, app_managed=True)[0]
                runner._total_page_count = 1
                runner._process_prepared_pages(
                    [prepared],
                    max_attempts=max_page_generation_attempts(0),
                    scheduler=WeightedApiScheduler(1),
                    review_scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=resolve_effective_model_config(
                        settings,
                        ROLE_PDF_REVIEW,
                    ),
                    concurrency=1,
                    total_pages=1,
                )
                runner._finalize_file_record(prepared, should_assemble=True)

            record = prepared.record
            page = record.pages[0]
            self.assertEqual(page.review_status, "failed")
            self.assertEqual(page.status, "success")
            page_image = Path(page.translated_image_path)
            self.assertEqual(page_image.suffix, ".jpg")
            self.assertTrue(page_image.is_file())
            with Image.open(page_image) as image:
                self.assertEqual(image.format, "JPEG")
            self.assertTrue(record.translated_image_path.endswith(".jpg"))
            self.assertEqual(record.translated_image_format, "JPEG")

    def test_review_pass_clears_the_previous_round_blocking_issues(self) -> None:
        """低-PDF(b)：这一轮审核通过后，上一轮的 blocking issue 必须清干净。

        不清的话面板会同时显示「通过」和上一轮那句问题描述，同一行自相矛盾。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            output_dir = root / "out"
            settings = _page_review_settings(root)
            settings.pdf.review_enabled = True
            settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
            settings.pdf_review_model_role.cloud_provider = "custom_openai"
            settings.pdf_review_model_role.cloud_model = "vision-review-model"
            settings.pdf_review_model_role.cloud_base_url = "https://images.example/v1"
            review_client = _FailThenPassReviewClient()
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                review_client=review_client,
                task_logger_enabled=False,
            )

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
                "core.model_roles.get_key",
                return_value="secret",
            ):
                prepared = runner._prepare_pdf_files(output_dir=output_dir, app_managed=True)[0]
                runner._total_page_count = 1
                runner._process_prepared_pages(
                    [prepared],
                    max_attempts=max_page_generation_attempts(3),
                    scheduler=WeightedApiScheduler(1),
                    review_scheduler=WeightedApiScheduler(1),
                    model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
                    review_model_config=resolve_effective_model_config(
                        settings,
                        ROLE_PDF_REVIEW,
                    ),
                    concurrency=1,
                    total_pages=1,
                )
                runner._finalize_file_record(prepared, should_assemble=True)

            runner._prepared_files = [prepared]
            page = prepared.record.pages[0]
            self.assertEqual(review_client.calls, 2)
            self.assertEqual(page.review_status, "passed")
            # 第一轮那条「编号标签误译」不许还挂在通过的页上。
            self.assertEqual(page.review_issues, [])
            snapshot = runner.pdf_page_snapshot()
            page_entry = snapshot["files"][0]["pages"][0]
            self.assertNotIn("编号标签误译", json.dumps(page_entry, ensure_ascii=False))

    def test_page_counters_are_incremented_under_the_counter_lock(self) -> None:
        """低-PDF(c)：三个统计计数器由工作线程自增，必须在锁里改。

        ``+=`` 是读—加—写三步，并发跑页时丢计数是必然的，报告里的「接口调用
        次数」会比实际少。
        """
        settings = AppSettings(target_lang="en")
        runner = PdfImageTranslationRunner([], settings, task_logger_enabled=False)
        counting_lock = _CountingLock()
        runner._counter_lock = counting_lock

        runner._record_api_call()
        runner._record_review_api_call()
        runner._record_rate_limit_reduction("接口反馈请求过于频繁，已自动放慢发送速度。")

        self.assertEqual(counting_lock.enter_count, 3)
        self.assertEqual(runner._api_call_count, 1)
        self.assertEqual(runner._review_api_call_count, 1)
        self.assertEqual(runner._rate_limit_reduction_count, 1)

        # 并发自增之后总数必须精确，不许少。
        runner._counter_lock = threading.Lock()
        runner._api_call_count = 0
        runner._review_api_call_count = 0

        def hammer() -> None:
            for _ in range(2000):
                runner._record_api_call()
                runner._record_review_api_call()

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(runner._api_call_count, 8 * 2000)
        self.assertEqual(runner._review_api_call_count, 8 * 2000)

    def test_rate_limit_backoff_gets_the_stop_flag(self) -> None:
        """低-PDF(d)：限流退避里睡的是整整 30 秒，必须把停止标志交给它。

        不交的话用户点了停止之后每一页都要先把这一觉睡完才肯回来，停止响应被
        拖成几十秒一页。
        """
        captured: dict[str, object] = {}

        def spy(exc, **kwargs):
            captured.update(kwargs)
            return handle_api_concurrency_limit(exc, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            image_client = _RateLimitedImageClient(_png_bytes(1200, 1600))
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=1)],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )
            image_client.runner = runner

            with patch.dict(sys.modules, {"pypdfium2": _fake_pdfium_module()}), patch(
                "core.model_roles.get_key",
                return_value="secret",
            ), patch(
                "core.pdf_image_translation.handle_api_concurrency_limit",
                side_effect=spy,
            ):
                started = time.monotonic()
                runner._run()
                elapsed = time.monotonic() - started

        self.assertIn("should_stop", captured)
        should_stop = captured["should_stop"]
        self.assertTrue(callable(should_stop))
        # 传进去的必须是这个 runner 自己的停止标志：客户端在抛限流之前已经
        # 按了停止，所以它现在就该是 True。
        self.assertTrue(should_stop())
        # 停止之后不许还在退避里睡着：整轮跑批远快于一次 30 秒的退避。
        self.assertLess(elapsed, 10.0)

    def test_process_file_dead_code_is_gone(self) -> None:
        """低-PDF(e)：``_process_file`` 是生产死代码，判定也已与生产分支漂移。

        留着它等于留一份没人跑、却被 6 个用例当真的第二套判定；用例已改指
        ``_process_prepared_pages`` + ``_finalize_file_record`` 这条真实路径。
        """
        self.assertFalse(hasattr(PdfImageTranslationRunner, "_process_file"))


class AuditPdfSecondRoundTests(unittest.TestCase):
    """对抗审查开出的两条整改：事务化重生成的产物残留、致命错误路上的状态外泄。"""

    def test_page_rerun_drops_the_compressed_pdf_it_could_not_rebuild(self) -> None:
        """R1：单页重生成这一轮没能重建压缩版时，上一版压缩 PDF 不许留下冒充产物。

        事务化之后不再「先删旧产物再重建」，于是没被本轮重写的旧产物会原地留下，
        而且继续挂在 ``record.compressed_pdf_path`` 上。用户在任务中心看到一份
        「已完成」的任务、两个下载入口，其中压缩版还是重生成之前的旧内容。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=2)],
                settings,
                source_root=root,
                image_client=_FakeImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            real_assemble = PdfImageTranslationRunner._assemble_translated_pdf
            fail_compressed = {"on": False}

            def assemble(self, record, output_pdf, *, compressed=False):
                if compressed and fail_compressed["on"]:
                    raise RuntimeError("磁盘写满")
                return real_assemble(self, record, output_pdf, compressed=compressed)

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 2})},
            ), patch("core.model_roles.get_key", return_value="secret"), patch.object(
                PdfImageTranslationRunner,
                "_assemble_translated_pdf",
                assemble,
            ):
                runner._run()

                record = runner._prepared_files[0].record
                self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
                compressed_pdf = Path(record.compressed_pdf_path)
                self.assertTrue(compressed_pdf.is_file())
                # 打上可辨认的旧字节：重生成之后它要么被换掉、要么被清掉，
                # 绝不能原样留下还挂在 record 上。
                stale_bytes = b"%PDF-1.4\n% stale compressed output\n"
                compressed_pdf.write_bytes(stale_bytes)
                _drain_all_messages(runner)

                fail_compressed["on"] = True
                self.assertTrue(runner.can_rerun_pages())
                runner.rerun_page(relative_path="source.pdf", page_number=1)
                _await_rerun(self, runner)

            # 本轮重生成本身是成功的：高清版换成了含新第 1 页的版本。
            self.assertEqual(runner.page_rerun_state()["error"], "")
            self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
            self.assertTrue(Path(record.translated_pdf_path).is_file())
            # 压缩版这一轮没产出：记录里不许再指着它，磁盘上也不许留着旧的那份。
            self.assertEqual(record.compressed_pdf_path, "")
            self.assertEqual(record.compressed_pdf_size_bytes, 0)
            self.assertTrue(record.compression_error)
            self.assertFalse(compressed_pdf.is_file())

    def test_image_rerun_removes_the_superseded_output_of_another_format(self) -> None:
        """R1（图片源）：换了输出格式的重生成，旧后缀那份译图不许留成孤儿。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "images"
            source_dir.mkdir()
            source_image = source_dir / "diagram.png"
            source_image.write_bytes(_png_bytes(1200, 1600))
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            image_client = _FormatSwitchingImageClient(
                _png_bytes(1200, 1600),
                _jpeg_bytes(1200, 1600),
                png_calls=1,
            )
            runner = PdfImageTranslationRunner(
                [
                    PdfFileItem(
                        path=source_image,
                        name="diagram",
                        size_kb=1.0,
                        page_count=1,
                        source_type=SOURCE_TYPE_IMAGE,
                    )
                ],
                settings,
                source_root=root,
                image_client=image_client,
                task_logger_enabled=False,
            )

            with patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

                record = runner._prepared_files[0].record
                self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
                first_output = Path(record.translated_image_path)
                self.assertEqual(first_output.suffix, ".png")
                self.assertTrue(first_output.is_file())
                _drain_all_messages(runner)

                self.assertTrue(runner.can_rerun_pages())
                runner.rerun_page(relative_path=record.relative_path, page_number=1)
                _await_rerun(self, runner)

            self.assertEqual(runner.page_rerun_state()["error"], "")
            second_output = Path(record.translated_image_path)
            self.assertEqual(second_output.suffix, ".jpg")
            self.assertTrue(second_output.is_file())
            # 上一版 .png 是被本轮取代的产物，不许留在输出目录里当第二份「译图」。
            self.assertFalse(first_output.is_file())

    def test_fatal_model_error_does_not_leak_the_stopped_page_status(self) -> None:
        """R2：致命模型错误抬出去时，在飞页的内部中止状态不许留在记录和清单里。

        ``stopped_unstarted`` 只是「撤回这条页记录」的内部信号。它一旦跟着致命
        错误留在 ``record.pages`` 里，就会被写进清单、送进逐页面板——前端不认识
        这个字符串，会把一张连译文图都没有的页显示成「已跑完 · 未审核」，报告的
        「未开始页面」也漏计。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            settings = _page_review_settings(root)
            settings.pdf.page_retry_attempts = 0
            settings.pdf.page_generation_concurrency = 3
            runner = PdfImageTranslationRunner(
                [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=3)],
                settings,
                source_root=root,
                image_client=_FatalThenSlowFailureImageClient(_png_bytes(1200, 1600)),
                task_logger_enabled=False,
            )

            with patch.dict(
                sys.modules,
                {"pypdfium2": _fake_pdfium_module_by_page_count({"source.pdf": 3})},
            ), patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

            record = runner._prepared_files[0].record
            leaked = [
                page.page_number
                for page in record.pages
                if page.status == PDF_PAGE_STATUS_STOPPED_UNSTARTED
            ]
            self.assertEqual(leaked, [])
            # 被撤回的两页回到「未开始」，报告口径才对得上。
            self.assertEqual(_unstarted_page_count(record), 2)

            output_dir = runner._finished_output_dir
            self.assertIsNotNone(output_dir)
            manifest_text = (output_dir / PDF_MANIFEST_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn(PDF_PAGE_STATUS_STOPPED_UNSTARTED, manifest_text)
            snapshot_text = json.dumps(runner.pdf_page_snapshot(), ensure_ascii=False)
            self.assertNotIn(PDF_PAGE_STATUS_STOPPED_UNSTARTED, snapshot_text)


class AuditPdfThirdRoundTests(unittest.TestCase):
    """R3：图片源单页重生成的输出路径必须「解析一次、之后原子覆盖」。

    ``resolve_translated_image_path`` 是有副作用的：app 托管目录下它会先把已交付
    的旧译图改名成 ``_R1`` 再返回基名。事务化重生成之后，这一步成了整条链路上
    唯一一个「先动磁盘、再写新产物」的动作——失败时回滚只还原记录字段，还不回
    文件名。
    """

    def _image_runner(
        self,
        root: Path,
        image_client,
    ) -> tuple[PdfImageTranslationRunner, Path]:
        source_dir = root / "images"
        source_dir.mkdir(exist_ok=True)
        source_image = source_dir / "diagram.png"
        source_image.write_bytes(_png_bytes(1200, 1600))
        settings = _page_review_settings(root)
        settings.pdf.page_retry_attempts = 0
        runner = PdfImageTranslationRunner(
            [
                PdfFileItem(
                    path=source_image,
                    name="diagram",
                    size_kb=1.0,
                    page_count=1,
                    source_type=SOURCE_TYPE_IMAGE,
                )
            ],
            settings,
            source_root=root,
            image_client=image_client,
            task_logger_enabled=False,
        )
        return runner, source_image

    def test_same_format_image_rerun_keeps_the_delivered_file_name(self) -> None:
        """同格式重生成：交付文件名不许漂，输出目录里不许多出 ``_R1``。

        旧实现每次重生成都重新走一遍 ``resolve_translated_image_path``：它先把
        ``diagram_英文.png`` 改名成 ``diagram_英文_R1.png``，再让新译图落到
        ``_R2``、``_R3``……用户原来那份译图连名字都没了，被挪走的旧版还永久留在
        输出目录里当孤儿。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, _ = self._image_runner(root, _FakeImageClient(_png_bytes(1200, 1600)))

            with patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

                record = runner._prepared_files[0].record
                self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
                delivered = Path(record.translated_image_path)
                self.assertTrue(delivered.is_file())
                _drain_all_messages(runner)

                for _ in range(2):
                    self.assertTrue(runner.can_rerun_pages())
                    runner.rerun_page(relative_path=record.relative_path, page_number=1)
                    _await_rerun(self, runner)
                    self.assertEqual(runner.page_rerun_state()["error"], "")

            # 交付文件名恒定：用户收藏夹里的那条路径不会因为重生成而失效。
            self.assertEqual(record.translated_image_path, str(delivered))
            self.assertTrue(delivered.is_file())
            siblings = sorted(
                path.name
                for path in delivered.parent.iterdir()
                if path.is_file() and path.name.startswith(delivered.stem)
            )
            self.assertEqual(siblings, [delivered.name])

    def test_failed_image_rerun_keeps_the_delivered_translated_image(self) -> None:
        """重生成中途写盘失败：记录指向的译图必须还在，字节一个都没变。

        旧实现在写新产物之前就把旧译图改名挪走了，回滚只还原 ``record`` 字段：
        任务显示「已完成」，下载入口却指着一个磁盘上已经不存在的文件。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner, _ = self._image_runner(root, _FakeImageClient(_png_bytes(1200, 1600)))

            real_copy2 = shutil.copy2

            def copy2(src, dst, *args, **kwargs):
                if Path(dst).name.endswith(".building"):
                    raise OSError("磁盘写满")
                return real_copy2(src, dst, *args, **kwargs)

            with patch("core.model_roles.get_key", return_value="secret"):
                runner._run()

                record = runner._prepared_files[0].record
                self.assertEqual(record.status, PDF_OUTPUT_STATE_COMPLETED)
                delivered = Path(record.translated_image_path)
                delivered_bytes = delivered.read_bytes()
                _drain_all_messages(runner)

                with patch("core.pdf_image_translation.shutil.copy2", copy2):
                    self.assertTrue(runner.can_rerun_pages())
                    runner.rerun_page(relative_path=record.relative_path, page_number=1)
                    _await_rerun(self, runner)

            # 失败要如实说出来。
            self.assertTrue(runner.page_rerun_state()["error"])
            # 而记录指向的译图必须仍然可下载，且内容还是上一版。
            self.assertEqual(record.translated_image_path, str(delivered))
            self.assertTrue(delivered.is_file())
            self.assertEqual(delivered.read_bytes(), delivered_bytes)
            strays = sorted(
                path.name
                for path in delivered.parent.iterdir()
                if path.is_file() and path.name.startswith(f"{delivered.stem}_R")
            )
            self.assertEqual(strays, [])


if __name__ == "__main__":
    unittest.main()
