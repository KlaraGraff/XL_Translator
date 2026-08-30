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
import shutil
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from config import BACKUPS_DIR as DEFAULT_BACKUPS_DIR, DB_PATH
from core.language_registry import is_custom_target_lang
from core.tm_text import normalize_tm_text_for_compare, normalize_tm_text_for_storage


# ── 哈希工具 ──────────────────────────────────────────────────────────────────

def _make_hash(source_text: str, lang_pair: str) -> str:
    """
    对 source_text + lang_pair 计算 SHA-256，取前 32 位十六进制字符（128-bit）。
    用于 source_hash 列，作为唯一标识进行索引和查询，替代全文字符串比对。
    碰撞概率约 1/2^128，实际可忽略。
    """
    raw = (source_text + "\x00" + lang_pair).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _match_hash(source_text: str, lang_pair: str) -> str:
    """匹配用哈希：按比较形态（换行折成空格）计算。

    存储形态自 9.3.x 起保留换行，但命中判定不能因此变严：旧库里同一段
    原文是折成一行存的，它们的 source_hash 恰好就是这个比较形态的哈希，
    所以新旧两种写法算出同一个键，旧库照常命中、也不会被写成重复行。
    """
    return _make_hash(normalize_tm_text_for_compare(source_text), lang_pair)


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
)

# Kept apart from the plain indexes: it is unique, so it can only be built once
# every row has a hash.  A legacy database carries ``source_hash`` at its column
# default of '', which collides on the second row.
_UNIQUE_HASH_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_hash_lang "
    "ON tm_entries(source_hash, lang_pair)"
)
# The same index without the guarantee, for a table that already holds
# duplicate source rows.  Lookups still use it; only the "cannot happen twice"
# assertion is dropped, and dropping rows to keep it would be the worse trade.
_PLAIN_HASH_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_hash_lang "
    "ON tm_entries(source_hash, lang_pair)"
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

# Injectable so tests never write into the developer's own data directory.
# A backup is taken only when a database has to be set aside and rebuilt.
BACKUPS_DIR = DEFAULT_BACKUPS_DIR


# ── 连接管理 ──────────────────────────────────────────────────────────────────

_wal_unavailable_logged = False


def _enable_wal(conn: sqlite3.Connection) -> str:
    """Put the TM database in WAL mode and report the mode actually in force.

    ``journal_mode`` is a persistent property of the database file, so the
    switch really happens once, on the first connection that finds a rollback
    journal.  That first switch needs a brief exclusive lock, which is why it
    runs *after* ``busy_timeout``: a concurrent reader makes it wait instead of
    failing outright.  Re-issuing the pragma on later connections is a no-op
    check that neither writes nor locks.

    WAL is what lets a reader (the library page paging through entries) and a
    writer (a task flushing its TM batch) proceed at the same time.  It is also
    what creates ``tm.db-wal`` / ``tm.db-shm``, which the maintenance module
    already lists as TM files to clear.
    """
    global _wal_unavailable_logged
    try:
        row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.Error as exc:
        if not _wal_unavailable_logged:
            _wal_unavailable_logged = True
            logger.warning(f"TM 数据库无法切换到 WAL，将按默认日志模式运行：{exc}")
        return ""
    mode = str(row[0] if row is not None else "").strip().lower()
    if mode != "wal" and not _wal_unavailable_logged:
        _wal_unavailable_logged = True
        logger.warning(f"TM 数据库未能切换到 WAL（当前 journal_mode={mode or '未知'}）。")
    return mode


@contextmanager
def _get_conn():
    """线程安全的连接上下文管理器（每次调用独立连接）。"""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Concurrent tasks flush TM batches on their own connections; without a
    # busy timeout the second writer fails instantly with "database is locked"
    # and its whole batch rolls back.
    conn.execute("PRAGMA busy_timeout = 5000")
    _enable_wal(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Bring the TM database to the current schema, keeping every entry it can.

    Every schema bump so far has been additive — new tables and indexes beside
    the untouched ``tm_entries``.  So an older database is upgraded in place by
    re-running the ``IF NOT EXISTS`` DDL and restamping the version; the user's
    entries are never the price of a version number.

    Only a database this build cannot work with — one missing columns the
    current DDL requires, one written by a newer build, or one that is not a
    database at all — is backed up and replaced.  Refusing to open it and
    leaving the memory locked is not an option: that is a dead end with no way
    out.  Neither is rebuilding a database that was merely *busy* at the moment
    we looked: a lock is another writer working, not a broken file.

    A file the OS will not hand over at all is the one case that is refused
    instead: it cannot be copied, so replacing it would destroy the only copy
    of the memory.  The maintenance page's explicit clear is the way out.

    The order below is load-bearing.  Tables first, then the hash backfill,
    then the unique hash index — building that index before the backfill would
    fail on a legacy database, where every row still carries the column default.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    state, stored_version = _inspect_db()
    if state == "unreadable":
        raise TmSchemaError(
            f"翻译记忆库 {DB_PATH} 暂时读不到（可能是权限或被其他程序占用），"
            "无法备份，因此不会覆盖它。原文件已原样保留；"
            "如确定要放弃它，请在维护页执行「清空翻译记忆库」。"
        )
    if state == "unusable":
        _recreate_db(stored_version)
    elif state == "upgraded":
        logger.info(
            f"TM 数据库为 v{stored_version}，按加法升级到 v{TM_SCHEMA_VERSION}，词条保留。"
        )
    elif state == "busy":
        logger.info("TM 数据库当前被占用，本次跳过版本判定，直接按现有结构打开。")

    try:
        _ensure_current_schema()
        _backfill_source_hashes()
        _ensure_hash_index()
    except sqlite3.OperationalError as exc:
        # A lock that outlasts the probe *and* the writers' own timeout.  Say
        # so; the bare SQLite message would surface as a 500 with nothing the
        # user could act on.
        if not _is_lock_error(exc):
            raise
        raise TmSchemaError(
            "翻译记忆库正被其他任务占用，本次没有改动它。请等当前任务结束后重试。"
        ) from exc
    logger.info(f"TM 数据库就绪：{DB_PATH}")


class TmSchemaError(ValueError):
    """Persisted TM data could not be opened *and* could not be replaced."""


def get_schema_status() -> dict[str, object]:
    """Report the database on disk without opening it for writing.

    ``can_write`` is False for ``unusable`` and ``unreadable``, and only the
    second of those actually turns a write away: for ``unusable`` ``init_db``
    clears the way by backing the file up rather than refusing to run.  A
    ``busy`` database is writable — the caller just has to wait for the lock.
    """
    state, stored_version = _inspect_db()
    return {
        "state": state,
        "current_version": TM_SCHEMA_VERSION,
        "stored_version": stored_version,
        "can_write": state not in {"unusable", "unreadable"},
    }


def _current_entry_columns() -> dict[str, tuple[str, bool, object]]:
    """Columns the current ``tm_entries`` DDL declares, with enough to re-add one.

    Derived by building the table in memory and reflecting it, rather than
    hand-listed, so a future column cannot be added to the DDL without this
    check noticing it.  Returns ``name -> (type, not_null, default)``.
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(_CREATE_TABLE_SQL)
        return {
            row[1]: (str(row[2] or ""), bool(row[3]), row[4])
            for row in probe.execute("PRAGMA table_info(tm_entries)")
        }
    finally:
        probe.close()


def _is_constant_default(default: object) -> bool:
    """Whether SQLite will accept this default on an ``ADD COLUMN``.

    It only takes literals there: anything time-based or parenthesised would
    have to be evaluated per existing row, which is exactly what it refuses.
    """
    text = str(default).strip().upper()
    return not text.startswith("CURRENT_") and "(" not in text


def _add_column_sql(name: str, spec: tuple[str, bool, object]) -> str | None:
    """Build the ``ALTER TABLE`` that grafts one missing column onto an old table.

    Returns ``None`` when SQLite could not accept the column after the fact.
    It refuses two shapes: ``NOT NULL`` without a default (the existing rows
    would have nothing to hold), and a default that is not a constant —
    ``CURRENT_TIMESTAMP`` and friends, because the value would differ per row.
    Those are the only schema gaps this build cannot repair in place; they are
    backed up and rebuilt instead, so the rows are still recoverable from the
    backup rather than silently gone.
    """
    column_type, not_null, default = spec
    if not_null and default is None:
        return None
    if default is not None and not _is_constant_default(default):
        return None
    clause = f'"{name}" {column_type}'.strip()
    if default is not None:
        clause += f" DEFAULT {default}"
    if not_null:
        clause += " NOT NULL"
    return f"ALTER TABLE tm_entries ADD COLUMN {clause}"


def _missing_entry_columns(columns: set[str]) -> dict[str, tuple[str, bool, object]]:
    return {
        name: spec
        for name, spec in _current_entry_columns().items()
        if name not in columns
    }


def _add_missing_entry_columns() -> None:
    """Graft any columns a newer DDL added onto the existing table.

    A column the user's database has not heard of is the most common shape of
    schema bump, and ``ALTER TABLE ADD COLUMN`` keeps every row.  Rebuilding
    the database over a missing column would spend the user's whole memory on
    a change that costs one statement.
    """
    with _get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tm_entries)")}
        if not columns:
            return
        for name, spec in _missing_entry_columns(columns).items():
            sql = _add_column_sql(name, spec)
            if sql is None:
                continue
            logger.info(f"TM 数据库补列：{name}")
            conn.execute(sql)


# The companion files that belong with the database: WAL mode keeps the first
# two, rollback-journal mode (what a filesystem that cannot do WAL falls back
# to) keeps the third.  Copying a database without its journal would preserve a
# torn snapshot, so all three travel together.
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

# How long the probe waits for another writer to finish before giving up.  It
# must be *longer* than the writers' own timeout (_get_conn, 5s): the probe
# concluding "unreadable" is what triggers a rebuild, so it has to be the last
# one to lose patience, not the first.
_INSPECT_BUSY_TIMEOUT_MS = 15000


def db_sidecar_paths(db_path) -> tuple:
    return tuple(
        db_path.with_name(f"{db_path.name}{suffix}") for suffix in _DB_SIDECAR_SUFFIXES
    )


def _is_lock_error(exc: BaseException) -> bool:
    """Tell "someone else is writing" apart from "this file is broken"."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _inspect_db(db_path=None) -> tuple[str, int | None]:
    """Classify the TM database without modifying it.

    ``missing``  no file yet.
    ``current``  already at the current schema version.
    ``upgraded`` older, but ``tm_entries`` already has every column the
                 current DDL needs, so the gap is pure addition.
    ``busy``     a real database that another writer is holding right now.
    ``unusable`` newer than this build, missing required columns, or not a
                 database at all — its content is already lost, so backing it
                 up and rebuilding costs nothing that still existed.
    ``unreadable`` the file is there but the OS will not open it: a permission
                 problem, or Windows AV/backup software holding it.  Told
                 apart from ``unusable`` because the content is very likely
                 intact, and a file that cannot be read cannot be backed up
                 either — so it must not be replaced.

    ``busy`` exists to keep a lock from being mistaken for corruption.
    ``unusable`` costs the user their memory (backed up, then rebuilt), and a
    contended database is the most ordinary thing in this app — every task
    flushes TM batches — so answering "unreadable" to a held lock would delete
    a perfectly good database at the worst possible moment.
    """
    path = db_path or DB_PATH
    if not path.exists():
        return "missing", None
    try:
        # Ask the filesystem before asking SQLite.  "unable to open database
        # file" covers both a permission wall and a corrupt header, and only
        # the first of those must be kept.
        with path.open("rb"):
            pass
    except OSError as exc:
        logger.warning(f"TM 数据库暂时读不到（不会覆盖，原文件保留）：{exc}")
        return "unreadable", None
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_INSPECT_BUSY_TIMEOUT_MS}")
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            version = 0
            if "tm_meta" in tables:
                row = conn.execute(
                    "SELECT meta_value FROM tm_meta WHERE meta_key = ?",
                    [TM_SCHEMA_VERSION_KEY],
                ).fetchone()
                if row is not None:
                    version = int(row["meta_value"])
            columns = (
                {row["name"] for row in conn.execute("PRAGMA table_info(tm_entries)")}
                if "tm_entries" in tables
                else None
            )
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            logger.info(f"TM 数据库正被占用，本次不做版本判定：{exc}")
            return "busy", None
        logger.warning(f"TM 数据库无法读取，将备份后重建：{exc}")
        return "unusable", None
    except Exception as exc:
        logger.warning(f"TM 数据库无法读取，将备份后重建：{exc}")
        return "unusable", None

    if version > TM_SCHEMA_VERSION:
        return "unusable", version
    if version == TM_SCHEMA_VERSION:
        return "current", version
    # An absent ``tm_entries`` has no rows to lose, so creating it is still
    # additive.  A table that exists but lacks columns is repaired in place
    # whenever SQLite will accept them after the fact; only a column it cannot
    # graft on (NOT NULL with no default) forces a rebuild.
    if columns is not None:
        missing = _missing_entry_columns(columns)
        if any(_add_column_sql(name, spec) is None for name, spec in missing.items()):
            return "unusable", version
    return "upgraded", version


def _backup_db() -> str:
    """Copy the unusable database (and its WAL sidecars) aside; return the path."""
    target_dir = BACKUPS_DIR / "tm"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = target_dir / f"tm_unusable_{stamp}.db"
    shutil.copy2(DB_PATH, target)
    for suffix in _DB_SIDECAR_SUFFIXES:
        sidecar = DB_PATH.with_name(f"{DB_PATH.name}{suffix}")
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(f"{target.name}{suffix}"))
    return str(target)


def _recreate_db(stored_version: int | None) -> None:
    """Set the unusable database aside and let ``init_db`` build a fresh one.

    The backup is a precondition, not a courtesy: without it the rebuild is
    indistinguishable from deleting the user's whole translation memory.  If
    the copy cannot be made the rebuild is refused and the file left exactly
    as it is — the maintenance page's explicit clear is the way out, and that
    one goes through file deletion rather than through here.
    """
    try:
        backup_path = _backup_db()
    except OSError as exc:
        raise TmSchemaError(
            f"翻译记忆库无法打开，也无法备份到 {BACKUPS_DIR / 'tm'}（{exc}）。"
            "原文件已原样保留，不会被覆盖；如确定要放弃它，"
            "请在维护页执行「清空翻译记忆库」。"
        ) from exc
    DB_PATH.unlink(missing_ok=True)
    for sidecar in db_sidecar_paths(DB_PATH):
        sidecar.unlink(missing_ok=True)
    logger.warning(
        "TM 数据库无法使用（stored_version={}），已备份到 {} 并重建空库。",
        stored_version,
        backup_path,
    )
    from settings import TM_RECOVERY_SCOPE, record_recovery_event

    record_recovery_event(
        TM_RECOVERY_SCOPE,
        stored_version=stored_version,
        current_version=TM_SCHEMA_VERSION,
        backup_path=backup_path,
    )


def discard_database() -> None:
    """Delete the database file outright, backup or no backup.

    This is what the maintenance page's explicit clear falls back to when the
    database cannot even be opened.  Every automatic path refuses to destroy a
    file it could not copy first; this one is the user pressing the button
    that says the old memory is expendable, so it goes ahead either way — a
    file whose contents are unreachable still has to be removable, or the app
    has no way out of a broken TM at all.
    """
    try:
        backup_path = _backup_db()
    except OSError as exc:
        backup_path = ""
        logger.warning(f"TM 数据库备份失败，按显式清空继续删除：{exc}")
    DB_PATH.unlink(missing_ok=True)
    for sidecar in db_sidecar_paths(DB_PATH):
        sidecar.unlink(missing_ok=True)
    logger.warning(
        "已按用户要求删除翻译记忆库，备份：{}",
        backup_path or "（备份失败）",
    )


def _ensure_current_schema() -> None:
    with _get_conn() as conn:
        conn.executescript(_CREATE_TABLE_SQL)
        conn.executescript(_CREATE_META_TABLE_SQL)
        conn.executescript(_CREATE_CONFLICT_TABLE_SQL)
    _add_missing_entry_columns()
    with _get_conn() as conn:
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
                [_match_hash(row["source_text"], row["lang_pair"]), row["id"]],
            )
        logger.info(f"TM 数据库修复：source_hash 回填完成，共 {len(rows)} 条")


def _ensure_hash_index() -> None:
    """Build the unique hash index, repairing stale hashes if it will not take.

    Runs after the backfill, never before: on a legacy database every row
    carries ``source_hash`` at its column default, and a unique index over a
    column that is '' everywhere fails on the second row.

    A database can still hold hashes that disagree with their text — an older
    build's hashing, or a hand-edited row.  Recomputing them all is cheap and
    normally produces distinct values, because ``tm_entries`` has enforced
    ``UNIQUE(source_text, lang_pair)`` since the first release and the hash is
    taken from exactly that pair.

    A table that never had that constraint — a hand-built or restored dump —
    can hold two rows with the same source, and then no recomputation will
    make the hashes distinct.  That falls back to a plain index: the index is
    only ever a lookup shortcut here (writes already resolve duplicates with
    their own select-then-insert retry), so the alternative — refusing to open
    the memory, or deleting the duplicate rows — would cost the user far more
    than the missing guarantee is worth.
    """
    try:
        with _get_conn() as conn:
            conn.execute(_UNIQUE_HASH_INDEX_SQL)
        return
    except sqlite3.IntegrityError as exc:
        logger.warning(f"TM 数据库哈希索引冲突，将按原文重算全部哈希：{exc}")

    with _get_conn() as conn:
        rows = conn.execute("SELECT id, source_text, lang_pair FROM tm_entries").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tm_entries SET source_hash = ? WHERE id = ?",
                [_match_hash(row["source_text"], row["lang_pair"]), row["id"]],
            )
    try:
        with _get_conn() as conn:
            conn.execute(_UNIQUE_HASH_INDEX_SQL)
    except sqlite3.IntegrityError as exc:
        logger.warning(
            f"TM 数据库存在重复原文，改建普通哈希索引（词条全部保留）：{exc}"
        )
        with _get_conn() as conn:
            conn.execute(_PLAIN_HASH_INDEX_SQL)


def _normalize_word_type(word_type: str | None) -> str:
    normalized = str(word_type or "").strip().lower()
    if normalized in {MANUAL_WORD_TYPE, IMPORT_WORD_TYPE}:
        return MANUAL_WORD_TYPE
    if normalized in {REVIEWED_AUTO_WORD_TYPE, "reviewed", "approved_auto"}:
        return REVIEWED_AUTO_WORD_TYPE
    if normalized in {CLEANING_LOCKED_WORD_TYPE, "cleaning_locked_auto"}:
        return CLEANING_LOCKED_WORD_TYPE
    return AUTO_WORD_TYPE


def _coerce_pinned(value: object) -> int:
    """Read an imported ``pinned`` field without inventing a pin.

    External JSON regularly omits the field or carries an explicit ``null``.
    Anything that is not a recognisable "yes" means not pinned — a wrongly
    pinned entry is protected from deletion and from cleaning, so guessing
    "pinned" would quietly turn an ordinary import into permanent data.
    """
    if value is None or isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "pinned"}:
        return 1
    return 0


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
    """先按原文逐字匹配，再退到匹配哈希。

    哈希这一步不是优化：命中判定按比较形态（换行折成空格）做，写入侧
    要是只认逐字相等，「多行写法」和「旧库里的单行写法」就会被当成两条
    不同的词条——轻则重复行，重则撞上 source_hash 唯一索引整批写入失败。
    """
    row = conn.execute(
        """
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, created_at, updated_at
        FROM tm_entries
        WHERE source_text = ? AND lang_pair = ?
        """,
        [source_text, lang_pair],
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, created_at, updated_at
        FROM tm_entries
        WHERE lang_pair = ? AND source_hash = ?
        ORDER BY id
        """,
        [lang_pair, _match_hash(source_text, lang_pair)],
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


def _same_target_ignoring_newlines(left: object, right: object) -> bool:
    """两段译文是否只差换行/空白写法。"""
    return normalize_tm_text_for_compare(str(left or "")) == normalize_tm_text_for_compare(
        str(right or "")
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
    force_overwrite: bool = False,
    task_id: str = "",
    _allow_insert_retry: bool = True,
) -> bool:
    """Write one entry, or reroute it through the conflict rules.

    ``force_overwrite`` is the explicit "the user asked for this value to win"
    path (import/restore with mode='overwrite').  It bypasses the trust
    ranking entirely and rewrites the row even when the translation already
    matches, so word_type / pinned / source_engine end up exactly as requested
    instead of keeping whatever the current row happened to carry.
    """
    word_type = _normalize_word_type(word_type)
    pinned = 1 if int(pinned or 0) else 0
    existing = _fetch_entry_by_source(conn, source_text, lang_pair)
    incoming_priority = _incoming_priority(word_type, pinned)
    source_hash = _match_hash(source_text, lang_pair)

    if existing is not None:
        if existing["target_text"] == target_text and not force_overwrite:
            return False
        if not force_overwrite and _same_target_ignoring_newlines(
            existing["target_text"], target_text
        ):
            # 只差换行/空白写法，不是译文冲突：不记冲突候选（否则旧库每跑一次
            # 就攒一批「甲 乙 → 甲\n乙」的假冲突）。只在「补回换行」这一个方向上
            # 采纳新写法，且不动固定/人工等更高优先级的条目。
            if (
                _entry_priority(existing) <= incoming_priority
                and "\n" in target_text
                and "\n" not in str(existing["target_text"] or "")
            ):
                conn.execute(
                    """
                    UPDATE tm_entries
                    SET target_text = ?,
                        source_hash = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [target_text, source_hash, existing["id"]],
                )
                return True
            return False
        if not force_overwrite:
            # Ordinary automatic results never silently replace an existing
            # value.  Keep the active entry and persist a reviewable candidate.
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

    try:
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
    except sqlite3.IntegrityError:
        if not _allow_insert_retry:
            raise
        # A concurrent task committed the same (source_text, lang_pair)
        # between our existence check and this INSERT. Re-run against the
        # winner so the normal priority/conflict rules decide, instead of
        # failing the caller's whole batch after a successful translation.
        return _upsert_entry(
            conn,
            source_text,
            target_text,
            lang_pair,
            word_type=word_type,
            source_engine=source_engine,
            pinned=pinned,
            protect_higher_priority=protect_higher_priority,
            cleanup_changed_reverse=cleanup_changed_reverse,
            force_overwrite=force_overwrite,
            task_id=task_id,
            _allow_insert_retry=False,
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

    # 三种哈希一起查，兼容历史脏数据、旧标准化入库数据与保留换行的新数据：
    #   raw        —— 未经标准化就入库的历史行
    #   storage    —— 保留换行的当前存储形态
    #   match      —— 换行折成空格的比较形态，同时也是旧版本的存储形态
    raw_hash_to_texts: dict[str, list[str]] = defaultdict(list)
    normalized_hash_to_texts: dict[str, list[str]] = defaultdict(list)
    for original_text in texts:
        raw_hash = _make_hash(original_text, lang_pair)
        raw_hash_to_texts[raw_hash].append(original_text)

        seen_hashes = {raw_hash}
        for candidate in (
            normalize_tm_text_for_storage(original_text),
            normalize_tm_text_for_compare(original_text),
        ):
            candidate_hash = _make_hash(candidate, lang_pair)
            if candidate_hash in seen_hashes:
                continue
            seen_hashes.add(candidate_hash)
            normalized_hash_to_texts[candidate_hash].append(original_text)

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


@dataclass(frozen=True)
class TmWriteResult:
    """一次写入尝试的真实结果（供 API/界面如实汇报，别再一律说「已保存」）。"""

    status: str
    changed: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.changed


# status 取值：
#   written        —— 新建或更新成功
#   unchanged      —— 库里已经是同一条译文，无需写入
#   blocked_pinned —— 被固定条目挡下，已登记为待裁决冲突，实际没写
#   invalid        —— 原文或译文为空
#   error          —— 数据库异常
_MANUAL_ENTRY_MESSAGES = {
    "written": "",
    "unchanged": "记忆库里已有相同译文，未重复写入。",
    "blocked_pinned": "该原文已有固定词条，新译文没有写入，已登记为待裁决冲突。",
    "invalid": "原文和译文都不能为空。",
}


def insert_manual_entry_detailed(
    source: str,
    target: str,
    lang_pair: str,
    *,
    sync_reverse: bool = False,
) -> TmWriteResult:
    """手动新增单条词条，并如实回报写没写进去。

    `_upsert_entry` 对固定条目只登记冲突候选、不覆盖，返回 False；旧接口把
    这一路和「异常」「已存在相同译文」压成同一个布尔值，界面于是在什么都没
    写的时候也弹「已保存」。这里把三种情况分开报。
    """
    normalized_source = normalize_tm_text_for_storage(source)
    normalized_target = normalize_tm_text_for_storage(target)
    if not normalized_source or not normalized_target:
        return TmWriteResult("invalid", False, _MANUAL_ENTRY_MESSAGES["invalid"])
    try:
        with _get_conn() as conn:
            existing = _fetch_entry_by_source(conn, normalized_source, lang_pair)
            changed = _upsert_entry(
                conn,
                normalized_source,
                normalized_target,
                lang_pair,
                word_type=MANUAL_WORD_TYPE,
                source_engine="manual",
                pinned=0,
            )
            if changed and sync_reverse:
                _sync_reverse_upsert(
                    conn,
                    normalized_source,
                    normalized_target,
                    lang_pair,
                    word_type=MANUAL_WORD_TYPE,
                    source_engine="manual",
                    pinned=0,
                )
    except Exception as exc:  # noqa: BLE001 - 汇报给界面，别静默吞掉
        logger.warning(f"TM 手动新增失败：{exc}")
        return TmWriteResult("error", False, f"写入记忆库失败：{exc}")

    if changed:
        logger.debug(
            f"TM 手动新增：{normalized_source[:20]} → {normalized_target[:20]}"
            f"{'' if not sync_reverse else '（已请求反向同步）'}"
        )
        return TmWriteResult("written", True)

    if existing is not None and _same_target_ignoring_newlines(
        existing["target_text"], normalized_target
    ):
        status = "unchanged"
    elif existing is not None and int(existing["pinned"] or 0):
        status = "blocked_pinned"
    else:
        status = "unchanged" if existing is not None else "error"
    message = _MANUAL_ENTRY_MESSAGES.get(status) or "词条没有写入记忆库。"
    logger.warning(f"TM 手动新增未写入（{status}）：{normalized_source[:20]}")
    return TmWriteResult(status, False, message)


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
    return insert_manual_entry_detailed(
        source,
        target,
        lang_pair,
        sync_reverse=sync_reverse,
    ).changed


def update_entry(entry_id: int, new_target: str) -> None:
    """更新单条词条的译文（TM 管理页手动编辑用）。"""
    bulk_update([(entry_id, new_target)], sync_reverse=False)
    logger.debug(f"TM 更新条目 id={entry_id}")


# status 取值：written / missing / pinned / cleaning_locked / conflict / invalid / error
_ENTRY_UPDATE_MESSAGES = {
    "invalid": "原文和译文都不能为空。",
    "missing": "词条不存在，可能已被删除。",
    "pinned": "固定词条不能直接编辑，请先解除固定。",
    "cleaning_locked": "清洗锁定词条需要明确确认后才能编辑。",
    "conflict": "新原文与现有词条冲突。",
}


def update_entry_full_detailed(
    entry_id: int,
    new_source: str,
    new_target: str,
    *,
    sync_reverse: bool = False,
    confirm_cleaning_locked: bool = False,
) -> TmWriteResult:
    """全量更新一条词条，并如实回报被拒的真实原因。

    旧接口只回一个布尔值，调用方（API）把「固定」「清洗锁定」「不存在」
    统统报成「与现有原文冲突」——那句话跟真实原因无关，用户按提示做也解不开。
    """
    new_source = normalize_tm_text_for_storage(new_source)
    new_target = normalize_tm_text_for_storage(new_target)
    if not new_source or not new_target:
        logger.warning(f"TM 全量更新失败：词条 id={entry_id} 的原文或译文为空")
        return TmWriteResult("invalid", False, _ENTRY_UPDATE_MESSAGES["invalid"])

    def _rejected(status: str) -> TmWriteResult:
        return TmWriteResult(status, False, _ENTRY_UPDATE_MESSAGES[status])

    try:
        with _get_conn() as conn:
            old_row = _fetch_entry(conn, entry_id)
            if old_row is None:
                logger.warning(f"TM 全量更新失败：词条 id={entry_id} 不存在")
                return _rejected("missing")
            if int(old_row["pinned"] or 0):
                logger.warning(f"TM 全量更新拒绝：固定条目 id={entry_id} 必须先解除固定")
                return _rejected("pinned")
            if (
                _normalize_word_type(old_row["word_type"]) == CLEANING_LOCKED_WORD_TYPE
                and not confirm_cleaning_locked
            ):
                logger.warning(
                    f"TM 全量更新拒绝：清洗锁定条目 id={entry_id} 需要明确确认"
                )
                return _rejected("cleaning_locked")
            conflict = _fetch_entry_by_source(conn, new_source, old_row["lang_pair"])
            if conflict is not None and int(conflict["id"]) != int(entry_id):
                logger.warning(f"TM 全量更新失败：词条 id={entry_id} 的新原文与现有词条冲突")
                return _rejected("conflict")

            lang_pair = old_row["lang_pair"]
            new_hash = _match_hash(new_source, lang_pair)

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
        return TmWriteResult("written", True)
    except Exception as e:
        logger.warning(f"TM 全量更新失败（可能原文冲突）：{e}")
        return TmWriteResult("error", False, f"更新记忆库失败：{e}")


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
    return update_entry_full_detailed(
        entry_id,
        new_source,
        new_target,
        sync_reverse=sync_reverse,
        confirm_cleaning_locked=confirm_cleaning_locked,
    ).changed


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
    删除指定语言对下（可按关键词过滤）的所有词条，**包括固定词条**。
    与 ``delete_unpinned_entries`` 的区别就在这里：这是用户明确确认过的清空动作。
    返回删除条数。
    """
    with _get_conn() as conn:
        rows = _select_scope_rows(conn, lang_pair, keyword, only_unpinned=False)
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
    return len(
        bulk_update_detailed(
            updates,
            sync_reverse=sync_reverse,
            expected_versions=expected_versions,
            word_type=word_type,
        )["updated"]
    )


def bulk_update_detailed(
    updates: list[tuple[int, str]],
    *,
    sync_reverse: bool = False,
    expected_versions: dict[int, str] | None = None,
    word_type: str | None = None,
) -> dict[str, list]:
    """批量更新译文，并逐条回报每个词条的去向。

    返回 `{"updated": [...], "missing": [...], "pinned": [...], "stale": [...],
    "unchanged": [...], "rows": [...]}`。前五个桶装 entry_id，供聚合计数；
    `rows` 是与 ``updates`` 提交行序一一对齐的去向字符串（五个桶名之一，外加
    "invalid" 表示译文规整后为空、这一行没有落库）。清洗确认写入必须拿 `rows`
    做逐条配对——同一词条挂两条建议时 entry_id 会同时出现在多个桶里（先到的
    updated、后到的被乐观并发拦成 stale），按 entry_id 反查会把两条都标错。
    """
    outcome: dict[str, list] = {
        "updated": [],
        "missing": [],
        "pinned": [],
        "stale": [],
        "unchanged": [],
        "rows": [],
    }
    if not updates:
        return outcome
    row_outcomes: list[str] = ["invalid"] * len(updates)
    outcome["rows"] = row_outcomes
    count = 0
    with _get_conn() as conn:
        for index, (raw_entry_id, raw_target) in enumerate(updates):
            entry_id = int(raw_entry_id)
            target_text = normalize_tm_text_for_storage(raw_target)
            if not target_text:
                # 译文规整后为空：写进去等于清掉译文，这一行不落库，逐行去向
                # 保持 "invalid"，让界面能对用户说清这一条为什么没写。
                continue
            old_row = _fetch_entry(conn, entry_id)
            if old_row is None:
                outcome["missing"].append(entry_id)
                row_outcomes[index] = "missing"
                continue
            if int(old_row["pinned"] or 0):
                outcome["pinned"].append(entry_id)
                row_outcomes[index] = "pinned"
                continue
            if expected_versions and expected_versions.get(entry_id):
                expected = str(expected_versions[entry_id])
                current = _entry_version(old_row)
                if expected != current:
                    outcome["stale"].append(entry_id)
                    row_outcomes[index] = "stale"
                    continue
            if old_row["target_text"] == target_text:
                outcome["unchanged"].append(entry_id)
                row_outcomes[index] = "unchanged"
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
            outcome["updated"].append(entry_id)
            row_outcomes[index] = "updated"
    logger.info(
        f"TM 批量更新 {count} 条"
        f"{'' if not sync_reverse else '（已请求反向同步）'}"
    )
    return outcome


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


# SQLite 的绑定变量上限：旧运行时（SQLite < 3.32，发布用的 Python 3.11 就带这一档）
# 只有 999，新版本是 32766。整库固定/解固动辄上万条 id，必须分片下发，
# 否则一句 IN (...) 直接抛 "too many SQL variables"，整次操作零生效。
_SQL_VARIABLE_CHUNK = 900


def _chunked(items: list, size: int = _SQL_VARIABLE_CHUNK):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _set_pinned_by_ids(
    conn: sqlite3.Connection,
    entry_ids: list[int],
    pinned: bool,
) -> None:
    for chunk in _chunked(entry_ids):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE tm_entries SET pinned = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({placeholders})",
            [1 if pinned else 0, *chunk],
        )


def bulk_pin_entries(ids: list[int], pinned: bool = True) -> None:
    """批量固定或解除固定（按 ID 列表）。"""
    if not ids:
        return
    with _get_conn() as conn:
        rows: list[sqlite3.Row] = []
        for chunk in _chunked(list(ids)):
            placeholders = ",".join("?" * len(chunk))
            rows.extend(
                conn.execute(
                    f"""
                    SELECT id, source_text, target_text, lang_pair, word_type, source_engine, pinned
                    FROM tm_entries
                    WHERE id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
            )
        if not rows:
            return
        _set_pinned_by_ids(conn, [int(row["id"]) for row in rows], pinned)
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
        _set_pinned_by_ids(conn, [int(row["id"]) for row in rows], pinned)
        for row in rows:
            _sync_reverse_pin(conn, row, pinned)
        return len(rows)


# ── 查询与统计 ────────────────────────────────────────────────────────────────

def get_stats(lang_pair: str) -> dict[str, int]:
    """返回指定语言对的词条统计。

    分类按 `_normalize_word_type` 归一后再计数：旧库里的 `'import'` 行属于
    人工维护、`'term'` 一类未知写法属于普通自动条目，按字面比对会把它们
    一股脑算进 auto，统计条上的「手动维护」于是比实际少。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT word_type, pinned, COUNT(*) AS cnt
            FROM tm_entries
            WHERE lang_pair = ?
            GROUP BY word_type, pinned
            """,
            [lang_pair],
        ).fetchall()

    total = 0
    pinned = 0
    by_type: dict[str, int] = defaultdict(int)
    for row in rows:
        count = int(row["cnt"] or 0)
        total += count
        if int(row["pinned"] or 0):
            pinned += count
        by_type[_normalize_word_type(row["word_type"])] += count

    manual = by_type.get(MANUAL_WORD_TYPE, 0)
    reviewed_auto = by_type.get(REVIEWED_AUTO_WORD_TYPE, 0)
    cleaning_locked = by_type.get(CLEANING_LOCKED_WORD_TYPE, 0)
    user_fixed = pinned
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
    """Persist a cleaning result set for explicit later approval.

    同一条建议（同词条 + 同新译文）只要还挂在 pending，就不再重复入库：
    清洗跑第二遍时模型往往给出一模一样的结果，逐次追加会让审阅列表里
    堆出成倍的相同行，用户得把同一句话勾选很多次。
    """
    if not suggestions:
        return 0
    created = 0
    with _get_conn() as conn:
        pending_rows = conn.execute(
            "SELECT entry_id, new_target FROM tm_cleaning_suggestions WHERE status = 'pending'"
        ).fetchall()
        seen: set[tuple[int, str]] = {
            (int(row["entry_id"]), str(row["new_target"])) for row in pending_rows
        }
        for item in suggestions:
            entry_id = int(item.get("entry_id") or 0)
            source_text = normalize_tm_text_for_storage(item.get("source_text", ""))
            old_target = normalize_tm_text_for_storage(item.get("old_target", ""))
            new_target = normalize_tm_text_for_storage(item.get("new_target", ""))
            lang_pair = str(item.get("lang_pair") or "").strip()
            expected_version = str(item.get("version") or item.get("expected_version") or "")
            if not entry_id or not source_text or not old_target or not new_target or not lang_pair or not expected_version:
                continue
            if (entry_id, new_target) in seen:
                continue
            seen.add((entry_id, new_target))
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


def count_cleaning_suggestions(lang_pair: str | None = None, *, status: str = "stale") -> int:
    """统计某状态的建议条数（全量口径）。

    复核面板顶部的「失效汇总」不要用这个：stale 行永久累积，全量数半年后
    会顶着一句「另有 137 条已失效」说陈年旧账——那边走
    :func:`count_stale_suggestions_in_review_window`。
    """
    where = ["status = ?"]
    params: list[object] = [status]
    if lang_pair:
        where.append("lang_pair = ?")
        params.append(lang_pair)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM tm_cleaning_suggestions WHERE " + " AND ".join(where),
            params,
        ).fetchone()
    return int(row["total"] or 0)


def count_stale_suggestions_in_review_window(lang_pair: str | None = None) -> int:
    """数「当前待审窗口」内失效的建议条数，供复核面板顶部的失效汇总用。

    建议表没有批次/任务归属列，「本批失效了几条」没法精确回答；但全量数
    stale 又会无限累积，面板会永久顶着一句说陈年旧账的提示。折中锚点：只数
    updated_at（失效时刻）不早于「现存最早 pending 建议 created_at」的失效
    行——也就是用户还没处理完的这段待审期间里失效的那些。pending 清零
    （批次处理完）时子查询为 NULL，比较不成立，计数自动归零，提示随之消失。
    """
    pending_where = "status = 'pending'"
    where = ["s.status = 'stale'"]
    params: list[object] = []
    if lang_pair:
        where.append("s.lang_pair = ?")
        params.append(lang_pair)
        pending_where += " AND lang_pair = ?"
        params.append(lang_pair)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM tm_cleaning_suggestions s WHERE "
            + " AND ".join(where)
            + " AND s.updated_at >= ("
            f"SELECT MIN(created_at) FROM tm_cleaning_suggestions WHERE {pending_where}"
            ")",
            params,
        ).fetchone()
    return int(row["total"] or 0)


def mark_cleaning_suggestions(
    suggestion_ids: list[int],
    status: str,
) -> int:
    """把建议标成已应用 / 已过期 / 已拒绝，返回真正改动的行数。

    id 列表按绑定变量上限分片下发——整库清洗一次能产出上万条建议，
    一句 `IN (...)` 会直接撞上 SQLite 的变量上限。
    """
    if status not in {"applied", "stale", "rejected"}:
        raise ValueError("status must be applied, stale, or rejected")
    ids = [int(item) for item in suggestion_ids if item]
    if not ids:
        return 0
    changed = 0
    with _get_conn() as conn:
        for chunk in _chunked(ids):
            placeholders = ",".join("?" * len(chunk))
            cursor = conn.execute(
                f"UPDATE tm_cleaning_suggestions SET status = ?, updated_at = CURRENT_TIMESTAMP "
                f"WHERE id IN ({placeholders}) AND status = 'pending'",
                [status, *chunk],
            )
            changed += int(cursor.rowcount or 0)
    return changed


def expire_stale_cleaning_suggestions(lang_pair: str | None = None) -> int:
    """把已经对不上当前词条的 pending 建议标成 stale，返回过期条数。

    建议是「当时那一版译文」的改写方案。词条后来被人工改过、被固定、
    或者干脆删了，这条建议就不该再出现在待审列表里——否则用户勾了它，
    写入时才被乐观并发拦下，界面上只看到一次莫名其妙的失败。
    """
    pending = list_cleaning_suggestions(lang_pair, status="pending")
    if not pending:
        return 0
    stale_ids: list[int] = []
    with _get_conn() as conn:
        for item in pending:
            row = _fetch_entry(conn, int(item["entry_id"]))
            if row is None or _entry_version(row) != str(item["expected_version"] or ""):
                stale_ids.append(int(item["id"]))
    if not stale_ids:
        return 0
    marked = mark_cleaning_suggestions(stale_ids, "stale")
    if marked:
        logger.info(f"TM 清洗建议过期 {marked} 条（对应词条已变更或已删除）")
    return marked


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
        # Both SELECTs must see one moment in time.  Read separately, a task
        # committing between them can leave the backup self-contradictory —
        # e.g. a conflict candidate pointing at an entry the file does not
        # contain.  An explicit deferred transaction pins the snapshot at the
        # first read and (in WAL mode) still lets writers proceed meanwhile.
        conn.execute("BEGIN DEFERRED")
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

    返回 {inserted, updated, skipped, duplicates}，四个数字都按实际发生的写入统计：
      inserted   实际写入的条数（新建 + 覆盖），也就是界面上的「新增或更新」；
      updated    其中覆盖已有词条的条数（inserted 的子集）；
      skipped    最终没有写入的条数；
      duplicates 原文在库中已存在的条数。
    'overwrite' 模式下备份里的每一条都会真正落库，用户不会看到「全是重复」却
    什么也没恢复。
    """
    normalized_entries: list[tuple[str, str, str, int, str, str, str]] = []
    for entry in entries:
        src = normalize_tm_text_for_storage(entry.get("source_text", ""))
        tgt = normalize_tm_text_for_storage(entry.get("target_text", ""))
        if not src or not tgt:
            continue
        pinned = _coerce_pinned(entry.get("pinned"))
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
    updated = 0
    skipped = 0
    duplicates = 0
    overwrite = mode == "overwrite"
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
            # 'overwrite' is an explicit user instruction: the imported value
            # wins over whatever is in the library, whatever its word_type.
            # Routing it through the trust ranking instead is how restoring a
            # backup into a non-empty library used to leave the old values in
            # place while reporting only "duplicates".
            changed = _upsert_entry(
                conn,
                write_source,
                tgt,
                lang_pair,
                word_type=effective_word_type,
                source_engine=source_engine,
                pinned=pinned,
                force_overwrite=overwrite,
            )
            if not changed:
                skipped += 1
                continue
            inserted += 1
            if existing is not None:
                updated += 1

            if preserve_status:
                # 目标行按 id 定位：_upsert_entry 可能是靠匹配哈希命中了写法
                # 不同的既有行（旧库里同一句原文是折成单行存的），这时按
                # source_text 逐字相等去 UPDATE 一行都匹配不到，备份里的
                # created_at / updated_at / source_engine 会静默丢失。
                target_row = (
                    existing
                    if existing is not None
                    else _fetch_entry_by_source(conn, write_source, lang_pair)
                )
                if target_row is not None:
                    # 哈希在语言对被 API 层映射过之后重算：沿用备份里的旧哈希
                    # 会让还原出来的行再也查不到。口径必须是 _match_hash（比较
                    # 形态），与全仓其它写入点一致；按存储形态算会让同一句原文
                    # 的多行写法和单行写法各存一条，且此后编辑任一条都会撞上
                    # UNIQUE(source_hash, lang_pair) 而无路可走。
                    metadata_updates = ["source_hash = ?", "source_engine = ?"]
                    metadata_values: list[object] = [
                        _match_hash(write_source, lang_pair),
                        source_engine,
                    ]
                    if created_at:
                        metadata_updates.append("created_at = ?")
                        metadata_values.append(created_at)
                    if updated_at:
                        metadata_updates.append("updated_at = ?")
                        metadata_values.append(updated_at)
                    metadata_values.append(int(target_row["id"]))
                    conn.execute(
                        "UPDATE tm_entries SET "
                        + ", ".join(metadata_updates)
                        + " WHERE id = ?",
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
        f"TM 导入：写入 {inserted} 条（其中覆盖 {updated} 条）"
        f"{'' if not sync_reverse else '（按显式请求同步反向）'}，"
        f"重复 {duplicates} 条，跳过 {skipped} 条（模式={mode}）"
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "duplicates": duplicates,
    }


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

    过滤走 `_normalize_word_type`，不用 SQL 字面比对：旧库（TM v2 之前）
    写下的 `'term'` 等历史写法归一后就是普通自动词条，字面比对会把它们
    永久排除在深度清洗之外——那正是最该被清洗的一批老数据。
    """
    sql = """
        SELECT id, source_text, source_hash, target_text, lang_pair,
               word_type, source_engine, pinned, updated_at
        FROM tm_entries
        WHERE lang_pair = ? AND pinned = 0
        ORDER BY id
    """
    with _get_conn() as conn:
        rows = conn.execute(sql, [lang_pair]).fetchall()
    result = []
    for row in rows:
        if _normalize_word_type(row["word_type"]) != AUTO_WORD_TYPE:
            continue
        item = dict(row)
        item["version"] = _entry_version(row)
        result.append(item)
    return result
