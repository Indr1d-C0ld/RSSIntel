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
    return d.astimezone(TZ).strftime("%d/%m %H:%M")


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
