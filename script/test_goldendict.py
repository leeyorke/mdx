"""GoldenDict 监听模块测试

覆盖：
1. 三个文件的格式解析（favorites / history / history.tmp）
2. 干净单词提取（短语、URL、路径、中文句子等被跳过）
3. 翻译格式化
4. 端到端：首次全量导入 -> 再次运行不重复 -> 新词增量导入
5. 生词本去重：删掉 state 后也不会把生词本已有的词再加进去
6. API 不可用时不标记已处理，恢复后自动补写

运行: uv run python -X utf8 script/test_goldendict.py
"""

import gc
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app.vocabulary as vocabulary  # noqa: E402
from app.goldendict import (  # noqa: E402
    GoldenDictWatcher,
    SingletonLock,
    State,
    extract_clean_word,
    format_translation,
    load_state,
    parse_favorites,
    parse_history,
    parse_history_tmp,
    save_state,
)
from app.main import app as fastapi_app  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  ✘ {name} {detail}")


# ── 测试夹具 ──────────────────────────────────────────────────────────

FAVORITES = """<root>
 <headword>artificial</headword>
 <headword>Consciousness</headword>
 <headword>DROP</headword>
 <headword>by-product</headword>
</root>
"""

HISTORY = """0 credential
0 step by step
12 claim
0 https://ngsl-book.pages.dev/12-%E6%80%BB%E7%BB%93/
0 以太网适配器 VMware Network Adapter VMnet8
0 contradicted
0 contradicte
0 consciousness
"""

HISTORY_TMP = """+ simplify
+ clueless
- simplify
+ 03-城市 The Shape of a City
"""


def write_fixtures(gd_dir: Path) -> None:
    gd_dir.mkdir(parents=True, exist_ok=True)
    (gd_dir / "favorites").write_text(FAVORITES, encoding="utf-8")
    (gd_dir / "history").write_text(HISTORY, encoding="utf-8")
    (gd_dir / "history.tmp").write_text(HISTORY_TMP, encoding="utf-8")


def db_words(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT word FROM vocabulary_words")]
    finally:
        conn.close()


def start_test_server() -> tuple[str, uvicorn.Server]:
    """在空闲端口启动真实的 API 服务，返回 (base_url, server)"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("测试 API 服务启动失败")
    return f"http://127.0.0.1:{port}", server


def stop_test_server(server: uvicorn.Server) -> None:
    server.should_exit = True
    for _ in range(100):
        if not server.started:
            break
        time.sleep(0.05)


def make_watcher(tmp: Path, *, api_base: str = "http://127.0.0.1:8000") -> GoldenDictWatcher:
    return GoldenDictWatcher(
        goldendict_dir=tmp / "goldendict",
        api_base=api_base,
        state_file=tmp / "state" / "goldendict_state.json",
        vocab_db=tmp / "user.db",
    )


# ── 1. 单元测试：解析与清洗 ───────────────────────────────────────────


def test_parsers() -> None:
    print("[1] 解析与干净单词提取")

    check(
        "favorites 解析",
        parse_favorites(FAVORITES) == ["artificial", "Consciousness", "DROP", "by-product"],
    )
    check(
        "history 解析",
        parse_history(HISTORY)
        == [
            "credential",
            "step by step",
            "claim",
            "https://ngsl-book.pages.dev/12-%E6%80%BB%E7%BB%93/",
            "以太网适配器 VMware Network Adapter VMnet8",
            "contradicted",
            "contradicte",
            "consciousness",
        ],
    )
    check(
        "history.tmp 只取 + 行",
        parse_history_tmp(HISTORY_TMP) == ["simplify", "clueless", "03-城市 The Shape of a City"],
    )

    cases = [
        ("credential", "credential"),
        ("  Consciousness  ", "consciousness"),
        ("DROP", "drop"),
        ("by-product", "by-product"),
        ("cyber-", "cyber-"),
        ("effort's", "effort's"),
        ('"claim"', "claim"),
        ("claim.", "claim"),
        # 非单词 -> None
        ("step by step", None),
        ("03-城市 The Shape of a City", None),
        ("https://ngsl-book.pages.dev/x/", None),
        ("0 q = Queue()", None),
        ("以太网适配器 VMnet8", None),
        ('"C:\\Program Files\\XMind\\XMind.exe"', None),
        ("a", None),
        ("", None),
        ("...", None),
    ]
    for raw, expected in cases:
        got = extract_clean_word(raw)
        check(f"extract_clean_word({raw!r}) == {expected!r}", got == expected, f"got {got!r}")


def test_format_translation() -> None:
    print("[2] 翻译格式化")
    entry = {
        "headword": "competitor",
        "pos": "noun",
        "phonetics": {"uk": "/kəmˈpetɪtə(r)/", "us": "/kəmˈpetɪtər/"},
        "senses": [
            {"number": "1", "chinese_definition": "参赛者；竞赛者"},
            {"number": "2", "chinese_definition": "（尤指商业方面的）竞争者，对手"},
        ],
    }
    text = format_translation(entry)
    expected = (
        "competitor\n"
        "英 /kəmˈpetɪtə(r)/ 美 /kəmˈpetɪtər/\n"
        "1. noun. 参赛者；竞赛者\n"
        "2. noun. （尤指商业方面的）竞争者，对手"
    )
    check("format_translation 输出与生词本既有样式一致", text == expected, repr(text))


def test_state_roundtrip() -> None:
    print("[3] state 读写")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state.json"
        st = State(initialized=True, processed={"a", "b"}, total_added=2)
        save_state(p, st)
        loaded = load_state(p)
        check("state 往返一致", loaded.processed == {"a", "b"} and loaded.initialized)
        p.write_text("{broken", encoding="utf-8")
        check("state 损坏时安全降级", load_state(p).processed == set())


def test_singleton_lock() -> None:
    print("[4] 单实例锁")
    with tempfile.TemporaryDirectory() as td:
        lock_file = Path(td) / "watcher.lock"
        a = SingletonLock(lock_file)
        b = SingletonLock(lock_file)
        check("第一次加锁成功", a.acquire())
        check("第二次加锁失败", not b.acquire())
        a.release()
        check("释放后可重新加锁", b.acquire())
        b.release()


# ── 2. 端到端测试 ─────────────────────────────────────────────────────


def test_end_to_end() -> None:
    print("[5] 端到端：首次导入 / 去重 / 增量 / API 故障恢复")

    api_base, server = start_test_server()
    try:
        # 把生词本 DB 指向临时文件，避免污染真实 user.db
        original_db = vocabulary.DB_FILE
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            gd_dir = tmp / "goldendict"
            write_fixtures(gd_dir)
            vocab_db = tmp / "user.db"
            vocabulary.DB_FILE = vocab_db  # API 写入临时库

            try:
                # 预置一条生词本已有数据（claim），验证生词本去重
                pre = httpx.Client(base_url=api_base, timeout=10, trust_env=False)
                r = pre.post("/api/vocabulary", json={"word": "claim", "translation": "声称"})
                pre.close()
                check("预置 claim 成功", r.status_code == 200)

                watcher = make_watcher(tmp, api_base=api_base)

                # ── 首次运行：全部导入 ──
                report = watcher.sync_once()
                words = db_words(vocab_db)
                expected_all = {
                    "artificial", "consciousness", "drop", "by-product",
                    "credential", "contradicted", "contradicte",
                    "simplify", "clueless",
                }
                check(
                    "首次导入数量正确（生词本已有的 claim 跳过）",
                    len(report.added) == 9 and report.skipped_existing == 1,
                    f"added={report.added} skipped_existing={report.skipped_existing}",
                )
                check("首次导入单词集合正确", set(words) == expected_all | {"claim"}, f"got {sorted(words)}")
                check("大小写不敏感去重（Consciousness/consciousness 只入一次）",
                      len([w for w in words if w == "consciousness"]) == 1)
                check("非单词条目被跳过", report.skipped_non_word.get("history") == 3
                      and report.skipped_non_word.get("history.tmp") == 1,
                      f"got {report.skipped_non_word}")
                conn = sqlite3.connect(str(vocab_db))
                note = conn.execute("SELECT note FROM vocabulary_words WHERE word='simplify'").fetchone()[0]
                translation = conn.execute(
                    "SELECT translation FROM vocabulary_words WHERE word='credential'"
                ).fetchone()[0]
                conn.close()
                check("note 含来源说明", "history.tmp" in note, note)
                check("translation 为词典格式", translation.startswith("credential\n英 /"), translation[:60])

                # ── 第二次运行：什么都不加 ──
                report2 = watcher.sync_once()
                check("第二次运行 0 新增", len(report2.added) == 0, f"got {report2.added}")
                check("第二次运行全部判为已处理", report2.skipped_processed == 9,
                      f"got {report2.skipped_processed}")
                check("生词本条数不变", len(db_words(vocab_db)) == 10)

                # ── 新词增量 ──
                (gd_dir / "history").write_text(HISTORY + "0 brandnewword\n", encoding="utf-8")
                report3 = watcher.sync_once()
                check("新词只加入 1 个", report3.added == ["brandnewword"], f"got {report3.added}")
                check("生词本总数 11", len(db_words(vocab_db)) == 11)

                # ── 删掉 state 也不会重复（生词本去重兜底） ──
                (watcher.state_file).unlink()
                watcher.state = load_state(watcher.state_file)
                report4 = watcher.sync_once()
                check("state 丢失后 0 新增（生词本去重）", len(report4.added) == 0,
                      f"got {report4.added}")

                # ── API 不可用：不标记已处理，恢复后补写 ──
                gd2 = tmp / "gd2"
                gd2.mkdir()
                (gd2 / "favorites").write_text("<root>\n <headword>recoverword</headword>\n</root>\n", encoding="utf-8")
                (gd2 / "history").write_text("0 recoversecond\n", encoding="utf-8")
                vocab_db2 = tmp / "user2.db"
                vocabulary.DB_FILE = vocab_db2  # API 此时写入第二个临时库
                watcher2 = GoldenDictWatcher(
                    goldendict_dir=gd2,
                    api_base="http://127.0.0.1:59999",  # 没有服务的端口
                    state_file=tmp / "state2.json",
                    vocab_db=vocab_db2,
                )
                report5 = watcher2.sync_once()
                check("API 不可用时报告错误", len(report5.errors) == 2, f"got {report5.errors}")
                check("API 不可用时 0 写入", len(report5.added) == 0)
                check("失败的词不被标记为已处理",
                      load_state(watcher2.state_file).processed == set())
                watcher2.api_base = api_base  # API 恢复
                report6 = watcher2.sync_once()
                check("API 恢复后自动补写", sorted(report6.added) == ["recoversecond", "recoverword"],
                      f"got {report6.added}")
                check("补写后生词本有 2 条", sorted(db_words(vocab_db2)) == ["recoversecond", "recoverword"])
            finally:
                vocabulary.DB_FILE = original_db
                gc.collect()  # 释放 sqlite 连接，保证临时目录可清理
    finally:
        stop_test_server(server)


def test_real_dictionary_translation() -> None:
    print("[6] 真实词典翻译查询（只读 oaldpe.db）")
    watcher = GoldenDictWatcher()
    text = watcher._lookup_translation("competitor")
    check("competitor 翻译包含音标与义项",
          text.startswith("competitor\n英 /") and "1. noun." in text, repr(text[:80]))
    check("未收录单词返回占位符",
          watcher._lookup_translation("zzqqxxnotaword") == "（暂无释义）")


def test_quiet_hours() -> None:
    print("[7] 免打扰时段")
    from datetime import datetime
    from unittest import mock

    import app.goldendict as gd

    check("解析 0-8", gd._parse_quiet_hours("0-8") == (0, 8))
    check("解析 22-6（跨午夜）", gd._parse_quiet_hours("22-6") == (22, 6))
    check("空值关闭免打扰", gd._parse_quiet_hours("") is None)
    check("非法值被忽略", gd._parse_quiet_hours("bad") is None)
    check("越界值被忽略", gd._parse_quiet_hours("25-8") is None)
    check("起止相同被忽略", gd._parse_quiet_hours("8-8") is None)

    with mock.patch.object(gd, "QUIET_HOURS", (0, 8)):
        check("00:30 免打扰", gd.in_quiet_hours(datetime(2026, 1, 1, 0, 30)))
        check("07:59 免打扰", gd.in_quiet_hours(datetime(2026, 1, 1, 7, 59)))
        check("08:00 解除免打扰", not gd.in_quiet_hours(datetime(2026, 1, 1, 8, 0)))
        check("23:00 不免打扰", not gd.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))
        check("00:30 距离结束约 7.5 小时",
              abs(gd.seconds_until_quiet_end(datetime(2026, 1, 1, 0, 30)) - 7.5 * 3600) < 1)
        check("09:00 距离次日结束约 23 小时",
              abs(gd.seconds_until_quiet_end(datetime(2026, 1, 1, 9, 0)) - 23 * 3600) < 1)
    with mock.patch.object(gd, "QUIET_HOURS", (22, 6)):
        check("跨午夜 23:00 免打扰", gd.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))
        check("跨午夜 03:00 免打扰", gd.in_quiet_hours(datetime(2026, 1, 1, 3, 0)))
        check("跨午夜 12:00 不免打扰", not gd.in_quiet_hours(datetime(2026, 1, 1, 12, 0)))
    with mock.patch.object(gd, "QUIET_HOURS", None):
        check("未配置时永不免打扰", not gd.in_quiet_hours(datetime(2026, 1, 1, 3, 0)))
        check("未配置时等待时长为 0",
              gd.seconds_until_quiet_end(datetime(2026, 1, 1, 3, 0)) == 0.0)


def test_run_forever_quiet_hours() -> None:
    print("[8] 持续监听：免打扰时段暂停轮询")
    from unittest import mock

    import app.goldendict as gd

    with tempfile.TemporaryDirectory() as td:
        watcher = GoldenDictWatcher(
            goldendict_dir=Path(td) / "gd",
            api_base="http://127.0.0.1:1",
            state_file=Path(td) / "state.json",
            vocab_db=Path(td) / "user.db",
        )

        sleeps: list[float] = []
        quiet_waits: list[float] = []
        sync_calls = {"n": 0}

        class _StopLoop(Exception):
            pass

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise _StopLoop

        def fake_chunks(seconds: float, chunk: float = 60.0) -> None:
            quiet_waits.append(seconds)

        def fake_sync() -> gd.SyncReport:
            sync_calls["n"] += 1
            return gd.SyncReport()

        quiet_states = iter([True, False, False])

        with mock.patch.object(gd.time, "sleep", fake_sleep), \
             mock.patch.object(gd, "_sleep_in_chunks", fake_chunks), \
             mock.patch.object(watcher, "sync_once", fake_sync), \
             mock.patch.object(gd, "in_quiet_hours", lambda: next(quiet_states)), \
             mock.patch.object(gd, "seconds_until_quiet_end", lambda: 1234.0):
            try:
                watcher.run_forever(interval=3600)
            except _StopLoop:
                pass

        check("免打扰时段先休眠到结束", quiet_waits == [1234.0], f"got {quiet_waits}")
        check("免打扰期间不执行同步", sync_calls["n"] == 2, f"got {sync_calls}")
        check("正常时段按 interval 休眠", sleeps == [3600, 3600], f"got {sleeps}")


def main() -> int:
    test_parsers()
    test_format_translation()
    test_state_roundtrip()
    test_singleton_lock()
    test_end_to_end()
    test_real_dictionary_translation()
    test_quiet_hours()
    test_run_forever_quiet_hours()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项:", FAILURES)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
