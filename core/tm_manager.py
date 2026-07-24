"""
翻译记忆库（Translation Memory）管理器。
存储介质：SQLite（平台原生应用数据目录下的 tm.db）

入库规则：
  len(source) ≤ max_len → 写入词库
  len(source) > max_len → 跳过入库，避免长句污染

固定规则：
  pinned=1 的词条不参与深度清洗，保护手动校对结果。
"""
import hashlib
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass

from loguru import logger

from config import BACKUPS_DIR as DEFAULT_BACKUPS_DIR, DB_PATH
from core.language_registry import is_custom_target_lang
from core.tm_text import normalize_tm_text_for_storage


# ── 哈希工具 ──────────────────────────────────────────────────────────────────

def _make_hash(source_text: str, lang_pair: str) -> str:
    """
    对 source_text + lang_pair 计算 SHA-256，取前 32 位十六进制字符（128-bit）。
    用于 source_hash 列，作为唯一标识进行索引和查询，替代全文字符串比对。
    碰撞概率约 1/2^128，实际可忽略。
    """
    raw = (source_text + "\x00" + lang_pair).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


# ── DDL ──────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tm_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text   TEXT    NOT NULL,
    source_hash   TEXT    NOT NULL DEFAULT '',
    target_text   TEXT    NOT NULL,
    lang_pair     TEXT    NOT NULL,
    word_type     TEXT    NOT NULL,
    source_engine TEXT    DEFAULT '',
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_text, lang_pair)
);
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_source_lang "
    "ON tm_entries(source_text, lang_pair)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hash_lang "
    "ON tm_entries(source_hash, lang_pair)",
)

_CREATE_META_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tm_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
"""

_CREATE_CONFLICT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tm_conflict_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id        INTEGER,
    source_text     TEXT NOT NULL,
    existing_target TEXT NOT NULL,
    candidate_target TEXT NOT NULL,
    lang_pair       TEXT NOT NULL,
    source_engine   TEXT DEFAULT '',
    task_id         TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tm_cleaning_suggestions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id         INTEGER NOT NULL,
    source_text      TEXT NOT NULL,
    old_target       TEXT NOT NULL,
    new_target       TEXT NOT NULL,
    lang_pair        TEXT NOT NULL,
    expected_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

TM_SCHEMA_VERSION = 3
TM_SCHEMA_VERSION_KEY = "tm_schema_version"
AUTO_WORD_TYPE = "auto"
REVIEWED_AUTO_WORD_TYPE = "reviewed_auto"
CLEANING_LOCKED_WORD_TYPE = "cleaning_locked"
MANUAL_WORD_TYPE = "manual"
IMPORT_WORD_TYPE = "import"

# This remains an injectable test-isolation path for existing TM contract
# fixtures.  The current baseline does not create, restore, or migrate backups
# automatically.
BACKUPS_DIR = DEFAULT_BACKUPS_DIR


# ── 连接管理 ──────────────────────────────────────────────────────────────────

@contextmanager
def _get_conn():
    """线程安全的连接上下文管理器（每次调用独立连接）。"""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Open only the current TM baseline; do not import or repair older data."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists() and _read_schema_version(DB_PATH) != TM_SCHEMA_VERSION:
        raise TmSchemaError(
            "TM 数据不是当前 schema；请在维护页明确清空该类别。"
        )

    _ensure_current_schema()
    _backfill_source_hashes()
    logger.info(f"TM 数据库就绪：{DB_PATH}")


class TmSchemaError(ValueError):
    """Persisted TM data is not safe for this new baseline to open."""


def get_schema_status() -> dict[str, object]:
    if not DB_PATH.exists():
        return {
            "state": "missing",
            "current_version": TM_SCHEMA_VERSION,
            "stored_version": None,
            "can_write": True,
        }
    version = _read_schema_version(DB_PATH)
    if version > TM_SCHEMA_VERSION:
        state = "future"
    elif version < TM_SCHEMA_VERSION:
        state = "incompatible"
    else:
        state = "current"
    return {
        "state": state,
        "current_version": TM_SCHEMA_VERSION,
        "stored_version": version,
        "can_write": state in {"missing", "current"},
    }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        [table_name],
    ).fetchone()
    return row is not None


def _read_schema_version(db_path) -> int:
    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "tm_meta"):
            return 0
        row = conn.execute(
            "SELECT meta_value FROM tm_meta WHERE meta_key = ?",
            [TM_SCHEMA_VERSION_KEY],
        ).fetchone()
        if row is None:
            return 0
        return int(row["meta_value"])
    except Exception as exc:
        logger.warning(f"TM 数据库版本检测失败，将按旧库处理：{exc}")
        return 0
    finally:
        conn.close()


def _ensure_current_schema() -> None:
    with _get_conn() as conn:
        conn.executescript(_CREATE_TABLE_SQL)
        conn.executescript(_CREATE_META_TABLE_SQL)
        conn.executescript(_CREATE_CONFLICT_TABLE_SQL)
        for sql in _INDEX_SQL:
            conn.execute(sql)
        conn.execute(
            """
            INSERT INTO tm_meta (meta_key, meta_value)
            VALUES (?, ?)
            ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value
            """,
            [TM_SCHEMA_VERSION_KEY, str(TM_SCHEMA_VERSION)],
        )


def _backfill_source_hashes() -> None:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source_text, lang_pair FROM tm_entries WHERE source_hash = ''"
        ).fetchall()
        if not rows:
            return

        logger.info(f"TM 数据库修复：开始回填 {len(rows)} 条空哈希数据...")
        for row in rows:
            conn.execute(
                "UPDATE tm_entries SET source_hash = ? WHERE id = ?",
                [_make_hash(row["source_text"], row["lang_pair"]), row["id"]],
            )
        logger.info(f"TM 数据库修复：source_hash 回填完成，共 {len(rows)} 条")


def _normalize_word_type(word_type: str | None) -> str:
    normalized = str(word_type or "").strip().lower()
    if normalized in {MANUAL_WORD_TYPE, IMPORT_WORD_TYPE}:
        return MANUAL_WORD_TYPE
    if normalized in {REVIEWED_AUTO_WORD_TYPE, "reviewed", "approved_auto"}:
        return REVIEWED_AUTO_WORD_TYPE
    if normalized in {CLEANING_LOCKED_WORD_TYPE, "cleaning_locked_auto"}:
        return CLEANING_LOCKED_WORD_TYPE
    return AUTO_WORD_TYPE


def _split_lang_pair(lang_pair: str) -> tuple[str, str] | None:
    text = str(lang_pair or "").strip()
    if not text or "-" not in text:
        return None

    if text.startswith("x-custom-"):
        custom_target_marker = "-x-custom-"
        marker_index = text.find(custom_target_marker, len("x-custom-"))
        if marker_index >= 0:
            source_lang = text[:marker_index]
            separator = "-"
            target_lang = text[marker_index + 1 :]
        else:
            source_lang, separator, target_lang = text.rpartition("-")
    else:
        source_lang, separator, target_lang = text.partition("-")
    if not separator or not source_lang or not target_lang:
        return None
    return source_lang, target_lang


def split_lang_pair(lang_pair: str) -> tuple[str, str] | None:
    """Public parser used by API/import boundaries."""
    return _split_lang_pair(lang_pair)


def _reverse_lang_pair(lang_pair: str) -> str | None:
    split_pair = _split_lang_pair(lang_pair)
    if split_pair is None:
        return None
    source_lang, target_lang = split_pair
    # Custom languages are target-only by contract and therefore never have
    # a reachable reverse language pair. Keep the prefix check in addition to
    # decoding so an opaque code-map value cannot create a synthetic reverse
    # pair before the language registry sees it.
    if (
        source_lang == target_lang
        or source_lang.startswith("x-custom-")
        or target_lang.startswith("x-custom-")
        or is_custom_target_lang(target_lang)
    ):
        return None
    return f"{target_lang}-{source_lang}"


def _entry_priority(row: sqlite3.Row | dict) -> int:
    if int(row["pinned"] or 0):
        return 50
    word_type = _normalize_word_type(row["word_type"])
    return {
        MANUAL_WORD_TYPE: 40,
        CLEANING_LOCKED_WORD_TYPE: 30,
        REVIEWED_AUTO_WORD_TYPE: 20,
        AUTO_WORD_TYPE: 10,
    }.get(word_type, 10)


def _incoming_priority(word_type: str, pinned: int = 0) -> int:
    if int(pinned or 0):
        return 50
    return _entry_priority({"pinned": 0, "word_type": word_type})


def _fetch_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, created_at, updated_at
        FROM tm_entries
        WHERE id = ?
        """,
        [entry_id],
    ).fetchone()


def _fetch_entry_by_source(
    conn: sqlite3.Connection,
    source_text: str,
    lang_pair: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, created_at, updated_at
        FROM tm_entries
        WHERE source_text = ? AND lang_pair = ?
        """,
        [source_text, lang_pair],
    ).fetchone()


def _entry_version(row: sqlite3.Row | dict) -> str:
    """Return a stable optimistic-concurrency token for a TM entry."""
    return "|".join(
        (
            str(row["id"]),
            str(row["source_hash"] or ""),
            str(row["target_text"] or ""),
            str(row["updated_at"] or ""),
        )
    )


def _record_conflict_candidate(
    conn: sqlite3.Connection,
    *,
    existing: sqlite3.Row | dict | None,
    source_text: str,
    candidate_target: str,
    lang_pair: str,
    source_engine: str = "",
    task_id: str = "",
) -> int:
    """Persist an automatic conflict without changing the active TM value."""
    if not candidate_target:
        return 0
    existing_target = str(existing["target_text"] if existing else "")
    if existing_target == candidate_target:
        return 0
    entry_id = int(existing["id"]) if existing is not None else None
    row = conn.execute(
        """
        SELECT id FROM tm_conflict_candidates
        WHERE source_text = ? AND candidate_target = ? AND lang_pair = ?
          AND status = 'pending'
        ORDER BY id DESC LIMIT 1
        """,
        [source_text, candidate_target, lang_pair],
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        """
        INSERT INTO tm_conflict_candidates (
            entry_id, source_text, existing_target, candidate_target,
            lang_pair, source_engine, task_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        [
            entry_id,
            source_text,
            existing_target,
            candidate_target,
            lang_pair,
            str(source_engine or ""),
            str(task_id or ""),
        ],
    )
    return int(cursor.lastrowid)


def _upsert_entry(
    conn: sqlite3.Connection,
    source_text: str,
    target_text: str,
    lang_pair: str,
    *,
    word_type: str,
    source_engine: str = "",
    pinned: int = 0,
    protect_higher_priority: bool = True,
    cleanup_changed_reverse: bool = True,
    task_id: str = "",
) -> bool:
    word_type = _normalize_word_type(word_type)
    pinned = 1 if int(pinned or 0) else 0
    existing = _fetch_entry_by_source(conn, source_text, lang_pair)
    incoming_priority = _incoming_priority(word_type, pinned)
    source_hash = _make_hash(source_text, lang_pair)

    if existing is not None:
        if existing["target_text"] == target_text:
            return False
        # Ordinary automatic results never silently replace an existing value.
        # Keep the active entry and persist a reviewable candidate instead.
        if incoming_priority <= _entry_priority({"word_type": AUTO_WORD_TYPE, "pinned": 0}):
            _record_conflict_candidate(
                conn,
                existing=existing,
                source_text=source_text,
                candidate_target=target_text,
                lang_pair=lang_pair,
                source_engine=source_engine,
                task_id=task_id,
            )
            return False
        if protect_higher_priority and _entry_priority(existing) > incoming_priority:
            _record_conflict_candidate(
                conn,
                existing=existing,
                source_text=source_text,
                candidate_target=target_text,
                lang_pair=lang_pair,
                source_engine=source_engine,
                task_id=task_id,
            )
            return False
        if cleanup_changed_reverse and existing["target_text"] != target_text:
            _delete_matching_reverse(conn, existing)
        conn.execute(
            """
            UPDATE tm_entries
            SET source_hash = ?,
                target_text = ?,
                word_type = ?,
                source_engine = ?,
                pinned = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            [
                source_hash,
                target_text,
                word_type,
                str(source_engine or ""),
                pinned,
                existing["id"],
            ],
        )
        return True

    conn.execute(
        """
        INSERT INTO tm_entries (
            source_text,
            source_hash,
            target_text,
            lang_pair,
            word_type,
            source_engine,
            pinned
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            source_text,
            source_hash,
            target_text,
            lang_pair,
            word_type,
            str(source_engine or ""),
            pinned,
        ],
    )
    return True


def _sync_reverse_upsert(
    conn: sqlite3.Connection,
    source_text: str,
    target_text: str,
    lang_pair: str,
    *,
    word_type: str,
    source_engine: str = "",
    pinned: int = 0,
) -> bool:
    reverse_pair = _reverse_lang_pair(lang_pair)
    if reverse_pair is None:
        return False

    # A requested reverse sync is still subject to conflict protection.  A
    # different active translation must never be replaced by this operation,
    # regardless of its current trust level.
    existing = _fetch_entry_by_source(conn, target_text, reverse_pair)
    if existing is not None:
        if existing["target_text"] != source_text:
            _record_conflict_candidate(
                conn,
                existing=existing,
                source_text=target_text,
                candidate_target=source_text,
                lang_pair=reverse_pair,
                source_engine=source_engine,
            )
            return False
        return False
    return _upsert_entry(
        conn,
        target_text,
        source_text,
        reverse_pair,
        word_type=word_type,
        source_engine=source_engine,
        pinned=pinned,
        protect_higher_priority=True,
        cleanup_changed_reverse=False,
    )


def _delete_matching_reverse(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> int:
    reverse_pair = _reverse_lang_pair(row["lang_pair"])
    if reverse_pair is None:
        return 0

    reverse = _fetch_entry_by_source(conn, row["target_text"], reverse_pair)
    if reverse is None or reverse["target_text"] != row["source_text"]:
        return 0
    if _entry_priority(reverse) > _entry_priority(row):
        return 0

    conn.execute("DELETE FROM tm_entries WHERE id = ?", [reverse["id"]])
    return 1


def _sync_reverse_pin(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict,
    pinned: bool,
) -> int:
    reverse_pair = _reverse_lang_pair(row["lang_pair"])
    if reverse_pair is None:
        return 0
    cur = conn.execute(
        """
        UPDATE tm_entries
        SET pinned = ?, updated_at = CURRENT_TIMESTAMP
        WHERE lang_pair = ? AND source_text = ? AND target_text = ?
        """,
        [
            1 if pinned else 0,
            reverse_pair,
            row["target_text"],
            row["source_text"],
        ],
    )
    return cur.rowcount


def _select_scope_rows(
    conn: sqlite3.Connection,
    lang_pair: str,
    keyword: str = "",
    *,
    only_unpinned: bool = False,
) -> list[sqlite3.Row]:
    base_where = "WHERE lang_pair = ?"
    params: list = [lang_pair]
    if only_unpinned:
        base_where += " AND pinned = 0"
    if keyword.strip():
        base_where += " AND (source_text LIKE ? OR target_text LIKE ?)"
        kw = f"%{keyword.strip()}%"
        params.extend([kw, kw])
    return conn.execute(
        f"""
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, created_at, updated_at
        FROM tm_entries
        {base_where}
        ORDER BY id
        """,
        params,
    ).fetchall()


# ── 核心 CRUD ─────────────────────────────────────────────────────────────────

def lookup_batch(
    texts: list[str],
    lang_pair: str,
    _chunk_size: int = 900,
) -> dict[str, str | None]:
    """
    批量查询 TM。
    返回 {原文: 译文} 字典，未命中的 key 值为 None。

    内部按 _chunk_size 分片执行 SQL，防止触发 SQLite 绑定变量数量上限
    （SQLITE_MAX_VARIABLE_NUMBER 编译默认值通常为 999）。
    """
    if not texts:
        return {}

    # 同时查询原始哈希与标准化哈希，兼容历史脏数据与新标准化入库数据。
    raw_hash_to_texts: dict[str, list[str]] = defaultdict(list)
    normalized_hash_to_texts: dict[str, list[str]] = defaultdict(list)
    for original_text in texts:
        raw_hash = _make_hash(original_text, lang_pair)
        raw_hash_to_texts[raw_hash].append(original_text)

        normalized_text = normalize_tm_text_for_storage(original_text)
        normalized_hash = _make_hash(normalized_text, lang_pair)
        if normalized_hash != raw_hash:
            normalized_hash_to_texts[normalized_hash].append(original_text)

    hits: dict[str, str] = {}  # source_text -> target_text
    hash_list = list({*raw_hash_to_texts.keys(), *normalized_hash_to_texts.keys()})
    for i in range(0, len(hash_list), _chunk_size):
        chunk = hash_list[i : i + _chunk_size]
        placeholders = ",".join("?" * len(chunk))
        sql = f"""
            SELECT source_hash, target_text FROM tm_entries
            WHERE lang_pair = ? AND source_hash IN ({placeholders})
        """
        with _get_conn() as conn:
            rows = conn.execute(sql, [lang_pair, *chunk]).fetchall()
        for row in rows:
            source_hash = row["source_hash"]
            for original_text in raw_hash_to_texts.get(source_hash, []):
                hits[original_text] = row["target_text"]
            for original_text in normalized_hash_to_texts.get(source_hash, []):
                hits.setdefault(original_text, row["target_text"])
    return {t: hits.get(t) for t in texts}


@dataclass(frozen=True)
class TmLookupExplanation:
    """Explain which directed TM pairs produced a hit for one source text."""

    source_text: str
    translation: str | None
    matched_lang_pairs: tuple[str, ...] = ()
    status: str = "miss"

    @property
    def matched_lang_pair(self) -> str | None:
        return self.matched_lang_pairs[0] if len(self.matched_lang_pairs) == 1 else None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_text": self.source_text,
            "translation": self.translation,
            "matched_lang_pairs": list(self.matched_lang_pairs),
            "matched_lang_pair": self.matched_lang_pair,
            "status": self.status,
        }


def lookup_batch_explained(
    texts: list[str],
    lang_pairs: list[str] | tuple[str, ...],
) -> dict[str, TmLookupExplanation]:
    """Query one or two real pairs and explain every result.

    A same-value hit in multiple pairs is reusable but remains explicitly
    marked as ``multi_language_same``; conflicting values are never selected.
    """
    pairs = list(dict.fromkeys(str(pair or "").strip() for pair in lang_pairs if pair))
    if not texts or not pairs:
        return {
            text: TmLookupExplanation(source_text=text, translation=None)
            for text in texts
        }

    pair_hits = {pair: lookup_batch(texts, pair) for pair in pairs}
    result: dict[str, TmLookupExplanation] = {}
    for text in texts:
        matches = [
            (pair, pair_hits[pair].get(text))
            for pair in pairs
            if pair_hits[pair].get(text) is not None
        ]
        if not matches:
            result[text] = TmLookupExplanation(source_text=text, translation=None)
            continue
        values = {value for _, value in matches}
        if len(values) == 1:
            result[text] = TmLookupExplanation(
                source_text=text,
                translation=matches[0][1],
                matched_lang_pairs=tuple(pair for pair, _ in matches),
                status=("unique_hit" if len(matches) == 1 else "multi_language_same"),
            )
            continue
        result[text] = TmLookupExplanation(
            source_text=text,
            translation=None,
            matched_lang_pairs=tuple(pair for pair, _ in matches),
            status="conflict",
        )
    return result


lookup_batch_with_explanations = lookup_batch_explained


def insert_batch(
    pairs: list[tuple[str, str]],
    lang_pair: str,
    max_len: int,
    engine_name: str = "",
    *,
    sync_reverse: bool = False,
    task_id: str = "",
) -> int:
    """
    批量写入新词条（INSERT OR UPDATE）。
    返回实际写入条数。
    """
    normalized_pairs: list[tuple[str, str]] = []
    for src, tgt in pairs:
        src = normalize_tm_text_for_storage(src)
        tgt = normalize_tm_text_for_storage(tgt)
        if not src or not tgt:
            continue
        if len(src) > max_len:
            continue  # 超长词条不入库
        normalized_pairs.append((src, tgt))

    if not normalized_pairs:
        return 0

    written = 0
    with _get_conn() as conn:
        for src, tgt in normalized_pairs:
            changed = _upsert_entry(
                conn,
                src,
                tgt,
                lang_pair,
                word_type=AUTO_WORD_TYPE,
                source_engine=engine_name,
                pinned=0,
                task_id=task_id,
            )
            if not changed:
                continue
            written += 1
            if sync_reverse and _sync_reverse_upsert(
                conn,
                src,
                tgt,
                lang_pair,
                word_type=AUTO_WORD_TYPE,
                source_engine=engine_name,
                pinned=0,
            ):
                written += 1

    logger.debug(
        f"TM 自动写入 {written} 条（{lang_pair}"
        f"{'' if not sync_reverse else '，按显式请求同步反向'}）"
    )
    return written


def insert_auto_entries(
    entries: list[dict[str, object]],
    target_lang: str,
    max_len: int,
    engine_name: str = "",
    *,
    allowed_source_langs: set[str] | None = None,
    manual_source_lang: str | None = None,
    task_id: str = "",
) -> int:
    """Apply the automatic-entry quality gate before writing TM.

    Each item must contain ``source_text``, ``translation`` and an actual
    ``source_lang``.  ``mixed``, ``und``, ``auto``, review/error results,
    placeholder-only results and out-of-scope languages are rejected.
    """
    target = str(target_lang or "").strip()
    if not target or not entries:
        return 0
    allowed = {str(item or "").strip().lower() for item in (allowed_source_langs or set())}
    manual = str(manual_source_lang or "").strip().lower()
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in entries:
        source = normalize_tm_text_for_storage(item.get("source_text", ""))
        translation = normalize_tm_text_for_storage(item.get("translation", item.get("target_text", "")))
        source_lang = str(item.get("source_lang") or "").strip().lower()
        if not source or not translation or source == translation:
            continue
        if source_lang in {"", "auto", "mixed", "und", "unknown"}:
            continue
        if item.get("tm_eligible") is False or item.get("quality_ok") is False:
            continue
        if allowed and source_lang not in allowed:
            continue
        if manual and source_lang != manual:
            continue
        grouped[f"{source_lang}-{target}"].append((source, translation))

    return sum(
        insert_batch(
            pairs,
            pair,
            max_len,
            engine_name,
            sync_reverse=False,
            task_id=task_id,
        )
        for pair, pairs in grouped.items()
    )


def insert_manual_entry(
    source: str,
    target: str,
    lang_pair: str,
    *,
    sync_reverse: bool = False,
) -> bool:
    """
    手动新增单条词条（word_type='manual'）。
    若原文已存在则更新译文（不修改固定状态）。
    返回 True 表示成功。
    """
    try:
        source = normalize_tm_text_for_storage(source)
        target = normalize_tm_text_for_storage(target)
        if not source or not target:
            return False
        with _get_conn() as conn:
            changed = _upsert_entry(
                conn,
                source,
                target,
                lang_pair,
                word_type=MANUAL_WORD_TYPE,
                source_engine="manual",
                pinned=0,
            )
            if changed:
                if sync_reverse:
                    _sync_reverse_upsert(
                        conn,
                        source,
                        target,
                        lang_pair,
                        word_type=MANUAL_WORD_TYPE,
                        source_engine="manual",
                        pinned=0,
                    )
        logger.debug(
            f"TM 手动新增：{source[:20]} → {target[:20]}"
            f"{'' if not sync_reverse else '（已请求反向同步）'}"
        )
        return changed
    except Exception as e:
        logger.warning(f"TM 手动新增失败：{e}")
        return False


def update_entry(entry_id: int, new_target: str) -> None:
    """更新单条词条的译文（TM 管理页手动编辑用）。"""
    bulk_update([(entry_id, new_target)], sync_reverse=False)
    logger.debug(f"TM 更新条目 id={entry_id}")


def update_entry_full(
    entry_id: int,
    new_source: str,
    new_target: str,
    *,
    sync_reverse: bool = False,
    confirm_cleaning_locked: bool = False,
) -> bool:
    """
    同时更新原文和译文，并解除该词条的固定状态。
    同时重新计算并更新 source_hash 以保持与原文的一致性。
    若新原文与其他词条冲突则返回 False。
    """
    new_source = normalize_tm_text_for_storage(new_source)
    new_target = normalize_tm_text_for_storage(new_target)
    if not new_source or not new_target:
        logger.warning(f"TM 全量更新失败：词条 id={entry_id} 的原文或译文为空")
        return False
    
    try:
        with _get_conn() as conn:
            old_row = _fetch_entry(conn, entry_id)
            if old_row is None:
                logger.warning(f"TM 全量更新失败：词条 id={entry_id} 不存在")
                return False
            if int(old_row["pinned"] or 0):
                logger.warning(f"TM 全量更新拒绝：固定条目 id={entry_id} 必须先解除固定")
                return False
            if (
                _normalize_word_type(old_row["word_type"]) == CLEANING_LOCKED_WORD_TYPE
                and not confirm_cleaning_locked
            ):
                logger.warning(
                    f"TM 全量更新拒绝：清洗锁定条目 id={entry_id} 需要明确确认"
                )
                return False
            conflict = _fetch_entry_by_source(conn, new_source, old_row["lang_pair"])
            if conflict is not None and int(conflict["id"]) != int(entry_id):
                logger.warning(f"TM 全量更新失败：词条 id={entry_id} 的新原文与现有词条冲突")
                return False

            lang_pair = old_row["lang_pair"]
            new_hash = _make_hash(new_source, lang_pair)

            conn.execute(
                """
                UPDATE tm_entries
                SET source_text = ?,
                    source_hash = ?,
                    target_text = ?,
                    word_type = ?,
                    source_engine = 'manual',
                    pinned = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [new_source, new_hash, new_target, MANUAL_WORD_TYPE, entry_id],
            )
            if sync_reverse:
                _delete_matching_reverse(conn, old_row)
                _sync_reverse_upsert(
                    conn,
                    new_source,
                    new_target,
                    lang_pair,
                    word_type="manual",
                    source_engine="manual",
                    pinned=0,
                )
        logger.debug(
            f"TM 全量更新条目 id={entry_id}"
            f"{'' if not sync_reverse else '（已请求反向同步）'}"
        )
        return True
    except Exception as e:
        logger.warning(f"TM 全量更新失败（可能原文冲突）：{e}")
        return False


def delete_entry(entry_id: int) -> bool:
    """Delete one entry, refusing protected/fixed rows."""
    with _get_conn() as conn:
        row = _fetch_entry(conn, entry_id)
        if row is None:
            return False
        if int(row["pinned"] or 0):
            logger.warning(f"TM 删除拒绝：固定条目 id={entry_id} 必须先解除固定")
            return False
        conn.execute("DELETE FROM tm_entries WHERE id = ?", [entry_id])
    logger.debug(f"TM 删除条目 id={entry_id}")
    return True


def delete_entries(entry_ids: list[int]) -> dict[str, int]:
    """Delete a batch while reporting protected and missing rows."""
    deleted = 0
    protected = 0
    missing = 0
    for entry_id in dict.fromkeys(int(item) for item in entry_ids):
        with _get_conn() as conn:
            row = _fetch_entry(conn, entry_id)
            if row is None:
                missing += 1
                continue
            if int(row["pinned"] or 0):
                protected += 1
                continue
            conn.execute("DELETE FROM tm_entries WHERE id = ?", [entry_id])
            deleted += 1
    return {"deleted": deleted, "protected": protected, "missing": missing}


def delete_unpinned_entries(lang_pair: str, keyword: str = "") -> int:
    """
    仅删除未固定词条（pinned=0）。
    返回删除条数。
    """
    with _get_conn() as conn:
        rows = _select_scope_rows(
            conn,
            lang_pair,
            keyword,
            only_unpinned=True,
        )
        for row in rows:
            conn.execute("DELETE FROM tm_entries WHERE id = ?", [row["id"]])
        return len(rows)


def delete_all_entries(lang_pair: str, keyword: str = "") -> int:
    """
    删除指定语言对下（可按关键词过滤）的所有词条。
    返回删除条数。
    """
    with _get_conn() as conn:
        rows = _select_scope_rows(conn, lang_pair, keyword, only_unpinned=True)
        for row in rows:
            conn.execute("DELETE FROM tm_entries WHERE id = ?", [row["id"]])
        return len(rows)


def clear_entries(*, lang_pair: str | None = None) -> int:
    """Delete all entries in an explicitly confirmed maintenance scope.

    This intentionally includes pinned/manual rows.  It is separate from the
    ordinary entry-management deletes, which still protect pinned terminology.
    """
    with _get_conn() as conn:
        if lang_pair:
            cursor = conn.execute("DELETE FROM tm_entries WHERE lang_pair = ?", [lang_pair])
            conn.execute("DELETE FROM tm_conflict_candidates WHERE lang_pair = ?", [lang_pair])
            conn.execute("DELETE FROM tm_cleaning_suggestions WHERE lang_pair = ?", [lang_pair])
        else:
            cursor = conn.execute("DELETE FROM tm_entries")
            conn.execute("DELETE FROM tm_conflict_candidates")
            conn.execute("DELETE FROM tm_cleaning_suggestions")
        return int(cursor.rowcount)


def count_entries() -> int:
    """Return a scalar for maintenance UI without exposing any TM text."""
    init_db()
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM tm_entries").fetchone()
    return int(row["total"] or 0)


def bulk_update(
    updates: list[tuple[int, str]],
    *,
    sync_reverse: bool = False,
    expected_versions: dict[int, str] | None = None,
    word_type: str | None = None,
) -> int:
    """
    批量更新译文（TM 清洗确认写入用）。
    updates: [(entry_id, new_target_text), ...]
    返回更新条数。
    """
    if not updates:
        return 0
    rows = [(normalize_tm_text_for_storage(tgt), eid) for eid, tgt in updates]
    rows = [(tgt, eid) for tgt, eid in rows if tgt]
    if not rows:
        return 0
    count = 0
    with _get_conn() as conn:
        for target_text, entry_id in rows:
            old_row = _fetch_entry(conn, int(entry_id))
            if old_row is None:
                continue
            if int(old_row["pinned"] or 0):
                continue
            if expected_versions and expected_versions.get(int(entry_id)):
                expected = str(expected_versions[int(entry_id)])
                current = _entry_version(old_row)
                if expected != current:
                    continue
            if old_row["target_text"] == target_text:
                continue
            conn.execute(
                """
                UPDATE tm_entries
                    SET target_text = ?,
                        word_type = COALESCE(?, word_type),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                [target_text, word_type, entry_id],
            )
            updated_row = dict(old_row)
            updated_row["target_text"] = target_text
            if sync_reverse:
                _delete_matching_reverse(conn, old_row)
                _sync_reverse_upsert(
                    conn,
                    updated_row["source_text"],
                    updated_row["target_text"],
                    updated_row["lang_pair"],
                    word_type=word_type or updated_row["word_type"],
                    source_engine=updated_row["source_engine"],
                    pinned=1 if updated_row["pinned"] else 0,
                )
            count += 1
    logger.info(
        f"TM 批量更新 {count} 条"
        f"{'' if not sync_reverse else '（已请求反向同步）'}"
    )
    return count


# ── 固定管理 ──────────────────────────────────────────────────────────────────

def pin_entry(entry_id: int, pinned: bool = True) -> None:
    """固定或解除固定单条词条。"""
    with _get_conn() as conn:
        row = _fetch_entry(conn, entry_id)
        if row is None:
            return
        conn.execute(
            "UPDATE tm_entries SET pinned = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [1 if pinned else 0, entry_id],
        )
        _sync_reverse_pin(conn, row, pinned)
    logger.debug(f"TM {'固定' if pinned else '解固'} id={entry_id}（已同步反向词条）")


def bulk_pin_entries(ids: list[int], pinned: bool = True) -> None:
    """批量固定或解除固定（按 ID 列表）。"""
    if not ids:
        return
    with _get_conn() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, source_text, target_text, lang_pair, word_type, source_engine, pinned
            FROM tm_entries
            WHERE id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        if not rows:
            return
        conn.execute(
            f"UPDATE tm_entries SET pinned = ?, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            [1 if pinned else 0, *ids],
        )
        for row in rows:
            _sync_reverse_pin(conn, row, pinned)
    logger.debug(f"TM 批量{'固定' if pinned else '解固'} {len(rows)} 条（已同步反向词条）")


def set_all_pinned(lang_pair: str, pinned: bool, keyword: str = "") -> int:
    """
    将指定语言对下（可按关键词过滤）的所有词条设置为固定或解固。
    返回影响条数。
    """
    with _get_conn() as conn:
        rows = _select_scope_rows(conn, lang_pair, keyword)
        if not rows:
            return 0
        placeholders = ",".join("?" * len(rows))
        conn.execute(
            f"UPDATE tm_entries SET pinned = ?, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            [1 if pinned else 0, *[row["id"] for row in rows]],
        )
        for row in rows:
            _sync_reverse_pin(conn, row, pinned)
        return len(rows)


# ── 查询与统计 ────────────────────────────────────────────────────────────────

def get_stats(lang_pair: str) -> dict[str, int]:
    """返回指定语言对的词条统计。"""
    sql = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned,
            SUM(CASE WHEN word_type = ? THEN 1 ELSE 0 END) as manual,
            SUM(CASE WHEN word_type = ? THEN 1 ELSE 0 END) as reviewed_auto,
            SUM(CASE WHEN word_type = ? THEN 1 ELSE 0 END) as cleaning_locked,
            SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as user_fixed
        FROM tm_entries
        WHERE lang_pair = ?
    """
    with _get_conn() as conn:
        row = conn.execute(
            sql,
            [MANUAL_WORD_TYPE, REVIEWED_AUTO_WORD_TYPE, CLEANING_LOCKED_WORD_TYPE, lang_pair],
        ).fetchone()

    total = int(row["total"] or 0)
    pinned = int(row["pinned"] or 0)
    manual = int(row["manual"] or 0)
    reviewed_auto = int(row["reviewed_auto"] or 0)
    cleaning_locked = int(row["cleaning_locked"] or 0)
    user_fixed = int(row["user_fixed"] or 0)
    return {
        "total": total,
        "pinned": pinned,
        "unpinned": max(total - pinned, 0),
        "manual": manual,
        "reviewed_auto": reviewed_auto,
        "cleaning_locked": cleaning_locked,
        "user_fixed": user_fixed,
        "auto": max(total - manual - reviewed_auto - cleaning_locked, 0),
    }


def list_conflict_candidates(
    lang_pair: str | None = None,
    *,
    status: str = "pending",
) -> list[dict[str, object]]:
    """List persisted automatic conflict candidates for user adjudication."""
    where = ["status = ?"]
    params: list[object] = [status]
    if lang_pair:
        where.append("lang_pair = ?")
        params.append(lang_pair)
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, entry_id, source_text, existing_target, candidate_target,
                   lang_pair, source_engine, task_id, status, created_at, updated_at
            FROM tm_conflict_candidates
            WHERE """ + " AND ".join(where) + " ORDER BY id",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_conflict_candidate(candidate_id: int, action: str) -> bool:
    """Resolve a candidate without allowing stale or hidden overwrites."""
    if action not in {"keep_existing", "use_candidate", "reject"}:
        raise ValueError("action must be keep_existing, use_candidate, or reject")
    with _get_conn() as conn:
        candidate = conn.execute(
            "SELECT * FROM tm_conflict_candidates WHERE id = ?",
            [candidate_id],
        ).fetchone()
        if candidate is None or candidate["status"] != "pending":
            return False
        if action == "use_candidate":
            entry = _fetch_entry(conn, int(candidate["entry_id"])) if candidate["entry_id"] else None
            if entry is None:
                return False
            candidate_update = conn.execute(
                """
                UPDATE tm_entries
                SET target_text = ?, word_type = ?, source_engine = 'manual',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND target_text = ?
                """,
                [
                    candidate["candidate_target"],
                    MANUAL_WORD_TYPE,
                    entry["id"],
                    candidate["existing_target"],
                ],
            )
            if candidate_update.rowcount != 1:
                return False
        conn.execute(
            "UPDATE tm_conflict_candidates SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            ["accepted" if action == "use_candidate" else action, candidate_id],
        )
    return True


def persist_cleaning_suggestions(suggestions: list[dict[str, object]]) -> int:
    """Persist a cleaning result set for explicit later approval."""
    if not suggestions:
        return 0
    created = 0
    with _get_conn() as conn:
        for item in suggestions:
            entry_id = int(item.get("entry_id") or 0)
            source_text = normalize_tm_text_for_storage(item.get("source_text", ""))
            old_target = normalize_tm_text_for_storage(item.get("old_target", ""))
            new_target = normalize_tm_text_for_storage(item.get("new_target", ""))
            lang_pair = str(item.get("lang_pair") or "").strip()
            expected_version = str(item.get("version") or item.get("expected_version") or "")
            if not entry_id or not source_text or not old_target or not new_target or not lang_pair or not expected_version:
                continue
            cursor = conn.execute(
                """
                INSERT INTO tm_cleaning_suggestions (
                    entry_id, source_text, old_target, new_target, lang_pair,
                    expected_version, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                [entry_id, source_text, old_target, new_target, lang_pair, expected_version],
            )
            created += int(cursor.rowcount == 1)
    return created


def list_cleaning_suggestions(
    lang_pair: str | None = None,
    *,
    status: str = "pending",
) -> list[dict[str, object]]:
    where = ["status = ?"]
    params: list[object] = [status]
    if lang_pair:
        where.append("lang_pair = ?")
        params.append(lang_pair)
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, entry_id, source_text, old_target, new_target, lang_pair, "
            "expected_version, status, created_at, updated_at "
            "FROM tm_cleaning_suggestions WHERE "
            + " AND ".join(where)
            + " ORDER BY id",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def mark_cleaning_suggestions(
    suggestion_ids: list[int],
    status: str,
) -> int:
    if status not in {"applied", "stale", "rejected"}:
        raise ValueError("status must be applied, stale, or rejected")
    if not suggestion_ids:
        return 0
    placeholders = ",".join("?" * len(suggestion_ids))
    with _get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE tm_cleaning_suggestions SET status = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({placeholders}) AND status = 'pending'",
            [status, *suggestion_ids],
        )
    return int(cursor.rowcount)


def count_entries_referencing_language(language_code: str) -> int:
    """Count TM entries whose directed pair references a language code."""
    code = str(language_code or "").strip()
    if not code:
        return 0
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM tm_entries
            WHERE lang_pair = ? OR lang_pair LIKE ? OR lang_pair LIKE ?
            """,
            [code, f"{code}-%", f"%-{code}"],
        ).fetchone()
    return int(row["total"] or 0)


def get_pin_count(lang_pair: str, keyword: str = "") -> dict[str, int]:
    """返回指定范围内 {pinned: N, unpinned: M} 统计。"""
    base_where = "WHERE lang_pair = ?"
    params: list = [lang_pair]
    if keyword.strip():
        base_where += " AND (source_text LIKE ? OR target_text LIKE ?)"
        kw = f"%{keyword.strip()}%"
        params.extend([kw, kw])
    sql = f"""
        SELECT pinned, COUNT(*) as cnt FROM tm_entries {base_where}
        GROUP BY pinned
    """
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = {"pinned": 0, "unpinned": 0}
    for row in rows:
        if row["pinned"]:
            result["pinned"] = row["cnt"]
        else:
            result["unpinned"] = row["cnt"]
    return result


def search_entries(
    lang_pair: str,
    keyword: str = "",
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """
    分页搜索词条。
    返回 (rows, total_count)。
    rows 每项含 id, source_text, target_text, word_type, source_engine, updated_at, pinned。
    """
    base_where = "WHERE lang_pair = ?"
    params: list = [lang_pair]

    if keyword.strip():
        base_where += " AND (source_text LIKE ? OR target_text LIKE ?)"
        kw = f"%{keyword.strip()}%"
        params.extend([kw, kw])

    count_sql = f"SELECT COUNT(*) FROM tm_entries {base_where}"
    data_sql  = f"""
        SELECT id, source_text, target_text, word_type, source_engine, updated_at, pinned
        FROM tm_entries {base_where}
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?
    """
    offset = (page - 1) * page_size

    with _get_conn() as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
        rows  = conn.execute(data_sql, [*params, page_size, offset]).fetchall()

    return [dict(r) for r in rows], total


# ── 导入 / 导出 ───────────────────────────────────────────────────────────────

def get_all_entries_for_export(lang_pair: str) -> list[dict]:
    """
    取出全部词条用于导出，包含所有字段。
    返回 [{source_text, target_text, word_type, pinned, updated_at}, ...]。
    """
    sql = """
        SELECT source_text, target_text, word_type, pinned, updated_at
        FROM tm_entries
        WHERE lang_pair = ?
        ORDER BY id
    """
    with _get_conn() as conn:
        rows = conn.execute(sql, [lang_pair]).fetchall()
    return [dict(r) for r in rows]


def get_full_export(custom_target_langs=None) -> dict[str, object]:
    """Return a complete current-format TM backup.

    The backup deliberately contains only the new TM schema.  Custom target
    language definitions are embedded so a restore cannot leave orphaned
    language-pair references.
    """
    with _get_conn() as conn:
        entries = conn.execute(
            """
            SELECT source_text, source_hash, target_text, lang_pair,
                   word_type, source_engine, pinned, created_at, updated_at
            FROM tm_entries
            ORDER BY lang_pair, id
            """
        ).fetchall()
        conflicts = conn.execute(
            """
            SELECT entry_id, source_text, existing_target, candidate_target,
                   lang_pair, source_engine, task_id, status, created_at, updated_at
            FROM tm_conflict_candidates
            ORDER BY id
            """
        ).fetchall()
    custom_defs = []
    for item in custom_target_langs or []:
        if hasattr(item, "model_dump"):
            custom_defs.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            custom_defs.append(dict(item))
    return {
        "format_version": "tm-full-v1",
        "custom_target_langs": custom_defs,
        "entries": [dict(row) for row in entries],
        "conflict_candidates": [dict(row) for row in conflicts],
    }


def import_entries(
    entries: list[dict],
    lang_pair: str,
    mode: str,
    *,
    sync_reverse: bool = False,
    preserve_status: bool = False,
) -> dict[str, int]:
    """
    从外部数据导入词条。
    mode: 'skip'（跳过重复）| 'overwrite'（覆盖重复）| 'keep_both'（保留双份）
    返回 {inserted, skipped, duplicates}。
    """
    normalized_entries: list[tuple[str, str, str, int, str, str, str]] = []
    for entry in entries:
        src = normalize_tm_text_for_storage(entry.get("source_text", ""))
        tgt = normalize_tm_text_for_storage(entry.get("target_text", ""))
        if not src or not tgt:
            continue
        pinned = 1 if str(entry.get("pinned", "0")) not in ("0", "False", "false", "") else 0
        normalized_entries.append(
            (
                src,
                tgt,
                _normalize_word_type(entry.get("word_type", AUTO_WORD_TYPE)),
                pinned,
                str(entry.get("source_engine") or "import"),
                str(entry.get("created_at") or "").strip(),
                str(entry.get("updated_at") or "").strip(),
            )
        )

    inserted = 0
    skipped = 0
    duplicates = 0
    with _get_conn() as conn:
        for (
            src,
            tgt,
            word_type,
            pinned,
            source_engine,
            created_at,
            updated_at,
        ) in normalized_entries:
            existing = _fetch_entry_by_source(conn, src, lang_pair)
            write_source = src
            if existing is not None:
                duplicates += 1
                if mode == "skip":
                    skipped += 1
                    continue
                if mode == "keep_both":
                    _record_conflict_candidate(
                        conn,
                        existing=existing,
                        source_text=src,
                        candidate_target=tgt,
                        lang_pair=lang_pair,
                        source_engine="import",
                    )
                    skipped += 1
                    continue

            effective_word_type = word_type if preserve_status else MANUAL_WORD_TYPE
            changed = _upsert_entry(
                conn,
                write_source,
                tgt,
                lang_pair,
                word_type=effective_word_type,
                source_engine=source_engine,
                pinned=pinned,
            )
            if not changed:
                skipped += 1
                continue
            inserted += 1

            if preserve_status:
                # Recompute the hash after any API-level language code map;
                # preserving an old hash would make the restored row
                # unreachable under its new language pair.
                metadata_updates = ["source_hash = ?", "source_engine = ?"]
                metadata_values: list[object] = [
                    _make_hash(write_source, lang_pair),
                    source_engine,
                ]
                if created_at:
                    metadata_updates.append("created_at = ?")
                    metadata_values.append(created_at)
                if updated_at:
                    metadata_updates.append("updated_at = ?")
                    metadata_values.append(updated_at)
                metadata_values.extend([write_source, lang_pair])
                conn.execute(
                    "UPDATE tm_entries SET "
                    + ", ".join(metadata_updates)
                    + " WHERE source_text = ? AND lang_pair = ?",
                    metadata_values,
                )
            if sync_reverse and _sync_reverse_upsert(
                conn,
                write_source,
                tgt,
                lang_pair,
                word_type=effective_word_type,
                source_engine=source_engine,
                pinned=pinned,
            ):
                inserted += 1

    logger.info(
        f"TM 导入：新增或更新 {inserted} 条"
        f"{'' if not sync_reverse else '（按显式请求同步反向）'}，"
        f"重复 {duplicates} 条，跳过 {skipped} 条（模式={mode}）"
    )
    return {"inserted": inserted, "skipped": skipped, "duplicates": duplicates}


def import_conflict_candidates(
    candidates: list[dict],
    *,
    pair_mapper=None,
) -> int:
    """Restore conflict candidates from a current-format full backup."""
    imported = 0
    with _get_conn() as conn:
        for item in candidates:
            source_text = normalize_tm_text_for_storage(item.get("source_text", ""))
            candidate_target = normalize_tm_text_for_storage(item.get("candidate_target", ""))
            if not source_text or not candidate_target:
                continue
            raw_pair = str(item.get("lang_pair") or "").strip()
            lang_pair = pair_mapper(raw_pair) if pair_mapper else raw_pair
            if not lang_pair:
                continue
            existing = _fetch_entry_by_source(conn, source_text, lang_pair)
            existing_target = str(
                existing["target_text"] if existing is not None
                else item.get("existing_target") or ""
            )
            if existing_target == candidate_target:
                continue
            status = str(item.get("status") or "pending").strip().lower()
            if status not in {"pending", "accepted", "rejected", "keep_existing"}:
                status = "pending"
            duplicate = conn.execute(
                """
                SELECT id FROM tm_conflict_candidates
                WHERE source_text = ? AND candidate_target = ? AND lang_pair = ?
                  AND status = ?
                LIMIT 1
                """,
                [source_text, candidate_target, lang_pair, status],
            ).fetchone()
            if duplicate is not None:
                continue
            conn.execute(
                """
                INSERT INTO tm_conflict_candidates (
                    entry_id, source_text, existing_target, candidate_target,
                    lang_pair, source_engine, task_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))
                """,
                [
                    int(existing["id"]) if existing is not None else None,
                    source_text,
                    existing_target,
                    candidate_target,
                    lang_pair,
                    str(item.get("source_engine") or "backup"),
                    str(item.get("task_id") or ""),
                    status,
                    item.get("created_at"),
                    item.get("updated_at"),
                ],
            )
            imported += 1
    return imported


def get_all_entries_for_cleaning(lang_pair: str) -> list[dict]:
    """
    只取普通自动词条供 TM 清洗模块使用。
    返回包含乐观并发版本的 `{id, source_text, target_text, version}`。
    """
    sql = """
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, updated_at
        FROM tm_entries
        WHERE lang_pair = ? AND pinned = 0 AND word_type = ?
        ORDER BY id
    """
    with _get_conn() as conn:
        rows = conn.execute(sql, [lang_pair, AUTO_WORD_TYPE]).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["version"] = _entry_version(row)
        result.append(item)
    return result
