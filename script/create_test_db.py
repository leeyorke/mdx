# create_test_db.py
import sqlite3
conn = sqlite3.connect('oaldpe.db')
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS mdx (entry TEXT PRIMARY KEY, paraphrase TEXT)")
cur.execute("REPLACE INTO mdx(entry, paraphrase) VALUES (?, ?)",
            ('apply', '<h1 class="headword">apply</h1><span class="pos">v</span>'
                      '<ul><li class="sense"><span class="iteration">1</span>'
                      '<span class="def">to put to use</span></li></ul>'))
conn.commit()
conn.close()
print('test record inserted')