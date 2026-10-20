"""生词本数据层 - 独立的 SQLite 数据库"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_FILE = Path(__file__).resolve().parent.parent / "user.db"

# ── Schema ──────────────────────────────────────────────────────────
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS vocabulary_words (
    id                TEXT PRIMARY KEY,
    word              TEXT NOT NULL,
    translation       TEXT NOT NULL,
    context_url       TEXT,
    context_text      TEXT,
    source_language   TEXT,
    target_language   TEXT,
    star              INTEGER NOT NULL DEFAULT 1 CHECK(star BETWEEN 1 AND 5),
    created_at        INTEGER NOT NULL,
    last_reviewed_at  INTEGER,
    review_count      INTEGER NOT NULL DEFAULT 0,
    note              TEXT
);

CREATE INDEX IF NOT EXISTS idx_vw_word      ON vocabulary_words(word);
CREATE INDEX IF NOT EXISTS idx_vw_star      ON vocabulary_words(star);
CREATE INDEX IF NOT EXISTS idx_vw_created   ON vocabulary_words(created_at);
CREATE INDEX IF NOT EXISTS idx_vw_reviewed  ON vocabulary_words(last_reviewed_at);
CREATE INDEX IF NOT EXISTS idx_vw_count     ON vocabulary_words(review_count);
"""


def _ensure_db() -> None:
    """确保表结构存在。

    幂等（``_CREATE_SQL`` 全是 ``IF NOT EXISTS``）：不能只看文件是否存在——
    空文件（被其他进程 connect 过）同样需要建表，否则后续查询会
    ``no such table``。
    """
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.executescript(_CREATE_SQL)
        conn.commit()
    finally:
        conn.close()


# ── Connection helper ───────────────────────────────────────────────
@contextmanager
def _get_conn():
    _ensure_db()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── CRUD ───────────────────────────────────────────────────────────


def add_word(
    word: str,
    translation: str,
    *,
    context_url: Optional[str] = None,
    context_text: Optional[str] = None,
    source_language: Optional[str] = None,
    target_language: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """添加生词，返回完整记录"""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    record = {
        "id": str(uuid.uuid4()),
        "word": word,
        "translation": translation,
        "context_url": context_url,
        "context_text": context_text,
        "source_language": source_language,
        "target_language": target_language,
        "star": 1,
        "created_at": now,
        "last_reviewed_at": None,
        "review_count": 0,
        "note": note,
    }

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO vocabulary_words
            (id, word, translation, context_url, context_text,
             source_language, target_language, star, created_at,
             last_reviewed_at, review_count, note)
            VALUES
            (:id, :word, :translation, :context_url, :context_text,
             :source_language, :target_language, :star, :created_at,
             :last_reviewed_at, :review_count, :note)
            """,
            record,
        )
        conn.commit()
    return record


def get_word(word_id: str) -> Optional[dict]:
    """按 ID 查询单个生词"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM vocabulary_words WHERE id = ?", (word_id,)
        ).fetchone()
    return dict(row) if row else None


def list_words(
    *,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "created_at",  # created_at | star | word
    sort_order: str = "desc",  # asc | desc
    search: Optional[str] = None,
) -> tuple[list[dict], int]:
    """查询生词列表，返回 (records, total)"""
    # 白名单校验，防止 SQL 注入
    valid_sort = {"created_at": "created_at", "star": "star", "word": "word"}
    valid_order = {"asc": "ASC", "desc": "DESC"}
    col = valid_sort.get(sort_by, "created_at")
    order = valid_order.get(sort_order.lower(), "DESC")

    where_clause = ""
    params: list = []
    if search:
        where_clause = "WHERE word LIKE ?"
        params.append(f"%{search}%")

    with _get_conn() as conn:
        # total
        total_row = conn.execute(
            f"SELECT COUNT(*) FROM vocabulary_words {where_clause}", params
        ).fetchone()
        total = total_row[0] if total_row else 0

        # records
        rows = conn.execute(
            f"""
            SELECT * FROM vocabulary_words
            {where_clause}
            ORDER BY {col} {order}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

    return [dict(r) for r in rows], total


def list_reviewed_words(*, limit: int = 100, offset: int = 0) -> list[dict]:
    """查询至少复习过一次的生词（复习提醒队列的来源）。

    按创建时间倒序返回，最新的生词优先。
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM vocabulary_words
            WHERE last_reviewed_at IS NOT NULL
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    return [dict(r) for r in rows]


def update_word(word_id: str, updates: dict) -> Optional[dict]:
    """
    更新生词（仅允许修改 star / translation / note）
    返回更新后的记录，不存在则返回 None
    """
    allowed = {"star", "translation", "note"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return get_word(word_id)

    # 校验 star 范围
    if "star" in filtered and not (1 <= filtered["star"] <= 5):
        raise ValueError("star must be between 1 and 5")

    set_clause = ", ".join(f"{k} = :{k}" for k in filtered)
    filtered["id"] = word_id

    with _get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE vocabulary_words SET {set_clause} WHERE id = :id",
            filtered,
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return get_word(word_id)


def delete_word(word_id: str) -> bool:
    """删除生词，返回是否成功"""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM vocabulary_words WHERE id = ?", (word_id,))
        conn.commit()
        return cursor.rowcount > 0


def review_word(word_id: str) -> Optional[dict]:
    """标记为复习一次：review_count+1、last_reviewed_at=now"""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE vocabulary_words
            SET review_count = review_count + 1,
                last_reviewed_at = ?
            WHERE id = ?
            """,
            (now, word_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return get_word(word_id)
