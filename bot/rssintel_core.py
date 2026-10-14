"""
RSSIntel — modulo comune del bot Telegram (nessuna dipendenza da Telegram).

Qui sta tutto cio' che tocca il database di RSSIntel, cosi' le regole di
accesso sono in un posto solo:

  - una connessione per operazione, aperta e chiusa ogni volta: niente
    transazioni di lettura lasciate aperte, che bloccherebbero il checkpoint
    del fetcher e farebbero crescere il WAL in silenzio;
  - letture con fetchall(), scritture in BEGIN IMMEDIATE brevi;
  - mai journal_mode, mai checkpoint: sono affari del fetcher;
  - il bot crea solo le proprie tabelle tg_*; se mancano quelle del portale
    si rifiuta di partire.

Lo stato che riguarda solo il bot (contatori d'uso, in seguito i contesti di
paginazione) sta in un database a parte nella StateDirectory, non in
rssintel.db: nessuna scrittura in piu' sul database condiviso.

Solo libreria standard.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sqlite3
import stat
import grp
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("RSSINTEL_DB", "/var/lib/rssintel/rssintel.db")
STATE_DIR = os.environ.get("STATE_DIRECTORY", "").split(":")[0] or "/var/lib/rssintel-bot"
STATE_DB = os.path.join(STATE_DIR, "state.db")

TZ = ZoneInfo(os.environ.get("RSSINTEL_TZ", "Europe/Rome"))

CODE_TTL_MIN = 15
# alfabeto senza caratteri ambigui (0/O, 1/I/L), tutto valido nei deep link
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 10

REQUIRED_TABLES = ("users", "feeds", "items", "items_fts", "saved_searches", "watch_hits")

TG_SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_links (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
  tg_user_id   INTEGER NOT NULL,
  chat_id      INTEGER NOT NULL,
  linked_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT,
  revoked_at   TEXT,
  blocked_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_links_active
  ON tg_links(tg_user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tg_links_user ON tg_links(username);
CREATE TABLE IF NOT EXISTS tg_link_codes (
  code_hash  TEXT PRIMARY KEY,
  username   TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL,
  used_at    TEXT
);
"""

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  day TEXT NOT NULL, cmd TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, cmd)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


# ---------------------------------------------------------------- connessioni

@contextlib.contextmanager
def db(path: str | None = None):
    """Connessione breve a rssintel.db (autocommit: transazioni esplicite)."""
    # mode=rw: un percorso sbagliato da' errore invece di creare un DB vuoto
    p = path or DB_PATH
    con = sqlite3.connect(f"file:{urllib.parse.quote(p)}?mode=rw", uri=True,
                          timeout=10, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        yield con
    finally:
        if con.in_transaction:          # non deve succedere: lo si dice forte
            con.execute("ROLLBACK")
            con.close()
            raise RuntimeError("transazione lasciata aperta su rssintel.db")
        con.close()


@contextlib.contextmanager
def write_tx(con: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, ROLLBACK su errore."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


@contextlib.contextmanager
def state():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    con = sqlite3.connect(STATE_DB, timeout=5, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        yield con
    finally:
        con.close()


# ------------------------------------------------------------- schema e avvio

def missing_tables() -> list[str]:
    with db() as con:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
    return [t for t in REQUIRED_TABLES if t not in have]


def ensure_schema(with_state: bool = True) -> None:
    """Tabelle del bot, idempotente. Le tabelle del portale non si toccano.

    with_state=False per la riga di comando: la StateDirectory esiste solo
    dopo il primo avvio del servizio.
    """
    with db() as con:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tg\\_%' ESCAPE '\\'"
        ).fetchall()}
        if not {"tg_links", "tg_link_codes"} <= have:
            with write_tx(con):
                for stmt in TG_SCHEMA.split(";"):
                    if stmt.strip():
                        con.execute(stmt)
    if with_state:
        with state() as s:
            s.executescript(STATE_SCHEMA)


def db_permissions() -> list[str]:
    """Problemi di permessi che romperebbero la condivisione del DB in WAL.

    La cartella deve essere setgid e scrivibile dal gruppo (2775, mai sticky:
    con fs.protected_regular=2 www-data non potrebbe aprire i -wal/-shm creati
    dal bot), il database 664 del gruppo www-data.
    """
    problems = []
    d = os.path.dirname(os.path.abspath(DB_PATH))
    try:
        sd, sf = os.stat(d), os.stat(DB_PATH)
    except OSError as e:
        return [f"stat fallito: {e.strerror}"]
    mode_d, mode_f = stat.S_IMODE(sd.st_mode), stat.S_IMODE(sf.st_mode)
    if not mode_d & stat.S_ISGID:
        problems.append(f"cartella {mode_d:o}: manca il setgid (attesa 2775)")
    if mode_d & stat.S_ISVTX:
        problems.append(f"cartella {mode_d:o}: sticky bit presente, va tolto (attesa 2775)")
    if not mode_d & stat.S_IWGRP:
        problems.append(f"cartella {mode_d:o}: non scrivibile dal gruppo (attesa 2775)")
    if mode_f & 0o660 != 0o660:
        problems.append(f"database {mode_f:o}: atteso 664")
    try:
        gname = grp.getgrgid(sf.st_gid).gr_name
    except KeyError:
        gname = str(sf.st_gid)
    if sf.st_gid != sd.st_gid:
        problems.append(f"database del gruppo {gname}, diverso da quello della cartella")
    return problems


def db_writable() -> bool:
    """Il DB e' scrivibile da qui (dentro la sandbox)? Transazione vuota."""
    try:
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
        return True
    except sqlite3.Error:
        return False


def sandbox_report(hidden: list[str], readonly: list[str]) -> list[tuple[str, bool]]:
    """Controlla dall'interno del servizio che la sandbox sia quella attesa.

    hidden:   percorsi che devono risultare inesistenti o illeggibili
    readonly: cartelle in cui la scrittura deve fallire
    """
    out = [("database scrivibile", db_writable())]
    for p in hidden:
        try:
            os.listdir(p)
            ok = False
        except OSError:
            ok = True
        out.append((f"{p} invisibile", ok))
    for p in readonly:
        probe = os.path.join(p, f".rssintel-bot-probe-{os.getpid()}")
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            os.unlink(probe)
            ok = False
        except OSError:
            ok = True
        out.append((f"{p} in sola lettura", ok))
    return out


# ------------------------------------------------------- collegamento account

def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def new_link_code(username: str) -> str:
    """Codice monouso per collegare un account Telegram all'utente RSSIntel.

    Si conserva solo l'impronta: chi legge il database (o il backup) non puo'
    usare un codice ancora valido.
    """
    with db() as con:
        r = con.execute("SELECT disabled FROM users WHERE username = ?", (username,)).fetchall()
        if not r:
            raise ValueError(f"utente RSSIntel inesistente: {username}")
        if int(r[0][0] or 0) == 1:
            raise ValueError(f"utente RSSIntel disattivato: {username}")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        with write_tx(con):
            con.execute("DELETE FROM tg_link_codes WHERE expires_at < datetime('now', '-1 day')")
            con.execute(
                "INSERT INTO tg_link_codes(code_hash, username, expires_at) "
                "VALUES (?, ?, datetime('now', ?))",
                (_hash_code(code), username, f"+{CODE_TTL_MIN} minutes"))
    return code


def redeem_link_code(code: str, tg_user_id: int, chat_id: int) -> str | None:
    """Consuma il codice e crea il collegamento. Ritorna lo username o None.

    Tutto in una transazione: codice segnato come usato, eventuale
    collegamento precedente dello stesso account Telegram revocato, nuovo
    collegamento creato.
    """
    if not code or len(code) > 32:
        return None
    h = _hash_code(code)
    with db() as con:
        with write_tx(con):
            r = con.execute(
                "SELECT c.username FROM tg_link_codes c JOIN users u ON u.username = c.username "
                "WHERE c.code_hash = ? AND c.used_at IS NULL AND c.expires_at > datetime('now') "
                "AND COALESCE(u.disabled, 0) = 0", (h,)).fetchall()
            if not r:
                return None
            username = r[0][0]
            con.execute("UPDATE tg_link_codes SET used_at = datetime('now') WHERE code_hash = ?", (h,))
            con.execute(
                "UPDATE tg_links SET revoked_at = datetime('now') "
                "WHERE tg_user_id = ? AND revoked_at IS NULL", (tg_user_id,))
            con.execute(
                "INSERT INTO tg_links(username, tg_user_id, chat_id, last_seen_at) "
                "VALUES (?, ?, ?, datetime('now'))", (username, tg_user_id, chat_id))
    return username


def resolve_link(tg_user_id: int) -> dict | None:
    """Collegamento attivo + utente RSSIntel, riletti dal DB a ogni richiesta.

    Niente cache: una disattivazione o uno «Scollega» dal portale valgono
    dal comando successivo.
    """
    with db() as con:
        r = con.execute(
            "SELECT l.id, l.username, l.chat_id, u.role, COALESCE(u.disabled, 0), l.blocked_at "
            "FROM tg_links l JOIN users u ON u.username = l.username "
            "WHERE l.tg_user_id = ? AND l.revoked_at IS NULL", (tg_user_id,)).fetchall()
    if not r:
        return None
    lid, username, chat_id, role, disabled, blocked = r[0]
    return {"link_id": lid, "username": username, "chat_id": chat_id,
            "role": role, "disabled": bool(disabled), "blocked": blocked is not None}


def touch_link(link_id: int) -> None:
    """Aggiorna last_seen_at al massimo una volta ogni 10 minuti."""
    with db() as con:
        cur = con.execute(
            "UPDATE tg_links SET last_seen_at = datetime('now') WHERE id = ? "
            "AND (last_seen_at IS NULL OR last_seen_at < datetime('now', '-10 minutes'))",
            (link_id,))
        cur.close()


def set_blocked(tg_user_id: int, blocked: bool) -> None:
    with db() as con:
        con.execute(
            "UPDATE tg_links SET blocked_at = " + ("datetime('now')" if blocked else "NULL") +
            " WHERE tg_user_id = ? AND revoked_at IS NULL", (tg_user_id,))


def revoke(link_id: int | None = None, username: str | None = None) -> int:
    with db() as con:
        if link_id is not None:
            cur = con.execute("UPDATE tg_links SET revoked_at = datetime('now') "
                              "WHERE id = ? AND revoked_at IS NULL", (link_id,))
        else:
            cur = con.execute("UPDATE tg_links SET revoked_at = datetime('now') "
                              "WHERE username = ? AND revoked_at IS NULL", (username,))
        return cur.rowcount


def active_links() -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT id, username, chat_id, linked_at, last_seen_at, blocked_at FROM tg_links "
            "WHERE revoked_at IS NULL ORDER BY id").fetchall()
    return [dict(zip(("id", "username", "chat_id", "linked_at", "last_seen_at", "blocked_at"), r))
            for r in rows]


# -------------------------------------------------------------------- stato

def fmt_dt(s: str | None) -> str:
    """'YYYY-MM-DD HH:MM:SS' UTC (datetime('now') di SQLite) -> '14/10 21:33' locale."""
    if not s:
        return "—"
    try:
        d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return s
    loc = d.astimezone(TZ)
    return loc.strftime("%d/%m %H:%M" if loc.year == datetime.now(TZ).year else "%d/%m/%Y %H:%M")


def status() -> dict:
    with db() as con:
        last_fetch = con.execute("SELECT MAX(last_fetch_at) FROM feeds WHERE enabled = 1").fetchall()[0][0]
        n_feeds, n_err = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN COALESCE(last_error, '') <> '' THEN 1 ELSE 0 END) "
            "FROM feeds WHERE enabled = 1").fetchall()[0]
        # fetched_at non ha indice: si guardano solo gli ultimi id (~200 al giorno),
        # altrimenti a cache fredda la scansione dei 300 MB richiede secondi
        n_24h = con.execute(
            "SELECT COUNT(*) FROM items WHERE id > (SELECT MAX(id) - 5000 FROM items) "
            "AND fetched_at >= datetime('now', '-1 day')").fetchall()[0][0]
        n_watch = con.execute("SELECT COUNT(*) FROM saved_searches WHERE watch = 1").fetchall()[0][0]
    return {"last_fetch": last_fetch, "feeds": n_feeds, "feeds_err": n_err or 0,
            "items_24h": n_24h, "watches": n_watch}


def count_usage(cmd: str) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with state() as s:
        s.execute("INSERT INTO usage(day, cmd, n) VALUES (?, ?, 1) "
                  "ON CONFLICT(day, cmd) DO UPDATE SET n = n + 1", (day, cmd))


# ===================================================== ricerca e lettura (L2)
#
# Stessa query di search.php: la stringa dell'utente va a MATCH cosi' com'e',
# risultati per data (non per pertinenza), stesso snippet(), stesso filtro per
# categoria. In piu' il bot e' tollerante: se FTS rifiuta la query (tipico da
# telefono: «l'Iran», «Al-Aqsa», «Iran?»), la riprova con le parole tra
# virgolette e dice come l'ha interpretata. La forma che ha funzionato e' quella
# da salvare in una sentinella, cosi' web, bot e worker eseguono la stessa.

import gzip
import html as _html
import re as _re
import time as _time
from urllib.parse import urlsplit

PER_PAGE = 6
# Oltre questo tempo la query e' troppo larga e si interrompe. A cache calda una
# ricerca comune («Iran», 7.000 risultati) richiede ~10 ms; a cache fredda e con il
# disco sotto pressione arriva a 2-3 s: il limite non deve colpire quelle.
SEARCH_BUDGET_S = float(os.environ.get("RSSINTEL_BOT_SEARCH_BUDGET_S", "8"))
TEXT_MAX = 2_000_000            # caratteri letti al massimo da un testo
FTS_OPERATORS = {"AND", "OR", "NOT"}

CATEGORIES = {   # come source_categories() in lib.php
    "generalista": "generalista", "specialistica": "specialistica",
    "istituzionale": "istituzionale", "analisi": "analisi",
    "blog": "blog", "advocacy": "advocacy",
}
CATEGORY_KEYS = ["", *CATEGORIES, "__none"]      # indice -> chiave (callback_data compatti)
RELIABILITY = {"A", "B", "C", "D", "E", "F"}

TEXT_DIR = os.environ.get("RSSINTEL_TXT_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "text")


class SearchError(Exception):
    """Query che FTS non accetta in nessuna forma."""


class SearchTooBroad(Exception):
    """Query interrotta perche' oltre il tempo concesso."""


def _quote(tok: str) -> str:
    return '"' + tok.replace('"', '""') + '"'


def fts_forms(q: str) -> list[str]:
    """La query come l'ha scritta, poi due ripieghi sempre piu' letterali."""
    toks = q.split()
    semi = " ".join(t if t in FTS_OPERATORS else _quote(t) for t in toks)
    full = " ".join(_quote(t) for t in toks)
    out = []
    for f in (q, semi, full):
        if f and f not in out:
            out.append(f)
    return out


def _fts_rejects(e: sqlite3.Error) -> bool:
    """Errore dovuto alla query (sintassi FTS5 in tutte le sue forme) e non al sistema.

    Elencare le frasi di FTS5 non basta: ne restavano fuori alcune
    («expected integer», «parser stack overflow»). Si escludono invece gli
    errori di sistema, che non vanno mascherati da query sbagliata.
    """
    m = str(e).lower()
    return not any(s in m for s in ("interrupted", "locked", "busy", "disk i/o", "unable to open",
                                    "readonly", "corrupt", "no such table"))


def category_filter(cat: str) -> tuple[str, tuple]:
    if cat == "__none":
        return " AND f.category IS NULL ", ()
    if cat in CATEGORIES:
        return " AND f.category = ? ", (cat,)
    return "", ()


def snippet_html(snip: str | None) -> str:
    """Prima l'escape, poi i marcatori: l'ordine che usa search.php."""
    s = _html.escape(" ".join((snip or "").split()), quote=False)   # niente a capo dentro l'estratto
    return s.replace("\x02", "<b>").replace("\x03", "</b>")


def clean_title(t: str | None) -> str:
    """Testo semplice: alcuni feed mandano i titoli con le entita' gia' scritte (&amp;)."""
    return _html.unescape(t or "").strip()


def search(q: str, cat: str = "", page: int = 1, per_page: int = PER_PAGE) -> dict:
    q = q.strip()
    cat_sql, cat_args = category_filter(cat)
    deadline = _time.monotonic() + SEARCH_BUDGET_S
    with db() as con:
        con.set_progress_handler(lambda: _time.monotonic() > deadline, 10_000)
        try:
            last = None
            for form in fts_forms(q):
                try:
                    total = con.execute(
                        "SELECT COUNT(*) FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                        "JOIN feeds f ON f.id = i.feed_id WHERE items_fts MATCH ?" + cat_sql,
                        (form, *cat_args)).fetchall()[0][0]
                except sqlite3.OperationalError as e:
                    if "interrupted" in str(e):
                        raise SearchTooBroad() from None
                    if _fts_rejects(e):
                        last = e
                        continue
                    raise
                pages = (total + per_page - 1) // per_page
                page = max(1, min(page, pages or 1))       # come il web: oltre la fine -> ultima
                try:
                    # Stessa query di search.php ma SENZA snippet(): con ORDER BY ... LIMIT
                    # SQLite lo calcolerebbe per tutte le corrispondenze prima di ordinare
                    # (37.000 per «a*»: 5 s a cache calda, 16 s a freddo). Gli estratti si
                    # calcolano dopo, solo per le righe della pagina: risultati e ordine identici.
                    rows = con.execute(
                        "SELECT i.id, i.title, i.link, i.published_at, i.fetched_at, "
                        "COALESCE(f.title, f.url), f.category, f.reliability "
                        "FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                        "JOIN feeds f ON f.id = i.feed_id WHERE items_fts MATCH ?" + cat_sql +
                        " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ? OFFSET ?",
                        (form, *cat_args, per_page, (page - 1) * per_page)).fetchall()
                    snips = {}
                    if rows:
                        ids = [r[0] for r in rows]
                        snips = dict(con.execute(
                            "SELECT rowid, snippet(items_fts, 1, char(2), char(3), '…', 24) FROM items_fts "
                            "WHERE items_fts MATCH ? AND rowid IN (" + ",".join("?" * len(ids)) + ")",
                            (form, *ids)).fetchall())
                except sqlite3.OperationalError as e:
                    if "interrupted" in str(e):
                        raise SearchTooBroad() from None
                    raise
                rows = [(*r, snips.get(r[0])) for r in rows]
                keys = ("id", "title", "link", "published_at", "fetched_at", "feed",
                        "category", "reliability", "snippet")
                return {"q": q, "effective": form, "reinterpreted": form != q, "cat": cat,
                        "total": total, "pages": pages, "page": page,
                        "rows": [dict(zip(keys, r)) for r in rows]}
            raise SearchError(str(last) if last else "query vuota")
        finally:
            con.set_progress_handler(None, 0)


def badge(category: str | None, reliability: str | None) -> str:
    """Come source_badge() in lib.php, in testo: «B · specialistica»."""
    parts = []
    rel = (reliability or "").upper()
    if rel in RELIABILITY:
        parts.append(rel)
    if category in CATEGORIES:
        parts.append(CATEGORIES[category])
    return " · ".join(parts)


def safe_url(u: str | None) -> str:
    """Come safe_url() in lib.php ma SENZA escape HTML: http(s), niente caratteri
    di controllo. L'escape si fa solo dove serve (mai sui bottoni URL)."""
    u = (u or "").strip()
    if not _re.match(r"(?i)^https?://", u) or _re.search(r"[\x00-\x1f\x7f]", u):
        return ""
    return u


def host(u: str) -> str:
    try:
        h = (urlsplit(u).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def read_text(path: str | None) -> str:
    """Testo dell'articolo, solo dentro la cartella dei testi (.txt o .txt.gz)."""
    if not path:
        return ""
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(TEXT_DIR)
        if (os.path.commonpath([real, base]) != base or not os.path.isfile(real)
                or not real.endswith((".txt", ".txt.gz"))):
            return ""
        if real.endswith(".gz"):
            with gzip.open(real, "rb") as f:
                raw = f.read(TEXT_MAX * 4)
        else:
            with open(real, "rb") as f:
                raw = f.read(TEXT_MAX * 4)
    except Exception:               # anche zlib.error di un .gz danneggiato: niente testo, non un crash
        return ""
    return raw.decode("utf-8", "replace")[:TEXT_MAX].strip()


def get_item(item_id: int) -> dict | None:
    with db() as con:
        r = con.execute(
            "SELECT i.id, i.title, i.link, i.published_at, i.fetched_at, i.text_path, "
            "COALESCE(f.title, f.url), f.category, f.reliability "
            "FROM items i JOIN feeds f ON f.id = i.feed_id WHERE i.id = ?", (item_id,)).fetchall()
    if not r:
        return None
    keys = ("id", "title", "link", "published_at", "fetched_at", "text_path", "feed", "category", "reliability")
    return dict(zip(keys, r[0]))


def split_text(text: str, size: int = 3500) -> list[str]:
    """Pezzi da al massimo `size` caratteri, tagliati ai capoversi quando si puo'."""
    out, cur = [], ""
    for para in _re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:                 # capoverso enorme: si taglia a una frase o a uno spazio
            cut = max(para.rfind(". ", 0, size), para.rfind(" ", 0, size))
            cut = cut + 1 if cut > size // 2 else size
            if cur:
                out.append(cur); cur = ""
            out.append(para[:cut].strip()); para = para[cut:].strip()
        if len(cur) + len(para) + 2 > size and cur:
            out.append(cur); cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


# ---------------------------------------- contesti di paginazione (state.db)
# I bottoni portano solo un gettone corto: la query vera resta qui, legata al
# collegamento che l'ha creata e valida 48 ore. Un bottone falsificato o
# vecchio non puo' far eseguire una query arbitraria.

CTX_TTL_S = 48 * 3600
CTX_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


CTX_SCHEMA = ("CREATE TABLE IF NOT EXISTS ctx (tok TEXT PRIMARY KEY, link_id INTEGER, "
              "q TEXT, orig TEXT, cat TEXT, created REAL)")


def ctx_new(link_id: int, q: str, orig: str, cat: str = "") -> str:
    """q = forma eseguita (quella che ha funzionato), orig = come l'ha scritta l'utente."""
    tok = "".join(secrets.choice(CTX_ALPHABET) for _ in range(8))
    now = _time.time()
    with state() as s:
        s.execute(CTX_SCHEMA)
        s.execute("DELETE FROM ctx WHERE created < ?", (now - CTX_TTL_S,))
        s.execute("INSERT INTO ctx VALUES (?, ?, ?, ?, ?, ?)", (tok, link_id, q, orig, cat, now))
    return tok


def ctx_get(tok: str, link_id: int) -> dict | None:
    with state() as s:
        s.execute(CTX_SCHEMA)
        r = s.execute("SELECT q, orig, cat FROM ctx WHERE tok = ? AND link_id = ? AND created >= ?",
                      (tok, link_id, _time.time() - CTX_TTL_S)).fetchall()
    return {"q": r[0][0], "orig": r[0][1], "cat": r[0][2] or ""} if r else None


def ctx_set_cat(tok: str, link_id: int, cat: str) -> None:
    with state() as s:
        s.execute("UPDATE ctx SET cat = ? WHERE tok = ? AND link_id = ?", (cat, tok, link_id))
