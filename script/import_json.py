import json
import sqlite3
from pathlib import Path

JSON_PATH = r"D:\leeyorke\Downloads\VibeReadingDB.json"
DB_PATH = r"D:\myworkspace\mdx\user.db"

# ========== 1. 读取 JSON ==========
data = json.loads(Path(JSON_PATH).read_text(encoding="utf-8"))
words = data.get("stores", {}).get("vocabularyWords", [])

# ========== 2. 建库 ==========
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# ========== 3. 建表（snake_case） ==========
cursor.execute("""
CREATE TABLE IF NOT EXISTS vocabulary_words (
    id TEXT PRIMARY KEY,
    word TEXT NOT NULL,
    translation TEXT,
    context_url TEXT,
    context_text TEXT,
    source_language TEXT,
    target_language TEXT,
    star INTEGER,
    created_at INTEGER,
    last_reviewed_at INTEGER,
    review_count INTEGER DEFAULT 0,
    note TEXT
);
""")

# ========== 4. 索引 ==========
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_vocabulary_words_word ON vocabulary_words(word);"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_vocabulary_words_star ON vocabulary_words(star);"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_vocabulary_words_created_at ON vocabulary_words(created_at);"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_vocabulary_words_last_reviewed_at ON vocabulary_words(last_reviewed_at);"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_vocabulary_words_review_count ON vocabulary_words(review_count);"
)

# ========== 5. 写入 ==========
sql = """
INSERT OR REPLACE INTO vocabulary_words (
    id, word, translation,
    context_url, context_text,
    source_language, target_language,
    star, created_at, last_reviewed_at,
    review_count, note
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

for w in words:
    cursor.execute(
        sql,
        (
            w.get("id"),
            w.get("word"),
            w.get("translation"),
            w.get("contextUrl"),
            w.get("contextText"),
            w.get("sourceLanguage"),
            w.get("targetLanguage"),
            w.get("star"),
            w.get("createdAt"),
            w.get("lastReviewedAt"),
            w.get("reviewCount", 0),
            w.get("note"),
        ),
    )

conn.commit()
conn.close()

print(f"导入完成：{len(words)} 条记录 -> vocabulary_words")
