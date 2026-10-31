"""阿里百炼（通义千问）OpenAI-compatible translation engine."""

from config import DASHSCOPE_OPENAI_BASE_URL
from engines.openai_engine import OpenAIEngine


def dashscope_thinking_params(thinking: str) -> dict:
    """连接的「深度思考」设置换成百炼的请求参数；auto 不传，由模型自己决定。"""
    if thinking == "off":
        return {"enable_thinking": False}
    if thinking == "on":
        return {"enable_thinking": True}
    return {}


class DashscopeEngine(OpenAIEngine):

    def __init__(self, api_key: str, model: str, thinking: str = "auto"):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=DASHSCOPE_OPENAI_BASE_URL,
            engine_name_prefix="dashscope",
            response_label="百炼",
            chat_extra=dashscope_thinking_params(thinking),
        )
