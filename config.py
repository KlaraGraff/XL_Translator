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
    "Anthropic":                   "claude",
    "OpenAI / ChatGPT":            "openai",
    "自定义配置":                    "custom_openai",
    "智谱 GLM":                    "zhipu",
    "阿里百炼 (通义)":             "dashscope",
    "硅基流动":                    "siliconflow",
    "DeepSeek":                    "deepseek",
}

IMAGE_GENERATION_MODEL_PROVIDERS = {
    "OpenAI / ChatGPT": "openai",
    "自定义配置": "custom_openai",
    "硅基流动": "siliconflow",
}

# 看图审核：能读图片输入的服务商。DeepSeek 自 2026-08-21 起 API 接受图片输入
# （现为 deepseek-flash；deepseek-v4-pro 不支持，测试连接会如实报错）。它不能生成
# 图片，所以不在上面的图像翻译名单里。
VISION_TEXT_MODEL_PROVIDERS = {
    "OpenAI / ChatGPT": "openai",
    "自定义配置": "custom_openai",
    "硅基流动": "siliconflow",
    "DeepSeek": "deepseek",
}

DEFAULT_CLOUD_PROVIDER = "custom_openai"
DEFAULT_CLOUD_MODEL = ""
DEFAULT_CUSTOM_OPENAI_BASE_URL = ""
DEFAULT_CUSTOM_OPENAI_API_KEY = ""

OPENAI_BASE_URL = "https://api.openai.com/v1"
CLAUDE_BASE_URL = "https://api.anthropic.com/v1"
# 默认国内站：国内用户在 cloud.siliconflow.cn 申请的密钥，在国际站 api.siliconflow.com
# 会被拒（Token is invalid）。国际站用户把 Base URL 改成 .com 即可。
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
LANYI_BASE_URL = "http://1.95.142.151:3000/v1"
ZHIPU_OPENAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DASHSCOPE_OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
CLOUD_PROVIDER_BASE_URL_DEFAULTS = {
    "openai": OPENAI_BASE_URL,
    "claude": CLAUDE_BASE_URL,
    "siliconflow": SILICONFLOW_BASE_URL,
    "lanyi": LANYI_BASE_URL,
    "deepseek": DEEPSEEK_BASE_URL,
}
CLOUD_PROVIDER_BASE_URL_DISABLED = {"zhipu", "dashscope"}
DISABLED_BASE_URL_PLACEHOLDER = "当前服务商无需填写 Base URL"

# 各家创建 API 密钥的控制台页面。设置页选中服务商时给一个「获取 ↗ API 密钥」链接。
# 不预填模型名称：各家型号换代太快，写死的推荐很快过时，模型由用户获取列表后自己选。
CLOUD_PROVIDER_API_KEY_PAGES = {
    "openai": "https://platform.openai.com/api-keys",
    "claude": "https://console.anthropic.com/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "zhipu": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
    "dashscope": "https://bailian.console.aliyun.com/?tab=model#/api-key",
    "siliconflow": "https://cloud.siliconflow.cn/account/ak",
}
# 同一家国内站、国际站账号不通用：Base URL 落在国际站时，链接也指向国际站控制台。
CLOUD_API_KEY_PAGES_BY_HOST = {
    "api.siliconflow.com": "https://cloud.siliconflow.com/account/ak",
}
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
CLOUD_REQUEST_TIMEOUT = 120  # 单次文本模型请求上限（秒）


def cloud_provider_base_url_default(provider: str) -> str:
    return CLOUD_PROVIDER_BASE_URL_DEFAULTS.get(str(provider or "").strip(), "")


def is_siliconflow_endpoint(provider: str, base_url: str = "") -> bool:
    """硅基流动：选了这家，或用「自定义配置」填了它的地址。它没有 OpenAI 的 ``/responses``
    和 ``/images/edits``，图像与审核请求要走它自己的接口。"""
    if str(provider or "").strip() == "siliconflow":
        return True
    host = (urlsplit(str(base_url or "").strip()).hostname or "").lower()
    return host.endswith(("siliconflow.cn", "siliconflow.com"))


def cloud_provider_uses_base_url(provider: str) -> bool:
    return str(provider or "").strip() not in CLOUD_PROVIDER_BASE_URL_DISABLED


def configured_cloud_base_url(provider: str, base_url: str) -> str:
    """Keep the user's request path; credential/scheduler scopes retain legacy normalization."""
    if not cloud_provider_uses_base_url(provider):
        return ""
    return str(base_url or "").strip().rstrip("/") or cloud_provider_base_url_default(provider).rstrip("/")


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

# ── 翻译提示词（V10 起分两层）──────────────────────────────
# 发给文档翻译模型的 system prompt 由程序拼成：
#   1. CORE_TRANSLATION_PROMPT —— 内置规则，用户看得到、改不了。底线（编号原样保留、
#      日期单位换写法、只返回可直接放回原文的译文）都在这里，用户的补充要求怎么写都删不掉。
#   2. 「自动识别场景」开着时，翻译前现写的文档说明（core/document_profile.py）。
#   3. 用户自己的「补充要求」（设置页里新建的、选中的那一条），只追加，不替换前两段。
# 返回格式（JSON 数组、条数一致）另由各引擎的 TASK_INSTRUCTION 固定追加，见
# engines/base_engine.py。
CORE_TRANSLATION_PROMPT = (
    "你是一名专业文档翻译助手。\n"
    "请按文档所属专业的常用表达翻译，术语前后一致。\n"
    "单字、短词和缩写的含义以附带的文档说明为准；没有说明时按文档所属专业理解，不要按日常含义直译。\n"
    "原文中的编号、文号、规格参数、版本号、附件标识与符号必须原样保留，一个字符都不能改动。\n"
    "日期、时间、年龄、金额等数值中的数字保持不变，但其中的中文计量用语（年、月、日、时、分、周岁、岁、元等）须换成目标语言的书写习惯；国际通用的数字与计量单位（如 38℃、30%、m²）本就不含中文，无需改动。\n"
    "目标语言不用「万」「亿」计数时（如英文、法文），带「万」「亿」的数须换算成完整的数值（如 12 万 → 120 000），译文里不留「万」「亿」。\n"
    "输出应简洁、准确，可直接放回原文档。"
)
# 用户补充要求接在内置规则后面时的引导语：补充要求不能推翻上面的底线。
EXTRA_PROMPT_HEADER = "在不违反以上规则的前提下，同时遵守以下补充要求："
# 「补充要求」选「无」时存的值。
NO_EXTRA_PROMPT = "无"
# V9.x–V10 测试版的场景预设名。v35 起它们不再是可选项：旧设置和旧导出文件里出现时
# 由 settings.migrate_prompt_layers 转换（选中的转成「自动识别场景」开关；改过的正文
# 转成一条补充要求），这里只留名字供迁移和重名检查用。
LEGACY_DOMAIN_PRESET_NAMES = ("自动识别场景", "同步工程场景", "资料管理场景", "行政生活化场景")
# 补充要求不能叫这些名字：「无」是空选项，「自定义」是 V9 旧版单条提示词的标记值。
RESERVED_EXTRA_PROMPT_NAMES = frozenset({NO_EXTRA_PROMPT, "自定义", *LEGACY_DOMAIN_PRESET_NAMES})

# ── 动态批次控制（云端 / 本地模式分档）────────────────────
CHUNK_CLOUD_DEFAULT = 20
CHUNK_CLOUD_MIN     = 10
CHUNK_CLOUD_MAX     = 30
CHUNK_LOCAL_DEFAULT = 10
CHUNK_LOCAL_MIN     = 5
CHUNK_LOCAL_MAX     = 20

# ── Word 独立批次策略（段落比 Excel 单元格更长，默认更保守）──────────────
WORD_BATCH_PARAGRAPHS_DEFAULT = 8
WORD_BATCH_PARAGRAPHS_MIN     = 1
WORD_BATCH_PARAGRAPHS_MAX     = 8
WORD_BATCH_CHARS_DEFAULT      = 800
WORD_BATCH_CHARS_MIN          = 800
WORD_BATCH_CHARS_MAX          = 12000
WORD_BATCH_SPLIT_CHARS_DEFAULT = WORD_BATCH_CHARS_DEFAULT
WORD_STRICT_RETRY_ATTEMPTS_DEFAULT = 3
WORD_STRICT_RETRY_ATTEMPTS_MIN     = 1
WORD_STRICT_RETRY_ATTEMPTS_MAX     = 8

# Excel / CAD：接口临时故障（5xx、超时、回答中断）后同一批原样重发的次数，
# 在「翻译参数」里按页面设置。0 表示不重发。
REQUEST_RETRY_ATTEMPTS_DEFAULT = 2
REQUEST_RETRY_ATTEMPTS_MIN     = 0
REQUEST_RETRY_ATTEMPTS_MAX     = 5

# ── 速率与重试（v38 起在「翻译参数」里按文件类型设置，见 core/surface_rate.py）──
# 每批字符上限：一批内全部原文的字符数之和。单条超过上限时单独成批。
RATE_BATCH_CHARS_CLOUD_DEFAULT = 1000
RATE_BATCH_CHARS_LOCAL_DEFAULT = 800
RATE_BATCH_CHARS_MIN           = WORD_BATCH_CHARS_MIN
RATE_BATCH_CHARS_MAX           = WORD_BATCH_CHARS_MAX
# 单段重试次数：拆到最小仍没译出来、或译文没通过检查的那一条单独再发的次数。
RATE_ITEM_RETRY_DEFAULT = 3
RATE_ITEM_RETRY_MIN     = 0
RATE_ITEM_RETRY_MAX     = 8

# 任务过程文件（APP_DATA_DIR/logs/process 下每次任务一个目录：运行摘要、断点、CAD 译文
# 对照表、PDF 单页重跑用的页面文件）的自动保留上限，在「设置 → 数据与维护」里可调。
# 「正常完成」与「需复核或出错」各一组，条数或体积任一先到就从最早的开始清理。
# 正常完成原先只留 5 次：任务中心留 200 条记录，第 6 次以前的 CAD 任务点「查看与修改
# 译文」就只剩「记录已被清理」；体积上限主要防 PDF（每页几 MB 的渲染图）。
PROCESS_RETENTION_SUCCEEDED_RUNS_DEFAULT = 20
PROCESS_RETENTION_FAILED_RUNS_DEFAULT    = 30
PROCESS_RETENTION_RUNS_MIN               = 1
PROCESS_RETENTION_RUNS_MAX               = 1000
PROCESS_RETENTION_MB_DEFAULT             = 1024
PROCESS_RETENTION_MB_MIN                 = 100
PROCESS_RETENTION_MB_MAX                 = 100 * 1024
WORD_REVIEW_HIGHLIGHT_DEFAULT      = True
# 复核底色只有两种，对应两类真正需要人动手的东西：译文没出来（保留了原文、
# 译文里残留源语言）和原文本身可疑。程序自己判过并放行的不上底色——9.3.0 之前
# 还有第三种 "semantic"（语义校验接受），但那是"已经没事了"的记录，涂上去只会
# 把满篇底色变成没有底色，9.3.1 整类删除。旧配置里残留的 "semantic" 颜色会在
# settings 归一化时自动丢弃（那个循环只认这张表里的键），不需要迁移。
REVIEW_MARK_UNRESOLVED = "unresolved"
REVIEW_MARK_FOREIGN_NOISE = "foreign_noise"
REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT = "FCE4D6"
REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT = "F4CCCC"
REVIEW_MARK_COLOR_DEFAULTS = {
    REVIEW_MARK_UNRESOLVED: REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT,
    REVIEW_MARK_FOREIGN_NOISE: REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT,
}
# 译文没有采用记忆库里「始终采用」的设定译法（重译后仍未采用）。它要人动手，
# 但不另设颜色、不多一个配色选项：沿用「需复核」那一种底色，只在复核清单里
# 写明问题是「未采用设定译法」。
REVIEW_MARK_TERM_NOT_APPLIED = "term_not_applied"
# 锁定行高时缩到最小字号仍放不下：要人去调行高或删减文字，同样沿用「需复核」底色。
REVIEW_MARK_LOCKED_ROW_OVERFLOW = "locked_row_overflow"
# 译文里的证件号、日期或人名与原文对不上（core/key_info_check.py），同样沿用「需复核」底色。
REVIEW_MARK_KEY_INFO = "key_info"
REVIEW_MARK_COLOR_ALIASES = {
    REVIEW_MARK_TERM_NOT_APPLIED: REVIEW_MARK_UNRESOLVED,
    REVIEW_MARK_LOCKED_ROW_OVERFLOW: REVIEW_MARK_UNRESOLVED,
    REVIEW_MARK_KEY_INFO: REVIEW_MARK_UNRESOLVED,
}
# 分三色之前只有一个 word_review.highlight_color。这个值是当年的默认值，留在这里
# 只当迁移哨兵用：旧配置里**没有 mark_colors**、而 highlight_color 又与它不同时，
# 说明用户在单色时代自己调过色，那就把两种标记都涂成用户调的那个色。已经有
# mark_colors 的配置直接按 mark_colors 走，不看这个值（见 settings 里的
# _review_mark_colors_from_payload）。
WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT = "FFF2CC"
WORD_REVIEW_EXISTING_HIGHLIGHT_POLICY_DEFAULT = "skip"
EXCEL_REVIEW_MARK_DEFAULT = True
EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT = "red_font"

# ── PDF 翻译 ────────────────────────────────────────────
PDF_RENDER_DPI_DEFAULT = 300
PDF_PAGE_RETRY_ATTEMPTS_DEFAULT = 1
PDF_PAGE_RETRY_ATTEMPTS_MIN = 0
PDF_PAGE_RETRY_ATTEMPTS_MAX = 8
PDF_PAGE_CONCURRENCY_DEFAULT = 10
# 逐页审核同时进行的页数（开启审核时，云端；本地模型按 CONCURRENCY_LOCAL_DEFAULT）。
PDF_REVIEW_CONCURRENCY_DEFAULT = 10
PDF_PAGE_CONCURRENCY_SAFETY_CAP = 20
PDF_PAGE_RENDER_AHEAD_COUNT = 2
PDF_ASPECT_RATIO_TOLERANCE = 0.01
PDF_MIN_READABLE_SHORT_EDGE_PX = 1200
PDF_MIN_READABLE_LONG_EDGE_PX = 1600
PDF_COMPRESSED_JPEG_QUALITY_DEFAULT = 85
PDF_COMPRESSED_MAX_LONG_EDGE_PX = 2200

# ── 跳过大幅面页（超大页判定）────────────────────────────────
# ISO 216 A4 物理尺寸，单位 pt（1/72 in）。工程 PDF 里的说明书正文几乎总是
# A4，附带的 CAD 图纸多为 A3 及以上；这是本判定纯按纸张尺寸识别「大幅面页」
# 的原始动机，但判定本身只看页面尺寸和 A4 的倍数关系，跟页面内容是不是图纸
# 无关——不送模型、原样矢量保留。
PDF_A4_LONG_EDGE_PT = 841.89
PDF_A4_SHORT_EDGE_PT = 595.276
# A3 相对 A4 长边、短边都精确放大 1.414 倍；真实 A4 导出（打印留边、扫描时
# 多出来的白边）实测常见超出也就几个百分点。1.15 卡在两者正中间，能吃掉正常的
# A4 超出，又不会把「稍大一点的 A4」误判成大幅面页。
PDF_OVERSIZED_PAGE_EDGE_FACTOR = 1.15

# ── 全局并发控制（输入框按模式限制区间）────────────────────
CONCURRENCY_UNLOCK_CODE      = "OA"
CONCURRENCY_CLOUD_DEFAULT    = 30
CONCURRENCY_LOCAL_DEFAULT    = 3
CONCURRENCY_DEFAULT          = CONCURRENCY_CLOUD_DEFAULT
CONCURRENCY_CLOUD_MIN        = 1
CONCURRENCY_CLOUD_MAX        = 50
CONCURRENCY_CLOUD_LOCKED_MAX = 50
CONCURRENCY_LOCAL_MIN        = 1
CONCURRENCY_LOCAL_MAX        = 10
CONCURRENCY_LOCAL_LOCKED_MAX = 5


def is_valid_concurrency_unlock_code(code: str) -> bool:
    return code == CONCURRENCY_UNLOCK_CODE


def get_cloud_concurrency_bounds(unlocked: bool) -> tuple[int, int]:
    if unlocked:
        return CONCURRENCY_CLOUD_MIN, 2**31 - 1
    return CONCURRENCY_CLOUD_MIN, CONCURRENCY_CLOUD_LOCKED_MAX


def get_local_concurrency_bounds(unlocked: bool) -> tuple[int, int]:
    if unlocked:
        return CONCURRENCY_LOCAL_MIN, 2**31 - 1
    return CONCURRENCY_LOCAL_MIN, CONCURRENCY_LOCAL_LOCKED_MAX


def get_concurrency_bounds(mode: str, unlocked: bool) -> tuple[int, int]:
    if mode == "local":
        return get_local_concurrency_bounds(unlocked)
    return get_cloud_concurrency_bounds(unlocked)


def get_concurrency_cap() -> int:
    return 2**31 - 1


def get_default_concurrency(mode: str) -> int:
    if mode == "local":
        return CONCURRENCY_LOCAL_DEFAULT
    return CONCURRENCY_CLOUD_DEFAULT

# ── 统一语言目录（Phase 1）────────────────────────────────
#
# This is the single built-in language directory used by source and target
# selectors.  Keep English first because it is the new-install target default.
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
    "中文": "zh",
}

# Kept as an extension point for callers that still pass include_optional;
# Phase 1 makes all built-ins available as targets, so this map is empty.
OPTIONAL_TARGET_LANGS: dict[str, str] = {}

# 源语言候选集合允许中文 + 当前所有内置语种。
SUPPORTED_SOURCE_LANGS: dict[str, str] = {
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
SETTINGS_SCHEMA_VERSION = 41
