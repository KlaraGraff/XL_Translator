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
import shutil
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime

from loguru import logger

from config import BACKUPS_DIR, DB_PATH
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

TM_SCHEMA_VERSION = 2
TM_SCHEMA_VERSION_KEY = "tm_schema_version"
AUTO_WORD_TYPE = "term"


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
    """应用启动时调用一次，确保表结构存在。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists() and _read_schema_version(DB_PATH) != TM_SCHEMA_VERSION:
        _migrate_legacy_db()

    _ensure_current_schema()
    _backfill_source_hashes()
    logger.info(f"TM 数据库就绪：{DB_PATH}")


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


def _backup_legacy_db() -> str:
    backup_dir = BACKUPS_DIR / "tm"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"tm_legacy_{timestamp}.db"
    shutil.copy2(DB_PATH, backup_path)
    return str(backup_path)


def _normalize_word_type(word_type: str | None) -> str:
    normalized = str(word_type or "").strip().lower()
    if normalized == "manual":
        return "manual"
    if normalized == "import":
        return "import"
    return AUTO_WORD_TYPE


def _load_legacy_entries(legacy_db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(legacy_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "tm_entries"):
            return []

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tm_entries)").fetchall()
        }

        def _select_expr(column: str, fallback: str) -> str:
            return column if column in columns else f"{fallback} AS {column}"

        rows = conn.execute(
            f"""
            SELECT
                source_text,
                target_text,
                lang_pair,
                {_select_expr("word_type", "'term'")},
                {_select_expr("source_engine", "''")},
                {_select_expr("pinned", "0")}
            FROM tm_entries
            ORDER BY id
            """
        ).fetchall()
        return rows
    finally:
        conn.close()


def _rewrite_from_legacy_rows(rows: list[sqlite3.Row]) -> int:
    if not rows:
        return 0

    to_insert: list[tuple] = []
    for row in rows:
        source_text = normalize_tm_text_for_storage(row["source_text"])
        target_text = normalize_tm_text_for_storage(row["target_text"])
        lang_pair = str(row["lang_pair"] or "").strip()
        if not source_text or not target_text or not lang_pair:
            continue

        to_insert.append(
            (
                source_text,
                _make_hash(source_text, lang_pair),
                target_text,
                lang_pair,
                _normalize_word_type(row["word_type"]),
                str(row["source_engine"] or ""),
                1 if bool(row["pinned"]) else 0,
            )
        )

    if not to_insert:
        return 0

    with _get_conn() as conn:
        conn.executemany(
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
            ON CONFLICT(source_text, lang_pair) DO UPDATE SET
                source_hash   = excluded.source_hash,
                target_text   = excluded.target_text,
                word_type     = excluded.word_type,
                source_engine = excluded.source_engine,
                pinned        = excluded.pinned,
                updated_at    = CURRENT_TIMESTAMP
            """,
            to_insert,
        )
    return len(to_insert)


def _migrate_legacy_db() -> None:
    backup_path = _backup_legacy_db()
    logger.info(f"检测到旧版 TM 数据库，已备份到：{backup_path}")

    legacy_rows: list[sqlite3.Row] = []
    try:
        legacy_rows = _load_legacy_entries(backup_path)
    except Exception as exc:
        logger.warning(f"旧版 TM 数据读取失败，将直接重建新库：{exc}")

    try:
        DB_PATH.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning(f"清理旧版 TM 数据库失败，将尝试覆盖重建：{exc}")

    _ensure_current_schema()

    if not legacy_rows:
        logger.warning("未读取到可迁移的旧词条，已直接启用全新 TM 数据库。")
        return

    try:
        migrated = _rewrite_from_legacy_rows(legacy_rows)
        logger.info(f"TM 数据库迁移完成：共迁移 {migrated} 条词条。")
    except Exception as exc:
        logger.warning(f"TM 数据库迁移失败，已丢弃旧词条并启用新库：{exc}")
        DB_PATH.unlink(missing_ok=True)
        _ensure_current_schema()


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


def insert_batch(
    pairs: list[tuple[str, str]],
    lang_pair: str,
    max_len: int,
    engine_name: str = "",
) -> int:
    """
    批量写入新词条（INSERT OR UPDATE）。
    返回实际写入条数。
    """
    to_insert: list[tuple] = []
    for src, tgt in pairs:
        src = normalize_tm_text_for_storage(src)
        tgt = normalize_tm_text_for_storage(tgt)
        if not src or not tgt:
            continue
        if len(src) > max_len:
            continue  # 超长词条不入库

        to_insert.append(
            (
                src,
                _make_hash(src, lang_pair),
                tgt,
                lang_pair,
                AUTO_WORD_TYPE,
                engine_name,
            )
        )

    if not to_insert:
        return 0

    sql = """
        INSERT INTO tm_entries (source_text, source_hash, target_text, lang_pair, word_type, source_engine)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_text, lang_pair) DO UPDATE SET
            target_text   = excluded.target_text,
            source_hash   = excluded.source_hash,
            source_engine = excluded.source_engine,
            updated_at    = CURRENT_TIMESTAMP
    """
    with _get_conn() as conn:
        conn.executemany(sql, to_insert)

    logger.debug(f"TM 写入 {len(to_insert)} 条（{lang_pair}）")
    return len(to_insert)


def insert_manual_entry(source: str, target: str, lang_pair: str) -> bool:
    """
    手动新增单条词条（word_type='manual'）。
    若原文已存在则更新译文（不修改固定状态）。
    返回 True 表示成功。
    """
    sql = """
        INSERT INTO tm_entries (source_text, source_hash, target_text, lang_pair, word_type, source_engine)
        VALUES (?, ?, ?, ?, 'manual', 'manual')
        ON CONFLICT(source_text, lang_pair) DO UPDATE SET
            target_text = excluded.target_text,
            source_hash = excluded.source_hash,
            updated_at  = CURRENT_TIMESTAMP
    """
    try:
        source = normalize_tm_text_for_storage(source)
        target = normalize_tm_text_for_storage(target)
        if not source or not target:
            return False
        with _get_conn() as conn:
            conn.execute(sql, [source, _make_hash(source, lang_pair), target, lang_pair])
        logger.debug(f"TM 手动新增：{source[:20]} → {target[:20]}")
        return True
    except Exception as e:
        logger.warning(f"TM 手动新增失败：{e}")
        return False


def update_entry(entry_id: int, new_target: str) -> None:
    """更新单条词条的译文（TM 管理页手动编辑用）。"""
    sql = """
        UPDATE tm_entries
        SET target_text = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """
    normalized_target = normalize_tm_text_for_storage(new_target)
    with _get_conn() as conn:
        conn.execute(sql, [normalized_target, entry_id])
    logger.debug(f"TM 更新条目 id={entry_id}")


def update_entry_full(entry_id: int, new_source: str, new_target: str) -> bool:
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
            # 先查出该词条的 lang_pair，以便计算新哈希
            row = conn.execute(
                "SELECT lang_pair FROM tm_entries WHERE id = ?", [entry_id]
            ).fetchone()
            if not row:
                logger.warning(f"TM 全量更新失败：词条 id={entry_id} 不存在")
                return False
            
            lang_pair = row["lang_pair"]
            new_hash = _make_hash(new_source, lang_pair)
            
            # 更新原文、译文、哈希、固定状态
            conn.execute(
                """
                UPDATE tm_entries
                SET source_text = ?, source_hash = ?, target_text = ?, pinned = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                [new_source, new_hash, new_target, entry_id],
            )
        logger.debug(f"TM 全量更新条目 id={entry_id}（已同步 source_hash）")
        return True
    except Exception as e:
        logger.warning(f"TM 全量更新失败（可能原文冲突）：{e}")
        return False


def delete_entry(entry_id: int) -> None:
    """删除单条词条。"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM tm_entries WHERE id = ?", [entry_id])
    logger.debug(f"TM 删除条目 id={entry_id}")


def delete_unpinned_entries(lang_pair: str, keyword: str = "") -> int:
    """
    仅删除未固定词条（pinned=0）。
    返回删除条数。
    """
    base_where = "WHERE lang_pair = ? AND pinned = 0"
    params: list = [lang_pair]
    if keyword.strip():
        base_where += " AND (source_text LIKE ? OR target_text LIKE ?)"
        kw = f"%{keyword.strip()}%"
        params.extend([kw, kw])
    with _get_conn() as conn:
        cur = conn.execute(f"DELETE FROM tm_entries {base_where}", params)
        return cur.rowcount


def delete_all_entries(lang_pair: str, keyword: str = "") -> int:
    """
    删除指定语言对下（可按关键词过滤）的所有词条。
    返回删除条数。
    """
    base_where = "WHERE lang_pair = ?"
    params: list = [lang_pair]
    if keyword.strip():
        base_where += " AND (source_text LIKE ? OR target_text LIKE ?)"
        kw = f"%{keyword.strip()}%"
        params.extend([kw, kw])
    with _get_conn() as conn:
        cur = conn.execute(f"DELETE FROM tm_entries {base_where}", params)
        return cur.rowcount


def bulk_update(updates: list[tuple[int, str]]) -> int:
    """
    批量更新译文（TM 清洗确认写入用）。
    updates: [(entry_id, new_target_text), ...]
    返回更新条数。
    """
    if not updates:
        return 0
    sql = """
        UPDATE tm_entries
        SET target_text = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """
    rows = [(normalize_tm_text_for_storage(tgt), eid) for eid, tgt in updates]
    rows = [(tgt, eid) for tgt, eid in rows if tgt]
    if not rows:
        return 0
    with _get_conn() as conn:
        conn.executemany(sql, rows)
    logger.info(f"TM 批量更新 {len(rows)} 条")
    return len(rows)


# ── 固定管理 ──────────────────────────────────────────────────────────────────

def pin_entry(entry_id: int, pinned: bool = True) -> None:
    """固定或解除固定单条词条。"""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE tm_entries SET pinned = ? WHERE id = ?",
            [1 if pinned else 0, entry_id],
        )
    logger.debug(f"TM {'固定' if pinned else '解固'} id={entry_id}")


def bulk_pin_entries(ids: list[int], pinned: bool = True) -> None:
    """批量固定或解除固定（按 ID 列表）。"""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE tm_entries SET pinned = ? WHERE id IN ({placeholders})",
            [1 if pinned else 0, *ids],
        )
    logger.debug(f"TM 批量{'固定' if pinned else '解固'} {len(ids)} 条")


def set_all_pinned(lang_pair: str, pinned: bool, keyword: str = "") -> int:
    """
    将指定语言对下（可按关键词过滤）的所有词条设置为固定或解固。
    返回影响条数。
    """
    base_where = "WHERE lang_pair = ?"
    params: list = [lang_pair]
    if keyword.strip():
        base_where += " AND (source_text LIKE ? OR target_text LIKE ?)"
        kw = f"%{keyword.strip()}%"
        params.extend([kw, kw])
    with _get_conn() as conn:
        cur = conn.execute(
            f"UPDATE tm_entries SET pinned = ? {base_where}",
            [1 if pinned else 0, *params],
        )
        return cur.rowcount


# ── 查询与统计 ────────────────────────────────────────────────────────────────

def get_stats(lang_pair: str) -> dict[str, int]:
    """返回指定语言对的词条统计。"""
    sql = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) as pinned,
            SUM(CASE WHEN word_type = 'manual' THEN 1 ELSE 0 END) as manual
        FROM tm_entries
        WHERE lang_pair = ?
    """
    with _get_conn() as conn:
        row = conn.execute(sql, [lang_pair]).fetchone()

    total = int(row["total"] or 0)
    pinned = int(row["pinned"] or 0)
    manual = int(row["manual"] or 0)
    return {
        "total": total,
        "pinned": pinned,
        "unpinned": max(total - pinned, 0),
        "manual": manual,
        "auto": max(total - manual, 0),
    }


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


def import_entries(
    entries: list[dict],
    lang_pair: str,
    mode: str,
) -> dict[str, int]:
    """
    从外部数据导入词条。
    mode: 'skip'（跳过重复）| 'overwrite'（覆盖重复）| 'keep_both'（保留双份）
    返回 {inserted, skipped, duplicates}。
    """
    # 取出现有原文集合
    with _get_conn() as conn:
        existing = {
            r[0] for r in conn.execute(
                "SELECT source_text FROM tm_entries WHERE lang_pair = ?", [lang_pair]
            ).fetchall()
        }

    to_insert:    list[tuple] = []
    to_update:    list[tuple] = []
    to_dup_insert: list[tuple] = []
    duplicates = 0

    for e in entries:
        src = normalize_tm_text_for_storage(e.get("source_text", ""))
        tgt = normalize_tm_text_for_storage(e.get("target_text", ""))
        if not src or not tgt:
            continue
        pinned = 1 if str(e.get("pinned", "0")) not in ("0", "False", "false", "") else 0
        wt = _normalize_word_type(e.get("word_type", AUTO_WORD_TYPE))

        if src in existing:
            duplicates += 1
            if mode == "overwrite":
                to_update.append((tgt, pinned, _make_hash(src, lang_pair), src, lang_pair))
            elif mode == "keep_both":
                dup_src = src + " [导入备份]"
                to_dup_insert.append((dup_src, _make_hash(dup_src, lang_pair), tgt, lang_pair, wt, "import", pinned))
        else:
            to_insert.append((src, _make_hash(src, lang_pair), tgt, lang_pair, wt, "import", pinned))

    ins_sql = """
        INSERT OR IGNORE INTO tm_entries
            (source_text, source_hash, target_text, lang_pair, word_type, source_engine, pinned)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    upd_sql = """
        UPDATE tm_entries
        SET target_text = ?, pinned = ?, source_hash = ?, updated_at = CURRENT_TIMESTAMP
        WHERE source_text = ? AND lang_pair = ?
    """
    inserted = 0
    with _get_conn() as conn:
        if to_insert:
            conn.executemany(ins_sql, to_insert)
            inserted += len(to_insert)
        if to_update:
            conn.executemany(upd_sql, to_update)
            inserted += len(to_update)
        if to_dup_insert:
            conn.executemany(ins_sql, to_dup_insert)
            inserted += len(to_dup_insert)

    skipped = duplicates if mode == "skip" else 0
    logger.info(f"TM 导入：新增 {inserted} 条，重复 {duplicates} 条（模式={mode}）")
    return {"inserted": inserted, "skipped": skipped, "duplicates": duplicates}


def get_all_entries_for_cleaning(lang_pair: str) -> list[dict]:
    """
    取出全部未固定词条供 TM 清洗模块使用（不分页，跳过 pinned=1 的词条）。
    返回 [{id, source_text, target_text}, ...]。
    """
    sql = """
        SELECT id, source_text, target_text
        FROM tm_entries
        WHERE lang_pair = ? AND pinned = 0
        ORDER BY id
    """
    with _get_conn() as conn:
        rows = conn.execute(sql, [lang_pair]).fetchall()
    return [dict(r) for r in rows]
