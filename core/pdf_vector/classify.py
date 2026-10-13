"""Decide per row whether to translate, keep, or send to review; translate the
rest through a caller-supplied engine; record where every final string came from.

decision        meaning
--------------  -------------------------------------------------------------
write           translated text will replace the source
keep_source     source stays (numbers, codes, model returned it unchanged)
review          source stays and a human should look (bad OCR, model issue)

translation_source: model | memory | glossary | retained_identifier | retained_numeric
                    | retained_by_model | none

With a ``memory`` (core.term_session.DocumentTermMemory) the page follows the
same translation-memory rules as Excel / Word: whole labels found in the
memory are reused, set translations are attached to each batch and checked,
and rows whose translation still misses a set translation are written but
listed for review (``term_review``).
"""
from __future__ import annotations

import gzip
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from .common import norm_text, numbers, text_has_cjk

PROMPT = (
    "你在翻译工程图纸（建筑、结构、机电、消防）上的标注。把每条{src}原文逐条翻译成简体中文工程术语。"
    "保留所有数字、单位、钢筋符号、字母代号、构件编号、比较符号、邮箱、电话、地址和公司名。"
    "缩写和代号（如 HA12、PCF、BAES）原样保留。"
    "如果某条原文是 OCR 识别错误产生的无意义字符，原样返回，不要猜测。"
    "只返回与输入一一对应的译文，不添加解释。"
)
CODE_MIN_SCORE = 0.85
WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
VOWELS = set("aeiouyàâäéèêëîïôöùûüÿAEIOUYÀÂÄÉÈÊËÎÏÔÖÙÛÜŸ")


def wordlike(tok: str) -> bool:
    return len(tok) >= 4 or (len(tok) == 3 and any(c.islower() for c in tok))


def garbage_word(tok: str) -> bool:
    if not any(c in VOWELS for c in tok):
        return True
    if re.search(r"(.)\1\1", tok.casefold()):
        return True
    if re.search(r"[^aeiouyàâäéèêëîïôöùûüÿ\W\d]{5,}", tok.casefold()):
        return True
    # mixed case inside a word (eEeS) is typical OCR noise
    inner = tok[1:]
    return any(c.isupper() for c in inner) and any(c.islower() for c in inner)


LEX_DIR = Path(__file__).resolve().parent / "lexicon"
_LEX: dict = {}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def lexicon(lang: str) -> set:
    """Word list exported from Tesseract's own language dictionary."""
    if lang not in _LEX:
        f = LEX_DIR / f"{ {'fr': 'fra', 'en': 'eng'}.get(lang, lang) }.words.txt.gz"
        words = set()
        if f.exists():
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for w in fh.read().split():
                    words.add(strip_accents(w.casefold()))
        _LEX[lang] = words
    return _LEX[lang]


def in_lexicon(tok: str, lang: str) -> bool:
    return strip_accents(tok.casefold()) in lexicon(lang)


def classify(row: dict, min_ocr: float, lang: str = "fr", strong_texts: set | None = None) -> tuple[str, str]:
    t = row["source_text"]
    if not any(c.isalpha() for c in t):
        return "keep_source", "retained_numeric"
    words = [w for w in WORD_RE.findall(t) if wordlike(w)]
    if not words:
        return "keep_source", "retained_identifier"
    if row.get("origin") == "ocr":
        score = row.get("ocr_score") or 0.0
        bad = sum(garbage_word(w) for w in words)
        if bad * 2 >= len(words) + 1 or (bad and len(words) <= 1):
            return "review", "ocr_garbage"
        if score < min_ocr:
            return "review", "low_ocr_confidence"
        # OCR cannot tell '<' from '≤' reliably on drawings; a wrong comparison
        # sign changes the engineering meaning, so these lines stay as drawn
        if re.search(r"[<>≤≥]", t):
            return "review", "ocr_operator_uncertain"
        # codes and numbers are copied into the translation verbatim, so an
        # OCR misread (BYC -> BYZ, x -> %) would become a new error on the sheet
        for w in row.get("words") or []:
            tok = w["text"]
            if any(c.isdigit() for c in tok) and any(c.isalpha() or c in "x×%*" for c in tok) and w["score"] < CODE_MIN_SCORE:
                return "review", "ocr_code_uncertain"
        long_words = [w for w in words if len(w) >= 4]
        if not long_words:
            if re.search(r"[\d=@#/]", t) and score >= 0.6:
                return "keep_source", "retained_identifier"
            return "review", "ocr_short_fragment"
        hits = sum(in_lexicon(w, lang) for w in long_words)
        lex_ok = hits * 2 >= len(long_words)
        repeated = strong_texts is not None and norm_text(t).casefold() in strong_texts
        if int(row.get("display_rotation", 0)) % 180 != 0:
            # sideways reads of horizontal text give confident junk
            if not lex_ok or score < 0.6:
                return "review", "ocr_rotated_unverified"
        elif not (lex_ok or score >= 0.9 or repeated or (score >= 0.8 and max(map(len, long_words)) >= 7)):
            return "review", "ocr_not_in_lexicon"
    return "translate", "candidate"


def harmonize(rows: list[dict], lang: str = "fr") -> int:
    """Repeated labels: a one-off read that is one edit away from a label read
    the same way at least twice elsewhere (same type size) takes that text.
    Catches real-word OCR swaps ('Haut' -> 'faut', 'Fosse' -> 'Fusse')."""
    from collections import Counter
    from difflib import SequenceMatcher

    ocr = [r for r in rows if r.get("origin") == "ocr"]
    cnt = Counter(norm_text(r["source_text"]) for r in ocr)
    common = [t for t, n in cnt.items() if n >= 2 and len(t) >= 4]
    changed = 0
    for r in ocr:
        t = norm_text(r["source_text"])
        if cnt[t] != 1 or len(t) < 4:
            continue
        best = None
        digits = re.sub(r"\D", "", t)
        tl = [w for w in WORD_RE.findall(t) if len(w) >= 4]
        t_hits = sum(in_lexicon(w, lang) for w in tl)
        for c in common:
            # letters only: never let a neighbour change a number or symbol
            # never adopt a shorter (truncated) read
            if not (len(t) <= len(c) <= len(t) + 2) or re.sub(r"\D", "", c) != digits:
                continue
            if re.sub(r"[A-Za-zÀ-ÿ\s]", "", c) != re.sub(r"[A-Za-zÀ-ÿ\s]", "", t):
                continue
            cl = [w for w in WORD_RE.findall(c) if len(w) >= 4]
            if sum(in_lexicon(w, lang) for w in cl) < max(1, t_hits):
                continue
            ratio = SequenceMatcher(None, t.casefold(), c.casefold()).ratio()
            if ratio >= 0.8 and (best is None or ratio > best[0]):
                best = (ratio, c)
        if best:
            r["ocr_text_original"] = r["source_text"]
            r["source_text"] = best[1]
            r["harmonized"] = True
            changed += 1
    return changed


def _sig(t: str) -> tuple:
    """What two reads must share to be the same label: digits and signs."""
    return (re.sub(r"\D", "", t), "".join(sorted(re.findall(r"[<>≤≥=±%/×x*+-]", t))))


def _letters_key(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(t.casefold()))


def canonicalize(rows: list[dict]) -> dict[str, str]:
    """Group reads of the same label and pick one text per group.

    OCR returns the same label in slightly different spellings ('200de haut'
    / '200de hau', 'Réservation' / 'Reservation').  Each group is sent to the
    model once and every member gets the same translation.  Reads merge only
    when their digits and signs are identical and their letters are nearly
    identical (short labels must match exactly apart from accents/case), so
    'Coupe A-A' and 'Coupe B-B' or 'Type 1' and 'Type 2' stay separate.
    """
    from collections import Counter
    from difflib import SequenceMatcher

    texts = Counter(norm_text(r["source_text"]) for r in rows)
    score: dict = {}
    for r in rows:
        score.setdefault(norm_text(r["source_text"]), []).append(r.get("ocr_score") or 1.0)
    uniq = sorted(texts, key=lambda t: (-texts[t], -sum(score[t]) / len(score[t]), -len(t)))
    canon_of: dict[str, str] = {}
    heads: list[str] = []
    for t in uniq:
        kt, st = _letters_key(t), _sig(t)
        hit = None
        for h in heads:
            if _sig(h) != st:
                continue
            kh = _letters_key(h)
            if kt == kh:
                hit = h
                break
            short = min(len(kt), len(kh))
            if short < 10:
                continue  # short labels: one letter can change the meaning
            ratio = SequenceMatcher(None, kt, kh).ratio()
            truncated = (kh.startswith(kt) or kt.startswith(kh)) and abs(len(kt) - len(kh)) <= 3
            if ratio >= 0.9 or truncated:
                hit = h
                break
        if hit is None:
            heads.append(t)
            canon_of[t] = t
        else:
            canon_of[t] = hit
    # the head of a group is its most frequent read; prefer the longer one on ties
    return canon_of


TranslateFn = Callable[[list[str], str], dict]
ProfileFn = Callable[[list[str]], str]
SOURCE_LANG_NAMES = {"fr": "法语", "en": "英语", "es": "西班牙语", "de": "德语", "it": "意大利语", "pt": "葡萄牙语"}


def _translate_with_retry(texts: list[str], prompt: str, translate: TranslateFn, errors: list[str],
                          **extra) -> tuple[dict, int]:
    """A failed batch comes back as the source text.  Words that came back
    untouched get one more try before we believe the model meant to keep them
    (company names and addresses really do stay)."""
    try:
        res = dict(translate(texts, prompt, **extra) or {})
    except Exception as exc:  # noqa: BLE001 - rows stay in review, the page keeps its source
        errors.append(f"{type(exc).__name__}: {exc}")
        res = {}
    again = [t for t in texts if norm_text(res.get(t) or "") == norm_text(t) and WORD_RE.search(t)]
    if again:
        try:
            res.update({k: v for k, v in (translate(again, prompt, **extra) or {}).items() if v})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"retry {type(exc).__name__}: {exc}")
    return res, len(again)


def classify_and_translate(rows: list[dict], *, translate: TranslateFn | None, profile: ProfileFn | None = None,
                           source_lang: str = "fr", min_ocr: float = 0.45, glossary: dict | None = None,
                           memory=None, memory_file=None, memory_location: str = "", log=None) -> dict:
    """Fill ``decision``, ``decision_reason``, ``translation`` and
    ``translation_source`` on every row; return a summary.

    ``translate(texts, system_prompt) -> {text: translation}`` and
    ``profile(texts) -> description`` are supplied by the caller (the app's
    configured translation engine), so this module has no engine knowledge.
    """
    gl = {norm_text(k).casefold(): v for k, v in (glossary or {}).items()}
    harmonize(rows, source_lang)
    to_model = []
    strong = {norm_text(r["source_text"]).casefold() for r in rows
              if r.get("origin") == "ocr" and (r.get("ocr_score") or 0) >= 0.9 and int(r.get("display_rotation", 0)) == 0}
    for r in rows:
        dec, why = classify(r, min_ocr, source_lang, strong)
        r["classification"] = why
        r["decision"], r["decision_reason"] = dec, why
        r["translation"], r["translation_source"] = r["source_text"], "none"
        if dec == "keep_source":
            r["translation_source"] = why
        elif dec == "translate":
            hit = gl.get(norm_text(r["source_text"]).casefold())
            if hit is not None:
                r.update(translation=hit, translation_source="glossary", decision="write", decision_reason="glossary")
            else:
                to_model.append(r)
    canon = canonicalize(to_model)
    for r in to_model:
        c = canon[norm_text(r["source_text"])]
        r["model_input"] = c
        if c != norm_text(r["source_text"]):
            r["same_label_as"] = c
    errors: list[str] = []
    scene, retried, res = "", 0, {}
    session, from_memory = None, {}
    if to_model and translate is not None:
        prompt = PROMPT.format(src=SOURCE_LANG_NAMES.get(source_lang, "外文"))
        if profile is not None:
            try:
                scene = profile([r["source_text"] for r in rows if any(ch.isalpha() for ch in r["source_text"])]) or ""
            except Exception as exc:  # noqa: BLE001 - the description is optional
                errors.append(f"profile {type(exc).__name__}: {exc}")
            if scene:
                from core.document_profile import append_profile
                prompt = append_profile(prompt, scene)
        inputs = sorted({r["model_input"] for r in to_model})
        if memory is not None:
            try:
                session = memory.open(inputs, profile=scene, file_path=memory_file, location=memory_location,
                                      lang_pairs=[f"{source_lang}-{memory.target_lang}"], log=log)
                from_memory = {k: v for k, v in session.hits.items() if v}
            except Exception as exc:  # noqa: BLE001 - the memory is optional, the page still translates
                errors.append(f"memory {type(exc).__name__}: {exc}")
                session = None
        ask = [text for text in inputs if text not in from_memory]
        if ask:
            extra = {"term_session": session} if session is not None else {}
            res, retried = _translate_with_retry(ask, prompt, translate, errors, **extra)
        res.update(from_memory)
    not_applied = session.not_applied_texts() if session is not None else set()
    for r in to_model:
        dst = norm_text(res.get(r["model_input"]) or "")
        r["model_output"] = dst or None
        if not dst:
            r.update(decision="review", decision_reason="model_missing")
        elif norm_text(dst).casefold() == norm_text(r["model_input"]).casefold() or not text_has_cjk(dst):
            r.update(decision="keep_source", decision_reason="retained_by_model", translation_source="retained_by_model")
        elif any(n not in numbers(dst) for n in numbers(r["source_text"])):
            r.update(decision="review", decision_reason="numeric_mismatch")
        elif sorted(re.findall(r"[<>≤≥=±]", r["source_text"])) != sorted(re.findall(r"[<>≤≥=±]", dst)):
            r.update(decision="review", decision_reason="operator_mismatch")
        else:
            reused = r["model_input"] in from_memory
            r.update(translation=dst, translation_source="memory" if reused else "model", decision="write",
                     decision_reason="memory" if reused else "model_translation")
            if r["model_input"] in not_applied:
                # written as translated (like Excel / Word), listed for review
                r["term_review"] = session.not_applied_detail(r["model_input"])
    if session is not None:
        # short words learned on this page: only text read from the PDF itself,
        # an OCR misread must never become a memory entry
        learned = {r["model_input"]: r["translation"] for r in to_model
                   if r["decision"] == "write" and r["translation_source"] == "model" and r.get("origin") != "ocr"}
        counts: dict[str, int] = {}
        for r in to_model:
            counts[r["model_input"]] = counts.get(r["model_input"], 0) + 1
        try:
            memory.close(session, learned, counts=counts)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"memory {type(exc).__name__}: {exc}")

    def count(key):
        c: dict = {}
        for r in rows:
            c[r[key]] = c.get(r[key], 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    return {"decision": count("decision"), "decision_reason": count("decision_reason"),
            "translation_source": count("translation_source"), "candidate_rows": len(to_model),
            "unique_requests": len({r["model_input"] for r in to_model}),
            "merged_variants": sum(1 for r in to_model if r.get("same_label_as")),
            "scene_profile": scene, "retried_unchanged": retried, "errors": errors[:5],
            "memory_hits": len(from_memory), "term_review": sum(1 for r in rows if r.get("term_review"))}
