"""模型服务是不是就跑在这台电脑上。

用户可以把本机的 Ollama、LM Studio 接成「本地模型」，也可以接成「自定义（OpenAI 兼容）」
这类云端连接、地址填 ``http://127.0.0.1:11434/v1``。后一种以前一律按云端处理：一次同时发
30 批、每批按云端的条数和字数。本机模型一次只算得动一两批，30 批同时排队，每批要等好几分钟，
等不到的就保留了原文（反馈 #24：3 核、8GB 内存的 Windows 电脑，PDF 一页只翻了一部分）。

这里只认「本机」地址（localhost、127.x、::1、0.0.0.0）。局域网里另一台机器上的模型服务算力
多少看不出来，仍按用户选的连接类型处理。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_LOCAL_HOST_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def is_this_computer_url(base_url: object) -> bool:
    """``base_url`` 指向本机时为 True；没填、填错或指向别的机器时为 False。"""
    text = str(base_url or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = f"http://{text}"
    try:
        host = (urlsplit(text).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    if host in _LOCAL_HOST_NAMES or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def config_runs_locally(config: object) -> bool:
    """一个模型连接（``EffectiveModelConfig``）的模型是不是在本机算：本地连接，或地址是本机。"""
    if str(getattr(config, "mode", "") or "").strip() == "local":
        return True
    return is_this_computer_url(getattr(config, "base_url", ""))


def settings_runs_locally(settings: object) -> bool:
    """按这份设置建出来的文本翻译引擎是不是在本机算。

    ``settings`` 是 ``build_engine`` 用的那份（多半已由 ``settings_for_text_role`` 换成这一用途
    的连接）：本地模式，或云端连接的 Base URL 指向本机。
    """
    engine = getattr(settings, "engine", None)
    if engine is None:
        return False
    if str(getattr(engine, "mode", "") or "").strip() == "local":
        return True
    return is_this_computer_url(engine_cloud_base_url(engine))


def engine_cloud_base_url(engine: object) -> str:
    """云端连接这次实际用的 Base URL（与 ``build_engine`` 取法相同）。"""
    try:
        from settings import get_cloud_provider_config

        provider = str(getattr(engine, "cloud_provider", "") or "").strip()
        return str(get_cloud_provider_config(engine, provider).cloud_base_url or "")
    except Exception:  # noqa: BLE001 - a half-configured engine is simply not local
        return str(getattr(engine, "cloud_base_url", "") or "")


def runtime_mode(config: object) -> str | None:
    """分速率档用的模式：模型在本机算就是 ``"local"``，否则照连接自己的模式（没有时 None）。"""
    if config is None:
        return None
    if config_runs_locally(config):
        return "local"
    return str(getattr(config, "mode", "") or "").strip() or None
