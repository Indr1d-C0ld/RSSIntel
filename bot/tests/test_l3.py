"""Collaudo L3 (sentinelle e Novita') del bot contro un finto server Telegram.

Uso: <venv con PTB>/bin/python test_l3.py <copia di rssintel.db>
     RSSINTEL_WATCH_PY = percorso di rssintel_watch.py (facoltativo: prova il worker)
"""
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (BOT, ME, calls, cbq, check, cli, env, msg, push, q, results,  # noqa: E402
                     roundtrip, work)

sys.path.insert(0, os.path.dirname(BOT))
os.environ.setdefault("RSSINTEL_DB", os.path.abspath(sys.argv[1]))
import rssintel_core as core  # noqa: E402

admin = q("SELECT username FROM users WHERE role='admin' AND COALESCE(disabled,0)=0 LIMIT 1")[0][0]
q("DELETE FROM saved_searches WHERE owner IN (?, 'altro-utente')", admin)


def wait_call(method, chat_id, since, wait=15.0):
    t = time.time()
    while time.time() - t < wait:
        out = [p for m, p in calls()[since:] if m == method and int(p.get("chat_id", 0)) == chat_id]
        if out:
            time.sleep(0.3)
            return out[-1]
        time.sleep(0.1)
    return None


def buttons(p):
    return [b for row in ((p or {}).get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def cbs(p, prefix):
    return [b["callback_data"] for b in buttons(p) if b.get("callback_data", "").startswith(prefix)]


def send(text):
    n0 = len(calls()); push(msg(ME, text))
    return wait_call("sendMessage", ME, n0)


def click(data, method="editMessageText"):
    n0 = len(calls()); push(cbq(ME, data))
    return wait_call(method, ME, n0)


def answers(since):
    return [p.get("text") or "" for m, p in calls()[since:] if m == "answerCallbackQuery"]


def watch_row(name):
    r = q("SELECT s.id, s.q, s.watch, s.last_checked_at, p.mode, p.expires_at, p.paused_until "
          "FROM saved_searches s LEFT JOIN tg_watch_prefs p ON p.search_id = s.id WHERE s.owner = ? AND s.name = ?",
          admin, name)
    return dict(zip(("id", "q", "watch", "last_checked_at", "mode", "expires_at", "paused_until"), r[0])) if r else None


sugg = os.path.join(work, "sentinelle.txt")
open(sugg, "w", encoding="utf-8").write(
    "# prova\nMar Rosso e Houthi | Houthi OR \"Mar Rosso\" | riepilogo\nTaiwan | Taiwan | riepilogo\n"
    'Sudan | Sudan OR Darfur | istantanea\nriga sbagliata\n')

print("== avvio e proposte")
_, out = cli("codice", admin)
code = out.split("Codice per")[1].split(":")[1].split()[0]
logf = open(os.path.join(work, "bot3.log"), "w+")
bot = subprocess.Popen([sys.executable, BOT], env=env(RSSINTEL_BOT_SUGGEST_FILE=sugg, RSSINTEL_BOT_WATCH_JOBS_S="2"),
                       stdout=logf, stderr=subprocess.STDOUT)
t = time.time()
while time.time() - t < 15 and not any(m == "deleteWebhook" for m, _ in calls()):
    time.sleep(0.2)
time.sleep(1.5)
n0 = len(calls()); push(msg(ME, f"/start {code}")); time.sleep(3)
msgs = [p for m, p in calls()[n0:] if m == "sendMessage" and int(p.get("chat_id", 0)) == ME]
check("collegato", any("Collegato" in p["text"] for p in msgs))
prop = next((p for p in msgs if cbs(p, "g:")), None)
check("dopo il collegamento propone le sentinelle del file (righe sbagliate ignorate)", prop and len(cbs(prop, "g:")) == 3)
check("nessuna sentinella creata senza un tocco", not q("SELECT 1 FROM saved_searches WHERE owner = ?", admin))

print("== creazione da proposta")
p = click("g:0", method="sendMessage")
check("anteprima con stima e modalita' della proposta",
      p and "articoli al giorno" in p["text"] and "Riepilogo</b>" in p["text"] and "scadenza: <b>mai" in p["text"],
      (p or {}).get("text", "")[:300])
tok = cbs(p, "w:")[0].split(":")[1]
p = click(f"w:{tok}:ok")
w = watch_row("Mar Rosso e Houthi")
check("«Crea»: sentinella attiva", p and "attiva" in p["text"] and w and w["watch"] == 1 and w["last_checked_at"])
check("stessa forma del portale: query, modalita', nessuna scadenza", w and w["q"] == 'Houthi OR "Mar Rosso"'
      and w["mode"] == "digest" and w["expires_at"] is None, w)
n0 = len(calls()); push(cbq(ME, f"w:{tok}:ok")); time.sleep(1.5)
check("secondo «Crea» sullo stesso messaggio: nessun doppione",
      q("SELECT COUNT(*) FROM saved_searches WHERE owner = ?", admin)[0][0] == 1)

print("== /segui")
p = send("/segui Groenlandia")
est = core.estimate("Groenlandia")
m = re.search(r"<b>([\d,]+) articoli al giorno", (p or {}).get("text", ""))
check("stima uguale alla query del worker", m and abs(float(m.group(1).replace(",", ".")) - est["per_day"]) < 0.051,
      (m and m.group(1), est["per_day"]))
check("consiglio coerente e scadenza 14 giorni preselezionata",
      p and core.MODES[core.suggest_mode(est["per_day"])] in p["text"] and "scadenza: <b>14 giorni" in p["text"])
tok = cbs(p, "w:")[0].split(":")[1]
p = click(f"w:{tok}:m:d")
check("cambio modalita'", p and "Modalità: <b>📅 Riepilogo" in p["text"])
p = click(f"w:{tok}:e:7")
check("cambio scadenza", p and "scadenza: <b>7 giorni" in p["text"])
p = click(f"w:{tok}:ok")
w = watch_row("Groenlandia")
check("creata con modalita' e scadenza scelte", w and w["mode"] == "digest" and w["expires_at"]
      and q("SELECT julianday(?) - julianday('now')", w["expires_at"])[0][0] > 6.9, w)
p = send("/segui l'iran")
check("query reinterpretata anche qui", p and "interpretata da" in p["text"])
p = click(f"w:{cbs(p, 'w:')[0].split(':')[1]}:no")
check("Annulla: niente creato", p and "Nessuna sentinella" in p["text"] and not watch_row("l'iran"))
p = send("/segui")
check("/segui senza parole: istruzioni (e proposte)", p and "seguito dalle parole" in (p.get("text") or "")
      or any("Oppure scegli" in (x.get("text") or "") for m_, x in calls()[-3:] if m_ == "sendMessage"))

print("== 🔔 Segui dai risultati")
p = send("Tel Aviv")
sw = cbs(p, "sw:")
check("bottone «Segui» sotto i risultati", bool(sw))
p = click(sw[0], method="sendMessage")
tok = cbs(p, "w:")[0].split(":")[1]
click(f"w:{tok}:ok")
check("creata dalla ricerca", watch_row("Tel Aviv") is not None)

print("== /allerte")
p = send("/allerte")
check("elenco delle sentinelle", p and "Mar Rosso e Houthi" in p["text"] and "Groenlandia" in p["text"] and "Tel Aviv" in p["text"])
sid = watch_row("Tel Aviv")["id"]
p = click(f"a:{sid}")
check("dettaglio", p and "Query:" in p["text"] and cbs(p, f"am:{sid}:"))
click(f"am:{sid}:i")
check("modalita' cambiata nel DB", watch_row("Tel Aviv")["mode"] == "instant")
click(f"ap:{sid}:1")
check("pausa 24 ore", watch_row("Tel Aviv")["paused_until"] is not None)
click(f"ap:{sid}:0")
check("ripresa", watch_row("Tel Aviv")["paused_until"] is None)
click(f"aw:{sid}:0")
check("spenta (come il portale)", watch_row("Tel Aviv")["watch"] == 0)
q("UPDATE saved_searches SET last_checked_at = '2000-01-01 00:00:00' WHERE id = ?", sid)
click(f"aw:{sid}:1")
w = watch_row("Tel Aviv")
check("riaccesa: punto di partenza «adesso», come il portale", w["watch"] == 1 and w["last_checked_at"] > "2020")
p = click(f"ad:{sid}")
check("elimina chiede conferma", p and "Eliminare" in p["text"] and watch_row("Tel Aviv"))
click(f"ay:{sid}")
check("eliminata, con le preferenze (cascata)", watch_row("Tel Aviv") is None
      and not q("SELECT 1 FROM tg_watch_prefs WHERE search_id = ?", sid))

print("== sentinelle di altri utenti")
osid = q("INSERT INTO saved_searches(owner, name, q, watch) VALUES ('altro-utente', 'ZZ-sentinella-altrui', 'Iran', 1) RETURNING id")[0][0]
for data in (f"am:{osid}:i", f"ap:{osid}:7", f"aw:{osid}:0", f"ae:{osid}:7", f"ay:{osid}"):
    push(cbq(ME, data))
time.sleep(3)
check("nessuna azione su sentinelle altrui", q("SELECT watch FROM saved_searches WHERE id = ?", osid) == [(1,)]
      and not q("SELECT 1 FROM tg_watch_prefs WHERE search_id = ?", osid))
p = click(f"a:{osid}")
check("dettaglio di una sentinella altrui: «non trovata»", p and "non trovata" in p["text"])
wsid = q("INSERT INTO saved_searches(owner, name, q, watch, last_checked_at) VALUES (?, 'dal portale', 'Gaza', 1, "
         "datetime('now')) RETURNING id", admin)[0][0]
p = send("/allerte")
check("sentinella creata dal web: Riepilogo per impostazione predefinita",
      p and re.search(r"dal portale</b> — 📅 Riepilogo", p["text"]))

print("== /novita")
mine = watch_row("Groenlandia")["id"]
its = [r[0] for r in q("SELECT id FROM items ORDER BY id DESC LIMIT 5")]
for iid in its[:3]:
    q("INSERT OR IGNORE INTO watch_hits(search_id, item_id) VALUES (?, ?)", mine, iid)
for iid in its[:2]:
    q("INSERT OR IGNORE INTO watch_hits(search_id, item_id) VALUES (?, ?)", osid, iid)
p = send("/novita")
check("segnalazioni raggruppate, solo le proprie", p and "3 segnalazioni" in p["text"] and "Groenlandia" in p["text"]
      and "ZZ-sentinella-altrui" not in p["text"], (p or {}).get("text", "")[:200])
check("bottoni scheda e «Viste»", cbs(p, "i:") and cbs(p, f"nv:{mine}") and cbs(p, "nva"))
n0 = len(calls())
p = click(f"nv:{mine}")
check("«Viste» per sentinella: segnate nel DB, come novita.php",
      q("SELECT COUNT(*) FROM watch_hits WHERE search_id = ? AND seen = 0", mine)[0][0] == 0
      and any("3 segnate" in a for a in answers(n0)))
check("le segnalazioni altrui restano", q("SELECT COUNT(*) FROM watch_hits WHERE search_id = ? AND seen = 0", osid)[0][0] == 2)
q("INSERT OR IGNORE INTO watch_hits(search_id, item_id) VALUES (?, ?)", mine, its[3])
q("INSERT OR IGNORE INTO watch_hits(search_id, item_id) VALUES (?, ?)", wsid, its[4])
click("nva")
check("«Segna tutto»: tutte le proprie, non quelle altrui",
      q("SELECT COUNT(*) FROM watch_hits w JOIN saved_searches s ON s.id = w.search_id WHERE s.owner = ? AND w.seen = 0", admin)[0][0] == 0
      and q("SELECT COUNT(*) FROM watch_hits WHERE search_id = ? AND seen = 0", osid)[0][0] == 2)
q("UPDATE watch_hits SET seen = 0 WHERE search_id = ?", mine)   # «visto» sul web e nel bot: la stessa colonna
p = send("/novita")
check("cio' che torna non visto (dal web) ricompare nel bot", p and "Groenlandia" in p["text"])

print("== scadenze")
fly = watch_row("Groenlandia")["id"]
q("UPDATE tg_watch_prefs SET expires_at = datetime('now', '+12 hours'), reminded_at = NULL WHERE search_id = ?", fly)
n0 = len(calls()); time.sleep(6)
rem = [p for m, p in calls()[n0:] if m == "sendMessage" and "scade il" in (p.get("text") or "")]
check("promemoria il giorno prima, una volta sola", len(rem) == 1, len(rem))
check("con il bottone «Prolunga»", rem and cbs(rem[0], f"ae:{fly}:7"))
click(f"ae:{fly}:7")
check("prolungata a 7 giorni da adesso", q("SELECT julianday(expires_at) - julianday('now') FROM tg_watch_prefs WHERE search_id = ?", fly)[0][0] > 6.9)
q("UPDATE tg_watch_prefs SET expires_at = datetime('now', '-1 minute') WHERE search_id = ?", fly)
n0 = len(calls()); time.sleep(6)
exp = [p for m, p in calls()[n0:] if m == "sendMessage" and "è scaduta" in (p.get("text") or "")]
w = watch_row("Groenlandia")
check("alla scadenza: spenta e avvisata, una volta sola", len(exp) == 1 and w["watch"] == 0 and w["expires_at"] is None,
      (len(exp), w))
q("UPDATE saved_searches SET watch = 1 WHERE id = ?", fly)        # = riaccesa dal portale
n0 = len(calls()); time.sleep(7)
check("riaccesa dal web: resta accesa dopo 3 controlli, nessun altro avviso",
      watch_row("Groenlandia")["watch"] == 1 and not [p for m, p in calls()[n0:] if "scadut" in str(p.get("text"))])

print("== casi della revisione")
q("INSERT INTO saved_searches(owner, name, q, feed_id, watch) VALUES (?, 'Taiwan', 'Taiwan OR Formosa', 3, 0)", admin)
p = send("/segui Taiwan")
check("nome gia' usato da una ricerca diversa: proposto un nome libero",
      p and "Taiwan (2)" in p["text"], (p or {}).get("text", "")[:120])
click(f"w:{cbs(p, 'w:')[0].split(':')[1]}:ok")
r0 = q("SELECT q, feed_id FROM saved_searches WHERE owner = ? AND name = 'Taiwan'", admin)
check("la ricerca salvata esistente resta intatta", r0 == [("Taiwan OR Formosa", 3)], r0)
check("la nuova ha il suo nome", watch_row("Taiwan (2)") is not None)
fly = watch_row("Groenlandia")["id"]
click(f"aw:{fly}:0")
q("UPDATE tg_watch_prefs SET expires_at = datetime('now', '-1 minute') WHERE search_id = ?", fly)
n0 = len(calls()); time.sleep(6)
check("scadenza di una sentinella gia' spenta: nessun avviso, scadenza azzerata",
      not [p for m, p in calls()[n0:] if "scadut" in str(p.get("text"))]
      and watch_row("Groenlandia")["expires_at"] is None)
click(f"ae:{fly}:7"); click(f"ae:{fly}:7")
w = watch_row("Groenlandia")
d = q("SELECT julianday(?) - julianday('now')", w["expires_at"])[0][0]
check("«Prolunga» su una spenta la riaccende; due tocchi non sommano", w["watch"] == 1 and 6.9 < d < 7.1, (w["watch"], d))
q("UPDATE tg_watch_prefs SET paused_until = datetime('now', '-1 hour') WHERE search_id = ?", fly)
p = send("/allerte")
check("pausa gia' finita: non mostrata", p and "in pausa" not in p["text"])
for k in range(40):
    q("INSERT INTO saved_searches(owner, name, q, watch) VALUES (?, ?, 'Iran', 1)", admin, f"riempitivo {k:02d} " + "x" * 60)
p = send("/allerte")
check("molte sentinelle con nomi lunghi: messaggio entro i limiti", p and len(p["text"]) < 4096 and "nel portale" in p["text"])
q("DELETE FROM saved_searches WHERE owner = ? AND name LIKE 'riempitivo %'", admin)

print("== il worker vede le sentinelle del bot")
wp = os.environ.get("RSSINTEL_WATCH_PY")
if wp and os.path.exists(wp):
    r = subprocess.run([sys.executable, wp], env=dict(os.environ, RSSINTEL_DB=os.environ["RSSINTEL_DB"]),
                       capture_output=True, text=True, timeout=120)
    n = q("SELECT COUNT(*) FROM saved_searches WHERE watch = 1")[0][0]
    check("rssintel_watch.py esegue anche le sentinelle create dal bot", f"[watch] {n} sentinelle" in r.stdout, r.stdout[-300:])
else:
    print("  (saltato: RSSINTEL_WATCH_PY non impostato)")

print("== arresto e log")
bot.send_signal(signal.SIGTERM)
try:
    rc = bot.wait(15)
except subprocess.TimeoutExpired:
    bot.kill(); rc = "timeout"
check("arresto pulito", rc == 0, rc)
logf.seek(0); log = logf.read()
check("nel log nessuna query ne' nome di sentinella", all(x not in log for x in ("Groenlandia", "Houthi", "Tel Aviv", "Mar Rosso")))
check("nel log niente errori non gestiti", "errore non gestito" not in log and "Traceback" not in log
      and "non riuscito" not in log, log[-800:])

q("DELETE FROM saved_searches WHERE owner IN (?, 'altro-utente')", admin)
ok, ko = results()
print(f"\nRisultato: {ok} OK, {ko} falliti")
sys.exit(1 if ko else 0)
