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
-- FTS5 self-contained (senza content=''): conserva il testo indicizzato, cosi'
-- snippet()/highlight() funzionano nei risultati di ricerca.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title,
  body,
  link,
  feed,
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

-- Utenti applicativi (autenticazione gestita dalla webapp: login.php).
-- La webapp la crea anche a runtime (login.php / users.php).
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'reader',   -- reader | collaborator | admin
  disabled      INTEGER NOT NULL DEFAULT 0,
  created_by    TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_login_at TEXT
);

-- Criteri di ricerca salvati, per utente (owner = username applicativo).
-- La webapp la crea anche a runtime al primo salvataggio (search.php).
CREATE TABLE IF NOT EXISTS saved_searches (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  owner        TEXT NOT NULL,
  name         TEXT NOT NULL,
  q            TEXT NOT NULL,
  feed_id      INTEGER,
  result_limit INTEGER,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(owner, name)
);

CREATE INDEX IF NOT EXISTS idx_items_feed ON items(feed_id);
CREATE INDEX IF NOT EXISTS idx_items_pub  ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_ann_item   ON annotations(item_id);
CREATE INDEX IF NOT EXISTS idx_saved_searches_owner ON saved_searches(owner, name);

-- Articoli salvati tra i favoriti, per utente, con nota personale.
-- La webapp la crea anche a runtime (favorites.php).
CREATE TABLE IF NOT EXISTS favorites (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  owner      TEXT NOT NULL,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  note       TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(owner, item_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_owner ON favorites(owner, created_at DESC);

-- Log accessi: una riga per richiesta HTTP (scritta in shutdown da log_access()).
-- La webapp la crea anche a runtime.
CREATE TABLE IF NOT EXISTS access_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL DEFAULT (datetime('now')),
  ip           TEXT NOT NULL DEFAULT '',
  username     TEXT,
  role         TEXT,
  path         TEXT NOT NULL DEFAULT '',
  method       TEXT NOT NULL DEFAULT '',
  query_string TEXT,
  status_code  INTEGER,
  user_agent   TEXT,
  referer      TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_log_ts   ON access_log(ts);
CREATE INDEX IF NOT EXISTS idx_access_log_ip   ON access_log(ip);
CREATE INDEX IF NOT EXISTS idx_access_log_user ON access_log(username);
CREATE INDEX IF NOT EXISTS idx_access_log_path ON access_log(path);

-- Tentativi di login (riusciti e falliti): visibilita' brute-force + rate-limit.
CREATE TABLE IF NOT EXISTS login_attempts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  username   TEXT NOT NULL DEFAULT '',
  ip         TEXT NOT NULL DEFAULT '',
  success    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip   ON login_attempts(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at);

-- Cache paese per IP (risoluzione opzionale via ip-api.com se cfg()['ipgeo']).
CREATE TABLE IF NOT EXISTS ip_geo_cache (
  ip           TEXT PRIMARY KEY,
  country_code TEXT,
  country_name TEXT,
  resolved_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
