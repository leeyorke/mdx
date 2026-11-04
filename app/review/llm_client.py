"""OpenAI 兼容 LLM 客户端（例句生成与写作评卷共用的传输层）

只负责「发请求、拿回 content 文本」，不碰任何业务语义：

- 例句（``app/review/example.py``）要的是例句 + 中文翻译 + 固定搭配；
- 写作评卷（``app/review/writing.py``）要的是分数 + 逐条批改 + 用词覆盖。

两者读的是同一份配置（``review_settings.llm``：``base_url`` / ``api_key`` /
``model``），区别只在 prompt 与 temperature——所以传输层抽到这里，谁也别复制
代理与归属头那套逻辑。

设计口径与项目既有约束一致：**任何失败都返回 ``None``，绝不抛出**。例句是复习
的锦上添花，评卷是检验环节的加分项，都不该因为「LLM 挂了 / 没配 / 超时」把
调用方（复习卡片、检验页面）一起拖下水。调用方拿 ``None`` 自己走降级路径。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Optional

import httpx

from app.review.constants import (
    OPENROUTER_HOST,
    OPENROUTER_REFERER,
    OPENROUTER_TITLE,
)

log = logging.getLogger("app.review.llm_client")

#: 例句生成的超时时间（秒）。例句只重写一句话，正常几秒就回，20s 已很宽裕。
#: 评卷用的是下面单独的 ``WRITING_GRADE_TIMEOUT_SECONDS``，不走这个值。
LLM_TIMEOUT_SECONDS = 20.0

#: 写作评卷的超时时间（秒）。评卷要让模型读一篇短文、逐词判覆盖、逐句挑语法错——
#: 输出一坨结构化 JSON。实测当前推理型端点（要憋内部推理）单次耗时剧烈波动：
#: 112s / 145s / 146s / 172s 都出现过，20s 必超时（表现为「本次调用失败，仅用词
#: 覆盖统计」），90s / 200s 也仍会偶发超。慢的是模型「只输出 JSON」时的内部推理
#: 本身——砍掉润色稿等输出块几乎不提速（146s vs 145s），所以只能靠拉长超时硬扛。
#: 给到 300s 覆盖观测到的最慢值。评卷在线程池里跑、不占事件循环，不会拖垮其它
#: 请求；代价是这次评卷页面要转圈两三分钟。要快就换一个非推理型 / 更快的评卷端点。
WRITING_GRADE_TIMEOUT_SECONDS = 300.0

#: 从模型输出里捞 JSON 对象的兜底正则：模型偶尔会无视「只输出 JSON」的要求，
#: 在前后裹上说明文字。先整段解析，失败再退到「抓第一个 {...}」。
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

#: 请求 OpenRouter 时额外带上的应用归属头（官方推荐，用于在后台展示调用来源）。
#: 别的提供商不认识这两个头，就不带（见 ``_llm_headers``）。
_OPENROUTER_HOST = OPENROUTER_HOST

# 代理从标准环境变量读，但**不用 httpx 自己的解析**：它的 NO_PROXY 解析碰到
# ``[::1]`` 这种带方括号的 IPv6 会抛 ``InvalidURL: Invalid port``——不是请求
# 失败，是 Client 都构造不出来，请求根本发不出去（``app/eudic.py`` 里有同一
# 个坑的长注释）。自己取出 URL 直接塞给 ``proxy=`` 参数绕开。
PROXY_ENV_NAMES = (
    "MDX_LLM_PROXY",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


def resolve_proxy() -> Optional[str]:
    """从环境变量里取代理地址，没有就返回 ``None``（直连）。"""
    for name in PROXY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def llm_config(config: Mapping[str, Any]) -> tuple[str, str, str]:
    """从复习配置里取出 ``(base_url, api_key, model)``。

    例句与评卷共用同一份 LLM 配置——评卷不单独配模型（这是个明确的取舍：
    零配置改动、开箱即用；代价是不能给评卷单独指一个更强的模型）。
    """
    llm = config.get("llm") or {}
    return (
        str(llm.get("base_url") or "").strip(),
        str(llm.get("api_key") or "").strip(),
        str(llm.get("model") or "").strip(),
    )


def is_configured(config: Mapping[str, Any]) -> bool:
    """LLM 是否可用（``base_url`` 与 ``model`` 都填了才真正能发请求）。"""
    base_url, _, model = llm_config(config)
    return bool(base_url and model)


def _llm_headers(api_key: str, base_url: str) -> dict[str, str]:
    """LLM 请求头：带了 key 就加 Bearer；OpenRouter 额外加两个归属头。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if _OPENROUTER_HOST in base_url.lower():
        headers["HTTP-Referer"] = OPENROUTER_REFERER
        headers["X-Title"] = OPENROUTER_TITLE
    return headers


def _endpoint(base_url: str) -> str:
    """把配置里的 ``base_url`` 补全成 ``/chat/completions`` 地址。

    用户在设置里可能只填到 ``https://host/v1``，也可能把完整路径填全了；
    两种都容忍，别让人去数斜杠。
    """
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    return endpoint


def chat(
    config: Mapping[str, Any],
    *,
    system: str,
    user: str,
    temperature: float = 0.2,
    timeout: float = LLM_TIMEOUT_SECONDS,
) -> Optional[str]:
    """调一次 OpenAI 兼容 ``/chat/completions``，返回 assistant 文本。

    返回 ``None`` 的情况（调用方都当成「这次没成，走降级」）：

    - 没配 ``base_url`` / ``model``；
    - 网络错误、TLS 失败、超时；
    - HTTP 4xx / 5xx；
    - 返回体结构异常（取不到 ``choices[0].message.content``）。

    不校验业务语义——文本拿到就返回，解析交给调用方（例句要例句对象，评卷
    要分数与批改数组，各自用 :func:`parse_json_payload` 兜底）。

    ``timeout`` 让调用方按请求重量自定：例句只生成一句话，默认 20s 足够；
    评卷要读一篇短文再吐一大坨结构化 JSON，慢端点（推理型模型憋内部推理）
    能跑到 45–65s，得单独放宽，否则永远卡在「本次调用失败」。
    """
    base_url, api_key, model = llm_config(config)
    if not (base_url and model):
        log.debug("LLM 未配置（缺 base_url 或 model），跳过调用")
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    proxy = resolve_proxy()

    try:
        with httpx.Client(
            timeout=timeout,
            trust_env=False,  # 别让 httpx 自己解析代理/NO_PROXY（见上）
            **({"proxy": proxy} if proxy else {}),
        ) as client:
            response = client.post(
                _endpoint(base_url),
                json=payload,
                headers=_llm_headers(api_key, base_url),
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 —— LLM 挂了不能拖垮调用方
        log.warning("调用 LLM 失败：%s", exc)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.warning("LLM 返回结构异常，取不到 content")
        return None

    if not isinstance(content, str):
        return None
    return content


def strip_code_fence(text: str) -> str:
    """去掉模型可能裹上的 ```` ```json ```` 围栏。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
    return cleaned.strip()


def parse_json_payload(raw: Optional[str]) -> Optional[dict[str, Any]]:
    """从模型输出里解析 JSON 对象；解析不了返回 ``None``。

    容忍三种形态：裸 JSON、带 ```` ```json ```` 围栏、JSON 前后混了说明文字。
    这是例句与评卷共用的解析兜底——模型输出畸形不能变成 500，只能变成
    「这一条没解析出来」，由调用方决定怎么降级。
    """
    if not raw:
        return None

    candidate = strip_code_fence(raw)
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        match = _JSON_BLOCK_RE.search(candidate)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except (TypeError, ValueError):
            return None

    if not isinstance(parsed, dict):
        return None
    return parsed
