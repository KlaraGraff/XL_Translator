"""记忆库词条在翻译中的用法：整段复用、句中采用、按场景选用，以及译后核对。

一次任务开始时把译入目标语言的全部词条读进 :class:`TermIndex`，之后：

- **整段**（:meth:`TermIndex.resolve_whole`）：原文整段与词条相同。「始终采用」或
  「仅整段复用」且译法唯一的直接套用；「按场景选用」的交给模型，随批附上候选译法。
- **句中**（:meth:`TermIndex.scan`）：只看用户定下的词条（手动维护）和带备注的自动
  词条，原文不超过 30 个字符。单字（一个汉字，或不超过两个字母）只在两侧都是边界
  （串首尾、空白、标点）时算出现，「甲级」里的「甲」不算。完全包含的只留最长的一条；
  部分重叠的两条都降为「按场景选用」。
- **提示词**（:func:`build_prompt_block`）：只列本批文字里出现的词条和所在条号，
  不把整个记忆库塞给模型。
- **核对**（:func:`term_applied`）：「始终采用」的词条翻完核对译文里有没有设定译法，
  允许按目标语言语法调整词形（复数、阴阳性、省音）。

本模块只做判断，不调模型、不写库；重译和统计在 :mod:`core.term_engine`。
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.tm_text import normalize_tm_text_for_compare

USAGE_ALWAYS = "always"
USAGE_SCENE = "scene"
USAGE_SEGMENT = "segment"
ORIGIN_MANUAL = "manual"

# 句中采用的词条原文上限：再长就是一句话，整段复用已经覆盖。
MAX_IN_SENTENCE_SOURCE_CHARS = 30
DEFAULT_BATCH_BUDGET = 20

_CJK_RE = re.compile(r"[㐀-鿿豈-﫿぀-ヿ가-힯]")
_LATIN_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_TARGETS = {"zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "ja", "ko"}


def whole_key(text: object) -> str:
    """整段比较用的形态：与记忆库查询相同（空白、换行折叠），大小写敏感。"""
    return normalize_tm_text_for_compare(str(text or ""))


def scan_key(text: object) -> str:
    """句中扫描用的形态：在整段形态上再转小写（英文原文里大小写常不统一）。

    只用 ``lower()``，不用 ``casefold()``：后者会改变长度（ß → ss），位置就对不上了。
    """
    return whole_key(text).lower()


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _is_latin_word_char(ch: str) -> bool:
    return _is_word_char(ch) and not _is_cjk(ch)


def is_single_unit(key: str) -> bool:
    """一个汉字，或不超过两个字母：这类词条只在两侧都是边界时才算出现。"""
    if len(key) == 1 and _is_cjk(key):
        return True
    return 0 < len(key) <= 2 and all(ch.isalpha() and not _is_cjk(ch) for ch in key)


def _boundary_ok(text: str, start: int, end: int, key: str) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if is_single_unit(key):
        return not (before and _is_word_char(before)) and not (after and _is_word_char(after))
    # 字母、数字开头或结尾的词条要整词出现：「door」不该命中「doorway」，
    # 但与汉字相邻无妨（「采用PVC管」）。
    if key and _is_latin_word_char(key[0]) and before and _is_latin_word_char(before):
        return False
    if key and _is_latin_word_char(key[-1]) and after and _is_latin_word_char(after):
        return False
    return True


# ── 词条与命中 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TermEntry:
    id: int
    source: str
    target: str
    lang_pair: str
    note: str = ""
    usage: str = USAGE_SEGMENT
    origin: str = "auto"
    scope: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TermEntry:
        return cls(
            id=int(row.get("id") or 0),
            source=str(row.get("source_text") or ""),
            target=str(row.get("target_text") or ""),
            lang_pair=str(row.get("lang_pair") or ""),
            note=str(row.get("note") or ""),
            usage=str(row.get("effective_usage") or USAGE_SEGMENT),
            origin=str(row.get("origin") or "auto"),
            scope=str(row.get("scope") or ""),
        )


@dataclass(frozen=True)
class TermCandidate:
    """一种译法：相同译文的几条词条合在一起（不同语言对、不同备注）。"""

    target: str
    notes: tuple[str, ...] = ()
    entry_ids: tuple[int, ...] = ()

    @property
    def note(self) -> str:
        return "；".join(note for note in self.notes if note)


@dataclass(frozen=True)
class TermHit:
    """一个词条在一段文字里出现。"""

    source: str
    usage: str
    candidates: tuple[TermCandidate, ...]
    whole: bool = False
    count: int = 1

    @property
    def key(self) -> str:
        return scan_key(self.source)

    @property
    def expected(self) -> str:
        return self.candidates[0].target if self.candidates else ""

    @property
    def entry_ids(self) -> tuple[int, ...]:
        return tuple(entry_id for cand in self.candidates for entry_id in cand.entry_ids)


def _candidates(rows: Sequence[TermEntry]) -> tuple[TermCandidate, ...]:
    by_target: dict[str, list[TermEntry]] = {}
    # 手动维护的排在前面，同来源按条目 id 稳定排序。
    for row in sorted(rows, key=lambda item: (item.origin != ORIGIN_MANUAL, item.id)):
        by_target.setdefault(whole_key(row.target), []).append(row)
    result = []
    for group in by_target.values():
        notes = tuple(dict.fromkeys(row.note for row in group if row.note))
        ids = tuple(row.id for row in group)
        result.append(TermCandidate(target=group[0].target, notes=notes, entry_ids=ids))
    return tuple(result)


def _distinct_targets(rows: Iterable[TermEntry]) -> set[str]:
    return {whole_key(row.target) for row in rows}


def _group_usage(rows: Sequence[TermEntry]) -> str:
    """几条同原文词条合起来怎么用：都「始终采用」且译法唯一才算始终采用。"""
    if rows and all(row.usage == USAGE_ALWAYS for row in rows) and len(_distinct_targets(rows)) == 1:
        return USAGE_ALWAYS
    return USAGE_SCENE


@dataclass
class WholeSegmentResolution:
    """整段查记忆库的结果。"""

    hits: dict[str, str] = field(default_factory=dict)
    entry_ids: dict[str, tuple[int, ...]] = field(default_factory=dict)
    usage_by_text: dict[str, str] = field(default_factory=dict)
    scene_texts: set[str] = field(default_factory=set)


def _path_parts(paths: Iterable[object]) -> list[str]:
    parts: list[str] = []
    for raw in paths:
        try:
            path = Path(str(raw))
        except (TypeError, ValueError):
            continue
        parts.extend(part.lower() for part in path.parts if part)
    return parts


class TermIndex:
    """一次任务用的词条表：整段查找 + 句中扫描。"""

    def __init__(
        self,
        rows: Iterable[Mapping[str, object] | TermEntry],
        *,
        lang_pairs: Iterable[str] | None = None,
        file_paths: Iterable[object] = (),
        apply_in_sentences: bool = True,
    ) -> None:
        allowed_pairs = {str(pair) for pair in lang_pairs} if lang_pairs is not None else None
        path_parts = _path_parts(file_paths)
        self.out_of_scope_keys: set[str] = set()
        entries: list[TermEntry] = []
        for row in rows:
            entry = row if isinstance(row, TermEntry) else TermEntry.from_row(row)
            if not entry.source.strip() or not entry.target.strip():
                continue
            if allowed_pairs is not None and entry.lang_pair not in allowed_pairs:
                continue
            if entry.scope and not any(entry.scope.lower() in part for part in path_parts):
                # 生效范围不含本次文件：整段也不能再从别处查回来（见 TaskTermSession）。
                self.out_of_scope_keys.add(whole_key(entry.source))
                continue
            entries.append(entry)
        self.entries = entries
        self.apply_in_sentences = apply_in_sentences
        # 整段旧译文已判定过期的文字（见 TaskTermSession）：送模型时不再把旧整句当候选，
        # 只按句中词条注入和核对。
        self.stale_whole_keys: set[str] = set()
        self._by_whole: dict[str, list[TermEntry]] = defaultdict(list)
        for entry in entries:
            self._by_whole[whole_key(entry.source)].append(entry)
        self._by_scan_key: dict[str, list[TermEntry]] = defaultdict(list)
        for entry in entries:
            if self._eligible_in_sentence(entry):
                self._by_scan_key[scan_key(entry.source)].append(entry)
        self._by_first: dict[str, list[str]] = defaultdict(list)
        for key in self._by_scan_key:
            self._by_first[key[0]].append(key)
        for keys in self._by_first.values():
            keys.sort(key=len, reverse=True)

    @classmethod
    def load(
        cls,
        target_lang: str,
        *,
        lang_pairs: Iterable[str] | None = None,
        file_paths: Iterable[object] = (),
        apply_in_sentences: bool = True,
        profile: str = "",
    ) -> TermIndex:
        from core import tm_manager

        rows = tm_manager.list_entries_for_target(target_lang, lang_pairs)
        if profile:
            # 自动保存的场景词条只是参考：备注说的场景和这份文档的说明对不上，就不拿给
            # 模型当候选（候选摆在眼前，模型很容易照抄，哪怕场景不符）。手动维护的不受影响。
            rows = [
                row
                for row in rows
                if str(row.get("origin") or "") == ORIGIN_MANUAL
                or note_fits_profile(str(row.get("note") or ""), profile, str(row.get("source_text") or ""))
            ]
        return cls(
            rows,
            lang_pairs=lang_pairs,
            file_paths=file_paths,
            apply_in_sentences=apply_in_sentences,
        )

    @staticmethod
    def _eligible_in_sentence(entry: TermEntry) -> bool:
        if entry.usage == USAGE_SEGMENT:
            return False
        if entry.origin != ORIGIN_MANUAL and not entry.note:
            return False
        key = scan_key(entry.source)
        return 0 < len(key) <= MAX_IN_SENTENCE_SOURCE_CHARS

    @property
    def in_sentence_term_count(self) -> int:
        return len(self._by_scan_key)

    @property
    def manual_entry_count(self) -> int:
        return sum(1 for entry in self.entries if entry.origin == ORIGIN_MANUAL)

    def whole_rows(self, text: str, pairs: Iterable[str] | None = None) -> list[TermEntry]:
        rows = self._by_whole.get(whole_key(text), [])
        if pairs is None:
            return list(rows)
        allowed = set(pairs)
        return [row for row in rows if row.lang_pair in allowed]

    # ── 整段 ──────────────────────────────────────────────────────────────

    def resolve_whole(
        self,
        text_pairs: Mapping[str, Iterable[str]],
        *,
        has_profile: bool,
    ) -> WholeSegmentResolution:
        """整段与词条相同的文字：能直接套用的套用，其余交给模型按场景选用。

        ``text_pairs`` 是每段文字允许查的语言对（自动识别源语言时按文件预检分组）。
        ``has_profile`` 表示本次有文档说明（「自动识别场景」读过全文）：有了它，
        没有备注的场景词条也交给模型判断，而不是照旧直接贴回。
        """
        result = WholeSegmentResolution()
        for text, pairs in text_pairs.items():
            rows = self.whole_rows(text, pairs)
            if not rows:
                continue
            always = [row for row in rows if row.usage == USAGE_ALWAYS]
            scene = [row for row in rows if row.usage == USAGE_SCENE]
            if always and len(_distinct_targets(always)) == 1:
                self._apply(result, text, always, USAGE_ALWAYS)
                continue
            targets = _distinct_targets(rows)
            if not always and not scene:
                if len(targets) == 1:
                    self._apply(result, text, rows, USAGE_SEGMENT)
                else:
                    result.scene_texts.add(text)
                continue
            evidence = has_profile or any(row.note for row in rows)
            if not evidence and len(targets) == 1:
                # 没有文档说明也没有备注，模型手里没有比旧译法更好的依据：照旧套用。
                self._apply(result, text, rows, USAGE_SCENE)
            else:
                result.scene_texts.add(text)
        return result

    @staticmethod
    def _apply(result: WholeSegmentResolution, text: str, rows: Sequence[TermEntry], usage: str) -> None:
        result.hits[text] = rows[0].target
        result.entry_ids[text] = tuple(row.id for row in rows)
        result.usage_by_text[text] = usage

    # ── 句中 ──────────────────────────────────────────────────────────────

    def scan(self, text: str, *, include_scene: bool = True, sentence_only: bool = False) -> list[TermHit]:
        """这段文字里出现的词条；整段相同的词条优先，不再拆开扫描。

        ``sentence_only``：跳过整段词条，只看句中出现的词条（核对整段复用的旧译文用）。
        """
        rows = [] if sentence_only or whole_key(text) in self.stale_whole_keys else self.whole_rows(text)
        if rows:
            usage = _group_usage(rows)
            if usage == USAGE_SCENE and not include_scene:
                return []
            return [TermHit(source=rows[0].source, usage=usage, candidates=_candidates(rows), whole=True)]
        if not self.apply_in_sentences or not self._by_first:
            return []
        haystack = scan_key(text)
        spans: list[tuple[int, int, str]] = []
        for start, ch in enumerate(haystack):
            for key in self._by_first.get(ch, ()):
                if haystack.startswith(key, start) and _boundary_ok(haystack, start, start + len(key), key):
                    spans.append((start, start + len(key), key))
        if not spans:
            return []
        # 完全包含的只留最长；部分重叠的两条都只作参考（按场景选用）。
        kept = [
            span
            for span in spans
            if not any(
                other is not span
                and other[0] <= span[0]
                and span[1] <= other[1]
                and (other[1] - other[0]) > (span[1] - span[0])
                for other in spans
            )
        ]
        overlapped: set[str] = set()
        for index, left in enumerate(kept):
            for right in kept[index + 1:]:
                if left[2] != right[2] and left[0] < right[1] and right[0] < left[1]:
                    overlapped.add(left[2])
                    overlapped.add(right[2])
        counts: dict[str, int] = {}
        for _start, _end, key in kept:
            counts[key] = counts.get(key, 0) + 1
        hits: list[TermHit] = []
        for key, count in counts.items():
            group = self._by_scan_key[key]
            usage = USAGE_SCENE if key in overlapped else _group_usage(group)
            if usage == USAGE_SCENE and not include_scene:
                continue
            hits.append(
                TermHit(
                    source=group[0].source,
                    usage=usage,
                    candidates=_candidates(group),
                    count=count,
                )
            )
        return hits


_SCENE_LATIN_WORD_RE = re.compile(r"[a-z]{4,}")


# 虚词和说明里的套话：「中的」「指的」这类两字组合在哪份说明里都可能出现，不能当成场景相同。
_SCENE_STOP_CHARS = set("的了和与及或在为是指等其之对于以并该此即也中上下内外时所者译作称用按各个本这那有")


def _scene_words(text: str, source: str) -> set[str]:
    """一段说明里的「说法」：中日韩文字取相邻两字，拉丁文取四个字母以上的词。
    含词条本身的字不算（「甲级」和「甲方」不能因为都有「甲」就算场景相同），含虚词的也不算。"""
    key = scan_key(text)
    skip = set(scan_key(source)) | _SCENE_STOP_CHARS
    words: set[str] = set()
    for left, right in zip(key, key[1:]):
        if _is_cjk(left) and _is_cjk(right) and left not in skip and right not in skip:
            words.add(left + right)
    words.update(_SCENE_LATIN_WORD_RE.findall(_strip_accents(key)))
    return words


def note_fits_profile(note: str, profile: str, source: str) -> bool:
    """备注和文档说明有相同的说法；任一方为空、或备注里取不出说法时不作判断（算相符）。"""
    if not note.strip() or not profile.strip():
        return True
    note_words = _scene_words(note, source)
    if not note_words:
        return True
    # 备注里有两个以上说法时，至少要对上两个，免得一个巧合的词就把候选放进来。
    needed = 2 if len(note_words) >= 2 else 1
    return len(note_words & _scene_words(profile, source)) >= needed


# ── 提示词 ────────────────────────────────────────────────────────────────────


def _format_positions(indices: Sequence[int]) -> str:
    return "第 " + "、".join(str(index) for index in indices) + " 条"


def _candidate_label(candidate: TermCandidate, source: str) -> str:
    target = candidate.target
    label = "原样保留" if whole_key(target) == whole_key(source) else target
    return f"{label}（{candidate.note}）" if candidate.note else label


def build_prompt_block(hits_per_text: Sequence[Sequence[TermHit]], target_lang_name: str) -> str:
    """本批文字里出现的词条，按「必须采用」「按场景选用」分两组，标上条号（从 1 起）。"""
    groups: dict[tuple[str, str], tuple[TermHit, list[int]]] = {}
    for index, hits in enumerate(hits_per_text, start=1):
        for hit in hits:
            group_key = (hit.usage, hit.key)
            if group_key not in groups:
                groups[group_key] = (hit, [])
            positions = groups[group_key][1]
            if index not in positions:
                positions.append(index)
    if not groups:
        return ""
    always_lines: list[str] = []
    scene_lines: list[str] = []
    for (usage, _key), (hit, positions) in groups.items():
        where = _format_positions(positions)
        if usage == USAGE_ALWAYS:
            expected = hit.expected
            shown = "原样保留" if whole_key(expected) == whole_key(hit.source) else expected
            always_lines.append(f"- [{where}] {hit.source} → {shown}")
        else:
            options = "；".join(
                f"{chr(ord('a') + offset)}. {_candidate_label(candidate, hit.source)}"
                for offset, candidate in enumerate(hit.candidates)
            )
            scene_lines.append(f"- [{where}] {hit.source}：{options}")
    lines = ["本批文字中出现的记忆库词条："]
    if always_lines:
        lines.append(f"必须采用（可按{target_lang_name or '目标语言'}语法调整词形）：")
        lines.extend(always_lines)
    if scene_lines:
        lines.append("按场景选用（结合该条含义和文档说明选用其一；均不适用时按常规翻译）：")
        lines.extend(scene_lines)
    return "\n".join(lines)


def build_correction_block(failures: Sequence[tuple[int, str, Sequence[TermHit]]]) -> str:
    """重译时附上的改正说明：``failures`` 为 (条号, 上次译文, 未采用的词条)。"""
    lines = ["以下各条的上次译文未采用设定译法，请重新翻译并改正："]
    for index, previous, hits in failures:
        wanted = "；".join(f"{hit.source} → {hit.expected}" for hit in hits)
        lines.append(f"- [第 {index} 条] 上次译文：{previous}；须采用：{wanted}")
    return "\n".join(lines)


def term_keys_of(hits: Iterable[TermHit]) -> set[tuple[str, str]]:
    return {(hit.usage, hit.key) for hit in hits}


def split_by_budget(
    texts: Sequence[str],
    hits_per_text: Sequence[Sequence[TermHit]],
    budget: int,
) -> list[list[int]]:
    """把一批按词条数拆小：每组涉及的不同词条不超过 ``budget``，不丢词条。

    一段文字自己就超过上限时单独成组（不能把一段拆开）。返回各组的下标。
    """
    budget = max(1, int(budget or DEFAULT_BATCH_BUDGET))
    groups: list[list[int]] = []
    current: list[int] = []
    current_keys: set[tuple[str, str]] = set()
    for index in range(len(texts)):
        keys = term_keys_of(hits_per_text[index])
        if current and len(current_keys | keys) > budget:
            groups.append(current)
            current, current_keys = [], set()
        current.append(index)
        current_keys |= keys
    if current:
        groups.append(current)
    return groups


# ── 核对 ──────────────────────────────────────────────────────────────────────


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _latin_tokens(text: str, *, fold: bool) -> list[str]:
    value = _strip_accents(unicodedata.normalize("NFKC", text))
    if fold:
        value = value.casefold()
    return _LATIN_WORD_RE.findall(value)


def _word_matches(expected: str, candidate: str) -> bool:
    """词形宽松比较：长词按前缀（容许复数、阴阳性词尾），短词整词。"""
    if len(expected) <= 3:
        return candidate == expected
    stem = expected[: max(4, len(expected) - 2)]
    return candidate.startswith(stem) and len(candidate) <= len(expected) + 3


def is_cjk_target(target_lang: str) -> bool:
    lang = str(target_lang or "").strip().lower()
    return lang in _CJK_TARGETS or lang.split("-", 1)[0] in {"zh", "ja", "ko"}


def term_applied(
    translation: str,
    expected: str,
    *,
    source: str,
    target_lang: str,
    whole: bool = False,
) -> bool | None:
    """译文是否采用了设定译法；无法核对时返回 None（按已采用处理）。

    - 设定译法与原文相同（「原样保留」）：原文还在译文里。
    - 中日韩目标语言：设定译法作为子串出现。
    - 其余：逐词比较，忽略大小写和重音；四个字母以上的词按词干前缀（容许词尾变化），
      三个字母以内的小写词（de、la、du）不查，大写或带数字的短词（A、PVC）整词且区分
      大小写。句中只有一个字母的译法（「甲 → A」）不核对：译文里到处都是这个字母。
    """
    translation = str(translation or "")
    expected = str(expected or "").strip()
    if not expected:
        return None
    if whole_key(expected) == whole_key(source):
        return scan_key(source) in scan_key(translation)
    if is_cjk_target(target_lang):
        normalize = lambda value: unicodedata.normalize("NFKC", value).casefold().replace(" ", "")  # noqa: E731
        return normalize(expected) in normalize(translation)
    raw_tokens = _latin_tokens(expected, fold=False)
    if not raw_tokens:
        return normalize_contains(expected, translation)
    if not whole and len(raw_tokens) == 1 and len(raw_tokens[0]) == 1:
        return None
    folded_translation = _latin_tokens(translation, fold=True)
    exact_translation = set(_latin_tokens(translation, fold=False))
    checks: list[tuple[str, bool]] = []
    for token in raw_tokens:
        if len(token) <= 3:
            if token.islower() and not any(ch.isdigit() for ch in token):
                continue
            checks.append((token, True))
        else:
            checks.append((token.casefold(), False))
    if not checks:
        # 设定译法只有几个小写短词（「sol」）：整词比较，不区分大小写。
        folded_set = set(folded_translation)
        return all(token.casefold() in folded_set for token in raw_tokens)
    for token, exact in checks:
        if exact:
            if token not in exact_translation:
                return False
        elif not any(_word_matches(token, word) for word in folded_translation):
            return False
    return True


def normalize_contains(expected: str, translation: str) -> bool:
    def normalize(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    return normalize(expected) in normalize(translation)


def chosen_candidate(hit: TermHit, translation: str, *, target_lang: str) -> TermCandidate | None:
    """按场景选用的词条，译文用了哪一种候选；都没用（按常规翻译）返回 None。"""
    if hit.whole:
        # 整段就是这个词：译文得和某个候选一致才算选了它。「classe A」里虽然含有候选
        # 「A」，却是模型新给的译法。
        wanted = whole_key(translation)
        return next((candidate for candidate in hit.candidates if whole_key(candidate.target) == wanted), None)
    for candidate in hit.candidates:
        if term_applied(
            translation,
            candidate.target,
            source=hit.source,
            target_lang=target_lang,
            whole=hit.whole,
        ):
            return candidate
    return None
