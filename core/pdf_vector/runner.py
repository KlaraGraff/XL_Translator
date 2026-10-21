"""「原位替换文字（保留矢量）」Beta：把 PDF 的一页在原位换成译文。

一页的流程：取出这一页 → 读文字（文字层 + 可选的 Tesseract 识别画成线条的
字）→ 分类、去重、写图纸说明、翻译 → 删掉原文字形、按原字号和颜色写中文 →
自检。自检不过的页不交付，调用方保留原页。

PDF 的读写用 PyMuPDF（AGPL-3.0，沿用其原许可，见 THIRD_PARTY_NOTICES.md），它属于「PDF 处理组件」，不随安装包分发。
"""
from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import extract, qa, writeback
from .classify import ProfileFn, TranslateFn, classify_and_translate
from .common import fitz, write_json

LogFn = Callable[[str, str], None]
# report(step, done, total, **info): where the page is, for the task's progress
# bar and run log.  Steps: "ocr" (tiles read, done/total), "read" (text read:
# rows, native, ocr, seconds), "write" (translations in: written back, then checked).
ReportFn = Callable[..., None]

# Which Tesseract languages to read with, by document language.
_OCR_LANGS = {"fr": "fra+eng", "en": "eng", "es": "spa+eng", "de": "deu+eng", "it": "ita+eng", "pt": "por+eng"}

_FONT_CANDIDATES = [
    # Windows
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simhei.ttf",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simsun.ttc",
    # macOS
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    # Linux
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
]


def find_cjk_font() -> Path | None:
    for candidate in _FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def ocr_languages(source_lang: str, installed: list[str] | None) -> str:
    wanted = _OCR_LANGS.get(source_lang, "eng+fra")
    if installed is None:
        return wanted
    have = [lang for lang in wanted.split("+") if lang in installed]
    return "+".join(have) or ("eng" if "eng" in installed else "")


@dataclass
class InplaceHooks:
    """What the PDF task hands to the in-place pages: the app's translation
    engine (as plain callables), and the OCR component if the user has it."""
    translate: TranslateFn | None
    profile: ProfileFn | None = None
    tesseract: str | None = None
    tesseract_languages: list[str] | None = None
    source_lang: str = "auto"
    ocr_wanted: bool = True
    # 记忆库（core.term_session.DocumentTermMemory）：与 Excel / Word 同一套规则。
    memory: object | None = None


_STOPWORDS = {
    "fr": {"de", "la", "le", "les", "des", "du", "et", "en", "à", "au", "pour", "sur", "par"},
    "en": {"the", "of", "and", "to", "in", "for", "on", "with", "by", "is"},
    "es": {"de", "la", "el", "los", "las", "del", "y", "en", "para", "con", "por"},
    "de": {"der", "die", "das", "und", "für", "mit", "von", "zu", "im", "den"},
}


def guess_language(texts: list[str], fallback: str = "fr") -> str:
    """Pick the source language from common short words (enough for drawings)."""
    import re

    words = [w.casefold() for t in texts for w in re.findall(r"[^\W\d_]+", t)]
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    best = max(scores, key=scores.get) if scores else fallback
    return best if scores.get(best, 0) >= 3 else fallback


@dataclass
class InplaceResult:
    ok: bool
    output_pdf: str = ""
    written: int = 0            # lines replaced by the translation
    kept: int = 0               # numbers, codes, names: nothing to translate
    review: int = 0             # unsure: source kept, listed for the user
    untranslated: int = 0       # the service gave up on it: source kept, listed for review
    term_review: int = 0        # written, but a set translation from the memory was not used
    not_written: int = 0        # translated but could not be placed safely
    ocr_used: bool = False
    unrecognized_regions: int = 0
    error: str = ""
    scene_profile: str = ""
    review_items: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_REASON_TEXT = {
    "low_ocr_confidence": "识别把握不大",
    "ocr_garbage": "识别结果像乱码",
    "ocr_short_fragment": "只认出零碎几个字母",
    "ocr_not_in_lexicon": "识别出的词查不到",
    "ocr_rotated_unverified": "旋转的文字未能确认",
    "ocr_code_uncertain": "编号或数字可能认错",
    "ocr_operator_uncertain": "比较符号（≤ ≥ < >）可能认错",
    "model_missing": "未获得译文",
    "numeric_mismatch": "译文里的数字和原文不一致",
    "operator_mismatch": "译文里的比较符号和原文不一致",
    "source_not_removable": "原文删不干净（可能在图片里）",
    "no_fit": "空间不够，放不下译文",
    "term_not_applied": "未采用记忆库中的设定译法",
}


def reason_text(reason: str) -> str:
    return _REASON_TEXT.get(reason, reason)


def translate_page_inplace(
    source_pdf: Path,
    page_index: int,
    work_dir: Path,
    *,
    translate: TranslateFn | None,
    profile: ProfileFn | None = None,
    tesseract: str | None = None,
    tesseract_languages: list[str] | None = None,
    source_lang: str = "fr",
    font_path: Path | None = None,
    log: LogFn | None = None,
    native_only: bool = False,
    memory=None,
    memory_file: Path | str | None = None,
    memory_location: str = "",
    report: ReportFn | None = None,
) -> InplaceResult:
    """Translate one page in place; the result PDF holds that single page."""
    def say(level: str, message: str) -> None:
        if log:
            log(level, message)

    def tell(step: str, done: int = 0, total: int = 0, **info) -> None:
        if report:
            report(step, done, total, **info)

    work_dir.mkdir(parents=True, exist_ok=True)
    font = font_path or find_cjk_font()
    if font is None:
        return InplaceResult(ok=False, error="找不到可用的中文字体")
    page_pdf = work_dir / "page.pdf"
    with fitz.open(source_pdf) as src:
        one = fitz.open()
        one.insert_pdf(src, from_page=page_index, to_page=page_index)
        one.save(page_pdf)
        one.close()

    if source_lang in ("", "auto", None):
        with fitz.open(page_pdf) as one_doc:
            source_lang = guess_language([one_doc[0].get_text()], fallback="fr")
    langs = ocr_languages(source_lang, tesseract_languages) if tesseract and not native_only else ""
    exe = tesseract if langs else None
    started = time.monotonic()
    info = extract.run(page_pdf, work_dir / "extract", 200, langs or "eng", (0, 90, 270),
                       max(2, (os.cpu_count() or 4) - 2), exe,
                       progress=lambda done, total: tell("ocr", done, total))
    rows = info["rows"]
    tell("read", 1, 1, rows=len(rows), native=sum(1 for r in rows if r.get("origin") == "native"),
         ocr=bool(exe), seconds=time.monotonic() - started)
    prefix = f"{memory_location}：" if memory_location else ""
    summary = classify_and_translate(
        rows, translate=translate, profile=profile, source_lang=source_lang,
        memory=memory, memory_file=memory_file or source_pdf, memory_location=memory_location,
        log=lambda level, message: say(level, f"{prefix}{message}"),
    )
    for err in summary.get("errors") or []:
        say("WARN", f"原位替换：{err}")
    manifest = work_dir / "manifest.json"
    write_json(manifest, {**info, "rows": rows, "summary": summary})

    out_pdf = work_dir / "page_inplace.pdf"
    report_path = work_dir / "writeback.json"
    tell("write", 0, 2)
    rep = writeback.run(page_pdf, manifest, out_pdf, work_dir / "preview.png", report_path, font)
    tell("write", 1, 2)
    check = qa.run(page_pdf, out_pdf, manifest, report_path, work_dir / "qa.json", work_dir / "ledger.csv")
    tell("write", 2, 2)

    decided_write = {r["id"] for r in rows if r["decision"] == "write"}
    written_ids = {w["id"] for w in rep["written"]}
    skipped = {s["id"]: s["reason"] for s in rep["skipped"]}
    missing = {r["id"] for r in rows if r["decision"] == "review" and r["decision_reason"] == "model_missing"}
    review_items = [
        {"text": r["source_text"], "reason": reason_text(r["decision_reason"] if r["decision"] == "review"
                                                         else skipped.get(r["id"], "")),
         "box_px": r["display_box"], **({"kind": "untranslated"} if r["id"] in missing else {})}
        for r in rows
        if r["decision"] == "review" or (r["id"] in decided_write and r["id"] not in written_ids)
    ]
    # 译文已写入、但没用上记忆库里设定的译法（重译后仍不符）：同样列给用户复核。
    review_items += [
        {"text": r["source_text"], "reason": reason_text("term_not_applied"), "detail": r["term_review"],
         "kind": "term", "box_px": r["display_box"]}
        for r in rows
        if r.get("term_review") and r["id"] in written_ids
    ]
    result = InplaceResult(
        ok=check.get("status") == "pass",
        output_pdf=str(out_pdf),
        written=len(written_ids),
        kept=sum(1 for r in rows if r["decision"] == "keep_source"),
        # 接口放弃、没拿到译文的（与 CAD 的「未获得译文」同一类）单独计数，不算「认不准」。
        review=sum(1 for r in rows if r["decision"] == "review") - len(missing),
        untranslated=len(missing),
        not_written=len(decided_write - written_ids),
        term_review=sum(1 for r in rows if r.get("term_review") and r["id"] in written_ids),
        ocr_used=bool(exe),
        unrecognized_regions=int((check.get("unrecognized_text") or {}).get("candidates") or 0),
        scene_profile=summary.get("scene_profile", ""),
        review_items=review_items,
    )
    if not result.ok:
        result.error = "自检没有通过，这一页保留原样"
        say("WARN", f"原位替换：第 {page_index + 1} 页自检没有通过（{_qa_reason(check)}），保留原页。")
    return result


def _qa_reason(check: dict) -> str:
    if not check.get("page", {}).get("same", True):
        return "页面尺寸或方向变了"
    if not check.get("images", {}).get("identical", True):
        return "图片有变化"
    if (check.get("vector_paths") or {}).get("removed_outside_written_rects"):
        return "删掉了文字以外的线条"
    if (check.get("numbers") or {}).get("failures"):
        return "数字不一致"
    if check.get("written_collisions"):
        return "译文互相重叠"
    return "检查未通过"


def cleanup(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
