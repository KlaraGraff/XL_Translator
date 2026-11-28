"""段落重排（BabelDOC）：一键安装、翻译子进程、逐页检查、接进 PDF「智能混合」、扩展页「PDF 组件」大类。

BabelDOC 和它几百兆的字体、版面模型不在测试环境里：子进程换成一个按同样协议回话的假程序，
安装换成假的下载源。真下载、真排版由 scripts/component_install_smoke.py 在 macOS / Windows 上跑，
真模型翻真样例在 Mac 上验证。
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from core import babeldoc_component as component
from core import babeldoc_component_install as installer
from core.component_download import RemoteFile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSLATOR_APP_DATA_DIR", str(tmp_path / "app-data"))
    numpy_site = tmp_path / "pdf-component" / "site-packages"
    numpy_site.mkdir(parents=True)
    monkeypatch.setattr(component, "numpy_site_dir", lambda: numpy_site)
    return tmp_path


# ── 小工具 ────────────────────────────────────────────────────────────────


def test_page_ranges_use_babeldoc_notation():
    assert component.page_ranges([7, 1, 2, 3, 3]) == "1-3,7"
    assert component.page_ranges([5]) == "5"
    assert component.page_ranges([2, 4, 5, 6, 9, 10]) == "2,4-6,9-10"
    assert component.page_ranges([]) == ""


def test_language_codes_pick_the_right_font_family():
    # BabelDOC 按代码里的 CN / TW / HK / KR 选字体：只写 zh、ko 会落到西文字体
    assert component.babeldoc_lang("zh") == "zh-CN"
    assert component.babeldoc_lang("zh_hans") == "zh-CN"
    assert component.babeldoc_lang("zh-TW") == "zh-TW"
    assert component.babeldoc_lang("zh-Hant") == "zh-TW"
    assert component.babeldoc_lang("zh-HK") == "zh-HK"
    assert component.babeldoc_lang("ko") == "ko-KR"
    assert component.babeldoc_lang("ja") == "ja"
    assert component.babeldoc_lang("fr") == "fr"
    assert component.babeldoc_lang("") == "en"
    assert component.babeldoc_lang("auto") == "en"


def test_literal_rules_name_the_values_models_like_to_rewrite():
    for word in ("number", "date", "code", "country", "14/6/24", "NLD"):
        assert word in component.LITERAL_RULES


# ── 一键安装 ──────────────────────────────────────────────────────────────


def _wheel(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _remote(name: str, data: bytes, sha256: str | None = None) -> RemoteFile:
    return RemoteFile(name, (f"https://primary.test/{name}", f"https://mirror.test/{name}"),
                      len(data), sha256 or hashlib.sha256(data).hexdigest())


def _fake_sources(monkeypatch, *, model_hash: str | None = None, runtime: bytes | None = None) -> list[str]:
    """假的 PyPI 和资源下载源；返回收到的请求地址。"""
    wheels = {
        "babeldoc-test.whl": _wheel({"babeldoc/__init__.py": b"__version__ = '0.7.1'\n",
                                     "babeldoc-0.7.1.dist-info/LICENSE": b"AGPL-3.0"}),
        "dependency-test.whl": _wheel({"dependency/__init__.py": b""}),
    }
    font, model = b"font bytes", b"layout model bytes"
    wheel_files = tuple(_remote(name, data) for name, data in wheels.items())
    runtime_files: tuple[RemoteFile, ...] = ()
    if runtime is not None:
        from tests.test_ocr_component import _conda

        wheels["vc14_runtime-test.conda"] = _conda({name: runtime + name.encode() for name in installer.WINDOWS_RUNTIME})
        runtime_files = (_remote("vc14_runtime-test.conda", wheels["vc14_runtime-test.conda"]),)
    assets = [("fonts", _remote("SourceHanSans.ttf", font)),
              ("models", _remote("doclayout.onnx", model, model_hash))]
    monkeypatch.setattr(installer, "wheel_files", lambda: wheel_files)
    monkeypatch.setattr(installer, "runtime_files", lambda: runtime_files)
    monkeypatch.setattr(installer, "asset_files", lambda: assets)
    routes = {f"https://primary.test/{name}": data for name, data in wheels.items()}
    routes["https://primary.test/SourceHanSans.ttf"] = font
    routes["https://primary.test/doclayout.onnx"] = model
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        value = routes.get(str(request.url))
        return httpx.Response(200, content=value) if value is not None else httpx.Response(404)

    monkeypatch.setattr(installer, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_install_unpacks_checks_and_records_the_component(hermetic, monkeypatch):
    _fake_sources(monkeypatch)
    probes: list[tuple[Path, Path, Path]] = []

    def probe(site_dir, home, numpy_site):
        probes.append((site_dir, home, numpy_site))
        return True, "0.7.1", ""

    monkeypatch.setattr(component, "probe_in_subprocess", probe)
    phases: list[str] = []
    info = installer.install(progress=lambda phase, done, total: phases.append(phase))
    assert info["installed"] is True and info["babeldoc"] == "0.7.1" and info["outdated"] is False
    site_dir, home = installer.installed_dirs()
    assert (site_dir / "babeldoc" / "__init__.py").is_file()
    # 字体、模型放在 BabelDOC 找它们的地方：HOME/.cache/babeldoc/<子目录>/
    assert (home / ".cache" / "babeldoc" / "fonts" / "SourceHanSans.ttf").read_bytes() == b"font bytes"
    assert (home / ".cache" / "babeldoc" / "models" / "doclayout.onnx").is_file()
    assert probes == [(site_dir, home, component.numpy_site_dir())]
    assert phases[0] == "download" and phases[-1] == "done" and "verify" in phases
    assert not (installer.component_root() / ".downloads").exists()
    assert component.component_paths() == (site_dir, home, component.numpy_site_dir())

    again = installer.install()
    assert again["path"] != info["path"] and not Path(info["path"]).exists()
    installer.uninstall()
    assert installer.installed_dirs() is None and component.component_paths() is None


def test_a_failed_trial_run_keeps_the_downloads_for_the_retry(hermetic, monkeypatch):
    seen = _fake_sources(monkeypatch)
    monkeypatch.setattr(component, "probe_in_subprocess", lambda *args: (False, "", "ImportError: boom"))
    with pytest.raises(installer.ComponentInstallError, match="无法在本机运行"):
        installer.install()
    assert installer.installed_dirs() is None
    assert [path for path in installer.component_root().iterdir() if path.name != ".downloads"] == []
    first = len(seen)
    # 重试：已核对过的 wheel 和几百兆的资源都不再下载
    monkeypatch.setattr(component, "probe_in_subprocess", lambda *args: (True, "0.7.1", ""))
    installer.install()
    assert len(seen) == first
    assert installer.installed_dirs() is not None


def test_a_file_that_does_not_match_leaves_nothing_behind(hermetic, monkeypatch):
    _fake_sources(monkeypatch, model_hash="0" * 64)
    monkeypatch.setattr(component, "probe_in_subprocess", lambda *args: (True, "0.7.1", ""))
    with pytest.raises(installer.ComponentInstallError):
        installer.install()
    assert installer.installed_dirs() is None
    assert not list(installer.component_root().glob(f"{installer.MANIFEST_VERSION}-*"))


def test_windows_gets_the_vc_runtime_next_to_onnxruntime(hermetic, monkeypatch):
    # 版面模型靠 ONNX Runtime 跑；Windows 上它要的 VC++ 运行库和文字识别组件一样放进 onnxruntime/capi。
    _fake_sources(monkeypatch, runtime=b"runtime:")
    monkeypatch.setattr(component, "probe_in_subprocess", lambda *args: (True, "0.7.1", ""))
    installer.install()
    site_dir, _home = installer.installed_dirs()
    for name in installer.WINDOWS_RUNTIME:
        assert (site_dir / "onnxruntime" / "capi" / name).read_bytes() == b"runtime:" + name.encode()


def test_only_windows_downloads_the_vc_runtime(monkeypatch):
    monkeypatch.setattr(installer, "platform_key", lambda: "windows-x64")
    assert installer.runtime_files() == (installer.WINDOWS_RUNTIME_PACKAGE,)
    assert installer.WINDOWS_RUNTIME_PACKAGE in installer.manifest_files()
    for key in ("macos-arm64", "linux-x64"):
        monkeypatch.setattr(installer, "platform_key", lambda key=key: key)
        assert installer.runtime_files() == ()


def test_install_needs_the_pdf_component(hermetic, monkeypatch):
    _fake_sources(monkeypatch)
    monkeypatch.setattr(component, "numpy_site_dir", lambda: None)
    with pytest.raises(installer.ComponentInstallError, match="PDF 处理组件"):
        installer.install()


def test_install_reports_unsupported_platforms(hermetic, monkeypatch):
    monkeypatch.setattr(installer, "wheel_files", lambda: ())
    assert installer.manifest_files() == () and installer.download_size() == 0
    with pytest.raises(installer.ComponentInstallError, match="不支持"):
        installer.install()


def test_not_enough_disk_space_is_said_before_downloading(hermetic, monkeypatch):
    seen = _fake_sources(monkeypatch)
    monkeypatch.setattr(installer.shutil, "disk_usage", lambda _path: type("U", (), {"free": 10})())
    with pytest.raises(installer.ComponentInstallError, match="磁盘空间不足"):
        installer.install()
    assert seen == []


def test_installed_record_cannot_point_outside_the_component_folder(hermetic):
    installer._write_record({"version": installer.MANIFEST_VERSION, "dir": "../elsewhere"})
    assert installer.installed_dirs() is None
    assert installer.installed_info()["installed"] is False


def test_manifest_pins_every_platform_we_ship():
    from core.babeldoc_component_manifest import ASSETS, BABELDOC_VERSION, WHEELS

    assert BABELDOC_VERSION == "0.7.1"
    assert set(WHEELS) == {"macos-arm64", "windows-x64", "linux-x64"}
    for key, files in WHEELS.items():
        names = [item.name.lower() for item in files]
        assert sum(name.startswith("babeldoc-0.7.1-") for name in names) == 1, key
        # NumPy、OpenCV、PyMuPDF 用「PDF 处理组件」里的那份，不再下载
        assert not [name for name in names if name.startswith(("numpy-", "opencv", "pymupdf-"))], key
        for item in files:
            assert item.size > 0 and len(item.sha256) == 64 and item.urls, item.name
    assert len(ASSETS) == 182
    by_name = {name: (size, sha) for _folder, name, size, sha, _sha3, _urls in ASSETS}
    model = [name for name in by_name if name.endswith(".onnx")]
    assert model and by_name[model[0]] == (
        75324598, "fece9af02f618b603ff7921ccec6861d13e7e1f9830e091dfb7e8ad9311e5b21")


def test_download_size_is_about_five_hundred_megabytes(monkeypatch):
    monkeypatch.setattr(installer, "platform_key", lambda: "macos-arm64")
    assert 420 * 1024 ** 2 < installer.download_size() < 560 * 1024 ** 2


# ── 翻译子进程 ─────────────────────────────────────────────────────────────


def test_worker_stops_reading_babeldoc_events_at_finish():
    # BabelDOC 发出 finish 后，事件流要过一会儿才结束；这时再要下一个事件会一直等下去
    # （GitHub 上 macOS、Windows 真装时试排就卡在这里，十分钟超时）。
    import asyncio

    class Sent:
        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

    async def events():
        yield {"type": "progress_update", "stage": "Parse", "overall_progress": 10.0}
        yield {"type": "progress_end", "stage": "Typesetting", "overall_progress": 100.0}
        yield {"type": "finish", "translate_result": "result"}
        await asyncio.Event().wait()  # 不会再有事件，也不结束

    async def run(sent):
        return await asyncio.wait_for(component._consume(events(), sent), timeout=5)

    sent = Sent()
    assert asyncio.run(run(sent)) == "result"
    assert [message["progress"] for message in sent.messages] == [10.0, 100.0]


def test_worker_reports_babeldoc_errors():
    import asyncio

    async def events():
        yield {"type": "error", "error": "layout model failed"}

    with pytest.raises(RuntimeError, match="layout model failed"):
        asyncio.run(component._consume(events(), type("P", (), {"send": lambda self, message: None})()))


# 与 core.babeldoc_component.worker_main 同一套协议的假 BabelDOC。custom_prompt 决定它怎么表现：
# crash（报完版本就退出）、error（报错）、hang（不回话，等着被停）。
_FAKE_WORKER = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    job = json.loads(Path(sys.argv[-1]).read_text(encoding="utf-8"))
    mode = job.get("custom_prompt") or ""
    def send(message):
        sys.stdout.write(json.dumps(message) + "\\n"); sys.stdout.flush()
    print("native library banner")  # 不是 JSON 的行要跳过
    send({"ready": True, "version": "0.7.1-test"})
    if mode == "crash":
        sys.exit(3)
    if mode == "error":
        print("Traceback: layout model failed", file=sys.stderr)
        send({"error": "RuntimeError: layout failed"}); sys.exit(1)
    if mode == "hang":
        time.sleep(60); sys.exit(0)
    for index, text in enumerate(["Hello", "World"]):
        send({"call": {"id": index, "kind": "llm", "text": text}})
    send({"progress": 50.0})
    answers = {}
    for _ in range(2):
        reply = json.loads(sys.stdin.readline())
        answers[reply["id"]] = reply.get("text") or "ERR:" + str(reply.get("error"))
    out = Path(job["output_dir"]); out.mkdir(parents=True, exist_ok=True)
    mono = out / "source.no_watermark.zh-CN.mono.pdf"
    mono.write_text(json.dumps({"answers": answers, "job": job}), encoding="utf-8")
    send({"progress": 100.0})
    send({"done": {"mono": str(mono), "version": "0.7.1-test"}})
''')


@pytest.fixture
def fake_worker(hermetic, monkeypatch):
    script = hermetic / "fake_babeldoc.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    calls: list[list[str]] = []

    def command(*args: str) -> list[str]:
        calls.append(list(args))
        return [sys.executable, str(script), *args]

    monkeypatch.setattr(component, "_sidecar_command", command)
    paths = (hermetic / "bd-site", hermetic / "bd-home", hermetic / "numpy-site")
    return calls, paths


def _translate(kind: str, text: str) -> str:
    if text == "World":
        raise RuntimeError("model said no")
    return f"{kind}:{text.upper()}"


_PIPES_CHILD = textwrap.dedent('''
    import json, multiprocessing, os, stat, sys
    sys.path.insert(0, sys.argv[1])
    from core.babeldoc_component import _Calls, _Protocol

    def child(queue):
        # BabelDOC 写 PDF 时另开的进程接过的标准输入，不能是收回话的管道。
        queue.put(stat.S_ISFIFO(os.fstat(0).st_mode))

    if __name__ == "__main__":
        protocol = _Protocol()
        calls = _Calls(protocol)
        print("library noise")  # 进 stderr，不混进回话通道
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=child, args=(queue,))
        process.start()
        child_stdin_is_pipe = queue.get(timeout=60)
        process.join(60)
        text = calls.call("llm", "hello")
        protocol.send({"done": {"text": text, "child_stdin_is_pipe": child_stdin_is_pipe}})
''')


def test_worker_pipes_stay_private_to_the_worker(tmp_path):
    script = tmp_path / "pipes_child.py"
    script.write_text(_PIPES_CHILD, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), str(ROOT)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stdin is not None
    call = json.loads(proc.stdout.readline())["call"]
    assert call["kind"] == "llm" and call["text"] == "hello"
    proc.stdin.write((json.dumps({"id": call["id"], "text": "你好"}) + "\n").encode("utf-8"))
    proc.stdin.flush()
    done = json.loads(proc.stdout.readline())["done"]
    proc.stdin.close()
    assert proc.stderr is not None
    err = proc.stderr.read()
    proc.wait(timeout=60)
    assert done == {"text": "你好", "child_stdin_is_pipe": False}
    assert b"library noise" in err


def test_translation_requests_go_through_our_engine(fake_worker, tmp_path):
    calls, paths = fake_worker
    progress: list[float] = []
    result = component.translate_pdf(
        tmp_path / "source.pdf", tmp_path / "work", pages=[5, 1, 2], lang_in="en", lang_out="zh",
        translate=_translate, workers=2, custom_prompt="Copy numbers.", glossary=[("passport", "护照")],
        progress=progress.append, paths=paths,
    )
    assert result.ok and result.version == "0.7.1-test", result
    assert result.requests == 2 and result.failed_requests == 1
    written = json.loads(Path(result.output_pdf).read_text(encoding="utf-8"))
    assert written["answers"] == {"0": "llm:HELLO", "1": "ERR:model said no"}
    job = written["job"]
    assert job["pages"] == "1-2,5" and job["lang_in"] == "en" and job["lang_out"] == "zh-CN"
    assert job["site_dirs"] == [str(paths[2]), str(paths[0])]  # NumPy 在前，组件目录接在后面
    assert job["home"] == str(paths[1]) and job["glossary"] == [["passport", "护照"]]
    assert job["custom_prompt"] == "Copy numbers." and job["workers"] == 2
    assert progress == [50.0, 100.0]
    assert calls == [[component.WORKER_FLAG, str(tmp_path / "work" / "job.json")]]


@pytest.mark.parametrize(("mode", "message"), [
    ("crash", "段落重排组件意外退出"),
    ("error", "段落重排组件处理这份文件时出错"),
])
def test_a_failing_worker_is_one_sentence_for_the_user(fake_worker, tmp_path, mode, message):
    _calls, paths = fake_worker
    result = component.translate_pdf(tmp_path / "source.pdf", tmp_path / "work", pages=[1], lang_in="en",
                                     lang_out="zh", translate=_translate, custom_prompt=mode, paths=paths)
    assert not result.ok and result.error == message and not result.stopped
    if mode == "error":
        assert "layout failed" in result.detail


def test_stopping_the_task_ends_the_worker(fake_worker, tmp_path):
    _calls, paths = fake_worker
    started = time.monotonic()
    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    result = component.translate_pdf(tmp_path / "source.pdf", tmp_path / "work", pages=[1], lang_in="en",
                                     lang_out="zh", translate=_translate, custom_prompt="hang", paths=paths,
                                     should_stop=stop.is_set)
    assert result.stopped and not result.ok
    assert time.monotonic() - started < 20


def test_without_the_component_nothing_starts(hermetic, tmp_path):
    result = component.translate_pdf(tmp_path / "source.pdf", tmp_path / "work", pages=[1], lang_in="en",
                                     lang_out="zh", translate=_translate)
    assert not result.ok and result.error == "未安装「段落重排组件」"


def test_the_worker_entry_is_wired_into_the_sidecar():
    text = (ROOT / "scripts" / "launch_sidecar.py").read_text(encoding="utf-8")
    assert component.WORKER_FLAG in text and "babeldoc_component" in text
    # BabelDOC 排版时用 multiprocessing：打包后的程序要先认出自己是子进程
    assert "freeze_support()" in text


# ── 逐页检查 ──────────────────────────────────────────────────────────────

fitz = pytest.importorskip("fitz")

SOURCE_LINES = [
    "The applicant must present a valid passport before 14/6/24 at the counter.",
    "Section 3 describes how records dated 20120826 are kept and checked.",
]
GOOD_LINES = ["申请人必须在 14/6/24 之前到柜台出示有效护照。", "第 3 节说明日期为 20120826 的记录如何保存和核对。"]


def _pdf(path: Path, pages: list[list[tuple[str, float]]]) -> Path:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        y = 80.0
        for text, size in lines:
            cjk = any("一" <= ch <= "鿿" for ch in text)
            page.insert_text((50, y), text, fontsize=size, fontname="china-s" if cjk else "helv")
            y += max(size, 10) * 2
    doc.save(path)
    return path


def _check(tmp_path: Path, source: list[list[tuple[str, float]]], output: list[list[tuple[str, float]]],
           pages: list[int] | None = None):
    from core.pdf_babeldoc_check import check_pages

    src = _pdf(tmp_path / "source.pdf", source)
    out = _pdf(tmp_path / "output.pdf", output)
    return check_pages(src, out, pages or list(range(1, len(source) + 1)), target_lang="zh")


def _lines(texts, size=10.0):
    return [(text, size) for text in texts]


def test_a_well_translated_page_passes(tmp_path):
    verdict = _check(tmp_path, [_lines(SOURCE_LINES)], [_lines(GOOD_LINES)])[1]
    assert verdict.ok, verdict


def test_each_problem_has_its_own_reason(tmp_path):
    from core import pdf_babeldoc_check as check

    cases = {
        check.REASON_UNTRANSLATED: _lines(SOURCE_LINES),
        check.REASON_NUMBERS: _lines(["申请人必须在 2024年6月14日 之前到柜台出示有效护照。", "第三节说明这些记录如何保存和核对。"]),
        check.REASON_TINY: _lines(GOOD_LINES, size=3.0),
        check.REASON_MARKUP: _lines([GOOD_LINES[0] + " <style id='1'>", GOOD_LINES[1]]),
    }
    for reason, output in cases.items():
        verdict = _check(tmp_path, [_lines(SOURCE_LINES)], [output])[1]
        assert reason in verdict.reasons, (reason, verdict)
        assert verdict.details


def test_a_list_merged_into_one_paragraph_fails(tmp_path):
    from core.pdf_babeldoc_check import REASON_LIST

    source = _lines(["- check the passport", "- check the visa", "- check the ticket", "- check the insurance"])
    verdict = _check(tmp_path, [source], [_lines(["核对护照；核对签证；核对机票；核对保险"])])[1]
    assert REASON_LIST in verdict.reasons
    kept = _check(tmp_path, [source], [_lines(["- 核对护照", "- 核对签证", "- 核对机票", "- 核对保险"])])[1]
    assert kept.ok, kept


def test_a_page_missing_from_the_output_fails(tmp_path):
    from core.pdf_babeldoc_check import REASON_MISSING_PAGE

    verdicts = _check(tmp_path, [_lines(SOURCE_LINES), _lines(SOURCE_LINES)], [_lines(GOOD_LINES)])
    assert verdicts[1].ok and verdicts[2].reasons == [REASON_MISSING_PAGE]


def test_reasons_have_english_and_french_entries():
    from core import pdf_babeldoc_check as check
    from core.pdf_babeldoc_pages import BABELDOC_CHECK_FAILED, BABELDOC_DONE_NOTE

    catalog: dict[str, dict[str, str]] = {}
    for path in (ROOT / "ui" / "src" / "i18n" / "catalog").glob("*.json"):
        catalog.update(json.loads(path.read_text(encoding="utf-8")))
    reasons = [value for name, value in vars(check).items() if name.startswith("REASON_")]
    assert len(reasons) == 8
    for text in [*reasons, BABELDOC_CHECK_FAILED, BABELDOC_DONE_NOTE, "未安装「段落重排组件」"]:
        assert catalog.get(text, {}).get("en") and catalog[text].get("fr"), text


# ── 接进 PDF「智能混合」 ──────────────────────────────────────────────────

A4 = (595.276, 841.89)
SENTENCE = "The applicant must present a valid passport and proof of travel insurance before departure."


def _text_source(path: Path, pages: int) -> None:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=A4[0], height=A4[1])
        y = 80
        for _ in range(12):
            page.insert_text((50, y), SENTENCE, fontsize=9)
            y += 16
    doc.save(path)


def _babeldoc_output(source: Path, target: Path, good_pages: set[int]) -> None:
    """假 BabelDOC 的输出：good_pages 写成中文，其余页原样（= 漏翻）。"""
    with fitz.open(str(source)) as src:
        out = fitz.open()
        for number in range(1, src.page_count + 1):
            if number in good_pages:
                page = out.new_page(width=A4[0], height=A4[1])
                y = 80
                for _ in range(12):
                    page.insert_text((50, y), "申请人出发前必须出示有效护照和旅行保险证明。BABELDOC", fontsize=9,
                                     fontname="china-s")
                    y += 16
            else:
                out.insert_pdf(src, from_page=number - 1, to_page=number - 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        out.save(target)


class _Runs:
    """记下假 BabelDOC 收到的参数。"""

    def __init__(self, good_pages: set[int]) -> None:
        self.good_pages = good_pages
        self.calls: list[dict] = []

    def __call__(self, source, work_dir, *, pages, lang_in, lang_out, translate, workers, custom_prompt,
                 glossary, progress, should_stop, paths=None):
        self.calls.append({"pages": list(pages), "lang_in": lang_in, "lang_out": lang_out, "workers": workers,
                           "glossary": glossary, "custom_prompt": custom_prompt,
                           "reply": translate("llm", "Translate: Hello")})
        output = Path(work_dir) / "out" / "mono.pdf"
        _babeldoc_output(Path(source), output, self.good_pages)
        progress(100.0)
        return component.BabelDocResult(True, output_pdf=str(output), requests=3, version="0.7.1")


def _run_task(tmp_path: Path, *, fake_babeldoc, installed=True, chat=True, pages=2, target="zh"):
    from core.api_scheduler import WeightedApiScheduler
    from core.model_roles import ROLE_IMAGE, SOURCE_INDEPENDENT, resolve_effective_model_config
    from core.pdf_image_translation import PdfFileItem, PdfImageTranslationRunner, max_page_generation_attempts
    from core.pdf_vector import InplaceHooks
    from core.pdf_word_route import WordRouteResult
    from settings import AppSettings, PdfSettings
    from tests.test_pdf_image_translation import _png_bytes, _RecordingImageClient

    source_pdf = tmp_path / "source.pdf"
    _text_source(source_pdf, pages)
    settings = AppSettings(target_lang=target)
    settings.pdf = PdfSettings.model_validate({"target_lang": target, "translation_route": "auto"})
    settings.image_model_role.source_role = SOURCE_INDEPENDENT
    settings.image_model_role.cloud_provider = "custom_openai"
    settings.image_model_role.cloud_model = "image-model"
    settings.image_model_role.cloud_base_url = "https://images.example/v1"
    client = _RecordingImageClient(_png_bytes(2481, 3508))
    chats: list[tuple[str, str]] = []

    def fake_chat(system: str, user: str) -> str:
        chats.append((system, user))
        return "你好"

    hooks = InplaceHooks(translate=lambda texts, _prompt: {t: t for t in texts}, profile=None, tesseract=None,
                         tesseract_languages=None, source_lang="en", ocr_wanted=False,
                         chat=fake_chat if chat else None, chat_supported=lambda: True,
                         chat_system_prompt="SYSTEM", fixed_terms=lambda _path: [("passport", "护照")],
                         chat_concurrency=3)
    runner = PdfImageTranslationRunner(
        [PdfFileItem(path=source_pdf, name="source", size_kb=1.0, page_count=pages)],
        settings, source_root=tmp_path, image_client=client, task_logger_enabled=False, inplace_hooks=hooks,
    )
    logs: list[tuple[str, str]] = []
    original_log = runner._log
    runner._log = lambda level, text: (logs.append((level, text)), original_log(level, text))[1]

    word_segments: list[int] = []   # 每次转 Word 的页数

    def fake_word(pdf_path, work_dir, final_path, settings, **kwargs):
        with fitz.open(str(pdf_path)) as segment:
            word_segments.append(segment.page_count)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"docx")
        return WordRouteResult(ok=True, output_path=str(final_path))

    def fake_docx_to_pdf(_docx, pdf_path, **_kwargs):
        doc = fitz.open()
        for _ in range(word_segments[-1]):
            doc.new_page(width=A4[0], height=A4[1]).insert_text((60, 100), "SEGMENT-TRANSLATED", fontsize=12)
        doc.save(pdf_path)
        return "word"

    site = tmp_path / "bd"
    with patch("core.model_roles.get_key", return_value="secret"), \
            patch("core.pdf_word_route.word_route_available", return_value=True), \
            patch("core.pdf_word_route.office_pdf_engine", return_value=("word", "test")), \
            patch("core.pdf_word_route.translate_pdf_via_word", fake_word), \
            patch("core.pdf_word_route.docx_to_pdf", fake_docx_to_pdf), \
            patch.object(component, "component_paths", lambda: (site, site, site) if installed else None), \
            patch.object(component, "translate_pdf", fake_babeldoc):
        prepared = runner._prepare_pdf_files(output_dir=tmp_path / "out", app_managed=True)[0]
        runner._run_babeldoc_pages([prepared], total_pages=pages)
        runner._run_word_segments([prepared])
        runner._total_page_count = pages
        runner._process_prepared_pages(
            [prepared], max_attempts=max_page_generation_attempts(0), scheduler=WeightedApiScheduler(1),
            review_scheduler=WeightedApiScheduler(1), model_config=resolve_effective_model_config(settings, ROLE_IMAGE),
            review_model_config=None, concurrency=1, total_pages=pages,
        )
        runner._finalize_file_record(prepared, should_assemble=True)
    return runner, prepared.record, client, logs, chats, word_segments


def _sorted_pages(record):
    return sorted(record.pages, key=lambda page: page.page_number)


def test_text_pages_use_paragraph_reflow_and_failing_pages_go_to_word(tmp_path):
    fake = _Runs(good_pages={1})
    _runner, record, client, logs, chats, word = _run_task(tmp_path, fake_babeldoc=fake)
    assert len(client.calls) == 0 and word == [1]   # only page 2 went to Word
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["pages"] == [1, 2] and call["lang_out"] == "zh" and call["workers"] == 3
    assert call["glossary"] == [("passport", "护照")] and call["custom_prompt"] == component.LITERAL_RULES
    # 翻译走我们的引擎：系统消息是我们的规则，用户消息是 BabelDOC 拼好的提示词
    assert chats == [("SYSTEM", "Translate: Hello")] and call["reply"] == "你好"
    first, second = _sorted_pages(record)
    assert first.babeldoc_status == "done" and Path(first.babeldoc_pdf_path).is_file()
    assert second.babeldoc_status != "done"
    assert second.route_note.startswith("段落重排没通过检查（有段落没有翻译")
    assert second.route_note.endswith("，改为转成 Word 翻译")
    with fitz.open(record.translated_pdf_path) as out:
        assert out.page_count == 2
        assert "BABELDOC" in out[0].get_text()
        assert "SEGMENT-TRANSLATED" in out[1].get_text()
    warnings = [text for level, text in logs if level == "WARN"]
    assert len(warnings) == 1 and "第 2 页段落重排没通过检查（有段落没有翻译" in warnings[0], warnings
    assert not [text for _level, text in logs if "大幅面" in text]
    assert record.skipped_oversize_page_count == 0


def test_without_the_component_text_pages_go_straight_to_word(tmp_path):
    # 智能混合没有子选项：没装组件时文字页本来就转 Word，不算「换了方式」，不另提示（2026-11-28）。
    fake = _Runs(good_pages={1, 2})
    _runner, record, client, logs, _chats, word = _run_task(tmp_path, fake_babeldoc=fake, installed=False)
    assert fake.calls == [] and len(client.calls) == 0 and word == [2]
    assert all(page.babeldoc_status != "done" for page in record.pages)
    assert all(not page.route_note for page in record.pages)
    assert not [text for level, text in logs if level == "WARN" and "段落重排" in text]
    with fitz.open(record.translated_pdf_path) as out:
        assert "SEGMENT-TRANSLATED" in out[0].get_text()


def test_a_model_without_chat_falls_back_to_word(tmp_path):
    fake = _Runs(good_pages={1, 2})
    _runner, record, _client, logs, _chats, _word = _run_task(tmp_path, fake_babeldoc=fake, chat=False)
    assert fake.calls == []
    assert any("所选的文本翻译模型不支持段落重排" in text for _level, text in logs)
    assert all("段落重排" not in page.route_note or "改为转成 Word" in page.route_note for page in record.pages)


def test_all_pages_passing_means_no_word_conversion(tmp_path):
    fake = _Runs(good_pages={1, 2})
    _runner, record, _client, _logs, _chats, word = _run_task(tmp_path, fake_babeldoc=fake)
    assert word == []
    assert all(page.babeldoc_status == "done" for page in record.pages)
    with fitz.open(record.translated_pdf_path) as out:
        assert all("BABELDOC" in page.get_text() for page in out)


# ── 设置与扩展页 ──────────────────────────────────────────────────────────


def test_installing_the_component_needs_no_setting(monkeypatch):
    """装好就用：智能混合的文字页先用段落重排，不用再改设置（原来装好后要把「文字页」改成它）。"""
    import api.babeldoc_plugin as plugin
    from core.pdf_babeldoc_pages import PdfBabelDocPagesMixin
    from settings import PdfSettings

    assert "auto_text_route" not in PdfSettings.model_fields
    assert not hasattr(plugin, "enable_babeldoc_route")

    class _Owner(PdfBabelDocPagesMixin):
        def __init__(self, route: str) -> None:
            self._settings = type("S", (), {"pdf": PdfSettings(translation_route=route)})()

    site = Path("bd")
    monkeypatch.setattr(component, "component_paths", lambda: (site, site, site))
    assert _Owner("auto")._babeldoc_route_chosen() is True
    assert _Owner("image")._babeldoc_route_chosen() is False
    monkeypatch.setattr(component, "component_paths", lambda: None)
    assert _Owner("auto")._babeldoc_route_chosen() is False


def test_fixed_glossary_takes_only_unambiguous_always_entries(monkeypatch):
    from core import tm_terms
    from core.pdf_babeldoc_pages import fixed_glossary

    def entry(entry_id, source, target, usage):
        return tm_terms.TermEntry(id=entry_id, source=source, target=target, lang_pair="en-zh", note="",
                                  usage=usage, origin="manual", scope="")

    rows = [
        entry(1, "passport", "护照", "always"),
        entry(2, "visa", "签证", "always"), entry(3, "visa", "签注", "always"),     # two translations
        entry(4, "ticket", "机票", "always"), entry(5, "ticket", "票", "scene"),    # not all always
        entry(6, "insurance", "保险", "scene"),
    ]
    seen: dict = {}

    def load(target_lang, *, lang_pairs, file_paths):
        seen.update(target=target_lang, pairs=lang_pairs, files=file_paths)
        return tm_terms.TermIndex(rows)

    monkeypatch.setattr(tm_terms.TermIndex, "load", staticmethod(load))
    assert fixed_glossary("zh", ["en-zh"], "/docs/a.pdf") == [("passport", "护照")]
    assert seen == {"target": "zh", "pairs": ["en-zh"], "files": ["/docs/a.pdf"]}


@pytest.fixture
def bundle(monkeypatch):
    from api import pdf_components_plugin as plugin

    installed: list[str] = []
    failures: dict[str, Exception] = {}
    missing = ["pdf", "babeldoc", "ocr"]

    def install_item(item, *, progress, cancel):
        progress("download", 5, 10)
        if item in failures:
            raise failures[item]
        installed.append(item)

    monkeypatch.setattr(plugin, "missing_items", lambda: list(missing))
    monkeypatch.setattr(plugin, "_item_size", lambda item: {"pdf": 100, "babeldoc": 500, "ocr": 40}[item])
    monkeypatch.setattr(plugin, "_install_item", install_item)
    monkeypatch.setattr(plugin, "status", lambda: {"missing": []})
    return plugin, installed, failures, missing


def test_install_all_goes_in_order_and_reports_progress_across_items(bundle):
    plugin, installed, _failures, _missing = bundle
    phases: list[tuple[str, int, int]] = []
    plugin._install_all(progress=lambda phase, done, total: phases.append((phase, done, total)))
    assert installed == ["pdf", "babeldoc", "ocr"]
    assert phases == [("pdf:download", 5, 640), ("babeldoc:download", 105, 640), ("ocr:download", 605, 640)]
    assert {item: value["status"] for item, value in plugin._RESULTS.items()} == {
        "pdf": "done", "babeldoc": "done", "ocr": "done"}


def test_one_failed_item_does_not_stop_the_others(bundle):
    plugin, installed, failures, _missing = bundle
    failures["babeldoc"] = RuntimeError("网络连接中断，下载没有完成。")
    with pytest.raises(plugin.BundleInstallError, match="段落重排组件没有装好，其余组件已装好"):
        plugin._install_all()
    assert installed == ["pdf", "ocr"]
    assert plugin._RESULTS["babeldoc"] == {"status": "failed", "error": "网络连接中断，下载没有完成。"}


def test_the_pdf_component_failing_stops_the_rest(bundle):
    plugin, installed, failures, _missing = bundle
    failures["pdf"] = RuntimeError("核对没通过")
    with pytest.raises(plugin.BundleInstallError, match="PDF 处理组件没有装好：核对没通过"):
        plugin._install_all()
    assert installed == []
    assert [plugin._RESULTS[item]["status"] for item in ("pdf", "babeldoc", "ocr")] == ["failed", "pending", "pending"]


def test_only_missing_items_are_installed(bundle):
    plugin, installed, _failures, missing = bundle
    missing[:] = ["babeldoc"]
    plugin._install_all()
    assert installed == ["babeldoc"] and list(plugin._RESULTS) == ["babeldoc"]


def test_cancel_stops_before_the_next_item(bundle):
    plugin, installed, _failures, _missing = bundle
    cancel = threading.Event()
    cancel.set()
    plugin._install_all(cancel=cancel)
    assert installed == [] and {value["status"] for value in plugin._RESULTS.values()} == {"cancelled"}


def test_install_all_waits_for_a_single_item_install(monkeypatch):
    from api import pdf_components_plugin as plugin

    monkeypatch.setattr(plugin, "_item_running", lambda: "文字识别组件")
    with pytest.raises(ValueError, match="文字识别组件正在安装"):
        plugin.start_install()
    monkeypatch.setattr(plugin, "_item_running", lambda: "")
    monkeypatch.setattr(plugin, "missing_items", list)
    with pytest.raises(ValueError, match="都已安装"):
        plugin.start_install()


def test_single_item_installs_wait_for_install_all(monkeypatch):
    from api import babeldoc_plugin, ocr_plugin, pdf2docx_plugin, pdf_components_plugin

    monkeypatch.setattr(pdf_components_plugin, "running", lambda: True)
    monkeypatch.setattr(babeldoc_plugin, "_unsupported_reason", lambda: "")
    monkeypatch.setattr(ocr_plugin.installer, "manifest_files", lambda: (object(),))
    monkeypatch.setattr(pdf2docx_plugin.installer, "manifest_files", lambda: (object(),))
    for module in (babeldoc_plugin, ocr_plugin, pdf2docx_plugin):
        with pytest.raises(ValueError, match="「PDF 组件」正在安装"):
            module.start_install()
