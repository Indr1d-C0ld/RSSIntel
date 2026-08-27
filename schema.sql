PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS feeds (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT NOT NULL UNIQUE,
  title         TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  etag          TEXT,
  last_modified TEXT,
  last_fetch_at TEXT,
  last_status   INTEGER,
  last_error    TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_id       INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  guid          TEXT,
  title         TEXT,
  link          TEXT,
  author        TEXT,
  published_at  TEXT,
  fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
  raw_path      TEXT,
  text_path     TEXT,
  content_hash  TEXT,
  UNIQUE(feed_id, guid),
  UNIQUE(feed_id, link)
);

-- FTS: indicizza titolo + testo + url + feed title (se vuoi)
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title,
  body,
  link,
  feed,
  content='',
  tokenize='unicode61'
);

-- Trigger “manuale” (non content=items) per semplicità e controllo
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, body, link, feed)
  VALUES (new.id, COALESCE(new.title,''), '', COALESCE(new.link,''), '');
END;

CREATE TABLE IF NOT EXISTS annotations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  note        TEXT NOT NULL,
  quote       TEXT,
  author      TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS tags (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS annotation_tags (
  annotation_id INTEGER NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
  tag_id        INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY(annotation_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_items_feed ON items(feed_id);
CREATE INDEX IF NOT EXISTS idx_items_pub  ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_ann_item   ON annotations(item_id);
