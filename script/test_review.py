"""复习提醒模块测试

覆盖：

1. 艾宾浩斯阶梯推进（auto / remembered / forgotten）
2. 阶梯走完后的两种处置（stop / restart）
3. 推送失败退避
4. 到期词的挑选与排序（此刻到期 / 今天到期 / 即将到期）
5. 免打扰时段（跨零点、当天窗口、关闭、起止相同）
6. 日历工具函数、「下次该唤醒」与「下次提醒」的计算
7. 配置与队列的落库（临时库，不碰真实 user.db；含老库补列迁移）
8. 队列同步（只收「复习过一次」的生词）
9. 一轮调度：每日一次批量提醒、每日配额、免打扰只挡通知、当天不重复提醒、
   失败退避
10. 复习会话：auto 受剩余额度限制、manual 不受限 + 提前复习补齐（含今天全复习完
    时 manual 仍凑得出一组，「再来一组」不空转）
11. 评分（记得 / 忘了，带 next_word_id）与例句按需生成（含固定搭配的中文意思）
12. LLM 请求头（OpenRouter 归属头）、代理环境变量与 NO_PROXY 坑、REST 接口与复习页面（no-cache + 例句预加载）

运行: uv run python -X utf8 script/test_review.py
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 测试期间不让 GoldenDict 监听随主 app 自动启动
os.environ["MDX_WATCH_AUTOSTART"] = "0"

import app.review.schedule as schedule  # noqa: E402
import app.review.service as service  # noqa: E402
import app.review.store as store  # noqa: E402
import app.vocabulary as vocabulary  # noqa: E402
from app.review.constants import REVIEW_PAGE_URL, SRS_INTERVALS_MINUTES  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  ✘ {name} {detail}")


# ── 夹具 ──────────────────────────────────────────────────────────────

NOW = int(datetime(2026, 3, 10, 12, 0, 0).timestamp() * 1000)

QUIET_OFF_MIDNIGHT = {"enabled": False, "start": "23:00", "end": "08:00"}
QUIET_NIGHT = {"enabled": True, "start": "23:00", "end": "08:00"}
QUIET_DAYTIME = {"enabled": True, "start": "08:00", "end": "23:00"}


def make_entry(**overrides) -> dict:
    entry = {
        "word_id": "w1",
        "word": "ephemeral",
        "translation": "adj. 短暂的",
        "star": 3,
        "stage": 0,
        "next_due_at": NOW,
        "last_notified_at": None,
        "push_count": 0,
        "failure_count": 0,
        "last_rated_at": None,
        "synced_at": NOW,
    }
    entry.update(overrides)
    return entry


# ── 1. 阶梯推进 ───────────────────────────────────────────────────────


def test_advance_stage() -> None:
    print("\n[1] 阶梯推进")

    advanced = schedule.advance_stage(make_entry(stage=2), "auto", NOW)
    check(
        "auto 走完当前一级再升一级",
        advanced.next_due_at == NOW + SRS_INTERVALS_MINUTES[2] * 60_000
        and advanced.stage == 3
        and advanced.graduated is False,
    )

    last = len(SRS_INTERVALS_MINUTES) - 1
    advanced = schedule.advance_stage(make_entry(stage=last), "auto", NOW)
    check(
        "阶梯走完即毕业",
        advanced.next_due_at == NOW + SRS_INTERVALS_MINUTES[last] * 60_000
        and advanced.graduated is True,
    )

    advanced = schedule.advance_stage(make_entry(stage=1), "remembered", NOW)
    check(
        "记得时跨一级",
        advanced.stage == 3
        and advanced.next_due_at == NOW + SRS_INTERVALS_MINUTES[2] * 60_000
        and advanced.graduated is False,
    )

    dropped = schedule.advance_stage(make_entry(stage=3), "forgotten", NOW)
    check(
        "忘了时退一级",
        dropped.stage == 2
        and dropped.next_due_at == NOW + SRS_INTERVALS_MINUTES[2] * 60_000,
    )

    floored = schedule.advance_stage(make_entry(stage=0), "forgotten", NOW)
    check("忘了不会掉到阶梯以下", floored.stage == 0 and floored.graduated is False)


def test_apply_advanced_stage() -> None:
    print("\n[2] 毕业后的处置")

    last = len(SRS_INTERVALS_MINUTES) - 1
    advanced = schedule.advance_stage(make_entry(stage=last), "auto", NOW)

    entry = schedule.apply_advanced_stage(
        make_entry(stage=last), advanced, "stop", NOW
    )
    check("默认停止推送", entry["graduated"] is True)

    entry = schedule.apply_advanced_stage(
        make_entry(stage=last, push_count=8), advanced, "restart", NOW
    )
    check(
        "restart 从头再来",
        entry["graduated"] is False
        and entry["stage"] == 0
        and entry["push_count"] == 0
        and entry["next_due_at"] == NOW + SRS_INTERVALS_MINUTES[0] * 60_000,
    )


def test_backoff_and_interval() -> None:
    print("\n[3] 退避与间隔")

    check("退避逐次翻倍", schedule.get_backoff_ms(0) == 2 * 60_000)
    check("退避翻倍", schedule.get_backoff_ms(1) == 4 * 60_000)
    check("退避继续翻倍", schedule.get_backoff_ms(2) == 8 * 60_000)
    check("退避封顶", schedule.get_backoff_ms(10) == 30 * 60_000)
    check("退避封顶（超大失败次数）", schedule.get_backoff_ms(50) == 30 * 60_000)

    check("间隔 0 = 5 分钟", schedule.get_interval_ms(0) == 5 * 60_000)
    last = len(SRS_INTERVALS_MINUTES) - 1
    check("阶梯共 9 级", last == 8, last)
    check(
        "间隔 7 = 8 天",
        schedule.get_interval_ms(7) == 8 * 24 * 60 * 60 * 1000,
    )
    check(
        "最后一级 = 12 天",
        schedule.get_interval_ms(last) == 12 * 24 * 60 * 60 * 1000,
    )
    check(
        "越界阶段夹到最后一级",
        schedule.get_interval_ms(99) == schedule.get_interval_ms(last),
    )


def test_pick_due_entries() -> None:
    print("\n[4] 到期词挑选")

    entries = {
        "due": make_entry(word_id="due", next_due_at=NOW - 1),
        "later": make_entry(word_id="later", next_due_at=NOW + 60_000),
        "done": make_entry(word_id="done", next_due_at=NOW - 1, graduated=True),
    }
    picked = [e["word_id"] for e in schedule.pick_due_entries(entries, NOW, 10)]
    check("只挑到期且未毕业的", picked == ["due"], picked)

    entries = {
        "hard": make_entry(word_id="hard", star=1, push_count=5),
        "medium": make_entry(word_id="medium", star=3, push_count=1),
        "easySameStar": make_entry(word_id="easySameStar", star=3, push_count=9),
    }
    picked = [e["word_id"] for e in schedule.pick_due_entries(entries, NOW, 10)]
    check(
        "同到期时间按星级、推送次数排序",
        picked == ["hard", "medium", "easySameStar"],
        picked,
    )

    entries = {
        f"w{i}": make_entry(word_id=f"w{i}", next_due_at=NOW - i) for i in range(10)
    }
    check("遵守数量上限", len(schedule.pick_due_entries(entries, NOW, 3)) == 3)

    # 调度器直接改这些对象——改完要能落库
    entries = {"a": make_entry(word_id="a", next_due_at=NOW - 1)}
    picked = schedule.pick_due_entries(entries, NOW, 10)
    picked[0]["next_due_at"] = 123
    check("返回的是队列原对象（改动可直接落库）", entries["a"]["next_due_at"] == 123)


def test_session_pickers() -> None:
    print("\n[5] 会话挑选（今天到期 / 即将到期 / 待评分）")

    day_start = schedule.get_start_of_day_ms(NOW)
    end_of_today = schedule.get_end_of_day_ms(NOW)

    check("当天零点", day_start == int(datetime(2026, 3, 10).timestamp() * 1000))
    check(
        "当天结束是 23:59:59.999",
        end_of_today == day_start + 24 * 60 * 60 * 1000 - 1,
    )

    entries = {
        "now": make_entry(word_id="now", next_due_at=NOW - 1),
        "deferred": make_entry(word_id="deferred", next_due_at=end_of_today),
        "tomorrow": make_entry(word_id="tomorrow", next_due_at=end_of_today + 60_000),
        "rated": make_entry(word_id="rated", next_due_at=NOW, last_rated_at=NOW),
        "done": make_entry(word_id="done", next_due_at=NOW, graduated=True),
    }

    picked = [e["word_id"] for e in schedule.pick_due_by(entries, end_of_today, 10)]
    check(
        "今天到期包含被推迟到今天的词（不传 day_start 不过滤已复习）",
        picked == ["now", "rated", "deferred"],
        picked,
    )

    picked = [
        e["word_id"]
        for e in schedule.pick_due_by(
            entries, end_of_today, 10, day_start_ms=day_start
        )
    ]
    check("排除今天已复习的词", picked == ["now", "deferred"], picked)

    picked = [
        e["word_id"]
        for e in schedule.pick_due_by(
            entries, end_of_today, 10, day_start_ms=day_start, exclude_ids=["now"]
        )
    ]
    check("exclude_ids 去重", picked == ["deferred"], picked)

    picked = [
        e["word_id"]
        for e in schedule.pick_upcoming_entries(entries, NOW, 10)
    ]
    check("即将到期只含此刻还没到期的", picked == ["deferred", "tomorrow"], picked)

    entries = {
        "a": make_entry(word_id="a", pending_review=True, last_notified_at=NOW - 10),
        "b": make_entry(word_id="b", pending_review=True, last_notified_at=NOW),
        "c": make_entry(word_id="c"),
    }
    picked = [e["word_id"] for e in schedule.pick_pending_entries(entries)]
    check("待评分按提醒时间排序", picked == ["a", "b"], picked)
    check(
        "pick_next_pending_entry 可排除当前词",
        schedule.pick_next_pending_entry(entries, "a")["word_id"] == "b",
    )


# ── 2. 免打扰时段 ─────────────────────────────────────────────────────


def test_quiet_hours() -> None:
    print("\n[6] 免打扰时段")

    check("解析 00:00", schedule.parse_time_to_minutes("00:00") == 0)
    check("解析 08:30", schedule.parse_time_to_minutes("08:30") == 510)
    check("解析 23:59", schedule.parse_time_to_minutes("23:59") == 1439)
    check("非法时间按 0 处理", schedule.parse_time_to_minutes("bogus") == 0)

    def at(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm)

    check(
        "跨零点的窗口是连续的",
        not schedule.is_within_quiet_hours(at(2026, 3, 10, 22, 59), QUIET_NIGHT)
        and schedule.is_within_quiet_hours(at(2026, 3, 10, 23, 0), QUIET_NIGHT)
        and schedule.is_within_quiet_hours(at(2026, 3, 10, 23, 59), QUIET_NIGHT)
        and schedule.is_within_quiet_hours(at(2026, 3, 11, 0, 30), QUIET_NIGHT)
        and schedule.is_within_quiet_hours(at(2026, 3, 11, 7, 59), QUIET_NIGHT)
        and not schedule.is_within_quiet_hours(at(2026, 3, 11, 8, 0), QUIET_NIGHT),
    )

    check(
        "当天内的窗口",
        schedule.is_within_quiet_hours(at(2026, 3, 10, 9, 0), QUIET_DAYTIME)
        and not schedule.is_within_quiet_hours(at(2026, 3, 10, 23, 0), QUIET_DAYTIME),
    )

    check(
        "关闭或起止相同时不生效",
        not schedule.is_within_quiet_hours(at(2026, 3, 10, 23, 30), QUIET_OFF_MIDNIGHT)
        and not schedule.is_within_quiet_hours(
            at(2026, 3, 10, 23, 30),
            {"enabled": True, "start": "08:00", "end": "08:00"},
        ),
    )

    deferred = datetime.fromtimestamp(
        schedule.get_next_allowed_timestamp(at(2026, 3, 10, 23, 30), QUIET_NIGHT) / 1000
    )
    check(
        "推迟到窗口结束（次日 8:00）",
        deferred.day == 11 and deferred.hour == 8 and deferred.minute == 0,
    )

    deferred = datetime.fromtimestamp(
        schedule.get_next_allowed_timestamp(at(2026, 3, 10, 2, 15), QUIET_NIGHT) / 1000
    )
    check(
        "窗口从昨天开始的，推迟到当天 8:00",
        deferred.day == 10 and deferred.hour == 8,
    )

    noon = at(2026, 3, 10, 12, 0)
    check(
        "窗口外保持原时间",
        schedule.get_next_allowed_timestamp(noon, QUIET_NIGHT)
        == int(noon.timestamp() * 1000),
    )


def test_calendar_helpers() -> None:
    print("\n[7] 日历工具")

    check(
        "按本地日历日编号",
        schedule.get_day_key(datetime(2026, 1, 5, 23, 59)) == "2026-01-05"
        and schedule.get_day_key(datetime(2026, 12, 31, 0, 0)) == "2026-12-31",
    )

    nxt = datetime.fromtimestamp(
        schedule.get_start_of_next_day(datetime(2026, 3, 10, 23, 59)) / 1000
    )
    check("次日本地零点", nxt.day == 11 and nxt.hour == 0 and nxt.minute == 0)


def test_next_times() -> None:
    print("\n[8] 唤醒点与下次提醒的时刻")

    config = {"enabled": True, "quiet_hours": QUIET_OFF_MIDNIGHT}

    check("队列为空时没有唤醒点", schedule.get_next_wakeup_at({}, config, NOW) is None)

    graduated = {"a": make_entry(word_id="a", graduated=True)}
    check(
        "全部毕业时没有唤醒点",
        schedule.get_next_wakeup_at(graduated, config, NOW) is None,
    )

    overdue = {"a": make_entry(word_id="a", next_due_at=NOW - 10 * 60_000)}
    check(
        "不会排在过去、也不会比最短间隔更近",
        schedule.get_next_wakeup_at(overdue, config, NOW) >= NOW + 30_000,
    )

    late_night = int(datetime(2026, 3, 10, 23, 30).timestamp() * 1000)
    state = {"a": make_entry(word_id="a", next_due_at=late_night + 60_000)}
    when = schedule.get_next_wakeup_at(
        state, {**config, "quiet_hours": QUIET_NIGHT}, late_night
    )
    woken = datetime.fromtimestamp(when / 1000)
    check("落在免打扰时段的唤醒点被推到窗口结束", woken.day == 11 and woken.hour == 8)

    # ── 下次提醒（get_next_notify_at） ──────────────────────────────
    check(
        "提醒关着 → 永远不响",
        schedule.get_next_notify_at({"a": make_entry()}, {"enabled": False}, {}, NOW)
        is None,
    )
    check(
        "空队列 → 不响",
        schedule.get_next_notify_at({}, config, {}, NOW) is None,
    )
    check(
        "全部毕业 → 不响",
        schedule.get_next_notify_at(graduated, config, {}, NOW) is None,
    )

    # 今天没响过、此刻就到期 → 到期即响（至少 30 秒后）
    when = schedule.get_next_notify_at(overdue, config, {}, NOW)
    check("今天没响过：到期即响", when == NOW + 30_000, when - NOW)

    # 今天已经响过 → 一天只一次，最早明天零点
    when = schedule.get_next_notify_at(
        overdue, config, {"2026-03-10": {"total": 3}}, NOW
    )
    expected = int(datetime(2026, 3, 11).timestamp() * 1000)
    check("今天响过了：顺延到明天零点", when == expected, when)

    # 明天零点落在免打扰时段 → 推到窗口结束（次日 8:00）
    when = schedule.get_next_notify_at(
        overdue, {**config, "quiet_hours": QUIET_NIGHT}, {"2026-03-10": {"total": 3}}, NOW
    )
    expected = int(datetime(2026, 3, 11, 8, 0).timestamp() * 1000)
    check("顺延后落在免打扰 → 推到窗口结束", when == expected, when)

    # 截图实景：配额用完后词被推迟到今天 23:59:59 + 今天已响过 22 次
    # → 那个 23:59 只是占位值，真正下次提醒是明天 08:00
    deferred = {"a": make_entry(word_id="a", next_due_at=schedule.get_end_of_day_ms(NOW))}
    when = schedule.get_next_notify_at(
        deferred,
        {**config, "quiet_hours": QUIET_NIGHT},
        {"2026-03-10": {"total": 22}},
        NOW,
    )
    expected = int(datetime(2026, 3, 11, 8, 0).timestamp() * 1000)
    check("推迟到 23:59 不会真响，下次是明天 08:00", when == expected, when)


# ── 3. 存储层 ─────────────────────────────────────────────────────────


def test_store_roundtrip() -> None:
    print("\n[9] 配置与队列落库")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            config = store.load_config()
            check(
                "默认配置齐全",
                config["enabled"] is False
                and config["max_per_day"] == 8
                and config["quiet_hours"]["start"] == "23:00",
            )
            check(
                "默认配置不再有每词每日上限",
                "max_per_word_per_day" not in config,
            )

            saved = store.save_config({"enabled": True, "max_per_day": 3})
            check(
                "保存后合并其余默认值",
                saved["enabled"] is True
                and saved["max_per_day"] == 3
                and saved["graduated_action"] == "stop",
            )
            check("重新读取仍是保存后的值", store.load_config()["max_per_day"] == 3)

            entry = make_entry(
                word_id="w1",
                next_due_at=NOW,
                last_collocations=[
                    {"text": "reach a verdict", "chinese": "作出裁决"},
                    {"text": "final verdict", "chinese": "最终裁决"},
                ],
                pending_review=True,
                last_rated_at=NOW - 1000,
            )
            store.upsert_schedule_entries([entry])
            loaded = store.get_schedule_entry("w1")
            check(
                "词条能落库并读回",
                loaded is not None
                and loaded["word"] == "ephemeral"
                and loaded["pending_review"] is True
                and loaded["last_collocations"]
                == [
                    {"text": "reach a verdict", "chinese": "作出裁决"},
                    {"text": "final verdict", "chinese": "最终裁决"},
                ],
            )
            check("最近评分时间能读写", loaded["last_rated_at"] == NOW - 1000)

            # 老库里搭配是纯字符串：读出来补齐成统一形状（意思留空）
            store.upsert_schedule_entries(
                [make_entry(word_id="legacy", next_due_at=NOW, last_collocations=["a", "b"])]
            )
            legacy = store.get_schedule_entry("legacy")
            check(
                "老库的纯字符串搭配读成统一形状",
                legacy["last_collocations"]
                == [{"text": "a", "chinese": ""}, {"text": "b", "chinese": ""}],
                legacy["last_collocations"],
            )

            store.save_stats({"2026-03-10": {"total": 3, "per_word": {"w1": 1}}})
            stats = store.load_stats()
            check(
                "每日统计能落库并读回",
                stats["2026-03-10"]["total"] == 3
                and stats["2026-03-10"]["per_word"]["w1"] == 1,
            )

            pruned = store.prune_stats(
                {"2026-01-01": {"total": 9, "per_word": {}}}, datetime(2026, 3, 10)
            )
            check("过期统计被清掉", pruned == {})

            stale = store.prune_stale_entries(
                {"old": make_entry(word_id="old", synced_at=0)}, NOW
            )
            check("太久没同步确认的词条被丢掉", stale == {})

            store.clear_schedule()
            check("清空队列生效", store.load_schedule() == {})


def test_store_migration() -> None:
    print("\n[10] 老库补列迁移")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        # 先按「没有 last_rated_at 列」的老结构建表并塞一行
        old_sql = store._CREATE_SQL.replace(
            "    -- 最近一次评分（复习）的时间，用来算「今天复习过了没有」\n"
            "    last_rated_at       INTEGER,\n",
            "",
        )
        conn = sqlite3.connect(db_path)
        conn.executescript(old_sql)
        conn.execute(
            """
            INSERT INTO review_schedule (word_id, word, translation, stage,
                next_due_at, synced_at)
            VALUES ('old1', 'ephemeral', 'adj. 短暂的', 0, ?, ?)
            """,
            (NOW, NOW),
        )
        conn.commit()
        conn.close()

        with mock.patch.object(store, "DB_FILE", db_path):
            loaded = store.load_schedule()
            check("老库能正常读取", "old1" in loaded, list(loaded))
            check(
                "老库自动补上 last_rated_at（为 None）",
                loaded["old1"]["last_rated_at"] is None,
            )
            store.upsert_schedule_entries(
                [make_entry(word_id="old1", last_rated_at=NOW)]
            )
            check(
                "补列后可正常写入",
                store.get_schedule_entry("old1")["last_rated_at"] == NOW,
            )


# ── 4. 业务层 ─────────────────────────────────────────────────────────


def _make_vocabulary(db_path: Path) -> None:
    """造一份生词库：一个复习过的、一个没复习的。"""
    vocabulary.DB_FILE = db_path
    vocabulary.add_word("ephemeral", "adj. 短暂的")
    reviewed = vocabulary.add_word("monetization", "货币化")
    vocabulary.review_word(reviewed["id"])


def _add_reviewed(word: str, translation: str) -> str:
    """再加一个「复习过一次」的生词，返回它的 ID。"""
    record = vocabulary.add_word(word, translation)
    vocabulary.review_word(record["id"])
    return record["id"]


def _make_due(now: int) -> None:
    """把队列里的词都标记成「此刻到期」。"""
    entries = list(store.load_schedule().values())
    for entry in entries:
        entry["next_due_at"] = now
    store.upsert_schedule_entries(entries)


def _due_ids() -> set:
    return {
        entry["word_id"]
        for entry in store.load_schedule().values()
        if int(entry.get("next_due_at", 0)) <= NOW
    }


def test_sync_queue() -> None:
    print("\n[11] 队列同步")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            result = service.sync_review_queue()

            check("只收复习过的词", result["total"] == 1, result)
            check("新增计数正确", result["added"] == 1, result)

            schedule_state = store.load_schedule()
            entry = next(iter(schedule_state.values()))
            check(
                "新词条从阶梯 0 开始、立刻到期",
                entry["stage"] == 0 and entry["push_count"] == 0,
            )
            check("新词条没有评分记录", entry["last_rated_at"] is None)

            # 再同步一次：不该重复添加
            again = service.sync_review_queue()
            check("重复同步不会重复添加", again["added"] == 0 and again["total"] == 1)


def test_run_tick() -> None:
    print("\n[12] 一轮调度（每日一次批量提醒）")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            _add_reviewed("serendipity", "n. 意外发现珍奇事物的运气")
            _add_reviewed("luminate", "v. 发亮")
            service.sync_review_queue()
            _make_due(NOW)
            service.update_config(
                {
                    "enabled": True,
                    "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
                }
            )

            calls: list[dict] = []

            def fake_notify(count, words, launch_url):
                calls.append(
                    {"count": count, "words": list(words), "launch_url": launch_url}
                )
                return True

            with mock.patch.object(service, "send_batch_notification", fake_notify):
                outcome = service.run_review_tick(now=NOW)
                check("一批只发一条通知", outcome["pushed"] == 1, outcome)
                check("通知覆盖全部到期词", outcome["words"] == 3, outcome)
                check("只调用一次通知", len(calls) == 1, calls)
                check(
                    "通知带上班别与地址",
                    calls[0]["count"] == 3
                    and sorted(calls[0]["words"]) == ["luminate", "monetization", "serendipity"]
                    and calls[0]["launch_url"] == REVIEW_PAGE_URL,
                    calls,
                )

                entries = list(store.load_schedule().values())
                check(
                    "推送后阶梯前进、标记待评分",
                    all(
                        e["stage"] == 1
                        and e["push_count"] == 1
                        and e["pending_review"] is True
                        and e["last_notified_at"] == NOW
                        for e in entries
                    ),
                    entries,
                )
                check(
                    "统计记下这批词数",
                    store.load_stats().get("2026-03-10", {}).get("total") == 3,
                )

                # 免打扰时段：只挡通知，不改到期时间
                _make_due(NOW)
                service.update_config(
                    {"quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"}}
                )
                before = {e["word_id"]: e["next_due_at"] for e in store.load_schedule().values()}
                outcome = service.run_review_tick(now=NOW)
                after = {e["word_id"]: e["next_due_at"] for e in store.load_schedule().values()}
                check(
                    "免打扰时段内不推送",
                    outcome["pushed"] == 0
                    and outcome["stopped_because"] == "quietHours",
                    outcome,
                )
                check("免打扰不改词的到期时间", before == after)
                check("免打扰时段不再发通知", len(calls) == 1, calls)

                # 当天已提醒过：不再发第二条，剩余到期词推迟到今天结束
                service.update_config(
                    {"quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"}}
                )
                _make_due(NOW)
                end_of_today = schedule.get_end_of_day_ms(NOW)
                outcome = service.run_review_tick(now=NOW)
                check(
                    "当天不重复提醒",
                    outcome["pushed"] == 0
                    and outcome["stopped_because"] == "alreadyNotified",
                    outcome,
                )
                check("没有第二条通知", len(calls) == 1, calls)
                check(
                    "剩余词推迟到今天结束（仍算今天到期）",
                    all(
                        e["next_due_at"] == end_of_today
                        for e in store.load_schedule().values()
                    ),
                )

                # 每日配额：一批只推 remaining 个，超额推迟到今天结束
                #（清空队列后重新同步即可，不重复加词）
                store.clear_schedule()
                store.save_stats({})
                service.sync_review_queue()
                _make_due(NOW)
                service.update_config({"max_per_day": 1})
                calls.clear()
                outcome = service.run_review_tick(now=NOW)
                check("配额内一批推完", outcome["pushed"] == 1 and outcome["words"] == 1, outcome)
                check("超额词被推迟", outcome["deferred"] == 2, outcome)
                deferred = [
                    e for e in store.load_schedule().values()
                    if e["next_due_at"] == end_of_today
                ]
                check("推迟到今天结束", len(deferred) == 2, deferred)

                # 额度用完：立刻再跑一轮就是 dailyCap，整队推迟且不推送
                _make_due(NOW)
                outcome = service.run_review_tick(now=NOW)
                check(
                    "额度用完后整队推迟且不推送",
                    outcome["pushed"] == 0 and outcome["stopped_because"] == "dailyCap",
                    outcome,
                )
                check("仍然只有一条通知", len(calls) == 1, calls)

                # 关闭功能
                service.update_config({"enabled": False})
                outcome = service.run_review_tick(now=NOW)
                check(
                    "功能关闭时不推送",
                    outcome["pushed"] == 0 and outcome["stopped_because"] == "disabled",
                    outcome,
                )

                # 推送失败要退避，而不是吃掉阶梯位置
                service.update_config({"enabled": True, "max_per_day": 8})
                store.save_stats({})
                _make_due(NOW)
                stages_before = {
                    e["word_id"]: e["stage"] for e in store.load_schedule().values()
                }

                def failing_notify(count, words, launch_url):
                    raise RuntimeError("通知发送失败")

                with mock.patch.object(
                    service, "send_batch_notification", failing_notify
                ):
                    outcome = service.run_review_tick(now=NOW)
                check(
                    "整批失败计数",
                    outcome["failed"] == 3 and outcome["pushed"] == 0,
                    outcome,
                )
                entries = {e["word_id"]: e for e in store.load_schedule().values()}
                check(
                    "失败时阶梯不动、改为退避",
                    all(
                        entries[wid]["stage"] == stage
                        and entries[wid]["failure_count"] == 1
                        and entries[wid]["next_due_at"] > NOW
                        for wid, stage in stages_before.items()
                    ),
                    entries,
                )
                check(
                    "失败不计入当天额度",
                    store.load_stats().get("2026-03-10", {}).get("total", 0) == 0,
                )

                # 没有到期的词
                store.clear_schedule()
                service.sync_review_queue()
                entries = list(store.load_schedule().values())
                for entry in entries:
                    entry["next_due_at"] = NOW + 60 * 60 * 1000
                store.upsert_schedule_entries(entries)
                with mock.patch.object(
                    service, "send_batch_notification", lambda *a, **k: True
                ):
                    outcome = service.run_review_tick(now=NOW)
                check(
                    "没有到期的词",
                    outcome["pushed"] == 0
                    and outcome["stopped_because"] == "nothingDue",
                    outcome,
                )


def _three_due_words() -> dict:
    """三个今天到期的词（monetization / serendipity / luminate）→ {word_id: word}。"""
    return {e["word_id"]: e["word"] for e in store.load_schedule().values()}


def test_session() -> None:
    print("\n[13] 复习会话")

    # ── 场景一：auto 会话 = 待评分 + 今天到期，评分后即时变少 ──────────
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            _add_reviewed("serendipity", "n. 意外发现珍奇事物的运气")
            _add_reviewed("luminate", "v. 发亮")
            service.sync_review_queue()
            _make_due(NOW)
            service.update_config(
                {
                    "enabled": True,
                    "max_per_day": 8,
                    "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
                }
            )

            with mock.patch.object(
                service, "send_batch_notification", lambda *a, **k: True
            ):
                service.run_review_tick(now=NOW)

            ids = _three_due_words()
            monetization = next(k for k, v in ids.items() if v == "monetization")

            session = service.build_session("auto", now=NOW)
            check("auto 会话包含全部待评分词", len(session["words"]) == 3, session)
            check("auto 会话带第一张卡", session["first_card"]["word"] in ids.values())
            check("auto 会话是 ready", session["state"] == "ready")

            service.rate_word(monetization, "remembered")
            session = service.build_session("auto", now=NOW)
            check("评分后 auto 会话排除该词", len(session["words"]) == 2, session)
            check("今日已复习计数", session["today"]["rated"] == 1, session["today"])

            # 今天复习过的词不再进 auto 会话（即便再次到期）
            _make_due(NOW)
            session = service.build_session("auto", now=NOW)
            check(
                "今天已复习的词不进 auto 会话",
                monetization not in {w["word_id"] for w in session["words"]},
                session,
            )

            # manual：同样不受免打扰影响，把今天到期的词全收
            session = service.build_session("manual", now=NOW)
            check(
                "manual 会话不受限制",
                len(session["words"]) == 2,
                session,
            )

    # ── 场景二：auto 受剩余额度限制，manual 不受 ──────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            _add_reviewed("serendipity", "n. 意外发现珍奇事物的运气")
            _add_reviewed("luminate", "v. 发亮")
            service.sync_review_queue()
            _make_due(NOW)
            service.update_config(
                {
                    "enabled": True,
                    "max_per_day": 8,
                    "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
                }
            )
            # 今天已经提醒过 6 个（只剩 2 个额度），且都没有评分
            store.save_stats({"2026-03-10": {"total": 6, "per_word": {}}})

            auto = service.build_session("auto", now=NOW)
            check(
                "auto 会话受剩余额度限制（8 - 6 = 2）",
                len(auto["words"]) == 2 and auto["today"]["remaining"] == 2,
                auto,
            )
            manual = service.build_session("manual", now=NOW)
            check(
                "manual 会话不受额度限制",
                len(manual["words"]) == 3,
                manual,
            )
            check(
                "manual 里的词都不是提前复习",
                all(not w["early"] for w in manual["words"]),
            )

    # ── 场景三：今天没有到期词时，manual 用即将到期的词补齐 ────────────
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            _add_reviewed("serendipity", "n. 意外发现珍奇事物的运气")
            _add_reviewed("luminate", "v. 发亮")
            service.sync_review_queue()
            entries = list(store.load_schedule().values())
            for entry in entries:
                entry["next_due_at"] = NOW + 24 * 60 * 60 * 1000  # 明天才到期
            store.upsert_schedule_entries(entries)

            auto = service.build_session("auto", now=NOW)
            check(
                "没有到期词时 auto 为空",
                auto["state"] == "empty" and auto["empty_reason"] == "nothingDue",
                auto,
            )
            manual = service.build_session("manual", now=NOW)
            check(
                "manual 用即将到期的词补齐",
                len(manual["words"]) == 3 and manual["state"] == "ready",
                manual,
            )
            check(
                "补齐的词打上提前复习标记",
                all(w["early"] for w in manual["words"]),
                manual,
            )

    # ── 场景五：今天全复习完了，manual 靠即将到期补齐（「再来一组」不能空转）
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            _add_reviewed("serendipity", "n. 意外发现珍奇事物的运气")
            _add_reviewed("luminate", "v. 发亮")
            service.sync_review_queue()

            # 全队今天都评过分、且都改成稍后到期：模拟晚上再点「再来一组」
            due_at = {
                "monetization": NOW + 50 * 60 * 1000,
                "serendipity": NOW + 30 * 60 * 1000,
                "luminate": NOW + 40 * 60 * 1000,
            }
            entries = list(store.load_schedule().values())
            for entry in entries:
                entry["last_rated_at"] = NOW
                entry["next_due_at"] = due_at[entry["word"]]
            store.upsert_schedule_entries(entries)

            auto = service.build_session("auto", now=NOW)
            check(
                "今天全复习完 → auto 为空",
                auto["state"] == "empty" and auto["empty_reason"] == "nothingDue",
                auto,
            )
            manual = service.build_session("manual", now=NOW)
            check(
                "manual 仍能凑出一组（提前复习）",
                manual["state"] == "ready" and len(manual["words"]) == 3,
                manual,
            )
            check(
                "补齐的词都打提前复习标记",
                all(w["early"] for w in manual["words"]),
                manual,
            )
            check(
                "提前复习取最快到期的一批",
                [w["word"] for w in manual["words"]]
                == ["serendipity", "luminate", "monetization"],
                manual,
            )

    # ── 场景四：两种空态 ──────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            service.sync_review_queue()
            store.clear_schedule()
            session = service.build_session("manual", now=NOW)
            check("空队列 → noWords", session["empty_reason"] == "noWords", session)

            service.sync_review_queue()
            entries = list(store.load_schedule().values())
            for entry in entries:
                entry["graduated"] = True
            store.upsert_schedule_entries(entries)
            session = service.build_session("manual", now=NOW)
            check(
                "全部毕业 → allGraduated",
                session["empty_reason"] == "allGraduated",
                session,
            )


def test_rate_and_card() -> None:
    print("\n[14] 评分与卡片")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            _add_reviewed("serendipity", "n. 意外发现珍奇事物的运气")
            service.sync_review_queue()
            _make_due(NOW)
            service.update_config({"enabled": True})

            ids = {e["word_id"]: e["word"] for e in store.load_schedule().values()}
            monetization = next(k for k, v in ids.items() if v == "monetization")
            serendipity = next(k for k, v in ids.items() if v == "serendipity")

            card = service.get_card(monetization)
            check("能取到卡片", card is not None and card["word"] == "monetization")
            check("卡片带上阶梯信息", card["total_stages"] == len(SRS_INTERVALS_MINUTES))

            with mock.patch.object(
                service, "send_batch_notification", lambda *a, **k: True
            ):
                service.run_review_tick(now=NOW)

            card = service.get_card()
            check("推送后能取到待评分卡片", card is not None)

            # 带 next_word_id：一次往返拿到下一张
            next_card = service.rate_word(monetization, "remembered", serendipity)
            check(
                "评分并返回指定的下一张卡片",
                next_card is not None and next_card["word_id"] == serendipity,
                next_card,
            )

            entry = store.get_schedule_entry(monetization)
            check(
                "记得时跨一级、清除待评分、记下评分时间",
                entry["stage"] == 3
                and entry["pending_review"] is False
                and entry["last_rated_at"] is not None,
                entry,
            )

            service.rate_word(monetization, "forgotten")
            entry = store.get_schedule_entry(monetization)
            check("忘了时退一级", entry["stage"] == 2, entry)

            # 不存在的词 / 非法评分
            check("给不存在的词评分返回 None", service.rate_word("nope", "remembered") is None)
            try:
                service.rate_word(monetization, "wrong")
                check("非法评分被拒绝", False)
            except ValueError:
                check("非法评分被拒绝", True)


def test_refresh_example() -> None:
    print("\n[15] 按需生成例句")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            service.sync_review_queue()
            ids = {e["word_id"]: e["word"] for e in store.load_schedule().values()}
            monetization = next(k for k, v in ids.items() if v == "monetization")

            result = service.refresh_example(monetization)
            check("能生成例句", result is not None, result)
            check("没有 LLM 时回落上下文/bare", result["source"] in ("context", "bare"))
            entry = store.get_schedule_entry(monetization)
            check(
                "例句被缓存到词条",
                (entry["last_example"] or "") == result["example"],
                (entry["last_example"], result["example"]),
            )

            check("不存在的词返回 None", service.refresh_example("nope") is None)


def test_collocation_meaning() -> None:
    print("\n[15b] 固定搭配的中文意思")

    from app.review.constants import EXAMPLE_COLLOCATION_LIMIT
    from app.review.example import build_example, normalize_collocations

    check(
        "新形状：搭配带中文意思",
        normalize_collocations([{"text": "reach a verdict", "chinese": "作出裁决"}])
        == [{"text": "reach a verdict", "chinese": "作出裁决"}],
    )
    check(
        "老形状：纯字符串意思留空",
        normalize_collocations(["jury verdict"])
        == [{"text": "jury verdict", "chinese": ""}],
    )
    check(
        "缺 chinese 键也留空",
        normalize_collocations([{"text": "verdict on"}])
        == [{"text": "verdict on", "chinese": ""}],
    )
    check("空搭配被丢掉", normalize_collocations(["", "  ", None]) == [])
    check(
        "超过上限按上限截断",
        len(normalize_collocations([f"c{i}" for i in range(8)]))
        == EXAMPLE_COLLOCATION_LIMIT,
    )
    check("非列表输入返回空", normalize_collocations("nope") == [])

    # 端到端：LLM 输出带中文意思的搭配，一路带进接口返回值并落库
    config = {
        "generate_example": True,
        "llm": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-x",
            "model": "m",
        },
    }
    entry = {
        "word": "verdict",
        "translation": "裁决",
        "context_text": "",
        "push_count": 1,
    }
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "sentence": "The jury reached a verdict.",
                            "sentence_chinese": "陪审团作出了裁决。",
                            "collocations": [
                                {"text": "reach a verdict", "chinese": "作出裁决"},
                                {"text": "final verdict", "chinese": "最终裁决"},
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    with mock.patch("app.review.example.httpx.Client") as client_cls:
        fake_client = client_cls.return_value.__enter__.return_value
        fake_client.post.return_value.json.return_value = payload
        result = build_example(entry, config)
    check(
        "build_example 返回带意思的搭配",
        result["collocations"]
        == [
            {"text": "reach a verdict", "chinese": "作出裁决"},
            {"text": "final verdict", "chinese": "最终裁决"},
        ],
        result,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            store.save_config({"llm": config["llm"]})
            service.sync_review_queue()
            ids = {e["word_id"]: e["word"] for e in store.load_schedule().values()}
            word_id = next(k for k, v in ids.items() if v == "monetization")

            with mock.patch("app.review.example.httpx.Client") as client_cls:
                fake_client = client_cls.return_value.__enter__.return_value
                fake_client.post.return_value.json.return_value = payload
                refreshed = service.refresh_example(word_id)
            check(
                "按需生成带回意思的搭配",
                refreshed["collocations"][0]
                == {"text": "reach a verdict", "chinese": "作出裁决"},
                refreshed,
            )
            cached = store.get_schedule_entry(word_id)
            check(
                "搭配缓存进词条",
                cached["last_collocations"][0]["chinese"] == "作出裁决",
                cached["last_collocations"],
            )


def test_open_url() -> None:
    print("\n[16] 通知点击地址（固定，不再可配置）")

    from app.review.constants import REVIEW_PAGE_URL

    check(
        "通知固定打开复习会话页",
        REVIEW_PAGE_URL == "http://127.0.0.1:4080/review",
        REVIEW_PAGE_URL,
    )
    check(
        "build_open_url 已删除",
        not hasattr(service, "build_open_url"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            # 老配置里的 open_url_template / page_base 读出来时被清掉
            store.save_config(
                {
                    "open_url_template": "https://old.test/{word_id}",
                    "page_base": "https://old.test",
                    "max_per_day": 6,
                }
            )
            cfg = store.load_config()
            check("老配置的 open_url_template 被清理", "open_url_template" not in cfg, cfg)
            check("老配置的 page_base 被清理", "page_base" not in cfg, cfg)
            check("其他配置不受影响", cfg["max_per_day"] == 6)


def test_batch_notification_text() -> None:
    print("\n[17] 批量通知文案")

    from app.review.notifier import build_batch_body, build_batch_title

    check("标题报数", build_batch_title(8) == "今天有 8 个单词要复习")
    check("标题单数也通顺", build_batch_title(1) == "今天有 1 个单词要复习")
    check(
        "正文预览前几个词",
        build_batch_body(["a", "b", "c", "d"]) == "a、b、c 等 1 个，点击开始复习",
        build_batch_body(["a", "b", "c", "d"]),
    )
    check(
        "词少时全部列出",
        build_batch_body(["a", "b"]) == "a、b，点击开始复习",
    )
    check("没有词也有引导", build_batch_body([]) == "点击开始复习")


def test_llm_headers() -> None:
    print("\n[18] LLM 请求头")

    from app.review.example import _llm_headers

    or_headers = _llm_headers("sk-x", "https://openrouter.ai/api/v1")
    check(
        "OpenRouter 带归属头",
        or_headers["HTTP-Referer"] == "https://pi.dev"
        and or_headers["X-Title"] == "pi",
        or_headers,
    )
    check("OpenRouter 仍带鉴权", or_headers["Authorization"] == "Bearer sk-x")

    other = _llm_headers("sk-x", "https://api.openai.com/v1")
    check(
        "其他提供商不带归属头",
        "HTTP-Referer" not in other and "X-Title" not in other,
        other,
    )
    check("其他提供商仍带鉴权", other["Authorization"] == "Bearer sk-x")

    check(
        "没有 key 时不带鉴权",
        "Authorization" not in _llm_headers("", "https://openrouter.ai/api/v1"),
    )

    # 真的发请求时，这两个头会透传到 httpx
    with mock.patch("app.review.example.httpx.Client") as client_cls:
        fake_client = client_cls.return_value.__enter__.return_value
        fake_client.post.return_value.json.return_value = {
            "choices": [{"message": {"content": '{"sentence": "x"}'}}]
        }
        from app.review.example import _request_example

        _request_example(
            "https://openrouter.ai/api/v1", "sk-x", "m", "prompt"
        )
        sent = fake_client.post.call_args.kwargs["headers"]
        check(
            "发往 OpenRouter 的请求带上归属头",
            sent.get("HTTP-Referer") == "https://pi.dev"
            and sent.get("X-Title") == "pi",
            sent,
        )
        check(
            "Client 关掉 trust_env，不让 httpx 自己解析 NO_PROXY",
            client_cls.call_args.kwargs.get("trust_env") is False,
            client_cls.call_args.kwargs,
        )


def test_llm_proxy_env() -> None:
    print("\n[18b] 例句请求的代理解析与 NO_PROXY 坑")

    from app.review.example import _request_example, _resolve_proxy

    # 回归：这台机器的 NO_PROXY 里带方括号 IPv6（``[::1]``），httpx 自己解析
    # 时会在构造 Client 阶段抛 ``InvalidURL: Invalid port``，例句请求永远
    # 发不出去（日志里刷屏的「Invalid port: ':1]'」就是这么来的）。
    hostile = {"NO_PROXY": "localhost,127.0.0.1,::1,[::1]"}
    fake_reply = {"choices": [{"message": {"content": '{"sentence": "x"}'}}]}

    with mock.patch.dict(
        os.environ,
        {
            **hostile,
            "MDX_LLM_PROXY": "",
            "HTTPS_PROXY": "",
            "https_proxy": "",
            "HTTP_PROXY": "",
            "http_proxy": "",
        },
    ):
        with mock.patch("app.review.example.httpx.Client") as client_cls:
            fake_client = client_cls.return_value.__enter__.return_value
            fake_client.post.return_value.json.return_value = fake_reply
            result = _request_example(
                "https://openrouter.ai/api/v1", "sk-x", "m", "prompt"
            )
            check(
                "带方括号 IPv6 的 NO_PROXY 下请求照常发出",
                result == {"sentence": "x"},
                result,
            )
            check(
                "没配代理时不传 proxy（直连）",
                "proxy" not in client_cls.call_args.kwargs,
                client_cls.call_args.kwargs,
            )

    with mock.patch.dict(
        os.environ,
        {
            "MDX_LLM_PROXY": "",
            "HTTPS_PROXY": "",
            "https_proxy": "",
            "HTTP_PROXY": "",
            "http_proxy": "",
        },
    ):
        check("没有任何代理变量时返回 None", _resolve_proxy() is None)

    with mock.patch.dict(
        os.environ,
        {"MDX_LLM_PROXY": "http://p:1", "https_proxy": "http://q:2"},
    ):
        check("MDX_LLM_PROXY 优先", _resolve_proxy() == "http://p:1")

    with mock.patch.dict(
        os.environ,
        {"MDX_LLM_PROXY": "", "HTTPS_PROXY": "", "https_proxy": "http://q:2"},
    ):
        check("退回 https_proxy", _resolve_proxy() == "http://q:2")

    # 有代理时把 URL 显式塞给 Client 的 proxy=，同样绕开 httpx 的解析
    with mock.patch.dict(os.environ, {**hostile, "https_proxy": "http://127.0.0.1:7897"}):
        with mock.patch("app.review.example.httpx.Client") as client_cls:
            fake_client = client_cls.return_value.__enter__.return_value
            fake_client.post.return_value.json.return_value = fake_reply
            result = _request_example(
                "https://openrouter.ai/api/v1", "sk-x", "m", "prompt"
            )
            check(
                "有代理时代理地址显式传给 Client",
                client_cls.call_args.kwargs.get("proxy")
                == "http://127.0.0.1:7897"
                and result == {"sentence": "x"},
                (client_cls.call_args.kwargs, result),
            )


# ── 5. 接口 ───────────────────────────────────────────────────────────


def test_api() -> None:
    print("\n[19] REST 接口与页面")

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app
    from app.review import api as review_api

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            client = TestClient(fastapi_app)

            resp = client.get("/api/review/status")
            check("状态接口可用", resp.status_code == 200, resp.text[:200])
            body = resp.json()
            check(
                "状态里带会话与通知可用性",
                "due_count" in body
                and "today_rated" in body
                and "next_notify_at" in body
                and "notification_available" in body,
                list(body),
            )
            check(
                "状态里不再有今日推送数",
                "today_count" not in body,
            )

            resp = client.put(
                "/api/review/config",
                json={"enabled": True, "max_per_day": 5},
            )
            check(
                "配置可更新",
                resp.status_code == 200
                and resp.json()["enabled"] is True
                and resp.json()["max_per_day"] == 5,
                resp.text[:200],
            )
            check(
                "配置里不再有每词每日上限",
                "max_per_word_per_day" not in resp.json(),
            )
            check(
                "配置里不再有通知地址模板",
                "open_url_template" not in resp.json()
                and "page_base" not in resp.json(),
                list(resp.json()),
            )

            resp = client.post("/api/review/sync")
            check("同步接口可用", resp.status_code == 200, resp.text[:200])

            resp = client.get("/api/review/session?mode=auto")
            check("会话接口可用", resp.status_code == 200, resp.text[:200])
            session = resp.json()
            check(
                "会话带词表与状态",
                "words" in session and "state" in session and "today" in session,
                list(session),
            )

            resp = client.get("/api/review/session?mode=bogus")
            check("非法会话模式被拒绝", resp.status_code == 422, resp.text[:200])

            # 全队的词今天都复习过了：auto 空了，manual 仍要凑得出一组
            # （「再来一组」点了没反应的回归）
            today_start = schedule.get_start_of_day_ms(service._now_ms())
            entries = list(store.load_schedule().values())
            for entry in entries:
                entry["last_rated_at"] = today_start
                entry["next_due_at"] = today_start + 24 * 60 * 60 * 1000
            store.upsert_schedule_entries(entries)
            auto = client.get("/api/review/session?mode=auto").json()
            check("今天全复习完 → auto 会话为空", auto["state"] == "empty", auto)
            manual = client.get("/api/review/session?mode=manual").json()
            check(
                "今天全复习完 → manual 仍凑得出一组",
                manual["state"] == "ready" and bool(manual["words"]),
                manual,
            )
            check(
                "HTTP 层补进来的词都打提前复习标记",
                all(w["early"] for w in manual["words"]),
                manual,
            )

            resp = client.get("/api/review/card")
            check("卡片接口可用（无卡片时返回 null）", resp.status_code == 200)

            resp = client.get("/review")
            check(
                "复习页面可访问",
                resp.status_code == 200 and "text/html" in resp.headers["content-type"],
                resp.headers.get("content-type", ""),
            )
            # 单文件页面（HTML+CSS+JS）改了浏览器必须马上拿到：不然启发式
            # 缓存会让旧 JS 活几个小时，表现为「接口返回是对的，页面还按
            # 旧代码渲染」
            check(
                "页面每次都回服务器验证（no-cache）",
                resp.headers.get("cache-control") == "no-cache",
                resp.headers.get("cache-control", ""),
            )
            page = resp.text
            check("页面里有立即复习", "立即复习" in page)
            check("页面里有复习卡片", "rate-remembered" in page)
            check("页面渲染搭配的中文意思", "collocation-zh" in page)
            check("页面预加载后面卡片的例句", "ensurePrefetch" in page)
            check("空会话时页面说明原因", "emptySessionText" in page)
            check("页面里有设置齿轮", "settings-gear" in page)
            check("页面里有设置弹层", "settings-dialog" in page)
            check("页面里还有开启开关", "开启复习提醒" in page)
            check("喇叭紧挨单词（word-line）", "word-line" in page)
            check("页面显示下次提醒", "下次提醒" in page)
            check("页面不再有测试通知按钮", "测试通知" not in page)
            check("页面不再有通知地址配置", "通知点击后打开的地址" not in page)
            for gone in ("立即推送", "同步队列", "清空队列"):
                check(f"页面不再有「{gone}」", gone not in page)

            word_id = next(iter(store.load_schedule().values()))["word_id"]
            resp = client.post("/api/review/rate", json={"word_id": "nope", "kind": "remembered"})
            check("给不存在的词评分不报错", resp.status_code == 200)

            resp = client.post(
                "/api/review/rate", json={"word_id": "nope", "kind": "wrong"}
            )
            check("非法评分被拒绝", resp.status_code == 400, resp.text[:200])

            resp = client.post(
                "/api/review/rate",
                json={"word_id": word_id, "kind": "remembered", "next_word_id": word_id},
            )
            check("评分可带 next_word_id", resp.status_code == 200, resp.text[:200])

            resp = client.post("/api/review/example", json={"word_id": word_id})
            check("例句接口可用", resp.status_code == 200, resp.text[:200])

            resp = client.post("/api/review/example", json={"word_id": "nope"})
            check("不存在的词没有例句", resp.status_code == 404, resp.text[:200])

            # 测试通知：必须 mock 掉发送器——否则在 Windows 上每跑一次测试
            # 就真弹一条通知，通知栏会被刷疯
            with mock.patch.object(
                review_api, "send_test_notification", return_value=True
            ) as fake_notify:
                resp = client.post("/api/review/notify-test")
                check("测试通知接口可用", resp.status_code == 200, resp.text[:200])
                check(
                    "测试通知打开固定复习地址",
                    resp.json()["open_url"] == REVIEW_PAGE_URL,
                    resp.text[:200],
                )
                check(
                    "通知地址取的是固定常量",
                    fake_notify.call_args.args[0] == REVIEW_PAGE_URL,
                    fake_notify.call_args,
                )

            with mock.patch.object(
                review_api, "send_test_notification", return_value=False
            ):
                resp = client.post("/api/review/notify-test")
                check("通知发送失败时返回 503", resp.status_code == 503, resp.text[:200])


def main() -> int:
    test_advance_stage()
    test_apply_advanced_stage()
    test_backoff_and_interval()
    test_pick_due_entries()
    test_session_pickers()
    test_quiet_hours()
    test_calendar_helpers()
    test_next_times()
    test_store_roundtrip()
    test_store_migration()
    test_sync_queue()
    test_run_tick()
    test_session()
    test_rate_and_card()
    test_refresh_example()
    test_collocation_meaning()
    test_open_url()
    test_batch_notification_text()
    test_llm_headers()
    test_llm_proxy_env()
    test_api()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项:", FAILURES)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
