"""欧路生词本接入测试

覆盖（对齐真实接口的实测行为）：

1. token 解析优先级：环境变量 > 设置页 > 无
2. 释义/语境的 HTML 清洗
3. ISO8601 时间解析
4. 欧路错误归一（401 → 鉴权错误，5xx → 接口错误）
5. 翻页聚合（跨页拼接、到空页停止）
6. 同步 upsert 规则（新词建行 / 已有词不覆盖星级与译文 /
   用户评过分的词保留本地星级 / 认领同名本地词）
7. 写回欧路（改星级不传 context_line / 删除 / 新增）
8. 配置落库
9. REST 接口

运行: uv run python -X utf8 script/test_eudic.py
"""

import json
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
import httpx  # noqa: E402

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


# ── 假 HTTP 客户端 ──────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


class FakeHttp:
    """记录所有请求，按 handler 分发响应。

    handler 可以抛异常，用来模拟传输层故障。
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.handler = None

    # httpx.Client 的接口形状
    def __call__(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, headers=None, params=None, content=None):
        body = _decode(content)
        self.calls.append({"method": method, "url": url, "body": body, "params": params})
        return self.handler(method, url, body, params)

    def get(self, url, headers=None, params=None):
        return self.request("GET", url, headers=headers, params=params)

    def post(self, url, headers=None, content=None):
        return self.request("POST", url, headers=headers, content=content)

    def last(self) -> dict:
        return self.calls[-1]

    def paths(self) -> list[str]:
        return [call["url"].replace(eudic.EUDIC_API_BASE, "") for call in self.calls]


def _decode(content):
    if content is None:
        return None
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


def _patch_http(fake: FakeHttp):
    return mock.patch.object(eudic.httpx, "Client", fake)


def _use_temp_db(tmp_dir: str) -> Path:
    """把生词表和欧路配置表都指到临时库。"""
    db_path = Path(tmp_dir) / "eudic.db"
    vocabulary.DB_FILE = db_path
    eudic.DB_FILE = db_path
    return db_path


# ── 1. token 解析 ───────────────────────────────────────────────────


def test_token_resolution() -> None:
    print("\n[1] token 解析优先级")

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EUDIT_API_KEY", None)
            os.environ.pop("EUDIC_API_KEY", None)

            token, source = eudic.resolve_token()
            check("没有 token 时返回 none", token is None and source == "none", f"{token!r} {source}")

            eudic.save_config({"fallbackToken": "NIS from-settings"})
            token, source = eudic.resolve_token()
            check(
                "设置页里的 token 兜底",
                token == "NIS from-settings" and source == "settings",
                f"{token!r} {source}",
            )

            os.environ["EUDIT_API_KEY"] = "NIS from-env"
            token, source = eudic.resolve_token()
            check(
                "环境变量优先于设置页",
                token == "NIS from-env" and source == "env",
                f"{token!r} {source}",
            )

            os.environ["EUDIC_API_KEY"] = "NIS alias"
            token, source = eudic.resolve_token()
            check("别名环境变量也认（EUDIT 优先）", token == "NIS from-env", f"{token!r}")

            # 空白的环境变量不算「已配置」——要能落到设置页的兜底上
            with mock.patch.dict(os.environ, {"EUDIT_API_KEY": "   "}, clear=False):
                os.environ.pop("EUDIC_API_KEY", None)
                token, source = eudic.resolve_token()
            check(
                "空白的环境变量不算已配置",
                token == "NIS from-settings" and source == "settings",
                f"{token!r} {source}",
            )


# ── 2. 文本清洗 ─────────────────────────────────────────────────────


def test_clean_text() -> None:
    print("\n[2] HTML 清洗")

    check(
        "去掉语境里的 <b> 标记",
        eudic.clean_text("not through <b>manifestation</b> or magical thinking.")
        == "not through manifestation or magical thinking.",
        eudic.clean_text("not through <b>manifestation</b> or magical thinking."),
    )

    check(
        "<br> 变成空格",
        eudic.clean_text("催吐剂<br><br>时 态: vomited")
        == "催吐剂 时 态: vomited",
        eudic.clean_text("催吐剂<br><br>时 态: vomited"),
    )

    check(
        "解码 HTML 实体",
        eudic.clean_text("a &amp; b &lt;c&gt;") == "a & b <c>",
        eudic.clean_text("a &amp; b &lt;c&gt;"),
    )

    check("空值返回空串", eudic.clean_text(None) == "" and eudic.clean_text("") == "")


# ── 3. 时间解析 ─────────────────────────────────────────────────────


def test_parse_iso_ms() -> None:
    print("\n[3] ISO8601 时间解析")

    value = eudic.parse_iso_ms("2026-06-16T20:32:49Z")
    check("带 Z 的时间能解析", value is not None and value > 0, str(value))
    check("解析出的是毫秒", value is not None and value > 1_000_000_000_000, str(value))
    check("空值返回 None", eudic.parse_iso_ms(None) is None)
    check("垃圾输入返回 None", eudic.parse_iso_ms("not-a-date") is None)


# ── 4. 错误归一 ─────────────────────────────────────────────────────


def test_error_mapping() -> None:
    print("\n[4] 欧路错误归一")

    fake = FakeHttp()
    fake.handler = lambda *a: FakeResponse(401, {"message": "token 无效"})
    with _patch_http(fake):
        try:
            eudic.list_categories("NIS bad")
            check("401 抛鉴权错误", False, "没有抛异常")
        except eudic.EudicAuthError as exc:
            check("401 抛鉴权错误", "token 无效" in str(exc), str(exc))
        except Exception as exc:  # noqa: BLE001
            check("401 抛鉴权错误", False, f"抛了 {type(exc).__name__}")

    fake.handler = lambda *a: FakeResponse(500, {"message": "服务器开小差了"})
    with _patch_http(fake):
        try:
            eudic.list_categories("NIS tok")
            check("5xx 抛接口错误", False, "没有抛异常")
        except eudic.EudicApiError as exc:
            check("5xx 抛接口错误", "服务器开小差了" in str(exc), str(exc))
        except Exception as exc:  # noqa: BLE001
            check("5xx 抛接口错误", False, f"抛了 {type(exc).__name__}")


# ── 4b. 网络抖动与重试 ──────────────────────────────────────────────


def test_network_retry() -> None:
    print("\n[4b] 网络抖动与重试")

    # 欧路的 TLS 连接有一半概率被对端掐断，重试要能救回来
    attempts = {"n": 0}

    def flaky(method, url, body, params):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("UNEXPECTED_EOF_WHILE_READING")
        return FakeResponse(200, {"data": [], "message": ""})

    fake = FakeHttp()
    fake.handler = flaky
    with _patch_http(fake), mock.patch.object(eudic.time, "sleep"):
        categories = eudic.list_categories("NIS tok")

    check("瞬时故障重试后成功", categories == [], categories)
    check("重试了正确的次数", len(fake.calls) == 3, len(fake.calls))

    # 一直失败 → 归一化成网络错误，不该再冒出 httpx 原生异常
    def always_fail(method, url, body, params):
        raise httpx.ConnectError("UNEXPECTED_EOF_WHILE_READING")

    fake2 = FakeHttp()
    fake2.handler = always_fail
    with _patch_http(fake2), mock.patch.object(eudic.time, "sleep"):
        try:
            eudic.list_categories("NIS tok")
            check("一直失败时抛网络错误", False, "没有抛异常")
        except eudic.EudicNetworkError as exc:
            check(
                "一直失败时抛网络错误",
                "已重试" in str(exc),
                str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            check("一直失败时抛网络错误", False, f"抛了 {type(exc).__name__}: {exc}")

    check(
        "网络错误也是 EudicError（路由层能统一接住）",
        issubclass(eudic.EudicNetworkError, eudic.EudicError),
    )

    # HTTP 层的错误不重试——欧路明确拒绝，重试只是浪费时间
    fake3 = FakeHttp()
    fake3.handler = lambda *a: FakeResponse(500, {"message": "服务器开小差了"})
    with _patch_http(fake3):
        try:
            eudic.list_categories("NIS tok")
            check("HTTP 错误不重试", False, "没有抛异常")
        except eudic.EudicApiError:
            check("HTTP 错误不重试", len(fake3.calls) == 1, len(fake3.calls))


# ── 5. 翻页聚合 ─────────────────────────────────────────────────────

def test_pagination() -> None:
    print("\n[5] 翻页聚合")

    page0 = [
        {"word": f"w{i}", "star": 1, "exp": "x", "add_time": "2026-01-01T00:00:00Z"}
        for i in range(100)
    ]
    page1 = [{"word": "last", "star": 1, "exp": "x", "add_time": "2026-01-01T00:00:00Z"}]

    def handler(method, url, body, params):
        page = params.get("page")
        if page == 0:
            return FakeResponse(200, {"data": page0, "message": ""})
        if page == 1:
            return FakeResponse(200, {"data": page1, "message": ""})
        return FakeResponse(200, {"data": [], "message": ""})

    fake = FakeHttp()
    fake.handler = handler
    with _patch_http(fake):
        words = list(eudic.iter_category_words("NIS tok", "0"))

    check("跨页拼接", len(words) == 101, len(words))
    check("第一页取满 100 条", len(fake.calls) == 2, len(fake.calls))
    check("第二页不满就停", fake.calls[1]["params"]["page"] == 1, fake.calls[1]["params"])
    check("page_size 用满 100", fake.calls[0]["params"]["page_size"] == 100)
    check("sort 固定 asc", fake.calls[0]["params"]["sort"] == "asc")


# ── 6. 同步 upsert 规则 ─────────────────────────────────────────────


def _row(word: str, **overrides) -> dict:
    row = {
        "word": word,
        "translation": "n. 释义",
        "context_text": "语境",
        "phonetic": "/test/",
        "star": 1,
        "created_at": 1_700_000_000_000,
        "source_language": "eng",
        "source_id": f"0:{word.lower()}",
        "source_category": "0",
    }
    row.update(overrides)
    return row


def test_upsert_rules() -> None:
    print("\n[6] 同步 upsert 规则")

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)

        # 新词
        result = vocabulary.upsert_eudic_words([_row("ephemeral")])
        check("新词建行", result["imported"] == 1 and result["updated"] == 0, result)

        words, total = vocabulary.list_words()
        check("表里有一条", total == 1, total)
        record = words[0]
        check("source 记为 eudic", record["source"] == "eudic", record["source"])
        check("source_id 正确", record["source_id"] == "0:ephemeral", record["source_id"])
        check("音标带过来了", record["phonetic"] == "/test/", record["phonetic"])
        check("源语言是 eng", record["source_language"] == "eng", record["source_language"])
        check("created_at 用欧路的", record["created_at"] == 1_700_000_000_000, record["created_at"])

        # 已有词：译文不覆盖；星级按 review_count 裁决
        result = vocabulary.upsert_eudic_words(
            [_row("ephemeral", translation="n. 别的释义", star=5, phonetic="/other/")]
        )
        check("已有词算 update", result["updated"] == 1 and result["imported"] == 0, result)
        record = vocabulary.list_words()[0][0]
        check("译文不被覆盖", record["translation"] == "n. 释义", record["translation"])
        check("音标不被覆盖", record["phonetic"] == "/test/", record["phonetic"])
        check(
            "没评过分的词采用欧路星级",
            record["star"] == 5,
            record["star"],
        )

        # 用户评过分 → 保留本地星级
        vocabulary.review_word(record["id"])
        result = vocabulary.upsert_eudic_words([_row("ephemeral", star=2)])
        record = vocabulary.list_words()[0][0]
        check("评过分的词保留本地星级", record["star"] == 5, record["star"])

        # 认领同名本地词
        local = vocabulary.add_word("Lucid", "adj. 清晰")
        result = vocabulary.upsert_eudic_words([_row("lucid", translation="adj. 清楚")])
        words, total = vocabulary.list_words()
        check("同名本地词不新建重复行", total == 2, total)
        adopted = next(w for w in words if w["id"] == local["id"])
        check("本地词被认领为 eudic", adopted["source"] == "eudic", adopted["source"])
        check("认领后补上 source_id", adopted["source_id"] == "0:lucid", adopted["source_id"])
        check("认领不覆盖本地译文", adopted["translation"] == "adj. 清晰", adopted["translation"])


# ── 7. 写回欧路 ─────────────────────────────────────────────────────


def test_write_back() -> None:
    print("\n[7] 写回欧路")

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)
        eudic.save_config(
            {"enabled": True, "categoryId": "0", "fallbackToken": "NIS test-token"}
        )

        fake = FakeHttp()
        fake.handler = lambda *a: FakeResponse(201, {"message": "单词添加成功"})
        with _patch_http(fake):
            eudic.push_star("ephemeral", 4)

        call = fake.last()
        check("改星级走 POST /word", call["method"] == "POST" and call["url"].endswith("/word"), call["url"])
        check("带上了 star", call["body"].get("star") == 4, call["body"])
        check("带上了语言", call["body"].get("language") == "en", call["body"])
        check(
            "改星级不传 context_line（传空会清掉语境）",
            "context_line" not in call["body"],
            call["body"],
        )
        check("带上了分组 id", call["body"].get("category_ids") == [0], call["body"])

        fake.calls.clear()
        with _patch_http(fake):
            eudic.push_delete("ephemeral")
        call = fake.last()
        check("删除走 DELETE /words", call["method"] == "DELETE" and call["url"].endswith("/words"), call["url"])
        check("删除带上了词", call["body"].get("words") == ["ephemeral"], call["body"])
        check("删除带上了分组", call["body"].get("category_id") == "0", call["body"])

        fake.calls.clear()
        with _patch_http(fake):
            eudic.push_new_word("ephemeral", 3, "语境原文")
        call = fake.last()
        check("新增走 POST /word", call["method"] == "POST" and call["url"].endswith("/word"), call["url"])
        check("新增带上了语境", call["body"].get("context_line") == "语境原文", call["body"])


# ── 8. 配置落库 ─────────────────────────────────────────────────────


def test_config() -> None:
    print("\n[8] 配置落库")

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)

        config = eudic.load_config()
        check("默认配置齐全", config["enabled"] is False and config["categoryId"] == "", config)

        eudic.save_config({"enabled": True, "categoryId": "0", "categoryName": "默认生词本"})
        config = eudic.load_config()
        check("保存后能读回", config["enabled"] is True and config["categoryId"] == "0", config)

        eudic.save_config({"syncNewWords": True})
        config = eudic.load_config()
        check("合并更新不丢旧字段", config["enabled"] is True and config["syncNewWords"] is True, config)

        eudic.save_config({"fallbackToken": "NIS secret-token-value"})

        public = eudic.public_config()
        blob = json.dumps(public)
        check(
            "对外的视图不含 token 明文",
            "secret-token-value" not in blob and "fallbackToken" not in public,
            blob,
        )
        check("对外的视图给出 token 来源", public["token_source"] in ("env", "settings", "none"), public)
        check("对外的视图说明有没有备用 token", public["has_fallback_token"] is True, public)


# ── 9. REST 接口 ────────────────────────────────────────────────────


def test_api() -> None:
    print("\n[9] REST 接口")

    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with tempfile.TemporaryDirectory() as tmp:
        _use_temp_db(tmp)
        eudic.save_config(
            {
                "enabled": True,
                "categoryId": "0",
                "fallbackToken": "NIS api-test-token",
            }
        )

        fake = FakeHttp()
        fake.handler = lambda *a: FakeResponse(
            200, {"data": [{"id": "0", "name": "默认生词本", "language": "en"}], "message": ""}
        )

        with _patch_http(fake):
            client = TestClient(fastapi_app)

            resp = client.get("/api/eudic/config")
            check("读配置可用", resp.status_code == 200, resp.text[:200])

            resp = client.put("/api/eudic/config", json={"enabled": True, "category_id": "0"})
            check("写配置可用", resp.status_code == 200 and resp.json()["enabled"] is True, resp.text[:200])

            resp = client.get("/api/eudic/categories")
            check("列生词本可用", resp.status_code == 200 and resp.json()["categories"][0]["name"] == "默认生词本", resp.text[:200])

            resp = client.post("/api/eudic/verify")
            check("验证连接可用", resp.status_code == 200 and resp.json()["ok"] is True, resp.text[:200])

            # 同步：欧路返回两个词
            def sync_handler(method, url, body, params):
                if url.endswith("/category"):
                    return FakeResponse(200, {"data": [{"id": "0", "name": "默认生词本", "language": "en"}], "message": ""})
                return FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "word": "ephemeral",
                                "star": 2,
                                "exp": "n. 短暂的<br>",
                                "phon": "/ɪˈfemərəl/",
                                "add_time": "2026-06-16T20:32:49Z",
                                "context_line": "a <b>ephemeral</b> joy",
                            }
                        ],
                        "message": "",
                    },
                )

            fake.handler = sync_handler
            resp = client.post("/api/eudic/sync")
            check("同步可用", resp.status_code == 200, resp.text[:200])

            words, total = vocabulary.list_words()
            check("同步把词写进了生词表", total == 1, total)
            check("释义里的 HTML 被洗掉了", words[0]["translation"] == "n. 短暂的", words[0]["translation"])
            check("语境里的 HTML 被洗掉了", words[0]["context_text"] == "a ephemeral joy", words[0]["context_text"])

            # 生词本列表：来源过滤
            resp = client.get("/api/vocabulary?source=eudic")
            check("按来源过滤可用", resp.json()["total"] == 1, resp.text[:200])
            resp = client.get("/api/vocabulary?source=local")
            check("按来源过滤（本地为空）", resp.json()["total"] == 0, resp.text[:200])
            resp = client.get("/api/vocabulary")
            check("不过滤时返回全部", resp.json()["total"] == 1, resp.text[:200])

            # 改星级 → 写回欧路
            word_id = words[0]["id"]
            fake.calls.clear()
            resp = client.patch(f"/api/vocabulary/{word_id}", json={"star": 5})
            check("改星级可用", resp.status_code == 200, resp.text[:200])
            check("响应带欧路同步结果", resp.json().get("eudic_sync", {}).get("ok") is True, resp.text[:300])
            check("改星级确实调了欧路", any(c["url"].endswith("/word") for c in fake.calls), fake.paths())

            # 删除 → 写回欧路
            fake.calls.clear()
            resp = client.delete(f"/api/vocabulary/{word_id}")
            check("删除可用", resp.status_code == 200, resp.text[:200])
            check("删除确实调了欧路", any(c["url"].endswith("/words") for c in fake.calls), fake.paths())

            # 欧路失败时，本地照样成功
            eudic_word = vocabulary.add_word("ephemeral2", "adj. 稍纵即逝的")
            vocabulary.set_word_source(
                eudic_word["id"], "eudic", "0:ephemeral2", "0"
            )
            fake.handler = lambda *a: FakeResponse(401, {"message": "token 无效"})
            resp = client.patch(f"/api/vocabulary/{eudic_word['id']}", json={"star": 4})
            body = resp.json()
            check(
                "欧路失败不阻断本地保存",
                resp.status_code == 200 and body["star"] == 4,
                resp.text[:200],
            )
            check(
                "欧路失败会报出来",
                body.get("eudic_sync", {}).get("ok") is False
                and "token 无效" in body["eudic_sync"].get("error", ""),
                resp.text[:300],
            )

            # 网络故障：返回 502 + 能看懂的原因，而不是裸的 500
            def dead_network(method, url, body, params):
                raise httpx.ConnectError("UNEXPECTED_EOF_WHILE_READING")

            fake.handler = dead_network
            with mock.patch.object(eudic.time, "sleep"):
                resp = client.post("/api/eudic/sync")
            check(
                "网络故障返回 502 而不是 500",
                resp.status_code == 502,
                f"{resp.status_code} {resp.text[:200]}",
            )
            check(
                "网络故障带中文原因",
                "连接欧路接口失败" in resp.text,
                resp.text[:200],
            )


def main() -> int:
    test_token_resolution()
    test_clean_text()
    test_parse_iso_ms()
    test_error_mapping()
    test_network_retry()
    test_pagination()
    test_upsert_rules()
    test_write_back()
    test_config()
    test_api()

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败项:", FAILURES)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
