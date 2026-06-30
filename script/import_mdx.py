import sqlite3
from readmdict import MDX
from pathlib import Path


MDX_FILE = "oaldpe.mdx"
DB_FILE = "oaldpe.db"


def create_db(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS mdx (
        entry TEXT PRIMARY KEY,
        paraphrase TEXT
    )
    """)

    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_entry
    ON mdx(entry)
    """)

    conn.commit()


def import_mdx(mdx_file, db_file):
    mdx = MDX(mdx_file)

    conn = sqlite3.connect(db_file)
    create_db(conn)

    cursor = conn.cursor()

    count = 0

    for key, value in mdx.items():
        try:
            word = key.decode("utf-8").strip()
        except:
            word = key.decode(errors="ignore").strip()

        try:
            html = value.decode("utf-8")
        except:
            html = value.decode(errors="ignore")

        if not word:
            continue

        cursor.execute(
            """
            INSERT OR REPLACE INTO mdx(entry, paraphrase)
            VALUES (?, ?)
            """,
            (word, html)
        )

        count += 1

        if count % 1000 == 0:
            conn.commit()
            print(f"Imported {count:,} entries...")

    conn.commit()
    conn.close()

    print(f"\nDone.")
    print(f"Entries imported: {count:,}")
    print(f"Database: {Path(db_file).resolve()}")


if __name__ == "__main__":
    import_mdx(MDX_FILE, DB_FILE)