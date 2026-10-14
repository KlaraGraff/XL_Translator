"""The shared format-adapter executor (core/adapter_runner.py) with a fake engine.

``tests/fixtures/adapters/echo`` gives one unit per line (``#!`` lines locked,
``{{name}}`` protected).  These pin what every adapter gets from the app: the
memory is used before the model, batches follow the rate, units the model
never translated keep their source and land in the review list, protected
fragments come back unchanged, stop and pause act between files, a failing
``write`` fails only its file, and the end message carries the counts.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.surfaces import unregister_surface
from api.surfaces.adapter import AdapterSurface
from api.surfaces.base import RunnerRequest
from api.task_contracts import TaskOptions
from config import CORE_TRANSLATION_PROMPT
from core import tm_manager
from core.adapter_runner import REVIEW_LIST_NAME, mask_protected, unmask_protected
from core.format_adapter import AdapterError, InProcessAdapterHost, load_manifest
from core.language_preflight import LANGUAGE_PREFLIGHT_SYSTEM_PROMPT, TranslationLanguageResult
from core.surface_rate import resolve_surface_rate
from core.task_runner_messages import DoneMsg, ErrorMsg, StoppedMsg
from settings import AppSettings, ExtensionSettings
from tests.app_data_isolation import IsolatedAppDataTestCase
from tests.test_format_adapter import ECHO_DIR, ECHO_KEY, register_echo


class FakeEngine:
    engine_name = "openai/fake"

    def __init__(self, *, fail: set[str] = frozenset(), drop_tokens: bool = False, delay: float = 0.0) -> None:
        self.fail = set(fail)
        self.drop_tokens = drop_tokens
        self.delay = delay
        self.calls: list[list[str]] = []
        self.prompts: list[str] = []
        self.chats: list[str] = []
        self.lock = threading.Lock()

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        with self.lock:
            self.calls.append(list(texts))
            self.prompts.append(system_prompt)
        if self.delay:
            time.sleep(self.delay)
        if self.fail & set(texts):
            raise ValueError("接口故障")
        out = {}
        for text in texts:
            value = f"[fr] {text}"
            if self.drop_tokens:
                value = value.replace("⟦1⟧", "")
            out[text] = value
        return out

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        return [TranslationLanguageResult(text, f"[fr] {text}", source_lang="en", target_lang=target_lang) for text in texts]

    def chat(self, system, user):
        with self.lock:
            self.chats.append(system)
        if system == LANGUAGE_PREFLIGHT_SYSTEM_PROMPT:
            return '{"source_langs": ["en"]}'
        return "测试文档"

    @property
    def sent(self) -> list[str]:
        return [text for batch in self.calls for text in batch]


def _item(path: Path) -> Any:
    return type("Item", (), {"path": path, "name": path.name, "size_kb": 0.1})()


def _drain(runner, timeout: float = 20.0) -> list[Any]:
    messages = []
    deadline = time.time() + timeout
    while runner.needs_poll():
        if time.time() > deadline:
            raise TimeoutError("runner did not finish")
        message = runner.get_message(timeout=0.05)
        if message is not None:
            messages.append(message)
    return messages


class AdapterRunnerTests(IsolatedAppDataTestCase):
    def setUp(self) -> None:
        super().setUp()
        tm_manager.init_db()
        self.source = self.app_data_dir.parent / "docs"
        self.source.mkdir(parents=True)
        self.manifest = load_manifest(ECHO_DIR)
        self.host = InProcessAdapterHost(ECHO_DIR, self.manifest)
        self.surface = AdapterSurface(self.manifest, self.host)
        patcher = patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _file(self, name: str, body: str) -> Path:
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _settings(self, **ext: Any) -> AppSettings:
        settings = AppSettings(target_lang="fr")
        # 「自动识别场景」默认开：这里关掉，免得每个测试多一次通读请求。
        settings.extensions["echo"] = ExtensionSettings(smart_context=False, **ext)
        return settings

    def _run(
        self,
        engine: FakeEngine,
        paths: list[Path],
        *,
        settings: AppSettings | None = None,
        options: dict | None = None,
        source_lang: str = "en",
        before: Any = None,
    ):
        settings = settings or self._settings()
        with patch("core.engine_dispatcher.build_role_engine", return_value=engine):
            runner = self.surface.build_runner(
                RunnerRequest(
                    files=[_item(path) for path in paths],
                    settings=settings,
                    source_root=self.source,
                    options=TaskOptions(options=options or {}),
                    source_lang=source_lang,
                    key_overrides={},
                )
            )
            if before is not None:
                before(runner)
            runner.start()
            messages = _drain(runner)
        return runner, messages

    @staticmethod
    def _end(messages: list[Any]) -> Any:
        return next(message for message in reversed(messages) if isinstance(message, (DoneMsg, ErrorMsg, StoppedMsg)))

    # ── basics ───────────────────────────────────────────────────────────

    def test_core_rules_come_from_the_settings_then_the_manifest_then_the_app(self) -> None:
        path = self._file("a.txt", "Hello\n")
        engine = FakeEngine()
        self._run(engine, [path])
        assert engine.prompts[0].startswith(CORE_TRANSLATION_PROMPT)

        manifest = self.manifest.model_copy(update={"prompt": "逐行翻译回声文本。"})
        self.surface = AdapterSurface(manifest, InProcessAdapterHost(ECHO_DIR, manifest))
        engine = FakeEngine()
        self._run(engine, [path])
        assert engine.prompts[0].startswith("逐行翻译回声文本。")
        assert CORE_TRANSLATION_PROMPT not in engine.prompts[0]

        engine = FakeEngine()
        self._run(engine, [path], settings=self._settings(prompt="只译英文行。"))
        assert engine.prompts[0].startswith("只译英文行。")
        assert "逐行翻译回声文本。" not in engine.prompts[0]

    def test_translates_writes_and_counts(self) -> None:
        path = self._file("a.txt", "#! keep me\nHello\n\nWorld\nHello\n")
        engine = FakeEngine()
        _runner, messages = self._run(engine, [path])
        done = self._end(messages)
        assert isinstance(done, DoneMsg), done
        # Locked lines never reach the model; the same text is sent once.
        assert engine.sent == ["Hello", "World"]
        result = done.file_results[0]
        assert result["success"] and result["status"] == "completed"
        out = Path(result["output_path"])
        assert out.name == "a_法文.txt"
        assert out.read_text(encoding="utf-8") == "#! keep me\n[fr] Hello\n\n[fr] World\n[fr] Hello\n"
        assert done.api_call_count == 1
        assert done.tm_hit_count == 0
        assert done.review == {"total_count": 0}
        assert not (Path(done.output_dir) / REVIEW_LIST_NAME).exists()

    def test_bilingual_switch_writes_source_then_translation(self) -> None:
        path = self._file("a.txt", "Hello\n#! code\n")
        _runner, messages = self._run(FakeEngine(), [path], options={"bilingual": True})
        done = self._end(messages)
        out = Path(done.file_results[0]["output_path"])
        assert out.name == "a_法文_双语.txt"
        assert out.read_text(encoding="utf-8") == "Hello\n[fr] Hello\n#! code\n"

    def test_adapter_switches_reach_write(self) -> None:
        path = self._file("a.txt", "Hello\n")
        _runner, messages = self._run(FakeEngine(), [path], options={"upper": True})
        assert Path(self._end(messages).file_results[0]["output_path"]).read_text(encoding="utf-8") == "[FR] HELLO\n"

    def test_memory_hits_skip_the_model(self) -> None:
        tm_manager.insert_batch([("Hello", "Bonjour")], "en-fr", 500, "test")
        path = self._file("a.txt", "Hello\nWorld\n")
        engine = FakeEngine()
        _runner, messages = self._run(engine, [path])
        done = self._end(messages)
        assert engine.sent == ["World"]
        assert done.tm_hit_count == 1
        assert Path(done.file_results[0]["output_path"]).read_text(encoding="utf-8") == "Bonjour\n[fr] World\n"
        assert done.file_results[0]["memory_count"] == 1

    def test_batches_follow_the_rate(self) -> None:
        lines = [f"Line {index}" for index in range(45)]
        path = self._file("a.txt", "\n".join(lines) + "\n")
        engine = FakeEngine()
        settings = self._settings()
        # 运行时注册的类型没有自己的一组速率：按文档翻译模型的吞吐量取。
        batch = resolve_surface_rate(settings, ECHO_KEY).batch_items
        _runner, messages = self._run(engine, [path], settings=settings)
        assert sorted(len(call) for call in engine.calls) == sorted(
            [batch] * (45 // batch) + ([45 % batch] if 45 % batch else [])
        )
        assert self._end(messages).api_call_count == len(engine.calls)

    def test_auto_source_language_is_detected_once_per_file(self) -> None:
        first = self._file("a.txt", "Hello\n")
        second = self._file("b.txt", "World\n")
        engine = FakeEngine()
        _runner, messages = self._run(engine, [first, second], source_lang="auto")
        done = self._end(messages)
        assert engine.chats.count(LANGUAGE_PREFLIGHT_SYSTEM_PROMPT) == 2
        assert done.language == {"detected": {"a.txt": ["en"], "b.txt": ["en"]}}
        # Two preflights + one batch per file.
        assert done.api_call_count == 4

    # ── review ───────────────────────────────────────────────────────────

    def test_units_without_a_translation_keep_their_source_and_are_listed(self) -> None:
        path = self._file("a.txt", "Hello\nBroken\n")
        engine = FakeEngine(fail={"Broken"})
        _runner, messages = self._run(engine, [path])
        done = self._end(messages)
        assert isinstance(done, DoneMsg)
        result = done.file_results[0]
        assert result["status"] == "needs_review" and result["review_count"] == 1
        assert Path(result["output_path"]).read_text(encoding="utf-8") == "[fr] Hello\nBroken\n"
        assert done.review["total_count"] == 1
        listing = Path(done.review["report_path"]).read_text(encoding="utf-8-sig")
        assert Path(done.review["report_path"]).name == REVIEW_LIST_NAME
        assert "a.txt\tline 2\tBroken\t未获得译文" in listing
        # The task center's result table reads these rows.
        assert done.review["items"] == [
            {
                "file": "a.txt",
                "location": "line 2",
                "excerpt": "Broken",
                "issue": "未获得译文",
                "action": "已保留原文",
                "severity": "warning",
            }
        ]

    def test_residual_chinese_keeps_the_translation_and_is_listed(self) -> None:
        path = self._file("a.txt", "设备清单\n")

        class Leaky(FakeEngine):
            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                self.calls.append(list(texts))
                return {text: "Liste des 设备" for text in texts}

        _runner, messages = self._run(Leaky(), [path], source_lang="zh")
        done = self._end(messages)
        result = done.file_results[0]
        assert result["review_count"] == 1
        assert Path(result["output_path"]).read_text(encoding="utf-8") == "Liste des 设备\n"
        assert "译文中仍有原文" in Path(done.review["report_path"]).read_text(encoding="utf-8-sig")
        assert done.review["items"][0]["action"] == "已保留译文"

    def test_protected_fragments_come_back_unchanged(self) -> None:
        path = self._file("a.txt", "Hello {{name}}, see {{link}}\n")
        engine = FakeEngine()
        _runner, messages = self._run(engine, [path])
        assert engine.sent == ["Hello ⟦1⟧, see ⟦2⟧"]
        assert "⟦1⟧" in engine.prompts[0]
        out = Path(self._end(messages).file_results[0]["output_path"])
        assert out.read_text(encoding="utf-8") == "[fr] Hello {{name}}, see {{link}}\n"

    def test_a_lost_protected_fragment_keeps_the_source(self) -> None:
        path = self._file("a.txt", "Hello {{name}}\n")
        _runner, messages = self._run(FakeEngine(drop_tokens=True), [path])
        done = self._end(messages)
        assert Path(done.file_results[0]["output_path"]).read_text(encoding="utf-8") == "Hello {{name}}\n"
        assert done.file_results[0]["review_count"] == 1
        assert "代码或链接" in Path(done.review["report_path"]).read_text(encoding="utf-8-sig")

    # ── failures, stop and pause ─────────────────────────────────────────

    def test_a_failing_write_fails_only_that_file(self) -> None:
        first = self._file("a.txt", "Hello\n")
        second = self._file("b.txt", "World\n")
        original = self.host.write

        def write(path, units, mode, out_path, options):
            if Path(path).name == "a.txt":
                raise AdapterError("磁盘已满")
            return original(path, units, mode, out_path, options)

        with patch.object(self.host, "write", side_effect=write):
            _runner, messages = self._run(FakeEngine(), [first, second])
        done = self._end(messages)
        assert isinstance(done, DoneMsg)
        assert [(item["name"], item["success"]) for item in done.file_results] == [("a.txt", False), ("b.txt", True)]
        assert "磁盘已满" in done.file_results[0]["error"]

    def test_all_files_failing_is_an_error(self) -> None:
        path = self.source / "bad.txt"
        path.write_bytes(b"\xff\xfe\x00")
        _runner, messages = self._run(FakeEngine(), [path])
        error = self._end(messages)
        assert isinstance(error, ErrorMsg)
        assert error.message == "无法读取此文件。"

    def test_stop_takes_effect_between_files(self) -> None:
        first = self._file("a.txt", "Hello\n")
        second = self._file("b.txt", "World\n")
        engine = FakeEngine()
        original = self.host.write

        def write_then_stop(runner):
            def write(*args):
                result = original(*args)
                runner.stop()
                return result

            patcher = patch.object(self.host, "write", side_effect=write)
            patcher.start()
            self.addCleanup(patcher.stop)

        _runner, messages = self._run(engine, [first, second], before=write_then_stop)
        stopped = self._end(messages)
        assert isinstance(stopped, StoppedMsg)
        assert [(item["name"], item["status"]) for item in stopped.files] == [("a.txt", "completed"), ("b.txt", "unstarted")]
        assert engine.sent == ["Hello"]

    def test_pause_holds_the_next_file_until_resume(self) -> None:
        first = self._file("a.txt", "Hello\n")
        second = self._file("b.txt", "World\n")
        engine = FakeEngine()
        original = self.host.write
        holder: dict[str, Any] = {}

        def write(*args):
            result = original(*args)
            holder["runner"].pause()
            return result

        with patch.object(self.host, "write", side_effect=write), patch(
            "core.engine_dispatcher.build_role_engine", return_value=engine
        ):
            runner = self.surface.build_runner(
                RunnerRequest(
                    files=[_item(first), _item(second)],
                    settings=self._settings(),
                    source_root=self.source,
                    options=TaskOptions(),
                    source_lang="en",
                    key_overrides={},
                )
            )
            holder["runner"] = runner
            runner.start()
            time.sleep(0.6)
            assert engine.sent == ["Hello"]
            assert runner.is_running()
            runner.resume()
            messages = _drain(runner)
        assert isinstance(self._end(messages), DoneMsg)
        assert engine.sent == ["Hello", "World"]


def test_mask_and_unmask_protected_fragments():
    masked, found = mask_protected("Run `a` then `a` and see http://x", ["`a`", "http://x", "absent"])
    assert masked == "Run ⟦1⟧ then ⟦2⟧ and see ⟦3⟧"
    assert found == ["`a`", "`a`", "http://x"]
    assert unmask_protected("Lancez ⟦1⟧ puis ⟦ 2 ⟧, voir ⟦3⟧", found) == "Lancez `a` puis `a`, voir http://x"
    assert unmask_protected("Lancez ⟦1⟧ puis ⟦1⟧", found) is None
    assert unmask_protected("Lancez ⟦1⟧ ⟦2⟧ ⟦3⟧ ⟦4⟧", found) is None
    # Source that already holds the brackets is not masked.
    assert mask_protected("⟦x⟧ `a`", ["`a`"]) == ("⟦x⟧ `a`", [])


class AdapterTaskRouteTests(IsolatedAppDataTestCase):
    """The echo adapter through the real routes, start to finish."""

    def setUp(self) -> None:
        super().setUp()
        register_echo()
        self.addCleanup(unregister_surface, ECHO_KEY)
        self.source = self.app_data_dir.parent / "notes"
        self.source.mkdir(parents=True)
        (self.source / "a.txt").write_text("Hello\n#! raw\nWorld\n", encoding="utf-8")
        for patcher in (
            patch("core.engine_dispatcher.build_role_engine", return_value=FakeEngine()),
            patch("api.surfaces.adapter.require_translation_model", lambda settings: None),
            patch("core.engine_dispatcher._backoff_sleep", lambda *args, **kwargs: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_task_runs_to_the_end(self) -> None:
        with TestClient(create_app()) as client:
            started = client.post(
                "/api/tasks",
                json={
                    "surface": ECHO_KEY,
                    "source_path": str(self.source),
                    "target_lang": "fr",
                    "source_lang": "en",
                    "options": {"bilingual": True},
                },
            )
            assert started.status_code == 202, started.text
            task_id = started.json()["task_id"]
            deadline = time.time() + 20
            while True:
                status = client.get(f"/api/tasks/{task_id}").json()
                if status.get("terminal") or time.time() > deadline:
                    break
                time.sleep(0.05)
        assert status["state"] == "done", status
        assert status["task_snapshot"]["options"] == {"bilingual": True, "upper": False}
        assert status["task_snapshot"]["adapter"]["id"] == "echo"
        result = status["result"]["file_results"][0]
        assert result["success"] is True
        assert Path(result["output_path"]).name == "a_法文_双语.txt"
        assert Path(result["output_path"]).read_text(encoding="utf-8") == "Hello\n[fr] Hello\n#! raw\nWorld\n[fr] World\n"
        assert [entry["state"] for entry in status["file_progress"]["files"]] == ["generated"]
        assert status["progress"]["overall_percent"] == 100.0
