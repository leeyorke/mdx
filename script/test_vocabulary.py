"""生词本数据层与接口测试

覆盖（修复「重复收藏会插第二条」后的行为）：

1. add_word 查重：同一个词（大小写不敏感、全表）不插第二条，返回已有记录
2. 重复添加不改动已有记录的任何字段（created_at / star / translation 原样）
3. 欧路词算「已有」：本地收藏一个已同步进来的欧路词不新建行
4. 正常路径仍然建行；首尾空格不影响查重
5. POST /api/vocabulary：新建 duplicate=false，重复 duplicate=true 且行数不变
6. 重复收藏不去推欧路（首次收藏才推）

运行: uv run python -X utf8 script/test_vocabulary.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 测试期间不让 GoldenDict 监听随主 app 自动启动
os.environ["MDX_WATCH_AUTOSTART"] = "0"

import app.eudic as eudic  # noqa: E402
import app.vocabulary as vocabulary  # noqa: E402

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


def _use_temp_db(tmp_dir: str) -> Path:
    """把生词表和欧路配置表都指到临时库（不碰真实 user.db）。"""
    db_path = Path(tmp_dir) / "vocabulary.db"
    vocabulary.DB_FILE = db_path
    eudic.DB_FILE = db_path
    return db_path


# ── 1. add_word 查重 ───────────────────────────────────────────────


def test_add_word_dedup() -> None:
    print("\n[1] add_word 查重")

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)

        first, created = vocabulary.add_word("ephemeral", "adj. 短暂的")
        check("首次添加返回 created=True", created is True)

        second, created_again = vocabulary.add_word("Ephemeral", "adj. 转瞬即逝的")
        check("重复添加返回 created=False", created_again is False)
        check("返回的是已有记录的 id", second["id"] == first["id"], second["id"])
        check(
            "全表仍然只有一行",
            vocabulary.count_words_by_source()["all"] == 1,
            vocabulary.count_words_by_source(),
        )

        words, total = vocabulary.list_words(limit=100)
        check("没有插入第二行", total == 1, total)
        check("旧译文没有被新翻译覆盖", words[0]["translation"] == "adj. 短暂的", words[0]["translation"])
        check("created_at 没有被刷新", words[0]["created_at"] == first["created_at"])
        check("星级保持默认 1", words[0]["star"] == 1)

        # 首尾空格：扩展传进来的是原始选区文本，可能带空格
        padded, created_padded = vocabulary.add_word("  ephemeral  ", "adj. 短暂的")
        check("带空格的同一个词也判重", created_padded is False)
        check("仍然返回已有记录", padded["id"] == first["id"])
        check("全表仍然只有一行", vocabulary.list_words(limit=100)[1] == 1)


# ── 2. 欧路词算「已有」 ─────────────────────────────────────────────


def test_eudic_word_counts_as_existing() -> None:
    print("\n[2] 欧路词算「已有」")

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)

        record, _created = vocabulary.add_word("lucid", "adj. 清晰")
        vocabulary.set_word_source(record["id"], "eudic", "0:lucid", "0")

        again, created_again = vocabulary.add_word("Lucid", "adj. 清楚")
        check("欧路词不让本地再插一条", created_again is False)
        check("返回欧路那条记录", again["id"] == record["id"])
        check("全表仍然只有一行", vocabulary.count_words_by_source()["all"] == 1)


# ── 3. REST 接口 ───────────────────────────────────────────────────


def test_api() -> None:
    print("\n[3] REST 接口")

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)
        # 临时库默认不推欧路；再显式关一道，避免环境里有 EUDIT_API_KEY 时
        # 测试真的去打欧路接口
        with mock.patch.object(eudic, "sync_new_words_enabled", return_value=False):
            client = TestClient(fastapi_app)

            resp = client.post("/api/vocabulary", json={"word": "apple", "translation": "苹果"})
            check("新建返回 200", resp.status_code == 200, resp.text[:200])
            body = resp.json()
            check("新建 duplicate=false", body["duplicate"] is False, body)

            resp = client.post("/api/vocabulary", json={"word": "Apple", "translation": "苹果"})
            check("重复添加返回 200", resp.status_code == 200, resp.text[:200])
            body = resp.json()
            check("重复添加 duplicate=true", body["duplicate"] is True, body)
            check("重复添加返回已有记录的 id", body["id"] == first_id(client), body)

            words, total = vocabulary.list_words(limit=100)
            check("表里仍然只有一条", total == 1, total)
            check("原有记录的字段没被改动", words[0]["translation"] == "苹果", words[0])


def first_id(client) -> str:
    """取出列表里第一条的 id（临时库里此时只应有一条）。"""
    resp = client.get("/api/vocabulary?limit=1")
    return resp.json()["records"][0]["id"]


# ── 4. 重复收藏不推欧路 ─────────────────────────────────────────────


def test_duplicate_skips_eudic_push() -> None:
    print("\n[4] 重复收藏不推欧路")

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)
        with mock.patch.object(eudic, "sync_new_words_enabled", return_value=True), \
                mock.patch.object(eudic, "push_new_word") as push:
            client = TestClient(fastapi_app)

            resp = client.post("/api/vocabulary", json={"word": "apple", "translation": "苹果"})
            check("首次收藏推一次欧路", push.call_count == 1, push.call_count)
            check("首次收藏 duplicate=false", resp.json()["duplicate"] is False)

            push.reset_mock()
            resp = client.post("/api/vocabulary", json={"word": "APPLE", "translation": "苹果"})
            check("重复收藏 duplicate=true", resp.json()["duplicate"] is True)
            check("重复收藏不再推欧路", push.call_count == 0, push.call_count)


def main() -> int:
    test_add_word_dedup()
    test_eudic_word_counts_as_existing()
    test_api()
    test_duplicate_skips_eudic_push()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项:", FAILURES)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
