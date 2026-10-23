"""复习提醒的业务逻辑

把扩展里 ``review-scheduler.ts`` / ``queue-sync.ts`` / ``review-handlers.ts``
的职责收拢到这里：同步队列、跑一轮调度（每日一次批量提醒）、组装复习
会话、记录评分、组装复习卡片。

扩展原来把这些状态放在 ``chrome.storage.local``，因为「后端不知道这个词上次
是什么时候被推的」。现在后端自己就是数据的源头，状态直接落在 ``user.db``。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Optional

from app.dictionary import query_word
from app.review.constants import (
    MAX_DEFERRALS_PER_TICK,
    MAX_QUEUE_SIZE,
    REVIEW_PAGE_URL,
    SRS_INTERVALS_MINUTES,
    VOCABULARY_PAGE_SIZE,
)
from app.review.example import build_example
from app.review.notifier import is_available, send_batch_notification
from app.review.schedule import (
    advance_stage,
    apply_advanced_stage,
    count_pending_entries,
    get_backoff_ms,
    get_day_key,
    get_end_of_day_ms,
    get_next_notify_at,
    get_start_of_day_ms,
    is_within_quiet_hours,
    pick_due_by,
    pick_due_entries,
    pick_next_pending_entry,
    pick_pending_entries,
    pick_upcoming_entries,
)
from app.review.store import (
    clear_schedule,
    delete_schedule_entries,
    get_schedule_entry,
    load_config,
    load_schedule,
    load_stats,
    prune_stale_entries,
    prune_stats,
    save_config,
    save_stats,
    upsert_schedule_entries,
)
from app.vocabulary import list_reviewed_words

log = logging.getLogger("app.review.service")


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


# ── 配置 ────────────────────────────────────────────────────────────


def get_config() -> dict[str, Any]:
    """读取当前配置。"""
    return load_config()


def update_config(updates: Mapping[str, Any]) -> dict[str, Any]:
    """合并配置并落库。"""
    config = save_config(dict(updates))
    log.info("复习提醒配置已更新：%s", sorted(updates.keys()))
    return config


# ── 队列同步 ────────────────────────────────────────────────────────


def _snapshot_fields(word: Mapping[str, Any], synced_at: int) -> dict[str, Any]:
    """每次同步都会刷新的字段。

    调度相关的状态（阶梯、到期时间、推送次数……）必须保留，否则后端每次重启
    单词都要从头爬阶梯。
    """
    return {
        "word": word.get("word", ""),
        "translation": word.get("translation", ""),
        "star": int(word.get("star", 1) or 1),
        "context_text": word.get("context_text"),
        "context_url": word.get("context_url"),
        "source_language": word.get("source_language"),
        "target_language": word.get("target_language"),
        "synced_at": synced_at,
    }


def _create_entry(word: Mapping[str, Any], now: int) -> dict[str, Any]:
    """新词条：阶梯从 0 开始，立刻到期。"""
    entry = {
        "word_id": word["id"],
        "stage": 0,
        "next_due_at": now,
        "last_notified_at": None,
        "push_count": 0,
        "failure_count": 0,
        "last_example": None,
        "last_sentence_chinese": None,
        "last_collocations": [],
        "last_angle": None,
        "pending_review": False,
        "graduated": False,
        "last_rated_at": None,
    }
    entry.update(_snapshot_fields(word, now))
    return entry


def _fetch_reviewed_words() -> list[dict[str, Any]]:
    """拉取所有「至少复习过一次」的生词。

    只有复习过的词才进队列：刚收藏、还没看过的词没有记忆曲线可排。
    """
    words: list[dict[str, Any]] = []
    offset = 0

    while len(words) < MAX_QUEUE_SIZE and offset < MAX_QUEUE_SIZE:
        limit = min(VOCABULARY_PAGE_SIZE, MAX_QUEUE_SIZE - len(words))
        page = list_reviewed_words(limit=limit, offset=offset)
        if not page:
            break

        words.extend(page)
        offset += len(page)

        if len(page) < limit:
            break

    return words[:MAX_QUEUE_SIZE]


def sync_review_queue() -> dict[str, Any]:
    """用生词本对齐复习队列。

    新词条从阶梯 0 开始、立刻到期；已有词条保留调度状态，只刷新快照字段。
    """
    now = _now_ms()
    schedule = load_schedule()

    words = _fetch_reviewed_words()
    seen_ids = {word["id"] for word in words}

    entries: dict[str, dict[str, Any]] = {}
    added = 0

    for word in words:
        existing = schedule.get(word["id"])
        if existing:
            merged = dict(existing)
            merged.update(_snapshot_fields(word, now))
            entries[word["id"]] = merged
        else:
            entries[word["id"]] = _create_entry(word, now)
            added += 1

    # 生词本里已经没有的词，复习队列里也该消失。
    removed_ids = [word_id for word_id in schedule if word_id not in seen_ids]
    removed = len(removed_ids)

    entries = prune_stale_entries(entries, now)
    upsert_schedule_entries(list(entries.values()))
    if removed_ids:
        delete_schedule_entries(removed_ids)

    _write_last_sync_at(now)

    log.info(
        "复习队列已同步：+%d -%d，合计 %d", added, removed, len(entries)
    )
    return {
        "added": added,
        "removed": removed,
        "total": len(entries),
        "last_sync_at": now,
    }


def _write_last_sync_at(now: int) -> None:
    """记录上次同步时间。"""
    from app.review.store import _get_conn  # 局部导入，避免循环依赖

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO review_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("last_sync_at", str(now)),
        )
        conn.commit()


def get_last_sync_at() -> Optional[int]:
    """上次同步时间（毫秒时间戳）。"""
    from app.review.store import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM review_settings WHERE key = ?", ("last_sync_at",)
        ).fetchone()

    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


# ── 调度 ────────────────────────────────────────────────────────────


def run_review_tick(now: Optional[int] = None) -> dict[str, Any]:
    """跑一轮调度：把今天到点的词合成**一条** Windows 通知推出去。

    返回 ``{"pushed", "words", "failed", "deferred", "stopped_because"}``：
    ``pushed`` 是通知条数（每天最多 1 条），``words`` 是这批覆盖的词数。

    - 免打扰时段内**不发通知、也不改词的到期时间**——唤醒点由
      ``get_next_wakeup_at`` 推到窗口结束，用户自己打开复习页面照常能复习；
    - 今天的提醒额度（``max_per_day``）用完、或今天已经提醒过之后，当天
      剩余的到期词整队推迟到今天 23:59:59：既不会立刻再次到期把调度器
      变成空转，会话里又仍然算"今天到期"，随时能手动复习；
    - 同一天已经提醒过就**不再发第二条**：晚些到期的词进当天会话可见，
      不再叮第二次；
    - 推送失败按指数退避且不占阶梯位置，也不计当天额度，当天还会再试。
    """
    now = now if now is not None else _now_ms()
    now_dt = datetime.fromtimestamp(now / 1000)

    outcome: dict[str, Any] = {
        "pushed": 0,
        "words": 0,
        "failed": 0,
        "deferred": 0,
        "stopped_because": None,
    }

    config = load_config()
    if not config.get("enabled"):
        outcome["stopped_because"] = "disabled"
        return outcome

    schedule = load_schedule()
    stats = prune_stats(load_stats(), now_dt)
    today_key = get_day_key(now_dt)
    stats.setdefault(today_key, {"total": 0, "per_word": {}})

    quiet_hours = config.get("quiet_hours", {})
    due = pick_due_entries(schedule, now, MAX_DEFERRALS_PER_TICK)
    dirty = False

    if is_within_quiet_hours(now_dt, quiet_hours):
        # 免打扰只挡通知：不动到期时间，唤醒点自然会跳到窗口结束。
        outcome["deferred"] = len(due)
        outcome["stopped_because"] = "quietHours"

    else:
        today_total = int(stats[today_key]["total"])
        already_notified = today_total > 0
        remaining = int(config.get("max_per_day", 8)) - today_total

        if remaining <= 0:
            # 今天的额度用完了：剩余的到期词整队推迟到今天结束，明天零点后
            # 优先补上；推迟到今天结束而不是明天零点，是为了让会话里它们
            # 仍然算"今天到期"，同时不会立刻再次到期把调度器变成空转。
            end_of_today = get_end_of_day_ms(now)
            for entry in due:
                entry["next_due_at"] = end_of_today
            outcome["deferred"] = len(due)
            outcome["stopped_because"] = "dailyCap"
            dirty = True

        elif already_notified:
            # 今天已经提醒过了：不再发第二条，晚些到期的词进当天会话可见。
            end_of_today = get_end_of_day_ms(now)
            for entry in due:
                entry["next_due_at"] = end_of_today
            outcome["deferred"] = len(due)
            outcome["stopped_because"] = "alreadyNotified"
            dirty = True

        elif not due:
            outcome["stopped_because"] = "nothingDue"

        else:
            batch = due[:remaining]
            overflow = due[remaining:]
            open_url = REVIEW_PAGE_URL

            try:
                delivered = send_batch_notification(
                    count=len(batch),
                    words=[str(entry.get("word", "")) for entry in batch],
                    launch_url=open_url,
                )
                if not delivered:
                    raise RuntimeError("Windows 通知发送失败")

                for candidate in batch:
                    advanced = advance_stage(candidate, "auto", now)
                    candidate.update(
                        apply_advanced_stage(
                            candidate,
                            advanced,
                            str(config.get("graduated_action", "stop")),
                            now,
                        )
                    )
                    candidate["last_notified_at"] = now
                    candidate["push_count"] = int(candidate.get("push_count", 0)) + 1
                    candidate["failure_count"] = 0
                    # 已推送但还没评分：复习会话按这个标记往下走。
                    candidate["pending_review"] = True

                    word_id = candidate["word_id"]
                    stats[today_key]["total"] += 1
                    per_word = stats[today_key]["per_word"]
                    per_word[word_id] = int(per_word.get(word_id, 0)) + 1

                outcome["pushed"] = 1
                outcome["words"] = len(batch)
                dirty = True

            except Exception as exc:  # noqa: BLE001
                # 推送失败不能吃掉这个词在阶梯上的位置，也不占当天额度。
                for candidate in batch:
                    failure_count = int(candidate.get("failure_count", 0)) + 1
                    candidate["failure_count"] = failure_count
                    candidate["next_due_at"] = now + get_backoff_ms(failure_count - 1)
                outcome["failed"] = len(batch)
                dirty = True
                log.warning("推送复习提醒失败（%d 个词）：%s", len(batch), exc)

            else:
                # 通知成功了，但今天额度也正好用完：超额的词推迟到今天结束，
                # 明天零点后优先补上。
                if overflow:
                    end_of_today = get_end_of_day_ms(now)
                    for entry in overflow:
                        entry["next_due_at"] = end_of_today
                    outcome["deferred"] = len(overflow)

    if (
        outcome["stopped_because"] is None
        and outcome["pushed"] == 0
        and outcome["deferred"] == 0
        and outcome["failed"] == 0
    ):
        outcome["stopped_because"] = "nothingDue"

    if dirty:
        # 什么都没变就别重写整张队列表——免打扰时段里调度器每 10 分钟
        # 会空转一轮，不差这一笔也无谓多这一笔。
        upsert_schedule_entries(list(schedule.values()))
        save_stats(stats)

    log.info("复习调度结束：%s", outcome)
    return outcome


# ── 复习会话 ────────────────────────────────────────────────────────


def _compose_session(
    schedule: Mapping[str, dict[str, Any]],
    config: Mapping[str, Any],
    stats: Mapping[str, dict[str, Any]],
    now: int,
    mode: str,
) -> tuple[list[dict[str, Any]], set[str], dict[str, int]]:
    """按模式挑出本次会话的词条（不发通知、不碰数据库）。

    - ``auto``（通知点进来 / 打开页面）：上次提醒还没评分的词 + 今天到期
      且今天没复习过的词，受今天剩余额度限制——"今天要复习的量"；
    - ``manual``（「立即复习」）：同样这两类，但**不受额度与免打扰限制**；
      不足一天配额时用即将到期的词补齐，补齐的打 ``early`` 标记。

    队列由页面自己拿着走，服务端不存会话状态：刷新页面、开两个标签页都
    不会把队列走丢或串台。
    """
    now_dt = datetime.fromtimestamp(now / 1000)
    day_start = get_start_of_day_ms(now)
    end_of_today = get_end_of_day_ms(now)

    today_notified = int(stats.get(get_day_key(now_dt), {}).get("total", 0))
    max_per_day = max(1, int(config.get("max_per_day", 8)))
    remaining = max(0, max_per_day - today_notified)

    pending = pick_pending_entries(schedule)
    pending_ids = {entry["word_id"] for entry in pending}

    # 会话看"今天到期"：被配额推迟到今天 23:59:59 的词仍然算今天的量。
    due = pick_due_by(
        schedule,
        end_of_today,
        MAX_QUEUE_SIZE,
        day_start_ms=day_start,
        exclude_ids=pending_ids,
    )

    early_ids: set[str] = set()
    if mode == "manual":
        chosen = pending + due
        if len(chosen) < max_per_day:
            fill = pick_upcoming_entries(
                schedule,
                now,
                max_per_day - len(chosen),
                day_start_ms=day_start,
                exclude_ids={entry["word_id"] for entry in chosen},
            )
            early_ids = {entry["word_id"] for entry in fill}
            chosen += fill
    else:
        chosen = pending + due[:remaining]

    return (
        chosen,
        early_ids,
        {"notified": today_notified, "quota": max_per_day, "remaining": remaining},
    )


def build_session(mode: str = "auto", now: Optional[int] = None) -> dict[str, Any]:
    """组装一次复习会话（页面「立即复习」和通知点击都走这里）。

    ``now`` 可注入（测试用）；不传用当前时间。
    """
    if mode not in ("auto", "manual"):
        mode = "auto"

    config = load_config()
    now = now if now is not None else _now_ms()
    schedule = load_schedule()
    stats = prune_stats(load_stats(), datetime.fromtimestamp(now / 1000))
    day_start = get_start_of_day_ms(now)

    chosen, early_ids, today = _compose_session(schedule, config, stats, now, mode)

    words = [
        {
            "word_id": entry["word_id"],
            "word": entry.get("word", ""),
            "early": entry["word_id"] in early_ids,
        }
        for entry in chosen
    ]
    first_card = build_card_data(chosen[0], len(chosen)) if chosen else None

    if chosen:
        state, empty_reason = "ready", None
    elif not schedule:
        state, empty_reason = "empty", "noWords"
    elif not any(not e.get("graduated") for e in schedule.values()):
        state, empty_reason = "empty", "allGraduated"
    else:
        state, empty_reason = "empty", "nothingDue"

    return {
        "mode": mode,
        "state": state,
        "empty_reason": empty_reason,
        "words": words,
        "first_card": first_card,
        "today": {**today, "rated": count_rated_today(schedule, day_start)},
        "next_due_at": get_next_due_at(schedule),
    }


# ── 复习卡片 ────────────────────────────────────────────────────────


def build_card_data(
    entry: Mapping[str, Any], pending_count: int
) -> dict[str, Any]:
    """组装一张复习卡片。

    释义来自词典库；查不到就留空——词典挂了不该让整张卡片打不开。
    """
    word = str(entry.get("word", ""))
    definition: Optional[dict[str, Any]] = None
    try:
        definition = query_word(word.lower())
    except Exception as exc:  # noqa: BLE001
        log.warning("查询词典释义失败（%s）：%s", word, exc)

    return {
        "word_id": entry.get("word_id"),
        "word": word,
        "translation": entry.get("translation", ""),
        "context_text": entry.get("context_text"),
        "source_language": entry.get("source_language"),
        "star": int(entry.get("star", 1) or 1),
        "stage": int(entry.get("stage", 0) or 0),
        "total_stages": len(SRS_INTERVALS_MINUTES),
        "example": entry.get("last_example") or "",
        "sentence_chinese": entry.get("last_sentence_chinese") or "",
        "collocations": entry.get("last_collocations") or [],
        "last_notified_at": entry.get("last_notified_at"),
        "definition": definition,
        "pending_count": pending_count,
    }


def get_card(word_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """取一张复习卡片。

    传 ``word_id`` 就取指定的那个词（会话队列、旧通知深链都是这种情况）；
    不传就返回下一个待评分的词（兼容旧行为，新页面走会话接口）。
    """
    schedule = load_schedule()

    if word_id:
        entry = schedule.get(word_id)
    else:
        entry = pick_next_pending_entry(schedule)

    if not entry:
        return None

    return build_card_data(entry, count_pending_entries(schedule))


def rate_word(
    word_id: str,
    kind: str,
    next_word_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """记录一次评分（``remembered`` / ``forgotten``），并返回下一张卡片。

    评分和取下一张卡片放在一次调用里：复习是「看 → 判断 → 下一张」的紧凑循环，
    分成两次往返会在每张卡片之间插进一段可见的加载态。``next_word_id`` 由
    页面手里的会话队列提供——队列归页面，服务端不存会话状态。
    """
    if kind not in ("remembered", "forgotten"):
        raise ValueError("评分只能是 remembered 或 forgotten")

    config = load_config()
    now = _now_ms()

    entry = get_schedule_entry(word_id)
    if not entry:
        return None

    advanced = advance_stage(entry, kind, now)
    updated = apply_advanced_stage(
        entry, advanced, str(config.get("graduated_action", "stop")), now
    )
    updated["pending_review"] = False
    updated["last_rated_at"] = now
    upsert_schedule_entries([updated])

    log.info(
        "「%s」标记为 %s，阶梯前进到 %s",
        entry.get("word"),
        kind,
        updated.get("stage"),
    )

    if next_word_id and next_word_id != word_id:
        return get_card(next_word_id)
    return None


def refresh_example(word_id: str) -> Optional[dict[str, Any]]:
    """按需生成（并缓存）一个词的例句。

    例句不再在推送时批量生成——一批可能几十个词，会把调度轮次阻塞几分钟；
    改成卡片渲染后由页面异步来要。任何一步失败都只影响例句本身：复习照常
    进行，最差退回收藏时的上下文。
    """
    config = load_config()
    entry = get_schedule_entry(word_id)
    if not entry:
        return None

    example = build_example(entry, config)

    updated = dict(entry)
    updated["last_example"] = example.get("sentence") or None
    updated["last_sentence_chinese"] = example.get("sentence_chinese") or None
    updated["last_collocations"] = example.get("collocations") or []
    updated["last_angle"] = example.get("angle")
    upsert_schedule_entries([updated])

    return {
        "word_id": word_id,
        "example": example.get("sentence") or "",
        "sentence_chinese": example.get("sentence_chinese") or "",
        "collocations": example.get("collocations") or [],
        "source": example.get("source", "bare"),
    }


def clear_queue() -> None:
    """清空复习队列。"""
    clear_schedule()


# ── 状态 ────────────────────────────────────────────────────────────


def count_rated_today(
    entries: Mapping[str, Mapping[str, Any]], day_start_ms: int
) -> int:
    """今天复习过（评过分）的词数。"""
    return sum(
        1
        for entry in entries.values()
        if int(entry.get("last_rated_at") or 0) >= day_start_ms
    )


def get_next_due_at(entries: Mapping[str, Mapping[str, Any]]) -> Optional[int]:
    """队列里下一个到期时刻；没有可复习的词时返回 ``None``。"""
    pending = [
        int(entry.get("next_due_at", 0))
        for entry in entries.values()
        if not entry.get("graduated")
    ]
    return min(pending) if pending else None


def _today_notified_at(
    entries: Mapping[str, Mapping[str, Any]], day_start_ms: int
) -> Optional[int]:
    """今天那条提醒的时间（没有则 ``None``）。"""
    stamps = [
        int(entry.get("last_notified_at") or 0)
        for entry in entries.values()
        if int(entry.get("last_notified_at") or 0) >= day_start_ms
    ]
    return max(stamps) if stamps else None


def get_status() -> dict[str, Any]:
    """复习提醒的运行状态（页面上显示的那一排数字）。"""
    config = load_config()
    schedule = load_schedule()
    stats = load_stats()
    now = _now_ms()
    now_dt = datetime.fromtimestamp(now / 1000)
    today_key = get_day_key(now_dt)
    day_start = get_start_of_day_ms(now)

    entries = list(schedule.values())
    pending = [entry for entry in entries if not entry.get("graduated")]

    chosen, _, _ = _compose_session(schedule, config, stats, now, "auto")

    return {
        "enabled": bool(config.get("enabled")),
        "queue_size": len(pending),
        "graduated_count": len(entries) - len(pending),
        # 现在点「立即复习」/ 打开页面能直接复习多少个词
        "due_count": len(chosen),
        "pending_count": count_pending_entries(schedule),
        "today_rated": count_rated_today(schedule, day_start),
        "today_notified": int(stats.get(today_key, {}).get("total", 0)),
        "today_notified_at": _today_notified_at(schedule, day_start),
        # 词本身的下次到期（SRS）；用户关心的是下面那个 next_notify_at
        "next_due_at": get_next_due_at(schedule),
        # 下一次真会响提醒的时刻（叠加免打扰 + 当天已提醒/额度闸门）
        "next_notify_at": get_next_notify_at(schedule, config, stats, now),
        "last_sync_at": get_last_sync_at(),
        "notification_available": is_available(),
        "config": config,
    }
