"""复习成果检验（写作 + LLM 评卷）模块测试

覆盖：

1. 写作三张表的建库（幂等）与老库兼容（建表走 store._CREATE_SQL）
2. build_challenge：词表来源（word_ids / 会话）、**词表不含释义**、空词表
3. LLM 评卷：正常 JSON / 带 ``` 围栏 / 畸形输出（不 500、字段回落）/ 分数钳制
4. 两条降级路径：LLM 未配置、调用异常 → 本地匹配兜底（score=None、llm_source=matcher）
5. 用词覆盖匹配（大小写不敏感、beliefs→believe 词形归一化）
6. span 校验：offset 对不上原文的 span 被丢弃，不让前端画错位
7. 草稿 → 评卷 → 保存笔记 的落库读回（含 grade JSON 往返、score /10 展示）
8. 笔记分页与 all/high/improve 筛选、排序白名单、counts 角标
9. REST 接口与 /verify 页面（no-cache）

运行: uv run python -X utf8 script/test_writing.py

测试里所有 LLM 调用都做了 mock，跑测试不发网络请求、不弹通知。
"""

import json
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

import app.review.service as service  # noqa: E402
import app.review.store as store  # noqa: E402
import app.review.writing as writing  # noqa: E402
import app.review.writing_store as writing_store  # noqa: E402
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


# ── 夹具 ──────────────────────────────────────────────────────────────

NOW = int(datetime(2026, 3, 10, 12, 0, 0).timestamp() * 1000)

LLM = {"base_url": "https://api.example.com/v1", "api_key": "sk-x", "model": "gpt-test"}

def _grade(score: int = 88) -> dict:
    """一份结构完整的评卷 JSON（span 按 TEXT 动态对齐）。"""
    return {
        "score": score,
        "usage": [
            {"word": "verdict", "used": True, "form_in_text": "verdict"},
            {"word": "deliberation", "used": False, "form_in_text": ""},
        ],
        "spans": [_verdict_span()],
        "feedback": "遣词造句十分地道。",
        "corrections": [
            {
                "original": "most of the people believes",
                "suggestion": "most people believe",
                "explanation": "主谓一致错误。",
                "type": "grammar",
            }
        ],
        "vocab_suggestions": [{"word": "deliberation", "note": "用语高级且准确。"}],
        "polished": "",
    }

TEXT = "The jury has reached a final verdict after three days of deliberation."


def _verdict_span() -> dict:
    """GOOD_GRADE 里 verdict 的 span：按 TEXT 动态算，别硬编码 offset。"""
    start = TEXT.index("verdict")
    return {"start": start, "end": start + len("verdict"), "text": "verdict", "kind": "good"}


def _make_vocabulary() -> None:
    """造两个复习过一次的生词（检验跟着复习会话的词走）。"""
    for word in ("verdict", "deliberation"):
        record, _created = vocabulary.add_word(word, f"{word} 的意思")
        vocabulary.review_word(record["id"])


def _sync_queue() -> None:
    service.sync_review_queue()


def _llm_json(raw: str):
    """把 LLM 返回体 mock 成给定 JSON 文本。"""
    return (
        mock.patch.object(writing.llm_client, "is_configured", return_value=True),
        mock.patch.object(writing.llm_client, "chat", return_value=raw),
    )


def _json_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ── 1. 建库 ───────────────────────────────────────────────────────────


def test_schema() -> None:
    print("\n[1] 写作三张表的建库")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            store._ensure_db()
            # 三张表都在
            with store._get_conn() as conn:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            check("review_writing_tasks 已建", "review_writing_tasks" in names)
            check("review_writing_submissions 已建", "review_writing_submissions" in names)
            check("review_writing_notes 已建", "review_writing_notes" in names)

            # 幂等：再跑一次 _ensure_db 不报错、不重建
            store._ensure_db()
            check("建库幂等", writing_store.get_task("nope") is None)

            # 老库兼容：一份只有复习表（不含写作表）的库，第一次用到检验时
            # 表会被补上。review_schedule 要给全列，否则 _CREATE_SQL 里
            # idx_rs_due(idx_rs_sync) 建 next_due_at / synced_at 索引时找不到列。
            db2 = Path(tmp) / "old.db"
            import sqlite3

            conn = sqlite3.connect(db2)
            conn.executescript(
                """
                CREATE TABLE review_schedule (
                    word_id TEXT PRIMARY KEY,
                    next_due_at INTEGER NOT NULL,
                    synced_at INTEGER NOT NULL
                );
                CREATE TABLE review_stats (day TEXT PRIMARY KEY);
                CREATE TABLE review_settings (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            conn.commit()
            conn.close()
            with mock.patch.object(store, "DB_FILE", db2):
                store._ensure_db()
                check(
                    "老库补上写作表",
                    writing_store.get_task("x") is None and writing_store.count_notes() == 0,
                )


# ── 2. build_challenge ────────────────────────────────────────────────


def test_build_challenge() -> None:
    print("\n[2] 检验词表（只给词形，不给释义）")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            vocabulary.DB_FILE = db_path
            _make_vocabulary()
            _sync_queue()
            ids = [e["word_id"] for e in store.load_schedule().values()]

            c = writing.build_challenge(ids, "paragraph", now=NOW)
            check("返回 task_id", bool(c["task_id"]))
            check("词表来自指定 ID", sorted(c["words"]) == ["deliberation", "verdict"], c["words"])
            check("词表不含释义（只有词形）", all(" " not in w and "的" not in w for w in c["words"]))

            task = writing_store.get_task(c["task_id"])
            check("任务落库且词表快照一致", sorted(task["words"]) == ["deliberation", "verdict"])
            check("任务模式落库", task["mode"] == "paragraph")

            # 空词表 → empty
            empty = writing.build_challenge([], "paragraph", now=NOW)
            check("无词可检验时 state=empty", empty["state"] == "empty", empty)
            check("空词表不带 task_id", empty["task_id"] is None)

            # 非法词表（全是不存在的 ID）→ empty
            bad = writing.build_challenge(["nope", "nah"], "paragraph", now=NOW)
            check("全不存在的 ID 也算 empty", bad["state"] == "empty", bad)


# ── 3. LLM 评卷 ───────────────────────────────────────────────────────


def test_grade_llm() -> None:
    print("\n[3] LLM 评卷（解析 / 围栏 / 畸形 / 分数钳制）")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            vocabulary.DB_FILE = db_path
            _make_vocabulary()
            _sync_queue()
            ids = [e["word_id"] for e in store.load_schedule().values()]
            task = writing.build_challenge(ids, "paragraph", now=NOW)
            tid = task["task_id"]

            # 正常 JSON
            p1, p2 = _llm_json(_json_text(_grade()))
            with p1, p2:
                r = writing.grade_writing(tid, TEXT, now=NOW)
            check("返回分数", r["score"] == 88, r)
            check("llm_source=llm", r["llm_source"] == "llm")
            check("逐条批改解析", r["grade"]["corrections"][0]["type"] == "grammar")
            check("span 解析", r["grade"]["spans"][0]["text"] == "verdict")
            check("词汇建议解析", r["grade"]["vocab_suggestions"][0]["word"] == "deliberation")
            check("返回 submission_id", bool(r["submission_id"]))

            # 带 ``` 围栏
            fenced = "```json\n" + _json_text(_grade()) + "\n```"
            p1, p2 = _llm_json(fenced)
            with p1, p2:
                r2 = writing.grade_writing(tid, TEXT, now=NOW)
            check("带 ``` 围栏也解析得出", r2["score"] == 88, r2)

            # 畸形输出：不 500，字段回落
            p1, p2 = _llm_json("这不是 JSON，评分大概是 999 分吧")
            with p1, p2:
                r3 = writing.grade_writing(tid, TEXT, now=NOW)
            check("畸形输出不 500、走兜底", r3["llm_source"] == "matcher", r3)

            # 分数钳制：超范围 / 非数字
            clamped = _grade(score=250)
            p1, p2 = _llm_json(_json_text(clamped))
            with p1, p2:
                r4 = writing.grade_writing(tid, TEXT, now=NOW)
            check("分数钳到 100", r4["score"] == 100, r4)

            nonum = _grade(score="很好")
            p1, p2 = _llm_json(_json_text(nonum))
            with p1, p2:
                r5 = writing.grade_writing(tid, TEXT, now=NOW)
            check("非数字分数回落 None", r5["score"] is None, r5)

            # 输入校验
            check("空文本返回 None", writing.grade_writing(tid, "   ", now=NOW) is None)
            check(
                "超长文本返回 None",
                writing.grade_writing(tid, "a" * (writing.MAX_TEXT_CHARS + 1), now=NOW) is None,
            )
            check("无效 task_id 返回 None", writing.grade_writing("nope", TEXT, now=NOW) is None)


# ── 4. 降级路径 ───────────────────────────────────────────────────────


def test_grade_fallback() -> None:
    print("\n[4] LLM 不可用时的本地兜底")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            vocabulary.DB_FILE = db_path
            _make_vocabulary()
            _sync_queue()
            ids = [e["word_id"] for e in store.load_schedule().values()]
            task = writing.build_challenge(ids, "paragraph", now=NOW)
            tid = task["task_id"]

            # 未配置 LLM
            with mock.patch.object(writing.llm_client, "is_configured", return_value=False):
                r = writing.grade_writing(tid, TEXT, now=NOW)
            check("未配 LLM 不报错", r is not None and r["llm_source"] == "matcher", r)
            check("未配 LLM 不打分", r["score"] is None)
            check("未配 LLM 也算用词覆盖", "verdict" in r["used_words"], r["used_words"])
            check("兜底有说明文案", "未配置" in (r["feedback"] or ""))

            # 配了但调用抛异常
            with mock.patch.object(writing.llm_client, "is_configured", return_value=True), \
                 mock.patch.object(writing.llm_client, "chat", return_value=None):
                r2 = writing.grade_writing(tid, TEXT, now=NOW)
            check("调用失败走兜底", r2["llm_source"] == "matcher", r2)


# ── 5. 用词覆盖匹配 ───────────────────────────────────────────────────


def test_match_used_words() -> None:
    print("\n[5] 用词覆盖匹配（大小写 / 词形归一化）")

    words = ["verdict", "deliberation", "believe", "run"]
    text = "The VERDICT came after deliberation; she believes in the run."
    used = writing.match_used_words(text, words)
    check("大小写不敏感", "verdict" in used, used)
    check("原形匹配", "deliberation" in used)
    check("believes→believe 归一化", "believe" in used, used)
    check("run 命中", "run" in used)

    used2 = writing.match_used_words("running and believed", ["run", "believe"])
    check("running→run", "run" in used2, used2)
    check("believed→believe", "believe" in used2)

    check("完全没用到返回空", writing.match_used_words("nothing here", words) == [])


# ── 6. span 校验 ──────────────────────────────────────────────────────


def test_validate_spans() -> None:
    print("\n[6] span 校验（对不上原文的丢弃）")

    t = "The jury reached a final verdict."
    start = t.index("verdict")
    end = start + len("verdict")
    # _validate_spans 只负责「声明 text 与原文切片对不上」这条——负偏移 / 空区间
    # 由上游 normalize_spans（normalize_grade）兜，见下一条用例。
    spans = writing._validate_spans(
        [
            {"start": start, "end": end, "text": "verdict", "kind": "good"},
            {"start": 0, "end": 3, "text": "WRONG", "kind": "good"},  # 声明与切片不符
            {"start": start, "end": end, "text": "verdict", "kind": "error"},
        ],
        t,
    )
    check("正确的 span 保留", len(spans) == 2 and spans[0]["text"] == "verdict", spans)
    check("错位 span 被丢弃", all(s["text"] == "verdict" for s in spans), spans)

    # 上游 normalize：负偏移 / 空区间 / 非法 kind 都在解析阶段就被丢掉
    grade = writing_store.normalize_grade(
        {
            "spans": [
                {"start": start, "end": end, "text": "verdict", "kind": "good"},
                {"start": -1, "end": 3, "text": "The", "kind": "good"},  # 负偏移
                {"start": 5, "end": 5, "text": "", "kind": "good"},  # 空区间
                {"start": 0, "end": 3, "text": "The", "kind": "weird"},  # 非法 kind
                {"start": "x", "end": 3, "text": "The", "kind": "good"},  # 非数字 offset
            ]
        }
    )
    check(
        "负偏移/空区间/非法 kind/非数字 offset 都被丢",
        grade["spans"] == [{"start": start, "end": end, "text": "verdict", "kind": "good"}],
        grade["spans"],
    )


# ── 7. 草稿 → 评卷 → 笔记 ────────────────────────────────────────────


def test_notes_flow() -> None:
    print("\n[7] 草稿 / 评卷 / 保存笔记")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            vocabulary.DB_FILE = db_path
            _make_vocabulary()
            _sync_queue()
            ids = [e["word_id"] for e in store.load_schedule().values()]
            task = writing.build_challenge(ids, "paragraph", now=NOW)
            tid = task["task_id"]

            # 草稿
            draft = writing.save_draft(tid, TEXT, now=NOW)
            check("草稿不打分", draft["score"] is None, draft)
            check("草稿 grade 为空形状", draft["grade"]["corrections"] == [])
            check("草稿也存成笔记", draft["note_id"] is not None)

            # 评卷后保存笔记
            p1, p2 = _llm_json(_json_text(_grade()))
            with p1, p2:
                graded = writing.grade_writing(tid, TEXT, now=NOW)
            note = writing.save_note(graded["submission_id"], now=NOW)
            check("笔记带上分数", note["score"] == 88, note)
            check("笔记 /10 展示", note["score_out_of_ten"] == 8.8, note["score_out_of_ten"])
            check("笔记 grade JSON 往返", note["grade"]["corrections"][0]["suggestion"] == "most people believe")
            check("笔记带词表", sorted(note["words"]) == ["deliberation", "verdict"])

            # 单篇读回
            got = writing.get_note(note["note_id"])
            check("单篇笔记可读回", got["note_id"] == note["note_id"])
            check("不存在的笔记返回 None", writing.get_note("nope") is None)

            # 删除
            check("删除笔记成功", writing.delete_note(note["note_id"]) is True)
            check("删除后读不到", writing.get_note(note["note_id"]) is None)
            check("再删返回 False", writing.delete_note(note["note_id"]) is False)


# ── 8. 列表筛选 ───────────────────────────────────────────────────────


def test_list_notes() -> None:
    print("\n[8] 笔记列表与 all/high/improve 筛选")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            vocabulary.DB_FILE = db_path
            _make_vocabulary()
            _sync_queue()
            ids = [e["word_id"] for e in store.load_schedule().values()]
            task = writing.build_challenge(ids, "paragraph", now=NOW)
            tid = task["task_id"]

            # 造三篇：草稿、高分、待优化
            writing.save_draft(tid, "draft text", now=NOW)
            for score in (92, 55):
                g = _grade(score=score)
                p1, p2 = _llm_json(_json_text(g))
                with p1, p2:
                    graded = writing.grade_writing(tid, TEXT, now=NOW)
                writing.save_note(graded["submission_id"], now=NOW)

            listing = writing.list_notes("all")
            check("全部含草稿", listing["total"] == 3, listing["total"])
            check("三种计数都在", listing["counts"] == {"all": 3, "high": 1, "improve": 1}, listing["counts"])
            check("总数（含草稿）", listing["grand_total"] == 3)

            high = writing.list_notes("high")
            check("高分练习 score>=80", high["total"] == 1 and high["records"][0]["score"] == 92, high)

            improve = writing.list_notes("improve")
            check("待优化 score<70", improve["total"] == 1 and improve["records"][0]["score"] == 55, improve)

            # 草稿不出现在高分/待优化里
            check("草稿不在 high", all(r["score"] is not None for r in high["records"]))
            check("草稿不在 improve", all(r["score"] is not None for r in improve["records"]))

            # 分页
            page = writing.list_notes("all", limit=2, offset=0)
            check("分页 limit 生效", len(page["records"]) == 2, len(page["records"]))

            # 排序白名单：非法排序字段不报错、回落 created_at
            safe = writing.list_notes("all", sort_by="DROP TABLE review_writing_notes;--")
            check("非法排序字段不注入", safe["total"] == 3, safe["total"])


# ── 9. REST 接口与页面 ────────────────────────────────────────────────


def test_api() -> None:
    print("\n[9] REST 接口与 /verify 页面")

    from fastapi.testclient import TestClient

    # 用整个 app 而不是裸 router：/verify 用 FileResponse 回页面，需要
    # middleware 栈（裸 router 上会报 fastapi_middleware_astack not found）。
    from app.main import app as fastapi_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "user.db"
        with mock.patch.object(store, "DB_FILE", db_path):
            vocabulary.DB_FILE = db_path
            _make_vocabulary()
            _sync_queue()
            ids = [e["word_id"] for e in store.load_schedule().values()]

            client = TestClient(fastapi_app)

            # 页面
            resp = client.get("/verify")
            check("检验页面可访问", resp.status_code == 200, resp.text[:200])
            check(
                "检验页面 no-cache",
                resp.headers.get("cache-control") == "no-cache",
                resp.headers.get("cache-control"),
            )

            # challenge
            resp = client.get(
                "/api/review/writing/challenge",
                params={"word_ids": ",".join(ids), "mode": "paragraph"},
            )
            check("challenge 接口可用", resp.status_code == 200, resp.text[:200])
            body = resp.json()
            check("challenge 只返回词形", "translation" not in body and all(" " not in w for w in body["words"]))
            tid = body["task_id"]

            # challenge 空词表
            resp = client.get("/api/review/writing/challenge", params={"word_ids": "nope"})
            check("challenge 无词返回 empty", resp.json()["state"] == "empty")

            # grade（mock 掉 LLM）
            p1, p2 = _llm_json(_json_text(_grade()))
            with p1, p2:
                resp = client.post(
                    "/api/review/writing/grade",
                    json={"task_id": tid, "text": TEXT, "mode": "paragraph"},
                )
            check("评卷接口可用", resp.status_code == 200, resp.text[:200])
            graded = resp.json()
            check("评卷返回分数", graded["score"] == 88)

            # grade 无效任务
            resp = client.post("/api/review/writing/grade", json={"task_id": "nope", "text": TEXT})
            check("无效任务评卷返回 404", resp.status_code == 404, resp.text[:200])

            # 保存笔记
            resp = client.post(
                "/api/review/writing/notes", json={"submission_id": graded["submission_id"]}
            )
            check("保存笔记接口可用", resp.status_code == 200, resp.text[:200])

            # 列表
            resp = client.get("/api/review/writing/notes", params={"filter": "high"})
            check("列表接口可用", resp.status_code == 200, resp.text[:200])
            check("列表带 counts", "counts" in resp.json() and "high" in resp.json()["counts"])

            # 草稿
            resp = client.post(
                "/api/review/writing/draft", json={"task_id": tid, "text": "草稿内容 verdict"}
            )
            check("草稿接口可用", resp.status_code == 200, resp.text[:200])

            # 单篇 + 删除
            note_id = writing.list_notes("all")["records"][0]["note_id"]
            resp = client.get(f"/api/review/writing/notes/{note_id}")
            check("笔记详情接口可用", resp.status_code == 200, resp.text[:200])
            resp = client.delete(f"/api/review/writing/notes/{note_id}")
            check("删除笔记接口可用", resp.status_code == 200, resp.text[:200])
            resp = client.delete(f"/api/review/writing/notes/{note_id}")
            check("再删返回 404", resp.status_code == 404, resp.text[:200])


def main() -> int:
    test_schema()
    test_build_challenge()
    test_grade_llm()
    test_grade_fallback()
    test_match_used_words()
    test_validate_spans()
    test_notes_flow()
    test_list_notes()
    test_api()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项:", FAILURES)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
