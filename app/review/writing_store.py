"""复习成果检验的数据层（写作 + LLM 评卷）

三张表（``review_writing_tasks`` / ``review_writing_submissions`` /
``review_writing_notes``）与复习队列表（``review_schedule`` 等）**完全独立**：
检验是复习链路末端的加分项，读写再多也绝不碰 SRS 进度。

建表走 ``store._CREATE_SQL``（``_ensure_db`` 每次连接都会幂等执行），所以这里
不需要单独的迁移——老库第一次用到检验功能时，表就已经在了。所有「词表 / 批改 /
用词」这类结构化字段都存 JSON 文本，读出来时统一在这里反序列化并做形状兜底
（库里被塞了畸形 JSON 也不能让接口 500）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

from app.review import store as review_store
from app.review.constants import (
    WRITING_CORRECTION_TYPES,
    WRITING_MODES,
    WRITING_NOTE_HIGH_SCORE,
    WRITING_NOTE_IMPROVE_BELOW,
    WRITING_SPAN_ERROR,
    WRITING_SPAN_GOOD,
)

# ── 连接 ────────────────────────────────────────────────────────────


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    """复用复习模块的连接 helper：``_ensure_db`` 会顺带把写作三张表建好。"""
    with review_store._get_conn() as conn:
        yield conn


def new_id() -> str:
    """生成一个主键（沿用生词本/复习队列的 uuid4 口径）。"""
    return str(uuid.uuid4())


# ── JSON 形状兜底 ───────────────────────────────────────────────────


def _load_list(raw: Any) -> list[Any]:
    """把库里存的 JSON 数组读回来；不是数组就返回空列表。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _dump(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def normalize_words(raw: Any) -> list[str]:
    """把词表统一成 ``[词形, ...]``（去空、去重、保持顺序）。"""
    seen: set[str] = set()
    words: list[str] = []
    for item in _load_list(raw):
        word = str(item or "").strip()
        if word and word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    return words


def normalize_grade(raw: Any) -> dict[str, Any]:
    """把评卷 JSON 读回来并兜底成固定形状。

    缺字段一律补空值，绝不让畸形输出把接口打 500。形状见 :mod:`app.review.writing`
    里 ``grade_writing`` 的说明。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        raw = {}

    return {
        "usage": _normalize_usage(raw.get("usage")),
        "spans": _normalize_spans(raw.get("spans")),
        "corrections": _normalize_corrections(raw.get("corrections")),
        "vocab_suggestions": _normalize_vocab(raw.get("vocab_suggestions")),
        "polished": str(raw.get("polished") or "").strip(),
    }


def _normalize_usage(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        out.append(
            {
                "word": word,
                "used": bool(item.get("used")),
                "form_in_text": str(item.get("form_in_text") or "").strip(),
            }
        )
    return out


def _normalize_spans(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in (WRITING_SPAN_GOOD, WRITING_SPAN_ERROR):
            continue
        try:
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (TypeError, ValueError):
            continue
        if start < 0 or end <= start:
            continue
        out.append(
            {
                "start": start,
                "end": end,
                "text": str(item.get("text") or "").strip(),
                "kind": kind,
            }
        )
    return out


def _normalize_corrections(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        original = str(item.get("original") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        if not original and not suggestion:
            continue
        ctype = str(item.get("type") or "").strip()
        if ctype not in WRITING_CORRECTION_TYPES:
            ctype = "word_choice"
        out.append(
            {
                "original": original,
                "suggestion": suggestion,
                "explanation": str(item.get("explanation") or "").strip(),
                "type": ctype,
            }
        )
    return out


def _normalize_vocab(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, Mapping):
            word = str(item.get("word") or "").strip()
            note = str(item.get("note") or "").strip()
        else:
            word = str(item or "").strip()
            note = ""
        if word:
            out.append({"word": word, "note": note})
    return out


# ── 行 → 字典 ───────────────────────────────────────────────────────


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["id"],
        "word_ids": _load_list(row["word_ids"]),
        "words": normalize_words(row["words"]),
        "mode": row["mode"],
        "created_at": int(row["created_at"] or 0),
    }


def _submission_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "submission_id": row["id"],
        "task_id": row["task_id"],
        "text": row["text"],
        "mode": row["mode"],
        "word_ids": _load_list(row["word_ids"]),
        "words": normalize_words(row["words"]),
        "used_words": normalize_words(row["used_words"]),
        "score": int(row["score"]) if row["score"] is not None else None,
        "feedback": row["feedback"] or "",
        "grade": normalize_grade(row["grade"]),
        "llm_source": row["llm_source"] or "llm",
        "created_at": int(row["created_at"] or 0),
    }


def _note_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "note_id": row["id"],
        "submission_id": row["submission_id"],
        "task_id": row["task_id"],
        "text": row["text"],
        "mode": row["mode"],
        "words": normalize_words(row["words"]),
        "word_ids": _load_list(row["word_ids"]),
        "score": int(row["score"]) if row["score"] is not None else None,
        "feedback": row["feedback"] or "",
        "grade": normalize_grade(row["grade"]),
        "created_at": int(row["created_at"] or 0),
    }


# ── 检验任务 ────────────────────────────────────────────────────────


def create_task(
    *, word_ids: list[str], words: list[str], mode: str, created_at: int
) -> dict[str, Any]:
    """建一次检验任务（词表快照），返回完整记录。"""
    task_id = new_id()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_writing_tasks (id, word_ids, words, mode, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                _dump(word_ids),
                _dump(words),
                mode if mode in WRITING_MODES else WRITING_MODES[0],
                created_at,
            ),
        )
        conn.commit()
    return get_task(task_id)  # type: ignore[return-value]


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    """按 ID 取一次检验任务。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_writing_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return _task_row(row) if row else None


# ── 提交 ────────────────────────────────────────────────────────────


def create_submission(
    *,
    task_id: Optional[str],
    text: str,
    mode: str,
    word_ids: list[str],
    words: list[str],
    used_words: list[str],
    score: Optional[int],
    feedback: str,
    grade: dict[str, Any],
    llm_source: str,
    created_at: int,
) -> dict[str, Any]:
    """落一次提交（评卷结果或草稿），返回完整记录。"""
    submission_id = new_id()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_writing_submissions
                (id, task_id, text, mode, word_ids, words, used_words,
                 score, feedback, grade, llm_source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                task_id,
                text,
                mode if mode in WRITING_MODES else WRITING_MODES[0],
                _dump(word_ids),
                _dump(words),
                _dump(used_words),
                score,
                feedback,
                _dump(grade) if grade else None,
                llm_source,
                created_at,
            ),
        )
        conn.commit()
    return get_submission(submission_id)  # type: ignore[return-value]


def get_submission(submission_id: str) -> Optional[dict[str, Any]]:
    """按 ID 取一次提交。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_writing_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _submission_row(row) if row else None


# ── 笔记 ────────────────────────────────────────────────────────────

#: 列表筛选口径：全部 / 高分练习 / 待优化。
NOTE_FILTERS = ("all", "high", "improve")

#: 列表排序白名单（防注入，沿用生词本 ``list_words`` 的做法）。
_NOTE_SORT_MAP = {"created_at": "created_at", "score": "score"}


def create_note_from_submission(
    submission: Mapping[str, Any], created_at: int
) -> dict[str, Any]:
    """把一次提交复制成笔记（草稿也走这里，grade/score 为空即草稿）。"""
    note_id = new_id()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_writing_notes
                (id, submission_id, task_id, text, mode, words, word_ids,
                 score, feedback, grade, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note_id,
                submission.get("submission_id") or submission.get("id"),
                submission.get("task_id"),
                str(submission.get("text") or ""),
                submission.get("mode") or WRITING_MODES[0],
                _dump(submission.get("words")),
                _dump(submission.get("word_ids")),
                submission.get("score"),
                submission.get("feedback") or "",
                _dump(submission.get("grade")) if submission.get("grade") else None,
                created_at,
            ),
        )
        conn.commit()
    return get_note(note_id)  # type: ignore[return-value]


def list_notes(
    *,
    note_filter: str = "all",
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """分页列笔记，返回 ``(records, total, counts)``。

    ``counts`` 是三种筛选各自的条数（不受当前筛选影响）——稿子上「全部 / 高分
    练习 / 待优化」三个 tab 的角标需要它，否则用户切到「待优化」前不知道那边
    有多少。筛选口径：高分 ``score >= 80``，待优化 ``score < 70``，草稿
    （``score IS NULL``）只出现在「全部」。
    """
    col = _NOTE_SORT_MAP.get(sort_by, "created_at")
    order = "ASC" if str(sort_order).lower() == "asc" else "DESC"

    def where(f: str) -> tuple[str, list]:
        if f == "high":
            return "score IS NOT NULL AND score >= ?", [WRITING_NOTE_HIGH_SCORE]
        if f == "improve":
            return "score IS NOT NULL AND score < ?", [WRITING_NOTE_IMPROVE_BELOW]
        return "1=1", []

    with _get_conn() as conn:
        counts: dict[str, int] = {}
        for name in NOTE_FILTERS:
            clause, params = where(name)
            counts[name] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM review_writing_notes WHERE {clause}",
                    params,
                ).fetchone()[0]
            )

        clause, params = where(note_filter if note_filter in NOTE_FILTERS else "all")
        total = counts.get(note_filter if note_filter in NOTE_FILTERS else "all", 0)
        rows = conn.execute(
            f"""
            SELECT * FROM review_writing_notes
            WHERE {clause}
            ORDER BY {col} {order}, created_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

    return [_note_row(row) for row in rows], total, counts


def get_note(note_id: str) -> Optional[dict[str, Any]]:
    """按 ID 取一篇笔记。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM review_writing_notes WHERE id = ?", (note_id,)
        ).fetchone()
    return _note_row(row) if row else None


def delete_note(note_id: str) -> bool:
    """删一篇笔记，返回是否删掉了。"""
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM review_writing_notes WHERE id = ?", (note_id,)
        )
        conn.commit()
        return cursor.rowcount > 0


def count_notes() -> int:
    """笔记总数（页面底部「已累计练习 N 篇短文」用）。"""
    with _get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM review_writing_notes").fetchone()[0])
