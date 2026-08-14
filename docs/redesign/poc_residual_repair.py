# -*- coding: utf-8 -*-
"""
PoC：残留中文「分类→修复阶梯→文档级一致性→报告锚定」重构的核心机制验证。
离线运行，不调 API。语料 = 真实双语输出文档。
"""
import re
import sys
import difflib
from docx import Document

CJK_RE = re.compile(r"[一-鿿]")
CJK_SPAN_RE = re.compile(r"[一-鿿]+")
CN_NUM = "一二三四五六七八九十"
CN_NUM_VAL = {c: i + 1 for i, c in enumerate(CN_NUM)}
CN_NUM_VAL["十"] = 10
ROMAN = {1:"I",2:"II",3:"III",4:"IV",5:"V",6:"VI",7:"VII",8:"VIII",9:"IX",10:"X"}
FR_ORD = {"première":1,"premier":1,"deuxième":2,"seconde":2,"troisième":3,
          "quatrième":4,"cinquième":5,"sixième":6,"septième":7,
          "huitième":8,"neuvième":9,"dixième":10}

# ---------- 1. 残留分类器 ----------
NUMBERING_PAT = re.compile(
    r"^\s*(?:[（(]\s*[%s]{1,3}\s*[）)]|[%s]{1,3}\s*[、\.．]|第\s*[%s0-9０-９]{1,3}\s*[节章条款项部分]?)" % (CN_NUM, CN_NUM, CN_NUM))
DATE_UNIT_PAT = re.compile(r"[0-9０-９]+\s*[年月日号周岁元万亿]")

def latin_word_count(text):
    return len(re.findall(r"[A-Za-zÀ-ſ]{2,}", text))

def classify_spans(target_text):
    """对译文中每个 CJK 连续片段分类。返回 [(span, start, category)]"""
    out = []
    prefix = NUMBERING_PAT.match(target_text)
    prefix_done = False
    for m in CJK_SPAN_RE.finditer(target_text):
        span, s = m.group(), m.start()
        if prefix and s < prefix.end():
            if prefix_done:
                continue                    # 同一前缀里的多个片段并成一条
            prefix_done = True
            span = target_text[prefix.start():prefix.end()].strip()
            cat = "numbering_prefix"        # 结构序号：确定性修复
        elif DATE_UNIT_PAT.search(target_text[max(0, s-8):m.end()+1]):
            cat = "cn_date_unit"            # 中文日期/数量单位：现有规则，阻断
        elif set(span) <= {"万", "亿"}:
            cat = "quantity_unit"           # 放行 + 记录
        elif len(span) <= 3 and latin_word_count(target_text) >= 3:
            cat = "term_fragment"           # 术语尾巴：外科修补
        else:
            cat = "sentence_block"          # 成句未译：反馈重译
        out.append((span, s, cat))
    return out

# ---------- 2. 同级序号惯例探测 + 确定性修复 ----------
ENUM_FAMILIES = [
    ("paren_roman",  re.compile(r"^\s*\(\s*([IVX]{1,4})\s*\)")),
    ("paren_arabic", re.compile(r"^\s*\(\s*(\d{1,2})\s*\)")),
    ("arabic_dot",   re.compile(r"^\s*(\d{1,2})[\.．]\s")),
]
def sibling_convention(pairs):
    """只在「源段以（X）开头」的同族段落里投票，避免被正文步骤编号污染。"""
    votes = {}
    fam_pat = re.compile(r"^\s*[（(]\s*[%s]{1,3}\s*[）)]" % CN_NUM)
    for si, ti, src, tgt in pairs:
        if not fam_pat.match(src):
            continue
        for fam, pat in ENUM_FAMILIES:
            if pat.match(tgt):
                votes[fam] = votes.get(fam, 0) + 1
    if votes:
        return max(votes, key=votes.get)
    return "paren_arabic"  # 无同级证据时的目标语默认

def deterministic_numbering_fix(target_text, convention):
    m = re.match(r"^(\s*)[（(]\s*([%s]{1,3})\s*[）)]" % CN_NUM, target_text)
    if not m:
        return None
    val = CN_NUM_VAL.get(m.group(2))
    if val is None:
        return None
    if convention == "paren_roman":
        repl = "(%s)" % ROMAN[val]
    elif convention == "paren_arabic":
        repl = "(%d)" % val
    else:
        repl = "%d." % val
    return target_text[:m.start()] + m.group(1) + repl + target_text[m.end():]

# ---------- 3. 外科修补验收器（diff 受限） ----------
def surgical_repair_ok(original, repaired, span_start, span_len, window=12):
    """修补稿只允许改残留片段附近；其余部分必须保持原样。数字必须全保留。"""
    if CJK_RE.search(repaired):
        leftover = classify_spans(repaired)
        if any(c not in ("quantity_unit",) for _, _, c in leftover):
            return False, "repaired text still has blocking CJK"
    lo, hi = max(0, span_start - window), span_start + span_len + window
    sm = difflib.SequenceMatcher(None, original, repaired, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i1 < lo or i2 > hi:
            return False, f"edit outside window: {tag} orig[{i1}:{i2}]={original[i1:i2]!r}"
    nums_o = re.findall(r"\d+(?:\.\d+)?", original)
    nums_r = re.findall(r"\d+(?:\.\d+)?", repaired)
    if sorted(nums_o) != sorted(nums_r):
        return False, f"numbers changed: {nums_o} -> {nums_r}"
    return True, "ok"

# ---------- 4. 文档级标题一致性 ----------
SECTION_FR_PATS = [
    ("section_n",   re.compile(r"^\s*Section\s+(\d{1,2})\b", re.I)),
    ("nieme_section", re.compile(r"^\s*([A-Za-zÀ-ſ]+)\s+section\s*[—\-–]?", re.I)),
]
def heading_consistency(pairs):
    """pairs: [(src, tgt, tgt_idx)] 其中 src 以 第X节 开头。返回 (majority, outliers)"""
    tagged = []
    for src, tgt, idx in pairs:
        form = None
        m = SECTION_FR_PATS[0][1].match(tgt)
        if m:
            form = ("section_n", int(m.group(1)))
        else:
            m = SECTION_FR_PATS[1][1].match(tgt)
            if m and m.group(1).lower() in FR_ORD:
                form = ("nieme_section", FR_ORD[m.group(1).lower()])
        tagged.append((src, tgt, idx, form))
    votes = {}
    for *_, form in tagged:
        if form:
            votes[form[0]] = votes.get(form[0], 0) + 1
    if not votes:
        return None, []
    majority = max(votes, key=votes.get)
    outliers = [(s, t, i, f) for s, t, i, f in tagged if f and f[0] != majority]
    return majority, outliers

def heading_fix(tgt, form_val):
    m = SECTION_FR_PATS[1][1].match(tgt)
    rest = tgt[m.end():].lstrip(" —–-")
    return f"Section {form_val} {rest}"

# ---------- 5. 配对 + 回放 ----------
def cjk_ratio(text):
    ns = re.sub(r"\s+", "", text)
    if not ns:
        return 0.0
    return sum(1 for ch in ns if CJK_RE.match(ch)) / len(ns)

def pair_paragraphs(doc):
    """双语文档：中文段后紧跟译文段。返回 [(src_idx, tgt_idx, src, tgt)]（均为 1-based 输出文档段号）"""
    paras = [(i + 1, p.text.strip()) for i, p in enumerate(doc.paragraphs)]
    nonempty = [(i, t) for i, t in paras if t]
    pairs = []
    k = 0
    while k < len(nonempty) - 1:
        i1, t1 = nonempty[k]
        i2, t2 = nonempty[k + 1]
        if cjk_ratio(t1) > 0.3 and cjk_ratio(t2) < 0.3:
            pairs.append((i1, i2, t1, t2))
            k += 2
        else:
            k += 1
    return pairs

def source_anchor(pairs, tgt_idx):
    """输出文档段号 → 源文档段号（源段序 = 该源段在配对序列中的序号，与源文档非空段一一对应）"""
    for n, (si, ti, s, t) in enumerate(pairs, 1):
        if ti == tgt_idx:
            return si, s
    return None, None

def replay(path):
    doc = Document(path)
    pairs = pair_paragraphs(doc)
    conv = sibling_convention(pairs)
    actions = {"clean": 0}
    findings = []
    for si, ti, src, tgt in pairs:
        spans = classify_spans(tgt)
        if not spans:
            actions["clean"] += 1
            continue
        for span, start, cat in spans:
            actions[cat] = actions.get(cat, 0) + 1
            fix = None
            if cat == "numbering_prefix":
                fix = deterministic_numbering_fix(tgt, conv)
            findings.append((si, ti, span, cat, tgt[:60], (fix or "")[:60]))
    sec_pairs = [(s, t, ti) for si, ti, s, t in pairs if re.match(r"^第[%s]{1,3}节" % CN_NUM, s)]
    majority, outliers = heading_consistency(sec_pairs)
    return {"path": path, "pairs": len(pairs), "convention": conv, "actions": actions,
            "findings": findings, "heading_majority": majority,
            "heading_outliers": [(s[:24], t[:44], i) for s, t, i, f in outliers],
            "heading_fixes": [heading_fix(t, f[1])[:60] for s, t, i, f in outliers]}

def test_surgical_validator():
    orig = ("Mise en œuvre du matériau de scellement : utiliser le mortier de réparation sans retrait "
            "à haute résistance pour injection Vetogrout CG518, remplir la rainure en V 型槽, "
            "lisser la surface et assurer une cure pendant au moins 3 jours.")
    s = orig.index("型槽")
    cases = [
        ("好修补：只删残留", orig.replace(" 型槽", ""), True),
        ("好修补：残留处改写", orig.replace("rainure en V 型槽", "rainure en V"), True),
        ("坏修补：顺手改了远处措辞", orig.replace(" 型槽", "").replace("Mise en œuvre", "Application"), False),
        ("坏修补：数字被改", orig.replace(" 型槽", "").replace("3 jours", "5 jours"), False),
        ("坏修补：整句重写", "Remplir la rainure en V avec le mortier CG518 et curer 3 jours.", False),
        ("坏修补：残留还在", orig, False),
    ]
    print("=" * 100)
    print("外科修补验收器（diff 受限）:")
    for name, repaired, expect in cases:
        ok, why = surgical_repair_ok(orig, repaired, s, 2)
        mark = "✓" if ok == expect else "✗✗✗ 预期不符"
        print(f"  {mark} {name}: accepted={ok} ({why})")

if __name__ == "__main__":
    test_surgical_validator()
    corpus = sys.argv[1:]
    for path in corpus:
        r = replay(path)
        print("=" * 100)
        print("DOC:", r["path"].split("/")[-1])
        print(f"配对段落: {r['pairs']}   同级序号惯例: {r['convention']}   动作统计: {r['actions']}")
        for si, ti, span, cat, tgt, fix in r["findings"]:
            print(f"  [输出p{ti} ← 源锚p{si}] {cat:16s} 残留={span!r:8s} | {tgt}")
            if fix:
                print(f"      -> 确定性修复: {fix}")
        print(f"  节标题主惯例: {r['heading_majority']}  离群 {len(r['heading_outliers'])} 处:")
        for (s, t, i), f in zip(r["heading_outliers"], r["heading_fixes"]):
            print(f"      [输出p{i}] {t}")
            print(f"        -> {f}")
