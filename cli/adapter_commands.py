"""``translator-sidecar adapter new | test | install | uninstall | pack``：制作、自测、安装和打包扩展。

- ``new``：从 ``cli/adapter_template/`` 生成一个能用的扩展（与模板仓库
  KlaraGraff/XL_Translator-extension-template 逐字节相同，只换 ``adapter.json`` 的 ``id``）。
- ``test``：安装前的全部检查（``core/adapter_install.inspect_package``），再用子进程宿主在
  ``samples/`` 上跑 scan / extract / write 并核对往返；扩展列表的自动检查跑的也是它。
- ``install`` / ``uninstall``：经翻译引擎安装或卸载（Translator 开着时立即出现在侧栏）。
- ``pack``：打出 zip、SHA-256 和扩展列表的条目；用的是模板里的 ``scripts/pack.py``，
  所以作者在 GitHub 工作流里打的包与这里打的相同。

不需要翻译引擎的 ``new``、``test``、``pack`` 不连引擎。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from core import adapter_install
from core.adapter_install import CONFIRM_NOTICE, inspect_package
from core.adapter_subprocess_host import SubprocessAdapterHost
from core.format_adapter import (
    BUNDLED_ADAPTERS_DIR,
    LANGUAGES,
    MANIFEST_FILE,
    AdapterError,
    AdapterHost,
    AdapterManifest,
    InProcessAdapterHost,
)
from translator_adapter import (
    MODE_BILINGUAL,
    MODE_TRANSLATION_ONLY,
    STATUS_LOCKED,
    STATUS_TRANSLATED,
    TextUnit,
    TranslatedUnit,
)

#: 模板（模板仓库的全部内容）。
TEMPLATE_DIR = Path(__file__).resolve().parent / "adapter_template"
#: 模板 ``adapter.json`` 里的示例 id，``new`` 换成作者给的。
TEMPLATE_ID = "my-format"
#: 自测写回时加在译文后面的标记，再取字时每段都应带着它。
TEST_MARK = " [TR]"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_MODE_NAMES = {MODE_TRANSLATION_ONLY: "仅译文", MODE_BILINGUAL: "原文 + 译文"}


class AdapterCommandError(Exception):
    """命令做不下去的原因（给作者看的一句话）。"""


# ── new ──────────────────────────────────────────────────────────────────


def template_files() -> list[str]:
    """模板里的文件（相对路径，``/`` 分隔，已排序）。"""
    return sorted(
        path.relative_to(TEMPLATE_DIR).as_posix()
        for path in TEMPLATE_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    )


def new_adapter(adapter_id: str, target: Path) -> list[str]:
    """在 ``target`` 生成扩展；返回写出的文件。``target`` 已有内容时拒绝。"""
    adapter_id = str(adapter_id or "").strip()
    if not _ID_RE.match(adapter_id):
        raise AdapterCommandError("id 只能用小写字母、数字、下划线和连字符，以字母或数字开头，最长 40 个字符")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise AdapterCommandError(f"{target} 已存在且不是空文件夹")
    if adapter_id in adapter_install.bundled_ids():
        raise AdapterCommandError(f"id「{adapter_id}」与应用自带的翻译类型相同，请换一个")
    files = template_files()
    for relative in files:
        source = TEMPLATE_DIR / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == MANIFEST_FILE:
            text = source.read_text(encoding="utf-8")
            text = text.replace(f'"id": "{TEMPLATE_ID}"', f'"id": "{adapter_id}"', 1)
            destination.write_bytes(text.encode("utf-8"))
        else:
            shutil.copyfile(source, destination)
    return files


# ── test ─────────────────────────────────────────────────────────────────


@dataclass
class SampleRun:
    """一份样例的试运行结果。"""

    file: str
    units: int = 0
    locked: int = 0
    scan_units: int | None = None
    modes: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "units": self.units,
            "locked": self.locked,
            "scan_units": self.scan_units,
            "modes": dict(self.modes),
            "seconds": round(self.seconds, 2),
        }


@dataclass
class CheckReport:
    """``adapter test`` 的结果。"""

    directory: str
    manifest: AdapterManifest | None = None
    files: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    size: int = 0
    runs: list[SampleRun] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not self.problems

    def to_dict(self) -> dict[str, Any]:
        manifest = self.manifest
        return {
            "ok": self.ok,
            "directory": self.directory,
            "id": manifest.id if manifest else "",
            "version": manifest.version if manifest else "",
            "label": dict(manifest.label) if manifest else {},
            "extensions": list(manifest.extensions) if manifest else [],
            "files": list(self.files),
            "samples": list(self.samples),
            "size": self.size,
            "runs": [run.to_dict() for run in self.runs],
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }


def check_adapter(directory: Path, *, allow_reserved_id: bool = False) -> CheckReport:
    """安装前的全部检查，加上在 ``samples/`` 上试运行。"""
    package = inspect_package(directory, reserved_ids=set() if allow_reserved_id else None)
    report = CheckReport(
        directory=str(directory),
        manifest=package.manifest,
        files=package.files,
        samples=package.samples,
        size=package.size,
        problems=list(package.problems),
        warnings=list(package.warnings),
    )
    manifest = package.manifest
    if manifest is None:
        return report
    report.warnings.extend(_text_warnings(manifest))
    if report.problems:
        report.warnings.append("未试运行样例：先修好上面列出的问题")
        return report
    runnable = [name for name in package.samples if Path(name).suffix.lower().lstrip(".") in manifest.extensions]
    if not runnable:
        suffixes = "、".join(f".{suffix}" for suffix in manifest.extensions)
        report.problems.append(f"samples/ 里没有 {suffixes} 样例，无法试运行")
        return report
    # 随应用提供的类型在应用进程里运行，可以导入应用模块和 lxml：照样在本进程里试。
    # 第三方扩展照安装后的样子，在子进程里试（只有标准库和 translator_adapter）。
    host: AdapterHost = (
        InProcessAdapterHost(directory, manifest) if is_bundled_adapter(directory) else SubprocessAdapterHost(directory, manifest)
    )
    options = {option.key: option.default for option in manifest.options}
    with tempfile.TemporaryDirectory(prefix="translator-adapter-test-") as work:
        for index, name in enumerate(runnable):
            output_dir = Path(work) / str(index)
            output_dir.mkdir()
            report.runs.append(_run_sample(host, manifest, directory / name, name, output_dir, options, report))
    return report


def is_bundled_adapter(directory: Path) -> bool:
    """``directory`` 是随应用提供的类型（源码树的 ``adapters/<id>/``）。"""
    try:
        return Path(directory).resolve().parent == BUNDLED_ADAPTERS_DIR.resolve()
    except OSError:
        return False


def _text_warnings(manifest: AdapterManifest) -> list[str]:
    warnings = []
    if not manifest.description:
        warnings.append("清单没有 description（页面「?」里的说明；提交到扩展列表时必须有）")
    elif missing := [lang for lang in LANGUAGES if lang not in manifest.description]:
        warnings.append(f"description 缺少 {', '.join(missing)}")
    for option in manifest.options:
        if option.help and (missing := [lang for lang in LANGUAGES if lang not in option.help]):
            warnings.append(f"开关 {option.key} 的 help 缺少 {', '.join(missing)}")
    return warnings


def _run_sample(
    host: AdapterHost,
    manifest: AdapterManifest,
    path: Path,
    name: str,
    output_dir: Path,
    options: dict[str, Any],
    report: CheckReport,
) -> SampleRun:
    run = SampleRun(file=name)
    started = time.monotonic()
    try:
        try:
            run.scan_units = host.scan(path).unit_count
        except AdapterError as exc:
            report.problems.append(f"{name}：scan 出错：{exc}")
        try:
            units = host.extract(path)
        except AdapterError as exc:
            report.problems.append(f"{name}：extract 出错：{exc}")
            return run
        run.units = len(units)
        run.locked = sum(1 for unit in units if unit.locked)
        translatable = run.units - run.locked
        if not units:
            report.warnings.append(f"{name}：没有取出任何文字")
        if run.scan_units is not None and run.scan_units != translatable:
            report.warnings.append(f"{name}：scan 统计 {run.scan_units} 段，extract 取出 {translatable} 段要翻译的文字")
        modes = [mode for mode, wanted in (
            (MODE_TRANSLATION_ONLY, manifest.capabilities.translation_only),
            (MODE_BILINGUAL, manifest.capabilities.bilingual),
        ) if wanted]
        for mode in modes:
            run.modes[mode] = _check_mode(host, path, name, units, mode, output_dir, options, report)
    finally:
        run.seconds = time.monotonic() - started
    return run


def _translated(units: list[TextUnit], suffix: str) -> list[TranslatedUnit]:
    return [
        TranslatedUnit(
            id=unit.id,
            text=unit.text,
            translation=unit.text if unit.locked else f"{unit.text}{suffix}",
            status=STATUS_LOCKED if unit.locked else STATUS_TRANSLATED,
            # 带段内格式标记的单元：写回时也把带标记的译文交过去，试一下适配器按标记写回的那条路。
            marked_translation=f"{unit.marked}{suffix}" if unit.marked and not unit.locked else "",
        )
        for unit in units
    ]


def _check_mode(
    host: AdapterHost,
    path: Path,
    name: str,
    units: list[TextUnit],
    mode: str,
    output_dir: Path,
    options: dict[str, Any],
    report: CheckReport,
) -> str:
    """写回一次（仅译文时再写一次带标记的），返回 ``ok`` / ``failed``。"""
    label = f"{name}（{_MODE_NAMES[mode]}）"
    problems_before = len(report.problems)
    plain = output_dir / f"{mode}{path.suffix}"
    try:
        host.write(path, _translated(units, ""), mode, plain, options)
    except AdapterError as exc:
        report.problems.append(f"{label}：write 出错：{exc}")
        return "failed"
    if not plain.is_file():
        report.problems.append(f"{label}：write 没有生成输出文件")
        return "failed"
    if mode != MODE_TRANSLATION_ONLY:
        return "ok"
    try:
        again = host.extract(plain)
    except AdapterError as exc:
        report.problems.append(f"{label}：写回的文件无法再取字：{exc}")
        return "failed"
    before, after = [unit.text for unit in units], [unit.text for unit in again]
    if before != after:
        report.problems.append(f"{label}：以原文作译文写回后再取字，与原文不一致（{_first_difference(before, after)}）")
    marked = output_dir / f"{mode}-marked{path.suffix}"
    try:
        host.write(path, _translated(units, TEST_MARK), mode, marked, options)
        again = host.extract(marked)
    except AdapterError as exc:
        report.problems.append(f"{label}：写回带标记的译文时出错：{exc}")
        return "failed"
    expected = [index for index, unit in enumerate(units) if not unit.locked]
    if len(again) != len(units):
        report.problems.append(f"{label}：写回带标记的译文后再取字，段数从 {len(units)} 变成 {len(again)}")
    elif expected:
        missing = [index for index in expected if TEST_MARK.strip() not in again[index].text]
        if len(missing) == len(expected):
            report.problems.append(f"{label}：写回时没有用上译文（每段都还是原文）")
        elif missing:
            report.warnings.append(
                f"{label}：{len(missing)} 段写回后没有带上译文，第一段是第 {missing[0] + 1} 段「{_short(units[missing[0]].text)}」"
            )
    return "ok" if len(report.problems) == problems_before else "failed"


def _first_difference(before: list[str], after: list[str]) -> str:
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new:
            return f"第 {index + 1} 段：「{_short(old)}」变成「{_short(new)}」"
    return f"段数从 {len(before)} 变成 {len(after)}"


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── pack ─────────────────────────────────────────────────────────────────


def pack_module() -> ModuleType:
    """模板里的 ``scripts/pack.py``：作者在 GitHub 工作流里用的就是这一份。"""
    path = TEMPLATE_DIR / "scripts" / "pack.py"
    spec = importlib.util.spec_from_file_location("translator_adapter_pack", path)
    if spec is None or spec.loader is None:
        raise AdapterCommandError(f"找不到打包脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pack_adapter(directory: Path, *, out: str = "", tag: str = "", repo: str = "", author: str = "") -> dict[str, Any]:
    """先做与安装相同的静态检查（不查 id 是否与自带类型重名），再打包。"""
    package = inspect_package(directory, reserved_ids=set())
    if package.problems:
        raise AdapterCommandError("检查未通过，没有打包：" + "；".join(package.problems))
    module = pack_module()
    try:
        return module.pack(directory, out=out or None, tag=tag, repo=repo, author=author)
    except module.PackError as exc:
        raise AdapterCommandError(str(exc)) from None


__all__ = [
    "CONFIRM_NOTICE",
    "TEMPLATE_DIR",
    "TEMPLATE_ID",
    "TEST_MARK",
    "AdapterCommandError",
    "CheckReport",
    "SampleRun",
    "check_adapter",
    "new_adapter",
    "pack_adapter",
    "pack_module",
    "template_files",
]
