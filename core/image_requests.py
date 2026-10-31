"""图片请求：把图片发给模型、取回结果的统一入口。

「图像翻译」（出图）和「图像翻译审核」（看图）以前各写一套发图片的代码，前面再各挡一张
服务商名单：名单外的服务商一律不放行，哪怕它的看图模型完全能用（如智谱、阿里百炼，
它们本来就是 OpenAI 兼容接口）。

这里改成按**接口写法**决定图片怎么发，而不是按服务商名字：

* 看图：每家服务商登记一种图片写法（``PROVIDER_IMAGE_FORMATS``，没登记的按 OpenAI 写法）。
  本软件实现了哪种写法，用这种写法的服务商就都能看图；模型本身能不能看图由「测试连接」
  实际发一张小图来判断。新增一家 OpenAI 兼容的服务商不用改这里任何一行。
* 出图：各家接口差别很大，只接已确认过的方式（``IMAGE_GENERATION_METHODS``），不替其他
  服务商去猜。要接新的一家，在这里给它登记一种方式、在 image_generation 里实现即可。

这一层不依赖 model_roles，只认服务商 id 字符串，model_roles 的能力判断反过来调用这里。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from config import (
    DASHSCOPE_OPENAI_BASE_URL,
    ZHIPU_OPENAI_BASE_URL,
    is_siliconflow_endpoint,
    normalize_cloud_base_url,
)

# ── 图片写法 ────────────────────────────────────────────────
OPENAI_FORMAT = "openai"
ANTHROPIC_FORMAT = "anthropic"

# 每家服务商发图片用的写法。没列出的服务商（包括以后新增的 OpenAI 兼容服务商）按 OpenAI 写法。
PROVIDER_IMAGE_FORMATS: dict[str, str] = {
    "claude": ANTHROPIC_FORMAT,
}

# 本软件已经实现的看图写法。Anthropic 的写法尚未实现，接入时按其官方文档补上。
IMPLEMENTED_VISION_FORMATS = frozenset({OPENAI_FORMAT})

# 本地运行器只做文字翻译，不参与图片请求。
LOCAL_PROVIDERS = frozenset({"ollama", "lm_studio", "custom_local"})


# 设置里不让填 Base URL 的服务商（文本翻译引擎里写死了地址），图片请求用同一个地址。
_FIXED_OPENAI_BASE_URLS = {
    "zhipu": ZHIPU_OPENAI_BASE_URL,
    "dashscope": DASHSCOPE_OPENAI_BASE_URL,
}


def image_base_url(provider: str, base_url: str) -> str:
    """图片请求发往的接口地址（不带结尾斜杠）。"""
    fixed = _FIXED_OPENAI_BASE_URLS.get(str(provider or "").strip())
    if fixed:
        return fixed.rstrip("/")
    return normalize_cloud_base_url(provider, base_url)


def provider_image_format(provider: str) -> str:
    return PROVIDER_IMAGE_FORMATS.get(str(provider or "").strip(), OPENAI_FORMAT)


def supports_vision(provider: str) -> bool:
    """这家服务商的看图请求本软件是否已经能发（不代表所选模型一定能看图）。"""
    value = str(provider or "").strip()
    if not value or value in LOCAL_PROVIDERS:
        return False
    return provider_image_format(value) in IMPLEMENTED_VISION_FORMATS


# ── 看图：OpenAI 写法的两种接口 ─────────────────────────────
# 没有 OpenAI ``/responses`` 接口、只能走 ``/chat/completions`` 的服务商。
# DeepSeek 的图片输入以 /chat/completions 为准（官方视觉指南的首选写法）；小米 MiMo、
# 智谱（GLM 视觉模型）、阿里百炼兼容模式（通义千问 VL）的官方看图示例同样只有
# /chat/completions，不去试 /responses。
CHAT_COMPLETIONS_ONLY_PROVIDERS = frozenset(
    {"siliconflow", "deepseek", "xiaomi", "zhipu", "dashscope"}
)
VISION_ROUTE_PATHS = {"chat": "chat/completions", "responses": "responses"}


def vision_routes(provider: str, base_url: str, api_mode: str = "") -> list[str]:
    """看图请求依次尝试的接口，和文本翻译共用连接上的「文本请求协议」设置。

    指定了协议就只走那一种；自动时先走 ``/responses``（OpenAI 与多数中转站已验证），
    对方没有这个接口再换 chat。只有 chat 接口的服务商直接走 chat。
    """
    mode = str(api_mode or "auto").strip()
    mode = {"codex_responses": "responses", "chat_completions": "chat"}.get(mode, mode)
    if mode in VISION_ROUTE_PATHS:
        return [mode]
    if provider in CHAT_COMPLETIONS_ONLY_PROVIDERS or is_siliconflow_endpoint(provider, base_url):
        return ["chat"]
    return ["responses", "chat"]


def image_data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_vision_payload(
    route: str,
    *,
    model: str,
    instructions: str,
    user_text: str,
    images: list[Path],
) -> dict[str, Any]:
    """同一组「说明 + 文字 + 若干图片」按接口写成请求体。"""
    if route == "chat":
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        content += [{"type": "image_url", "image_url": {"url": image_data_url(p)}} for p in images]
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": content},
            ],
            "stream": False,
        }
    content = [{"type": "input_text", "text": user_text}]
    content += [{"type": "input_image", "image_url": image_data_url(p)} for p in images]
    return {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": content}],
        "store": False,
        "stream": False,
    }


# ── 出图：只接已确认过的方式 ───────────────────────────────
# openai：gpt-image 系列走 /images/edits，其余模型走 /responses 的 image_generation 工具。
# siliconflow：硅基流动自己的 /images/generations（原图放在 image 字段）。
IMAGE_GENERATION_METHODS: dict[str, str] = {
    "openai": "openai",
    "custom_openai": "openai",
    "siliconflow": "siliconflow",
}


def supports_image_generation(provider: str) -> bool:
    return str(provider or "").strip() in IMAGE_GENERATION_METHODS


def image_generation_method(provider: str, base_url: str = "") -> str:
    """这条连接的出图方式；「自定义配置」填了硅基流动的地址时也走硅基流动的方式。"""
    if is_siliconflow_endpoint(provider, base_url):
        return "siliconflow"
    return IMAGE_GENERATION_METHODS.get(str(provider or "").strip(), "")
