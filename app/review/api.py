"""复习提醒的 REST 接口 + 复习页面

页面就在 ``/review``：Windows 通知点击后用默认浏览器打开这个地址，
就能复习或改配置，不需要浏览器扩展在场。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.review import service
from app.review.notifier import send_test_notification

log = logging.getLogger("app.review.api")

router = APIRouter(tags=["Review"])

STATIC_DIR = Path(__file__).resolve().parent / "static"


# ── Schemas ─────────────────────────────────────────────────────────


class ReviewConfigPayload(BaseModel):
    """复习提醒配置（只需传要改的字段）。"""

    enabled: Optional[bool] = Field(None, description="是否开启复习提醒")
    generate_example: Optional[bool] = Field(
        None, description="是否用 LLM 生成例句（关闭则用收藏时的上下文）"
    )
    max_per_day: Optional[int] = Field(
        None, ge=1, le=50, description="每天最多推送多少条"
    )
    max_per_word_per_day: Optional[int] = Field(
        None, ge=1, le=10, description="每个词每天最多推送多少条"
    )
    graduated_action: Optional[str] = Field(
        None, description="走完整个阶梯之后怎么办：stop | restart"
    )
    quiet_hours: Optional[dict] = Field(
        None, description="免打扰时段：{enabled, start, end}"
    )
    open_url_template: Optional[str] = Field(
        None, description="通知点击后打开的地址模板，{word_id} 会被替换"
    )
    page_base: Optional[str] = Field(None, description="后端页面地址")
    llm: Optional[dict] = Field(
        None, description="LLM 配置：{base_url, api_key, model}"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "enabled": True,
                    "max_per_day": 8,
                    "max_per_word_per_day": 3,
                    "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
                    "open_url_template": "http://127.0.0.1:4080/review?word={word_id}",
                }
            ]
        }
    }


class RatePayload(BaseModel):
    """一次评分。"""

    word_id: str = Field(..., description="生词 ID")
    kind: str = Field(
        ...,
        description="评分：remembered（记得）| forgotten（忘了）",
        examples=[{"kind": "remembered"}],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"word_id": "550e8400-e29b-41d4-a716-446655440000", "kind": "remembered"}
            ]
        }
    }


# ── 状态 / 配置 ─────────────────────────────────────────────────────


@router.get(
    "/api/review/status",
    summary="复习提醒状态",
    description="返回队列大小、今日推送数、下次到期时间、通知是否可用等状态。",
)
async def get_review_status():
    return service.get_status()


@router.get(
    "/api/review/config",
    summary="读取复习提醒配置",
    description="返回当前生效的复习提醒配置。",
)
async def get_review_config():
    return service.get_config()


@router.put(
    "/api/review/config",
    summary="更新复习提醒配置",
    description="按字段合并更新复习提醒配置，只需传要改的字段。",
)
async def put_review_config(payload: ReviewConfigPayload = Body(...)):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="请提供至少一个更新字段")
    return service.update_config(updates)


# ── 队列 ────────────────────────────────────────────────────────────


@router.post(
    "/api/review/sync",
    summary="同步复习队列",
    description="用生词本里「至少复习过一次」的词对齐复习队列。",
)
async def sync_review_queue():
    return service.sync_review_queue()


@router.post(
    "/api/review/tick",
    summary="立即跑一轮调度",
    description="立刻执行一次调度：把到点的词推成 Windows 通知。免打扰与每日配额照常生效。",
)
async def run_review_tick():
    return service.run_review_tick()


@router.delete(
    "/api/review/queue",
    summary="清空复习队列",
    description="清空全部复习进度（不影响生词本）。",
)
async def clear_review_queue():
    service.clear_queue()
    return {"message": "复习队列已清空"}


# ── 复习卡片 ────────────────────────────────────────────────────────


@router.get(
    "/api/review/card",
    summary="取一张复习卡片",
    description="返回一张待复习的卡片；不传 word_id 时接着上一次的会话往下走。",
)
async def get_review_card(
    word_id: Optional[str] = Query(
        None, description="指定要复习的生词 ID（通知点进来时带上）"
    ),
):
    card = service.get_card(word_id)
    if card is None:
        return None
    return card


@router.post(
    "/api/review/rate",
    summary="提交复习评分",
    description="记录一次评分并返回下一张卡片。",
)
async def rate_review_word(payload: RatePayload = Body(...)):
    try:
        card = service.rate_word(payload.word_id, payload.kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return card


@router.post(
    "/api/review/notify-test",
    summary="发送测试通知",
    description="弹一条测试用的 Windows 通知，确认通知中心与点击跳转是否正常。",
)
async def notify_test():
    config = service.get_config()
    url = service.build_open_url(config, None)
    delivered = send_test_notification(url)
    if not delivered:
        raise HTTPException(status_code=503, detail="当前环境无法发送 Windows 通知")
    return {"message": "测试通知已发送", "open_url": url}


# ── 页面 ────────────────────────────────────────────────────────────


@router.get(
    "/review",
    summary="复习页面",
    description="复习 + 配置页面（Windows 通知点击后打开的就是它）。",
    response_class=FileResponse,
    include_in_schema=False,
)
async def review_page():
    page = STATIC_DIR / "review.html"
    if not page.exists():  # pragma: no cover —— 打包漏文件时的兜底
        raise HTTPException(status_code=404, detail="复习页面不存在")
    return FileResponse(page, media_type="text/html; charset=utf-8")


@router.get(
    "/api/review/pending",
    summary="待评分的词",
    description="返回已推送但还没评分的词条 ID 列表。",
)
async def list_pending() -> List[dict[str, Any]]:
    schedule = service.load_schedule()
    return [
        {
            "word_id": entry["word_id"],
            "word": entry.get("word"),
            "last_notified_at": entry.get("last_notified_at"),
        }
        for entry in schedule.values()
        if entry.get("pending_review")
    ]
