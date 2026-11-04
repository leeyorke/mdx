"""复习成果检验的业务编排（写作 + LLM 评卷 + 写作笔记）

复习走完会话之后，把本次的词（**只给词形、不给释义**）交给用户写一篇短文或逐词
造句，发给 LLM 评卷打分 + 校语法，结果可存成「写作笔记」按分数回顾。

三条硬约束（都沿用项目既有口径）：

- **绝不碰 SRS**：这里只读写 ``review_writing_*`` 三张表，``review_schedule``
  的阶梯/到期时间一个字段都不动——检验是加分项，不能反过来影响记忆曲线。
- **词表不含释义**：``build_challenge`` 只返回 ``word``。检验的意义就是凭记忆
  用词，把翻译摆旁边等于抄。
- **LLM 不可用不报错**：没配 LLM 或调用失败时走本地兜底（只算用词覆盖、不打
  分），接口照常返回，页面能明确提示「这次没评上分」，而不是点了没反应。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping, Optional

from app.review import llm_client
from app.review import writing_store as store
from app.review.constants import (
    WRITING_CORRECTION_TYPES,
    WRITING_MAX_TEXT_CHARS,
    WRITING_MAX_WORDS,
    WRITING_MODE_PARAGRAPH,
    WRITING_MODES,
    WRITING_NOTE_IMPROVE_BELOW,
    WRITING_SPAN_ERROR,
    WRITING_SPAN_GOOD,
)
from app.review.store import load_schedule
from app.review.service import _now_ms, build_session

log = logging.getLogger("app.review.writing")

#: 提交文本最长字符数（防止把整本书贴进 prompt）。
MAX_TEXT_CHARS = WRITING_MAX_TEXT_CHARS


# ── 检验词表 ────────────────────────────────────────────────────────


def build_challenge(
    word_ids: Optional[Iterable[str]] = None,
    mode: str = WRITING_MODE_PARAGRAPH,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """组装一次检验：返回 ``task_id`` + 词列表（**只有词形，没有释义**）。

    ``word_ids`` 为空时取「立即复习」会话的词表——检验本来就该跟着刚复习完的
    这一组走。前端也可以把页面手里已有的会话队列 ID 直接传进来，省一次组装。
    """
    if mode not in WRITING_MODES:
        mode = WRITING_MODE_PARAGRAPH
    now = now if now is not None else _now_ms()

    # ``None`` = 调用方没指定词表 → 回落「立即复习」会话的词；``[]`` = 调用方
    # 显式给了空词表 → 就地 empty（别误回落到会话，否则「检验空词表」会被
    # 偷偷换成会话的词，用户莫名其妙）。
    if word_ids is None:
        explicit_ids: Optional[list[str]] = None
    else:
        explicit_ids = [str(w).strip() for w in word_ids if str(w).strip()]
    state, empty_reason = "ready", None

    if explicit_ids is not None:
        schedule = load_schedule()
        picked = [schedule[wid] for wid in explicit_ids if wid in schedule]
        if not picked:
            state, empty_reason = "empty", "noWords"
    else:
        session = build_session("manual", now=now)
        schedule = load_schedule()
        picked = []
        for wid in {w["word_id"] for w in session.get("words", [])}:
            entry = schedule.get(wid)
            if entry:
                picked.append(entry)
        if not picked:
            state = "empty"
            empty_reason = session.get("empty_reason") or "noWords"

    # 词表只留词形；顺序按会话给的顺序，去重。
    words: list[str] = []
    seen: set[str] = set()
    kept_ids: list[str] = []
    for entry in picked:
        word = str(entry.get("word") or "").strip()
        if not word or word.lower() in seen:
            continue
        seen.add(word.lower())
        words.append(word)
        kept_ids.append(entry["word_id"])
    words = words[:WRITING_MAX_WORDS]
    kept_ids = kept_ids[:WRITING_MAX_WORDS]

    if state != "empty" and not words:
        state, empty_reason = "empty", "noWords"

    task: Optional[dict[str, Any]] = None
    if words:
        task = store.create_task(
            word_ids=kept_ids, words=words, mode=mode, created_at=now
        )

    return {
        "state": state,
        "empty_reason": empty_reason,
        "mode": mode,
        "task_id": task["task_id"] if task else None,
        # 只含词形——检验不给释义
        "words": words,
        "word_ids": kept_ids,
        "max_words": WRITING_MAX_WORDS,
        "max_text_chars": MAX_TEXT_CHARS,
    }


# ── 本地兜底：用词覆盖匹配 ───────────────────────────────────────────


def _tokens(text: str) -> list[str]:
    """把文本切成小写词元（只留字母数字与撇号内部的字符）。"""
    return re.findall(r"[a-z0-9']+", text.lower())


def _stem(token: str) -> str:
    """极简词形归一化：beliefs→believ、believed→believ、running→runn。

    只做后缀截断，不做真正的词形还原——本地兜底只负责「这个词大概用上了」，
    权威判断交给 LLM。
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


#: 前缀比对要求的最短词干长度。太短的前缀匹配会误伤（``a`` / ``ap`` 这种会撞上
#: 任意 a 开头的词），所以两边都到 3 个字符以上才做前缀比对——``run`` / ``runn``
#: 能过，``ap`` → ``apple`` 这种被挡住。
_PREFIX_MIN_LEN = 3


def _same_word(stemmed: str, target_stem: str) -> bool:
    """词干相等，或一方是另一方的前缀（覆盖 believe/believ、run/runn）。"""
    if stemmed == target_stem:
        return True
    if len(stemmed) < _PREFIX_MIN_LEN or len(target_stem) < _PREFIX_MIN_LEN:
        return False
    shorter, longer = sorted((stemmed, target_stem), key=len)
    return longer.startswith(shorter)


def match_used_words(text: str, words: Iterable[str]) -> list[str]:
    """大小写不敏感的整词匹配，算出文本里真正用到的目标词。

    先整词匹配，再对没命中的词用 :func:`_stem` 归一化后比对（词干相等或互为
    前缀），覆盖 believes / believed / believing 这类词形变化。返回按词表顺序
    排列的词形（不是文本里的形式）。
    """
    tokens = _tokens(text)
    token_set = set(tokens)
    stem_set = {_stem(t) for t in tokens}

    used: list[str] = []
    for word in words:
        key = word.lower()
        if key in token_set:
            used.append(word)
            continue
        target_stem = _stem(key)
        if any(_same_word(t, target_stem) for t in stem_set):
            used.append(word)
    return used


# ── 评卷 ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a strict but encouraging English teacher grading a Chinese learner's "
    "writing. You always answer with a single JSON object and nothing else."
)

_USER_PROMPT_TEMPLATE = """\
The learner was asked to use ALL of these target words in their writing:
{word_list}

Writing mode: {mode}

The learner's text:
\"\"\"
{text}
\"\"\"

Grade it and answer with a single JSON object using exactly these keys:
{{
  "score": 88,
  "usage": [{{"word": "verdict", "used": true, "form_in_text": "verdict"}}],
  "spans": [
    {{"start": 32, "end": 39, "text": "verdict", "kind": "good"}},
    {{"start": 62, "end": 70, "text": "believes", "kind": "error"}}
  ],
  "feedback": "一句中文总评",
  "corrections": [
    {{"original": "most of the people believes", "suggestion": "most people believe",
      "explanation": "主谓一致错误……", "type": "grammar"}}
  ],
  "vocab_suggestions": [{{"word": "deliberation", "note": "用语高级且准确……"}}],
  "polished": "修改后的完整短文"
}}

Rules:
- "score" is an integer 0-100.
- "usage" must cover EVERY target word; a word counts as used if any inflected
  form appears (believes counts for believe). Set "used" false if it is missing.
- "spans" marks the learner's ORIGINAL text: "start"/"end" are character offsets
  into the exact text above, "text" must equal that slice, and "kind" is "good"
  for a correctly used target word or "error" for a mistake.
- "corrections" lists each grammar / spelling / tense / collocation / word-choice
  problem, with a short Chinese "explanation" and "type" from: {types}.
- "vocab_suggestions" praises or upgrades vocabulary actually used.
- "polished" is the corrected full text (empty string if nothing to fix).
- "feedback" and "explanation" are in Chinese.
- No markdown, no explanation, JSON only.
"""


def _format_mode(mode: str) -> str:
    return (
        "one paragraph using all the words"
        if mode == WRITING_MODE_PARAGRAPH
        else "one or more sentences per word"
    )


def _validate_spans(
    spans: list[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    """按原文校验 span，丢掉对不上的（LLM 会数错 offset，不能让前端画错位）。"""
    kept: list[dict[str, Any]] = []
    for span in spans:
        start, end = int(span["start"]), int(span["end"])
        slice_text = text[start:end]
        declared = str(span.get("text") or "")
        # text 字段没给或与切片一致都接受；切片与声明的词对不上就丢弃。
        if declared and declared != slice_text:
            continue
        kept.append({**span, "text": slice_text})
    return kept


def _clamp_score(raw: Any) -> Optional[int]:
    """分数钳到 0-100 的整数；给不出数字就返回 ``None``。"""
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, value))


def _grade_via_llm(
    config: Mapping[str, Any], task_words: list[str], text: str, mode: str
) -> Optional[dict[str, Any]]:
    """调 LLM 评卷；返回规整后的 grade dict，失败返回 ``None``。"""
    prompt = _USER_PROMPT_TEMPLATE.format(
        word_list=", ".join(task_words),
        mode=_format_mode(mode),
        text=text,
        types=", ".join(WRITING_CORRECTION_TYPES),
    )
    raw = llm_client.chat(
        config,
        system=_SYSTEM_PROMPT,
        user=prompt,
        temperature=0.2,
        timeout=llm_client.WRITING_GRADE_TIMEOUT_SECONDS,
    )
    if raw is None:
        return None
    parsed = llm_client.parse_json_payload(raw)
    if parsed is None:
        log.warning("评卷输出解析不出 JSON，走本地兜底")
        return None

    grade = store.normalize_grade(parsed)
    grade["spans"] = _validate_spans(grade["spans"], text)
    score = _clamp_score(parsed.get("score"))
    feedback = str(parsed.get("feedback") or "").strip()

    usage = grade["usage"]
    used = [item["word"] for item in usage if item["used"]]
    # LLM 没给 usage 时，用本地匹配补一份，保证「用了哪些词」始终有值。
    if not usage:
        used = match_used_words(text, task_words)
        usage = [
            {"word": w, "used": w in used, "form_in_text": ""} for w in task_words
        ]
        grade["usage"] = usage

    return {
        "grade": grade,
        "score": score,
        "feedback": feedback,
        "used_words": used,
        "llm_source": "llm",
    }


def grade_writing(
    task_id: str, text: str, mode: Optional[str] = None, now: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """评一次卷：写 submission，返回评卷结果。

    返回 ``None`` 的情况（路由转 404）：``task_id`` 无效、或 ``text`` 为空 /
    超长。

    LLM 不可用（没配 / 调用失败）时**不报错**：走本地匹配算用词覆盖，
    ``score`` 与 ``grade`` 留空、``llm_source="matcher"``，让页面明确提示
    「这次没评上分」，而不是静默失败。
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        return None
    now = now if now is not None else _now_ms()

    task = store.get_task(task_id)
    if not task:
        return None

    task_mode = mode if mode in WRITING_MODES else task["mode"]
    task_words = task["words"]
    config = review_config()

    result: Optional[dict[str, Any]] = None
    if llm_client.is_configured(config):
        result = _grade_via_llm(config, task_words, text, task_mode)

    if result is None:
        used = match_used_words(text, task_words)
        result = {
            "grade": store.normalize_grade({}),
            "score": None,
            "feedback": "未配置 LLM（或本次调用失败），仅做了用词覆盖统计，未打分。",
            "used_words": used,
            "llm_source": "matcher",
        }

    submission = store.create_submission(
        task_id=task_id,
        text=text,
        mode=task_mode,
        word_ids=task["word_ids"],
        words=task_words,
        used_words=result["used_words"],
        score=result["score"],
        feedback=result["feedback"],
        grade=result["grade"],
        llm_source=result["llm_source"],
        created_at=now,
    )

    return _presentation(submission)


def _presentation(submission: Mapping[str, Any]) -> dict[str, Any]:
    """把一条 submission 变成接口返回体（评卷结果 + 提交 ID）。"""
    return {
        "submission_id": submission["submission_id"],
        "task_id": submission["task_id"],
        "mode": submission["mode"],
        "text": submission["text"],
        "words": submission["words"],
        "used_words": submission["used_words"],
        "score": submission["score"],
        "feedback": submission["feedback"],
        "grade": submission["grade"],
        "llm_source": submission["llm_source"],
        "created_at": submission["created_at"],
    }


def review_config() -> dict[str, Any]:
    """读复习配置（LLM 部分就是评卷用的配置，复用例句那套，不单独配模型）。"""
    from app.review.store import load_config

    return load_config()


# ── 草稿与笔记 ──────────────────────────────────────────────────────


def save_draft(
    task_id: str, text: str, mode: Optional[str] = None, now: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """把稿子存成草稿笔记（不打分、不评卷）。

    对应检验页面的「保存草稿」：``score``/``grade`` 为空，之后可以再评卷。
    返回 ``None`` 表示 ``task_id`` 无效或文本为空。
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        return None
    now = now if now is not None else _now_ms()

    task = store.get_task(task_id)
    if not task:
        return None

    task_mode = mode if mode in WRITING_MODES else task["mode"]
    submission = store.create_submission(
        task_id=task_id,
        text=text,
        mode=task_mode,
        word_ids=task["word_ids"],
        words=task["words"],
        used_words=match_used_words(text, task["words"]),
        score=None,
        feedback="",
        grade={},
        llm_source="draft",
        created_at=now,
    )
    note = store.create_note_from_submission(submission, now)
    return _note_presentation(note)


def save_note(
    submission_id: str, now: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """把一次（已评卷的）提交保存为笔记。

    对应评卷弹层的「保存为笔记」。同一 ``submission_id`` 允许重复保存（用户可能
    反复存），不做唯一约束。返回 ``None`` 表示提交不存在。
    """
    now = now if now is not None else _now_ms()
    submission = store.get_submission(submission_id)
    if not submission:
        return None
    note = store.create_note_from_submission(submission, now)
    return _note_presentation(note)


def _note_presentation(note: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "note_id": note["note_id"],
        "submission_id": note["submission_id"],
        "task_id": note["task_id"],
        "mode": note["mode"],
        "text": note["text"],
        "words": note["words"],
        "score": note["score"],
        "feedback": note["feedback"],
        "grade": note["grade"],
        # 笔记列表展示成 x.y / 10（0-100 / 10），后端统一存 0-100 整数
        "score_out_of_ten": round(note["score"] / 10, 1) if note["score"] is not None else None,
        "created_at": note["created_at"],
    }


def list_notes(
    note_filter: str = "all",
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> dict[str, Any]:
    """分页列写作笔记（带三种筛选各自的计数与总数）。"""
    records, total, counts = store.list_notes(
        note_filter=note_filter,
        limit=max(1, min(100, limit)),
        offset=max(0, offset),
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "filter": note_filter if note_filter in store.NOTE_FILTERS else "all",
        "records": [_note_presentation(note) for note in records],
        "total": total,
        "counts": counts,
        "grand_total": store.count_notes(),
        "limit": limit,
        "offset": offset,
    }


def get_note(note_id: str) -> Optional[dict[str, Any]]:
    """取一篇写作笔记。"""
    note = store.get_note(note_id)
    return _note_presentation(note) if note else None


def delete_note(note_id: str) -> bool:
    """删一篇写作笔记。"""
    return store.delete_note(note_id)
