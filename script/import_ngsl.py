import sqlite3
import pandas as pd

# ==========================
# 配置
# ==========================
CSV_FILE = r"D:\myfile\NGSL_1.2_stats.csv"
DB_FILE = "ngsl.db"
TABLE_NAME = "ngsl_words"

# ==========================
# 读取 CSV
# ==========================
df = pd.read_csv(CSV_FILE)

# 字段名改为蛇形命名
df.columns = [
    "lemma",
    "sfi_rank",
    "sfi",
    "adjusted_frequency_per_million_u",
]

# 新增熟练度字段（默认1）
df["star"] = 1

# ==========================
# 创建数据库
# ==========================
conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

# 删除旧表
cursor.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")

# 创建新表
cursor.execute(f"""
CREATE TABLE {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lemma TEXT NOT NULL UNIQUE,
    sfi_rank INTEGER,
    sfi REAL,
    adjusted_frequency_per_million_u REAL,
    star INTEGER NOT NULL DEFAULT 1 CHECK(star BETWEEN 1 AND 5)
)
""")

# 插入数据
cursor.executemany(
    f"""
    INSERT INTO {TABLE_NAME}
    (
        lemma,
        sfi_rank,
        sfi,
        adjusted_frequency_per_million_u,
        star
    )
    VALUES (?, ?, ?, ?, ?)
    """,
    df.values.tolist(),
)

# 创建索引
cursor.execute(f"CREATE INDEX idx_{TABLE_NAME}_rank ON {TABLE_NAME}(sfi_rank)")

cursor.execute(f"CREATE INDEX idx_{TABLE_NAME}_star ON {TABLE_NAME}(star)")

conn.commit()
conn.close()

print(f"数据库已生成：{DB_FILE}")
print(f"表名：{TABLE_NAME}")
print(f"共导入 {len(df)} 条记录")
