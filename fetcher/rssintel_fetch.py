#!/usr/bin/env python3
import os
import re
import sys
import time
import hashlib
import sqlite3
import gzip
import tempfile
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
import feedparser

try:
    import trafilatura
except Exception:
    trafilatura = None


# =========================
# CONFIG (Pi-friendly)
# Sovrascrivibile via variabili d'ambiente (vedi config.sample.php).
# =========================
DB_PATH = os.environ.get("RSSINTEL_DB", "/var/lib/rssintel/rssintel.db")
RAW_DIR = os.environ.get("RSSINTEL_RAW_DIR", "/var/lib/rssintel/raw")
TXT_DIR = os.environ.get("RSSINTEL_TXT_DIR", "/var/lib/rssintel/text")

UA = os.environ.get("RSSINTEL_UA", "RSSIntel/1.1 (+https://example.com; research)")
TIMEOUT = 25

# Frequenza e carico
PER_FEED_MAX_NEW_ITEMS_PER_RUN = 50     # evita “valanghe” su feed molto attivi
REQUEST_DELAY_SECONDS = 0.25            # piccolo respiro tra HTTP (stabilità + meno picchi)

# Stabilità/I/O
SAVE_RAW_HTML = False                  # TRUE solo se ti serve debug forense
COMPRESS_TEXT_GZ = True                # .txt.gz riduce I/O su SD (CPU ok su Pi4)
COMPRESS_RAW_GZ = True                 # se SAVE_RAW_HTML=True, salva .html.gz
MAX_HTML_BYTES = 2_500_000              # cap RAM: ~2.5MB per articolo (tune)

# Se item già indicizzato e ha file testo valido, non riscaricare
SKIP_IF_ALREADY_INDEXED = True


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs():
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(TXT_DIR, exist_ok=True)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    cur = con.cursor()

    # ---- Anti-corruzione / stabilità su Raspberry ----
    cur.execute("PRAGMA journal_mode=WAL;")
    # NORMAL è un buon compromesso: molto più sicuro di OFF, molto meno I/O di FULL
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.execute("PRAGMA busy_timeout=5000;")
    # checkpoint più frequente = WAL non cresce troppo (meno stress su FS)
    cur.execute("PRAGMA wal_autocheckpoint=1000;")  # ~1000 pagine
    # cache moderata per ridurre I/O senza gonfiare RAM (valore in KiB con negativo)
    cur.execute("PRAGMA cache_size=-32000;")        # ~32MB
    # mmap può accelerare letture (se supportato)
    cur.execute("PRAGMA mmap_size=268435456;")      # 256MB
    con.commit()
    return con


def clean_text_basic(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|iframe).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p\s*>", "\n", html)
    html = re.sub(r"(?is)<.*?>", " ", html)
    html = re.sub(r"[ \t\r]+", " ", html)
    html = re.sub(r"\n\s+\n", "\n\n", html)
    return html.strip()


def extract_text(html: str) -> str:
    if trafilatura:
        t = trafilatura.extract(html, include_comments=False, include_tables=False)
        if t:
            return t.strip()
    return clean_text_basic(html)


def atomic_write_bytes(path: str, data: bytes) -> None:
    # Scrittura atomica: riduce rischio file “mezzo scritto” dopo crash/powerloss
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def atomic_write_text(path: str, text: str, gz: bool) -> str:
    if gz:
        b = gzip.compress(text.encode("utf-8", errors="ignore"), compresslevel=6)
        if not path.endswith(".gz"):
            path = path + ".gz"
        atomic_write_bytes(path, b)
        return path
    else:
        atomic_write_bytes(path, text.encode("utf-8", errors="ignore"))
        return path


def fetch_url_limited(session: requests.Session, url: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """
    Ritorna (html_text, status_code, error_string). html_text può essere None.
    Scarica in streaming con cap MAX_HTML_BYTES per contenere RAM.
    """
    try:
        r = session.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, stream=True, allow_redirects=True)
        status = r.status_code
        r.raise_for_status()

        chunks = []
        total = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_HTML_BYTES:
                return None, status, f"HTML too large (> {MAX_HTML_BYTES} bytes)"
            chunks.append(chunk)

        raw = b"".join(chunks)
        # decodifica in modo tollerante
        enc = r.encoding or "utf-8"
        html = raw.decode(enc, errors="ignore")
        return html, status, None
    except Exception as ex:
        # se r non esiste o ha fallito prima di status, status None
        status = None
        try:
            status = getattr(locals().get("r", None), "status_code", None)
        except Exception:
            pass
        return None, status, str(ex)


def fts_delete_insert(cur: sqlite3.Cursor, item_id: int, title: str, body: str, link: str, feed_title: str) -> None:
    # FTS5-safe (niente UPSERT)
    cur.execute("INSERT INTO items_fts(items_fts, rowid) VALUES('delete', ?)", (item_id,))
    cur.execute(
        "INSERT INTO items_fts(rowid, title, body, link, feed) VALUES(?,?,?,?,?)",
        (item_id, title or "", body or "", link or "", feed_title or ""),
    )


def process_feed(con: sqlite3.Connection, session: requests.Session, feed_row) -> int:
    feed_id, url, etag, last_mod = feed_row
    cur = con.cursor()

    headers = {"User-Agent": UA}
    if etag:
        headers["If-None-Match"] = etag
    if last_mod:
        headers["If-Modified-Since"] = last_mod

    try:
        r = session.get(url, timeout=TIMEOUT, headers=headers)
        status = r.status_code

        if status == 304:
            cur.execute(
                "UPDATE feeds SET last_fetch_at=?, last_status=?, last_error=NULL, updated_at=datetime('now') WHERE id=?",
                (now_utc(), status, feed_id),
            )
            con.commit()
            return 0

        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        feed_title = (parsed.feed.get("title") or "").strip()

        new_etag = r.headers.get("ETag")
        new_lm = r.headers.get("Last-Modified")

        cur.execute(
            "UPDATE feeds SET title=?, etag=?, last_modified=?, last_fetch_at=?, last_status=?, last_error=NULL, updated_at=datetime('now') WHERE id=?",
            (feed_title or None, new_etag, new_lm, now_utc(), status, feed_id),
        )
        con.commit()

        added = 0
        processed_new = 0

        # Transazione per feed: più efficiente e coerente
        cur.execute("BEGIN;")
        for e in parsed.entries:
            if processed_new >= PER_FEED_MAX_NEW_ITEMS_PER_RUN:
                break

            guid = (e.get("id") or e.get("guid") or "").strip() or None
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            author = (e.get("author") or "").strip() or None

            published = None
            if e.get("published_parsed"):
                published = time.strftime("%Y-%m-%d %H:%M:%S", e.published_parsed)

            if not link:
                continue

            # Esiste già?
            existing = cur.execute(
                "SELECT id, text_path, content_hash FROM items WHERE feed_id=? AND link=?",
                (feed_id, link),
            ).fetchone()

            if existing:
                item_id, text_path, content_hash = existing

                # Se già indicizzato e file presente, non riscaricare (riduce I/O e rete)
                if SKIP_IF_ALREADY_INDEXED:
                    if text_path and os.path.isfile(text_path):
                        continue

            # Inserisci item se nuovo
            item_id = None
            if not existing:
                try:
                    cur.execute(
                        "INSERT INTO items(feed_id, guid, title, link, author, published_at, fetched_at) "
                        "VALUES(?,?,?,?,?,?,datetime('now'))",
                        (feed_id, guid, title or None, link, author, published),
                    )
                    item_id = cur.lastrowid
                    added += 1
                    processed_new += 1
                except sqlite3.IntegrityError:
                    # collisione su UNIQUE(feed_id, link) / guid: recupera id e prosegui come existing
                    row = cur.execute("SELECT id, text_path, content_hash FROM items WHERE feed_id=? AND link=?",
                                      (feed_id, link)).fetchone()
                    if not row:
                        continue
                    item_id, text_path, content_hash = row
            else:
                item_id = existing[0]

            # Fetch articolo (cap RAM)
            html, st, err = fetch_url_limited(session, link)
            time.sleep(REQUEST_DELAY_SECONDS)

            if html is None:
                # salva errore su item? (non indispensabile)
                continue

            text = extract_text(html)
            if not text:
                continue

            ch = sha256(text)

            # Se era existing e hash uguale, evita riscrittura file/fts
            if existing and existing[2] and existing[2] == ch:
                continue

            # Salvataggio file (atomico)
            raw_path = None
            if SAVE_RAW_HTML:
                rp = os.path.join(RAW_DIR, f"{item_id}.html")
                raw_path = atomic_write_text(rp, html, gz=COMPRESS_RAW_GZ)

            tp = os.path.join(TXT_DIR, f"{item_id}.txt")
            text_path = atomic_write_text(tp, text, gz=COMPRESS_TEXT_GZ)

            cur.execute(
                "UPDATE items SET title=?, author=?, published_at=?, raw_path=?, text_path=?, content_hash=?, fetched_at=datetime('now') WHERE id=?",
                (title or None, author, published, raw_path, text_path, ch, item_id),
            )

            # Aggiorna FTS (delete+insert)
            fts_delete_insert(cur, item_id, title, text, link, feed_title)

        cur.execute("COMMIT;")
        con.commit()

        # checkpoint leggero post-feed (riduce rischio di WAL enorme)
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass

        return added

    except Exception as ex:
        try:
            cur.execute(
                "UPDATE feeds SET last_fetch_at=?, last_status=?, last_error=?, updated_at=datetime('now') WHERE id=?",
                (now_utc(), None, str(ex), feed_id),
            )
            con.commit()
        except Exception:
            pass
        return 0


def main() -> int:
    ensure_dirs()

    con = db_connect()
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    feeds = con.execute(
        "SELECT id, url, etag, last_modified FROM feeds WHERE enabled=1 ORDER BY id"
    ).fetchall()

    total_added = 0
    for fr in feeds:
        total_added += process_feed(con, session, fr)

    print(f"[rssintel] done. new_items={total_added}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
