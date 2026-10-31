"""Decide per row whether to translate, keep, or send to review; translate the
rest through a caller-supplied engine; record where every final string came from.

decision        meaning
--------------  -------------------------------------------------------------
write           translated text will replace the source
keep_source     source stays (numbers, codes, already in the target language,
                model returned it unchanged)
review          source stays and a human should look (bad OCR, model issue)

The target language is any language whose text is written letter after letter
(core/pdf_vector/fonts.py); whether a line is already in the target language
follows CAD (core.cad_language.classify_text_language).  The source may be any
language the PDF text layer holds, Chinese included.  Outline lettering is read
by Tesseract (Latin script, checked against word lists) or by the PP-OCR models
of the text recognition component (Chinese, Japanese and Latin script; Chinese and
Japanese lines are checked on the model's own character probabilities).

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
import inspect
import re
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path

from core.cad_language import (
    NON_LATIN_SCRIPT_RE,
    TARGET,
    classify_text_language,
    is_residual_foreign_text,
    target_script_pattern,
)

from .common import norm_text, numbers

PROMPT = (
    "你在翻译工程图纸（建筑、结构、机电、消防）上的标注。把每条{src}原文逐条翻译成{dst}工程术语。"
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


# PP-OCR reads (core.pdf_vector.cjk_ocr) of Chinese / Japanese lines: the model's
# own character probabilities stand in for the word lists used on Latin script.
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
# Measured on real drawings with a text layer as the answer (2026-10): of the lines
# that would be translated, those under 0.9 were mostly misread (5 of 8), and a
# character under 0.6 marks a likely look-alike (「压」 read as 「分」).
CJK_MIN_SCORE = 0.9         # whole line
CJK_MIN_CHAR = 0.6          # any one character
CJK_SINGLE_MIN = 0.95       # a lone character: as often a symbol read as one
OPERATOR_MIN = 0.9
# a lone character that is as likely a drawing symbol (a square, a cross, a bar, an arrow)
SYMBOL_LIKE = set("口囗回田日目一二三十丁丨工土士王干川人八入乙了卜冂凵匚亅中个丫")


def _classify_cjk_read(row: dict, t: str) -> tuple[str, str]:
    words = row.get("words") or []
    score = row.get("ocr_score") or 0.0
    probs = [w["score"] for w in words if CJK_RE.search(w["text"])]
    if score < CJK_MIN_SCORE or (probs and min(probs) < CJK_MIN_CHAR):
        return "review", "low_ocr_confidence"
    letters = [c for c in t if c.isalpha()]
    if len(letters) == 1:
        if letters[0] in SYMBOL_LIKE:
            return "keep_source", "retained_identifier"
        if score < CJK_SINGLE_MIN:
            return "review", "ocr_short_fragment"
    if re.search(r"[<>≤≥]", t):
        signs = [w["score"] for w in words if re.search(r"[<>≤≥]", w["text"])]
        if not signs or min(signs) < OPERATOR_MIN:
            return "review", "ocr_operator_uncertain"
    for w in words:
        tok = w["text"]
        if any(c.isdigit() for c in tok) and any(c.isalpha() or c in "x×%*" for c in tok) and w["score"] < CODE_MIN_SCORE:
            return "review", "ocr_code_uncertain"
    return "translate", "candidate"


def classify(row: dict, min_ocr: float, lang: str = "fr", strong_texts: set | None = None,
             target_lang: str = "zh") -> tuple[str, str]:
    t = row["source_text"]
    if not any(c.isalpha() for c in t):
        return "keep_source", "retained_numeric"
    words = [w for w in WORD_RE.findall(t) if wordlike(w)]
    if not words and not NON_LATIN_SCRIPT_RE.search(t):
        return "keep_source", "retained_identifier"
    if classify_text_language(t, target_lang) == TARGET:
        # 已是目标语言（中文图纸译成中文时的中文标注、法文图纸里的法文）；拿不准的照常翻译
        return "keep_source", "already_target"
    if row.get("origin") == "ocr" and row.get("engine") == "ppocr" and CJK_RE.search(t):
        return _classify_cjk_read(row, t)
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
    # letters of every script count (Chinese labels differ only in their characters)
    return "".join(c for c in strip_accents(t.casefold()) if c.isalnum())


def canonicalize(rows: list[dict]) -> dict[str, str]:
    """Group reads of the same label and pick one text per group.

    OCR returns the same label in slightly different spellings ('200de haut'
    / '200de hau', 'Réservation' / 'Reservation').  Each group is sent to the
    model once and every member gets the same translation.  Reads merge only
    when their digits and signs are identical and their letters are nearly
    identical (short labels must match exactly apart from accents/case), so
    'Coupe A-A' and 'Coupe B-B' or 'Type 1' and 'Type 2' stay separate.
    Near-identical merging is for OCR reads only: text read from the PDF itself
    is exact, and two such labels that differ by one character ('一层平面图（东区）'
    / '一层平面图（西区）') are two labels.
    """
    from collections import Counter
    from difflib import SequenceMatcher

    texts = Counter(norm_text(r["source_text"]) for r in rows)
    score: dict = {}
    exact: set[str] = set()
    for r in rows:
        score.setdefault(norm_text(r["source_text"]), []).append(r.get("ocr_score") or 1.0)
        if r.get("origin") != "ocr":
            exact.add(norm_text(r["source_text"]))
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
            if short < 10 or t in exact or h in exact:
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
SOURCE_LANG_NAMES = {"fr": "法语", "en": "英语", "es": "西班牙语", "de": "德语", "it": "意大利语", "pt": "葡萄牙语",
                     "zh": "中文", "ja": "日语", "ko": "韩语"}


def target_language_name(target_lang: str) -> str:
    """提示词里的目标语言：中文按「简体中文」（与以前一样），繁体写明用字，其余按语言列表里的名称。"""
    code = str(target_lang or "").strip()
    if code.lower() in ("", "zh", "zh-cn", "zh-hans"):
        return "简体中文"
    from core.language_registry import get_target_lang_prompt_name

    return get_target_lang_prompt_name(code)


def translation_problem(source: str, translation: str, target_lang: str) -> str:
    """译文能不能写回：空串可以；``retained_by_model`` 是模型原样退回或没译成目标语言的
    文字（保留原文），``residual_source`` 是译文里还夹着原文的文字（列进复核）。"""
    if norm_text(translation).casefold() == norm_text(source).casefold():
        return "retained_by_model"
    pattern = target_script_pattern(target_lang)
    if pattern is not None:
        # 中文、日文、韩文、俄文等：译文里要有目标语言自己的文字（与以前「须含汉字」一样）
        return "" if re.search(pattern, translation) else "retained_by_model"
    if is_residual_foreign_text(translation, target_lang):
        # 译成西文，译文里还留着汉字等原文文字：半截没译
        return "residual_source"
    return ""


def page_rule_problem(source: str, translation: str, target_lang: str) -> str:
    """原位替换在公共规则之外多查的几条：译文里夹着原文、数字、比较符号。

    没过的不再直接列进复核，而是和公共规则没过的一起交给模型复查（core/translation_review）；
    这里返回给模型看的一句话，通过时为空串。"""
    dst = norm_text(translation)
    if not dst or translation_problem(source, dst, target_lang) == "retained_by_model":
        # 模型原样退回、没译成目标语言：页面自己的「再问一次、仍原样就保留」管
        return ""
    if translation_problem(source, dst, target_lang) == "residual_source":
        return "译文里还夹着没翻译的原文文字"
    lost = [n for n in numbers(source, dates=True) if n not in numbers(dst, dates=True)]
    if lost:
        return "原文中的数字在译文里没找到：" + "、".join(dict.fromkeys(lost))
    if sorted(re.findall(r"[<>≤≥=±]", source)) != sorted(re.findall(r"[<>≤≥=±]", dst)):
        return "译文里的比较符号（< > ≤ ≥ = ±）和原文不一致"
    return ""


def takes_extra_check(fn) -> bool:
    """The translate hook takes ``extra_check`` (this page's own rules, reviewed by the
    model together with the shared ones); a wrapper says so with an attribute."""
    if fn is None:
        return False
    if getattr(fn, "takes_extra_check", False):
        return True
    try:
        return "extra_check" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _collect_review(result, accepted: dict | None, rejected: dict | None) -> None:
    """The app's service hands back what the model review decided (TranslationResults)."""
    if accepted is not None:
        accepted.update(getattr(result, "review_accepted", None) or {})
    if rejected is not None:
        rejected.update(getattr(result, "rejected", None) or {})


def supports_resend_unchanged(fn) -> bool:
    """The translate hook takes ``resend_unchanged`` (see _translate_with_retry); a
    wrapper that passes it through says so with a ``resends_unchanged`` attribute."""
    if fn is None:
        return False
    if getattr(fn, "resends_unchanged", False):
        return True
    try:
        return "resend_unchanged" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def takes_source_lang(fn) -> bool:
    """The translate hook takes ``source_lang`` (the page's own source language, so the
    engine's request says "from Chinese" rather than "from auto"); a wrapper that
    passes it through says so with a ``takes_source_lang`` attribute."""
    if fn is None:
        return False
    if getattr(fn, "takes_source_lang", False):
        return True
    try:
        return "source_lang" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _reason(exc: BaseException) -> str:
    """The sentence the run log shows for a failed request (「原位替换：…」).

    It used to read 「ApiKeyTemporarilyUnavailableError: 模型服务持续提示繁忙…」: the
    class name means nothing to the user.  Same rule as the batch notices
    (core.engine_dispatcher.user_facing_batch_reason); the raw error goes to the
    debug log there.
    """
    from core.engine_dispatcher import user_facing_batch_reason

    return user_facing_batch_reason(exc)


def _translate_with_retry(texts: list[str], prompt: str, translate: TranslateFn, errors: list[str],
                          accepted: dict | None = None, rejected: dict | None = None,
                          **extra) -> tuple[dict, int]:
    """Texts the service gave up on are left out of the result (the app's
    translate hook drops them, see core/text_translation_service.py); words that
    came back untouched may be a model keeping a name.  Both get one more try
    before we believe it: still missing means "no translation" (review), still
    untouched means the model meant to keep it (company names and addresses
    really do stay)."""
    def needs_another_try(text: str, translated: str | None) -> bool:
        has_words = bool(WORD_RE.search(text)) or bool(NON_LATIN_SCRIPT_RE.search(text))
        return has_words and (not translated or norm_text(translated) == norm_text(text))

    if supports_resend_unchanged(translate):
        # The app's service resends a batch's untouched words as soon as that batch
        # returns, instead of waiting here for the slowest batch first.
        picked: set[str] = set()
        lock = threading.Lock()

        def pick(text: str, translated: str | None) -> bool:
            if not needs_another_try(text, translated):
                return False
            with lock:
                picked.add(text)
            return True

        try:
            raw = translate(texts, prompt, resend_unchanged=pick, **extra) or {}
            _collect_review(raw, accepted, rejected)
            res = dict(raw)
        except Exception as exc:  # noqa: BLE001 - rows stay in review, the page keeps its source
            errors.append(_reason(exc))
            return {}, 0
        return res, len(picked)
    try:
        raw = translate(texts, prompt, **extra) or {}
        _collect_review(raw, accepted, rejected)
        res = dict(raw)
    except Exception as exc:  # noqa: BLE001 - rows stay in review, the page keeps its source
        errors.append(_reason(exc))
        return {}, 0
    # 规则没过、复查也没确认的不再问第二次：那不是「没拿到译文」。
    again = [t for t in texts if needs_another_try(t, res.get(t)) and t not in (rejected or {})]
    if again:
        try:
            raw = translate(again, prompt, **extra) or {}
            got = {k: v for k, v in raw.items() if v}
            res.update(got)
            if accepted is not None:
                accepted.update({k: v for k, v in (getattr(raw, "review_accepted", None) or {}).items()})
            if rejected is not None:
                for k in got:
                    rejected.pop(k, None)
        except Exception as exc:  # noqa: BLE001
            errors.append(_reason(exc))
    return res, len(again)


def classify_and_translate(rows: list[dict], *, translate: TranslateFn | None, profile: ProfileFn | None = None,
                           source_lang: str = "fr", min_ocr: float = 0.45, glossary: dict | None = None,
                           memory=None, memory_file=None, memory_location: str = "", log=None,
                           target_lang: str = "zh") -> dict:
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
        dec, why = classify(r, min_ocr, source_lang, strong, target_lang)
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
    # 规则没过、模型复查判意思一致的（不再用下面的规则否决）/ 复查也没确认的 → 理由。
    review_accepted: dict[str, str] = {}
    review_rejected: dict[str, str] = {}
    scene, retried, res = "", 0, {}
    session, from_memory = None, {}
    if to_model and translate is not None:
        prompt = PROMPT.format(src=SOURCE_LANG_NAMES.get(source_lang, "外文"), dst=target_language_name(target_lang))
        if profile is not None:
            try:
                scene = profile([r["source_text"] for r in rows if any(ch.isalpha() for ch in r["source_text"])]) or ""
            except Exception as exc:  # noqa: BLE001 - the description is optional
                errors.append(_reason(exc))
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
                errors.append(_reason(exc))
                session = None
        ask = [text for text in inputs if text not in from_memory]
        if ask:
            extra = {"term_session": session} if session is not None else {}
            if source_lang in SOURCE_LANG_NAMES and takes_source_lang(translate):
                extra["source_lang"] = source_lang
            if takes_extra_check(translate):
                # 这一页的数字、比较符号、残留原文：和公共规则一起只放行，没过的交给模型复查。
                extra["extra_check"] = lambda src, dst: page_rule_problem(src, dst, target_lang)
            res, retried = _translate_with_retry(ask, prompt, translate, errors,
                                                 accepted=review_accepted, rejected=review_rejected, **extra)
        res.update(from_memory)
    not_applied = session.not_applied_texts() if session is not None else set()
    for r in to_model:
        dst = norm_text(res.get(r["model_input"]) or "")
        r["model_output"] = dst or None
        problem = translation_problem(r["model_input"], dst, target_lang) if dst else ""
        if not dst and r["model_input"] in review_rejected:
            r.update(decision="review", decision_reason="check_failed",
                     review_note=review_rejected[r["model_input"]])
        elif not dst:
            r.update(decision="review", decision_reason="model_missing")
        elif problem == "retained_by_model":
            r.update(decision="keep_source", decision_reason="retained_by_model", translation_source="retained_by_model")
        elif r["model_input"] in review_accepted:
            # 写回，不列复核；报告里记一行。这次可以用，但不存进记忆库。
            r.update(translation=dst, translation_source="model", decision="write",
                     decision_reason="model_review_accepted", review_note=review_accepted[r["model_input"]])
        elif problem:
            r.update(decision="review", decision_reason=problem)
        elif any(n not in numbers(dst, dates=True) for n in numbers(r["source_text"], dates=True)):
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
                   if r["decision"] == "write" and r["translation_source"] == "model" and r.get("origin") != "ocr"
                   and r["decision_reason"] != "model_review_accepted"}
        counts: dict[str, int] = {}
        for r in to_model:
            counts[r["model_input"]] = counts.get(r["model_input"], 0) + 1
        try:
            memory.close(session, learned, counts=counts)
        except Exception as exc:  # noqa: BLE001
            errors.append(_reason(exc))

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
            "memory_hits": len(from_memory), "term_review": sum(1 for r in rows if r.get("term_review")),
            "review_accepted": sum(1 for r in to_model if r["decision_reason"] == "model_review_accepted")}
