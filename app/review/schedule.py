"""艾宾浩斯调度算法（纯函数）

从 vibe-reading ``src/utils/review/schedule.ts`` 移植而来，行为保持一致：

- 单词纯靠时间往上爬阶梯（``auto``）；
- 复习卡片上的「记得 / 忘了」只做微调——记得跨两级，忘了退一级；
- 免打扰时段、每日配额、失败退避都在这里算，调度器只负责执行。

本模块不碰数据库、不发通知，方便单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Mapping, Optional

from app.review.constants import (
    BACKOFF_BASE_MINUTES,
    BACKOFF_MAX_MINUTES,
    MIN_WAKEUP_DELAY_MS,
    SRS_INTERVALS_MINUTES,
)

MAX_STAGE = len(SRS_INTERVALS_MINUTES) - 1

AdvanceKind = Literal["auto", "remembered", "forgotten"]

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def clamp_stage(stage: int) -> int:
    """把阶段限制在合法区间内。"""
    return max(0, min(int(stage), MAX_STAGE))


def interval_at(stage: int) -> int:
    """某一阶段的间隔（毫秒）。"""
    return SRS_INTERVALS_MINUTES[clamp_stage(stage)] * 60_000


def get_interval_ms(stage: int) -> int:
    """公开接口：某阶段距离下次到期还有多久（毫秒）。"""
    return interval_at(stage)


@dataclass(frozen=True)
class AdvancedStage:
    """一次推进的结果。"""

    stage: int
    next_due_at: int
    graduated: bool


def advance_stage(entry: Mapping[str, Any], kind: AdvanceKind, now: int) -> AdvancedStage:
    """把词条推进到下一阶段，并算出下一次到期时间。

    时间本身驱动阶梯（``auto``）；通知上的按钮只是微调——已经会的词一次跨两级，
    刚忘掉的词退回一级。
    """
    stage = clamp_stage(int(entry.get("stage", 0)))

    if kind == "forgotten":
        reverted = max(stage - 1, 0)
        return AdvancedStage(
            stage=reverted,
            next_due_at=now + interval_at(reverted),
            graduated=False,
        )

    if kind == "remembered":
        jumped = stage + 2
        return AdvancedStage(
            stage=jumped,
            # 多等一级：这个词刚证明自己简单。
            next_due_at=now + interval_at(stage + 1),
            graduated=jumped > MAX_STAGE,
        )

    nxt = stage + 1
    return AdvancedStage(
        stage=nxt,
        next_due_at=now + interval_at(stage),
        graduated=nxt > MAX_STAGE,
    )


def apply_advanced_stage(
    entry: Mapping[str, Any],
    advanced: AdvancedStage,
    graduated_action: str,
    now: int,
) -> dict[str, Any]:
    """把推进结果写回词条（返回新字典）。"""
    updated = dict(entry)

    if not advanced.graduated:
        updated["stage"] = advanced.stage
        updated["next_due_at"] = advanced.next_due_at
        updated["graduated"] = False
        return updated

    if graduated_action == "restart":
        updated["stage"] = 0
        updated["next_due_at"] = now + interval_at(0)
        updated["push_count"] = 0
        updated["graduated"] = False
        return updated

    updated["stage"] = advanced.stage
    updated["graduated"] = True
    return updated


def get_backoff_ms(failure_count: int) -> int:
    """推送失败后的退避时间（毫秒）。

    没有退避的话，通知权限被拒或 LLM 挂掉会变成热循环。
    """
    exponent = max(0, min(int(failure_count), 10))
    return min(BACKOFF_BASE_MINUTES * 2**exponent, BACKOFF_MAX_MINUTES) * 60_000


def pick_due_entries(
    entries: Mapping[str, dict[str, Any]],
    now: int,
    limit: int,
) -> list[dict[str, Any]]:
    """挑出已到期的词条：最该复习的（逾期最久、星级最低）排在前面。

    返回的是**队列里的原对象**——调度器直接改它们，改完再整体落库。
    """
    due = [
        entry
        for entry in entries.values()
        if not entry.get("graduated") and int(entry.get("next_due_at", 0)) <= now
    ]
    due.sort(
        key=lambda e: (
            int(e.get("next_due_at", 0)),
            int(e.get("star", 1)),
            int(e.get("push_count", 0)),
            str(e.get("word_id", "")),
        )
    )
    return due[: max(0, limit)]


def parse_time_to_minutes(value: str) -> int:
    """``HH:mm`` → 当天零点起的分钟数；格式不对按 0 处理。"""
    match = _TIME_RE.match(value or "")
    if not match:
        return 0
    return int(match.group(1)) * 60 + int(match.group(2))


def is_within_quiet_hours(date: datetime, quiet_hours: Mapping[str, Any]) -> bool:
    """当前是否处于免打扰时段。

    支持跨零点的窗口（默认 23:00 → 08:00）。起止相同的窗口视为「没有免打扰」，
    而不是「全天免打扰」。
    """
    if not quiet_hours.get("enabled", True):
        return False

    start = parse_time_to_minutes(str(quiet_hours.get("start", "")))
    end = parse_time_to_minutes(str(quiet_hours.get("end", "")))

    if start == end:
        return False

    current = date.hour * 60 + date.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def get_next_allowed_timestamp(date: datetime, quiet_hours: Mapping[str, Any]) -> int:
    """``date`` 之后最早允许推送的时刻；在免打扰时段内就是窗口结束那一刻。"""
    if not is_within_quiet_hours(date, quiet_hours):
        return int(date.timestamp() * 1000)

    start = parse_time_to_minutes(str(quiet_hours.get("start", "")))
    end = parse_time_to_minutes(str(quiet_hours.get("end", "")))
    current = date.hour * 60 + date.minute

    end_date = date.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)

    # 跨零点的窗口：昨天开始的窗口今天结束，今天才开始的窗口明天结束。
    if start > end and current >= start:
        end_date += timedelta(days=1)

    return int(end_date.timestamp() * 1000)


def get_day_key(date: datetime) -> str:
    """本地日历日，例如 ``2026-01-31``。"""
    return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"


def get_start_of_next_day(date: datetime) -> int:
    """明天零点（毫秒时间戳）。"""
    nxt = (date + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(nxt.timestamp() * 1000)


def get_next_wakeup_at(
    entries: Mapping[str, Mapping[str, Any]],
    review_config: Mapping[str, Any],
    now: int,
) -> Optional[int]:
    """下一次该唤醒调度器的时间戳；队列里没有可推送的词时返回 ``None``。

    免打扰时段会把唤醒时间推后，而不是简单地跳过这一次。返回的时刻不会比
    ``now + MIN_WAKEUP_DELAY_MS`` 更近——否则一堆同时到期的词会变成热循环。
    """
    pending = [
        int(entry.get("next_due_at", 0))
        for entry in entries.values()
        if not entry.get("graduated")
    ]
    if not pending:
        return None

    earliest = min(pending)
    due_date = datetime.fromtimestamp(max(earliest, now) / 1000)
    allowed = get_next_allowed_timestamp(due_date, review_config.get("quiet_hours", {}))
    return max(allowed, now + MIN_WAKEUP_DELAY_MS)


def count_pending_entries(entries: Mapping[str, Mapping[str, Any]]) -> int:
    """已推送但还没评分的词条数量。"""
    return sum(1 for entry in entries.values() if entry.get("pending_review"))


def pick_next_pending_entry(
    entries: Mapping[str, Mapping[str, Any]],
    exclude_word_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """取下一个待评分的词条（推送时间最早的优先）。"""
    candidates = [
        dict(entry)
        for entry in entries.values()
        if entry.get("pending_review") and entry.get("word_id") != exclude_word_id
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda e: (
            int(e.get("last_notified_at") or 0),
            int(e.get("next_due_at", 0)),
            str(e.get("word_id", "")),
        )
    )
    return candidates[0]


def defer_entries(
    entries: Mapping[str, Mapping[str, Any]],
    word_ids: Iterable[str],
    timestamp: int,
) -> None:
    """把一批词条整体推迟到某个时刻（就地修改）。"""
    for word_id in word_ids:
        entry = entries.get(word_id)
        if entry is not None:
            entry["next_due_at"] = timestamp
