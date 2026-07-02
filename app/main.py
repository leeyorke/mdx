"""FastAPI 字典查询接口"""

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.dictionary import query_word, search_words
from app.vocabulary import (
    add_word,
    delete_word,
    get_word,
    list_words,
    review_word,
    update_word,
)

app = FastAPI(
    title="OALD 词典 API",
    description="Oxford Advanced Learner's Dictionary 英汉词典查询接口",
    version="1.0.0",
)


# ── Request schemas ─────────────────────────────────────────────────

class AddVocabularyWordPayload(BaseModel):
    word: str
    translation: str
    contextUrl: Optional[str] = None
    contextText: Optional[str] = None
    sourceLanguage: Optional[str] = None
    targetLanguage: Optional[str] = None
    note: Optional[str] = None


class UpdateVocabularyWordPayload(BaseModel):
    star: Optional[int] = Field(None, ge=1, le=5)
    translation: Optional[str] = None
    note: Optional[str] = None


class ReviewResponse(BaseModel):
    id: str
    review_count: int
    last_reviewed_at: int


# ── Root / Dictionary ───────────────────────────────────────────────

@app.get("/")
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


@app.get("/api/word/{word}")
async def get_word_endpoint(word: str):
    """查询指定单词的完整释义"""
    result = query_word(word.lower())
    if result is None:
        raise HTTPException(status_code=404, detail=f"未找到单词 '{word}' 的释义")
    return result


@app.get("/api/search")
async def search(
    q: str = Query(..., description="搜索前缀"),
    limit: int = Query(20, ge=1, le=100, description="返回结果数量"),
):
    """按前缀模糊搜索单词"""
    results = search_words(q.lower(), limit)
    return {"query": q, "results": results, "count": len(results)}


# ── Vocabulary ──────────────────────────────────────────────────────

@app.post("/api/vocabulary")
async def create_vocabulary(payload: AddVocabularyWordPayload):
    """添加生词到生词本"""
    record = add_word(
        word=payload.word,
        translation=payload.translation,
        context_url=payload.contextUrl,
        context_text=payload.contextText,
        source_language=payload.sourceLanguage,
        target_language=payload.targetLanguage,
        note=payload.note,
    )
    return record


@app.get("/api/vocabulary")
async def list_vocabulary(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    sortBy: str = Query("createdAt", description="排序字段: createdAt | star | word"),
    sortOrder: str = Query("desc", description="排序方向: asc | desc"),
    search: Optional[str] = Query(None, description="按 word 模糊搜索"),
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


@app.get("/api/vocabulary/{word_id}")
async def get_vocabulary(word_id: str):
    """按 ID 查看生词详情"""
    record = get_word(word_id)
    if record is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return record


@app.patch("/api/vocabulary/{word_id}")
async def patch_vocabulary(word_id: str, payload: UpdateVocabularyWordPayload):
    """更新生词（star / translation / note）"""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="请提供至少一个更新字段")

    record = update_word(word_id, updates)
    if record is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return record


@app.delete("/api/vocabulary/{word_id}")
async def remove_vocabulary(word_id: str):
    """删除生词"""
    if not delete_word(word_id):
        raise HTTPException(status_code=404, detail="生词不存在")
    return {"message": "删除成功"}


@app.post("/api/vocabulary/{word_id}/review")
async def review_vocabulary(word_id: str):
    """标记复习一次"""
    record = review_word(word_id)
    if record is None:
        raise HTTPException(status_code=404, detail="生词不存在")
    return record
