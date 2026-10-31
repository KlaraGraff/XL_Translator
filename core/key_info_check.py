"""译文定稿后核对关键信息：证件号和编号、日期、人名拼写、拼音人名。

合同、证件这类文件里，人名和编号错一个字母、日期颠倒日月都是实质错误，而现有的
译文检查只核对数字（:mod:`core.translation_filter`）。这里补四条规则，只判断、
不改译文、不重译；查出来的由任务流程标记需复核，并在复核清单里写明哪一处。

为了不误判，每条规则只在找到「明确对不上」的证据时才报，找不到对应、拿不准、
写法有歧义的一律不报：

1. **证件号、编号**：原文里「1–4 个大写字母 + 4 位以上数字」（或数字在前、字母在后）
   连写的整串，如 ED2914389、K19498。译文里这串数字还在、前后的字母却变了才报。
   多了空格、全角、大小写不同不算；数字后面是单位的（1500MM、2000KVA）不看。
2. **日期**：只比写法明确的完整日期（法文、英文月份名，中文年月日，ISO，法文和中文
   里的「日/月/年」）。原文和译文里完整日期个数相同、又有一个对不上才报；句子里有
   区间（du…au、至、到）时不比。英文的 07/09/2026 两种读法都算对。
3. **人名拼写**：只看紧跟在 Monsieur、Madame、Mr 这类称谓后面的名字。译文里没有这个
   名字、却有一个只差一两个字母的拉丁字母名字才报（FARISSI → FARESSI）。
4. **拼音人名**（只在译成中文时）：称谓后面的名字能整个拆成拼音、带常见中国姓氏，就是
   中国人名的拼音。仅凭拼音定不了汉字，所以紧挨着称谓（先生、女士）或括注的拼音写出
   的汉字，读音对得上就报「推测的汉字」，对不上就报「读音不符」。记忆库里给这个名字
   设定过译法的不报。

实测（scripts/keyfield_shadow/）：用户记忆库 1581 条历史译文和两份合同样张各翻 3 次，
误报 0；日期规则抓到 2 处日月颠倒。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

KIND_ID = "id"
KIND_DATE = "date"
KIND_NAME_SPELLING = "name_spelling"
KIND_PINYIN_NAME = "pinyin_name"

# 复核清单里的「问题」一栏（四条规则共用一句）。
REVIEW_PROBLEM = "关键信息需核对"


@dataclass(frozen=True)
class KeyInfoIssue:
    kind: str
    message: str


# Word、PPT、Excel 段内格式标记（<g1>…</g1>、<x1/>），核对前去掉。
_MARK_RE = re.compile(r"<\s*/?\s*g\s*\d+\s*>|<\s*x\s*\d+\s*/\s*>", re.IGNORECASE)


def _clean(text: object) -> str:
    return unicodedata.normalize("NFKC", _MARK_RE.sub(" ", str(text or "")))


def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def _is_chinese_target(target_lang: str) -> bool:
    return str(target_lang or "").lower().startswith("zh")


# ── 1. 证件号、编号 ──────────────────────────────────────────────────────────

_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,4}\d{4,}[A-Z]{0,2}|\d{4,}[A-Z]{1,3})(?![A-Za-z0-9])")
# 数字后面跟着的是单位、不是编号的一部分（大写写法）。
_UNIT_SUFFIXES = frozenset(
    "MM CM M KM KG G T KN N MPA KPA PA KW W KVA VA V KV A MA HZ L ML H S MIN DB LM LX K MW GW KWH".split()
)
_ID_LIKE_RE = re.compile(r"[A-Z0-9]+")


def _check_ids(source: str, translation: str) -> list[KeyInfoIssue]:
    issues: list[KeyInfoIssue] = []
    squeezed = re.sub(r"\s+", "", translation).upper()
    for token in dict.fromkeys(_ID_RE.findall(source)):
        if token[0].isdigit() and re.sub(r"\d", "", token) in _UNIT_SUFFIXES:
            continue
        if token in squeezed:
            continue
        digits = re.sub(r"\D", "", token)
        # 数字串整个不见了归现有的数字核对管；这里只管「数字还在、字母变了」。
        if digits not in re.sub(r"\D", " ", squeezed).split():
            continue
        written = next(
            (
                candidate
                for candidate in _ID_LIKE_RE.findall(re.sub(r"\s+", "", translation.upper()))
                if digits in candidate and candidate != digits
            ),
            "",
        )
        if written:
            issues.append(KeyInfoIssue(KIND_ID, f"编号 {token} 在译文中写成了 {written}"))
        else:
            issues.append(KeyInfoIssue(KIND_ID, f"编号 {token} 在译文中缺少字母部分"))
    return issues


# ── 2. 日期 ────────────────────────────────────────────────────────────────

_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_EN_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
)
_EN_MONTHS = {name: index + 1 for index, name in enumerate(_EN_MONTH_NAMES)}
_EN_MONTHS.update({name[:3]: index + 1 for index, name in enumerate(_EN_MONTH_NAMES)})
_EN_MONTHS["sept"] = 9
_MONTHS = {**_FR_MONTHS, **_EN_MONTHS}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_RANGE_RE = re.compile(
    r"\bdu\b[^.;]*\bau\b|\bfrom\b[^.;]*\bto\b|\bentre\b[^.;]*\bet\b|\bbetween\b|至|到|～|~|—|–"
    r"|\d\s*-\s*\d{1,2}(?:er)?\s+(?:" + _MONTH_ALT + r")",
    re.IGNORECASE,
)
_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_DAY_FIRST_LANGS = frozenset({"fr", "zh", "es", "pt", "it", "de", "ar", "ru"})

_DATE_PATTERNS = (
    ("dmy_name", re.compile(r"(\d{1,2})(?:er|st|nd|rd|th)?\s+(" + _MONTH_ALT + r")\.?,?\s+(\d{4})", re.IGNORECASE)),
    ("mdy_name", re.compile(r"(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.IGNORECASE)),
    ("cn", re.compile(
        r"([0-9〇零一二三四五六七八九]{4})\s*年\s*([0-9一二三四五六七八九十]{1,3})\s*月\s*([0-9一二三四五六七八九十]{1,3})\s*[日号]"
    )),
    ("ymd", re.compile(r"(?<![\d/.-])(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?![\d/.-])")),
    ("dmy_num", re.compile(r"(?<![\d/.-])(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?![\d/.-])")),
)


def _cn_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if len(text) == 4 and all(ch in _CN_DIGITS and _CN_DIGITS[ch] < 10 for ch in text):
        return int("".join(str(_CN_DIGITS[ch]) for ch in text))
    total = current = 0
    for ch in text:
        if ch not in _CN_DIGITS:
            return None
        value = _CN_DIGITS[ch]
        if value == 10:
            total += (current or 1) * 10
            current = 0
        else:
            current = value
    return total + current


# 每种完整日期都带四位年份：没有四位年份的文字不用再逐个写法去找。
_YEAR_RE = re.compile(r"\d{4}|[〇零一二三四五六七八九]{4}")


def _complete_dates(text: str, lang: str) -> list[frozenset[tuple[int, int, int]]]:
    """每个完整日期给出它可能的 (年, 月, 日) 读法；写法有歧义时给出两种。"""
    if not _YEAR_RE.search(text):
        return []
    lang = re.split(r"[-_]", str(lang or "").lower(), maxsplit=1)[0]
    dates: list[frozenset[tuple[int, int, int]]] = []
    for kind, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if kind == "dmy_name":
                readings = {(int(match[3]), _MONTHS[match[2].lower()], int(match[1]))}
            elif kind == "mdy_name":
                readings = {(int(match[3]), _MONTHS[match[1].lower()], int(match[2]))}
            elif kind == "cn":
                parts = (_cn_number(match[1]), _cn_number(match[2]), _cn_number(match[3]))
                if None in parts:
                    continue
                readings = {parts}  # type: ignore[arg-type]
            elif kind == "ymd":
                readings = {(int(match[1]), int(match[2]), int(match[3]))}
            else:
                first, second, year = int(match[1]), int(match[2]), int(match[3])
                readings = {(year, second, first)}
                if lang not in _DAY_FIRST_LANGS:
                    readings.add((year, first, second))  # 英文、未知语言：月/日 也算
            valid = frozenset(r for r in readings if 1 <= r[1] <= 12 and 1 <= r[2] <= 31)
            if valid:
                dates.append(valid)
    return dates


def _date_label(reading: tuple[int, int, int]) -> str:
    # 写成 2026-09-29：哪种界面语言都读得懂，也不会再有日月先后的歧义。
    return f"{reading[0]:04d}-{reading[1]:02d}-{reading[2]:02d}"


def _check_dates(source: str, translation: str, source_lang: str, target_lang: str) -> list[KeyInfoIssue]:
    source_dates = _complete_dates(source, source_lang)
    if not source_dates or _RANGE_RE.search(source) or _RANGE_RE.search(translation):
        return []
    target_dates = _complete_dates(translation, target_lang)
    if len(source_dates) != len(target_dates):
        return []
    remaining = list(target_dates)
    for readings in source_dates:
        hit = next((candidate for candidate in remaining if candidate & readings), None)
        if hit is not None:
            remaining.remove(hit)
            continue
        written = min(remaining[0]) if remaining else None
        message = f"日期与原文不符：原文为 {_date_label(min(readings))}"
        if written is not None:
            message += f"，译文为 {_date_label(written)}"
        return [KeyInfoIssue(KIND_DATE, message)]
    return []


# ── 3、4. 人名 ─────────────────────────────────────────────────────────────

_TITLE = r"(?:Monsieur|Madame|Mademoiselle|M\.|Mme\.?|Mlle\.?|Mr\.?|Mrs\.?|Ms\.?|Miss|Dr\.?|Maître|Me)"
_PARTICLES = frozenset({"el", "al", "ben", "bin", "ait", "aït", "de", "du", "da", "van", "von", "der", "ou"})
_NAME_WORD = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+"
_NAME_RE = re.compile(
    _TITLE + r"\s+((?:" + _NAME_WORD + r")(?:\s+(?:" + _NAME_WORD + r"|(?i:" + "|".join(sorted(_PARTICLES)) + r")))*)"
)
# 称谓后面是职务、冠词，不是名字（Monsieur le Président、Madame la Directrice）。
_NOT_NAMES = frozenset({
    "le", "la", "les", "the", "directeur", "directrice", "président", "presidente", "présidente", "maire",
    "gérant", "gerant", "gérante", "notaire", "juge", "ministre", "consul", "représentant", "representant",
    "représentante", "secretary", "president", "director", "manager", "chairman", "minister",
})


def titled_names(text: str) -> list[str]:
    """紧跟在称谓后面的名字（原文写法）。"""
    names: list[str] = []
    for match in _NAME_RE.finditer(_clean(text)):
        words = match[1].split()
        while words and words[-1].lower() in _PARTICLES:
            words.pop()
        if not words or words[0].lower() in _NOT_NAMES or words[0].lower() in _PARTICLES:
            continue
        names.append(" ".join(words))
    return list(dict.fromkeys(names))


@lru_cache(maxsize=1)
def _reading_table() -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    from core.pinyin_readings import READINGS

    by_char: dict[str, set[str]] = {}
    for reading, chars in READINGS.items():
        for ch in chars:
            by_char.setdefault(ch, set()).add(reading)
    syllables = {reading for reading in READINGS if len(reading) >= 2 or reading in {"a", "e", "o"}}
    # 护照上 ü 写成 YU（吕 LYU、女 NYU）
    syllables |= {"lyu", "nyu", "lyue", "nyue"}
    return {ch: frozenset(values) for ch, values in by_char.items()}, frozenset(syllables)


_SURNAMES = frozenset("""
wang li zhang liu chen yang huang zhao wu zhou xu sun ma zhu hu guo he lin luo gao zheng liang xie song tang
han feng deng cao peng zeng xiao tian dong pan yuan cai jiang yu du ye cheng wei su lv lyu ding ren lu yao
shen zhong cui tan fan liao shi jin jia xia fu fang zou xiong bai meng qin qiu hou yin xue yan duan lei long
tao mao hao gu gong shao wan qian dai mo kong xiang chang kang yi ni qi niu wen zhan shang ou ouyang situ sima
zhuge shangguan murong
""".split())


def _pinyin_split(word: str) -> list[str] | None:
    _table, syllables = _reading_table()
    value = _strip_accents(word).lower().replace("ü", "v").replace("'", "").replace("’", "")
    best: list[list[str] | None] = [None] * (len(value) + 1)
    best[0] = []
    for start in range(len(value)):
        prefix = best[start]
        if prefix is None:
            continue
        for end in range(start + 1, min(len(value), start + 6) + 1):
            current = best[end]
            if value[start:end] in syllables and (current is None or len(current) > len(prefix) + 1):
                best[end] = [*prefix, value[start:end]]
    return best[len(value)]


def pinyin_name_parts(name: str) -> list[list[str]] | None:
    """名字是中国人名的拼音时返回各词的音节，否则返回 None。

    条件：2–3 个词，每个词都能完整拆成拼音音节，第一个或最后一个词是常见中国姓氏，
    总共 2–4 个音节。摩洛哥、法国、英美常见姓名组合 5429 个实测没有一个被认成。
    """
    words = [word for word in re.split(r"[\s\-]+", name) if word]
    if not 2 <= len(words) <= 3:
        return None
    parts = [_pinyin_split(word) for word in words]
    if any(part is None for part in parts):
        return None
    total = sum(len(part) for part in parts if part)
    if not 2 <= total <= 4:
        return None
    if not any("".join(part) in _SURNAMES for part in (parts[0], parts[-1]) if part):
        return None
    return [part for part in parts if part]


def _name_key(name: str) -> str:
    return " ".join(_strip_accents(name).lower().replace("-", " ").split())


def pinyin_names(text: str, *, fixed_names: Iterable[str] = ()) -> list[str]:
    """这段原文里称谓后面的中国人名拼音（记忆库已设定译法的除外）。"""
    fixed = {_name_key(name) for name in fixed_names}
    return [
        name for name in titled_names(text)
        if _name_key(name) not in fixed and pinyin_name_parts(name) is not None
    ]


def _edit_distance_within(a: str, b: str, limit: int) -> bool:
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1] <= limit


_LATIN_RUN_RE = re.compile(r"[A-Za-zÀ-ÿ'’\-]+(?:\s+[A-Za-zÀ-ÿ'’\-]+)*")


def _check_name_spelling(name: str, translation: str, target_lang: str) -> list[KeyInfoIssue]:
    key = _name_key(name)
    if key in _name_key(translation):
        return []
    cjk_target = re.split(r"[-_]", str(target_lang or "").lower(), maxsplit=1)[0] in {"zh", "ja", "ko"}
    for run in _LATIN_RUN_RE.findall(translation):
        for written in run.replace("-", " ").split():
            # 译成拉丁字母语言时，普通词首字母也大写（句首）；只拿全大写的词比，
            # 名字改成首字母大写的写法已在上面按不分大小写比过了。
            if not cjk_target and not written.isupper():
                continue
            candidate = _strip_accents(written).lower()
            for source_word in key.split():
                if len(source_word) < 4 or candidate == source_word or candidate in key.split():
                    continue
                if _edit_distance_within(source_word, candidate, 2 if len(source_word) >= 7 else 1):
                    return [KeyInfoIssue(KIND_NAME_SPELLING, f"人名拼写与原文不符：原文为 {name}，译文写成 {run}")]
    return []


_HONORIFIC_RE = re.compile(r"(?:先生|女士|小姐|夫人|博士|律师|教授|医生)")
_HAN_RE = re.compile(r"[一-鿿]+")


def _sound_matches(hanzi: str, syllables: list[str]) -> int:
    table, _syllables = _reading_table()
    if len(hanzi) != len(syllables):
        return -1
    count = 0
    for char, syllable in zip(hanzi, syllables):
        readings = table.get(char, frozenset())
        normalized = {"lyu": "lv", "nyu": "nv", "lyue": "lve", "nyue": "nve"}.get(syllable, syllable)
        if syllable in readings or normalized in readings:
            count += 1
    return count


def _anchored_hanzi(translation: str, name: str, length: int) -> list[str]:
    """紧挨着称谓（先生、女士……）或括注拼音写出的那几个汉字。"""
    found: list[str] = []
    latin = re.escape(name).replace(r"\ ", r"[\s\-]+")
    anchors = [
        # 汉字（拼音）、汉字 先生
        (re.compile(r"([一-鿿]+)\s*[（(]\s*" + latin + r"\s*[)）]", re.IGNORECASE), "before"),
        (re.compile(r"([一-鿿]+)\s*" + _HONORIFIC_RE.pattern), "before"),
        # 拼音（汉字）
        (re.compile(latin + r"\s*[（(]\s*([一-鿿]+)\s*[)）]", re.IGNORECASE), "inside"),
    ]
    for pattern, where in anchors:
        for match in pattern.finditer(translation):
            run = match.group(1)
            if where == "before":
                run = _HONORIFIC_RE.sub("", run)
                found.append(run[-length:] if len(run) >= length else "")
            else:
                found.append(run if len(run) == length else "")
    return [run for run in dict.fromkeys(found) if run]


def _check_pinyin_name(name: str, translation: str) -> list[KeyInfoIssue]:
    parts = pinyin_name_parts(name)
    if parts is None:
        return []
    syllables = [syllable for part in parts for syllable in part]
    orders = [syllables]
    if len(parts) >= 2:
        orders.append([syllable for part in parts[-1:] + parts[:-1] for syllable in part])  # 名在前、姓在后
    for hanzi in _anchored_hanzi(translation, name, len(syllables)):
        best = max(_sound_matches(hanzi, order) for order in orders)
        if best == len(syllables):
            return [KeyInfoIssue(KIND_PINYIN_NAME, f"原文人名 {name} 只有拼音，译文中的「{hanzi}」是推测的汉字")]
        if best >= max(2, len(syllables) - 1):
            return [KeyInfoIssue(KIND_PINYIN_NAME, f"原文人名 {name} 只有拼音，译文中的「{hanzi}」与拼音读音不符")]
    return []


# ── 入口 ───────────────────────────────────────────────────────────────────


def check_key_info(
    source: str,
    translation: str,
    *,
    source_lang: str = "",
    target_lang: str = "",
    fixed_names: Iterable[str] = (),
) -> list[KeyInfoIssue]:
    """核对一段原文和它的最终译文，返回对不上的关键信息（没有就是空列表）。

    ``fixed_names`` 是记忆库里设定过译法的名字：它们的汉字写法来自用户，不算推测。
    """
    src = _clean(source)
    tgt = _clean(translation)
    if not src.strip() or not tgt.strip() or src.strip() == tgt.strip():
        return []
    issues = _check_ids(src, tgt) + _check_dates(src, tgt, source_lang, target_lang)
    fixed = {_name_key(name) for name in fixed_names}
    for name in titled_names(src):
        issues += _check_name_spelling(name, tgt, target_lang)
        if _is_chinese_target(target_lang) and _name_key(name) not in fixed:
            issues += _check_pinyin_name(name, tgt)
    return list(dict.fromkeys(issues))


def review_detail(issues: Iterable[KeyInfoIssue]) -> str:
    """复核清单「处理」一栏：哪几处对不上，加一句怎么处理。"""
    items = list(issues)
    text = "；".join(issue.message for issue in items) + "。请对照原文核对。"
    if any(issue.kind == KIND_PINYIN_NAME for issue in items):
        text += "如需固定人名的汉字写法，可在记忆库中为该人名设定译法。"
    return text


def review_summary(found: Mapping[str, Sequence[KeyInfoIssue]], *, limit: int = 5) -> str:
    """一句话汇总（Excel 复核清单只按格列出，原因写在这一行里）：前几处的原因，其余计数。"""
    messages = [issue.message for issues in found.values() for issue in issues]
    shown = "；".join(messages[:limit])
    if len(messages) > limit:
        shown += f"；另有 {len(messages) - limit} 处"
    text = f"有 {len(found)} 条译文中的证件号、日期或人名与原文对不上，已标记需复核：{shown}。请对照原文核对。"
    if any(issue.kind == KIND_PINYIN_NAME for issues in found.values() for issue in issues):
        text += "如需固定人名的汉字写法，可在记忆库中为该人名设定译法。"
    return text


def name_hint_block(names: Iterable[str]) -> str:
    """随批附上的提醒：拼音人名原样保留（只在本批原文出现拼音人名、译成中文时附上）。"""
    listed = "、".join(dict.fromkeys(names))
    if not listed:
        return ""
    return (
        f"本批原文中的 {listed} 是用拼音书写的中国人名，仅凭拼音无法确定汉字。"
        "译文中按原文拼写原样保留，不要写成汉字。"
    )
