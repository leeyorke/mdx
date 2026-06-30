"""FastAPI 字典查询接口"""

from fastapi import FastAPI, HTTPException, Query
from app.dictionary import query_word, search_words

app = FastAPI(
    title="OALD 词典 API",
    description="Oxford Advanced Learner's Dictionary 英汉词典查询接口",
    version="1.0.0",
)


@app.get("/")
async def root():
    return {
        "name": "OALD 词典 API",
        "version": "1.0.0",
        "endpoints": {
            "查找单词": "GET /api/word/{word}",
            "搜索单词": "GET /api/search?q=前缀",
        },
    }


@app.get("/api/word/{word}")
async def get_word(word: str):
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
