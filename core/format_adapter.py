"""格式适配器：清单、加载与调用（扩展性阶段 D）。

一种新文件类型只需要一个目录：``adapter.json``（清单：名称、扩展名、能力、开关）加
``adapter.py``（三个函数：扫描、取字、写回，见 ``translator_adapter``）。页面、模型调用、
记忆库、重试、复核标记、进度和任务中心都由应用提供：``api/surfaces/adapter.py`` 按清单
生成一个 ``Surface``，``core/adapter_runner.py`` 是所有适配器共用的执行器。

本模块只管「读清单、校验、把适配器的三个函数调起来」，不认识任何具体格式。
``InProcessAdapterHost`` 在应用进程里加载随应用发布的适配器（``adapters/<id>/``）；
以后从外部安装的适配器换成子进程实现，执行器只认 ``AdapterHost`` 这几个方法，不感知差别。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import json
import re
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app_meta import APP_VERSION
from translator_adapter import (
    MODES,
    OutputFile,
    ScanItem,
    TextUnit,
    TranslatedUnit,
)

#: 应用支持的适配器协议版本（含两端）。``translator_adapter.ADAPTER_API_VERSION`` 升版时
#: 只往上加，旧版本写的适配器仍在区间内。
SUPPORTED_ADAPTER_API_VERSIONS = (1, 1)

MANIFEST_FILE = "adapter.json"
ADAPTER_MODULE_FILE = "adapter.py"

#: 随应用发布的适配器目录（源码树根目录 ``adapters/``；打包时由
#: ``packaging/sidecar/translator_sidecar.spec`` 的 ``datas`` 收进同一相对位置）。
BUNDLED_ADAPTERS_DIR = Path(__file__).resolve().parents[1] / "adapters"

#: 「双语对照」开关的键：两种输出都支持的类型由应用统一加上这个开关，清单里不能再用。
BILINGUAL_OPTION_KEY = "bilingual"
#: 执行器交给适配器的 options 里带上目标语言（设置里的语言代码，如 "fr"）：写回时改语言
#: 属性、断行规则要用。清单开关同样不能用这个键。
TARGET_LANG_OPTION_KEY = "target_lang"
RESERVED_OPTION_KEYS = frozenset({BILINGUAL_OPTION_KEY, TARGET_LANG_OPTION_KEY})

#: 描述符和界面要求的三种界面语言。
LANGUAGES = ("zh", "en", "fr")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_OPTION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,16}$")
_ICON_RE = re.compile(r"^[a-z0-9-]{0,32}$")
_CLOSING_MARK_RE = re.compile(r"<\s*/\s*g\s*\d+\s*>", re.IGNORECASE)
#: 清单里内置规则的长度上限：比应用通用的那份长得多就该拆成补充要求了。
_PROMPT_MAX_CHARS = 4000


class AdapterManifestError(ValueError):
    """清单缺字段、写错或与本应用不兼容；消息可直接写进日志。"""


class AdapterError(RuntimeError):
    """调用适配器时出错（读不了文件、返回值不合约定、写回失败）。"""


def _three_languages(value: Any, field: str, *, required: bool = True) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} 必须是 {{zh, en, fr}} 三种语言的对象")
    texts = {lang: str(value.get(lang) or "").strip() for lang in LANGUAGES}
    if required:
        missing = [lang for lang in LANGUAGES if not texts[lang]]
        if missing:
            raise ValueError(f"{field} 缺少 {', '.join(missing)}")
    return {lang: text for lang, text in texts.items() if text}


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    if not parts:
        raise ValueError(f"无法识别的版本号：{value!r}")
    return tuple(int(part) for part in parts[:4])


class AdapterChoice(BaseModel):
    """「选择」类选项里的一项：传给适配器的值，和界面上显示的名字。"""

    model_config = ConfigDict(extra="forbid")

    value: str
    label: dict[str, str]

    @field_validator("value")
    @classmethod
    def _value(cls, value: str) -> str:
        if not _OPTION_KEY_RE.match(value):
            raise ValueError("选项值只能用小写字母、数字和下划线，以字母开头")
        return value

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: Any) -> dict[str, str]:
        return _three_languages(value, "label")


class AdapterOption(BaseModel):
    """清单里的一个选项；字段与描述符的 ``OptionDef`` 一致。

    ``kind`` 为 ``"toggle"``（默认）是开关，``default`` 是布尔值；为 ``"choice"`` 是多选一，
    ``choices`` 列出各项，``default`` 是其中一项的 ``value``（不写时取第一项）。
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    kind: Literal["toggle", "choice"] = "toggle"
    label: dict[str, str]
    help: dict[str, str] = Field(default_factory=dict)
    default: bool | str = False
    choices: list[AdapterChoice] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _OPTION_KEY_RE.match(value):
            raise ValueError("开关的 key 只能用小写字母、数字和下划线，以字母开头")
        if value in RESERVED_OPTION_KEYS:
            raise ValueError(f"开关的 key 不能用 {value!r}（由应用统一提供）")
        return value

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: Any) -> dict[str, str]:
        return _three_languages(value, "label")

    @field_validator("help", mode="before")
    @classmethod
    def _help(cls, value: Any) -> dict[str, str]:
        return _three_languages(value or {}, "help", required=False)

    @model_validator(mode="after")
    def _kind(self) -> AdapterOption:
        if self.kind == "toggle":
            if self.choices:
                raise ValueError(f"开关 {self.key} 不能有 choices（多选一请写 \"kind\": \"choice\"）")
            if not isinstance(self.default, bool):
                raise ValueError(f"开关 {self.key} 的 default 只能是 true 或 false")
            return self
        values = [choice.value for choice in self.choices]
        if len(values) < 2:
            raise ValueError(f"选项 {self.key} 至少要有两项 choices")
        if len(set(values)) != len(values):
            raise ValueError(f"选项 {self.key} 的 choices 里 value 重复")
        if self.default is False:
            self.default = values[0]
        if self.default not in values:
            raise ValueError(f"选项 {self.key} 的 default 应为 choices 里的一个 value：{', '.join(values)}")
        return self


class AdapterCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 能写出「原文 + 译文」。
    bilingual: bool = False
    #: 能写出「仅译文」。
    translation_only: bool = True

    @model_validator(mode="after")
    def _at_least_one(self) -> AdapterCapabilities:
        if not (self.bilingual or self.translation_only):
            raise ValueError("capabilities 至少要支持一种输出（bilingual 或 translation_only）")
        return self


class AdapterManifest(BaseModel):
    """``adapter.json``：一个格式适配器是什么、处理哪些文件、页面上显示什么。"""

    model_config = ConfigDict(extra="forbid")

    #: 小写字母、数字、``_`` 和 ``-``；页面键是 ``ext:<id>``，设置在 ``settings.extensions[<id>]``。
    id: str
    version: str
    adapter_api_version: int
    label: dict[str, str]
    file_noun: dict[str, str]
    #: 页面「?」里的一两句说明（处理什么、哪些内容不翻译）；可省略。
    description: dict[str, str] = Field(default_factory=dict)
    #: 这种文件的内置规则（发给模型的基础要求，补充要求接在后面）；可省略，省略时用应用
    #: 通用的那份。用户可在「翻译参数」里这个类型的页签改写或恢复默认。
    prompt: str = ""
    #: ``ui/src/icons.ts`` 里的图标名；界面不认识时用默认图标。
    icon: str = ""
    extensions: list[str]
    capabilities: AdapterCapabilities = Field(default_factory=AdapterCapabilities)
    options: list[AdapterOption] = Field(default_factory=list)
    license: str = ""
    repo: str = ""
    #: 需要的最低应用版本；空表示不限。
    min_app_version: str = ""
    #: 只对随应用提供的类型生效：没在「扩展」页拨过时也显示在侧栏（像内置四种一样）。
    #: 用户装进来的扩展写了也按不显示处理——它们装上时本来就打开了（``api/extension_install_job.py``）。
    visible_by_default: bool = False
    #: 只对随应用提供的类型生效：侧栏默认顺序里紧跟在哪个类型后面（如 ``word``）；空表示排在最后。
    sidebar_after: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id 只能用小写字母、数字、下划线和连字符，最长 40 个字符")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 40:
            raise ValueError("version 不能为空，最长 40 个字符")
        return value

    @field_validator("adapter_api_version")
    @classmethod
    def _api_version(cls, value: int) -> int:
        low, high = SUPPORTED_ADAPTER_API_VERSIONS
        if not low <= value <= high:
            raise ValueError(f"本应用支持的适配器协议版本为 {low}–{high}，清单写的是 {value}")
        return value

    @field_validator("label", "file_noun", mode="before")
    @classmethod
    def _localized(cls, value: Any, info: Any) -> dict[str, str]:
        return _three_languages(value, info.field_name)

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, value: Any) -> dict[str, str]:
        return _three_languages(value or {}, "description", required=False)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) > _PROMPT_MAX_CHARS:
            raise ValueError(f"prompt 最长 {_PROMPT_MAX_CHARS} 个字符")
        return value

    @field_validator("icon")
    @classmethod
    def _icon(cls, value: str) -> str:
        if not _ICON_RE.match(value):
            raise ValueError("icon 只能用小写字母、数字和连字符")
        return value

    @field_validator("extensions", mode="before")
    @classmethod
    def _extensions(cls, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("extensions 至少要有一个扩展名")
        normalized: list[str] = []
        for item in value:
            suffix = str(item or "").strip().lower().lstrip(".")
            if not _EXTENSION_RE.match(suffix):
                raise ValueError(f"无法识别的扩展名：{item!r}")
            if suffix not in normalized:
                normalized.append(suffix)
        return normalized

    @field_validator("options")
    @classmethod
    def _unique_options(cls, value: list[AdapterOption]) -> list[AdapterOption]:
        keys = [option.key for option in value]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"开关的 key 重复：{', '.join(duplicates)}")
        return value

    @field_validator("sidebar_after")
    @classmethod
    def _sidebar_after(cls, value: str) -> str:
        value = value.strip().lower()
        if value and not re.match(r"^(?:ext:)?[a-z0-9][a-z0-9_-]{0,39}$", value):
            raise ValueError("sidebar_after 应是一个类型键，如 word 或 ext:text")
        return value

    @field_validator("min_app_version")
    @classmethod
    def _min_app_version(cls, value: str) -> str:
        value = value.strip()
        if value:
            _version_tuple(value)
        return value

    @property
    def key(self) -> str:
        """页面键 ``ext:<id>``。"""
        return f"ext:{self.id}"

    def supports_app(self, app_version: str = APP_VERSION) -> bool:
        if not self.min_app_version:
            return True
        return _version_tuple(app_version) >= _version_tuple(self.min_app_version)


def _validation_summary(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "__root__")
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{location}：{message}" if location else message)
    return "；".join(parts)


def parse_manifest(data: Any, *, source: str = MANIFEST_FILE, app_version: str = APP_VERSION) -> AdapterManifest:
    """校验清单内容（已解析的 JSON 对象），并核对是否适用于 ``app_version``。"""
    try:
        manifest = AdapterManifest.model_validate(data)
    except ValidationError as exc:
        raise AdapterManifestError(f"{source} 不符合要求：{_validation_summary(exc)}") from exc
    if not manifest.supports_app(app_version):
        raise AdapterManifestError(
            f"{source}：需要应用 {manifest.min_app_version} 或更新的版本（当前 {app_version}）"
        )
    return manifest


def load_manifest(directory: str | Path, *, app_version: str = APP_VERSION) -> AdapterManifest:
    """读 ``<directory>/adapter.json`` 并校验。"""
    path = Path(directory) / MANIFEST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AdapterManifestError(f"{path.parent.name}：没有 {MANIFEST_FILE}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterManifestError(f"{path.parent.name}/{MANIFEST_FILE} 无法读取：{exc}") from exc
    return parse_manifest(data, source=f"{path.parent.name}/{MANIFEST_FILE}", app_version=app_version)


def discover_adapters(
    root: str | Path, *, app_version: str = APP_VERSION
) -> tuple[list[tuple[Path, AdapterManifest]], list[str]]:
    """``root`` 下每个带 ``adapter.json`` 的子目录：返回（可用的, 跳过原因）。

    目录不存在或为空时两项都为空；按目录名排序，同一个 id 只取第一个。
    """
    base = Path(root)
    found: list[tuple[Path, AdapterManifest]] = []
    problems: list[str] = []
    if not base.is_dir():
        return found, problems
    seen: set[str] = set()
    for directory in sorted(path for path in base.iterdir() if path.is_dir()):
        if directory.name.startswith((".", "_")) or not (directory / MANIFEST_FILE).is_file():
            continue
        try:
            manifest = load_manifest(directory, app_version=app_version)
        except AdapterManifestError as exc:
            problems.append(str(exc))
            continue
        if not (directory / ADAPTER_MODULE_FILE).is_file():
            problems.append(f"{directory.name}：没有 {ADAPTER_MODULE_FILE}")
            continue
        if manifest.id in seen:
            problems.append(f"{directory.name}：id {manifest.id!r} 与前面的适配器重复")
            continue
        seen.add(manifest.id)
        found.append((directory, manifest))
    return found, problems


# ── 调用适配器 ──────────────────────────────────────────────────────────────


class AdapterHost(Protocol):
    """执行器眼里的一个适配器：清单 + 三个调用。参数与返回值都能 JSON 往返。"""

    manifest: AdapterManifest

    def scan(self, path: Path, options: Mapping[str, Any] | None = None) -> ScanItem: ...

    def extract(self, path: Path, options: Mapping[str, Any] | None = None) -> list[TextUnit]: ...

    def write(
        self,
        path: Path,
        units: list[TranslatedUnit],
        mode: str,
        out_path: Path,
        options: Mapping[str, Any],
    ) -> OutputFile: ...


_T = TypeVar("_T", ScanItem, TextUnit, TranslatedUnit, OutputFile)


def _coerce(value: Any, cls: type[_T], what: str) -> _T:
    """适配器返回的对象或字典 → 一份经 JSON 形态复制的 ``cls``（与子进程拿到的一样）。"""
    if isinstance(value, cls):
        return cls.from_dict(value.to_dict())
    if isinstance(value, Mapping):
        try:
            return cls.from_dict(value)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{what} 返回值不合约定：{exc}") from exc
    raise AdapterError(f"{what} 应返回 {cls.__name__}，实际是 {type(value).__name__}")


def validate_units(units: list[TextUnit], what: str = "extract") -> list[TextUnit]:
    """单元 id 非空且在文件内唯一；``protected`` 只留原文里确实出现的片段；``marked``
    去掉标记后须与原文一字不差、标记成对不嵌套，否则按没有标记处理（与 Word 同一套校验）。"""
    seen: set[str] = set()
    for unit in units:
        if not unit.id:
            raise AdapterError(f"{what} 返回了没有 id 的单元")
        if unit.id in seen:
            raise AdapterError(f"{what} 返回的单元 id 重复：{unit.id!r}")
        seen.add(unit.id)
        unit.protected = [fragment for fragment in dict.fromkeys(unit.protected) if fragment and fragment in unit.text]
        if unit.marked and not _usable_marks(unit.marked, unit.text):
            unit.marked = ""
    return units


def _usable_marks(marked: str, text: str) -> bool:
    # 只有带标记的单元才用到；word_inline_marks 带着 python-docx，不在模块顶部导入。
    from core.word_inline_marks import MAX_GROUPS_PER_UNIT, marks_match, strip_marks

    if marked == text or strip_marks(marked) != text or not marks_match(marked, marked):
        return False
    return len(_CLOSING_MARK_RE.findall(marked)) <= MAX_GROUPS_PER_UNIT


def _takes_second_argument(function: Callable[..., Any]) -> bool:
    try:
        inspect.signature(function).bind("path", {})
    except (TypeError, ValueError):
        return False
    return True


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """每次从源码编译 ``adapter.py``，不读也不写 ``__pycache__``。

    随应用发布的适配器在签过名的安装包里，往里面写缓存文件会改动安装包。
    """

    def get_code(self, fullname: str) -> CodeType:
        return compile(self.get_data(self.path), self.path, "exec", dont_inherit=True)


class InProcessAdapterHost:
    """在应用进程里加载并调用一个适配器（只给随应用发布的适配器用）。

    ``adapter.py`` 第一次用到时才加载；``extract`` 与 ``write`` 必须有，``scan`` 可省略
    （省略时按 ``extract`` 的结果数单元）。适配器抛出的异常一律换成 ``AdapterError``。
    """

    def __init__(self, directory: str | Path, manifest: AdapterManifest | None = None) -> None:
        self.directory = Path(directory)
        self.manifest = manifest or load_manifest(self.directory)
        self._module: ModuleType | None = None
        self._lock = threading.Lock()

    @property
    def module_name(self) -> str:
        return f"_translator_adapter_{self.manifest.id.replace('-', '_')}"

    def _load(self) -> ModuleType:
        with self._lock:
            if self._module is not None:
                return self._module
            path = self.directory / ADAPTER_MODULE_FILE
            spec = importlib.util.spec_from_file_location(
                self.module_name, path, loader=_SourceOnlyLoader(self.module_name, str(path))
            )
            if spec is None or spec.loader is None:
                raise AdapterError(f"无法加载 {self.manifest.id}/{ADAPTER_MODULE_FILE}")
            module = importlib.util.module_from_spec(spec)
            # 适配器里的 dataclass 等要按模块名回查 sys.modules。
            sys.modules[self.module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                sys.modules.pop(self.module_name, None)
                raise AdapterError(f"加载 {self.manifest.id}/{ADAPTER_MODULE_FILE} 失败：{exc!r}") from exc
            for name in ("extract", "write"):
                if not callable(getattr(module, name, None)):
                    sys.modules.pop(self.module_name, None)
                    raise AdapterError(f"{self.manifest.id}/{ADAPTER_MODULE_FILE} 缺少函数 {name}()")
            self._module = module
            return module

    def load(self) -> None:
        """现在就加载 ``adapter.py``（冻结包冒烟检查用）；失败抛 ``AdapterError``。"""
        self._load()

    def _call(self, name: str, *args: Any) -> Any:
        function: Callable[..., Any] = getattr(self._load(), name)
        try:
            return function(*args)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__) from exc

    def _call_with_options(self, name: str, path: Path, options: Mapping[str, Any] | None) -> Any:
        # scan / extract 的第二个参数可选：写了 ``def extract(path, options=None)`` 的适配器拿到
        # 开关值和目标语言（如关掉「翻译演讲者备注」时取字就跳过备注），只收路径的照旧。
        if options is not None and _takes_second_argument(getattr(self._load(), name)):
            return self._call(name, str(path), dict(options))
        return self._call(name, str(path))

    def scan(self, path: Path, options: Mapping[str, Any] | None = None) -> ScanItem:
        if callable(getattr(self._load(), "scan", None)):
            return _coerce(self._call_with_options("scan", path, options), ScanItem, "scan")
        units = self.extract(path, options)
        return ScanItem(unit_count=sum(1 for unit in units if not unit.locked))

    def extract(self, path: Path, options: Mapping[str, Any] | None = None) -> list[TextUnit]:
        raw = self._call_with_options("extract", path, options)
        if not isinstance(raw, (list, tuple)):
            raise AdapterError(f"extract 应返回 TextUnit 列表，实际是 {type(raw).__name__}")
        return validate_units([_coerce(item, TextUnit, "extract") for item in raw])

    def write(
        self,
        path: Path,
        units: list[TranslatedUnit],
        mode: str,
        out_path: Path,
        options: Mapping[str, Any],
    ) -> OutputFile:
        if mode not in MODES:
            raise ValueError(f"unknown write mode: {mode!r}")
        copies = [_coerce(unit, TranslatedUnit, "write") for unit in units]
        result = self._call("write", str(path), copies, mode, str(out_path), dict(options))
        if result is None:
            return OutputFile(path=str(out_path))
        return _coerce(result, OutputFile, "write")


__all__ = [
    "ADAPTER_MODULE_FILE",
    "BILINGUAL_OPTION_KEY",
    "BUNDLED_ADAPTERS_DIR",
    "MANIFEST_FILE",
    "RESERVED_OPTION_KEYS",
    "TARGET_LANG_OPTION_KEY",
    "SUPPORTED_ADAPTER_API_VERSIONS",
    "AdapterCapabilities",
    "AdapterChoice",
    "AdapterError",
    "AdapterHost",
    "AdapterManifest",
    "AdapterManifestError",
    "AdapterOption",
    "InProcessAdapterHost",
    "discover_adapters",
    "load_manifest",
    "parse_manifest",
    "validate_units",
]
