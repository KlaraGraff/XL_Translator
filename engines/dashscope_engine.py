"""阿里百炼（通义千问）OpenAI-compatible translation engine."""

from config import DASHSCOPE_OPENAI_BASE_URL
from engines.openai_engine import OpenAIEngine


class DashscopeEngine(OpenAIEngine):

    def __init__(self, api_key: str, model: str = "qwen-max"):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=DASHSCOPE_OPENAI_BASE_URL,
            engine_name_prefix="dashscope",
            response_label="百炼",
        )
