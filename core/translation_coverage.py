"""Deterministic coverage detection for untranslated-only tasks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.language_registry import get_default_source_lang, is_auto_source_lang
from core.translation_filter import should_translate

COVERAGE_COVERED = "covered"
COVERAGE_SOURCE_ONLY = "source_only"
COVERAGE_AMBIGUOUS = "ambiguous"
COVERAGE_IGNORED = "ignored"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_NON_CJK_LETTER_RUN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# \u8bd1\u6587\u91cc\u5939\u5e26\u7684\u4e2d\u6587\u7247\u6bb5\uff1a\u628a\u7d27\u90bb\u7684\u6570\u5b57\u4e00\u5e76\u6536\u8fdb\u6765\uff0c\u62a5\u544a\u91cc\u624d\u770b\u5f97\u51fa\u662f\u300c2026\u5e748\u67089\u65e5\u300d
# \u8fd9\u79cd\u65e5\u671f\uff0c\u800c\u4e0d\u662f\u5b64\u96f6\u96f6\u4e00\u4e2a\u300c\u5e74\u300d\u5b57\u3002
_CJK_FRAGMENT_RE = re.compile(r"[\u4e00-\u9fff\d\uff10-\uff19]*[\u4e00-\u9fff][\u4e00-\u9fff\d\uff10-\uff19]*")

# \u300c\u8bd1\u6587\u91cc\u53ea\u5939\u5e26\u5c11\u91cf\u4e2d\u6587\u300d\u7684\u5224\u5b9a\u9608\u503c\u3002\u7f16\u53f7\u524d\u7f00\uff08\u4e00.1\uff09\u3001\u65e5\u671f\uff082026\u5e748\u67089\u65e5\uff09\u3001
# \u4e2a\u522b\u4e13\u540d\u5c5e\u4e8e\u8fd9\u4e00\u7c7b\uff1a\u6574\u53e5\u5df2\u7ecf\u662f\u76ee\u6807\u8bed\u8a00\uff0c\u4e0d\u8be5\u5224\u6210\u300c\u672a\u8bd1\u6e90\u6587\u300d\u3002
#
# \u4e4b\u524d\u53ea\u6309\u7edd\u5bf9\u5b57\u6570\uff0812 \u4e2a\u5b57\uff09\u5224\u5b9a\uff0c\u5b9e\u6d4b\u5750\u5b9e\u8fc7\u4e00\u6b21\u771f\u5b9e\u8bef\u62a5\uff1a1330 \u5b57\u7684\u6cd5\u6587\u8bd1\u6587\u6bb5\u843d\u91cc
# \u5939\u4e86 15 \u4e2a\u4e2d\u6587\u5b57\uff08\u5168\u662f\u65e5\u671f\uff0c\u5982\u300c2025\u5e7412\u67088\u65e5\u300d\uff09\uff0c15 > 12\uff0c\u4e8e\u662f\u6574\u6bb5\u88ab\u5224\u6210\u4e2d\u6587
# \u539f\u6587\uff0c\u8ddf\u5b83\u914d\u5bf9\u7684\u539f\u6587\u6bb5\u843d\u4e5f\u4e00\u8d77\u88ab\u62a5\u6210\u300c\u672a\u8bd1\u6e90\u6587\u300d\u2014\u2014\u4e24\u6761\u8bef\u62a5\uff0c\u5b9e\u9645\u4e0a\u662f\u4e00\u6bb5\u7ffb\u5b8c\u6574
# \u7684\u8bd1\u6587\u3002\u6539\u6210\u6309\u6bd4\u4f8b\u5224\u5b9a\uff1a\u4e2d\u6587\u5b57\u6570\u5360\u5168\u6587\u957f\u5ea6\u7684\u6bd4\u4f8b\u8db3\u591f\u4f4e\uff0c\u624d\u7b97\u300c\u987a\u5e26\u5939\u5e26\u300d\uff1b\u540c\u65f6
# \u4fdd\u7559\u4e00\u4e2a\u5bbd\u677e\u7684\u7edd\u5bf9\u4e0a\u9650\uff0c\u9632\u6b62\u6bd4\u4f8b\u7b97\u6cd5\u5728\u8d85\u957f\u6587\u6863\u91cc\u653e\u8fc7\u5927\u6bb5\u771f\u6b63\u6ca1\u7ffb\u7684\u5185\u5bb9\u3002
_INCIDENTAL_CJK_MAX_CHARS = 60
_INCIDENTAL_CJK_MAX_RATIO_OF_TEXT = 0.05
_INCIDENTAL_CJK_MAX_RATIO = 0.2
_INCIDENTAL_CJK_MIN_LETTERS = 12

# ---------------------------------------------------------------------------
# 语言证据：这段文字到底像哪一门语言
#
# 9.3.4 之前这里只有 en/fr 一对标记词，而且是硬编码的「非英即法」：英译中时
# 「Société Générale Contract」因为带 é 被判成「法语，所以不是英语」→ 既不算源文
# 也不算译文 → 静默进 ignored，不翻译、也不进报告，用户翻到才发现。这一版做三件事：
#   1) 加一层文字体系判定（中日韩俄阿希泰希腊天城体各有专属字符区间，最硬的证据）；
#   2) 标记词表扩到 7 门拉丁语言，德译英这种同字母语言对终于有话可说；
#   3) 同一个词出现在两门以上语言的表里就从所有表里删掉——"in" 英德荷都有、"la" 法西
#      意都有，它们证明不了任何事，留着只会制造假证据。变音符号同理。
#
# 证据只有三态：True（像）/ False（不像）/ None（没话说）。None 绝不能被当成 False
# 用——「说不上来」和「确定不是」在补译里是两个完全不同的后果，见 looks_like_source_text。
# ---------------------------------------------------------------------------
_LANGUAGE_MARKER_WORDS: dict[str, set[str]] = {
    "en": {
        "and", "are", "the", "this", "that", "these", "those", "with", "without",
        "from", "for", "of", "on", "in", "is", "to", "was", "were", "be", "been",
        "shall", "will", "must", "may", "should", "which", "its", "their", "they",
        "there", "have", "has", "had", "not", "any", "all", "each", "such",
        "after", "before", "between", "during", "per", "than", "then", "when",
        "where", "while", "by", "at", "as", "or", "if", "into", "upon", "other",
        "no", "do", "does",
    },
    "fr": {
        "avec", "aux", "ce", "ces", "cette", "cet", "dans", "des", "du", "de",
        "est", "et", "la", "le", "les", "pour", "sans", "sur", "une", "un", "par",
        "que", "qui", "plus", "sont", "ont", "leur", "leurs", "ainsi", "selon",
        "entre", "dont", "où", "chaque", "tout", "tous", "doit", "peut", "être",
        "non", "il", "elle", "nous", "vous", "mais", "comme", "lors", "sous",
        "vers",
    },
    "de": {
        "der", "die", "das", "den", "dem", "des", "und", "oder", "mit", "von",
        "für", "auf", "im", "in", "ist", "sind", "nicht", "ein", "eine", "einer",
        "einem", "eines", "zu", "zur", "zum", "bei", "nach", "aus", "wird",
        "werden", "durch", "als", "dass", "sich", "auch", "sowie", "kein",
        "keine", "wenn", "über", "unter", "vor", "gemäß", "sie", "wir", "aber",
        "wie", "nur", "noch", "muss", "kann",
    },
    "es": {
        "el", "la", "los", "las", "del", "de", "en", "un", "una", "uno", "para",
        "con", "sin", "que", "por", "como", "este", "esta", "estos", "estas",
        "son", "ser", "está", "están", "más", "pero", "todo", "todos", "cada",
        "según", "entre", "sobre", "desde", "hasta", "cuando", "donde", "también",
        "no", "se", "su", "sus", "al", "ya", "muy",
    },
    "it": {
        "il", "lo", "la", "le", "gli", "dei", "delle", "della", "degli", "del",
        "dal", "dalla", "nel", "nella", "alla", "al", "per", "con", "senza",
        "una", "un", "che", "non", "sono", "essere", "come", "questo", "questa",
        "questi", "anche", "tra", "sul", "sulla", "più", "ogni", "secondo",
        "sia", "ma", "se", "si", "ha", "hanno",
    },
    "pt": {
        "os", "as", "do", "da", "dos", "das", "um", "uma", "com", "sem", "para",
        "por", "que", "não", "são", "ser", "como", "este", "esta", "também",
        "entre", "sobre", "desde", "até", "quando", "onde", "cada", "pelo",
        "pela", "no", "na", "nos", "nas", "ao", "aos", "de", "mais", "mas", "se",
        "seu", "sua",
    },
    "nl": {
        "de", "het", "een", "en", "van", "niet", "voor", "zijn", "aan", "naar",
        "uit", "op", "met", "door", "als", "bij", "die", "dat", "in", "of", "om",
        "te", "ook", "deze", "dit", "tussen", "over", "worden", "wordt", "moet",
        "kan", "zal", "wie", "hun", "maar", "meer", "nog", "geen",
    },
}
_LANGUAGE_DIACRITICS: dict[str, set[str]] = {
    "fr": set("àâçéèêëîïôùûüÿœæ"),
    "de": set("äöüß"),
    "es": set("áéíóúñü¿¡"),
    "pt": set("ãõáéíóúâêôçàü"),
    "it": set("àèéìíòóùî"),
    "nl": set("ëïéèü"),
}
_MARKER_MIN_WORD_LENGTH = 2


def _unique_by_language(
    table: dict[str, set[str]],
    *,
    min_length: int = 1,
) -> dict[str, frozenset[str]]:
    """只保留「只属于一门语言」的标记，共享的一律丢掉。"""
    unique: dict[str, frozenset[str]] = {}
    for language, items in table.items():
        others: set[str] = set()
        for other_language, other_items in table.items():
            if other_language != language:
                others |= other_items
        unique[language] = frozenset(
            item for item in items - others if len(item) >= min_length
        )
    return unique


_UNIQUE_MARKER_WORDS = _unique_by_language(
    _LANGUAGE_MARKER_WORDS,
    min_length=_MARKER_MIN_WORD_LENGTH,
)
_UNIQUE_DIACRITICS = _unique_by_language(_LANGUAGE_DIACRITICS)
_FRENCH_ELISION_RE = re.compile(r"\b(?:[cdjlmnstqu]|jusqu|lorsqu)['’]", re.IGNORECASE)
# 变音符号/省音撇号只是旁证，比不上一个功能词命中，但比什么都没有强。
_DIACRITIC_EVIDENCE_WEIGHT = 2
# 否定结论要留余量：1 比 0 不许定案。词表扩到 7 门之后，door / over / met / van / son /
# el 这些荷、西专属词会把一堆普通英文短标签判成「确定不是英文」——而这类短标签里往往
# 一个 en 专属功能词都没有，own_score 恒为 0。落到产品上就是 zh→en（本工具最主要的语言
# 对）已经翻好的双语格「防火门 / Fire Door」被判成未译，整格再翻一遍。弱证据只配给出
# 「说不上来」，这跟本文件开头写的三态原则是同一条。
_EVIDENCE_DECISIVE_MARGIN = 2

# 有专属文字体系的语言：字符区间比任何词表都硬。缺了这一层，「英译俄」里一整格英文
# 会因为「有 3 个字母以上的词」被当成俄文译文，整格漏译。
# 只收真正的假名：片假名区间 U+30A0–U+30FF 里还混着 ゠(U+30A0)、・(U+30FB)、
# ー(U+30FC) 这几个标点/长音号，中文排版里照样会出现（外国人名分隔、从日文资料复制
# 过来的长音号）。把它们算成假名，中译日时一个纯中文格就成了「日文译文」——既不算源文
# 也不算译文，静默进 ignored，不翻译也不进报告，正是 高-4 那个病换个路径复发。
# 「ー」不会单独出现在没有别的假名的日文里，去掉它不损失日文判定力。
_KANA_RE = re.compile(r"[ぁ-ゖゝ-ゟァ-ヺヽ-ヿｦ-ﾝ]")
_HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿԀ-ԯ]")
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿ]")
_HEBREW_RE = re.compile(r"[֐-׿]")
_THAI_RE = re.compile(r"[฀-๿]")
_LAO_RE = re.compile(r"[຀-໿]")
_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-zÀ-ɏ]")
_LANGUAGE_SCRIPT_RE = {
    "ko": _HANGUL_RE,
    "ru": _CYRILLIC_RE,
    "uk": _CYRILLIC_RE,
    "bg": _CYRILLIC_RE,
    "sr": _CYRILLIC_RE,
    "mn": _CYRILLIC_RE,
    "ar": _ARABIC_RE,
    "fa": _ARABIC_RE,
    "ur": _ARABIC_RE,
    "he": _HEBREW_RE,
    "th": _THAI_RE,
    "lo": _LAO_RE,
    "el": _GREEK_RE,
    "hi": _DEVANAGARI_RE,
    "mr": _DEVANAGARI_RE,
    "ne": _DEVANAGARI_RE,
}

# 极短的目标语言片段：编号符号、计量单位、罗马数字。这类文本天然不满足「至少 3 个
# 字母的自然语言词」门槛（"N°" 只有一个字母），但整份双语文档里大量出现——常见于
# 「序号 / N°」这类只有一个词的表头格。实测坐实过：8 个「序号 / N°」双语格被误判成
# 「未译源文」，根因就是 looks_like_target_text("N°") 原先恒为 False。这里按需求列出
# 的例子（N°、m²、kg、%、Réf.、No.、纯数字、罗马数字）加一个前置正则，命中就直接判
# 定为有效的目标语言内容，不再送进「至少 3 个字母」的自然语言词检测。
_SHORT_TARGET_TOKEN_RE = re.compile(
    r"""
    N°\.?|                                            # N° / N°.
    N[o0]\.?|Nº\.?|                                    # No. / No / Nº
    R[ée]f\.?|                                              # Réf. / Ref.
    Art\.?|                                                 # Art.
    §\s*[0-9]+(?:[.,][0-9]+)*|                         # § 3.2
    [0-9]+(?:[.,][0-9]+)*\s*
        (?:%|°C|°|m²|m³|km²|km|kg|g|mm|cm)?|
    m²|m³|km²|km|kg|g|mm|cm|%|°C|
    M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|I?V?I{0,3})
    """,
    re.IGNORECASE | re.VERBOSE,
)


# 外文简称：CCTEB、ONEE、SARL、PV、BTR-ANODE-CCTEB-032 这类全大写缩写和文号。
# 单位名称、公司名、文件编号在双语文档里大量出现，它们本身就是"译文"——法文版和
# 中文版写的是同一串字母。这类内容既不该送去翻译，也不值得占用一次模型判定。
# 只认全大写：小写混进来（Béton、Travaux）就不是缩写，走正常的自然语言词检测。
_FOREIGN_ACRONYM_RE = re.compile(r"[A-Z][A-Z0-9]*(?:[.&/\-][A-Z0-9]+)*\.?")
_FOREIGN_ACRONYM_MAX_CHARS = 24


def looks_like_foreign_acronym(text: str) -> bool:
    """Return whether text is a short all-caps foreign abbreviation or reference code."""
    cleaned = clean_coverage_text(text)
    if not cleaned or len(cleaned) > _FOREIGN_ACRONYM_MAX_CHARS:
        return False
    if sum(1 for char in cleaned if char.isalpha()) < 2:
        return False
    return bool(_FOREIGN_ACRONYM_RE.fullmatch(cleaned))


def looks_like_short_target_token(text: str) -> bool:
    """Return whether text is a short target-language token (N°、m²、kg、罗马数字、CCTEB……)."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False
    if _SHORT_TARGET_TOKEN_RE.fullmatch(cleaned):
        return True
    return looks_like_foreign_acronym(cleaned)


@dataclass
class CoverageUnit:
    """One source/translation coverage decision at a concrete document position."""

    source_text: str
    status: str
    location: str
    reason: str
    target_text: str = ""
    kind: str = ""
    section_path: str = ""
    data: dict = field(default_factory=dict)


def clean_coverage_text(text: str | None) -> str:
    return str(text or "").strip()


def non_empty_lines(text: str | None) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def join_lines(lines: list[str]) -> str:
    return "\n".join(line.strip() for line in lines if line.strip()).strip()


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(str(text or "")))


def contains_kana(text: str) -> bool:
    """Return whether text carries hiragana/katakana — 日文独有，中文不会有。"""
    return bool(_KANA_RE.search(str(text or "")))


def contains_non_cjk_letters(text: str) -> bool:
    return any(char.isalpha() and not contains_cjk(char) for char in str(text or ""))


def contains_meaningful_non_cjk_word(text: str) -> bool:
    """Return whether text contains a likely natural-language target word."""
    for match in _NON_CJK_LETTER_RUN_RE.finditer(str(text or "")):
        token = match.group(0)
        if contains_cjk(token):
            continue
        if sum(1 for char in token if char.isalpha()) >= 3:
            return True
    return False


def count_cjk_chars(text: str) -> int:
    return len(_CJK_RE.findall(str(text or "")))


def count_non_cjk_letters(text: str) -> int:
    return sum(1 for char in str(text or "") if char.isalpha() and not _CJK_RE.match(char))


def residual_cjk_fragments(text: str, *, limit: int = 3) -> list[str]:
    """Return the CJK fragments left inside an otherwise translated segment."""
    fragments: list[str] = []
    seen: set[str] = set()
    for match in _CJK_FRAGMENT_RE.finditer(str(text or "")):
        fragment = match.group(0).strip()
        if not fragment or fragment in seen:
            continue
        seen.add(fragment)
        fragments.append(fragment)
        if len(fragments) >= limit:
            break
    return fragments


def has_incidental_cjk(text: str, *, target_lang: str) -> bool:
    """Return whether text is target-language prose carrying only a few CJK chars."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False
    if str(target_lang or "").strip().lower() in {"zh", "ja"}:
        return False

    cjk_count = count_cjk_chars(cleaned)
    if cjk_count == 0 or cjk_count > _INCIDENTAL_CJK_MAX_CHARS:
        return False
    total_len = len(cleaned)
    if total_len == 0 or cjk_count / total_len > _INCIDENTAL_CJK_MAX_RATIO_OF_TEXT:
        # 按占比判定，而不是只看绝对字数：短标题（如「抢工方案」，4 字 100% 中文）
        # 必须仍判成源语言；长段落里夹几个字的日期（1330 字里 15 个中文字，1.1%）
        # 不该被这一条拦下。
        return False
    letter_count = count_non_cjk_letters(cleaned)
    if letter_count < _INCIDENTAL_CJK_MIN_LETTERS:
        return False
    if cjk_count > letter_count * _INCIDENTAL_CJK_MAX_RATIO:
        return False
    return contains_meaningful_non_cjk_word(cleaned)


def _normalize_lang(language: str | None) -> str:
    """"ja-JP" / " JA " → "ja"。与 core.translation_filter._normalize_lang 同口径。

    带地区子标签的语言码必须先归一，否则 "ja-JP" 落不进任何按语言分支的判据：日文源文
    会走到「拉丁语言遇 CJK 即反证」那条路上被判成非源文，整份文档静默 ignored。
    """
    cleaned = str(language or "").strip().lower()
    if not cleaned:
        return ""
    return re.split(r"[-_]", cleaned, maxsplit=1)[0]


def _script_evidence(text: str, language: str, rival: str) -> bool | None:
    """Script-level evidence: 有专属文字体系的语言，看字符区间就能定。"""
    if language == "zh":
        if not _CJK_RE.search(text):
            return False
        # 汉字＋假名的是日文，不是中文。
        return not _KANA_RE.search(text)
    if language == "ja":
        if _KANA_RE.search(text):
            return True
        if not _CJK_RE.search(text):
            return False
        # 纯汉字：跟中文分不开，除非对手压根不是汉字圈的语言。
        return None if rival in {"", "zh", "ja"} else True

    own_script = _LANGUAGE_SCRIPT_RE.get(language)
    if own_script is not None:
        return bool(own_script.search(text))

    # 自己是拉丁字母语言：文本里一个拉丁字母都没有、却是别的文字体系，就是反证。
    if _LATIN_LETTER_RE.search(text):
        return None
    if _CJK_RE.search(text) or _KANA_RE.search(text):
        return False
    for script in _LANGUAGE_SCRIPT_RE.values():
        if script.search(text):
            return False
    return None


def _is_same_alphabet_pair(source_lang: str | None, target_lang: str | None) -> bool:
    """Return whether both languages are written in the plain Latin alphabet.

    「同字母语言对」指英译德、英译荷这类两边共用一套字母、光看字符区间分不出谁是谁的
    组合——只有这种组合才需要靠功能词去防「两行都是原文却被配成一对」。任何一边有专属
    文字体系（中、日、韩、俄、阿、希、泰……）都不算：那种组合看字符就分得开，用不着
    这道闸，硬套只会误伤。
    """
    source = _normalize_lang(resolve_coverage_source_lang(source_lang))
    target = _normalize_lang(target_lang)
    if not source or not target or source == target:
        return False
    return all(
        code not in {"zh", "ja"} and code not in _LANGUAGE_SCRIPT_RE
        for code in (source, target)
    )


def _marker_score(
    language: str,
    *,
    words: set[str],
    chars: set[str],
    has_french_elision: bool,
) -> int:
    score = len(words & _UNIQUE_MARKER_WORDS.get(language, frozenset()))
    if chars & _UNIQUE_DIACRITICS.get(language, frozenset()):
        score += _DIACRITIC_EVIDENCE_WEIGHT
    elif language == "fr" and has_french_elision:
        score += _DIACRITIC_EVIDENCE_WEIGHT
    return score


def _marker_evidence(
    text: str,
    language: str,
    *,
    rival: str | None = None,
) -> bool | None:
    """只按功能词/变音符号判，不看文字体系。

    跟 _language_evidence 分开是有原因的：_script_evidence 判 zh 时只问「有汉字且没
    假名」，这对「后半是不是仍然是中文原文」根本不是证据——中译法的译文行里留一个中文
    序号「（二）」就够它返 True 了。要拿「后半仍是源语言」当理由去否掉一次配对，只能用
    功能词这种真·语言证据，见 split_existing_bilingual_text 里的同字母闸门。
    """
    normalized = _normalize_lang(language)
    normalized_rival = _normalize_lang(rival)
    if not normalized or normalized == normalized_rival:
        return None

    if normalized not in _UNIQUE_MARKER_WORDS:
        # 词表里没有这门语言（自定义目标语言、暂未收录的语种）：能给的只有「没话说」。
        # 拿别人的标记词反推「所以不是它」会出大事——中译某自定义语言时，法文译文会因为
        # 「更像法语」被判成非译文，整格重翻一遍，格里多出一条重复译文。
        return None

    words = {match.group(0).casefold() for match in _NON_CJK_LETTER_RUN_RE.finditer(text)}
    # 用 lower 而不是 casefold：casefold 会把 ß 折成 ss，德语最硬的那个记号就没了。
    chars = set(text.lower())
    has_french_elision = bool(_FRENCH_ELISION_RE.search(text))

    def score(language_code: str) -> int:
        return _marker_score(
            language_code,
            words=words,
            chars=chars,
            has_french_elision=has_french_elision,
        )

    own_score = score(normalized)
    if normalized_rival:
        candidates = [normalized_rival]
    else:
        candidates = [key for key in _UNIQUE_MARKER_WORDS if key != normalized]
    other_score = max((score(candidate) for candidate in candidates), default=0)
    if own_score > other_score:
        return True
    if other_score - own_score >= _EVIDENCE_DECISIVE_MARGIN:
        return False
    return None


def _language_evidence(
    text: str,
    language: str,
    *,
    rival: str | None = None,
) -> bool | None:
    """Return whether text looks like ``language`` rather than another language.

    先看文字体系（最硬的证据），文字体系说不上来时才看功能词。

    ``rival`` 指定要跟哪一门语言比。给了就只跟它比——补译判「这是不是源文」时，
    唯一有资格顶掉源文身份的就是目标语言，跟第三门语言比毫无意义（英译中的
    「Société Générale Contract」不该因为「更像法语」而被判成非英文）。不给就跟
    词表里所有其他语言比，用于判「这段拉丁字母文字到底是不是目标语言」——中译法的
    文档里夹一段英文，那不是法文译文。
    """
    normalized = _normalize_lang(language)
    normalized_rival = _normalize_lang(rival)
    if not normalized or normalized == normalized_rival:
        return None

    script = _script_evidence(text, normalized, normalized_rival)
    if script is not None:
        return script
    return _marker_evidence(text, normalized, rival=normalized_rival)


def resolve_coverage_source_lang(source_lang: str | None) -> str:
    """Turn 「自动识别」 into a concrete source language for coverage checks.

    补译判定必须先知道源语言是哪一门，才分得清「原文」和「译文」。而自动识别是在
    提取之后才出结果的，提取阶段拿到的就是字面量 auto——它既不是 zh 也不是任何
    受支持的语言码，于是所有中文单元格都会被判成「不是源文」，补译清单变成 0 条，
    最后输出一份一个字都没翻的文件。这里统一落到默认源语言，不让 auto 漏进判定。
    """
    candidate = str(source_lang or "").strip()
    if not candidate or is_auto_source_lang(candidate):
        return get_default_source_lang()
    return candidate


def looks_like_source_text(
    text: str,
    *,
    source_lang: str,
    target_lang: str,
) -> bool:
    """Return whether text is a credible source-language segment."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False

    source_lang = resolve_coverage_source_lang(source_lang)
    source = _normalize_lang(source_lang)
    if source == "zh":
        if not contains_cjk(cleaned):
            return False
        if _looks_translated_despite_cjk(
            cleaned,
            source_lang=source,
            target_lang=target_lang,
        ):
            return False
        return should_translate(
            cleaned,
            target_lang=target_lang,
            source_lang=source_lang,
        )

    # 源语言不是中文时，旧代码在这里对含汉字的文本一刀 `return False`——理由是「中文
    # 只可能是译文」。这条在日译中/韩译中上是灾难：一-龥 码区同时装着中文、日文、韩文
    # 汉字，「工事契約書」这类纯汉字标题连带「コンクリート打設」整批判成非源文，既不译
    # 也不进报告（补译模式下整份日文文档 ignored）。判据交给 should_translate 这个唯一
    # 漏斗——它已经按语言对判定（假名/谚文是硬证据，源语言落在日/韩时汉字按原文处理），
    # 抽取路径和补译路径这才是同一把尺子。
    #
    # 「看起来不是源语言」也不足以否掉源文身份。证据表覆盖不到的语言、或者只是带了几个
    # 法语变音符号的英文公司名（英译中里的 Société Générale Contract），一旦在这里被
    # 否掉，就既不算源文也不算译文——静默落进 ignored，不翻译、也不进报告，用户翻到
    # 才发现。只有当这段文字确实像目标语言（＝像一条已经产出的译文）时才否得掉；其余
    # 一律当源文送去翻译：多翻一条看得见、删得掉，漏译一条没人知道。
    if _language_evidence(cleaned, source, rival=target_lang) is False and looks_like_target_text(
        cleaned,
        source_lang=source_lang,
        target_lang=target_lang,
    ):
        return False
    if not (contains_non_cjk_letters(cleaned) or contains_cjk(cleaned)):
        # 纯数字/符号的格子不算源文（假名、谚文本身是字母，走上一半）。
        return False
    return should_translate(
        cleaned,
        target_lang=target_lang,
        source_lang=source_lang,
    )


def looks_like_target_text(
    text: str,
    *,
    source_lang: str,
    target_lang: str,
) -> bool:
    """Return whether text is a credible target-language segment."""
    cleaned = clean_coverage_text(text)
    if not cleaned:
        return False

    target = str(target_lang or "").strip().lower()
    if target == "zh":
        return contains_cjk(cleaned)

    if looks_like_short_target_token(cleaned):
        return True

    if target == "ja" and contains_kana(cleaned):
        # 日文译文天然含汉字，「夹带零星 CJK」那把尺子量不了它——中译日时
        # 「工事は2026年8月9日に完了する」会被当成中文原文，整表再补一遍重复译文。
        # 假名才是可靠证据：中文里不会出现平假名/片假名。
        return True

    if contains_cjk(cleaned):
        # 整句已是目标语言、只夹带编号或日期这类零星中文时，仍算译文；
        # 残留的中文另由 residual_cjk_fragments() 单独提示，不再判成「未译源文」。
        return _looks_translated_despite_cjk(
            cleaned,
            source_lang=source_lang,
            target_lang=target,
        )
    target_evidence = _language_evidence(cleaned, target)
    if target_evidence is False:
        return False
    return contains_meaningful_non_cjk_word(cleaned)


def _looks_translated_despite_cjk(
    cleaned: str,
    *,
    source_lang: str,
    target_lang: str,
) -> bool:
    if resolve_coverage_source_lang(source_lang).lower() != "zh":
        return False
    target = _normalize_lang(target_lang)
    if target == "ja":
        # 中译日：has_incidental_cjk 这条尺子对日文恒为 False（日文本来就满是汉字），
        # 拿它判「这是不是已经翻好的日文」永远判不出来。改看假名。
        return contains_kana(cleaned)
    if not has_incidental_cjk(cleaned, target_lang=target):
        return False
    return _language_evidence(cleaned, target) is not False


def split_existing_bilingual_text(
    text: str,
    *,
    source_lang: str,
    target_lang: str,
) -> tuple[str, str] | None:
    """
    Split app-style bilingual text into source and target parts.

    The split is intentionally conservative: it only accepts a boundary where
    the left side looks like source text and the right side looks like target
    text. This lets multi-line source text stay intact.
    """
    lines = non_empty_lines(text)
    if len(lines) < 2:
        return None

    # 边界从末尾往前扫，而不是从「只有第一行是原文」往后扫。原文本身可能有好几
    # 行——实测坐实过一例：单元格第一行「变配电室」、第二行「专项」，第三行才是
    # 法文译文；旧代码从 split_index=1 试起，第一次就命中「源文=变配电室，译文=
    # 专项+法文」，把「专项」这个真正的中文原文错判成译文里的残留中文。
    #
    # 但也不能反过来一路贪心到底：译文同样可能有好几行（「污染/破坏/Contamination/
    # Détérioration」），从最大源文侧试起会把 Contamination 吞进原文侧。所以先从末尾
    # 逐行往前扩，直到某一行不再像目标语言为止——这一行就是边界候选，译文侧取到的
    # 是最长的、整段都像目标语言的后缀。
    boundary = len(lines)
    while boundary > 1 and looks_like_target_text(
        lines[boundary - 1],
        source_lang=source_lang,
        target_lang=target_lang,
    ):
        boundary -= 1
    candidates = [boundary] if boundary < len(lines) else []
    # 后缀扫描没收敛时（例如逐行看都不像目标语言、合起来才像），退回旧的逐位试探，
    # 保证行为不比原来差。
    candidates.extend(
        index for index in range(len(lines) - 1, 0, -1) if index != boundary
    )
    for split_index in candidates:
        source_candidate = join_lines(lines[:split_index])
        target_candidate = join_lines(lines[split_index:])
        if not source_candidate or not target_candidate:
            continue
        if not looks_like_target_text(
            target_candidate,
            source_lang=source_lang,
            target_lang=target_lang,
        ):
            continue
        # 同字母语言对（英译德、英译荷……）的死角：两行都是原文时，后一行照样满足
        # 「有 3 个字母以上的自然语言词」，必然被当成译文，整格判 covered 后再不进
        # 补译清单——引擎不支持仲裁或仲裁失败时就是彻底漏译。有正面的功能词证据说后半
        # 仍是源语言时，这一刀不能切。
        #
        # 这道闸只对同字母语言对开，而且只认功能词证据。第一轮写的是「两半文字体系
        # 相同」＋ _language_evidence，两处都错：_script_signature 是「出现过就算」的
        # 集合比较，中文行里有一个 HRB400、C30 这样的材料牌号（施工表里到处都是），
        # 两半签名就都成了 {cjk, latin}；而 _language_evidence 判 zh 时走 _script_evidence，
        # 「有汉字且没假名」就返 True——译文行里留一个中文序号「（二）」就够了。两个错
        # 凑到一起，中译法里已经翻好的格子被整批打回补译清单，重翻一遍。跨文字体系的
        # 格子本来也用不着这道闸：中译法的「（二）Ligature des armatures」是一条译文，
        # 译文里残留的中文归残留体检管，在这里否掉配对等于把残留检查的输入整个抽掉。
        if _is_same_alphabet_pair(source_lang, target_lang) and (
            _marker_evidence(
                target_candidate,
                resolve_coverage_source_lang(source_lang),
                rival=target_lang,
            )
            is True
        ):
            continue
        if not looks_like_source_text(
            source_candidate,
            source_lang=source_lang,
            target_lang=target_lang,
        ):
            continue
        return source_candidate, target_candidate
    return None


def coverage_summary(units: list[CoverageUnit]) -> dict[str, int]:
    summary = {
        COVERAGE_COVERED: 0,
        COVERAGE_SOURCE_ONLY: 0,
        COVERAGE_AMBIGUOUS: 0,
        COVERAGE_IGNORED: 0,
    }
    for unit in units:
        summary[unit.status] = summary.get(unit.status, 0) + 1
    return summary


def group_ignored_units(units: list[CoverageUnit]) -> list[tuple[str, list[str]]]:
    """把 ``ignored`` 状态的单元按 ``reason`` 分组，返回 ``[(reason, locations), ...]``。

    审计批次 2 第④条：ignored 是补译计划里的静默黑洞——公式格、看起来已经是译文的格、
    不符合候选规则的格都会落进这个状态，但补译日志过去只汇报 covered / source_only /
    ambiguous 三类计数，从不提 ignored，用户没法知道"这一格为什么没被补"。这里只负责
    产出证据（哪些位置、什么理由）；格式化成日志文案由 ``format_ignored_coverage_report``
    统一负责——Excel 和 Word 的板式只差一个量词（格／处），由调用方传入。

    放在共享层而不是 excel_coverage.py：判定 ignored 状态的三条理由分别在
    excel_coverage.py／word_coverage.py 各写各的，但"按 reason 分组"这一步跟格式
    无关，只要有一份 ``CoverageUnit`` 列表就能做。放这里意味着 Word 侧以后想接同样
    的日志汇总，改的是它自己的任务日志调用点，不用再挪判定逻辑或重写一遍分组代码。

    顺序：按 reason 首次出现的顺序，不按数量重排——补译计划本身是按 sheet / 段落的
    遍历顺序生成的，这样分组顺序就是"文档里先遇到哪类问题就先报哪类"，同一份文档
    每次跑结果稳定，也方便测试断言。
    """
    grouped: dict[str, list[str]] = {}
    for unit in units:
        if unit.status != COVERAGE_IGNORED:
            continue
        grouped.setdefault(unit.reason, []).append(unit.location)
    return list(grouped.items())


def format_ignored_coverage_report(
    file_name: str,
    units: list[CoverageUnit],
    *,
    unit_noun: str = "格",
    sample_limit: int = 5,
) -> tuple[list[str], str]:
    """把 ignored 分组结果排成任务日志行，返回 ``(摘要行列表, 全量明细)``。

    摘要行进用户可见的任务日志：一行总数加每个理由一行，位置只点 ``sample_limit``
    个样例、超出的写「等 N ×量词」——任务日志消息会被脱敏管线截到 500 字符，全量
    坐标塞进去也只会被静默砍掉，所以截样例不是审美选择而是硬约束。全量明细单独
    返回，调用方落到 loguru 调试输出里去（stderr，本仓库没配文件 sink；任务面板
    前端不按 level 过滤，DEBUG 行发进任务日志照样全量刷在用户面前，起不到
    「降权重」的作用）。

    零 ignored 返回 ``([], "")``：仓库立场「无关提示不挂」，调用方一行都不用出。
    """
    groups = group_ignored_units(units)
    if not groups:
        return [], ""
    total = sum(len(locations) for _, locations in groups)
    lines = [f"  → {file_name}：{total} {unit_noun}未补译（默认跳过），按原因分类如下："]
    for reason, locations in groups:
        sample = locations[:sample_limit]
        remaining = len(locations) - len(sample)
        detail = "、".join(sample)
        if remaining > 0:
            detail += f"，等 {remaining} {unit_noun}"
        lines.append(f"    · {reason}（{len(locations)} {unit_noun}）：{detail}")
    full_detail = "；".join(
        f"{reason}：{'、'.join(locations)}" for reason, locations in groups
    )
    return lines, f"{file_name}：补译 ignored 全量明细 | {full_detail}"
