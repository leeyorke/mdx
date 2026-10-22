"""欧路生词本的 REST 接口

扩展通过这些接口配置欧路接入、拉取生词本列表、触发同步。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from app import eudic

log = logging.getLogger("app.eudic.api")

router = APIRouter(tags=["Eudic"])


class EudicConfigPayload(BaseModel):
    """欧路接入配置（只需传要改的字段）。"""

    enabled: Optional[bool] = Field(None, description="是否启用欧路同步")
    fallback_token: Optional[str] = Field(
        None, description="备用 token（环境变量 EUDIT_API_KEY 没生效时用）"
    )
    category_id: Optional[str] = Field(None, description="要同步的生词本 id")
    category_name: Optional[str] = Field(None, description="生词本名称")
    sync_new_words: Optional[bool] = Field(
        None, description="本地新收藏的词是否同时推给欧路"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"enabled": True, "category_id": "0", "sync_new_words": True}
            ]
        }
    }


@router.get(
    "/api/eudic/config",
    summary="读取欧路配置",
    description="返回欧路接入配置。token 不回传明文，只给出来源和是否已设置。",
)
async def get_eudic_config():
    return eudic.public_config()


@router.put(
    "/api/eudic/config",
    summary="更新欧路配置",
    description="按字段合并更新欧路接入配置，只需传要改的字段。",
)
async def put_eudic_config(payload: EudicConfigPayload = Body(...)):
    updates = payload.model_dump(exclude_unset=True)

    mapping = {
        "enabled": "enabled",
        "fallback_token": "fallbackToken",
        "category_id": "categoryId",
        "category_name": "categoryName",
        "sync_new_words": "syncNewWords",
    }
    stored = {mapping[key]: value for key, value in updates.items() if key in mapping}

    if stored:
        eudic.save_config(stored)

    return eudic.public_config()


@router.get(
    "/api/eudic/categories",
    summary="列出欧路生词本",
    description="实时向欧路查询账号里的生词本列表。",
)
async def list_eudic_categories():
    token, source = eudic.resolve_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="未配置欧路 token：请设置环境变量 EUDIT_API_KEY，或在设置页填入备用 token",
        )

    try:
        categories = eudic.list_categories(token)
    except eudic.EudicAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except eudic.EudicError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "categories": [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "language": str(item.get("language") or eudic.LANGUAGE),
            }
            for item in categories
        ],
        "token_source": source,
    }


@router.post(
    "/api/eudic/verify",
    summary="验证欧路连接",
    description="用当前 token 打一次欧路接口，确认能不能连上，并回传生词本列表。",
)
async def verify_eudic():
    token, source = eudic.resolve_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="未配置欧路 token：请设置环境变量 EUDIT_API_KEY，或在设置页填入备用 token",
        )

    try:
        categories = eudic.list_categories(token)
    except eudic.EudicAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except eudic.EudicError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "ok": True,
        "token_source": source,
        "categories": [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "language": str(item.get("language") or eudic.LANGUAGE),
            }
            for item in categories
        ],
    }


@router.post(
    "/api/eudic/sync",
    summary="同步欧路生词本",
    description="把欧路生词本里的词同步进本地生词表。已有词只补空字段，星级以用户评分为准。",
)
async def sync_eudic():
    try:
        return eudic.sync_from_eudic()
    except eudic.EudicAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except eudic.EudicError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
