# list_tables.py
import sqlite3, sys
db = sys.argv[1] if len(sys.argv) > 1 else 'oaldpe.db'
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
for r in cur.fetchall():
    print(r[0])
conn.close()