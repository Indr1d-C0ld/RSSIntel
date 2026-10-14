"""Collaudo L4 (notifiche) del bot contro un finto server Telegram.

Uso: <venv con PTB>/bin/python test_l4.py <copia di rssintel.db>
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (BASE, BOT, ME, calls, cbq, check, cli, env, msg, push, q, results,  # noqa: E402
                     roundtrip, statedir, work)

sys.path.insert(0, os.path.dirname(BOT))
os.environ.setdefault("RSSINTEL_DB", os.path.abspath(sys.argv[1]))
import rssintel_core as core  # noqa: E402

admin = q("SELECT username FROM users WHERE role='admin' AND COALESCE(disabled,0)=0 LIMIT 1")[0][0]
q("DELETE FROM saved_searches WHERE owner = ?", admin)
STATE_DB = os.path.join(statedir, "state.db")
ITEMS = [r[0] for r in q("SELECT id FROM items WHERE title IS NOT NULL ORDER BY id DESC LIMIT 200")]
_next = [0]


def ctl(path, obj):
    urllib.request.urlopen(urllib.request.Request(BASE + path, json.dumps(obj).encode(),
                                                  {"Content-Type": "application/json"}))


def st(sql, *a):
    c = sqlite3.connect(STATE_DB); r = c.execute(sql, a).fetchall(); c.commit(); c.close()
    return r


def hits(sid, n):
    out = []
    for _ in range(n):
        iid = ITEMS[_next[0]]; _next[0] += 1
        out.append(q("INSERT INTO watch_hits(search_id, item_id) VALUES (?, ?) RETURNING id", sid, iid)[0][0])
    return out


def pushes(since):
    return [p for m, p in calls()[since:] if m == "sendMessage" and int(p.get("chat_id", 0)) == ME
            and "🔔" in (p.get("text") or "") and "nuov" in (p.get("text") or "")]


def wait_push(since, wait=12.0, n=1):
    t = time.time()
    while time.time() - t < wait:
        if len(pushes(since)) >= n:
            time.sleep(0.5)
            return pushes(since)
        time.sleep(0.2)
    return pushes(since)


def buttons(p):
    return [b for row in ((p or {}).get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def start_bot(**extra):
    e = env(**{"RSSINTEL_BOT_PUSH_S": "1", "RSSINTEL_BOT_QUIET": "", "RSSINTEL_BOT_RETRY_BASE_S": "1",
               "RSSINTEL_BOT_NOISY_S": "2", **extra})
    lf = open(os.path.join(work, f"bot4-{time.time():.0f}.log"), "w+")
    b = subprocess.Popen([sys.executable, BOT], env=e, stdout=lf, stderr=subprocess.STDOUT)
    n0 = len(calls()); t = time.time()
    while time.time() - t < 15 and not any(m == "deleteWebhook" for m, _ in calls()[n0:]):
        time.sleep(0.2)
    time.sleep(2.5)                              # primo giro: inizializza il segnaposto
    return b, lf


def stop_bot(b):
    b.send_signal(signal.SIGTERM)
    try:
        return b.wait(15)
    except subprocess.TimeoutExpired:
        b.kill(); return "timeout"


print("== preparazione")
_, out = cli("codice", admin)
code = out.split("Codice per")[1].split(":")[1].split()[0]
bot, logf = start_bot()
r, _ = roundtrip(msg(ME, f"/start {code}"), ME)
check("collegato", "Collegato" in r)
A, _ = core.create_watch(admin, "A istantanea", "Groenlandia", "instant", None)
Bd, _ = core.create_watch(admin, "B riepilogo", "Taiwan", "digest", None)
C, _ = core.create_watch(admin, "C solo novita", "Sudan", "novita", None)
D, _ = core.create_watch(admin, "D in pausa", "Houthi", "instant", None)
core.set_pause(admin, D, 1)
E, _ = core.create_watch(admin, "E riservata", '"Mar Rosso"', "instant", None)
core.set_detail(admin, E, "counts")
time.sleep(2)
check("all'avvio nessuno storico: il segnaposto parte dall'ultima segnalazione",
      not st("SELECT COUNT(*) FROM outbox")[0][0] if os.path.exists(STATE_DB) else True)

print("== istantanea")
n0 = len(calls())
ha = hits(A, 3)
ps = wait_push(n0)
p = ps[0] if ps else {}
check("un solo messaggio per il giro, con le 3 segnalazioni", len(ps) == 1 and "3 nuove" in p.get("text", ""), [x.get("text", "")[:80] for x in ps])
check("con suono (la prima dell'ora)", p.get("disable_notification") in (False, None), p.get("disable_notification"))
check("bottoni scheda, «✓ Viste» e «Novità»", any(b.get("callback_data", "").startswith("i:") for b in buttons(p))
      and any(b.get("callback_data", "").startswith("vb:") for b in buttons(p)) and any(b.get("callback_data") == "nvl" for b in buttons(p)))
check("titoli e nome della sentinella", "A istantanea (3)" in p.get("text", ""))
n0 = len(calls()); hits(A, 2); ps = wait_push(n0)
check("seconda istantanea entro l'ora: silenziosa (tetto ai suoni)", ps and ps[0].get("disable_notification") is True)

print("== «✓ Viste» sulla notifica")
vb = [b["callback_data"] for b in buttons(p) if b.get("callback_data", "").startswith("vb:")][0]
_u = cbq(ME, vb); _u["callback_query"]["message"]["reply_markup"] = p["reply_markup"]   # come Telegram: la notifica con i suoi bottoni
n0 = len(calls()); push(_u); time.sleep(2)
check("segnate viste nel DB (come novita.php)", q(f"SELECT COUNT(*) FROM watch_hits WHERE id IN ({','.join(map(str, ha))}) AND seen = 0")[0][0] == 0)
check("il bottone diventa «✓ viste»", any(m == "editMessageReplyMarkup" and "✓ viste" in json.dumps(pp, ensure_ascii=False)
                                         for m, pp in calls()[n0:]))

print("== riepilogo, solo Novità, pausa, riservata")
n0 = len(calls())
hb = hits(Bd, 2); hc = hits(C, 2); hd = hits(D, 2)
time.sleep(4)
check("riepilogo: nessun invio subito, in coda per il prossimo orario",
      not pushes(n0) and st(f"SELECT COUNT(*) FROM outbox WHERE hit_id IN ({','.join(map(str, hb))}) AND state = 'pending' AND due_at > ?",
                            time.time())[0][0] == 2)
check("«solo Novità» e «in pausa»: mai in coda", not st(f"SELECT 1 FROM outbox WHERE hit_id IN ({','.join(map(str, hc + hd))})"))
q(f"UPDATE watch_hits SET seen = 1 WHERE id = {hb[0]}")          # vista sul web prima dell'invio
st("UPDATE outbox SET due_at = ? WHERE hit_id IN (%s)" % ",".join(map(str, hb)), time.time() - 1)   # = arriva l'orario
ps = wait_push(n0)
check("all'orario: inviato, silenzioso", ps and ps[0].get("disable_notification") is True and "1 nuova" in ps[0]["text"],
      [x.get("text", "")[:80] for x in ps])
check("la segnalazione gia' vista sul web non e' stata mandata",
      st("SELECT state FROM outbox WHERE hit_id = ?", hb[0]) == [("dropped",)])
n0 = len(calls()); hits(E, 2); ps = wait_push(n0)
t = ps[0]["text"] if ps else ""
check("riservata: solo il conteggio, niente nome ne' titoli", ps and "2 da sentinelle riservate" in t and "E riservata" not in t
      and "<b>1.</b>" not in t, t[:200])

print("== tante insieme")
n0 = len(calls()); hits(A, 30); ps = wait_push(n0)
check("oltre 25: un riepilogo unico", len(ps) == 1 and "Troppe per elencarle" in ps[0]["text"] and "30 nuove" in ps[0]["text"],
      [x.get("text", "")[:80] for x in ps])

print("== errori di Telegram")
n0 = len(calls()); ctl("/_fault", {"method": "sendMessage", "code": 429, "retry_after": 2}); hits(A, 1)
ps = wait_push(n0, wait=15)
check("429: aspetta e consegna una volta sola", len(ps) == 1 and any(m == "sendMessage!429" for m, _ in calls()[n0:]))
n0 = len(calls()); ctl("/_fault", {"method": "sendMessage", "code": 502}); hits(A, 1)
ps = wait_push(n0, wait=15)
check("errore temporaneo: riprova e consegna, marcata «reinvio»", len(ps) == 1 and "reinvio" in ps[0]["text"]
      and any(m == "sendMessage!502" for m, _ in calls()[n0:]), [x.get("text", "")[:60] for x in ps])
ctl("/_forbid", {"chat_id": ME, "on": True})
n0 = len(calls()); hf = hits(A, 2); time.sleep(5)
check("bot bloccato: niente reinvii a vuoto, collegamento segnato",
      q("SELECT blocked_at IS NOT NULL FROM tg_links WHERE tg_user_id = ? AND revoked_at IS NULL", ME) == [(1,)]
      and sum(1 for m, _ in calls()[n0:] if m == "sendMessage!403") == 1
      and st(f"SELECT COUNT(*) FROM outbox WHERE hit_id IN ({','.join(map(str, hf))}) AND state = 'dropped'")[0][0] == 2)
ctl("/_forbid", {"chat_id": ME, "on": False})
roundtrip(msg(ME, "/stato"), ME)
check("chi riscrive al bot lo ha sbloccato", q("SELECT blocked_at FROM tg_links WHERE tg_user_id = ? AND revoked_at IS NULL", ME) == [(None,)])

print("== casi della revisione")
n0 = len(calls()); ctl("/_fault", {"method": "sendMessage", "code": 400}); ctl("/_fault", {"method": "sendMessage", "code": 400})
ctl("/_fault", {"method": "sendMessage", "code": 400}); hbr = hits(A, 1); time.sleep(10)
check("errore permanente (400): al terzo tentativo si smette", st("SELECT state, attempts FROM outbox WHERE hit_id = ?", hbr[0]) == [("failed", 3)],
      st("SELECT state, attempts FROM outbox WHERE hit_id = ?", hbr[0]))
n0 = len(calls()); ctl("/_fault", {"method": "sendMessage", "code": 502}); hr = hits(A, 2)
ps = wait_push(n0, wait=15)
first_vb = None
for m, pp in calls()[n0:]:
    if m == "sendMessage!502":
        first_vb = [b["callback_data"] for b in buttons(pp) if b.get("callback_data", "").startswith("vb:")][0]
_u = cbq(ME, first_vb); _u["callback_query"]["message"]["reply_markup"] = {"inline_keyboard": []}
push(_u); time.sleep(2)
check("«✓ Viste» del primo invio vale anche dopo il reinvio",
      q(f"SELECT COUNT(*) FROM watch_hits WHERE id IN ({','.join(map(str, hr))}) AND seen = 0")[0][0] == 0)
hs = hits(Bd, 1)
time.sleep(3)
core.set_watch(admin, Bd, False)                                      # «Spegni» dopo l'accodamento
st("UPDATE outbox SET due_at = ? WHERE hit_id = ?", time.time() - 1, hs[0])
time.sleep(4)
check("sentinella spenta dopo l'accodamento: la notifica non parte", st("SELECT state FROM outbox WHERE hit_id = ?", hs[0]) == [("dropped",)])
core.set_watch(admin, Bd, True)
hv = hits(Bd, 1); time.sleep(3)
q("UPDATE tg_links SET revoked_at = datetime('now') WHERE tg_user_id = ? AND revoked_at IS NULL", ME)   # = «Scollega» dal portale
st("UPDATE outbox SET due_at = ? WHERE hit_id = ?", time.time() - 1, hv[0])
n0 = len(calls()); time.sleep(4)
check("account scollegato: le notifiche in coda non partono", not pushes(n0) and st("SELECT state FROM outbox WHERE hit_id = ?", hv[0]) == [("dropped",)])
_, out = cli("codice", admin); code2 = out.split("Codice per")[1].split(":")[1].split()[0]
roundtrip(msg(ME, f"/start {code2}"), ME)

print("== crash a meta' invio")
rc = stop_bot(bot)
hk = hits(A, 1)
link_id = q("SELECT id FROM tg_links WHERE tg_user_id = ? AND revoked_at IS NULL", ME)[0][0]
st("INSERT INTO outbox(link_id, chat_id, owner, hit_id, search_id, item_id, mode, due_at, state, created, batch) "
   "VALUES (?, ?, ?, ?, ?, ?, 'instant', ?, 'sending', ?, 999999)",
   link_id, ME, admin, hk[0], A, ITEMS[_next[0] - 1], time.time(), time.time())
st("UPDATE kv SET v = ? WHERE k = 'hit_cursor'", json.dumps(hk[0]))    # come se fosse gia' stata accodata
n0 = len(calls())
bot, logf = start_bot()
ps = wait_push(n0, wait=15)
check("riga rimasta «in invio»: ripartita, marcata reinvio, nessuna persa",
      ps and "reinvio" in ps[0]["text"] and st("SELECT state FROM outbox WHERE hit_id = ?", hk[0]) == [("sent",)],
      [x.get("text", "")[:60] for x in ps])

print("== ore di quiete")
rc = stop_bot(bot)
now = datetime.now(core.TZ)
qa, qb = now - timedelta(minutes=5), now + timedelta(minutes=10)
bot, logf = start_bot(RSSINTEL_BOT_QUIET=f"{qa:%H:%M}-{qb:%H:%M}")
n0 = len(calls()); hq = hits(A, 2); time.sleep(5)
check("in quiete: niente invii, in coda per la fine della quiete",
      not pushes(n0) and st(f"SELECT COUNT(*) FROM outbox WHERE hit_id IN ({','.join(map(str, hq))}) AND quiet = 1 AND due_at > ?",
                            time.time())[0][0] == 2)
st("UPDATE outbox SET due_at = ? WHERE hit_id IN (%s)" % ",".join(map(str, hq)), time.time() - 1)
ps = wait_push(n0)
check("a fine quiete: un riepilogo silenzioso", ps and ps[0].get("disable_notification") is True)

print("== sentinella rumorosa")
fake = q("INSERT INTO saved_searches(owner, name, q, watch) VALUES (?, 'R rumorosa', 'Iran', 1) RETURNING id", admin)[0][0]
core.set_mode(admin, fake, "instant")
for k, iid in enumerate(ITEMS[100:114]):
    q("INSERT INTO watch_hits(search_id, item_id, found_at, seen) VALUES (?, ?, datetime('now', ?), 1)",
      fake, iid, "-3 hours" if k < 7 else "-30 hours")
n0 = len(calls()); time.sleep(5)
sug = [p for m, p in calls()[n0:] if m == "sendMessage" and "R rumorosa" in (p.get("text") or "")]
check("proposta di passare al riepilogo, una volta sola", len(sug) == 1 and any(b.get("callback_data") == f"am:{fake}:d" for b in buttons(sug[0])))

print("== limiti, stato e log")
lens = [len(p.get("text") or "") for m, p in calls() if m == "sendMessage"]
check("nessun messaggio oltre 4096 caratteri", max(lens) <= 4096)
r, _ = roundtrip(msg(ME, "/stato"), ME)
check("/stato riporta la coda delle notifiche", "Notifiche:" in r and "ultima inviata" in r, r[-200:])
rc = stop_bot(bot)
check("arresto pulito", rc == 0, rc)
log = "".join(open(os.path.join(work, f)).read() for f in os.listdir(work) if f.startswith("bot4-"))
titles = [core.clean_title(t) for (t,) in q("SELECT title FROM items WHERE id IN (%s)" % ",".join(map(str, ITEMS[:20])))]
check("nel log nessun titolo ne' nome di sentinella", not any(t and t in log for t in titles)
      and all(x not in log for x in ("A istantanea", "E riservata", "R rumorosa")))
check("nel log niente errori non gestiti", "errore non gestito" not in log and "Traceback" not in log and "non riuscito" not in log,
      log[-600:])

q("DELETE FROM saved_searches WHERE owner = ?", admin)
ok, ko = results()
print(f"\nRisultato: {ok} OK, {ko} falliti")
sys.exit(1 if ko else 0)
