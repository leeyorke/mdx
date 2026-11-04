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
from app.review import writing
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


# ── 复习成果检验（写作 + LLM 评卷）Schemas ──────────────────────────


class WritingGradePayload(BaseModel):
    """提交短文 / 造句，交给 LLM 评卷。"""

    task_id: str = Field(..., description="检验任务 ID（challenge 接口返回的）")
    text: str = Field(..., description="用户写的短文或逐词造句")
    mode: Optional[str] = Field(
        None, description="写作模式：paragraph（默认）| sentences，不传沿用任务的"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "task_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                    "text": "The jury has reached a final verdict after deliberation.",
                    "mode": "paragraph",
                }
            ]
        }
    }


class WritingDraftPayload(BaseModel):
    """保存草稿（不打分、不评卷）。"""

    task_id: str = Field(..., description="检验任务 ID")
    text: str = Field(..., description="用户写的草稿")
    mode: Optional[str] = Field(None, description="写作模式，不传沿用任务的")


class WritingNotePayload(BaseModel):
    """把一次（已评卷的）提交保存为写作笔记。"""

    submission_id: str = Field(..., description="评卷返回的提交 ID")

    model_config = {
        "json_schema_extra": {
            "examples": [{"submission_id": "0f14d0ab-9605-4a62-a9e4-5ed2668c3ebe"}]
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


# ── 复习成果检验（写作 + LLM 评卷）─────────────────────────────────


@router.get(
    "/api/review/writing/challenge",
    summary="检验词表（只给词形，不给释义）",
    description=(
        "组装一次检验：返回 task_id 与本次要用的词表。word_ids 不传时取「立即复习」"
        "会话的词表（检验跟着刚复习完的一组走）；传了就用指定的词。词表只含 word，"
        "绝不含 translation——检验的意义就是凭记忆用词。无词可检验时返回 "
        "state=empty + empty_reason。"
    ),
)
async def get_writing_challenge(
    word_ids: Optional[str] = Query(
        None, description="逗号分隔的生词 ID；不传则取本次会话词表"
    ),
    mode: str = Query("paragraph", pattern="^(paragraph|sentences)$"),
):
    ids = [w.strip() for w in word_ids.split(",")] if word_ids else None
    return writing.build_challenge(ids, mode)


@router.post(
    "/api/review/writing/grade",
    summary="发送给 LLM 评卷",
    description=(
        "把用户写的短文/造句交给 LLM 打分 + 校语法，返回分数、每个目标词是否用到、"
        "总评、逐条批改、内联高亮 span、词汇建议。评卷走线程池（LLM 往返要 2-5 秒，"
        "别占事件循环拖慢状态接口与调度器）。未配 LLM 或调用失败时不报错：走本地"
        "匹配只算用词覆盖，score 为 null、llm_source=matcher，页面据此提示"
        "「这次没评上分」。"
    ),
)
async def grade_writing(payload: WritingGradePayload = Body(...)):
    result = await run_in_threadpool(
        writing.grade_writing, payload.task_id, payload.text, payload.mode
    )
    if result is None:
        raise HTTPException(
            status_code=404, detail="检验任务不存在，或文本为空/超长"
        )
    return result


@router.post(
    "/api/review/writing/draft",
    summary="保存草稿为笔记",
    description="把稿子存成草稿笔记（不打分、不评卷），之后可再评卷。",
)
async def save_writing_draft(payload: WritingDraftPayload = Body(...)):
    note = writing.save_draft(payload.task_id, payload.text, payload.mode)
    if note is None:
        raise HTTPException(status_code=404, detail="检验任务不存在，或文本为空/超长")
    return note


@router.post(
    "/api/review/writing/notes",
    summary="保存为写作笔记",
    description="把一次（已评卷的）提交存为写作笔记。同一提交可重复保存。",
)
async def save_writing_note(payload: WritingNotePayload = Body(...)):
    note = writing.save_note(payload.submission_id)
    if note is None:
        raise HTTPException(status_code=404, detail="提交不存在")
    return note


@router.get(
    "/api/review/writing/notes",
    summary="写作笔记列表",
    description=(
        "分页列写作笔记，带三种筛选各自的计数：all（全部，含草稿）| high"
        "（高分练习 score>=80）| improve（待优化 score<70）。排序字段走白名单防注入。"
    ),
)
async def list_writing_notes(
    filter: str = Query("all", pattern="^(all|high|improve)$"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sortBy: str = Query("createdAt", description="createdAt | score"),
    sortOrder: str = Query("desc", pattern="^(asc|desc)$"),
):
    return writing.list_notes(
        note_filter=filter,
        limit=limit,
        offset=offset,
        sort_by="created_at" if sortBy == "createdAt" else sortBy,
        sort_order=sortOrder,
    )


@router.get(
    "/api/review/writing/notes/{note_id}",
    summary="写作笔记详情",
    description="按 ID 取一篇写作笔记（含完整评卷结果）。",
)
async def get_writing_note(note_id: str):
    note = writing.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return note


@router.delete(
    "/api/review/writing/notes/{note_id}",
    summary="删除写作笔记",
    description="按 ID 删除一篇写作笔记。",
)
async def delete_writing_note(note_id: str):
    if not writing.delete_note(note_id):
        raise HTTPException(status_code=404, detail="笔记不存在")
    return {"message": "删除成功"}


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
    "/verify",
    summary="复习成果检验页面",
    description="复习完成后检验成果：用会话的词写短文 → LLM 评卷 → 存写作笔记。",
    response_class=FileResponse,
    include_in_schema=False,
)
async def verify_page():
    page = STATIC_DIR / "verify.html"
    if not page.exists():  # pragma: no cover —— 打包漏文件时的兜底
        raise HTTPException(status_code=404, detail="检验页面不存在")
    # 同 /review：单文件页面必须 no-cache，否则改一次旧 JS 能活几小时。
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
