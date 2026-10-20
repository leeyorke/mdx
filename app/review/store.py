"""复习提醒的数据层

与生词本共用同一个 SQLite 文件（``user.db``），表结构与扩展里原本存在
``chrome.storage.local`` 的状态一一对应：

- ``review_schedule``  —— 每个词的复习进度（阶梯、到期时间、推送次数……）
- ``review_stats``     —— 每日推送配额统计
- ``review_settings``  —— 复习提醒的配置（JSON）
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from app.review.constants import (
    DEFAULT_GRADUATED_ACTION,
    DEFAULT_MAX_PER_DAY,
    DEFAULT_MAX_PER_WORD_PER_DAY,
    DEFAULT_OPEN_URL_TEMPLATE,
    DEFAULT_PAGE_BASE,
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    ENTRY_TTL_DAYS,
    STATS_TTL_DAYS,
)
from app.review.schedule import get_day_key
from app.vocabulary import DB_FILE

# ── Schema ──────────────────────────────────────────────────────────

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS review_schedule (
    word_id             TEXT PRIMARY KEY,
    word                TEXT NOT NULL,
    translation         TEXT NOT NULL,
    context_text        TEXT,
    context_url         TEXT,
    source_language     TEXT,
    target_language     TEXT,
    star                INTEGER NOT NULL DEFAULT 1,
    stage               INTEGER NOT NULL DEFAULT 0,
    next_due_at         INTEGER NOT NULL,
    last_notified_at    INTEGER,
    push_count          INTEGER NOT NULL DEFAULT 0,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    last_example        TEXT,
    last_sentence_chinese TEXT,
    last_collocations   TEXT,
    last_angle          TEXT,
    pending_review      INTEGER NOT NULL DEFAULT 0,
    graduated           INTEGER NOT NULL DEFAULT 0,
    synced_at           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rs_due  ON review_schedule(next_due_at);
CREATE INDEX IF NOT EXISTS idx_rs_sync ON review_schedule(synced_at);

CREATE TABLE IF NOT EXISTS review_stats (
    day       TEXT PRIMARY KEY,
    total     INTEGER NOT NULL DEFAULT 0,
    per_word  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS review_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

#: 词条在数据库里的列顺序，和 :func:`_row_to_entry` 一一对应。
_ENTRY_COLUMNS = (
    "word_id",
    "word",
    "translation",
    "context_text",
    "context_url",
    "source_language",
    "target_language",
    "star",
    "stage",
    "next_due_at",
    "last_notified_at",
    "push_count",
    "failure_count",
    "last_example",
    "last_sentence_chinese",
    "last_collocations",
    "last_angle",
    "pending_review",
    "graduated",
    "synced_at",
)


def default_config() -> dict[str, Any]:
    """复习提醒的默认配置。"""
    return {
        "enabled": False,
        "generate_example": True,
        "max_per_day": DEFAULT_MAX_PER_DAY,
        "max_per_word_per_day": DEFAULT_MAX_PER_WORD_PER_DAY,
        "graduated_action": DEFAULT_GRADUATED_ACTION,
        "quiet_hours": {
            "enabled": True,
            "start": DEFAULT_QUIET_HOURS_START,
            "end": DEFAULT_QUIET_HOURS_END,
        },
        "open_url_template": DEFAULT_OPEN_URL_TEMPLATE,
        "page_base": DEFAULT_PAGE_BASE,
        "llm": {"base_url": "", "api_key": "", "model": ""},
    }


def _ensure_db() -> None:
    """确保表结构存在（幂等）。"""
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.executescript(_CREATE_SQL)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    _ensure_db()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── 配置 ────────────────────────────────────────────────────────────


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把用户配置合并到默认配置上（缺失字段用默认值补齐）。"""
    merged = dict(base)
    for key, value in override.items():
        if key in ("quiet_hours", "llm") and isinstance(value, dict):
            nested = dict(merged.get(key, {}))
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    """读取配置；从未配置过时返回默认配置。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM review_settings WHERE key = ?", ("config",)
        ).fetchone()

    if not row:
        return default_config()

    try:
        stored = json.loads(row["value"])
    except (TypeError, ValueError):
        return default_config()

    if not isinstance(stored, dict):
        return default_config()

    return _merge_config(default_config(), stored)


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """合并并保存配置，返回保存后的完整配置。"""
    config = _merge_config(load_config(), updates)

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("config", json.dumps(config, ensure_ascii=False)),
        )
        conn.commit()

    return config


# ── 调度队列 ────────────────────────────────────────────────────────


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    entry = {key: row[key] for key in _ENTRY_COLUMNS}
    entry["star"] = int(entry["star"] or 1)
    entry["stage"] = int(entry["stage"] or 0)
    entry["next_due_at"] = int(entry["next_due_at"] or 0)
    entry["push_count"] = int(entry["push_count"] or 0)
    entry["failure_count"] = int(entry["failure_count"] or 0)
    entry["pending_review"] = bool(entry["pending_review"])
    entry["graduated"] = bool(entry["graduated"])
    entry["synced_at"] = int(entry["synced_at"] or 0)

    collocations: list[str] = []
    raw = entry.get("last_collocations")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                collocations = [str(item) for item in parsed]
        except (TypeError, ValueError):
            collocations = []
    entry["last_collocations"] = collocations

    return entry


def _entry_to_row(entry: dict[str, Any]) -> tuple:
    return (
        entry["word_id"],
        entry.get("word", ""),
        entry.get("translation", ""),
        entry.get("context_text"),
        entry.get("context_url"),
        entry.get("source_language"),
        entry.get("target_language"),
        int(entry.get("star", 1) or 1),
        int(entry.get("stage", 0) or 0),
        int(entry.get("next_due_at", 0) or 0),
        entry.get("last_notified_at"),
        int(entry.get("push_count", 0) or 0),
        int(entry.get("failure_count", 0) or 0),
        entry.get("last_example"),
        entry.get("last_sentence_chinese"),
        json.dumps(entry.get("last_collocations") or [], ensure_ascii=False),
        entry.get("last_angle"),
        1 if entry.get("pending_review") else 0,
        1 if entry.get("graduated") else 0,
        int(entry.get("synced_at", 0) or 0),
    )


def load_schedule() -> dict[str, dict[str, Any]]:
    """读取全部复习队列（按生词 ID 索引）。"""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM review_schedule").fetchall()

    return {row["word_id"]: _row_to_entry(row) for row in rows}


def get_schedule_entry(word_id: str) -> Optional[dict[str, Any]]:
    """按 ID 读取单个词条。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_schedule WHERE word_id = ?", (word_id,)
        ).fetchone()

    return _row_to_entry(row) if row else None


def upsert_schedule_entries(entries: list[dict[str, Any]]) -> None:
    """批量写入词条。"""
    if not entries:
        return

    placeholders = ", ".join("?" for _ in _ENTRY_COLUMNS)
    columns = ", ".join(_ENTRY_COLUMNS)
    values = [_entry_to_row(entry) for entry in entries]

    with _get_conn() as conn:
        conn.executemany(
            f"""
            INSERT INTO review_schedule ({columns}) VALUES ({placeholders})
            ON CONFLICT(word_id) DO UPDATE SET
                word = excluded.word,
                translation = excluded.translation,
                context_text = excluded.context_text,
                context_url = excluded.context_url,
                source_language = excluded.source_language,
                target_language = excluded.target_language,
                star = excluded.star,
                stage = excluded.stage,
                next_due_at = excluded.next_due_at,
                last_notified_at = excluded.last_notified_at,
                push_count = excluded.push_count,
                failure_count = excluded.failure_count,
                last_example = excluded.last_example,
                last_sentence_chinese = excluded.last_sentence_chinese,
                last_collocations = excluded.last_collocations,
                last_angle = excluded.last_angle,
                pending_review = excluded.pending_review,
                graduated = excluded.graduated,
                synced_at = excluded.synced_at
            """,
            values,
        )
        conn.commit()


def delete_schedule_entries(word_ids: list[str]) -> int:
    """删除指定词条，返回删除数量。"""
    if not word_ids:
        return 0

    placeholders = ", ".join("?" for _ in word_ids)
    with _get_conn() as conn:
        cursor = conn.execute(
            f"DELETE FROM review_schedule WHERE word_id IN ({placeholders})", word_ids
        )
        conn.commit()
        return cursor.rowcount


def clear_schedule() -> None:
    """清空整个复习队列。"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM review_schedule")
        conn.commit()


def prune_stale_entries(
    entries: dict[str, dict[str, Any]], now: int
) -> dict[str, dict[str, Any]]:
    """丢掉太久没被同步确认的词条。"""
    cutoff = now - ENTRY_TTL_DAYS * 24 * 60 * 60 * 1000
    return {
        word_id: entry
        for word_id, entry in entries.items()
        if int(entry.get("synced_at", 0)) >= cutoff
    }


# ── 每日配额 ────────────────────────────────────────────────────────


def load_stats() -> dict[str, dict[str, Any]]:
    """读取每日推送统计。"""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM review_stats").fetchall()

    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            per_word = json.loads(row["per_word"])
        except (TypeError, ValueError):
            per_word = {}
        if not isinstance(per_word, dict):
            per_word = {}

        stats[row["day"]] = {
            "total": int(row["total"] or 0),
            "per_word": {str(k): int(v) for k, v in per_word.items()},
        }

    return stats


def save_stats(stats: dict[str, dict[str, Any]]) -> None:
    """覆盖写入每日统计。"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM review_stats")
        conn.executemany(
            "INSERT INTO review_stats (day, total, per_word) VALUES (?, ?, ?)",
            [
                (
                    day,
                    int(value.get("total", 0)),
                    json.dumps(value.get("per_word", {}), ensure_ascii=False),
                )
                for day, value in stats.items()
            ],
        )
        conn.commit()


def prune_stats(
    days: dict[str, dict[str, Any]], now: datetime
) -> dict[str, dict[str, Any]]:
    """只保留最近若干天的配额统计。"""
    cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=STATS_TTL_DAYS - 1
    )
    cutoff_key = get_day_key(cutoff)

    return {
        day: value
        for day, value in days.items()
        if day >= cutoff_key and isinstance(value, dict)
    }
