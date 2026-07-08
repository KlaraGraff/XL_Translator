"""
全局常量（不可变默认值）。
用户可修改的配置在 settings.py 中管理。
"""
from urllib.parse import urlsplit, urlunsplit

from app_meta import APP_NAME as APP_NAME, APP_VERSION as APP_VERSION
from core.app_paths import get_app_data_dir

# ── 路径常量 ──────────────────────────────────────────────
APP_DATA_DIR  = get_app_data_dir()
DB_PATH       = APP_DATA_DIR / "tm.db"
SETTINGS_PATH = APP_DATA_DIR / "settings.json"
KEYS_PATH     = APP_DATA_DIR / "keys.json"
LOG_PATH      = APP_DATA_DIR / "app.log"
BACKUPS_DIR   = APP_DATA_DIR / "backups"

# ── TM 分流默认阈值 ───────────────────────────────────────
DEFAULT_MAX_LEN = 25

# ── 翻译引擎标识符 ────────────────────────────────────────
CLOUD_ENGINES = {
    "Claude (Anthropic)":          "claude",
    "OpenAI / ChatGPT":            "openai",
    "OpenAI 兼容":                  "custom_openai",
    "智谱 GLM":                    "zhipu",
    "阿里百炼 (通义)":             "dashscope",
    "硅基流动":                    "siliconflow",
}

IMAGE_GENERATION_MODEL_PROVIDERS = {
    "OpenAI / ChatGPT": "openai",
    "OpenAI 兼容": "custom_openai",
    "硅基流动": "siliconflow",
}

VISION_TEXT_MODEL_PROVIDERS = {
    "OpenAI / ChatGPT": "openai",
    "OpenAI 兼容": "custom_openai",
    "硅基流动": "siliconflow",
}

DEFAULT_CLOUD_PROVIDER = "custom_openai"
DEFAULT_CLOUD_MODEL = ""
DEFAULT_CUSTOM_OPENAI_BASE_URL = ""
DEFAULT_CUSTOM_OPENAI_API_KEY = ""

OPENAI_BASE_URL = "https://api.openai.com/v1"
CLAUDE_BASE_URL = "https://api.anthropic.com/v1"
SILICONFLOW_BASE_URL = "https://api.siliconflow.com/v1"
LANYI_BASE_URL = "http://1.95.142.151:3000/v1"
CLOUD_PROVIDER_BASE_URL_DEFAULTS = {
    "openai": OPENAI_BASE_URL,
    "claude": CLAUDE_BASE_URL,
    "siliconflow": SILICONFLOW_BASE_URL,
    "lanyi": LANYI_BASE_URL,
}
CLOUD_PROVIDER_BASE_URL_DISABLED = {"zhipu", "dashscope"}
DISABLED_BASE_URL_PLACEHOLDER = "当前服务商无需填写 Base URL"
LOCAL_MODEL_PROVIDERS = {
    "Ollama": "ollama",
    "LM Studio": "lm_studio",
    "自定义": "custom_local",
}
DEFAULT_LOCAL_MODEL_PROVIDER = "ollama"
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"

# ── 本地模型配置 ───────────────────────────────────────────
OLLAMA_BASE_URL   = "http://localhost:11434"
OLLAMA_TIMEOUT    = 120  # 秒


def cloud_provider_base_url_default(provider: str) -> str:
    return CLOUD_PROVIDER_BASE_URL_DEFAULTS.get(str(provider or "").strip(), "")


def cloud_provider_uses_base_url(provider: str) -> bool:
    return str(provider or "").strip() not in CLOUD_PROVIDER_BASE_URL_DISABLED


def normalize_cloud_base_url(provider: str, base_url: str) -> str:
    provider_name = str(provider or "").strip()
    if not cloud_provider_uses_base_url(provider_name):
        return ""
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return cloud_provider_base_url_default(provider_name).rstrip("/")

    parsed = urlsplit(normalized)
    if parsed.scheme and parsed.netloc and parsed.path in {"", "/"}:
        return urlunsplit(
            (parsed.scheme, parsed.netloc, "/v1", parsed.query, parsed.fragment)
        ).rstrip("/")
    return normalized

OLLAMA_RECOMMENDED_MODELS = [
    "qwen2.5:14b",
    "qwen2.5:32b",
    "deepseek-r1:14b",
    "deepseek-r1:32b",
    "llama3.1:8b",
    "mistral:7b",
]

# ── 专业领域 Prompt 预设 ──────────────────────────────────
# 结构：dict[domain, dict[lang, prompt]]
#   lang key：目标语言代码（如 "en"/"fr"/"ar"）或 "_base"（通用基础指令，兜底使用）
#   get_system_prompt() 优先取 target_lang，缺失时回退到 "_base"
DOMAIN_PRESETS: dict[str, dict[str, str]] = {
    "同步工程场景": {
        "_base": (
            "你是一名面向工程同步场景的专业翻译助手。\n"
            "请优先采用工程资料与项目沟通中的常用表达，保持术语前后一致。\n"
            "原文中的编号、日期、计量单位、规格参数、版本号与符号必须原样保留。\n"
            "输出应简洁、准确、可直接用于工程过程文件、往来沟通与进度同步材料。"
        ),
        "en": (
            "你是一名面向工程同步场景的专业翻译助手。\n"
            "请优先采用工程资料与项目沟通中的常用表达，保持术语前后一致。\n"
            "原文中的编号、日期、计量单位、规格参数、版本号与符号必须原样保留。\n"
            "输出应简洁、准确、可直接用于工程过程文件、往来沟通与进度同步材料。"
        ),
        "fr": (
            "Tu es un assistant de traduction professionnel pour la synchronisation de projets d’ingénierie.\n"
            "Utiliser des formulations courantes dans les documents techniques et la communication de projet, avec une terminologie cohérente.\n"
            "Conserver strictement inchangés les numéros, dates, unités, paramètres, versions et symboles du texte source.\n"
            "La traduction doit être concise, précise et directement exploitable dans les documents de suivi et de coordination."
        ),
    },
    "资料管理场景": {
        "_base": (
            "你是一名面向资料管理场景的专业翻译助手。\n"
            "请使用资料整理、归档、送审、台账与表单语境下的规范表达，保证字段名称一致。\n"
            "涉及编号、文号、日期、版本、附件标识时必须完整保留，不得改写结构。\n"
            "输出应便于资料员直接用于整理、流转、归档与审查。"
        ),
        "en": (
            "你是一名面向资料管理场景的专业翻译助手。\n"
            "请使用资料整理、归档、送审、台账与表单语境下的规范表达，保证字段名称一致。\n"
            "涉及编号、文号、日期、版本、附件标识时必须完整保留，不得改写结构。\n"
            "输出应便于资料员直接用于整理、流转、归档与审查。"
        ),
        "fr": (
            "Tu es un assistant de traduction professionnel pour la gestion documentaire.\n"
            "Employer des formulations normalisées adaptées au classement, à l’archivage, à la soumission, aux registres et aux formulaires, avec cohérence des champs.\n"
            "Conserver intégralement les numéros, références, dates, versions et identifiants de pièces jointes sans modifier la structure.\n"
            "Le résultat doit être directement réutilisable pour le tri, la circulation, l’archivage et la revue documentaire."
        ),
    },
    "行政生活化场景": {
        "_base": (
            "你是一名面向行政与日常办公场景的翻译助手。\n"
            "请使用自然、清晰、礼貌且易理解的通用表达，避免过强行业术语。\n"
            "保留原文中的数字、时间、地址、联系人、编号等关键信息，不改变事实含义。\n"
            "输出应适用于通知、邮件、流程说明、日常沟通与生活化文本。"
        ),
        "en": (
            "你是一名面向行政与日常办公场景的翻译助手。\n"
            "请使用自然、清晰、礼貌且易理解的通用表达，避免过强行业术语。\n"
            "保留原文中的数字、时间、地址、联系人、编号等关键信息，不改变事实含义。\n"
            "输出应适用于通知、邮件、流程说明、日常沟通与生活化文本。"
        ),
        "fr": (
            "Tu es un assistant de traduction pour l’administration et le bureau au quotidien.\n"
            "Utiliser un style naturel, clair, poli et facile à comprendre, sans surcharge de jargon technique.\n"
            "Conserver les informations clés du texte source (chiffres, dates, heures, adresses, contacts, références) sans altérer le sens factuel.\n"
            "La traduction doit convenir aux notifications, e-mails, consignes de processus, communications courantes et contenus de vie quotidienne."
        ),
    },
    "自定义": {},
}

# ── 动态批次控制（云端 / 本地模式分档）────────────────────
CHUNK_CLOUD_DEFAULT = 20
CHUNK_CLOUD_MIN     = 10
CHUNK_CLOUD_MAX     = 30
CHUNK_LOCAL_DEFAULT = 12
CHUNK_LOCAL_MIN     = 5
CHUNK_LOCAL_MAX     = 20

# ── Word 独立批次策略（段落比 Excel 单元格更长，默认更保守）──────────────
WORD_BATCH_PARAGRAPHS_DEFAULT = 8
WORD_BATCH_PARAGRAPHS_MIN     = 1
WORD_BATCH_PARAGRAPHS_MAX     = 16
WORD_BATCH_CHARS_DEFAULT      = 3000
WORD_BATCH_CHARS_MIN          = 800
WORD_BATCH_CHARS_MAX          = 12000
WORD_BATCH_SPLIT_CHARS_DEFAULT = 3000
WORD_BATCH_SPLIT_CHARS_MIN     = 1500
WORD_BATCH_SPLIT_CHARS_MAX     = 30000
WORD_STRICT_RETRY_ATTEMPTS_DEFAULT = 3
WORD_STRICT_RETRY_ATTEMPTS_MIN     = 1
WORD_STRICT_RETRY_ATTEMPTS_MAX     = 8
WORD_REVIEW_HIGHLIGHT_DEFAULT      = True
REVIEW_MARK_SEMANTIC = "semantic"
REVIEW_MARK_UNRESOLVED = "unresolved"
REVIEW_MARK_FOREIGN_NOISE = "foreign_noise"
REVIEW_MARK_COLOR_SEMANTIC_DEFAULT = "FFF2CC"
REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT = "FCE4D6"
REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT = "F4CCCC"
REVIEW_MARK_COLOR_DEFAULTS = {
    REVIEW_MARK_SEMANTIC: REVIEW_MARK_COLOR_SEMANTIC_DEFAULT,
    REVIEW_MARK_UNRESOLVED: REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT,
    REVIEW_MARK_FOREIGN_NOISE: REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT,
}
WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT = REVIEW_MARK_COLOR_SEMANTIC_DEFAULT
WORD_REVIEW_EXISTING_HIGHLIGHT_POLICY_DEFAULT = "skip"
EXCEL_REVIEW_MARK_DEFAULT = True
EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT = "red_font"

# ── PDF 翻译 ────────────────────────────────────────────
PDF_RENDER_DPI_DEFAULT = 300
PDF_PAGE_RETRY_ATTEMPTS_DEFAULT = 3
PDF_PAGE_RETRY_ATTEMPTS_MIN = 0
PDF_PAGE_RETRY_ATTEMPTS_MAX = 8
PDF_PAGE_CONCURRENCY_DEFAULT = 3
PDF_PAGE_CONCURRENCY_SAFETY_CAP = 20
PDF_PAGE_RENDER_AHEAD_COUNT = 2
PDF_ASPECT_RATIO_TOLERANCE = 0.01
PDF_MIN_READABLE_SHORT_EDGE_PX = 1200
PDF_MIN_READABLE_LONG_EDGE_PX = 1600
PDF_COMPRESSED_JPEG_QUALITY_DEFAULT = 85
PDF_COMPRESSED_MAX_LONG_EDGE_PX = 2200

# ── 全局并发控制（输入框按模式限制区间）────────────────────
CONCURRENCY_UNLOCK_CODE      = "OA"
CONCURRENCY_CLOUD_DEFAULT    = 20
CONCURRENCY_LOCAL_DEFAULT    = 3
CONCURRENCY_DEFAULT          = CONCURRENCY_CLOUD_DEFAULT
CONCURRENCY_CLOUD_MIN        = 1
CONCURRENCY_CLOUD_MAX        = 50
CONCURRENCY_CLOUD_LOCKED_MAX = 20
CONCURRENCY_LOCAL_MIN        = 1
CONCURRENCY_LOCAL_MAX        = 10
CONCURRENCY_LOCAL_LOCKED_MAX = 5


def is_valid_concurrency_unlock_code(code: str) -> bool:
    return code == CONCURRENCY_UNLOCK_CODE


def get_cloud_concurrency_bounds(unlocked: bool) -> tuple[int, int]:
    if unlocked:
        return CONCURRENCY_CLOUD_MIN, CONCURRENCY_CLOUD_MAX
    return CONCURRENCY_CLOUD_MIN, CONCURRENCY_CLOUD_LOCKED_MAX


def get_local_concurrency_bounds(unlocked: bool) -> tuple[int, int]:
    if unlocked:
        return CONCURRENCY_LOCAL_MIN, CONCURRENCY_LOCAL_MAX
    return CONCURRENCY_LOCAL_MIN, CONCURRENCY_LOCAL_LOCKED_MAX


def get_concurrency_bounds(mode: str, unlocked: bool) -> tuple[int, int]:
    if mode == "local":
        return get_local_concurrency_bounds(unlocked)
    return get_cloud_concurrency_bounds(unlocked)


def get_concurrency_cap() -> int:
    return max(CONCURRENCY_CLOUD_MAX, CONCURRENCY_LOCAL_MAX)


def get_default_concurrency(mode: str) -> int:
    if mode == "local":
        return CONCURRENCY_LOCAL_DEFAULT
    return CONCURRENCY_CLOUD_DEFAULT

# ── 目标语言（第二阶段内置扩容）──────────────────────────
SUPPORTED_LANGS: dict[str, str] = {
    "英文": "en",
    "法文": "fr",
    "阿拉伯语": "ar",
    "越南语": "vi",
    "柬埔寨语（高棉语）": "km",
    "西班牙语": "es",
    "葡萄牙语": "pt",
    "德文": "de",
    "意大利语": "it",
    "俄语": "ru",
    "日语": "ja",
    "韩语": "ko",
    "泰语": "th",
    "印度尼西亚语": "id",
    "马来语": "ms",
    "菲律宾语（他加禄语）": "tl",
    "印地语": "hi",
    "孟加拉语": "bn",
    "乌尔都语": "ur",
    "波斯语": "fa",
    "土耳其语": "tr",
    "希伯来语": "he",
    "希腊语": "el",
    "波兰语": "pl",
    "荷兰语": "nl",
    "瑞典语": "sv",
    "丹麦语": "da",
    "挪威语": "no",
    "芬兰语": "fi",
    "捷克语": "cs",
    "斯洛伐克语": "sk",
    "斯洛文尼亚语": "sl",
    "匈牙利语": "hu",
    "罗马尼亚语": "ro",
    "保加利亚语": "bg",
    "乌克兰语": "uk",
    "塞尔维亚语": "sr",
    "克罗地亚语": "hr",
    "立陶宛语": "lt",
    "拉脱维亚语": "lv",
    "爱沙尼亚语": "et",
    "斯瓦希里语": "sw",
    "阿姆哈拉语": "am",
    "泰米尔语": "ta",
    "泰卢固语": "te",
    "马拉雅拉姆语": "ml",
    "卡纳达语": "kn",
    "马拉地语": "mr",
    "古吉拉特语": "gu",
    "旁遮普语": "pa",
    "尼泊尔语": "ne",
    "僧伽罗语": "si",
    "缅甸语": "my",
    "老挝语": "lo",
    "蒙古语": "mn",
    "哈萨克语": "kk",
    "乌兹别克语": "uz",
    "阿塞拜疆语": "az",
}

# 中文仅作为“可选目标语言补充项”提供给特定场景使用，
# 不进入默认目标语言顺序，避免改变现有默认值与旧流程体验。
OPTIONAL_TARGET_LANGS: dict[str, str] = {
    "中文": "zh",
}

# 源语言候选集合允许中文 + 当前所有内置语种。
SUPPORTED_SOURCE_LANGS: dict[str, str] = {
    "中文": "zh",
    **SUPPORTED_LANGS,
}

# ── 重试配置 ──────────────────────────────────────────────
RETRY_MAX_ATTEMPTS = 3
RETRY_WAIT_MIN     = 0.7   # 秒
RETRY_WAIT_MAX     = 5.0   # 秒

# ── A4 打印保护 ───────────────────────────────────────────
PRINT_GUARD_LINE_HEIGHT_MULTIPLIER = 1.35   # 估算单行高度 = 字号 * 此系数
PRINT_GUARD_FONT_STEP              = 0.5    # 每次缩小步长（磅）
PRINT_GUARD_FONT_FLOOR             = 6.0    # 字号触底阈值（磅）

# ── 双语回填 ──────────────────────────────────────────────
BILINGUAL_SEPARATOR = "\n"   # 原文与译文之间的分隔符

# ── 应用版本 / 元信息 ─────────────────────────────────────
# 版本元信息已迁移至 app_meta.py；这里保留 re-export 兼容旧导入。
SETTINGS_SCHEMA_VERSION = 24
