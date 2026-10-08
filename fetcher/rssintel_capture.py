#!/usr/bin/env python3
"""
RSSIntel — worker delle catture visive.

Processa la coda `captures`: per ogni richiesta pendente rende la pagina
originale con Chromium headless e salva un PNG a pagina intera.

Gira con il python3 di SISTEMA (ha requests e Pillow), non con
/opt/rssintel-venv, che non ha Pillow. Il filtro SSRF viene importato dal
fetcher: stessa logica, nessuna duplicazione da tenere allineata.

Principi ereditati dagli errori gia' pagati in questo progetto:
  - la rete NON sta dentro una transazione aperta (vedi process_feed);
  - i file si scrivono 0644 espliciti (vedi atomic_write_bytes/mkstemp);
  - ogni URL passa da is_public_url(): `url` viene da items.link, cioe' da
    contenuto di feed remoti, e qui apriamo un vero browser.
"""
import fcntl
import hashlib
import os
import sqlite3
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/rssintel")

from cdp import WSError                      # noqa: E402
from shot import capture                     # noqa: E402
from rssintel_fetch import is_public_url      # noqa: E402

DB_PATH      = os.environ.get("RSSINTEL_DB", "/var/lib/rssintel/rssintel.db")
CAP_DIR      = os.environ.get("RSSINTEL_CAP_DIR", "/var/lib/rssintel/captures")
LOCK_PATH    = os.environ.get("RSSINTEL_CAP_LOCK", "/run/lock/rssintel-capture.lock")
UA           = os.environ.get("RSSINTEL_UA", "RSSIntel/1.1 (+research)")

MAX_PER_RUN  = int(os.environ.get("RSSINTEL_CAP_MAX", "10"))
PAGE_TIMEOUT = float(os.environ.get("RSSINTEL_CAP_TIMEOUT", "60"))
MAX_BYTES    = int(os.environ.get("RSSINTEL_CAP_MAXBYTES", str(20 * 1024 * 1024)))
THUMB_WIDTH  = 320


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    con.execute("PRAGMA foreign_keys=ON;")
    con.commit()
    return con


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def make_thumb(png_path: str, thumb_path: str) -> bool:
    """Miniatura per l'elenco dei favoriti. Fallire qui non e' grave."""
    try:
        from PIL import Image
        im = Image.open(png_path).convert("RGB")
        w, h = im.size
        # solo la parte alta: e' quella che identifica la pagina
        im = im.crop((0, 0, w, min(h, int(w * 1.4))))
        im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2), Image.LANCZOS)
        im.save(thumb_path, "JPEG", quality=82, optimize=True)
        os.chmod(thumb_path, 0o644)
        return True
    except Exception:
        return False


def claim_next(con: sqlite3.Connection):
    """
    Prende in carico una richiesta pendente. Transazione breve: la cattura
    (decine di secondi di rete e rendering) avviene DOPO il commit.
    """
    con.execute("BEGIN IMMEDIATE")
    row = con.execute(
        "SELECT id, item_id, url FROM captures "
        "WHERE status='pending' ORDER BY requested_at LIMIT 1"
    ).fetchone()
    if not row:
        con.commit()
        return None
    con.execute(
        "UPDATE captures SET status='running', started_at=datetime('now') WHERE id=?",
        (row[0],),
    )
    con.commit()
    return row


def finish(con: sqlite3.Connection, cap_id: int, **fields) -> None:
    """Chiude la riga con esito. Anche questa e' una transazione breve."""
    fields.setdefault("finished_at", None)
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        f"UPDATE captures SET {cols}, finished_at=datetime('now') WHERE id=?",
        (*fields.values(), cap_id),
    )
    con.commit()


def process_one(con: sqlite3.Connection, cap_id: int, item_id: int, url: str) -> bool:
    ok, why = is_public_url(url)
    if not ok:
        finish(con, cap_id, status="error", error=f"URL bloccato: {why}")
        return False

    d = os.path.join(CAP_DIR, str(item_id))
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    png = os.path.join(d, f"{cap_id}.png")
    thumb = os.path.join(d, f"{cap_id}_thumb.jpg")

    try:
        meta = capture(url, png, user_agent=UA, timeout=PAGE_TIMEOUT)
    except (WSError, Exception) as ex:       # noqa: B014 - vogliamo tutto
        finish(con, cap_id, status="error",
               error=f"{type(ex).__name__}: {ex}"[:500])
        for p in (png, thumb):
            if os.path.exists(p):
                os.unlink(p)
        return False

    size = os.path.getsize(png)
    if size > MAX_BYTES:
        os.unlink(png)
        finish(con, cap_id, status="error",
               error=f"immagine troppo grande ({size} byte, tetto {MAX_BYTES})")
        return False

    make_thumb(png, thumb)
    rel = os.path.relpath(png, CAP_DIR)
    note = "pagina troncata all'altezza massima" if meta.get("clipped") else None

    finish(con, cap_id, status="done", png_path=rel, bytes=size,
           sha256=sha256_file(png), final_url=meta.get("final_url"),
           width=meta.get("width"), height=meta.get("height"), error=note)
    return True


def sweep_orphans(con: sqlite3.Connection) -> int:
    """
    Rimuove i file senza riga corrispondente. Eliminando un feed, la cascata
    cancella items -> captures, ma i file sul disco resterebbero.
    """
    if not os.path.isdir(CAP_DIR):
        return 0
    live = {
        (str(i), f"{c}.png")
        for c, i in con.execute(
            "SELECT id, item_id FROM captures WHERE png_path IS NOT NULL")
    }
    removed = 0
    for sub in os.listdir(CAP_DIR):
        p = os.path.join(CAP_DIR, sub)
        if not os.path.isdir(p):
            continue
        for fn in os.listdir(p):
            base = fn.replace("_thumb.jpg", ".png")
            if (sub, base) not in live:
                try:
                    os.unlink(os.path.join(p, fn))
                    removed += 1
                except OSError:
                    pass
        try:
            os.rmdir(p)          # riesce solo se vuota
        except OSError:
            pass
    return removed


def main() -> int:
    os.makedirs(CAP_DIR, exist_ok=True)

    # Una sola istanza: Chromium e' pesante, due in parallelo non servono.
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[capture] un'altra istanza e' gia' in esecuzione, esco")
        return 0

    con = db()
    done = failed = 0
    try:
        for _ in range(MAX_PER_RUN):
            row = claim_next(con)
            if not row:
                break
            cap_id, item_id, url = row
            t0 = time.time()
            try:
                if process_one(con, cap_id, item_id, url):
                    done += 1
                    print(f"[capture] #{cap_id} item={item_id} ok "
                          f"({time.time()-t0:.1f}s)")
                else:
                    failed += 1
                    print(f"[capture] #{cap_id} item={item_id} fallita")
            except Exception:
                failed += 1
                traceback.print_exc()
                try:
                    finish(con, cap_id, status="error",
                           error="errore interno del worker")
                except Exception:
                    pass
        orph = sweep_orphans(con)
        print(f"[capture] done={done} failed={failed} orfani_rimossi={orph}")
    finally:
        con.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
