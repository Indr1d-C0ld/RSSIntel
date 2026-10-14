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
CREATE TABLE IF NOT EXISTS tg_watch_prefs (
  search_id    INTEGER PRIMARY KEY REFERENCES saved_searches(id) ON DELETE CASCADE,
  mode         TEXT NOT NULL DEFAULT 'digest' CHECK (mode IN ('instant', 'digest', 'novita')),
  detail       TEXT NOT NULL DEFAULT 'titles' CHECK (detail IN ('titles', 'counts')),
  paused_until TEXT,
  expires_at   TEXT,
  reminded_at  TEXT,
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
TG_TABLES = {"tg_links", "tg_link_codes", "tg_watch_prefs"}

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
        if not TG_TABLES <= have:
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
    """Aggiorna last_seen_at al massimo una volta ogni 10 minuti.

    Chi scrive al bot non lo ha bloccato: si azzera anche blocked_at, perche'
    l'evento «sbloccato» puo' andare perso (aggiornamenti scartati al riavvio).
    """
    with db() as con:
        cur = con.execute(
            "UPDATE tg_links SET last_seen_at = datetime('now'), blocked_at = NULL WHERE id = ? "
            "AND (last_seen_at IS NULL OR last_seen_at < datetime('now', '-10 minutes') OR blocked_at IS NOT NULL)",
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


# ========================================================== sentinelle (L3)
#
# Una sentinella e' una ricerca salvata con l'allerta accesa (saved_searches,
# watch=1): la stessa cosa che si crea dal portale, scritta con la stessa SQL
# di search.php, cosi' compare con 🔔 sul web e rssintel_watch.py la riesegue
# dopo ogni raccolta. Le preferenze che riguardano solo il bot (modalita' di
# notifica, pausa, scadenza) stanno in tg_watch_prefs, che sparisce da sola
# con la ricerca (ON DELETE CASCADE): PHP e worker non vanno toccati.

MODES = {"instant": "⚡ Istantanea", "digest": "📅 Riepilogo", "novita": "📥 Solo Novità"}
MODE_CODE = {"i": "instant", "d": "digest", "n": "novita"}
DEFAULT_MODE = "digest"         # anche per le sentinelle create dal web
NAME_MAX = 80                   # come search.php
ESTIMATE_DAYS = 14


def resolve_query(q: str) -> tuple[str, int]:
    """Forma che FTS accetta (la stessa logica della ricerca) e totale."""
    r = search(q, per_page=1)
    return r["effective"], r["total"]


def estimate(q_effective: str, days: int = ESTIMATE_DAYS) -> dict:
    """Articoli al giorno che la sentinella avrebbe trovato: la stessa
    condizione di rssintel_watch.py (MATCH + fetched_at), sugli ultimi giorni."""
    deadline = _time.monotonic() + SEARCH_BUDGET_S
    with db() as con:
        con.set_progress_handler(lambda: _time.monotonic() > deadline, 10_000)
        try:
            rows = con.execute(
                "SELECT date(i.fetched_at), COUNT(*) FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                "WHERE items_fts MATCH ? AND i.fetched_at > datetime('now', ?) GROUP BY 1",
                (q_effective, f"-{days} days")).fetchall()
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise SearchTooBroad() from None
            raise
        finally:
            con.set_progress_handler(None, 0)
    total = sum(n for _, n in rows)
    return {"days": days, "total": total, "per_day": total / days, "max_day": max((n for _, n in rows), default=0)}


def suggest_mode(per_day: float) -> str:
    # oltre 5 al giorno una notifica per giro di raccolta diventa rumore
    return "digest" if per_day > 5 else "instant"


def free_name(owner: str, name: str, q: str) -> tuple[str, bool]:
    """Nome per una nuova sentinella che non sovrascriva una ricerca salvata diversa.

    Il nome lo propone il bot (dalla query), non l'utente: se esiste gia' una
    ricerca con quel nome ma un'altra query o un filtro per fonte, si usa
    «nome (2)», «nome (3)»... Ritorna (nome, esiste_gia_identica).
    """
    base = " ".join(name.split())[:NAME_MAX] or q[:NAME_MAX]
    with db() as con:
        for n in range(1, 100):
            cand = base if n == 1 else f"{base[:NAME_MAX - 6]} ({n})"
            r = con.execute("SELECT q, feed_id FROM saved_searches WHERE owner = ? AND name = ?",
                            (owner, cand)).fetchall()
            if not r:
                return cand, False
            if r[0][0] == q and r[0][1] is None:
                return cand, True              # la stessa ricerca: la si aggiorna
    raise ValueError("troppe ricerche con lo stesso nome")


def create_watch(owner: str, name: str, q: str, mode: str, expire_days: int | None) -> tuple[int, bool]:
    """Salva (o aggiorna) la ricerca e accende l'allerta. Ritorna (id, sostituita).

    Stessa SQL di search.php: upsert su (owner, name) e punto di partenza
    «adesso» solo se l'allerta non era gia' accesa (niente storico).
    """
    name = name.strip()[:NAME_MAX]
    if mode not in MODES:
        raise ValueError("modalita' non valida")
    with db() as con:
        with write_tx(con):
            old = con.execute("SELECT q, feed_id, result_limit FROM saved_searches WHERE owner = ? AND name = ?",
                              (owner, name)).fetchall()
            con.execute(
                "INSERT INTO saved_searches(owner, name, q, feed_id, result_limit) VALUES (?, ?, ?, NULL, 50) "
                "ON CONFLICT(owner, name) DO UPDATE SET q = excluded.q, feed_id = excluded.feed_id, "
                "result_limit = excluded.result_limit, created_at = datetime('now')",
                (owner, name, q))
            con.execute(
                "UPDATE saved_searches SET last_checked_at = CASE WHEN watch = 1 THEN last_checked_at "
                "ELSE datetime('now') END, watch = 1, last_error = NULL WHERE owner = ? AND name = ?",
                (owner, name))
            sid = con.execute("SELECT id FROM saved_searches WHERE owner = ? AND name = ?",
                              (owner, name)).fetchall()[0][0]
            con.execute(
                "INSERT INTO tg_watch_prefs(search_id, mode, expires_at) VALUES (?, ?, "
                "CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', '+' || ? || ' days') END) "
                "ON CONFLICT(search_id) DO UPDATE SET mode = excluded.mode, expires_at = excluded.expires_at, "
                "reminded_at = NULL, paused_until = NULL, updated_at = datetime('now')",
                (sid, mode, expire_days, expire_days))
    return sid, bool(old and (old[0][0] != q or old[0][1] is not None))


_WATCH_COLS = ("id", "name", "q", "watch", "last_error", "mode", "paused_until", "expires_at", "unseen", "detail")


def list_watches(owner: str) -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT s.id, s.name, s.q, s.watch, s.last_error, COALESCE(p.mode, ?), "
            "CASE WHEN p.paused_until > datetime('now') THEN p.paused_until END, p.expires_at, "
            "(SELECT COUNT(*) FROM watch_hits w WHERE w.search_id = s.id AND w.seen = 0), COALESCE(p.detail, 'titles') "
            "FROM saved_searches s LEFT JOIN tg_watch_prefs p ON p.search_id = s.id "
            "WHERE s.owner = ? ORDER BY s.watch DESC, s.name COLLATE NOCASE",
            (DEFAULT_MODE, owner)).fetchall()
    return [dict(zip(_WATCH_COLS, r)) for r in rows]


def get_watch(owner: str, sid: int) -> dict | None:
    return next((w for w in list_watches(owner) if w["id"] == sid), None)


def _prefs_upsert(con, sid: int) -> None:
    con.execute("INSERT INTO tg_watch_prefs(search_id, mode) VALUES (?, ?) ON CONFLICT(search_id) DO NOTHING",
                (sid, DEFAULT_MODE))


def _owned(con, owner: str, sid: int) -> bool:
    return bool(con.execute("SELECT 1 FROM saved_searches WHERE id = ? AND owner = ?", (sid, owner)).fetchall())


def set_mode(owner: str, sid: int, mode: str) -> bool:
    if mode not in MODES:
        return False
    with db() as con:
        with write_tx(con):
            if not _owned(con, owner, sid):
                return False
            _prefs_upsert(con, sid)
            con.execute("UPDATE tg_watch_prefs SET mode = ?, updated_at = datetime('now') WHERE search_id = ?",
                        (mode, sid))
    return True


def set_pause(owner: str, sid: int, days: int) -> bool:
    """Pausa delle notifiche (la sentinella continua a raccogliere in Novita'). 0 = riprendi."""
    with db() as con:
        with write_tx(con):
            if not _owned(con, owner, sid):
                return False
            _prefs_upsert(con, sid)
            con.execute("UPDATE tg_watch_prefs SET paused_until = CASE WHEN ? = 0 THEN NULL "
                        "ELSE datetime('now', '+' || ? || ' days') END, updated_at = datetime('now') "
                        "WHERE search_id = ?", (days, days, sid))
    return True


def set_watch(owner: str, sid: int, on: bool) -> bool:
    """Accende o spegne l'allerta, come i pulsanti di search.php."""
    with db() as con:
        cur = con.execute(
            "UPDATE saved_searches SET watch = 1, last_checked_at = datetime('now'), last_error = NULL "
            "WHERE id = ? AND owner = ? AND watch = 0" if on else
            "UPDATE saved_searches SET watch = 0 WHERE id = ? AND owner = ?", (sid, owner))
        return cur.rowcount > 0


def extend_watch(owner: str, sid: int, days: int) -> bool:
    with db() as con:
        with write_tx(con):
            if not _owned(con, owner, sid):
                return False
            _prefs_upsert(con, sid)
            # «almeno N giorni da adesso»: un secondo tocco non aggiunge altri giorni
            con.execute(
                "UPDATE tg_watch_prefs SET expires_at = MAX(COALESCE(expires_at, ''), datetime('now', '+' || ? || ' days')), "
                "reminded_at = NULL, updated_at = datetime('now') WHERE search_id = ?", (days, sid))
            # prolungare una sentinella gia' scaduta (spenta) vuol dire riaccenderla
            con.execute("UPDATE saved_searches SET watch = 1, last_checked_at = datetime('now'), last_error = NULL "
                        "WHERE id = ? AND watch = 0", (sid,))
    return True


def delete_watch(owner: str, sid: int) -> bool:
    """Come «Elimina» in search.php: via la ricerca, le corrispondenze e le preferenze (cascata)."""
    with db() as con:
        cur = con.execute("DELETE FROM saved_searches WHERE id = ? AND owner = ?", (sid, owner))
        return cur.rowcount > 0


def unseen(owner: str, per_search: int = 5) -> list[dict]:
    """Segnalazioni non viste raggruppate per sentinella, le piu' recenti prima."""
    with db() as con:
        groups = con.execute(
            "SELECT s.id, s.name, COUNT(*) FROM watch_hits w JOIN saved_searches s ON s.id = w.search_id "
            "WHERE s.owner = ? AND w.seen = 0 GROUP BY s.id ORDER BY MAX(w.id) DESC", (owner,)).fetchall()
        out = []
        for sid, name, n in groups:
            items = con.execute(
                "SELECT i.id, i.title, COALESCE(f.title, f.url), i.published_at, i.fetched_at "
                "FROM watch_hits w JOIN items i ON i.id = w.item_id JOIN feeds f ON f.id = i.feed_id "
                "WHERE w.search_id = ? AND w.seen = 0 "
                "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?", (sid, per_search)).fetchall()
            out.append({"id": sid, "name": name, "n": n, "items": [
                dict(zip(("id", "title", "feed", "published_at", "fetched_at"), r)) for r in items]})
    return out


def mark_seen(owner: str, sid: int | None = None) -> int:
    """Stessa SQL di novita.php: solo le sentinelle dell'utente."""
    with db() as con:
        if sid is None:
            cur = con.execute("UPDATE watch_hits SET seen = 1 WHERE seen = 0 AND search_id IN "
                              "(SELECT id FROM saved_searches WHERE owner = ?)", (owner,))
        else:
            cur = con.execute("UPDATE watch_hits SET seen = 1 WHERE seen = 0 AND search_id = ? AND search_id IN "
                              "(SELECT id FROM saved_searches WHERE owner = ?)", (sid, owner))
        return cur.rowcount


def chats_of(username: str) -> list[int]:
    with db() as con:
        return [r[0] for r in con.execute(
            "SELECT l.chat_id FROM tg_links l JOIN users u ON u.username = l.username "
            "WHERE l.username = ? AND l.revoked_at IS NULL AND l.blocked_at IS NULL AND COALESCE(u.disabled, 0) = 0",
            (username,)).fetchall()]


def expire_due() -> list[dict]:
    """Spegne le sentinelle scadute. Scatta UNA volta: insieme all'allerta si
    azzera la scadenza, cosi' se la riaccendi dal web non si rispegne."""
    with db() as con:
        with write_tx(con):
            rows = con.execute(
                "SELECT s.id, s.owner, s.name, s.watch FROM tg_watch_prefs p JOIN saved_searches s ON s.id = p.search_id "
                "WHERE p.expires_at IS NOT NULL AND p.expires_at <= datetime('now')").fetchall()
            for sid, *_ in rows:
                con.execute("UPDATE saved_searches SET watch = 0 WHERE id = ?", (sid,))
                con.execute("UPDATE tg_watch_prefs SET expires_at = NULL, reminded_at = NULL, "
                            "updated_at = datetime('now') WHERE search_id = ?", (sid,))
    # gia' spente a mano: si azzera la scadenza senza avvisare
    return [{"id": a, "owner": b, "name": c} for a, b, c, w in rows if w == 1]


def reminders_due() -> list[dict]:
    """Sentinelle che scadono entro 24 ore e non sono ancora state ricordate.
    Si segnano con mark_reminded() solo dopo un invio riuscito."""
    with db() as con:
        rows = con.execute(
            "SELECT s.id, s.owner, s.name, p.expires_at FROM tg_watch_prefs p "
            "JOIN saved_searches s ON s.id = p.search_id WHERE s.watch = 1 AND p.reminded_at IS NULL "
            "AND p.expires_at IS NOT NULL AND p.expires_at <= datetime('now', '+1 day') "
            "AND p.expires_at > datetime('now')").fetchall()
    return [{"id": a, "owner": b, "name": c, "expires_at": d} for a, b, c, d in rows]


def mark_reminded(sid: int) -> None:
    """Dopo l'invio riuscito: un errore di rete non deve far perdere il promemoria."""
    with db() as con:
        con.execute("UPDATE tg_watch_prefs SET reminded_at = datetime('now') WHERE search_id = ?", (sid,))


def load_suggestions(path: str | None) -> list[dict]:
    """Sentinelle da proporre al primo collegamento, da un file fuori dal codice
    (sono interessi personali: non devono finire nel repo pubblico).
    Una per riga:  Nome | query FTS | istantanea|riepilogo|novita"""
    out = []
    if not path:
        return out
    word = {"istantanea": "instant", "riepilogo": "digest", "novita": "novita", "novità": "novita"}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split("|")]
                if len(parts) == 3 and parts[0] and parts[1] and word.get(parts[2].lower()):
                    out.append({"name": parts[0][:NAME_MAX], "q": parts[1], "mode": word[parts[2].lower()]})
    except (OSError, UnicodeError, ValueError):     # file mancante o non UTF-8: nessuna proposta, non un crash
        return []
    return out[:8]


# --------------------------------------- creazione in due passi (state.db)

PEND_SCHEMA = ("CREATE TABLE IF NOT EXISTS pend (tok TEXT PRIMARY KEY, link_id INTEGER, q TEXT, orig TEXT, "
               "name TEXT, mode TEXT, days INTEGER, created REAL)")
PEND_TTL_S = 3600


def pend_new(link_id: int, q: str, orig: str, name: str, mode: str, days: int | None) -> str:
    tok = "".join(secrets.choice(CTX_ALPHABET) for _ in range(8))
    now = _time.time()
    with state() as s:
        s.execute(PEND_SCHEMA)
        s.execute("DELETE FROM pend WHERE created < ?", (now - PEND_TTL_S,))
        s.execute("INSERT INTO pend VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (tok, link_id, q, orig, name, mode, days, now))
    return tok


def pend_get(tok: str, link_id: int) -> dict | None:
    with state() as s:
        s.execute(PEND_SCHEMA)
        r = s.execute("SELECT q, orig, name, mode, days FROM pend WHERE tok = ? AND link_id = ? AND created >= ?",
                      (tok, link_id, _time.time() - PEND_TTL_S)).fetchall()
    return dict(zip(("q", "orig", "name", "mode", "days"), r[0])) if r else None


def pend_set(tok: str, link_id: int, **kw) -> None:
    cols = {k: v for k, v in kw.items() if k in ("mode", "days")}
    if not cols:
        return
    with state() as s:
        s.execute(f"UPDATE pend SET {', '.join(c + ' = ?' for c in cols)} WHERE tok = ? AND link_id = ?",
                  (*cols.values(), tok, link_id))


def pend_drop(tok: str, link_id: int) -> bool:
    """Consuma la richiesta: un secondo «Crea» sullo stesso messaggio non fa nulla."""
    with state() as s:
        s.execute(PEND_SCHEMA)
        return s.execute("DELETE FROM pend WHERE tok = ? AND link_id = ?", (tok, link_id)).rowcount > 0


# ======================================================== notifiche (L4)
#
# Le segnalazioni le scrive solo rssintel_watch.py (watch_hits, AUTOINCREMENT,
# una transazione per volta): i loro id arrivano quindi in ordine e un
# segnaposto «ultimo id visto» basta a non perderne e a non ripeterne.
# La coda delle notifiche sta nella StateDirectory del bot, non in rssintel.db:
# i titoli gia' inviati non finiscono nel database condiviso ne' nei backup, e
# per le notifiche rssintel.db si legge soltanto.
#
# Consegna «almeno una volta»: una riga passa a 'sending' prima dell'invio e a
# 'sent' dopo. Se il processo muore in mezzo, all'avvio torna 'pending' e il
# messaggio riparte marcato «↻ reinvio».

import json as _json
from datetime import timedelta

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id   INTEGER NOT NULL,
  chat_id   INTEGER NOT NULL,
  owner     TEXT NOT NULL,
  hit_id    INTEGER NOT NULL,
  search_id INTEGER NOT NULL,
  item_id   INTEGER NOT NULL,
  mode      TEXT NOT NULL,
  due_at    REAL NOT NULL,          -- epoch
  quiet     INTEGER NOT NULL DEFAULT 0,   -- spostata dalle ore di quiete: silenziosa
  state     TEXT NOT NULL DEFAULT 'pending',   -- pending/sending/sent/dropped/collapsed/failed
  attempts  INTEGER NOT NULL DEFAULT 0,
  next_try  REAL NOT NULL DEFAULT 0,
  resend    INTEGER NOT NULL DEFAULT 0,
  batch     INTEGER,
  first_batch INTEGER,             -- primo invio: «✓ Viste» del messaggio originale resta valido dopo un reinvio
  taken_at  REAL,                  -- quando e' passata a 'sending'
  created   REAL NOT NULL,
  sent_at   REAL,
  UNIQUE (link_id, hit_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(state, link_id, due_at);
CREATE INDEX IF NOT EXISTS idx_outbox_batch ON outbox(batch);
CREATE INDEX IF NOT EXISTS idx_outbox_first ON outbox(first_batch);
"""
KEEP_SENT_DAYS = 14             # righe inviate tenute per «✓ Viste» e /stato
FAIL_AFTER_S = 24 * 3600        # oltre, una notifica che non parte si da' per persa
BACKLOG_MAX = 25                # oltre, un riepilogo unico invece della lista
MAX_LINES = 15
TAKE_MAX = 200                  # righe prese per collegamento a ogni giro (liste IN limitate)
STALE_SENDING_S = 600           # 'sending' piu' vecchia di cosi': il giro che la inviava e' fallito
BADREQUEST_MAX = 3              # errori permanenti (chat inesistente...): dopo 3 tentativi, 'failed'


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def push_config() -> dict:
    slots = [ _hhmm(x) for x in os.environ.get("RSSINTEL_BOT_DIGEST_AT", "08:00,13:00,19:00").split(",") if x.strip()]
    q = os.environ.get("RSSINTEL_BOT_QUIET", "02:00-08:00").strip()
    quiet = tuple(_hhmm(x) for x in q.split("-")) if q else None
    return {"slots": sorted(slots), "quiet": quiet,
            "sound_per_hour": int(os.environ.get("RSSINTEL_BOT_SOUND_PER_HOUR", "1")),
            "sound_per_day": int(os.environ.get("RSSINTEL_BOT_SOUND_PER_DAY", "8"))}


def _in_quiet(t: datetime, quiet) -> bool:
    if not quiet:
        return False
    (h1, m1), (h2, m2) = quiet
    a, b, x = h1 * 60 + m1, h2 * 60 + m2, t.hour * 60 + t.minute
    return (a <= x < b) if a < b else (x >= a or x < b)      # anche finestre a cavallo della mezzanotte


def _quiet_end(t: datetime, quiet) -> datetime:
    (h2, m2) = quiet[1]
    end = t.replace(hour=h2, minute=m2, second=0, microsecond=0)
    return end if end > t else end + timedelta(days=1)


def due_at(mode: str, now: float, cfg: dict) -> tuple[float, bool]:
    """Quando inviare una segnalazione: (epoch, spostata_dalla_quiete)."""
    t = datetime.fromtimestamp(now, TZ)
    if mode == "instant":
        due = t
    else:                                       # riepilogo: il prossimo orario fisso
        due = None
        for h, m in cfg["slots"]:
            c = t.replace(hour=h, minute=m, second=0, microsecond=0)
            if c > t:
                due = c
                break
        if due is None:
            h, m = cfg["slots"][0]
            due = (t + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    if _in_quiet(due, cfg["quiet"]):
        return _quiet_end(due, cfg["quiet"]).timestamp(), True
    return due.timestamp(), False


@contextlib.contextmanager
def outbox():
    with state() as s:
        s.executescript(OUTBOX_SCHEMA)
        yield s


def kv_get(key: str, default=None):
    with state() as s:
        s.executescript(STATE_SCHEMA)
        r = s.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchall()
    return _json.loads(r[0][0]) if r else default


def kv_set(key: str, value) -> None:
    with state() as s:
        s.executescript(STATE_SCHEMA)
        s.execute("INSERT INTO kv(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                  (key, _json.dumps(value)))


def enqueue(cfg: dict, now: float | None = None) -> int:
    """Nuove segnalazioni -> coda. Ritorna quante righe sono state accodate."""
    now = now or _time.time()
    cursor = kv_get("hit_cursor")
    with db() as con:
        top = con.execute("SELECT COALESCE(MAX(id), 0) FROM watch_hits").fetchall()[0][0]
        if cursor is None:                      # prima volta: niente storico
            kv_set("hit_cursor", top)
            return 0
        if top <= cursor:
            return 0                            # niente di nuovo: nessuna scrittura
        rows = con.execute(
            "SELECT w.id, w.search_id, w.item_id, s.owner, COALESCE(p.mode, ?), "
            "CASE WHEN p.paused_until > datetime('now') THEN 1 ELSE 0 END "
            "FROM watch_hits w JOIN saved_searches s ON s.id = w.search_id "
            "LEFT JOIN tg_watch_prefs p ON p.search_id = s.id "
            "WHERE w.id > ? AND w.id <= ? AND w.seen = 0 ORDER BY w.id",
            (DEFAULT_MODE, cursor, top)).fetchall()
        links = {}
        for owner in {r[3] for r in rows}:
            links[owner] = con.execute(
                "SELECT l.id, l.chat_id FROM tg_links l JOIN users u ON u.username = l.username "
                "WHERE l.username = ? AND l.revoked_at IS NULL AND l.blocked_at IS NULL "
                "AND COALESCE(u.disabled, 0) = 0", (owner,)).fetchall()
    n = 0
    with outbox() as s:
        s.execute("BEGIN IMMEDIATE")
        try:
            for hit, sid, item, owner, mode, paused in rows:
                if mode == "novita" or paused:
                    continue                    # restano in Novita', senza notifica
                due, quiet = due_at(mode, now, cfg)
                for link_id, chat_id in links.get(owner, ()):
                    n += s.execute(
                        "INSERT OR IGNORE INTO outbox(link_id, chat_id, owner, hit_id, search_id, item_id, mode, "
                        "due_at, quiet, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (link_id, chat_id, owner, hit, sid, item, mode, due, int(quiet), now)).rowcount
            s.execute("COMMIT")
        except BaseException:
            s.execute("ROLLBACK")
            raise
    kv_set("hit_cursor", top)
    return n


def recover_sending(older_than: float | None = None) -> int:
    """Righe rimaste a meta' invio -> di nuovo in coda, marcate come reinvio.
    All'avvio tutte; a ogni giro quelle ferme da piu' di STALE_SENDING_S."""
    with outbox() as s:
        if older_than is None:
            return s.execute("UPDATE outbox SET state = 'pending', resend = 1 WHERE state = 'sending'").rowcount
        return s.execute("UPDATE outbox SET state = 'pending', resend = 1 WHERE state = 'sending' AND taken_at < ?",
                         (_time.time() - older_than,)).rowcount


def due_links(now: float | None = None) -> list[int]:
    now = now or _time.time()
    with outbox() as s:
        return [r[0] for r in s.execute(
            "SELECT DISTINCT link_id FROM outbox WHERE state = 'pending' AND due_at <= ? AND next_try <= ?",
            (now, now)).fetchall()]


def take_batch(link_id: int, now: float | None = None) -> dict | None:
    """Le righe scadute di un collegamento, segnate 'sending' sotto un nuovo batch.

    Prima si scartano quelle gia' viste sul web (o sparite: articolo o ricerca
    eliminati) e quelle di sentinelle passate a «solo Novita'» o in pausa.
    """
    now = now or _time.time()
    with db() as con:
        # il collegamento vale ancora? Scollegato (portale, /esci), bloccato o utente
        # disattivato: le notifiche in coda non partono piu'
        ok = con.execute(
            "SELECT 1 FROM tg_links l JOIN users u ON u.username = l.username WHERE l.id = ? "
            "AND l.revoked_at IS NULL AND l.blocked_at IS NULL AND COALESCE(u.disabled, 0) = 0",
            (link_id,)).fetchall()
    if not ok:
        drop_link(link_id)
        return None
    with outbox() as s:
        rows = s.execute(
            "SELECT id, hit_id, search_id, item_id, mode, quiet, resend, chat_id, owner FROM outbox "
            "WHERE link_id = ? AND state = 'pending' AND due_at <= ? AND next_try <= ? ORDER BY hit_id LIMIT ?",
            (link_id, now, now, TAKE_MAX)).fetchall()
    if not rows:
        return None
    hits = [r[1] for r in rows]
    with db() as con:
        live = {}
        for i in range(0, len(hits), 500):
            part = hits[i:i + 500]
            for hid, seen, watch, mode, paused, detail, name, title, link, feed, pub, fetched in con.execute(
                    "SELECT w.id, w.seen, s.watch, COALESCE(p.mode, ?), CASE WHEN p.paused_until > datetime('now') THEN 1 ELSE 0 END, "
                    "COALESCE(p.detail, 'titles'), s.name, i.title, i.link, COALESCE(f.title, f.url), "
                    "i.published_at, i.fetched_at "
                    "FROM watch_hits w JOIN saved_searches s ON s.id = w.search_id "
                    "JOIN items i ON i.id = w.item_id JOIN feeds f ON f.id = i.feed_id "
                    "LEFT JOIN tg_watch_prefs p ON p.search_id = s.id "
                    f"WHERE w.id IN ({','.join('?' * len(part))})", (DEFAULT_MODE, *part)).fetchall():
                live[hid] = {"seen": seen or not watch, "mode": mode, "paused": paused, "detail": detail, "name": name,
                             "title": title, "link": link, "feed": feed, "when": pub or fetched}
    keep, drop = [], []
    for r in rows:
        x = live.get(r[1])
        if x is None or x["seen"] or x["mode"] == "novita" or x["paused"]:
            drop.append(r[0])
        else:
            keep.append((r, x))
    with outbox() as s:
        s.execute("BEGIN IMMEDIATE")
        try:
            if drop:
                s.execute(f"UPDATE outbox SET state = 'dropped' WHERE id IN ({','.join('?' * len(drop))})", drop)
            batch = None
            if keep:
                batch = (s.execute("SELECT COALESCE(MAX(batch), 0) + 1 FROM outbox").fetchall()[0][0])
                ids = [r[0] for r, _ in keep]
                s.execute(f"UPDATE outbox SET state = 'sending', batch = ?, first_batch = COALESCE(first_batch, ?), "
                          f"attempts = attempts + 1, taken_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                          (batch, batch, now, *ids))
            s.execute("COMMIT")
        except BaseException:
            s.execute("ROLLBACK")
            raise
    if not keep:
        return None
    first = keep[0][0]
    return {"batch": batch, "link_id": link_id, "chat_id": first[7], "owner": first[8],
            "resend": any(r[6] for r, _ in keep),
            # suono deciso con la modalita' di ADESSO (potrebbe essere cambiata dopo l'accodamento)
            "audible_wanted": any(x["mode"] == "instant" and not r[5] for r, x in keep),
            "rows": [{"outbox_id": r[0], "hit_id": r[1], "search_id": r[2], "item_id": r[3], **x} for r, x in keep]}


def batch_done(batch: int, ok: bool, now: float | None = None, error: str = "") -> None:
    """Esito dell'invio. Errore temporaneo: si riprova con attese crescenti, poi 'failed'."""
    now = now or _time.time()
    with outbox() as s:
        if ok:
            s.execute("UPDATE outbox SET state = 'sent', sent_at = ? WHERE batch = ?", (now, batch))
            s.execute("DELETE FROM outbox WHERE state IN ('sent', 'dropped', 'collapsed', 'failed') AND created < ?",
                      (now - KEEP_SENT_DAYS * 86400,))
            return
        if error == "forbidden":
            s.execute("UPDATE outbox SET state = 'dropped' WHERE batch = ?", (batch,))
            return
        att = s.execute("SELECT MAX(attempts) FROM outbox WHERE batch = ?", (batch,)).fetchall()[0][0] or 1
        if error == "badrequest" and att >= BADREQUEST_MAX:
            s.execute("UPDATE outbox SET state = 'failed' WHERE batch = ?", (batch,))
            return
        # per riga, dall'ora in cui doveva partire (non da quando e' stata accodata)
        s.execute("UPDATE outbox SET state = 'failed' WHERE batch = ? AND ? - due_at > ?", (batch, now, FAIL_AFTER_S))
        base = float(os.environ.get("RSSINTEL_BOT_RETRY_BASE_S", "30"))
        wait = min(base * 2 ** max(0, att - 1), 1800)              # 30 s, 1, 2, 4... fino a 30 min
        s.execute("UPDATE outbox SET state = 'pending', resend = 1, next_try = ? WHERE batch = ? AND state = 'sending'",
                  (now + wait, batch))


def retry_later(batch: int, seconds: float) -> None:
    """Limite di Telegram (RetryAfter): di nuovo in coda dopo l'attesa, senza contarlo come tentativo."""
    with outbox() as s:
        s.execute("UPDATE outbox SET state = 'pending', attempts = MAX(attempts - 1, 0), next_try = ? "
                  "WHERE batch = ?", (_time.time() + seconds, batch))


def drop_link(link_id: int) -> None:
    with outbox() as s:
        s.execute("UPDATE outbox SET state = 'dropped' WHERE link_id = ? AND state IN ('pending', 'sending')",
                  (link_id,))


def batch_hits(batch: int, link_id: int) -> list[int]:
    """Segnalazioni di un messaggio: anche se poi sono state reinviate sotto un altro batch."""
    with outbox() as s:
        return [r[0] for r in s.execute("SELECT hit_id FROM outbox WHERE (batch = ? OR first_batch = ?) AND link_id = ?",
                                        (batch, batch, link_id)).fetchall()]


def mark_hits_seen(owner: str, hit_ids: list[int]) -> int:
    """«✓ Viste» su una notifica: come novita.php, solo segnalazioni dell'utente."""
    if not hit_ids:
        return 0
    with db() as con:
        return con.execute(
            f"UPDATE watch_hits SET seen = 1 WHERE seen = 0 AND id IN ({','.join('?' * len(hit_ids))}) "
            "AND search_id IN (SELECT id FROM saved_searches WHERE owner = ?)", (*hit_ids, owner)).rowcount


def sound_allowed(link_id: int, cfg: dict, now: float | None = None) -> bool:
    """Tetto alle notifiche sonore: al massimo N all'ora e M al giorno; poi silenziose."""
    now = now or _time.time()
    hist = [t for t in kv_get(f"sound:{link_id}", []) if now - t < 86400]
    return (sum(1 for t in hist if now - t < 3600) < cfg["sound_per_hour"]
            and len(hist) < cfg["sound_per_day"])


def sound_used(link_id: int, now: float | None = None) -> None:
    now = now or _time.time()
    hist = [t for t in kv_get(f"sound:{link_id}", []) if now - t < 86400]
    kv_set(f"sound:{link_id}", hist + [now])


def outbox_stats() -> dict:
    with outbox() as s:
        r = dict(s.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state").fetchall())
        last = s.execute("SELECT MAX(sent_at) FROM outbox").fetchall()[0][0]
    return {"pending": r.get("pending", 0) + r.get("sending", 0), "failed": r.get("failed", 0),
            "sent": r.get("sent", 0), "last_sent": last}


def set_detail(owner: str, sid: int, detail: str) -> bool:
    if detail not in ("titles", "counts"):
        return False
    with db() as con:
        with write_tx(con):
            if not _owned(con, owner, sid):
                return False
            _prefs_upsert(con, sid)
            con.execute("UPDATE tg_watch_prefs SET detail = ?, updated_at = datetime('now') WHERE search_id = ?",
                        (detail, sid))
    return True


def noisy_instant(owner_links: dict | None = None) -> list[dict]:
    """Sentinelle istantanee con piu' di 5 segnalazioni al giorno negli ultimi 2 giorni."""
    with db() as con:
        rows = con.execute(
            "SELECT s.id, s.owner, s.name FROM saved_searches s JOIN tg_watch_prefs p ON p.search_id = s.id "
            "WHERE s.watch = 1 AND p.mode = 'instant' AND "
            "(SELECT COUNT(*) FROM watch_hits w WHERE w.search_id = s.id AND w.found_at > datetime('now', '-1 day')) > 5 AND "
            "(SELECT COUNT(*) FROM watch_hits w WHERE w.search_id = s.id AND w.found_at > datetime('now', '-2 day') "
            " AND w.found_at <= datetime('now', '-1 day')) > 5").fetchall()
    return [{"id": a, "owner": b, "name": c} for a, b, c in rows]
