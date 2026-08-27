#!/usr/bin/env python3
import os, sqlite3, gzip

DB = os.environ.get("RSSINTEL_DB", "/var/lib/rssintel/rssintel.db")  # override via env


def read_text(path: str) -> str:
    """Legge il testo estratto, decomprimendo i .gz (il fetcher salva .txt.gz)."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="ignore") as f:
                return f.read()
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def main():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys=ON;")
    cur = con.cursor()

    rows = cur.execute("""
      SELECT i.id, COALESCE(i.title,''), COALESCE(i.link,''),
             COALESCE(f.title,f.url,'') AS feed_title,
             COALESCE(i.text_path,'')
      FROM items i
      JOIN feeds f ON f.id=i.feed_id
      ORDER BY i.id
    """).fetchall()

    n = 0
    for item_id, title, link, feed_title, text_path in rows:
        body = read_text(text_path)
        # FTS5 self-contained: DELETE per rowid + INSERT (idempotente se rilanciato).
        cur.execute("DELETE FROM items_fts WHERE rowid = ?", (item_id,))
        cur.execute("INSERT INTO items_fts(rowid, title, body, link, feed) VALUES(?,?,?,?,?)",
                    (item_id, title, body, link, feed_title))
        n += 1
        if n % 500 == 0:
            con.commit()
            print(f"reindexed {n}/{len(rows)}")
    con.commit()
    print(f"done. reindexed={n}")
    con.close()


if __name__ == "__main__":
    main()
