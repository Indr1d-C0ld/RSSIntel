-- RSSIntel — allerte sulle ricerche salvate (fase 1).
-- Da eseguire come www-data:  sudo -u www-data sqlite3 /percorso/del/rssintel.db < migrate_watch.sql
ALTER TABLE saved_searches ADD COLUMN watch           INTEGER NOT NULL DEFAULT 0;
ALTER TABLE saved_searches ADD COLUMN last_checked_at TEXT;
ALTER TABLE saved_searches ADD COLUMN last_error      TEXT;

CREATE TABLE IF NOT EXISTS watch_hits (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  search_id INTEGER NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
  item_id   INTEGER NOT NULL REFERENCES items(id)          ON DELETE CASCADE,
  found_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  seen      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(search_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_watch_hits_unseen ON watch_hits(search_id, seen);
