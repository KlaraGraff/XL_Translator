"""关键信息核对原型（只判断，不改译文）。实测用，不是应用代码。

原则：只在找到「明确对不上」的证据时才报；找不到对应、拿不准的一律不报。
汉字读音表 pinyin_readings.json 由 pypinyin（MIT License）的字典生成，只为实测时不必另装依赖。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_READINGS: dict[str, list[str]] = {
    ch: value.split() for ch, value in json.loads(
        (Path(__file__).with_name("pinyin_readings.json")).read_text(encoding="utf-8")
    ).items()
}


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


# ── R1 证件号/编号：大写字母与 4 位以上数字连写的整串 ────────────────
ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,4}\d{4,}[A-Z]{0,2}|\d{4,}[A-Z]{1,3})(?![A-Za-z0-9])")
UNIT_LETTERS = {"MM", "CM", "M", "KM", "KG", "G", "T", "KN", "N", "MPA", "KPA", "PA", "KW", "W", "KVA",
                "VA", "V", "KV", "A", "MA", "HZ", "L", "ML", "H", "S", "MIN", "DB", "LM", "LX", "K", "MW", "GW", "KWH"}


def check_ids(source: str, target: str) -> list[Finding]:
    found: list[Finding] = []
    tgt = re.sub(r"\s+", "", nfkc(target)).upper()
    for token in dict.fromkeys(ID_RE.findall(nfkc(source))):
        # 单位只出现在数字后面（1500MM、2000KVA）；字母在前的（K19498）是编号
        if token[0].isdigit() and re.sub(r"\d", "", token) in UNIT_LETTERS:
            continue
        if token in tgt:
            continue
        digits = re.sub(r"\D", "", token)
        # 只在数字串还在、字母却变了的时候报：数字整个不见了由现有的数字核对负责
        if digits in re.sub(r"\D", " ", tgt).split():
            found.append(Finding("证件号", f"编号 {token} 在译文中被改动"))
    return found


# ── R2 日期：只比较写法明确的完整日期，数量相同且对不上才报 ──────────
FR_MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
             "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
             "décembre": 12, "decembre": 12}
EN_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                               "august", "september", "october", "november", "december"])}
EN_MONTHS.update({m[:3]: i for m, i in list(EN_MONTHS.items())})
EN_MONTHS["sept"] = 9
MONTH_WORDS = {**FR_MONTHS, **EN_MONTHS}
MONTH_ALT = "|".join(sorted(MONTH_WORDS, key=len, reverse=True))
RANGE_RE = re.compile(r"\bdu\b[^.;]*\bau\b|\bfrom\b[^.;]*\bto\b|\bentre\b[^.;]*\bet\b|\bbetween\b|至|到|～|~|—|–|\d\s*-\s*\d{1,2}\s*(?:" + MONTH_ALT + r")", re.IGNORECASE)
CN_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if len(text) == 4 and all(ch in CN_NUM for ch in text):  # 二〇二六
        return int("".join(str(CN_NUM[ch]) for ch in text))
    total, cur = 0, 0
    for ch in text:
        if ch not in CN_NUM:
            return None
        v = CN_NUM[ch]
        if v == 10:
            total += (cur or 1) * 10
            cur = 0
        else:
            cur = v
    return total + cur


def complete_dates(text: str, lang: str) -> list[frozenset]:
    """每个完整日期返回它可能的 (年, 月, 日) 读法集合；写法有歧义时给出多种读法。"""
    text = nfkc(text)
    out: list[frozenset] = []
    spans: list[tuple[int, int]] = []

    def add(match, readings):
        readings = {r for r in readings if 1 <= r[1] <= 12 and 1 <= r[2] <= 31}
        if readings:
            out.append(frozenset(readings))
            spans.append(match.span())

    for m in re.finditer(r"(\d{1,2})(?:er|st|nd|rd|th)?\s+(" + MONTH_ALT + r")\.?,?\s+(\d{4})", text, re.IGNORECASE):
        add(m, {(int(m[3]), MONTH_WORDS[m[2].lower()], int(m[1]))})
    for m in re.finditer(r"(" + MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text, re.IGNORECASE):
        add(m, {(int(m[3]), MONTH_WORDS[m[1].lower()], int(m[2]))})
    for m in re.finditer(r"([0-9〇零一二三四五六七八九]{4})\s*年\s*([0-9一二三四五六七八九十]{1,3})\s*月\s*([0-9一二三四五六七八九十]{1,3})\s*[日号]", text):
        y, mo, d = _cn_int(m[1]), _cn_int(m[2]), _cn_int(m[3])
        if None not in (y, mo, d):
            add(m, {(y, mo, d)})
    for m in re.finditer(r"(?<![\d/.-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d/.-])", text):
        add(m, {(int(m[1]), int(m[2]), int(m[3]))})
    for m in re.finditer(r"(?<![\d/.-])(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?![\d/.-])", text):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if lang in ("fr", "zh"):
            add(m, {(y, b, a)})       # 法文和中文里的数字日期都是 日/月/年
        else:
            add(m, {(y, b, a), (y, a, b)})  # 英文可能是 月/日/年，两种读法都算对
    return out


def check_dates(source: str, target: str, source_lang: str, target_lang: str) -> list[Finding]:
    if RANGE_RE.search(nfkc(source)) or RANGE_RE.search(nfkc(target)):
        return []
    src = complete_dates(source, source_lang)
    tgt = complete_dates(target, target_lang)
    if not src or len(src) != len(tgt):
        return []
    remaining = list(tgt)
    for readings in src:
        hit = next((t for t in remaining if t & readings), None)
        if hit is None:
            shown = min(readings)
            return [Finding("日期", f"原文日期 {shown[0]}-{shown[1]:02d}-{shown[2]:02d} 在译文中对不上")]
        remaining.remove(hit)
    return []


# ── R3/R4 人名：只看紧跟在 Monsieur / Madame / Mr 等称谓后的名字 ─────
TITLE_RE = r"(?:Monsieur|Madame|Mademoiselle|M\.|Mme\.?|Mlle\.?|Mr\.?|Mrs\.?|Ms\.?|Miss|Dr\.?|Maître|Me)"
PARTICLES = {"el", "al", "ben", "bin", "ait", "aït", "de", "du", "da", "van", "von", "der", "ou"}
NAME_WORD = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+"
NAME_RE = re.compile(TITLE_RE + r"\s+((?:" + NAME_WORD + r")(?:\s+(?:" + NAME_WORD + r"|(?i:el|al|ben|bin|ait|aït|de|du|da|van|von|der|ou)))*)")
TITLE_STOP = {"le", "la", "les", "the", "directeur", "directrice", "président", "presidente", "maire", "gérant",
              "gerant", "notaire", "juge", "ministre", "consul", "representant", "représentant"}


def titled_names(text: str) -> list[str]:
    names = []
    for m in NAME_RE.finditer(nfkc(text)):
        words = m[1].split()
        while words and words[-1].lower() in PARTICLES:
            words.pop()
        if not words or words[0].lower() in TITLE_STOP or words[0].lower() in PARTICLES:
            continue
        names.append(" ".join(words))
    return list(dict.fromkeys(names))


SYLLABLES = {
    plain for values in _READINGS.values() for plain in values
    if len(plain) >= 2 or plain in {"a", "e", "o"}
}
# 护照上 ü 写作 YU（吕 LYU、女 NYU）
SYLLABLES |= {"lyu", "nyu", "lyue", "nyue"}
SURNAMES = frozenset(["wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou", "xu", "sun", "ma", "zhu", "hu", "guo", "he", "lin", "luo", "gao", "zheng", "liang", "xie", "song", "tang", "han", "feng", "deng", "cao", "peng", "zeng", "xiao", "tian", "dong", "pan", "yuan", "cai", "jiang", "yu", "du", "ye", "cheng", "wei", "su", "lv", "lyu", "ding", "ren", "lu", "yao", "shen", "zhong", "cui", "tan", "fan", "liao", "shi", "jin", "jia", "xia", "fu", "fang", "zou", "xiong", "bai", "meng", "qin", "qiu", "hou", "yin", "xue", "yan", "duan", "lei", "long", "tao", "mao", "hao", "gu", "gong", "shao", "wan", "qian", "dai", "mo", "kong", "xiang", "chang", "kang", "yi", "ni", "qi", "niu", "wen", "zhan", "shang", "ou", "ouyang", "situ", "sima", "zhuge", "shangguan", "murong"])


def pinyin_split(word: str) -> list[str] | None:
    w = strip_accents(word).lower().replace("ü", "v").replace("'", "").replace("’", "")
    best: list[list[str] | None] = [None] * (len(w) + 1)
    best[0] = []
    for i in range(len(w)):
        if best[i] is None:
            continue
        for j in range(i + 1, min(len(w), i + 6) + 1):
            if w[i:j] in SYLLABLES and (best[j] is None or len(best[j]) > len(best[i]) + 1):
                best[j] = best[i] + [w[i:j]]
    return best[len(w)]


def chinese_pinyin_name(name: str) -> list[list[str]] | None:
    """名字整体能拆成拼音、其中一个词是常见中国姓氏、共 2–4 个音节时，返回各词的音节。"""
    words = [w for w in re.split(r"[\s\-]+", name) if w]
    if not 1 <= len(words) <= 3:
        return None
    parts = [pinyin_split(w) for w in words]
    if any(p is None for p in parts):
        return None
    total = sum(len(p) for p in parts)
    if not 2 <= total <= 4:
        return None
    if len(words) == 1 or not any("".join(p) in SURNAMES for p in (parts[0], parts[-1])):
        return None
    return parts


def readings(hanzi: str) -> list[set[str]]:
    return [set(_READINGS.get(ch, [ch])) for ch in hanzi]


def _sound_match(hanzi: str, syllables: list[str]) -> int:
    rs = readings(hanzi)
    if len(rs) != len(syllables):
        return -1
    norm = lambda s: s.replace("yu", "v") if s in {"lyu", "nyu", "lyue", "nyue"} else s
    return sum(1 for r, s in zip(rs, syllables) if norm(s) in r or s in r)


HONORIFICS = "先生|女士|小姐|博士|律师|教授|夫人"
HAN_RUN = re.compile(r"[一-鿿]{2,4}")


def check_names(source: str, target: str, target_lang: str, memory_names: dict[str, str] | None = None) -> list[Finding]:
    memory_names = {k.lower(): v for k, v in (memory_names or {}).items()}
    found: list[Finding] = []
    tgt = nfkc(target)
    tgt_fold = strip_accents(tgt).lower()
    tgt_latin = re.findall(r"[A-Za-zÀ-ÿ'’\-]+(?:\s+[A-Za-zÀ-ÿ'’\-]+)*", tgt)
    for name in titled_names(source):
        key = strip_accents(name).lower()
        present = re.sub(r"\s+", " ", key) in re.sub(r"\s+", " ", tgt_fold)
        if not present:
            # R3：译文里有一个拼法很接近、但不完全相同的拉丁字母名字 → 拼写被改
            for words in tgt_latin:
                cand = strip_accents(words).lower()
                for src_word in key.split():
                    for tw in cand.split():
                        if len(src_word) >= 4 and tw != src_word and _edit1or2(src_word, tw):
                            found.append(Finding("人名拼写", f"原文 {name}，译文写成 {words}"))
        if target_lang.startswith("zh"):
            parts = chinese_pinyin_name(name)
            if parts is None or key in memory_names:
                continue
            syllables = [s for p in parts for s in p]
            orders = [syllables]
            if len(parts) >= 2:
                orders.append([s for p in parts[-1:] + parts[:-1] for s in p])  # 名在前姓在后
            for run in HAN_RUN.findall(re.sub(HONORIFICS, " ", tgt)):
                for k in range(len(run)):
                    seg = run[k:k + len(syllables)]
                    if len(seg) != len(syllables):
                        continue
                    best = max(_sound_match(seg, o) for o in orders)
                    if best == len(syllables):
                        found.append(Finding("拼音人名", f"{name} 原文只有拼音，译文写成「{seg}」是推测的汉字"))
                    elif best >= len(syllables) - 1 and best >= 2:
                        found.append(Finding("拼音人名", f"{name} 被写成「{seg}」，汉字读音与拼音对不上"))
    return list(dict.fromkeys(found))


def _edit1or2(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    # 长名字允许差 2 个字母，短名字只允许差 1 个
    return prev[-1] <= (2 if len(a) >= 7 else 1)


def check_all(source: str, target: str, source_lang: str, target_lang: str, memory_names=None) -> list[Finding]:
    return (check_ids(source, target) + check_dates(source, target, source_lang, target_lang)
            + check_names(source, target, target_lang, memory_names))
