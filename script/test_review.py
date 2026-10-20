"""复习提醒模块测试

覆盖（对齐 vibe-reading 原来的 ``src/utils/review/__tests__``）：

1. 艾宾浩斯阶梯推进（auto / remembered / forgotten）
2. 阶梯走完后的两种处置（stop / restart）
3. 推送失败退避
4. 到期词的挑选与排序
5. 免打扰时段（跨零点、当天窗口、关闭、起止相同）
6. 日历工具函数与「下次该唤醒」的计算
7. 配置与队列的落库（临时库，不碰真实 user.db）
8. 队列同步（只收「复习过一次」的生词）
9. 一轮调度：推送、每日配额、单词配额、免打扰推迟
10. 评分（记得 / 忘了）与下一张卡片
11. REST 接口与复习页面

运行: uv run python -X utf8 script/test_review.py
"""

import os
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
from app.review.constants import SRS_INTERVALS_MINUTES  # noqa: E402

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
        "push_count": 0,
        "failure_count": 0,
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
    check(
        "间隔 7 = 15 天",
        schedule.get_interval_ms(7) == 15 * 24 * 60 * 60 * 1000,
    )
    check("越界阶段夹到最后一级", schedule.get_interval_ms(99) == schedule.get_interval_ms(7))


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


# ── 2. 免打扰时段 ─────────────────────────────────────────────────────


def test_quiet_hours() -> None:
    print("\n[5] 免打扰时段")

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
    print("\n[6] 日历工具")

    check(
        "按本地日历日编号",
        schedule.get_day_key(datetime(2026, 1, 5, 23, 59)) == "2026-01-05"
        and schedule.get_day_key(datetime(2026, 12, 31, 0, 0)) == "2026-12-31",
    )

    nxt = datetime.fromtimestamp(
        schedule.get_start_of_next_day(datetime(2026, 3, 10, 23, 59)) / 1000
    )
    check("次日本地零点", nxt.day == 11 and nxt.hour == 0 and nxt.minute == 0)


def test_next_wakeup() -> None:
    print("\n[7] 下次该唤醒的时刻")

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


# ── 3. 存储层 ─────────────────────────────────────────────────────────


def test_store_roundtrip() -> None:
    print("\n[8] 配置与队列落库")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            config = store.load_config()
            check(
                "默认配置齐全",
                config["enabled"] is False
                and config["max_per_day"] == 8
                and config["max_per_word_per_day"] == 3
                and config["quiet_hours"]["start"] == "23:00",
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
                last_collocations=["a", "b"],
                pending_review=True,
            )
            store.upsert_schedule_entries([entry])
            loaded = store.get_schedule_entry("w1")
            check(
                "词条能落库并读回",
                loaded is not None
                and loaded["word"] == "ephemeral"
                and loaded["pending_review"] is True
                and loaded["last_collocations"] == ["a", "b"],
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


# ── 4. 业务层 ─────────────────────────────────────────────────────────


def _make_vocabulary(db_path: Path) -> None:
    """造一份生词库：一个复习过的、一个没复习的。"""
    vocabulary.DB_FILE = db_path
    vocabulary.add_word("ephemeral", "adj. 短暂的")
    reviewed = vocabulary.add_word("monetization", "货币化")
    vocabulary.review_word(reviewed["id"])


def _make_due(now: int) -> None:
    """把队列里的词都标记成「此刻到期」。"""
    entries = list(store.load_schedule().values())
    for entry in entries:
        entry["next_due_at"] = now
    store.upsert_schedule_entries(entries)


def test_sync_queue() -> None:
    print("\n[9] 队列同步")

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

            # 再同步一次：不该重复添加
            again = service.sync_review_queue()
            check("重复同步不会重复添加", again["added"] == 0 and again["total"] == 1)


def test_run_tick() -> None:
    print("\n[10] 一轮调度")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            service.sync_review_queue()
            _make_due(NOW)
            service.update_config(
                {
                    "enabled": True,
                    "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
                }
            )

            pushed: list[str] = []

            def fake_notify(word, translation, sentence, launch_url):
                pushed.append(word)
                return True

            with mock.patch.object(service, "send_review_notification", fake_notify):
                outcome = service.run_review_tick(now=NOW)
                check("到点的词被推送", outcome["pushed"] == 1, outcome)
                check("推送记录正确", pushed == ["monetization"], pushed)

                entry = next(iter(store.load_schedule().values()))
                check(
                    "推送后阶梯前进、标记待评分",
                    entry["stage"] == 1
                    and entry["push_count"] == 1
                    and entry["pending_review"] is True,
                )

                # 免打扰时段：整队推迟
                _make_due(NOW)
                service.update_config(
                    {
                        "quiet_hours": {"enabled": True, "start": "00:00", "end": "23:59"}
                    }
                )
                outcome = service.run_review_tick(now=NOW)
                check(
                    "免打扰时段内推迟且不推送",
                    outcome["pushed"] == 0
                    and outcome["stopped_because"] == "quietHours",
                    outcome,
                )

                # 每日配额
                _make_due(NOW)
                service.update_config(
                    {
                        "quiet_hours": {"enabled": False, "start": "23:00", "end": "08:00"},
                        "max_per_day": 1,
                    }
                )
                outcome = service.run_review_tick(now=NOW)
                check(
                    "超出每日配额时推迟到明天",
                    outcome["pushed"] == 0 and outcome["stopped_because"] == "dailyCap",
                    outcome,
                )

                # 单词配额
                _make_due(NOW)
                service.update_config({"max_per_day": 8, "max_per_word_per_day": 1})
                outcome = service.run_review_tick(now=NOW)
                check(
                    "超出单词配额时推迟到明天",
                    outcome["pushed"] == 0
                    and outcome["stopped_because"] == "perWordCap",
                    outcome,
                )

                # 关闭功能
                service.update_config({"enabled": False})
                outcome = service.run_review_tick(now=NOW)
                check(
                    "功能关闭时不推送",
                    outcome["pushed"] == 0 and outcome["stopped_because"] == "disabled",
                    outcome,
                )

                # 推送失败要退避，而不是吃掉阶梯位置
                _make_due(NOW)
                service.update_config({"enabled": True, "max_per_word_per_day": 3})

                def failing_notify(word, translation, sentence, launch_url):
                    raise RuntimeError("通知发送失败")

                with mock.patch.object(
                    service, "send_review_notification", failing_notify
                ):
                    service.run_review_tick(now=NOW)

                entry = next(iter(store.load_schedule().values()))
                check(
                    "推送失败时阶梯不动、改为退避",
                    entry["stage"] == 1
                    and entry["failure_count"] == 1
                    and entry["next_due_at"] > NOW,
                    entry,
                )


def test_rate_and_card() -> None:
    print("\n[11] 评分与卡片")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            service.sync_review_queue()
            _make_due(NOW)
            service.update_config({"enabled": True})

            word_id = next(iter(store.load_schedule().values()))["word_id"]

            card = service.get_card(word_id)
            check("能取到卡片", card is not None and card["word"] == "monetization")
            check("卡片带上阶梯信息", card["total_stages"] == len(SRS_INTERVALS_MINUTES))

            with mock.patch.object(
                service, "send_review_notification", lambda *a, **k: True
            ):
                service.run_review_tick(now=NOW)

            card = service.get_card()
            check("推送后能取到待评分卡片", card is not None)

            next_card = service.rate_word(word_id, "remembered")
            check("评分后返回下一张卡片（没有则为空）", next_card is None)

            entry = store.get_schedule_entry(word_id)
            check(
                "记得时跨一级、清除待评分",
                entry["stage"] == 3 and entry["pending_review"] is False,
                entry,
            )

            service.rate_word(word_id, "forgotten")
            entry = store.get_schedule_entry(word_id)
            check("忘了时退一级", entry["stage"] == 2, entry)


def test_open_url() -> None:
    print("\n[12] 通知点击地址")

    config = {"open_url_template": "http://127.0.0.1:4080/review?word={word_id}"}
    check(
        "带生词 ID",
        service.build_open_url(config, "abc-123")
        == "http://127.0.0.1:4080/review?word=abc-123",
    )
    check(
        "不带生词 ID 时去掉空参数",
        service.build_open_url(config, None) == "http://127.0.0.1:4080/review",
        service.build_open_url(config, None),
    )
    check(
        "自定义模板",
        service.build_open_url({"open_url_template": "https://x.test/{word_id}"}, "w1")
        == "https://x.test/w1",
    )


# ── 5. 接口 ───────────────────────────────────────────────────────────


def test_api() -> None:
    print("\n[13] REST 接口与页面")

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "review.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            _make_vocabulary(db_path)
            client = TestClient(fastapi_app)

            resp = client.get("/api/review/status")
            check("状态接口可用", resp.status_code == 200, resp.text[:200])
            body = resp.json()
            check(
                "状态里带队列与通知可用性",
                "queue_size" in body and "notification_available" in body,
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

            resp = client.post("/api/review/sync")
            check("同步接口可用", resp.status_code == 200, resp.text[:200])

            resp = client.get("/api/review/card")
            check("卡片接口可用（无卡片时返回 null）", resp.status_code == 200)

            resp = client.get("/review")
            check(
                "复习页面可访问",
                resp.status_code == 200 and "text/html" in resp.headers["content-type"],
                resp.headers.get("content-type", ""),
            )
            check("页面里含复习卡片", "rate-remembered" in resp.text)

            resp = client.post("/api/review/rate", json={"word_id": "nope", "kind": "remembered"})
            check("给不存在的词评分不报错", resp.status_code == 200)

            resp = client.post(
                "/api/review/rate", json={"word_id": "nope", "kind": "wrong"}
            )
            check("非法评分被拒绝", resp.status_code == 400, resp.text[:200])


def main() -> int:
    test_advance_stage()
    test_apply_advanced_stage()
    test_backoff_and_interval()
    test_pick_due_entries()
    test_quiet_hours()
    test_calendar_helpers()
    test_next_wakeup()
    test_store_roundtrip()
    test_sync_queue()
    test_run_tick()
    test_rate_and_card()
    test_open_url()
    test_api()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项:", FAILURES)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
