"""Per-file automatic source-language preflight contracts.

The preflight is intentionally independent from document readers.  Excel,
Word and future inputs provide representative text candidates, while the
translation engine is supplied as a single ``request`` callback.  This keeps
the one-request-per-file rule testable and prevents the detector from reading
or sending an entire document.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from config import SUPPORTED_SOURCE_LANGS
from core.language_registry import resolve_language_code

AUTO_SOURCE_LANG = "auto"
UNKNOWN_SOURCE_LANG = "und"
MIXED_SOURCE_LANG = "mixed"

DEFAULT_MAX_SAMPLES = 12
DEFAULT_SAMPLE_MAX_CHARS = 240
DEFAULT_TOTAL_MAX_CHARS = 2400

_FORMULA_RE = re.compile(r"^\s*=")
_NUMBER_ONLY_RE = re.compile(r"^[\s\d\W_]+$", re.UNICODE)
_NUMBERING_ONLY_RE = re.compile(
    r"^\s*(?:\d+(?:[.．]\d+)*|[A-Za-z]|[IVXLCDM]+)[.)、．]\s*$",
    re.IGNORECASE,
)
_MARKDOWN_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

LANGUAGE_PREFLIGHT_SYSTEM_PROMPT = (
    "Identify the actual source language(s) in the supplied document samples. "
    "Return JSON only in the form {\"source_langs\":[\"<ISO-639-1>\"]}. "
    "Return one code for a single language, or at most two codes ordered by "
    "estimated proportion for substantial bilingual content. Ignore isolated "
    "brand names, model numbers, units, abbreviations and short foreign terms. "
    "Use only ISO codes from the supported catalog; never return auto."
)


@dataclass(frozen=True)
class LanguagePreflightResult:
    """Result for exactly one file-level source-language preflight."""

    source_langs: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    requested: bool = False
    uncertain: bool = False
    error: str = ""

    @property
    def primary_source_lang(self) -> str | None:
        return self.source_langs[0] if self.source_langs else None

    @property
    def request_count(self) -> int:
        return 1 if self.requested else 0

    def tm_lang_pairs(
        self,
        target_lang: str,
        *,
        custom_target_langs: Iterable[object] | None = None,
    ) -> tuple[str, ...]:
        """Return only real source-language TM pairs for this result."""
        from core.language_registry import build_lang_pair

        pairs: list[str] = []
        for source_lang in self.source_langs:
            try:
                pair = build_lang_pair(
                    target_lang,
                    source_lang=source_lang,
                )
            except ValueError:
                continue
            if pair not in pairs:
                pairs.append(pair)
        return tuple(pairs)

    def to_dict(self, target_lang: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_langs": list(self.source_langs),
            "candidates": list(self.candidates),
            "requested": self.requested,
            "request_count": self.request_count,
            "uncertain": self.uncertain,
        }
        if self.error:
            payload["error"] = self.error
        if target_lang:
            payload["tm_lang_pairs"] = list(self.tm_lang_pairs(target_lang))
        return payload


@dataclass(frozen=True)
class TranslationLanguageResult:
    """One translated item with the model-reported actual source language."""

    source_text: str
    translation: str
    source_lang: str = UNKNOWN_SOURCE_LANG
    target_lang: str = ""
    tm_eligible: bool = False

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "source_text": self.source_text,
            "translation": self.translation,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "tm_eligible": self.tm_eligible,
        }


def _normalize_candidate(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _is_representative_candidate(text: str) -> bool:
    if not text or _FORMULA_RE.match(text):
        return False
    if _NUMBER_ONLY_RE.fullmatch(text) or _NUMBERING_ONLY_RE.fullmatch(text):
        return False
    # A candidate must contain at least one letter or CJK character.  This
    # filters dates, dimensions, model numbers and punctuation-only cells.
    return bool(re.search(r"[A-Za-z\u00c0-\u024f\u0370-\u052f\u0900-\u0dff\u3040-\u30ff\u3400-\u9fff]", text))


def extract_preflight_candidates(
    texts: Iterable[object],
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    max_sample_chars: int = DEFAULT_SAMPLE_MAX_CHARS,
    max_total_chars: int = DEFAULT_TOTAL_MAX_CHARS,
) -> list[str]:
    """Select a bounded, de-duplicated sample without sending full input."""
    if max_samples <= 0 or max_sample_chars <= 0 or max_total_chars <= 0:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    for raw in texts:
        candidate = _normalize_candidate(raw)
        if not _is_representative_candidate(candidate):
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        remaining = max_total_chars - total_chars
        if remaining <= 0:
            break
        candidate = candidate[: min(max_sample_chars, remaining)]
        if not candidate:
            continue
        seen.add(key)
        candidates.append(candidate)
        total_chars += len(candidate)
        if len(candidates) >= max_samples:
            break
    return candidates


def build_preflight_prompt(
    candidates: Iterable[str],
    *,
    target_lang: str = "",
) -> tuple[str, str]:
    """Build a strict detector prompt and bounded JSON user payload."""
    sample_list = list(candidates)
    system = LANGUAGE_PREFLIGHT_SYSTEM_PROMPT
    user = json.dumps(
        {"target_lang": str(target_lang or "").strip(), "samples": sample_list},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return system, user


def _response_payload(raw_response: object) -> object:
    if isinstance(raw_response, (dict, list)):
        return raw_response
    text = str(raw_response or "").strip()
    fenced = _MARKDOWN_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # A strict JSON response is required, but accepting a plain code list
        # makes a transient provider formatting error non-fatal and still keeps
        # invalid values out of TM.
        return [item.strip() for item in text.split(",") if item.strip()]


def parse_preflight_response(raw_response: object) -> tuple[tuple[str, ...], bool, str]:
    """Parse one/two actual source ISO codes from a model response.

    Returns ``(codes, uncertain, error)``. Unknown/mixed/und responses never
    become TM language codes; they produce an empty code tuple with
    ``uncertain=True``.
    """
    payload = _response_payload(raw_response)
    if isinstance(payload, Mapping):
        values = payload.get("source_langs")
        if values is None:
            values = payload.get("source_languages")
        if values is None:
            values = payload.get("languages")
    else:
        values = payload
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return (), True, "预检响应缺少 source_langs 数组。"

    aliases = dict(SUPPORTED_SOURCE_LANGS)
    codes: list[str] = []
    uncertain = False
    for value in values:
        candidate = str(value or "").strip().lower()
        if candidate in {AUTO_SOURCE_LANG, UNKNOWN_SOURCE_LANG, MIXED_SOURCE_LANG}:
            uncertain = True
            continue
        resolved = resolve_language_code(candidate, aliases)
        if resolved is None:
            uncertain = True
            continue
        resolved = str(resolved).strip().lower()
        if resolved not in SUPPORTED_SOURCE_LANGS.values():
            uncertain = True
            continue
        if resolved not in codes:
            if len(codes) >= 2:
                uncertain = True
                continue
            codes.append(resolved)
    if not codes:
        uncertain = True
    return tuple(codes[:2]), uncertain, "" if codes else "未能确定实际源语言。"


def parse_preflight_languages(raw_response: object) -> list[str]:
    """Compatibility helper returning only detected actual source codes."""
    codes, _uncertain, _error = parse_preflight_response(raw_response)
    return list(codes)


def normalize_translation_language_result(
    source_text: str,
    raw_item: object,
    *,
    target_lang: str | None = None,
    allowed_source_langs: Iterable[str] | None = None,
    manual_source_lang: str | None = None,
) -> TranslationLanguageResult:
    """Normalize a model item and gate TM eligibility on actual source_lang."""
    source = str(source_text or "")
    if isinstance(raw_item, Mapping):
        translation = raw_item.get("translation")
        if translation is None:
            translation = raw_item.get("target_text")
        if translation is None:
            translation = raw_item.get("translated")
        reported_source = raw_item.get("source_lang")
    else:
        translation = raw_item
        reported_source = None

    candidate = str(reported_source or "").strip().lower()
    resolved = resolve_language_code(candidate, dict(SUPPORTED_SOURCE_LANGS)) if candidate else None
    actual_source = str(resolved or "").strip().lower()
    if actual_source not in {str(code).lower() for code in SUPPORTED_SOURCE_LANGS.values()}:
        actual_source = UNKNOWN_SOURCE_LANG

    eligible = actual_source not in {UNKNOWN_SOURCE_LANG, AUTO_SOURCE_LANG, MIXED_SOURCE_LANG}
    if allowed_source_langs is not None:
        allowed = {str(item or "").strip().lower() for item in allowed_source_langs}
        eligible = eligible and actual_source in allowed
    if manual_source_lang:
        eligible = eligible and actual_source == str(manual_source_lang).strip().lower()

    return TranslationLanguageResult(
        source_text=source,
        translation="" if translation is None else str(translation),
        source_lang=actual_source,
        target_lang=str(target_lang or "").strip(),
        tm_eligible=eligible,
    )


def preflight_file_source_languages(
    texts: Iterable[object],
    *,
    target_lang: str,
    request: Callable[[str, str], object],
) -> LanguagePreflightResult:
    """Run at most one model request for one file's candidate texts."""
    candidates = tuple(extract_preflight_candidates(texts))
    if not candidates:
        return LanguagePreflightResult(candidates=(), requested=False)
    system, user = build_preflight_prompt(candidates, target_lang=target_lang)
    try:
        raw_response = request(system, user)
        source_langs, uncertain, error = parse_preflight_response(raw_response)
    except Exception as exc:  # a failed detector must not fail translation
        return LanguagePreflightResult(
            candidates=candidates,
            requested=True,
            uncertain=True,
            error=f"预检请求失败：{exc}",
        )
    return LanguagePreflightResult(
        source_langs=source_langs,
        candidates=candidates,
        requested=True,
        uncertain=uncertain,
        error=error,
    )


def preflight_files_source_languages(
    files: Mapping[str, Iterable[object]],
    *,
    target_lang: str,
    request: Callable[[str, str], object],
) -> dict[str, LanguagePreflightResult]:
    """Preflight each file once; no request is made for empty files."""
    return {
        str(file_id): preflight_file_source_languages(
            texts,
            target_lang=target_lang,
            request=request,
        )
        for file_id, texts in files.items()
    }


# Names used by the API layer and older Phase 1 fixtures.  Keeping these thin
# wrappers makes the request-count and candidate bounds semantics identical.
extract_language_probe_texts = extract_preflight_candidates


def build_language_preflight_prompt(
    candidates: Iterable[str],
    *,
    target_lang: str = "",
) -> str:
    """Return the bounded user JSON payload for ``engine.chat`` callers."""
    _system, user = build_preflight_prompt(candidates, target_lang=target_lang)
    return user


def preflight_files(
    files: Mapping[str, Iterable[object]],
    detector: Callable[[list[str], str], object],
    *,
    target_lang: str,
) -> dict[str, LanguagePreflightResult]:
    """Run a detector once per file using ``detector(samples, target_lang)``."""
    results: dict[str, LanguagePreflightResult] = {}
    for file_id, texts in files.items():
        candidates = tuple(extract_preflight_candidates(texts))
        if not candidates:
            results[str(file_id)] = LanguagePreflightResult(candidates=())
            continue
        try:
            raw_response = detector(list(candidates), target_lang)
            source_langs, uncertain, error = parse_preflight_response(raw_response)
        except Exception as exc:
            results[str(file_id)] = LanguagePreflightResult(
                candidates=candidates,
                requested=True,
                uncertain=True,
                error=f"预检请求失败：{exc}",
            )
            continue
        results[str(file_id)] = LanguagePreflightResult(
            source_langs=source_langs,
            candidates=candidates,
            requested=True,
            uncertain=uncertain,
            error=error,
        )
    return results
