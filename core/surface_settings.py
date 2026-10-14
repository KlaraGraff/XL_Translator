"""按翻译页面取设置：内置页面读平铺字段，运行时注册的类型读 ``settings.extensions``。

内置四个页面的设置是 ``AppSettings`` 上按页面名平铺的字段（``excel_target_lang``、
``word_smart_context``、``cad_domain_custom_prompts`` …），以前各处用
``getattr(settings, f"{surface}_target_lang", 默认值)`` 取。运行时注册的类型（键为
``ext:<id>``）没有这些字段，它的设置在 ``settings.extensions[<id>]``（v39 起）。

``surface_setting`` 对内置页面与原来的 ``getattr`` 完全一样（字段不存在时返回给定的
默认值），所以改用它不改变内置页面的任何行为。
"""

from __future__ import annotations

from typing import Any

from settings import AppSettings, ExtensionSettings

#: 运行时注册的类型的页面键前缀，如 ``ext:srt``。
EXTENSION_PREFIX = "ext:"
#: 运行时注册的类型的模型页面键（``Surface.page_key``）：只用文档翻译模型。
EXTENSION_PAGE_KEY = "adapter"

_MISSING: Any = object()


def extension_id(key: str) -> str | None:
    """``ext:<id>`` 里的 id；不是运行时注册的类型时为 None。"""
    normalized = str(key or "").strip().lower()
    if not normalized.startswith(EXTENSION_PREFIX):
        return None
    return normalized[len(EXTENSION_PREFIX):] or None


def is_extension_key(key: str) -> bool:
    return extension_id(key) is not None


def extension_settings(settings: AppSettings, key: str) -> ExtensionSettings:
    """某个运行时注册的类型的设置；还没存过时给一份默认值（不写回 ``settings``）。"""
    ext_id = extension_id(key)
    if ext_id is None:
        raise ValueError(f"not an extension surface key: {key!r}")
    return settings.extensions.get(ext_id) or ExtensionSettings()


def surface_setting(settings: AppSettings, key: str, name: str, default: Any = _MISSING) -> Any:
    """页面 ``key`` 的设置项 ``name``（如 ``target_lang``、``smart_context``）。

    内置页面读 ``settings.<key>_<name>``，运行时注册的类型读
    ``settings.extensions[<id>].<name>``。没有这一项时返回 ``default``；没给默认值则
    抛 ``AttributeError``，与 ``getattr`` 相同。
    """
    if is_extension_key(key):
        source: object = extension_settings(settings, key)
        attribute = name
    else:
        source = settings
        attribute = f"{str(key or '').strip().lower()}_{name}"
    if default is _MISSING:
        return getattr(source, attribute)
    return getattr(source, attribute, default)


def set_surface_setting(settings: AppSettings, key: str, name: str, value: Any) -> None:
    """写页面 ``key`` 的设置项 ``name``；该页面没有这一项时抛 ``AttributeError``。"""
    ext_id = extension_id(key)
    if ext_id is not None:
        target = settings.extensions.get(ext_id)
        if target is None:
            target = settings.extensions[ext_id] = ExtensionSettings()
        if name not in ExtensionSettings.model_fields:
            raise AttributeError(f"extension settings have no {name!r}")
        setattr(target, name, value)
        return
    attribute = f"{str(key or '').strip().lower()}_{name}"
    if attribute not in AppSettings.model_fields:
        raise AttributeError(f"settings have no {attribute!r}")
    setattr(settings, attribute, value)
