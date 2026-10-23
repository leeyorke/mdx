"""复习提醒的 REST 接口 + 复习页面

页面就在 ``/review``：Windows 通知点击后用默认浏览器打开这个地址，直接
进入当天的复习会话；「立即复习」按钮随时能开一场不受配额限制的手动会话。
不需要浏览器扩展在场。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.review import service
from app.review.constants import REVIEW_PAGE_URL
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
        None, ge=1, le=50, description="每天最多复习多少个词（到点只提醒一次）"
    )
    graduated_action: Optional[str] = Field(
        None, description="走完整个阶梯之后怎么办：stop | restart"
    )
    quiet_hours: Optional[dict] = Field(
        None, description="免打扰时段：{enabled, start, end}（只挡通知，不挡手动复习）"
    )
    llm: Optional[dict] = Field(
        None, description="LLM 配置：{base_url, api_key, model}"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "enabled": True,
                    "max_per_day": 8,
                    "quiet_hours": {"enabled": True, "start": "23:00", "end": "08:00"},
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
    next_word_id: Optional[str] = Field(
        None,
        description="会话里的下一个生词 ID；传了就连同它的卡片一起返回，少一次往返",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "word_id": "550e8400-e29b-41d4-a716-446655440000",
                    "kind": "remembered",
                    "next_word_id": "661f9511-f3ac-52e5-b827-557766551111",
                }
            ]
        }
    }


class ExamplePayload(BaseModel):
    """按需生成例句。"""

    word_id: str = Field(..., description="生词 ID")

    model_config = {
        "json_schema_extra": {
            "examples": [{"word_id": "550e8400-e29b-41d4-a716-446655440000"}]
        }
    }


# ── 状态 / 配置 ─────────────────────────────────────────────────────


@router.get(
    "/api/review/status",
    summary="复习提醒状态",
    description="返回可复习词数、今日已复习/已提醒、下次到期时间、通知是否可用等状态。",
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


# ── 复习会话 ────────────────────────────────────────────────────────


@router.get(
    "/api/review/session",
    summary="组装一次复习会话",
    description=(
        "返回本次会话的有序词表与第一张卡片。mode=auto（通知点进来、打开页面）"
        "受今天剩余额度限制；mode=manual（「立即复习」）不受额度与免打扰限制，"
        "不足一天配额时用即将到期的词补齐（early=true，页面上显示「提前复习」）。"
        "队列由页面自己拿着走，服务端不存会话状态。"
    ),
)
async def get_review_session(mode: str = Query("auto", pattern="^(auto|manual)$")):
    return service.build_session(mode)


@router.post(
    "/api/review/example",
    summary="按需生成例句",
    description="给一个词生成（并缓存）例句；失败时退回收藏时的上下文，不影响复习。",
)
async def post_review_example(payload: ExamplePayload = Body(...)):
    # 生成要花一两秒（LLM 往返），放到线程池里跑：页面会并发预加载后面
    # 几张卡片的例句，占着事件循环的话请求会排队、连状态接口和调度器都
    # 会被拖慢。
    example = await run_in_threadpool(service.refresh_example, payload.word_id)
    if example is None:
        raise HTTPException(status_code=404, detail="词条不在复习队列里")
    return example


# ── 队列（内部 / 调试用，页面上没有对应按钮） ──────────────────────


@router.post(
    "/api/review/sync",
    summary="同步复习队列",
    description="用生词本里「至少复习过一次」的词对齐复习队列。调度器内部使用。",
)
async def sync_review_queue():
    return service.sync_review_queue()


@router.post(
    "/api/review/tick",
    summary="立即跑一轮调度",
    description=(
        "立刻执行一次调度：把今天到点的词合成一条 Windows 通知推出去。"
        "免打扰与每日配额照常生效（同一天最多提醒一次）。内部/调试用。"
    ),
)
async def run_review_tick():
    return service.run_review_tick()


@router.delete(
    "/api/review/queue",
    summary="清空复习队列",
    description="清空全部复习进度（不影响生词本）。内部/调试用。",
)
async def clear_review_queue():
    service.clear_queue()
    return {"message": "复习队列已清空"}


# ── 复习卡片 ────────────────────────────────────────────────────────


@router.get(
    "/api/review/card",
    summary="取一张复习卡片",
    description="按生词 ID 取一张待复习的卡片；不传时返回下一个待评分的词（兼容旧行为）。",
)
async def get_review_card(
    word_id: Optional[str] = Query(
        None, description="指定要复习的生词 ID（会话队列、旧通知深链会带上）"
    ),
):
    card = service.get_card(word_id)
    if card is None:
        return None
    return card


@router.post(
    "/api/review/rate",
    summary="提交复习评分",
    description=(
        "记录一次评分并返回下一张卡片；下一张由调用方通过 next_word_id "
        "指定（会话队列在页面手里）。"
    ),
)
async def rate_review_word(payload: RatePayload = Body(...)):
    try:
        card = service.rate_word(payload.word_id, payload.kind, payload.next_word_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return card


@router.post(
    "/api/review/notify-test",
    summary="发送测试通知",
    description="弹一条测试用的 Windows 通知，确认通知中心与点击跳转是否正常。",
)
async def notify_test():
    url = REVIEW_PAGE_URL
    delivered = send_test_notification(url)
    if not delivered:
        raise HTTPException(status_code=503, detail="当前环境无法发送 Windows 通知")
    return {"message": "测试通知已发送", "open_url": url}


# ── 页面 ────────────────────────────────────────────────────────────


@router.get(
    "/review",
    summary="复习页面",
    description="会话式复习 + 配置页面（Windows 通知点击后打开的就是它）。",
    response_class=FileResponse,
    include_in_schema=False,
)
async def review_page():
    page = STATIC_DIR / "review.html"
    if not page.exists():  # pragma: no cover —— 打包漏文件时的兜底
        raise HTTPException(status_code=404, detail="复习页面不存在")
    # 页面是单文件（HTML + CSS + JS 全在里面），改一次浏览器就得拿到新的：
    # 不带的 Cache-Control 的话浏览器会按启发式规则缓存，旧 JS 能活几个
    # 小时——表现就是「接口返回明明是对的，页面还按旧代码渲染」。
    # no-cache 是「每次都必须回服务器验证」：文件没变就 304，变了立刻拿到。
    return FileResponse(
        page,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


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
