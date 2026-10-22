"""欧路（Eudic）生词本接入

对接 ``https://api.frdic.com/api/open/v1/studylist``：把欧路生词本里的词
同步进本地生词表，并把本地的星级变更/删除写回欧路。

接口行为是 2026-10 用真实账号实测出来的（文档里没写清），几条要紧的：

- ``POST /studylist/word`` 对**已收藏**的词也是 upsert，能改星级。所以
  写回星级就它一个接口，不用碰 ``POST /studylist/words``（那个会把词
  追加进指定的分组，是个副作用）。
- 省略 ``context_line`` 时原值保留；传空会把语境清掉。所以改星级时
  **不要**带 ``context_line``。
- 任何写操作都会刷新 ``add_time``。所以同步时绝不能覆盖已有行的
  ``created_at``，否则「按添加时间排序」会被写回操作打乱。
- ``exp`` 和 ``context_line`` 里带 HTML（``<br>`` / ``<b>``），得清洗。
- 手动推上去的词，欧路那边 ``exp`` 是空字符串。

Token 从环境变量 ``EUDIT_API_KEY`` 读（用户级环境变量，后端进程继承），
设置页里存的备用 token 排第二。
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import ssl
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from app.vocabulary import DB_FILE, upsert_eudic_words

# ── 常量 ────────────────────────────────────────────────────────────

EUDIC_API_BASE = "https://api.frdic.com/api/open/v1/studylist"

#: 欧路只支持这几种语言，而账号里只有一个英语生词本。
LANGUAGE = "en"
LANGUAGE_TO_ISO6393 = {"en": "eng"}

#: 环境变量名（按顺序取第一个非空的）。用户设的是 ``EUDIT_API_KEY``。
TOKEN_ENV_NAMES = ("EUDIT_API_KEY", "EUDIC_API_KEY")

#: 欧路分页边界：page 从 0 开始，最大 50；page_size 最大 100。
MAX_PAGE = 50
PAGE_SIZE = 100

#: 同步时被当作「同一个词」的欧路侧唯一键模板。
def make_source_id(category_id: str, word: str) -> str:
    return f"{category_id}:{word.strip().lower()}"


# ── 错误 ────────────────────────────────────────────────────────────


class EudicError(Exception):
    """欧路调用失败的基类。"""


class EudicAuthError(EudicError):
    """token 无效/没配置/权限不足。"""


class EudicApiError(EudicError):
    """欧路返回了错误状态。"""


class EudicNetworkError(EudicError):
    """连不上欧路（网络抖动、超时等）。

    单独一类，是为了让它跟「欧路明确拒绝」区分开：前者值得重试，后者
    重试只是浪费时间。
    """


# ── 配置存储 ────────────────────────────────────────────────────────

_CONFIG_KEY = "config"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS eudic_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def default_config() -> dict[str, Any]:
    """欧路接入的默认配置。"""
    return {
        "enabled": False,
        # 环境变量没生效时的兜底 token（一般用不上）
        "fallbackToken": "",
        "categoryId": "",
        "categoryName": "",
        "syncNewWords": False,
        "lastSyncedAt": 0,
    }


def _ensure_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.executescript(_CREATE_SQL)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    _ensure_db()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def load_config() -> dict[str, Any]:
    """读取配置；从未配置过时返回默认配置。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM eudic_settings WHERE key = ?", (_CONFIG_KEY,)
        ).fetchone()

    if not row:
        return default_config()

    try:
        stored = json.loads(row["value"])
    except (TypeError, ValueError):
        return default_config()

    if not isinstance(stored, dict):
        return default_config()

    merged = default_config()
    merged.update(stored)
    return merged


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    """合并并保存配置，返回保存后的完整配置。"""
    config = load_config()
    config.update(updates)

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO eudic_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_CONFIG_KEY, json.dumps(config, ensure_ascii=False)),
        )
        conn.commit()

    return config


# ── Token ───────────────────────────────────────────────────────────


def resolve_token() -> tuple[Optional[str], str]:
    """返回 (token, 来源)。

    来源取值 ``env`` / ``settings`` / ``none``。环境变量优先——它不在
    数据库里，也不会被导出配置带走。
    """
    for name in TOKEN_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, "env"

    stored = (load_config().get("fallbackToken") or "").strip()
    if stored:
        return stored, "settings"

    return None, "none"


def _require_token() -> str:
    token, _ = resolve_token()
    if not token:
        raise EudicAuthError(
            "未配置欧路 token：请设置环境变量 EUDIT_API_KEY，或在设置页填入备用 token"
        )
    return token


# ── 文本清洗 ────────────────────────────────────────────────────────

_BR_RE = re.compile(r"(?i)<br\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_NEWLINE_RE = re.compile(r"\s*\n\s*")


def clean_text(value: Optional[str]) -> str:
    """去掉欧路释义/语境里的 HTML 标签。

    欧路返回的 ``exp`` 长这样：
    ``vt. 呕吐；催吐剂<br><br>时 态: vomited, vomiting<br>名 词: vomiter<br>``
    ``context_line`` 里则是 ``not through <b>manifestation</b> or magical
    thinking.``。直接存进去，闪卡背面就会显示原始标签。
    """
    if not value:
        return ""

    text = _BR_RE.sub(" ", value)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _SPACE_RE.sub(" ", text)
    text = _NEWLINE_RE.sub("\n", text)
    return text.strip()


def parse_iso_ms(value: Optional[str]) -> Optional[int]:
    """把欧路的 ISO8601 时间转成毫秒时间戳。"""
    if not value:
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


# ── HTTP 客户端 ─────────────────────────────────────────────────────

#: 代理从标准环境变量读，但**不用 httpx 自己的解析**：它的 NO_PROXY 解析
#: 碰到 ``[::1]`` 这种带方括号的 IPv6 会抛 ``InvalidURL: Invalid port``。
#: 自己取出 URL 直接塞给 httpx 的 ``proxy=`` 参数，绕开那个坑。
PROXY_ENV_NAMES = (
    "EUDIC_PROXY",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

#: 一次请求最多试几次（含首次）。
#:
#: 实测这台机器连 ``api.frdic.com`` 时，TLS 连接有大约一半的概率被对端
#: 直接掐断（``UNEXPECTED_EOF_WHILE_READING`` / ``ConnectionReset``），
#: 直连和走代理都一样——是服务端的抖动。重试几次就能救回来。
MAX_ATTEMPTS = 4

#: 重试退避的基数（秒），按次数指数增长。
RETRY_BACKOFF_SECONDS = 0.3

#: 连接超时单独调短：掐断的连接有时不是立刻断，而是挂到读超时才报，
#: 那会把一次失败的同步拖成十几秒。握手卡住的话快速失败、赶紧重试。
CONNECT_TIMEOUT = 8.0
READ_TIMEOUT = 20.0


def _tls_max_version() -> ssl.TLSVersion:
    """欧路请求的 TLS 上限，默认锁在 1.2。

    这条链路上有中间设备处理不了 OpenSSL 的 TLS 1.3 ClientHello：握手
    会被直接掐断，报 ``UNEXPECTED_EOF_WHILE_READING``。实测同一个请求，
    允许 1.3 时成功率只有 30%，把上限压到 1.2 之后稳定 100%。

    .NET（浏览器、PowerShell）不受影响——它们的 ClientHello 长得不一样，
    所以同一个地址在浏览器里好使、在 Python 里却抽风。

    想放开的话设 ``EUDIC_TLS_MAX=1.3``。
    """
    if (os.environ.get("EUDIC_TLS_MAX") or "").strip() == "1.3":
        return ssl.TLSVersion.TLSv1_3
    return ssl.TLSVersion.TLSv1_2


def _ssl_context() -> ssl.SSLContext:
    """给欧路请求单独准备的 TLS 上下文（证书照常校验）。"""
    context = ssl.create_default_context()
    context.maximum_version = _tls_max_version()
    return context


def resolve_proxy() -> Optional[str]:
    """从环境变量里取代理地址，没有就返回 None（直连）。"""
    for name in PROXY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"NIS {token}",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }


def _check_response(response: httpx.Response) -> None:
    """把欧路的错误响应翻成异常。"""
    if response.status_code < 400:
        return

    message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
    except ValueError:
        message = ""

    if response.status_code in (401, 403):
        raise EudicAuthError(message or "token 无效或已过期")

    raise EudicApiError(
        f"欧路接口返回 HTTP {response.status_code}：{message or response.text[:200]}"
    )


def _request(
    method: str,
    path: str,
    token: str,
    *,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """发一个请求，瞬时网络错误自动重试。

    只重试传输层的错误（连接被掐断、超时、协议错误）；HTTP 层的 4xx/5xx
    是欧路明确拒绝，重试只会浪费时间，所以立刻抛出去。
    """
    proxy = resolve_proxy()
    last_error: Optional[Exception] = None
    verify = _ssl_context()

    for attempt in range(MAX_ATTEMPTS):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    connect=CONNECT_TIMEOUT,
                    read=READ_TIMEOUT,
                    write=READ_TIMEOUT,
                    pool=CONNECT_TIMEOUT,
                ),
                trust_env=False,
                verify=verify,
                headers=_headers(token),
                **({"proxy": proxy} if proxy else {}),
            ) as client:
                response = client.request(
                    method,
                    f"{EUDIC_API_BASE}{path}",
                    params=params,
                    content=(
                        json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        if payload is not None
                        else None
                    ),
                )

            _check_response(response)

            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}

        except httpx.TransportError as exc:
            # 连接被对端掐断之类的瞬时故障——欧路那边这个接口就是会抖，
            # 重试几次基本都能成。
            last_error = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
                continue
            raise EudicNetworkError(
                f"连接欧路接口失败（已重试 {MAX_ATTEMPTS} 次）：{exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise EudicNetworkError(f"连接欧路接口失败：{exc}") from exc

    raise EudicNetworkError(f"连接欧路接口失败：{last_error}")  # pragma: no cover


def _get(
    path: str, token: str, params: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    return _request("GET", path, token, params=params)


def _post(
    path: str, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return _request("POST", path, token, payload=payload)


def _delete(
    path: str, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return _request("DELETE", path, token, payload=payload)


# ── 欧路接口 ────────────────────────────────────────────────────────


def list_categories(token: str) -> list[dict[str, Any]]:
    """列出账号里的生词本。"""
    data = _get("/category", token, {"language": LANGUAGE})
    items = data.get("data") or []
    return [item for item in items if isinstance(item, dict)]


def list_words(
    token: str, category_id: str, page: int = 0, page_size: int = PAGE_SIZE
) -> list[dict[str, Any]]:
    """取一页生词本里的词。"""
    data = _get(
        "/words",
        token,
        {
            "language": LANGUAGE,
            "category_id": category_id,
            "page": page,
            "page_size": page_size,
            "sort": "asc",
        },
    )
    items = data.get("data") or []
    return [item for item in items if isinstance(item, dict)]


def iter_category_words(token: str, category_id: str) -> Iterator[dict[str, Any]]:
    """翻页取全本生词。

    欧路的 page 上限是 50、page_size 上限 100，所以单本最多 5000 词；
    到了上限就停（多出来的取不到）。
    """
    for page in range(MAX_PAGE + 1):
        words = list_words(token, category_id, page=page)
        if not words:
            return
        yield from words
        if len(words) < PAGE_SIZE:
            return


def get_word(token: str, word: str) -> dict[str, Any]:
    """查一个词在欧路里的详情。"""
    data = _get("/word", token, {"language": LANGUAGE, "word": word})
    return data if isinstance(data, dict) else {}


def _category_ids(category_id: Optional[str]) -> Optional[list[Any]]:
    """把生词本 id 转成欧路要的 ``category_ids`` 形式。

    欧路那边默认分组的 id 是整数 ``0``（``GET /word`` 返回的就是
    ``[0]``），所以纯数字的 id 转成整数再发。
    """
    if not category_id:
        return None
    if category_id.isdigit():
        return [int(category_id)]
    return [category_id]


def upsert_word(
    token: str,
    word: str,
    star: Optional[int] = None,
    context_line: Optional[str] = None,
    category_id: Optional[str] = None,
) -> dict[str, Any]:
    """新增/更新一个词（欧路对已收藏的词也是 upsert）。

    ``context_line`` 省略时保留原值；传空会把语境清掉，所以改星级时
    不要带这个参数。
    """
    payload: dict[str, Any] = {"language": LANGUAGE, "word": word}
    if star is not None:
        payload["star"] = int(star)
    if context_line is not None:
        payload["context_line"] = context_line

    category_ids = _category_ids(category_id)
    if category_ids is not None:
        payload["category_ids"] = category_ids

    return _post("/word", token, payload)


def delete_words(token: str, category_id: str, words: list[str]) -> dict[str, Any]:
    """从生词本里删除词。"""
    return _delete(
        "/words",
        token,
        {"language": LANGUAGE, "category_id": category_id, "words": words},
    )


# ── 写回欧路 ────────────────────────────────────────────────────────


def config_category_id() -> str:
    """当前配置的生词本 id（没配置时用默认分组 ``0``）。"""
    return str(load_config().get("categoryId") or "0")


def push_star(word: str, star: int) -> None:
    """把星级写回欧路。"""
    token = _require_token()
    upsert_word(token, word, star=star, category_id=config_category_id())


def push_delete(word: str) -> None:
    """把删除写回欧路。"""
    token = _require_token()
    delete_words(token, config_category_id(), [word])


def push_new_word(word: str, star: int, context_line: Optional[str] = None) -> None:
    """把本地新收藏的词加进欧路。"""
    token = _require_token()
    upsert_word(
        token,
        word,
        star=star,
        context_line=context_line or "",
        category_id=config_category_id(),
    )


# ── 同步 ────────────────────────────────────────────────────────────


def sync_new_words_enabled() -> bool:
    """是否要把本地新收藏的词推给欧路。"""
    return bool(load_config().get("syncNewWords"))


def build_rows(words: list[dict[str, Any]], category_id: str) -> list[dict[str, Any]]:
    """把欧路返回的词整理成 ``upsert_eudic_words`` 要的行。"""
    rows: list[dict[str, Any]] = []
    for item in words:
        word = (item.get("word") or "").strip()
        if not word:
            continue

        try:
            star = int(item.get("star") or 1)
        except (TypeError, ValueError):
            star = 1

        rows.append(
            {
                "word": word,
                "translation": clean_text(item.get("exp")),
                "context_text": clean_text(item.get("context_line")),
                "phonetic": (item.get("phon") or "").strip(),
                "star": max(1, min(5, star)),
                "created_at": parse_iso_ms(item.get("add_time")),
                "source_language": LANGUAGE_TO_ISO6393.get(LANGUAGE),
                "source_id": make_source_id(category_id, word),
                "source_category": category_id,
            }
        )
    return rows


def resolve_category(token: str) -> tuple[str, str]:
    """确定要同步哪个生词本，返回 (id, name)。

    账号里只有一个生词本，所以配置没写就直接用第一个。
    """
    config = load_config()
    category_id = str(config.get("categoryId") or "")
    if category_id:
        return category_id, str(config.get("categoryName") or "")

    categories = list_categories(token)
    if not categories:
        raise EudicApiError("欧路账号里没有生词本")

    first = categories[0]
    category_id = str(first.get("id") or "")
    name = str(first.get("name") or "")
    save_config({"categoryId": category_id, "categoryName": name})
    return category_id, name


def sync_from_eudic() -> dict[str, Any]:
    """把欧路生词本同步进本地生词表。"""
    config = load_config()
    if not config.get("enabled"):
        return {
            "skipped": "未启用欧路同步",
            "imported": 0,
            "updated": 0,
            "skipped_count": 0,
            "total": 0,
        }

    token = _require_token()
    category_id, category_name = resolve_category(token)

    words = list(iter_category_words(token, category_id))
    rows = build_rows(words, category_id)
    result = upsert_eudic_words(rows)

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    save_config({"lastSyncedAt": now, "categoryId": category_id, "categoryName": category_name})

    return {
        "skipped": "",
        "imported": result["imported"],
        "updated": result["updated"],
        "skipped_count": result["skipped"],
        "total": len(rows),
        "category_id": category_id,
        "category_name": category_name,
        "synced_at": now,
    }


def public_config() -> dict[str, Any]:
    """给前端看的配置（不含 token 明文）。"""
    config = load_config()
    _, token_source = resolve_token()
    return {
        "enabled": bool(config.get("enabled")),
        "token_source": token_source,
        "has_fallback_token": bool((config.get("fallbackToken") or "").strip()),
        "category_id": str(config.get("categoryId") or ""),
        "category_name": str(config.get("categoryName") or ""),
        "sync_new_words": bool(config.get("syncNewWords")),
        "last_synced_at": int(config.get("lastSyncedAt") or 0),
        "language": LANGUAGE,
    }
