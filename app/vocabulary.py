"""生词本数据层 - 独立的 SQLite 数据库"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

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
    note              TEXT,
    -- 来源：local = 本地收藏，eudic = 来自欧路生词本
    source            TEXT NOT NULL DEFAULT 'local',
    -- 欧路侧唯一键 "<category_id>:<word.lower()>"，用于同步时对齐同一个词
    source_id         TEXT,
    -- 欧路生词本 id，写回欧路时要用
    source_category   TEXT,
    -- 音标（欧路侧带过来）
    phonetic          TEXT
);

CREATE INDEX IF NOT EXISTS idx_vw_word      ON vocabulary_words(word);
CREATE INDEX IF NOT EXISTS idx_vw_star      ON vocabulary_words(star);
CREATE INDEX IF NOT EXISTS idx_vw_created   ON vocabulary_words(created_at);
CREATE INDEX IF NOT EXISTS idx_vw_reviewed  ON vocabulary_words(last_reviewed_at);
CREATE INDEX IF NOT EXISTS idx_vw_count     ON vocabulary_words(review_count);
"""

#: 欧路接入后新增的列。``ALTER TABLE ADD COLUMN`` 不能带约束，所以列定义
#: 单独放这里，迁移时按需补齐。
_MIGRATION_COLUMNS = (
    ("source", "TEXT NOT NULL DEFAULT 'local'"),
    ("source_id", "TEXT"),
    ("source_category", "TEXT"),
    ("phonetic", "TEXT"),
)


def _migrate_db(conn: sqlite3.Connection) -> None:
    """给已存在的表补上新增的列和索引。

    ``CREATE TABLE IF NOT EXISTS`` 对已存在的表是空操作，所以老库里不会
    凭空多出 ``source`` 这些列——不补的话，查询会直接 ``no such column``。

    索引也在这里建，不能放进 ``_CREATE_SQL``：那份 SQL 先跑，而老库里
    ``source`` 列还不存在，建索引会先炸 ``no such column: source``。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(vocabulary_words)")}
    if not existing:
        # 表还没建（全新库），_CREATE_SQL 里已经带了新列
        return

    for name, ddl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE vocabulary_words ADD COLUMN {name} {ddl}")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vw_source ON vocabulary_words(source)"
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vw_source_id
        ON vocabulary_words(source_id) WHERE source_id IS NOT NULL
        """
    )


def _ensure_db() -> None:
    """确保表结构存在。

    幂等（``_CREATE_SQL`` 全是 ``IF NOT EXISTS``）：不能只看文件是否存在——
    空文件（被其他进程 connect 过）同样需要建表，否则后续查询会
    ``no such table``。
    """
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.executescript(_CREATE_SQL)
        _migrate_db(conn)
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
    source: str = "local",
    source_id: Optional[str] = None,
    source_category: Optional[str] = None,
    phonetic: Optional[str] = None,
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
        "source": source,
        "source_id": source_id,
        "source_category": source_category,
        "phonetic": phonetic,
    }

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO vocabulary_words
            (id, word, translation, context_url, context_text,
             source_language, target_language, star, created_at,
             last_reviewed_at, review_count, note,
             source, source_id, source_category, phonetic)
            VALUES
            (:id, :word, :translation, :context_url, :context_text,
             :source_language, :target_language, :star, :created_at,
             :last_reviewed_at, :review_count, :note,
             :source, :source_id, :source_category, :phonetic)
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
    source: Optional[str] = None,  # None/'all' = 不过滤；否则按来源过滤
) -> tuple[list[dict], int]:
    """查询生词列表，返回 (records, total)"""
    # 白名单校验，防止 SQL 注入
    valid_sort = {"created_at": "created_at", "star": "star", "word": "word"}
    valid_order = {"asc": "ASC", "desc": "DESC"}
    col = valid_sort.get(sort_by, "created_at")
    order = valid_order.get(sort_order.lower(), "DESC")

    where_clauses = []
    params: list = []
    if search:
        where_clauses.append("word LIKE ?")
        params.append(f"%{search}%")
    if source and source != "all":
        where_clauses.append("source = ?")
        params.append(source)

    where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

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


def count_words_by_source() -> dict[str, int]:
    """各来源的生词数量。

    给生词本页面的来源筛选做计数提示用——不然用户切到「欧路」之前根本
    不知道那边有词。注意这里统计的是**全量**，不受当前搜索/筛选影响：
    筛选器上的计数要能告诉你「那边有多少可看」。
    """
    counts = {"all": 0, "local": 0, "eudic": 0}

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM vocabulary_words GROUP BY source"
        ).fetchall()

    for row in rows:
        source = row["source"] or "local"
        total = int(row["n"])
        counts["all"] += total
        if source in ("local", "eudic"):
            counts[source] += total

    return counts


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


# ── 欧路同步 ────────────────────────────────────────────────────────


def set_word_source(
    word_id: str,
    source: str,
    source_id: Optional[str] = None,
    source_category: Optional[str] = None,
) -> Optional[dict]:
    """标记生词的来源（内部使用，不开放给客户端）。

    客户端 PATCH 的白名单里没有 ``source``——不然它能把一个纯本地词
    伪造成欧路词，再通过「删除」把欧路那边的收藏删掉。
    """
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE vocabulary_words
            SET source = ?, source_id = ?, source_category = ?
            WHERE id = ?
            """,
            (source, source_id, source_category, word_id),
        )
        conn.commit()
    return get_word(word_id)


def upsert_eudic_words(rows: Iterable[dict]) -> dict:
    """把欧路生词本里的词并入本地表。

    对齐顺序：

    1. ``source_id`` 命中 → 同一个欧路词，更新它
    2. 同名的本地词 → 认领为欧路词（同一个词只留一条，不出现两份）
    3. 都没有 → 新建

    已有词**只补空字段**，``star`` 也不覆盖：欧路 App 里的星级可能比用户
    在扩展里打的分旧，而用户的评分是主动行为。只有当这个词还没被用户
    评分过（``review_count = 0``）时，才采用欧路的星级。
    """
    imported = 0
    updated = 0
    skipped = 0

    with _get_conn() as conn:
        for row in rows:
            word = (row.get("word") or "").strip()
            if not word:
                skipped += 1
                continue

            source_id = row.get("source_id")
            existing = conn.execute(
                "SELECT * FROM vocabulary_words WHERE source_id = ?", (source_id,)
            ).fetchone()
            if existing is None:
                existing = conn.execute(
                    """
                    SELECT * FROM vocabulary_words
                    WHERE lower(word) = lower(?) AND source = 'local'
                    """,
                    (word,),
                ).fetchone()

            star = max(1, min(5, int(row.get("star") or 1)))

            if existing is None:
                now = int(datetime.now(timezone.utc).timestamp() * 1000)
                conn.execute(
                    """
                    INSERT INTO vocabulary_words
                    (id, word, translation, context_url, context_text,
                     source_language, target_language, star, created_at,
                     last_reviewed_at, review_count, note,
                     source, source_id, source_category, phonetic)
                    VALUES
                    (:id, :word, :translation, NULL, :context_text,
                     :source_language, NULL, :star, :created_at,
                     NULL, 0, NULL,
                     'eudic', :source_id, :source_category, :phonetic)
                    """,
                    {
                        "id": str(uuid.uuid4()),
                        "word": word,
                        "translation": row.get("translation") or "",
                        "context_text": row.get("context_text") or None,
                        "source_language": row.get("source_language") or None,
                        "star": star,
                        "created_at": int(row.get("created_at") or now),
                        "source_id": source_id,
                        "source_category": row.get("source_category") or None,
                        "phonetic": row.get("phonetic") or None,
                    },
                )
                imported += 1
                continue

            updates: dict = {}
            if not existing["translation"] and row.get("translation"):
                updates["translation"] = row["translation"]
            if not existing["context_text"] and row.get("context_text"):
                updates["context_text"] = row["context_text"]
            if not existing["phonetic"] and row.get("phonetic"):
                updates["phonetic"] = row["phonetic"]
            if (
                int(existing["review_count"] or 0) == 0
                and int(existing["star"] or 1) != star
            ):
                updates["star"] = star

            # 认领：这个词本地已经有了，只是欧路那边也收录了
            if existing["source"] != "eudic":
                updates["source"] = "eudic"
                updates["source_id"] = source_id
                updates["source_category"] = row.get("source_category") or None
                if not existing["source_language"] and row.get("source_language"):
                    updates["source_language"] = row["source_language"]

            if updates:
                set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                conn.execute(
                    f"UPDATE vocabulary_words SET {set_clause} WHERE id = :id",
                    {**updates, "id": existing["id"]},
                )
                updated += 1

        conn.commit()

    return {"imported": imported, "updated": updated, "skipped": skipped}
