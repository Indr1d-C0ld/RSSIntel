#!/usr/bin/env python3
import os, sqlite3

DB = os.environ.get("RSSINTEL_DB", "/var/lib/rssintel/rssintel.db")

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

    n=0
    for item_id, title, link, feed_title, text_path in rows:
        body=""
        if text_path and os.path.isfile(text_path):
            with open(text_path, "r", encoding="utf-8", errors="ignore") as f:
                body = f.read()
        # delete + insert (FTS5-safe)
        cur.execute("INSERT INTO items_fts(items_fts, rowid) VALUES('delete', ?)", (item_id,))
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
