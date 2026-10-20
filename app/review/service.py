"""复习提醒的业务逻辑

把扩展里 ``review-scheduler.ts`` / ``queue-sync.ts`` / ``review-handlers.ts``
的职责收拢到这里：同步队列、跑一轮调度、记录评分、组装复习卡片。

扩展原来把这些状态放在 ``chrome.storage.local``，因为「后端不知道这个词上次
是什么时候被推的」。现在后端自己就是数据的源头，状态直接落在 ``user.db``。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import quote

from app.dictionary import query_word
from app.review.constants import (
    DEFAULT_OPEN_URL_TEMPLATE,
    MAX_DEFERRALS_PER_TICK,
    MAX_PUSHES_PER_TICK,
    MAX_QUEUE_SIZE,
    SRS_INTERVALS_MINUTES,
    VOCABULARY_PAGE_SIZE,
)
from app.review.example import build_example
from app.review.notifier import is_available, send_review_notification
from app.review.schedule import (
    advance_stage,
    apply_advanced_stage,
    count_pending_entries,
    get_backoff_ms,
    get_day_key,
    get_next_allowed_timestamp,
    get_start_of_next_day,
    is_within_quiet_hours,
    pick_due_entries,
    pick_next_pending_entry,
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


def build_open_url(config: Mapping[str, Any], word_id: Optional[str] = None) -> str:
    """通知点击后要打开的地址。

    ``open_url_template`` 里的 ``{word_id}`` 会被替换成被提醒的生词 ID；
    没有具体词时（测试通知、首页入口）就把这个参数整个去掉。
    """
    template = str(config.get("open_url_template") or DEFAULT_OPEN_URL_TEMPLATE).strip()
    if not template:
        template = DEFAULT_OPEN_URL_TEMPLATE

    if word_id:
        return template.replace("{word_id}", quote(word_id))

    url = template.replace("{word_id}", "")
    # 空参数没有意义：`?word=` → `?`，末尾的 `?` / `&` 也不要留
    url = url.replace("?word=", "?").replace("&word=", "&")
    if url.endswith(("?", "&")):
        url = url[:-1]
    return url


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
    """跑一轮调度：把到点的词推送成 Windows 通知。

    返回 ``{"pushed", "failed", "deferred", "stopped_because"}``。
    """
    now = now if now is not None else _now_ms()
    now_dt = datetime.fromtimestamp(now / 1000)

    outcome: dict[str, Any] = {
        "pushed": 0,
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
    due = pick_due_entries(
        schedule, now, MAX_PUSHES_PER_TICK + MAX_DEFERRALS_PER_TICK
    )

    if is_within_quiet_hours(now_dt, quiet_hours):
        # 免打扰与每日配额都是全局闸门：整队一起推迟，而不是一条一条慢慢放。
        defer_to = get_next_allowed_timestamp(now_dt, quiet_hours)
        for entry in due:
            entry["next_due_at"] = defer_to
        outcome["deferred"] = len(due)
        outcome["stopped_because"] = "quietHours"

    elif stats[today_key]["total"] >= int(config.get("max_per_day", 8)):
        defer_to = get_start_of_next_day(now_dt)
        for entry in due:
            entry["next_due_at"] = defer_to
        outcome["deferred"] = len(due)
        outcome["stopped_because"] = "dailyCap"

    else:
        tomorrow_at = get_start_of_next_day(now_dt)
        max_per_word = int(config.get("max_per_word_per_day", 3))
        pushed_this_tick = 0

        for candidate in due:
            if pushed_this_tick >= MAX_PUSHES_PER_TICK:
                break

            word_id = candidate["word_id"]
            per_word = stats[today_key]["per_word"]
            if int(per_word.get(word_id, 0)) >= max_per_word:
                candidate["next_due_at"] = tomorrow_at
                outcome["deferred"] += 1
                # 单词配额这个闸门没法从计数里看出来，必须单独标记。
                if outcome["stopped_because"] is None:
                    outcome["stopped_because"] = "perWordCap"
                continue

            example = build_example(candidate, config)
            open_url = build_open_url(config, word_id)

            try:
                delivered = send_review_notification(
                    word=str(candidate.get("word", "")),
                    translation=str(candidate.get("translation", "")),
                    sentence=str(example.get("sentence", "")),
                    launch_url=open_url,
                )
                if not delivered:
                    raise RuntimeError("Windows 通知发送失败")

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
                candidate["last_example"] = (
                    example.get("sentence") or candidate.get("last_example")
                )
                candidate["last_sentence_chinese"] = (
                    example.get("sentence_chinese")
                    or candidate.get("last_sentence_chinese")
                )
                candidate["last_collocations"] = (
                    example.get("collocations") or candidate.get("last_collocations") or []
                )
                candidate["last_angle"] = example.get("angle")
                # 已推送但还没评分：复习卡片按这个标记往下走。
                candidate["pending_review"] = True

                stats[today_key]["total"] += 1
                per_word[word_id] = int(per_word.get(word_id, 0)) + 1
                pushed_this_tick += 1
                outcome["pushed"] += 1

            except Exception as exc:  # noqa: BLE001
                # 推送失败不能吃掉这个词在阶梯上的位置。
                failure_count = int(candidate.get("failure_count", 0)) + 1
                candidate["failure_count"] = failure_count
                candidate["next_due_at"] = now + get_backoff_ms(failure_count - 1)
                outcome["failed"] += 1
                log.warning("推送复习提醒失败（%s）：%s", candidate.get("word"), exc)

    if (
        outcome["stopped_because"] is None
        and outcome["pushed"] == 0
        and outcome["deferred"] == 0
        and outcome["failed"] == 0
    ):
        outcome["stopped_because"] = "nothingDue"

    upsert_schedule_entries(list(schedule.values()))
    save_stats(stats)

    log.info("复习调度结束：%s", outcome)
    return outcome


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
    """取一张待复习的卡片。

    传 ``word_id`` 就取指定的那个词（通知里点进来的就是这种情况）；
    不传就接着上一次的会话往下走。
    """
    schedule = load_schedule()

    if word_id:
        entry = schedule.get(word_id)
    else:
        entry = pick_next_pending_entry(schedule)

    if not entry:
        return None

    return build_card_data(entry, count_pending_entries(schedule))


def rate_word(word_id: str, kind: str) -> Optional[dict[str, Any]]:
    """记录一次评分（``remembered`` / ``forgotten``），并返回下一张卡片。

    评分和取下一张卡片放在一次调用里：复习是「看 → 判断 → 下一张」的紧凑循环，
    分成两次往返会在每张卡片之间插进一段可见的加载态。
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
    upsert_schedule_entries([updated])

    log.info(
        "「%s」标记为 %s，阶梯前进到 %s",
        entry.get("word"),
        kind,
        updated.get("stage"),
    )

    schedule = load_schedule()
    next_entry = pick_next_pending_entry(schedule, exclude_word_id=word_id)
    if not next_entry:
        return None

    return build_card_data(next_entry, count_pending_entries(schedule))


def clear_queue() -> None:
    """清空复习队列。"""
    clear_schedule()


# ── 状态 ────────────────────────────────────────────────────────────


def get_status() -> dict[str, Any]:
    """复习提醒的运行状态（页面上显示的那一排数字）。"""
    config = load_config()
    schedule = load_schedule()
    stats = load_stats()
    today_key = get_day_key(datetime.now())

    entries = list(schedule.values())
    pending = [entry for entry in entries if not entry.get("graduated")]
    next_due_at = (
        min(int(entry.get("next_due_at", 0)) for entry in pending) if pending else None
    )

    return {
        "enabled": bool(config.get("enabled")),
        "queue_size": len(pending),
        "graduated_count": len(entries) - len(pending),
        "today_count": int(stats.get(today_key, {}).get("total", 0)),
        "pending_count": count_pending_entries(schedule),
        "next_due_at": next_due_at,
        "last_sync_at": get_last_sync_at(),
        "notification_available": is_available(),
        "config": config,
    }
