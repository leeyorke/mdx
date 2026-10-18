"""FastAPI 字典查询接口"""

import logging
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query, Path, Body
from pydantic import BaseModel, Field

from app.dictionary import query_word, search_words
from app.goldendict import start_embedded_watcher, stop_embedded_watcher
from app.vocabulary import (
    add_word,
    delete_word,
    get_word,
    list_words,
    review_word,
    update_word,
)

log = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """随主应用启停 GoldenDict 监听器（失败不影响 API 服务）"""
    try:
        start_embedded_watcher()
    except Exception:
        log.exception("GoldenDict 监听启动失败（不影响 API 服务）")
    try:
        yield
    finally:
        try:
            stop_embedded_watcher()
        except Exception:
            log.exception("GoldenDict 监听停止时出错")


app = FastAPI(
    title="OALD 词典 API",
    description="Oxford Advanced Learner's Dictionary 英汉词典查询接口",
    version="1.0.0",
    contact={"name": "API Support", "email": "support@example.com"},
    license={"name": "MIT"},
    servers=[{"url": "http://127.0.0.1:8000", "description": "本地开发服务"}],
    external_docs={
        "description": "项目 README",
        "url": "/README.md",
    },
    lifespan=lifespan,
)


# ── Request schemas ─────────────────────────────────────────────────


class AddVocabularyWordPayload(BaseModel):
    word: str = Field(
        ...,
        description="要添加的单词",
    )
    translation: str = Field(
        ...,
        description="单词的翻译",
    )
    context_url: Optional[str] = Field(
        None,
        description="上下文链接",
    )
    context_text: Optional[str] = Field(
        None,
        description="上下文文本",
    )
    source_language: Optional[str] = Field(
        None,
        description="源语言",
    )
    target_language: Optional[str] = Field(
        None,
        description="目标语言",
    )
    note: Optional[str] = Field(
        None,
        description="备注",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "word": "hello",
                    "translation": "你好",
                    "context_url": "https://example.com",
                    "context_text": "Used in a sentence.",
                    "source_language": "en",
                    "target_language": "zh",
                    "note": "Important word",
                }
            ]
        }
    }


class UpdateVocabularyWordPayload(BaseModel):
    star: Optional[int] = Field(
        None,
        ge=1,
        le=5,
        description="评分（1-5）",
    )
    translation: Optional[str] = Field(
        None,
        description="新的翻译",
    )
    note: Optional[str] = Field(
        None,
        description="新的备注",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"star": 5, "translation": "新的翻译", "note": "更新后的笔记"}]
        }
    }


class VocabularyRecordResponse(BaseModel):
    id: str = Field(description="生词 ID")
    word: str = Field(description="单词")
    translation: str = Field(description="翻译")
    context_url: Optional[str] = Field(None, description="上下文链接")
    context_text: Optional[str] = Field(None, description="上下文文本")
    source_language: Optional[str] = Field(None, description="源语言")
    target_language: Optional[str] = Field(None, description="目标语言")
    star: int = Field(description="评分（1-5）")
    created_at: int = Field(description="创建时间（毫秒时间戳）")
    last_reviewed_at: Optional[int] = Field(
        None, description="最后复习时间（毫秒时间戳）"
    )
    review_count: int = Field(description="复习次数")
    note: Optional[str] = Field(None, description="备注")

    model_config = {"from_attributes": True}


class AddVocabularyWordResponse(VocabularyRecordResponse):
    pass


class VocabularyListResponse(BaseModel):
    records: List[VocabularyRecordResponse] = Field(description="生词记录列表")
    total: int = Field(description="总记录数")
    limit: int = Field(description="每页数量")
    offset: int = Field(description="偏移量")

    model_config = {"from_attributes": True}


class VocabularyMessageResponse(BaseModel):
    message: str = Field(description="消息文本")


class WordEntryResponse(BaseModel):
    model_config = {"extra": "allow"}


class SearchResponse(BaseModel):
    query: str = Field(description="搜索关键词")
    results: List[str] = Field(description="匹配结果列表")
    count: int = Field(description="结果数量")


# ── Root / Dictionary ───────────────────────────────────────────────


@app.get(
    "/",
    summary="API 信息",
    description="返回 API 的基本信息和可用端点列表。",
    tags=["Root"],
    responses={
        200: {
            "description": "API 信息",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "API 信息",
                            "value": {
                                "name": "OALD 词典 API",
                                "version": "1.0.0",
                                "endpoints": {
                                    "查找单词": "GET /api/word/{word}",
                                    "搜索单词": "GET /api/search?q=前缀",
                                    "添加生词": "POST /api/vocabulary",
                                    "查询生词": "GET /api/vocabulary",
                                    "查看生词": "GET /api/vocabulary/{word_id}",
                                    "更新生词": "PATCH /api/vocabulary/{word_id}",
                                    "删除生词": "DELETE /api/vocabulary/{word_id}",
                                    "复习生词": "POST /api/vocabulary/{word_id}/review",
                                },
                            },
                        }
                    },
                }
            },
        }
    },
)
async def root():
    return {
        "name": "OALD 词典 API",
        "version": "1.0.0",
        "endpoints": {
            "查找单词": "GET /api/word/{word}",
            "搜索单词": "GET /api/search?q=前缀",
            "添加生词": "POST /api/vocabulary",
            "查询生词": "GET /api/vocabulary",
            "查看生词": "GET /api/vocabulary/{word_id}",
            "更新生词": "PATCH /api/vocabulary/{word_id}",
            "删除生词": "DELETE /api/vocabulary/{word_id}",
            "复习生词": "POST /api/vocabulary/{word_id}/review",
        },
    }


@app.get(
    "/api/word/{word}",
    summary="查询单词详情",
    description="查询指定单词在 OALD 词典中的完整释义，包括词性、发音、释义、例句和主题标签。",
    tags=["Dictionary"],
    response_model=WordEntryResponse,
    responses={
        404: {
            "description": "单词未找到",
            "content": {
                "application/json": {"example": {"detail": "未找到单词 'hello' 的释义"}}
            },
        },
        200: {
            "description": "查询成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "单词释义",
                            "value": {
                                "headword": "hello",
                                "pos": "exclamation",
                                "phonetics": {"uk": "həˈləʊ", "us": "həˈloʊ"},
                                "senses": [
                                    {
                                        "number": "1",
                                        "definition": "used as a greeting",
                                        "chinese_definition": "你好；哈罗",
                                        "examples": [
                                            {
                                                "text": "Hello, how are you?",
                                                "chinese": "你好，你好吗？",
                                            }
                                        ],
                                        "topics": [
                                            {"name": "Communication", "level": "B1"}
                                        ],
                                    }
                                ],
                            },
                        }
                    },
                }
            },
        },
    },
)
async def get_word_endpoint(
    word: str = Path(..., description="要查询的单词", examples=[{"value": "hello"}]),
):
    """查询指定单词的完整释义"""
    result = query_word(word.lower())
    if result is None:
        raise HTTPException(status_code=404, detail=f"未找到单词 '{word}' 的释义")
    return result or {}


@app.get(
    "/api/search",
    summary="搜索单词",
    description="根据前缀在 OALD 词典中模糊搜索匹配的单词列表。",
    tags=["Dictionary"],
    response_model=SearchResponse,
    responses={
        422: {
            "description": "参数验证失败",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["query", "q"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            }
                        ]
                    }
                }
            },
        },
        200: {
            "description": "搜索成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "搜索示例",
                            "value": {
                                "query": "hel",
                                "results": ["hello", "help"],
                                "count": 2,
                            },
                        }
                    },
                }
            },
        },
    },
)
async def search(
    q: str = Query(..., description="搜索前缀", examples=[{"value": "hel"}]),
    limit: int = Query(
        20,
        ge=1,
        le=100,
        description="返回结果数量",
        examples=[{"value": 20}],
    ),
):
    """按前缀模糊搜索单词"""
    results = search_words(q.lower(), limit)
    return {"query": q, "results": results, "count": len(results)}


# ── Vocabulary ──────────────────────────────────────────────────────


@app.post(
    "/api/vocabulary",
    summary="添加生词",
    description="将新单词添加到用户的生词本中。",
    tags=["Vocabulary"],
    response_model=AddVocabularyWordResponse,
    responses={
        422: {
            "description": "请求体验证失败",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "word"],
                                "msg": "field required",
                                "type": "value_error.missing",
                            }
                        ]
                    }
                }
            },
        },
        200: {
            "description": "创建成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "生词记录",
                            "value": {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "word": "hello",
                                "translation": "你好",
                                "context_url": None,
                                "context_text": None,
                                "source_language": None,
                                "target_language": None,
                                "star": 1,
                                "created_at": 1700000000000,
                                "last_reviewed_at": None,
                                "review_count": 0,
                                "note": None,
                            },
                        }
                    },
                }
            },
        },
    },
)
async def create_vocabulary(
    payload: AddVocabularyWordPayload = Body(..., description="生词信息"),
):
    """添加生词到生词本"""
    record = add_word(
        word=payload.word,
        translation=payload.translation,
        context_url=payload.context_url,
        context_text=payload.context_text,
        source_language=payload.source_language,
        target_language=payload.target_language,
        note=payload.note,
    )
    return record


@app.get(
    "/api/vocabulary",
    summary="分页查询生词本",
    description="分页获取用户的生词本列表，支持按创建时间、星级或单词排序，并可按单词模糊搜索。",
    tags=["Vocabulary"],
    response_model=VocabularyListResponse,
    responses={
        422: {
            "description": "参数验证失败",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["query", "limit"],
                                "msg": "value_error.number.not_le",
                                "type": "value_error.number.not_le",
                            }
                        ]
                    }
                }
            },
        },
        200: {
            "description": "查询成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "分页示例",
                            "value": {
                                "records": [
                                    {
                                        "id": "550e8400-e29b-41d4-a716-446655440000",
                                        "word": "hello",
                                        "translation": "你好",
                                        "context_url": None,
                                        "context_text": None,
                                        "source_language": None,
                                        "target_language": None,
                                        "star": 5,
                                        "created_at": 1700000000000,
                                        "last_reviewed_at": 1700000000000,
                                        "review_count": 2,
                                        "note": "复习用",
                                    }
                                ],
                                "total": 1,
                                "limit": 20,
                                "offset": 0,
                            },
                        }
                    },
                }
            },
        },
    },
)
async def list_vocabulary(
    limit: int = Query(
        20, ge=1, le=100, description="返回数量", examples=[{"value": 20}]
    ),
    offset: int = Query(0, ge=0, description="偏移量", examples=[{"value": 0}]),
    sortBy: str = Query(
        "createdAt",
        description="排序字段: createdAt | star | word",
        examples=[{"value": "createdAt"}],
    ),
    sortOrder: str = Query(
        "desc",
        description="排序方向: asc | desc",
        examples=[{"value": "desc"}],
    ),
    search: Optional[str] = Query(
        None, description="按 word 模糊搜索", examples=[{"value": "hel"}]
    ),
):
    """分页查询生词本"""
    # 将前端驼峰字段名映射为 SQLite 字段名
    sort_map = {"createdAt": "created_at", "star": "star", "word": "word"}
    sort_by_db = sort_map.get(sortBy, "created_at")

    records, total = list_words(
        limit=limit,
        offset=offset,
        sort_by=sort_by_db,
        sort_order=sortOrder,
        search=search,
    )
    return {"records": records, "total": total, "limit": limit, "offset": offset}


@app.get(
    "/api/vocabulary/{word_id}",
    summary="查看生词详情",
    description="根据生词 ID 查看详细信息。",
    tags=["Vocabulary"],
    response_model=VocabularyRecordResponse,
    responses={
        404: {
            "description": "生词不存在",
            "content": {"application/json": {"example": {"detail": "生词不存在"}}},
        },
        200: {
            "description": "查询成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "生词详情",
                            "value": {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "word": "hello",
                                "translation": "你好",
                                "context_url": None,
                                "context_text": None,
                                "source_language": None,
                                "target_language": None,
                                "star": 3,
                                "created_at": 1700000000000,
                                "last_reviewed_at": 1700000000000,
                                "review_count": 1,
                                "note": "复习用",
                            },
                        }
                    },
                }
            },
        },
    },
)
async def get_vocabulary(
    word_id: str = Path(
        ...,
        description="生词 ID",
        examples=[{"value": "550e8400-e29b-41d4-a716-446655440000"}],
    ),
):
    """按 ID 查看生词详情"""
    record = get_word(word_id)
    if record is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return record or {}


@app.patch(
    "/api/vocabulary/{word_id}",
    summary="更新生词",
    description="更新生词的评分、翻译或笔记。至少需要提供一个更新字段。",
    tags=["Vocabulary"],
    response_model=VocabularyRecordResponse,
    responses={
        400: {
            "description": "未提供更新字段",
            "content": {
                "application/json": {"example": {"detail": "请提供至少一个更新字段"}}
            },
        },
        404: {
            "description": "生词不存在",
            "content": {"application/json": {"example": {"detail": "生词不存在"}}},
        },
        422: {
            "description": "请求体验证失败",
            "content": {
                "application/json": {
                    "example": {
                        "detail": [
                            {
                                "loc": ["body", "star"],
                                "msg": "value_error.number.not_le",
                                "type": "value_error.number.not_le",
                            }
                        ]
                    }
                }
            },
        },
        200: {
            "description": "更新成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "更新后的生词",
                            "value": {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "word": "hello",
                                "translation": "你好",
                                "context_url": None,
                                "context_text": None,
                                "source_language": None,
                                "target_language": None,
                                "star": 5,
                                "created_at": 1700000000000,
                                "last_reviewed_at": 1700000001000,
                                "review_count": 1,
                                "note": "更新后的笔记",
                            },
                        }
                    },
                }
            },
        },
    },
)
async def patch_vocabulary(
    word_id: str = Path(
        ...,
        description="生词 ID",
        examples=[{"value": "550e8400-e29b-41d4-a716-446655440000"}],
    ),
    payload: UpdateVocabularyWordPayload = Body(..., description="更新负载"),
):
    """更新生词（star / translation / note）"""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="请提供至少一个更新字段")

    record = update_word(word_id, updates)
    if record is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return record


@app.delete(
    "/api/vocabulary/{word_id}",
    summary="删除生词",
    description="根据生词 ID 永久删除生词记录。",
    tags=["Vocabulary"],
    response_model=VocabularyMessageResponse,
    responses={
        404: {
            "description": "生词不存在",
            "content": {"application/json": {"example": {"detail": "生词不存在"}}},
        },
        200: {
            "description": "删除成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "删除响应",
                            "value": {"message": "删除成功"},
                        }
                    },
                }
            },
        },
    },
)
async def remove_vocabulary(
    word_id: str = Path(
        ...,
        description="生词 ID",
        examples=[{"value": "550e8400-e29b-41d4-a716-446655440000"}],
    ),
):
    """删除生词"""
    if not delete_word(word_id):
        raise HTTPException(status_code=404, detail="生词不存在")
    return {"message": "删除成功"}


@app.post(
    "/api/vocabulary/{word_id}/review",
    summary="标记复习",
    description="将生词标记为复习一次，自动增加复习次数并更新最后复习时间。",
    tags=["Vocabulary"],
    response_model=VocabularyRecordResponse,
    responses={
        404: {
            "description": "生词不存在",
            "content": {"application/json": {"example": {"detail": "生词不存在"}}},
        },
        200: {
            "description": "复习成功",
            "content": {
                "application/json": {
                    "examples": {
                        "normal": {
                            "summary": "复习响应",
                            "value": {
                                "id": "550e8400-e29b-41d4-a716-446655440000",
                                "word": "hello",
                                "translation": "你好",
                                "context_url": None,
                                "context_text": None,
                                "source_language": None,
                                "target_language": None,
                                "star": 2,
                                "created_at": 1700000000000,
                                "last_reviewed_at": 1700000002000,
                                "review_count": 2,
                                "note": None,
                            },
                        }
                    },
                }
            },
        },
    },
)
async def review_vocabulary(
    word_id: str = Path(
        ...,
        description="生词 ID",
        examples=[{"value": "550e8400-e29b-41d4-a716-446655440000"}],
    ),
):
    """标记复习一次"""
    record = review_word(word_id)
    if record is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return record
