"""智谱 GLM OpenAI-compatible translation engine."""

from config import ZHIPU_OPENAI_BASE_URL
from engines.openai_engine import OpenAIEngine


class ZhipuEngine(OpenAIEngine):

    def __init__(self, api_key: str, model: str = "glm-4"):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=ZHIPU_OPENAI_BASE_URL,
            engine_name_prefix="zhipu",
            response_label="智谱",
        )
