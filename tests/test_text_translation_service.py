"""The shared text-translation wiring (core/text_translation_service.py).

CAD and the PDF in-place pages used to wire the engine, the prompt, batching
and the translation memory each on their own.  These pin what the shared
service does with a fake engine: batches and prompt parts, dropping what the
API never translated, stopping between batches, building the engine once,
and which memory language pairs it looks up.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from core.language_preflight import TranslationLanguageResult
from core.text_translation_service import TextTranslationService
from core.term_session import DocumentTermMemory
from settings import AppSettings


class FakeEngine:
    engine_name = "openai/fake"

    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.fail = set(fail)
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        with self.lock:
            self.calls.append({"texts": list(texts), "target": target_lang, "prompt": system_prompt, "source": source_lang})
        if self.fail & set(texts):
            raise ValueError("接口故障")
        return {text: f"{text}-译" for text in texts}

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        with self.lock:
            self.calls.append({"texts": list(texts), "target": target_lang, "prompt": system_prompt, "source": source_lang})
        if self.fail & set(texts):
            raise ValueError("接口故障")
        return [TranslationLanguageResult(text, f"{text}-译", source_lang="fr", target_lang=target_lang) for text in texts]

    def chat(self, system, user):
        return "建筑施工图，消防说明"


def _settings() -> AppSettings:
    settings = AppSettings(target_lang="zh")
    # The smallest batch the rate settings allow.
    settings.rate.cad.batch_items = 10
    settings.rate.cad.concurrency = 1
    settings.rate.cad.request_retry = 0
    settings.rate.cad.item_retry = 0
    return settings


def _service(engine: FakeEngine, **overrides) -> TextTranslationService:
    options = {
        "surface_key": "cad",
        "rate_surface": "cad",
        "target_lang": "zh",
        "source_lang": "fr",
        "marked_in": "cad_review",
    }
    options.update(overrides)
    with patch("core.engine_dispatcher.build_role_engine", return_value=engine):
        service = TextTranslationService(_settings(), **options)
        service.engine()
    return service


@pytest.fixture(autouse=True)
def _no_backoff():
    with patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None):
        yield


def test_batches_follow_the_rate_and_the_prompt_carries_its_parts():
    engine = FakeEngine()
    service = _service(engine)
    texts = [f"Mot {index}" for index in range(12)]
    result = service.translate(texts, glossary={"BAES": "应急照明"}, context="消防平面图")

    assert result == {text: f"{text}-译" for text in texts}
    assert sorted(len(call["texts"]) for call in engine.calls) == [2, 10]
    prompt = engine.calls[0]["prompt"]
    assert prompt.startswith(service.system_prompt())
    assert "消防平面图" in prompt
    assert prompt.endswith("固定术语（必须遵守）：\nBAES = 应急照明")
    assert {(call["target"], call["source"]) for call in engine.calls} == {("zh", "fr")}


def test_the_caller_gets_the_batch_counts():
    from core.engine_dispatcher import TranslationBatchRunStats

    calls: list[dict] = []

    def fake_translate_texts(texts, *args, **kwargs):
        calls.append(kwargs)
        return {text: f"{text}-译" for text in texts}

    stats = TranslationBatchRunStats()
    with patch("core.engine_dispatcher.translate_texts", fake_translate_texts):
        _service(FakeEngine(), surface_key="pdf").translate(["Porte"], stats=stats)
    assert calls[0]["stats"] is stats


def test_a_given_prompt_replaces_the_page_prompt():
    engine = FakeEngine()
    service = _service(engine)
    service.translate(["Porte"], prompt="只译图纸标注")
    assert engine.calls[0]["prompt"] == "只译图纸标注"


def test_items_the_api_gave_up_on_are_dropped():
    engine = FakeEngine(fail={"Cassé"})
    service = _service(engine)
    errors: list[str] = []
    dropped = service.translate(["Porte", "Cassé"], error_callback=errors.append)

    # A returned source is not a translation: CAD and the in-place PDF pages both
    # read a missing entry as "no translation" and list it for review.
    assert dropped == {"Porte": "Porte-译"}
    assert errors


def test_stop_takes_effect_between_batches():
    engine = FakeEngine()
    service = _service(engine)
    sent: list[int] = []

    def should_stop() -> bool:
        return bool(engine.calls)

    texts = [f"Mot {index}" for index in range(25)]
    result = service.translate(texts, should_stop=should_stop, progress=lambda done, total: sent.append(done))
    assert len(engine.calls) == 1
    assert sent == [10]
    # Batches never sent are not translations.
    assert set(result) == set(engine.calls[0]["texts"])


def test_translation_with_sources_reports_languages_and_drops_failures():
    engine = FakeEngine(fail={"Cassé"})
    service = _service(engine)
    result = service.translate_with_sources(["Porte", "Cassé"], glossary={"BAES": "应急照明"})
    assert result == {"Porte": ("Porte-译", "fr")}
    assert all("BAES = 应急照明" in call["prompt"] for call in engine.calls)


def test_the_engine_is_built_once_however_many_threads_ask():
    built: list[object] = []

    def build(*args, **kwargs):
        built.append(object())
        return built[-1]

    with patch("core.engine_dispatcher.build_role_engine", side_effect=build):
        service = TextTranslationService(_settings(), surface_key="cad", rate_surface="cad", target_lang="zh")
        threads = [threading.Thread(target=service.engine) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert len(built) == 1
    assert service.engine() is built[0]


def test_memory_pairs_follow_the_source_language():
    engine = FakeEngine()
    assert _service(engine, source_lang="en", target_lang="fr").memory_pairs == ["en-fr"]
    assert _service(engine, source_lang="auto", target_lang="fr").memory_pairs == ["en-fr", "zh-fr"]
    assert _service(engine, source_lang="", target_lang="zh-TW").memory_pairs == ["en-zh-TW", "fr-zh-TW"]


def test_memory_lookup_takes_the_first_hit_and_short_words_only_from_locked_entries():
    engine = FakeEngine()
    service = _service(engine, source_lang="auto", target_lang="zh", smart_mode=True)
    calls: list[tuple[tuple[str, ...], str, bool]] = []
    table = {
        ("en-zh", False): {"Door": "门", "FM": "防火门（自动）"},
        ("en-zh", True): {"FM": None},
        ("fr-zh", False): {"Mur": "墙", "FM": "x"},
        ("fr-zh", True): {"FM": "防火门"},
    }

    def lookup(texts, pair, locked_only=False):
        calls.append((tuple(texts), pair, locked_only))
        found = table.get((pair, locked_only), {})
        return {text: found.get(text) for text in texts}

    with patch("core.tm_manager.lookup_batch", side_effect=lookup):
        hits = service.memory_lookup(["Door", "Mur", "FM", "Inconnu"])

    assert hits["Door"] == "门" and hits["Mur"] == "墙" and hits["Inconnu"] is None
    # 「自动识别场景」：a short word takes only a locked entry — not the automatic
    # en-zh one, but the locked fr-zh one.
    assert hits["FM"] == "防火门"
    assert [pair for _texts, pair, locked in calls if not locked] == ["en-zh", "fr-zh"]


def test_term_memory_uses_the_page_settings_and_takes_overrides():
    engine = FakeEngine()
    service = _service(engine, source_lang="en", target_lang="fr", smart_mode=True)
    memory = service.term_memory()
    assert isinstance(memory, DocumentTermMemory)
    assert memory.lang_pairs == ["en-fr"]
    assert memory.target_lang == "fr" and memory.smart_mode is True and memory.marked_in == "cad_review"
    other = service.term_memory(target_lang="ja", lang_pairs=())
    assert other.target_lang == "ja" and other.lang_pairs == []


def test_document_profile_names_the_target_language():
    engine = FakeEngine()
    service = _service(engine, target_lang="fr")
    seen: list[str] = []
    def fake_chat(system, user):
        seen.append(system)
        return "图纸说明"

    engine.chat = fake_chat
    assert service.document_profile(["Porte coupe-feu EI 60", "Mur porteur"]) != ""
    assert "法文" in seen[0]
    service.document_profile(["Porte coupe-feu EI 60", "Mur porteur"], target_language="简体中文")
    assert "简体中文" in seen[1]
