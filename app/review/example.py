"""例句生成

复习提醒最好带一句新鲜例句——只有「hello / 你好」的通知没有记忆价值。

生成走可选的 OpenAI 兼容接口（配置里填 base_url / api_key / model 即可）；
没配 LLM、或者调用失败时，会退回收藏这个词时抓到的上下文句子，再不行就
只显示生词和翻译。**任何一步都不能让推送失败**：例句只是锦上添花。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Optional

import httpx

from app.review.constants import (
    EXAMPLE_COLLOCATION_LIMIT,
    EXAMPLE_LLM_TIMEOUT_SECONDS,
    OPENROUTER_HOST,
    OPENROUTER_REFERER,
    OPENROUTER_TITLE,
    REVIEW_EXAMPLE_ANGLES,
)

log = logging.getLogger("app.review.example")

#: 记录上一个推送用过的角度，避免连续两次用同一个场景。
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = (
    "You are an English teacher helping a Chinese learner build vocabulary. "
    "You always answer with a single JSON object and nothing else."
)

_USER_PROMPT_TEMPLATE = """\
Target word: {word}
Dictionary translation: {translation}
{context_line}
Usage angle: {angle}

Write ONE short natural English sentence that uses the target word in a way that
fits the usage angle, and that differs from the previous example.

Answer with a single JSON object using exactly these keys:
{{
  "sentence": "the English sentence",
  "sentence_chinese": "该句子的中文翻译",
  "collocations": [
    {{"text": "reach a verdict", "chinese": "作出裁决"}}
  ]
}}

Rules:
- The sentence must contain the target word exactly as written.
- Keep it under 25 words.
- "collocations" must contain 1-5 common collocations of the target word.
- Each collocation needs a short "chinese" note explaining what it means.
- No markdown, no explanation, JSON only.
"""


def _strip_code_fence(text: str) -> str:
    """去掉模型可能裹上的 ```json 围栏。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
    return cleaned.strip()


def parse_llm_payload(raw: str) -> Optional[dict[str, Any]]:
    """从模型输出里解析 JSON；解析不了就返回 ``None``。"""
    if not raw:
        return None

    candidate = _strip_code_fence(raw)
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


def pick_angle(push_count: int) -> str:
    """按推送次数轮换使用场景。"""
    if push_count <= 0:
        return REVIEW_EXAMPLE_ANGLES[0]
    return REVIEW_EXAMPLE_ANGLES[push_count % len(REVIEW_EXAMPLE_ANGLES)]


def normalize_collocations(raw: Any) -> list[dict[str, str]]:
    """把搭配统一成 ``[{"text", "chinese"}]``。

    ``chinese`` 是这个搭配的中文意思——只背搭配不知道意思等于没背。
    兼容两种形状：新版模型输出带 ``chinese`` 的对象；老库、老输出里的纯
    字符串意思留空，页面只显示搭配本身。
    """
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, Mapping):
            text = str(item.get("text") or "").strip()
            chinese = str(item.get("chinese") or "").strip()
        else:
            text = str(item or "").strip()
            chinese = ""
        if text:
            normalized.append({"text": text, "chinese": chinese})
        if len(normalized) >= EXAMPLE_COLLOCATION_LIMIT:
            break
    return normalized


def _llm_config(config: Mapping[str, Any]) -> tuple[str, str, str]:
    llm = config.get("llm") or {}
    return (
        str(llm.get("base_url") or "").strip(),
        str(llm.get("api_key") or "").strip(),
        str(llm.get("model") or "").strip(),
    )


def _llm_headers(api_key: str, base_url: str) -> dict[str, str]:
    """LLM 请求头。

    请求 OpenRouter 时额外带上 ``HTTP-Referer`` / ``X-Title`` 两个归属头
    （官方推荐，用于在 OpenRouter 后台展示调用来源）；其他提供商不认识
    这两个头，就不带。
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if OPENROUTER_HOST in base_url.lower():
        headers["HTTP-Referer"] = OPENROUTER_REFERER
        headers["X-Title"] = OPENROUTER_TITLE
    return headers


#: 代理从标准环境变量读，但**不用 httpx 自己的解析**：它的 NO_PROXY 解析
#: 碰到 ``[::1]`` 这种带方括号的 IPv6 会抛 ``InvalidURL: Invalid port``——
#: 不是请求失败，是 Client 都构造不出来，请求根本发不出去（``app/eudic.py``
#: 里有同一个坑的长注释）。自己取出 URL 直接塞给 ``proxy=`` 参数绕开。
PROXY_ENV_NAMES = (
    "MDX_LLM_PROXY",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


def _resolve_proxy() -> Optional[str]:
    """从环境变量里取代理地址，没有就返回 None（直连）。"""
    for name in PROXY_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _request_example(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
) -> Optional[dict[str, Any]]:
    """调用 OpenAI 兼容接口拿例句。"""
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"

    headers = _llm_headers(api_key, base_url)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.9,
    }

    # trust_env=False + 自己解析代理：httpx 默认会读 NO_PROXY 环境变量，而这台
    # 机器的 NO_PROXY 里带方括号 IPv6（``[::1]``），构造 Client 时直接抛
    # InvalidURL，例句请求永远发不出去。
    proxy = _resolve_proxy()

    try:
        with httpx.Client(
            timeout=EXAMPLE_LLM_TIMEOUT_SECONDS,
            trust_env=False,
            **({"proxy": proxy} if proxy else {}),
        ) as client:
            response = client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 —— LLM 挂了不能影响推送
        log.warning("调用 LLM 生成例句失败：%s", exc)
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.warning("LLM 返回结构异常，无法取到例句")
        return None

    return parse_llm_payload(content)


def build_example(
    entry: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """给一个词条准备例句。

    返回 ``{"sentence", "sentence_chinese", "collocations", "angle", "source"}``，
    其中 ``collocations`` 是 ``[{"text", "chinese"}]``（搭配 + 中文意思），
    ``source`` 取值 ``llm`` / ``context`` / ``bare``。
    """
    word = str(entry.get("word", ""))
    translation = str(entry.get("translation", ""))
    context_text = str(entry.get("context_text") or "").strip()
    # GoldenDict 抓到的「上下文」有时就是生词本身，那当例句毫无意义。
    if context_text.lower() == word.lower():
        context_text = ""
    angle = pick_angle(int(entry.get("push_count", 0) or 0))

    if config.get("generate_example", True):
        base_url, api_key, model = _llm_config(config)
        if base_url and model:
            context_line = (
                f"Sentence the learner collected this word in: {context_text}"
                if context_text
                else ""
            )
            prompt = _USER_PROMPT_TEMPLATE.format(
                word=word,
                translation=translation.replace("\n", " / "),
                context_line=context_line,
                angle=angle,
            )
            parsed = _request_example(base_url, api_key, model, prompt)
            if parsed:
                sentence = str(parsed.get("sentence") or "").strip()
                if sentence:
                    return {
                        "sentence": sentence,
                        "sentence_chinese": str(
                            parsed.get("sentence_chinese") or ""
                        ).strip(),
                        "collocations": normalize_collocations(
                            parsed.get("collocations")
                        ),
                        "angle": angle,
                        "source": "llm",
                    }

    if context_text:
        return {
            "sentence": context_text,
            "sentence_chinese": "",
            "collocations": [],
            "angle": angle,
            "source": "context",
        }

    return {
        "sentence": "",
        "sentence_chinese": "",
        "collocations": [],
        "angle": angle,
        "source": "bare",
    }
