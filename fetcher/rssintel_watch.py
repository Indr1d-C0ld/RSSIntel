#!/usr/bin/env python3
"""
RSSIntel — sentinelle sulle ricerche salvate.

Per ogni ricerca salvata con l'allerta attiva esegue la stessa query FTS di
search.php sui soli articoli indicizzati dopo l'ultimo controllo, e registra le
corrispondenze nuove in `watch_hits`. La webapp le mostra in «Novità».

Viene avviato subito dopo ogni giro del fetcher (ExecStartPost nella unit
rssintel-fetch.service), cosi' le corrispondenze compaiono appena l'articolo
e' indicizzato. Solo libreria standard.

Scelte, e perche':
  - Il segno d'avanzamento e' `items.fetched_at`, non l'id dell'articolo: il
    fetcher aggiorna fetched_at quando reindicizza il testo, quindi un articolo
    il cui testo arriva a un giro successivo (download fallito la prima volta)
    viene comunque riconsiderato. Con l'id lo si perderebbe. Una finestra di
    sovrapposizione di 10 minuti assorbe le differenze d'orologio; UNIQUE su
    (ricerca, articolo) impedisce i doppioni.
  - Niente storico all'attivazione: chi accende un'allerta su «Iran» vuole le
    notizie nuove, non 7.000 vecchie. La webapp imposta last_checked_at
    all'attivazione.
  - Letture fuori transazione, scritture in transazioni brevi: lo stesso
    principio che ha corretto il fetcher (lock tenuto durante la rete).
"""
import os
import sqlite3
import sys
import time

DB_PATH = os.environ.get("RSSINTEL_DB", "/var/lib/rssintel/rssintel.db")
OVERLAP = "-10 minutes"


def main() -> int:
    t0 = time.time()
    con = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)  # transazioni esplicite
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")

    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='watch_hits'").fetchone():
        print("[watch] tabella watch_hits assente: niente da fare (migrazione non eseguita?)")
        return 0

    now = con.execute("SELECT datetime('now')").fetchone()[0]
    watches = con.execute(
        "SELECT id, owner, name, q, feed_id, last_checked_at "
        "FROM saved_searches WHERE watch = 1 ORDER BY id").fetchall()

    total = 0
    for sid, owner, name, q, feed_id, last in watches:
        since = last or now
        sql = ("SELECT i.id FROM items_fts JOIN items i ON i.id = items_fts.rowid "
               "WHERE items_fts MATCH ? AND i.fetched_at > datetime(?, ?)")
        params = [q, since, OVERLAP]
        if feed_id is not None:
            sql += " AND i.feed_id = ?"
            params.append(feed_id)

        err = None
        try:
            ids = [r[0] for r in con.execute(sql, params)]
        except sqlite3.Error as ex:
            # tipicamente una sintassi FTS non valida salvata dall'utente:
            # si registra e si passa alla ricerca successiva
            ids, err = [], str(ex)[:300]

        new = 0
        con.execute("BEGIN IMMEDIATE")
        try:
            for iid in ids:
                cur = con.execute(
                    "INSERT OR IGNORE INTO watch_hits(search_id, item_id) VALUES (?, ?)",
                    (sid, iid))
                new += cur.rowcount
            con.execute(
                "UPDATE saved_searches SET last_checked_at = ?, last_error = ? WHERE id = ?",
                (now, err, sid))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        total += new
        if new or err:
            print(f"[watch] {owner}/{name!r}: {new} nuove"
                  + (f" — ERRORE: {err}" if err else ""))

    print(f"[watch] {len(watches)} sentinelle, {total} nuove corrispondenze "
          f"({time.time() - t0:.2f}s)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
