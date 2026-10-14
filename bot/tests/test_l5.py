"""Collaudo L5 (bollettino e salute delle fonti) del bot contro un finto server Telegram.

Uso: <venv con PTB>/bin/python test_l5.py <copia di rssintel.db>
"""
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (BOT, ME, calls, cbq, check, cli, env, msg, push, q, results,  # noqa: E402
                     roundtrip, statedir, work)

sys.path.insert(0, os.path.dirname(BOT))
os.environ.setdefault("RSSINTEL_DB", os.path.abspath(sys.argv[1]))
import rssintel_core as core  # noqa: E402

admin = q("SELECT username FROM users WHERE role='admin' AND COALESCE(disabled,0)=0 LIMIT 1")[0][0]
STATE_DB = os.path.join(statedir, "state.db")
TODAY = datetime.now(core.TZ)
YDAY = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
# un giorno pieno con tutte le sezioni: il piu' recente con almeno 3 sezioni non vuote
DAY = next(d for d in ((TODAY - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(1, 30))
           if len(core.bulletin(d, admin)["sections"]) >= 3)


def wait_call(method, since, wait=15.0, pred=lambda p: True):
    t = time.time()
    while time.time() - t < wait:
        out = [p for m, p in calls()[since:] if m == method and int(p.get("chat_id", 0)) == ME and pred(p)]
        if out:
            time.sleep(0.3)
            return out[-1]
        time.sleep(0.1)
    return None


def buttons(p):
    return [b for row in ((p or {}).get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def send(text):
    n0 = len(calls()); push(msg(ME, text))
    return wait_call("sendMessage", n0)


def click(data, method="editMessageText"):
    n0 = len(calls()); push(cbq(ME, data))
    return wait_call(method, n0)


def st(sql, *a):
    c = sqlite3.connect(STATE_DB); r = c.execute(sql, a).fetchall(); c.commit(); c.close()
    return r


print("== avvio")
_, out = cli("codice", admin)
code = out.split("Codice per")[1].split(":")[1].split()[0]
logf = open(os.path.join(work, "bot5.log"), "w+")
bot = subprocess.Popen([sys.executable, BOT], env=env(RSSINTEL_BOT_BULLETIN_AT="00:00", RSSINTEL_BOT_BULLETIN_UNTIL="24",
                                                      RSSINTEL_BOT_MORNING_S="2", RSSINTEL_BOT_HEALTH_S="2",
                                                      RSSINTEL_BOT_PUSH_S="3600"),
                       stdout=logf, stderr=subprocess.STDOUT)
t = time.time()
while time.time() - t < 15 and not any(m == "deleteWebhook" for m, _ in calls()):
    time.sleep(0.2)
time.sleep(1.5)
r, _ = roundtrip(msg(ME, f"/start {code}"), ME)
check("collegato", "Collegato" in r)
time.sleep(5)                                   # primo controllo delle fonti: registra lo stato iniziale

print("== /oggi")
p = send("/oggi")
ref = core.bulletin(TODAY.strftime("%Y-%m-%d"), admin)
check("bollettino di oggi, «in corso», con i totali del core",
      p and "oggi, in corso" in p["text"] and f"{ref['n_items']:,} articoli".replace(",", ".") in p["text"], (p or {}).get("text", "")[:200])
dd = datetime.strptime(DAY, "%Y-%m-%d")
p = send(f"/oggi {dd.day}/{dd.month}")
ref = core.bulletin(DAY, admin)
check("/oggi gg/mm: il giorno giusto", p and f"{dd:%d/%m/%Y}" in p["text"])
secs = [b for b in buttons(p) if re.match(r"^b:\d{8}:\d:1$", b.get("callback_data", ""))]
check("un bottone per sezione non vuota, nell'ordine del portale, con i conteggi",
      [b["text"] for b in secs] == [f"{s_['label'].capitalize()} {len(s_['rows'])}" for s_ in ref["sections"]],
      ([b["text"] for b in secs], [(s_["label"], len(s_["rows"])) for s_ in ref["sections"]]))
check("navigazione fra i giorni", any(b.get("text", "").startswith("◀") for b in buttons(p))
      and any(b.get("text") == "Oggi" for b in buttons(p)))
p = send("/oggi ieri")
check("/oggi ieri", p and f"{datetime.strptime(YDAY, '%Y-%m-%d'):%d/%m/%Y}" in p["text"])
p = send("/oggi 31/02")
check("data impossibile: rifiutata", p and "non valida" in p["text"])
for bad in ("/oggi 13/10/123", "/oggi 1/1/999", "/oggi 29/02/2026"):
    p = send(bad)
    check(f"«{bad}»: rifiutata con un messaggio", p and "non valida" in p["text"], (p or {}).get("text", "")[:80])
tw = TODAY + timedelta(days=1)
p = send(f"/oggi {tw.day}/{tw.month}")
check("domani senza anno: «non è ancora arrivato» (non l'anno scorso)", p and "non è ancora arrivato" in p["text"],
      (p or {}).get("text", "")[:80])
tm = TODAY + timedelta(days=1)
p = send(f"/oggi {tm.day}/{tm.month}/{tm.year}")
check("giorno futuro: rifiutato", p and "non è ancora arrivato" in p["text"])

print("== sezioni")
gen = next((s_ for s_ in ref["sections"] if s_["key"] == "generalista"), None)
ana = next((s_ for s_ in ref["sections"] if s_["key"] == "analisi"), None)
ymd = DAY.replace("-", "")
if ana:
    p = click(f"b:{ymd}:{core.BULLETIN_ORDER.index('analisi')}:1")
    check("sezione: titolo, totale e 10 righe con 📄", p and f"{len(ana['rows'])} articoli" in p["text"]
          and len([b for b in buttons(p) if b.get("callback_data", "").startswith("i:")]) == min(10, len(ana["rows"])))
    check("primo articolo come il core", p and buttons(p)[0]["callback_data"] == f"i:{ana['rows'][0]['id']}")
if gen:
    gi = core.BULLETIN_ORDER.index("generalista")
    p = click(f"b:{ymd}:{gi}:2")
    shown = min(12, len(gen["rows"]))
    check("generalista: al massimo 12, come il web, poi «… e altri N nel portale»",
          p and len([b for b in buttons(p) if b.get("callback_data", "").startswith("i:")]) == shown - 10
          and (len(gen["rows"]) <= 12 or f"e altri {len(gen['rows']) - 12}" in p["text"]), (p or {}).get("text", "")[-200:])
    p = click(f"b:{ymd}:{gi}:99")
    check("pagina oltre la fine: l'ultima", p and "pag. 2/2" in p["text"])
p = click(f"b:{ymd}")
check("« Bollettino: torna al riepilogo", p and "Bollettino di" in p["text"])
n0 = len(calls()); push(cbq(ME, "b:29991231")); push(cbq(ME, f"b:{ymd}:9:1")); push(cbq(ME, "b:20261399")); time.sleep(3)
check("giorni futuri, date impossibili, sezioni fuori scala: ignorati",
      not [1 for m, p in calls()[n0:] if m == "editMessageText"])

print("== invio del mattino")
p = click(f"bp:1:{ymd}")
check("attivato dal bottone", p and "attivo ✓" in json.dumps(p.get("reply_markup"), ensure_ascii=False))
link_id = q("SELECT id FROM tg_links WHERE tg_user_id = ? AND revoked_at IS NULL", ME)[0][0]
check("attivato dopo l'ora: il primo arriva domani, non subito",
      json.loads(st("SELECT v FROM kv WHERE k = ?", f"bulletin_sent:{link_id}")[0][0]) == TODAY.strftime("%Y-%m-%d"))
st("UPDATE kv SET v = ? WHERE k = ?", json.dumps(YDAY), f"bulletin_sent:{link_id}")   # = un nuovo giorno
n0 = len(calls()); time.sleep(6)
mp = [p for m, p in calls()[n0:] if m == "sendMessage" and int(p.get("chat_id", 0)) == ME and "Bollettino di" in (p.get("text") or "")]
check("il bollettino di ieri arriva, silenzioso, una volta sola",
      len(mp) == 1 and mp[0].get("disable_notification") is True
      and f"{datetime.strptime(YDAY, '%Y-%m-%d'):%d/%m/%Y}" in mp[0]["text"], [x.get("text", "")[:60] for x in mp])
p = click(f"bp:0:{ymd}")
check("spento dal bottone", p and "Ricevilo ogni mattina" in json.dumps(p.get("reply_markup"), ensure_ascii=False))
st("UPDATE kv SET v = ? WHERE k = ?", json.dumps(YDAY), f"bulletin_sent:{link_id}")
n0 = len(calls()); time.sleep(5)
check("spento: niente bollettino del mattino", not [1 for m, p in calls()[n0:] if "Bollettino di" in str(p.get("text"))])

print("== salute delle fonti")
coll = [p for m, p in calls() if m == "sendMessage" and "Salute delle fonti" in (p.get("text") or "")]
check("copia con tutte le fonti ferme: un solo avviso «raccolta ferma», non venti righe",
      len(coll) == 1 and "raccolta potrebbe essere ferma" in coll[0]["text"] and coll[0]["text"].count("⚠️") == 1,
      [x.get("text", "")[:120] for x in coll])
fid, fname = q("SELECT id, COALESCE(title, url) FROM feeds WHERE enabled = 1 AND COALESCE(last_error, '') = '' ORDER BY id LIMIT 1")[0]
n0 = len(calls())
q("UPDATE feeds SET last_error = 'HTTP 503 di prova' WHERE id = ?", fid)
time.sleep(6)
al = [p for m, p in calls()[n0:] if m == "sendMessage" and "Salute delle fonti" in (p.get("text") or "")]
check("una fonte va in errore: un avviso, con suono, agli amministratori",
      len(al) == 1 and "HTTP 503 di prova" in al[0]["text"] and al[0].get("disable_notification") is False, [x.get("text", "")[:120] for x in al])
q("UPDATE feeds SET last_error = NULL WHERE id = ?", fid)
n0 = len(calls()); time.sleep(6)
al = [p for m, p in calls()[n0:] if m == "sendMessage" and "Salute delle fonti" in (p.get("text") or "")]
check("problema rientrato: un avviso silenzioso, una volta",
      len(al) == 1 and ("di nuovo regolare" in al[0]["text"] or "errore risolto" in al[0]["text"])
      and al[0].get("disable_notification") is True, [x.get("text", "")[:120] for x in al])

print("== arresto e log")
bot.send_signal(signal.SIGTERM)
try:
    rc = bot.wait(15)
except subprocess.TimeoutExpired:
    bot.kill(); rc = "timeout"
check("arresto pulito", rc == 0, rc)
logf.seek(0); log = logf.read()
titles = [core.clean_title(x["title"]) for s_ in ref["sections"] for x in s_["rows"][:5]]
check("nel log nessun titolo", not any(t and len(t) > 12 and t in log for t in titles))
check("nel log niente errori non gestiti", "errore non gestito" not in log and "Traceback" not in log
      and "non riuscito" not in log, log[-600:])
lens = [len(p.get("text") or "") for m, p in calls() if m in ("sendMessage", "editMessageText")]
check("nessun messaggio oltre 4096 caratteri", max(lens) <= 4096)

ok, ko = results()
print(f"\nRisultato: {ok} OK, {ko} falliti")
sys.exit(1 if ko else 0)
