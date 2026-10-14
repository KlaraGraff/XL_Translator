"""文字翻译服务：一个页面的执行器与文档翻译模型之间的那段共用接线。

CAD 的 ``build_runner`` 和 PDF「原位替换」的 ``_inplace_hooks`` 以前各写一遍同样的事：
按「翻译参数」取速率与重试 → 建引擎 → 拼系统提示词（补充要求、文档说明、固定术语）→
``translate_texts`` 分批请求（调度、限流降速、拆小重试、逐批日志）→ 记忆库整段复用与
词条附批（``core.term_session``）。这里把它收成一个对象，页面只说自己要什么：

    service = TextTranslationService(settings, surface_key="cad", rate_surface="cad",
                                     target_lang=..., source_lang=..., ...)
    service.translate(texts, glossary=..., context=..., term_session=...)

以后新增一种文件类型（例如只负责「取字 / 写回」的格式适配器）就用它接上记忆库、
重试和日志，不必再抄一遍。Excel / Word 的执行器把这些内联在各自的 ``_run()`` 里，
暂不改用。

调用都经 ``engine_dispatcher.<函数>`` 取，测试替换 ``core.engine_dispatcher`` 里的函数
（``translate_texts``、``build_role_engine`` …）时这里跟着生效。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any

from core import engine_dispatcher, tm_manager
from core.document_profile import append_profile, is_context_sensitive, request_document_profile
from core.language_registry import get_target_lang_display
from core.surface_rate import SurfaceRate, engine_settings_for_rate, resolve_surface_rate
from core.term_session import DocumentTermMemory
from settings import AppSettings

#: 程序判断不了原文语种时，记忆库按这几种原文语言去查。
_AUTO_MEMORY_SOURCES = ("en", "fr", "zh")


class TextTranslationService:
    """一个任务里文字翻译的共用部分：引擎、提示词、分批请求、记忆库。

    ``surface_key`` 是页面键（``cad``、``ext:<id>``），决定补充要求和「自动识别场景」读哪
    一页的设置；``rate_surface`` 是「翻译参数」里速率与重试的那一组（``cad``、
    ``pdf_text`` …）。速率在建对象时取定，任务中途改设置不影响已开始的任务。
    """

    def __init__(
        self,
        settings: AppSettings,
        *,
        surface_key: str,
        rate_surface: str,
        target_lang: str,
        source_lang: str = "auto",
        connection_ids: Iterable[str] = (),
        api_scheduler: Any = None,
        marked_in: str = "",
        smart_mode: bool | None = None,
        default_prompt: str = "",
    ) -> None:
        self.settings = settings
        self.surface_key = surface_key
        self.target_lang = target_lang
        self.source_lang = source_lang
        self.connection_ids = tuple(connection_ids)
        self.api_scheduler = api_scheduler
        #: 未采用设定译法时复核标记记在哪里（见 ``DocumentTermMemory.marked_in``）。
        self.marked_in = marked_in
        #: 运行时注册的类型的内置规则默认值（清单的 ``prompt``）；用户改过的优先，见
        #: ``engine_dispatcher.core_prompt_for``。
        self.default_prompt = default_prompt
        #: 「自动识别场景」：短词只认用户定下的记忆，翻译前可先写一段文档说明。
        self.smart_mode = (
            engine_dispatcher.smart_context_enabled(settings, surface_key)
            if smart_mode is None
            else bool(smart_mode)
        )
        self.rate: SurfaceRate = resolve_surface_rate(settings, rate_surface)
        self._engine: Any = None
        self._engine_built = False
        self._engine_lock = threading.Lock()
        self._system_prompt: str | None = None

    # ── 引擎与提示词 ──────────────────────────────────────────────────────

    def engine(self) -> Any:
        """文档翻译引擎，第一次用到时才建（之后同一个）。

        在任务线程里第一次调用时，拿到的是任务开始时的密钥快照；需要在开始任务时就
        暴露连接问题的页面（CAD）在建执行器时先调用一次。
        """
        with self._engine_lock:
            if not self._engine_built:
                self._engine = engine_dispatcher.build_role_engine(
                    engine_settings_for_rate(self.settings, self.rate),
                    "translation",
                    connection_ids=self.connection_ids,
                )
                self._engine_built = True
            return self._engine

    def system_prompt(self) -> str:
        """内置规则 + 这一页选的补充要求 + 目标语言说明（不含文档说明和固定术语）。"""
        if self._system_prompt is None:
            self._system_prompt = engine_dispatcher.get_system_prompt(
                self.settings,
                target_lang=self.target_lang,
                source_lang=self.source_lang,
                page_key=self.surface_key,
                default_core_prompt=self.default_prompt,
            )
        return self._system_prompt

    def _prompt(self, prompt: str | None, glossary: dict[str, str] | None, context: str) -> str:
        text = append_profile(self.system_prompt() if prompt is None else prompt, context)
        if glossary:
            text += "\n固定术语（必须遵守）：\n" + "\n".join(f"{k} = {v}" for k, v in glossary.items())
        return text

    def _engine_for(self, term_session: Any) -> Any:
        # 记忆库：本批出现的词条随批附上，翻完核对，未采用的重译（core.term_engine）。
        return term_session.wrap(self.engine()) if term_session is not None else self.engine()

    # ── 翻译 ──────────────────────────────────────────────────────────────

    def translate(
        self,
        texts: list[str],
        *,
        prompt: str | None = None,
        glossary: dict[str, str] | None = None,
        context: str = "",
        term_session: Any = None,
        progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        error_callback: Callable[[str], None] | None = None,
        request_callback: Callable[[Any], None] | None = None,
    ) -> dict[str, str]:
        """分批翻译，返回 ``{原文: 译文}``。

        ``prompt`` 不给时用 ``system_prompt()``；``context`` 是「自动识别场景」的文档说明，
        ``glossary`` 是必须遵守的固定术语，都接在提示词后面。

        接口放弃的批次、停止时没发出去的批次，``translate_texts`` 按原文占位交回。那不是
        译文，这里把它们摘掉，调用方据此记为「未获得译文」、标需复核（CAD 与 PDF 原位替换）。
        """
        stats = engine_dispatcher.TranslationBatchRunStats()
        results = engine_dispatcher.translate_texts(
            texts,
            self._engine_for(term_session),
            self.target_lang,
            self._prompt(prompt, glossary, context),
            self.rate.batch_items,
            self.rate.concurrency,
            source_lang=self.source_lang,
            api_scheduler=self.api_scheduler,
            # 逐批回调：进度条跟着批次走，而不是停住再跳。
            progress_callback=progress,
            # 停止在批次之间生效，不必等全部请求发完。
            should_stop=should_stop,
            # 拆小重试、整批放弃、限流降速这些情况写进运行日志。
            error_callback=error_callback,
            # 每批请求的发出与返回：写进运行日志。
            request_callback=request_callback,
            stats=stats,
            throughput_unlocked=bool(self.settings.engine.concurrency_unlocked),
            # 接口临时故障时同一批的重发次数（翻译参数 → 失败重试次数）。
            transient_retries=self.rate.request_retry,
            max_chars=self.rate.batch_chars,
            item_retries=self.rate.item_retry,
        )
        for text in stats.untranslated_sources | stats.stopped_sources:
            if results.get(text) == text:
                results.pop(text, None)
        return results

    def translate_with_sources(
        self,
        texts: list[str],
        *,
        glossary: dict[str, str] | None = None,
        context: str = "",
        term_session: Any = None,
        progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        error_callback: Callable[[str], None] | None = None,
    ) -> dict[str, tuple[str, str]]:
        """程序判断不了语种的文字：翻译的同时让模型回报原文语种，返回 ``{原文: (译文, 语种)}``。

        接口放弃或停止时没发出去的条目按原文占位交回，那不是译文，也不是语种判断，不返回。
        """
        stats = engine_dispatcher.TranslationBatchRunStats()
        results = engine_dispatcher.translate_texts_with_sources(
            texts,
            self._engine_for(term_session),
            self.target_lang,
            self._prompt(None, glossary, context),
            self.rate.batch_items,
            self.rate.concurrency,
            progress,
            error_callback,
            should_stop=should_stop,
            api_scheduler=self.api_scheduler,
            stats=stats,
            throughput_unlocked=bool(self.settings.engine.concurrency_unlocked),
            transient_retries=self.rate.request_retry,
            max_chars=self.rate.batch_chars,
            item_retries=self.rate.item_retry,
        )
        failed = stats.untranslated_sources | stats.stopped_sources
        return {text: (item.translation, item.source_lang) for text, item in results.items() if text not in failed}

    def document_profile(
        self,
        texts: list[str],
        *,
        target_language: str | None = None,
        log: Callable[[str, str], None] | None = None,
        neighbor_rows: Iterable[str] = (),
    ) -> str:
        """「自动识别场景」：读一遍文字，让模型写一段文档说明；失败时为空。"""
        return request_document_profile(
            self.engine().chat,
            texts,
            target_language=(
                get_target_lang_display(self.target_lang) if target_language is None else target_language
            ),
            log=log,
            neighbor_rows=neighbor_rows,
        )

    # ── 记忆库 ────────────────────────────────────────────────────────────

    @property
    def memory_pairs(self) -> list[str]:
        """记忆库按哪些语言对查：选了原文语言就是那一对；自动识别时按常见原文语言逐一查。"""
        source_pair = str(self.source_lang or "").strip()
        if source_pair and source_pair != "auto":
            return [f"{source_pair}-{self.target_lang}"]
        target_base = str(self.target_lang or "").split("-", 1)[0].lower()
        # 原文和译文同一种语言的组合查不出东西（英译英），不必去查。
        return [f"{candidate}-{self.target_lang}" for candidate in _AUTO_MEMORY_SOURCES if candidate != target_base]

    def memory_lookup(self, texts: list[str]) -> dict[str, str | None]:
        """整段查记忆库，按 ``memory_pairs`` 的顺序，先查到的为准。"""
        hits: dict[str, str | None] = {text: None for text in texts}
        remaining = list(texts)
        for pair in self.memory_pairs:
            if not remaining:
                break
            current = tm_manager.lookup_batch(remaining, pair)
            if self.smart_mode:
                # 「自动识别场景」：「甲」「FM」这类短词只认用户定下的记忆，自动存下的可能来自别的场景。
                short = [text for text in remaining if is_context_sensitive(text)]
                if short:
                    current.update(tm_manager.lookup_batch(short, pair, locked_only=True))
            for text, value in current.items():
                if value and hits.get(text) in (None, ""):
                    hits[text] = value
            remaining = [text for text in remaining if not hits.get(text)]
        return hits

    def term_memory(
        self,
        *,
        target_lang: str | None = None,
        lang_pairs: Iterable[str] | None = None,
        **extra: Any,
    ) -> DocumentTermMemory:
        """记忆库会话的工厂（与 Excel / Word 同一套规则：整段复用、句中采用设定译法、
        按备注场景选用、核对后重译、自动保存短词）。``extra`` 原样交给 ``DocumentTermMemory``。"""
        return DocumentTermMemory(
            self.settings.tm,
            target_lang=self.target_lang if target_lang is None else target_lang,
            lang_pairs=self.memory_pairs if lang_pairs is None else lang_pairs,
            smart_mode=self.smart_mode,
            engine_provider=self.engine,
            marked_in=self.marked_in,
            **extra,
        )
