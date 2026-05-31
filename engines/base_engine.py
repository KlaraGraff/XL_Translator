"""
翻译引擎抽象基类。
所有具体引擎必须继承此类并实现 translate_batch()。

公共常量与工具方法：
  TASK_INSTRUCTION      — 统一翻译任务指令模板（各引擎共用）
  get_source_lang_name  — 源语言代码 → 中文显示名
  get_target_lang_name  — 目标语言代码 → 中文显示名
  strip_markdown_json() — 健壮剥离 LLM 响应中的 markdown 代码块
  parse_response()      — 通用 JSON 数组响应解析，降级返回原文
"""
import json
import re
from abc import ABC, abstractmethod

from core.language_registry import get_source_lang_display, get_target_lang_display


# ── 公共常量 ──────────────────────────────────────────────

TASK_INSTRUCTION = (
    "你的任务是将以下 JSON 数组中的每个词条从{source_lang_name}翻译为{target_lang_name}。\n"
    "请严格保持 JSON 数组格式输出，不要添加任何解释或 markdown 代码块。\n"
    "输出的数组长度必须与输入完全一致，顺序一一对应。\n"
    "翻译规则：\n"
    "  1. 必须翻译词条中的所有源语言成分，禁止只输出原词条中已有的其他语言/型号/符号部分；\n"
    "  2. 参数间的连接符（*/×/x）或空格允许适当调整，但严禁修改、截断或省略任何数值；\n"
    "  3. 括号组（如（200×200））和规格参数（如 DN100）必须完整保留，不得截断；\n"
    "  4. 原文中的专名、机构名、地点名、型号、单位、标准号和缩写应在译文中保留或按目标语言习惯处理，不得误删；\n"
    "  5. 每一项仅返回目标语言译文，不要返回原文、解释、备注或多版本；\n"
    "  6. JSON 数组中的每一项都必须是字符串，严禁返回 null。"
)


def get_source_lang_name(source_lang: str) -> str:
    return get_source_lang_display(source_lang)


def get_target_lang_name(target_lang: str) -> str:
    return get_target_lang_display(target_lang, include_optional=True)


# ── 工具函数 ──────────────────────────────────────────────

def strip_markdown_json(raw: str) -> str:
    """
    健壮剥离 LLM 响应中可能包裹的 markdown 代码块，提取 JSON 内容。
    支持以下格式：
      ```json\n...\n```
      ```\n...\n```
      以及前置有说明文字的情况（取第一个完整代码块）
    若无代码块则原样返回。
    """
    raw = raw.strip()
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw)
    if m:
        return m.group(1).strip()
    return raw


def parse_response(originals: list[str], raw: str, engine_label: str = "") -> dict[str, str]:
    """
    通用 JSON 数组响应解析。
    先剥离 markdown 代码块，再解析 JSON，长度校验通过后返回 {原文: 译文}。
    任何解析失败均降级返回 {原文: 原文}，并记录 warning 日志。
    """
    cleaned = strip_markdown_json(raw)
    try:
        translations = json.loads(cleaned)
        if isinstance(translations, list) and len(translations) == len(originals):
            return {
                src: ("" if tgt is None else str(tgt))
                for src, tgt in zip(originals, translations)
            }
    except json.JSONDecodeError:
        pass
    label = f"{engine_label} " if engine_label else ""
    raise ValueError(f"{label}响应解析失败 (无法匹配为包含 {len(originals)} 条记录的数组)，大模型原始内容：{raw[:200]}")


# ── 抽象基类 ──────────────────────────────────────────────

class TranslationEngine(ABC):

    @abstractmethod
    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        """
        批量翻译。
        :param texts:         待翻译词条列表
        :param target_lang:   目标语言代码，如 'en'、'fr'、'ar' 等
        :param system_prompt: 领域专属 System Prompt
        :param source_lang:   源语言代码，如 'zh'、'en'、'fr' 等；默认兼容旧流程为 'zh'
        :return:              {原文: 译文} 字典，条数与 texts 一致
        """

    def chat(self, system: str, user: str) -> str:
        """
        直接发起单次 API 调用，不附加任何翻译格式指令。
        用于清洗、评估等非批量翻译场景。
        子类可按需覆盖；默认抛出 NotImplementedError。
        """
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 chat() 方法")

    @property
    def engine_name(self) -> str:
        """引擎标识名，用于 TM 记录 source_engine 字段。"""
        return self.__class__.__name__
