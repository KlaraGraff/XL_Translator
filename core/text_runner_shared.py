"""Excel 和 Word 两个运行器共用的几步：文档说明、原文语言预检、补译跳过留痕、过程记录收尾。

以前 core/task_runner.py 和 core/word_task_runner.py 各抄一份，修一边容易忘另一边。
这里只放两边逐字相同的部分，各自特有的前后步骤仍留在各自的运行器里。

这里用到的辅助函数（preflight_files、get_system_prompt、logger 等）都从运行器自己的
模块里取（`self._runner_module().名字`），不在本文件导入：和合并前是同一个对象，
测试对 core.task_runner.* / core.word_task_runner.* 打的 patch 也照样生效。
运行器在类里写 `_RUNNER_MODULE = __name__` 指明是哪个模块。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any


class TextRunnerSharedMixin:
    _RUNNER_MODULE: str = ""
    # 补译跳过留痕里的量词：Excel 是「格」，Word 是「处」。
    _COVERAGE_UNIT_NOUN: str = "格"

    def _runner_module(self) -> ModuleType:
        return sys.modules[self._RUNNER_MODULE]

    def _start_document_profile(self, engine, texts, target_lang):
        """「自动识别场景」：在后台请模型通读全部文字、写文档说明，返回 Future。"""
        m = self._runner_module()
        texts = list(texts)

        def run() -> str:
            try:
                return m.request_document_profile(
                    engine.chat,
                    texts,
                    target_language=m.get_target_lang_display(target_lang),
                    log=self._log,  # type: ignore[attr-defined]
                )
            except Exception as exc:  # noqa: BLE001 - the description is optional.
                m.logger.debug(f"文档说明请求失败：{exc!r}")
                return ""

        return m.run_in_background(run, name="document-profile")

    def _detect_source_languages(
        self,
        *,
        engine,
        file_texts,
        settings,
        system_prompt,
        target_lang,
        detect_workers,
        page_key: str,
        status_text: str,
    ) -> tuple[str, str, list[str], list[str], dict[str, Any], int]:
        """逐个文件做一次语言预检，定下源语言、系统提示词和 TM 语言对。

        返回 (源语言, 系统提示词, 认出的语言, TM 语言对, 每个文件的预检结果, 请求次数)。
        之后各自的步骤（Excel 去掉已是目标语言的格子、补译重算；Word 记下每段的候选
        语言）由运行器接着做，最后调用 `_log_source_language_summary`。
        """
        m = self._runner_module()
        runner: Any = self
        runner._queue.put(m.StatusMsg(phase_desc=status_text))

        detector_calls = {"count": 0}
        detector_lock = threading.Lock()

        def _detect_file_language(samples, detected_target):
            with detector_lock:
                detector_calls["count"] += 1
            return engine.chat(
                m.LANGUAGE_PREFLIGHT_SYSTEM_PROMPT,
                m.build_language_preflight_prompt(
                    samples,
                    target_lang=detected_target,
                ),
            )

        file_payloads = {
            str(file_item.path): texts
            for file_item, texts in zip(runner._files, file_texts)
        }
        file_language_preflights: dict[str, Any] = {}
        # 每个文件一次模型请求，按并发设置同时发出（各文件互不依赖）。每返回一个
        # 文件推一次进度、报「正在检测语言 x/y」（x 是已识别完的文件数），进度条
        # 和进度卡标题都看得出还在往前走（先报进度再报状态，标题才是这句状态）。
        detect_total = len(file_payloads)
        detect_paths = list(file_payloads)
        runner._translate_progress.report("detect", 0, detect_total)
        runner._queue.put(m.StatusMsg(phase_desc=f"状态：正在检测语言 0/{detect_total}…"))
        for file_path in detect_paths:
            runner._file_progress.emit(file_path, "active", "detect")

        def _detected(index: int, done: int) -> None:
            runner._translate_progress.report("detect", done, detect_total)
            runner._queue.put(m.StatusMsg(phase_desc=f"状态：正在检测语言 {done}/{detect_total}…"))
            runner._file_progress.emit(detect_paths[index], "waiting", "translate")

        for detected_result in m.map_concurrently(
            lambda file_path: m.preflight_files(
                {file_path: file_payloads[file_path]},
                _detect_file_language,
                target_lang=target_lang,
            ),
            detect_paths,
            max_workers=detect_workers,
            on_done=_detected,
        ):
            file_language_preflights.update(detected_result)
        detected_sources: list[str] = []
        for result in file_language_preflights.values():
            for detected in result.source_langs:
                if detected not in detected_sources:
                    detected_sources.append(detected)
        if detected_sources:
            source_lang = detected_sources[0]
            system_prompt = m.get_system_prompt(
                settings,
                target_lang=target_lang,
                source_lang=source_lang,
                page_key=page_key,
            )
        else:
            source_lang = m.AUTO_SOURCE_LANG
        # 一门语言都没认出来时 source_lang 保持 auto，不再假定是中文（#283）：
        # 假定中文会把日文里只有汉字的格子当成已是中文跳过、补译时把英文法文格子
        # 当成「不是原文」。原文语言未知时的判法见 translation_filter._is_source_script_text
        # 和 translation_coverage.looks_like_source_text。
        tm_language_pairs = m.get_tm_language_pairs(detected_sources, target_lang)
        return (
            source_lang,
            system_prompt,
            detected_sources,
            tm_language_pairs,
            file_language_preflights,
            detector_calls["count"],
        )

    def _log_source_language_summary(
        self, call_count: int, detected_sources: list[str], tm_language_pairs: list[str]
    ) -> None:
        m = self._runner_module()
        m.logger.info(
            f"自动语言预检完成：{call_count} 次请求，"
            f"实际源语言={','.join(detected_sources) or '未确定'}，"
            f"TM 语言对={','.join(tm_language_pairs) or '无'}"
        )
        self._log(  # type: ignore[attr-defined]
            "INFO",
            "已识别原文语言："
            + (
                "、".join(m.get_source_lang_display(code) for code in detected_sources)
                or "未能确定"
            ),
        )

    def _log_ignored_coverage_units(self, file_name: str, units: list) -> None:
        """按理由分组回报补译计划里被跳过（ignored）的格子 / 段落——审计批次 2 第④条。

        旧版补译日志只报 covered / source_only / ambiguous 三类计数，ignored（看起来
        已经是译文的格、不符合候选规则的格、回填关闭时被保护的公式格）从不出现在日志
        里：用户看到"补了几格"，看不到"跳过了几格、为什么跳过"，翻完对着原件发现漏译
        也无从查起。这里补上这条自查线索，语气是"如实告知"，不是"报警"——大多数
        ignored 本来就是正确判断（已经是双语的格不用再翻、用户关了回填的公式格不该
        被覆盖），只有当用户凭位置去核对、发现误判时，这条线索才用得上。

        零 ignored 时一行都不出：仓库立场是"无关提示不挂"，见 CLAUDE.md。
        """
        m = self._runner_module()
        lines, full_detail = m.format_ignored_coverage_report(
            file_name, units, unit_noun=self._COVERAGE_UNIT_NOUN
        )
        for line in lines:
            self._log("INFO", line)  # type: ignore[attr-defined]
        # 全量坐标只进 loguru 调试输出（stderr，本仓库没配文件 sink）：任务面板
        # 不按 level 过滤，发进任务日志的话会跟分组摘要一起整段刷在用户面前，
        # 反而淹掉摘要。打包用户看不到 stderr——这是取舍：全量明细服务于开发排
        # 查，用户核对靠摘要里的样例位置就够。
        if full_detail:
            m.logger.debug(full_detail)

    def _finish_process_record(
        self,
        process_artifacts: Any,
        status: str,
        *,
        elapsed_sec: float | None,
        output_dir: Path | str,
        label: str,
    ) -> bool:
        """收尾内部过程记录（放在 app data 里，不进成果文件夹）；收好了返回 True。

        写摘要或收尾失败都只记日志：过程记录不能挡住交付。
        """
        m = self._runner_module()
        runner: Any = self
        if elapsed_sec is not None:
            try:
                (process_artifacts.work_dir / "run-summary.json").write_text(
                    m.json.dumps(
                        {
                            "task_id": runner.task_id,
                            "status": status,
                            "output_dir": str(output_dir),
                            "files": len(runner._files),
                            "elapsed_sec": round(float(elapsed_sec), 3),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                m.logger.debug("{} 过程摘要写入失败：{}", label, exc)
        try:
            process_artifacts.finish(status)
            return True
        except Exception as exc:  # process evidence must never block delivery
            m.logger.warning("{} 过程记录收尾失败：{}", label, exc)
            return False
