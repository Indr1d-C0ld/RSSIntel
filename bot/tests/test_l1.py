"""Collaudo L1 del bot contro un finto server Telegram e una copia del DB.

Uso: <venv con PTB>/bin/python test_l1.py <copia di rssintel.db>
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(HERE, "..", "bot", "rssintel_bot.py")
sys.path.insert(0, HERE)
import fake_tg  # noqa: E402

DB = os.path.abspath(sys.argv[1])
# costruito a pezzi: nel sorgente non deve comparire nulla con la forma di un token
TOKEN = "123456:" + "TESTtest" * 5
ME, OTHER, STRANGER = 7700000111, 7700000222, 7700000999   # lunghi come quelli veri: non si confondono con i tempi nei log
OK = KO = 0


def check(desc, cond, extra=""):
    global OK, KO
    if cond:
        OK += 1; print(f"  OK    {desc}")
    else:
        KO += 1; print(f"  FALLITO  {desc} {extra}")


srv, st = fake_tg.start()
BASE = f"http://127.0.0.1:{srv.server_port}"
work = tempfile.mkdtemp(dir=HERE)
cred = os.path.join(work, "cred"); os.makedirs(cred)
statedir = os.path.join(work, "state")
open(os.path.join(cred, "bot_token"), "w").write(TOKEN)
open(os.path.join(cred, "allowed_ids"), "w").write(f"{ME}\n{OTHER}\n")


def env(**kw):
    e = dict(os.environ, RSSINTEL_DB=DB, CREDENTIALS_DIRECTORY=cred, STATE_DIRECTORY=statedir,
             RSSINTEL_BOT_API_BASE=BASE, PYTHONUNBUFFERED="1",
             RSSINTEL_BOT_HIDDEN="/nonexistent-hidden", RSSINTEL_BOT_READONLY="/proc")
    e.update(kw)
    return e


def cli(*args):
    r = subprocess.run([sys.executable, BOT, *args], env=env(), capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def calls():
    return json.load(urllib.request.urlopen(BASE + "/_calls"))


def sent_to(chat_id, since):
    return [p for m, p in calls()[since:] if m in ("sendMessage", "editMessageText")
            and int(p.get("chat_id", 0)) == chat_id]


def push(upd):
    req = urllib.request.Request(BASE + "/_push", json.dumps(upd).encode(),
                                 {"Content-Type": "application/json"})
    urllib.request.urlopen(req)


def user(uid):
    return {"id": uid, "is_bot": False, "first_name": f"U{uid}"}


def msg(uid, text, chat=None, ctype="private"):
    m = {"message_id": int(time.time() * 1000) % 10**9, "date": int(time.time()),
         "chat": {"id": chat or uid, "type": ctype, "first_name": "x"} if ctype == "private"
         else {"id": chat, "type": ctype, "title": "gruppo"},
         "from": user(uid), "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"message": m}


def cbq(uid, data):
    return {"callback_query": {"id": str(time.time()), "from": user(uid), "chat_instance": "ci",
                               "data": data, "message": {"message_id": 5, "date": int(time.time()),
                                                         "chat": {"id": uid, "type": "private"},
                                                         "text": "x"}}}


def roundtrip(upd, chat_id, wait=15.0):
    n0 = len(calls())
    push(upd)
    t = time.time()
    while time.time() - t < wait:
        out = sent_to(chat_id, n0)
        if out:
            time.sleep(0.2)
            return " ".join(p.get("text", "") for p in sent_to(chat_id, n0)), n0
        time.sleep(0.1)
    return "", n0


def q(sql, *a):
    c = sqlite3.connect(DB); c.execute("PRAGMA foreign_keys=ON")
    r = c.execute(sql, a).fetchall(); c.commit(); c.close()
    return r


admin = q("SELECT username FROM users WHERE role='admin' AND COALESCE(disabled,0)=0 LIMIT 1")[0][0]

print("== riga di comando")
rc, out = cli("codice", "utente-che-non-esiste")
check("codice per un utente inesistente rifiutato", rc == 1 and "inesistente" in out)
rc, out = cli("codice", admin)
code = out.split("Codice per")[1].split(":")[1].split()[0] if rc == 0 else ""
check("codice generato", rc == 0 and len(code) == 10, out)
check("nel DB c'e' solo l'impronta del codice",
      not q("SELECT 1 FROM tg_link_codes WHERE code_hash = ?", code) and
      q("SELECT COUNT(*) FROM tg_link_codes WHERE used_at IS NULL")[0][0] >= 1)

print("== avvio")
logf = open(os.path.join(work, "bot.log"), "w+")
bot = subprocess.Popen([sys.executable, BOT], env=env(), stdout=logf, stderr=subprocess.STDOUT)
t = time.time()
while time.time() - t < 15 and not any(m == "deleteWebhook" for m, _ in calls()):
    time.sleep(0.2)
time.sleep(1.5)
names = [m for m, _ in calls()]
check("si collega (getMe, deleteWebhook con drop_pending_updates)",
      "getMe" in names and any(m == "deleteWebhook" and p.get("drop_pending_updates") for m, p in calls()))
check("menu predefinito con il solo /start",
      any(m == "setMyCommands" and [c["command"] for c in p["commands"]] == ["start"] for m, p in calls()))

print("== lista bianca e collegamento")
n0 = len(calls()); push(msg(STRANGER, "/start")); push(msg(STRANGER, "ciao")); time.sleep(2)
check("estraneo: nessuna risposta", not sent_to(STRANGER, n0))
r, _ = roundtrip(msg(ME, "/stato"), ME)
check("non collegato: /stato rifiutato", "non è collegato" in r, r)
r, _ = roundtrip(msg(ME, "/start SBAGLIATO1"), ME)
check("codice sbagliato rifiutato", "non valido" in r, r)
r, n0 = roundtrip(msg(ME, f"/start {code}"), ME)
check("codice giusto: collegato", "Collegato" in r and admin in r, r)
check("menu completo impostato per la chat",
      any(m == "setMyCommands" and p.get("scope", {}).get("type") == "chat" for m, p in calls()[n0:]))
check("collegamento nel DB", q("SELECT COUNT(*) FROM tg_links WHERE tg_user_id=? AND revoked_at IS NULL", ME)[0][0] == 1)
r, _ = roundtrip(msg(OTHER, f"/start {code}"), OTHER)
check("lo stesso codice non vale una seconda volta", "non valido" in r, r)

print("== comandi")
r, _ = roundtrip(msg(ME, "/stato"), ME)
check("/stato risponde con raccolta e permessi (admin)", "Stato di RSSIntel" in r and "Permessi del database" in r, r)
check("/stato riporta la sandbox", "Sandbox: ok" in r, r)
r, _ = roundtrip(msg(ME, "/aiuto"), ME)
check("/aiuto", "Cercare" in r and "/stato" in r, r)
r, _ = roundtrip(msg(ME, "iran"), ME)
check("testo libero: e' una ricerca (dettagli in test_l2)", "risultati" in r, r)

print("== bottoni")
n0 = len(calls()); push(cbq(STRANGER, "esci:si")); time.sleep(1.5)
check("bottone falsificato da un estraneo ignorato",
      q("SELECT COUNT(*) FROM tg_links WHERE tg_user_id=? AND revoked_at IS NULL", ME)[0][0] == 1
      and not sent_to(STRANGER, n0))
n0 = len(calls()); push(cbq(ME, "esci:qualsiasi")); time.sleep(1.5)
check("dati di bottone sconosciuti ignorati",
      q("SELECT COUNT(*) FROM tg_links WHERE tg_user_id=? AND revoked_at IS NULL", ME)[0][0] == 1)
r, _ = roundtrip(msg(ME, "/esci"), ME)
check("/esci chiede conferma", "Scollegare" in r, r)
r, _ = roundtrip(cbq(ME, "esci:si"), ME)
check("conferma: scollegato", "scollegato" in r and
      q("SELECT COUNT(*) FROM tg_links WHERE tg_user_id=? AND revoked_at IS NULL", ME)[0][0] == 0, r)

print("== revoca dal portale e disattivazione")
_, out = cli("codice", admin); code2 = out.split("Codice per")[1].split(":")[1].split()[0]
roundtrip(msg(ME, f"/start {code2}"), ME)
q("UPDATE tg_links SET revoked_at = datetime('now') WHERE tg_user_id=? AND revoked_at IS NULL", ME)  # = Scollega in profile.php
r, _ = roundtrip(msg(ME, "/stato"), ME)
check("dopo «Scollega» dal portale il comando successivo e' rifiutato", "non è collegato" in r, r)
_, out = cli("codice", admin); code3 = out.split("Codice per")[1].split(":")[1].split()[0]
roundtrip(msg(ME, f"/start {code3}"), ME)
q("UPDATE users SET disabled=1 WHERE username=?", admin)
r, _ = roundtrip(msg(ME, "/stato"), ME)
check("utente disattivato: rifiutato", "disattivato" in r, r)
q("UPDATE users SET disabled=0 WHERE username=?", admin)
r, _ = roundtrip(msg(ME, "/stato"), ME)
check("riattivato: funziona senza ricollegarsi", "Stato di RSSIntel" in r, r)
_, out = cli("codice", admin); code4 = out.split("Codice per")[1].split(":")[1].split()[0]
q("UPDATE tg_link_codes SET expires_at = datetime('now','-1 minute') WHERE used_at IS NULL")
r, _ = roundtrip(msg(OTHER, f"/start {code4}"), OTHER)
check("codice scaduto rifiutato", "non valido" in r, r)
rc, out = cli("collegamenti")
check("CLI collegamenti", rc == 0 and admin in out, out)

print("== gruppi")
n0 = len(calls())
push({"my_chat_member": {"chat": {"id": -1001, "type": "group", "title": "g"}, "from": user(ME),
                         "date": int(time.time()),
                         "old_chat_member": {"user": fake_tg.BOT_USER, "status": "left"},
                         "new_chat_member": {"user": fake_tg.BOT_USER, "status": "member"}}})
time.sleep(1.5)
check("aggiunto a un gruppo: esce", any(m == "leaveChat" and int(p["chat_id"]) == -1001 for m, p in calls()[n0:]))
n0 = len(calls()); push(msg(ME, "/stato", chat=-1002, ctype="group")); time.sleep(1.5)
check("messaggio da un gruppo: nessuna risposta, esce",
      not sent_to(-1002, n0) and any(m == "leaveChat" for m, _ in calls()[n0:]))

print("== arresto")
bot.send_signal(signal.SIGTERM)
try:
    rc = bot.wait(15)
except subprocess.TimeoutExpired:
    bot.kill(); rc = "timeout"
check("SIGTERM: esce pulito", rc == 0, rc)
logf.seek(0); log = logf.read()
check("log senza token", TOKEN not in log and "TESTtest" not in log)
_bad = [c for c in (code, code2, code3, code4, str(ME), str(STRANGER)) if c in log]
check("log senza codici ne' ID", not _bad, [l for l in log.splitlines() if any(b in l for b in _bad)][:5])
check("log con nomi dei comandi e tempi", "stato " in log and " ms" in log)

print("== token revocato a bot avviato")
p = subprocess.Popen([sys.executable, BOT], env=env(RSSINTEL_BOT_WATCHDOG_S="2"),
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(4)
urllib.request.urlopen(urllib.request.Request(BASE + "/_revoke", b"{}", {"Content-Type": "application/json"}))
try:
    rc = p.wait(30); out = p.stdout.read()
except subprocess.TimeoutExpired:
    p.kill(); rc = "vivo"; out = p.stdout.read()
check("si ferma con codice 78 (non resta acceso e sordo)", rc == 78, (rc, out[-300:]))
check("nessun token nell'output", TOKEN not in out and "TESTtest" not in out)
st.revoked = False

print("== token rifiutato")
open(os.path.join(cred, "bot_token"), "w").write(fake_tg.BAD_TOKEN)
r = subprocess.run([sys.executable, BOT], env=env(), capture_output=True, text=True, timeout=60)
out = r.stdout + r.stderr
check("esce con codice 78", r.returncode == 78, (r.returncode, out[-400:]))
check("il token non compare nell'output", fake_tg.BAD_TOKEN not in out and "BADbad" not in out, out[-400:])
check("lo dice chiaramente", "rifiutato il token" in out, out[-300:])

print("== rete assente all'avvio")
open(os.path.join(cred, "bot_token"), "w").write(TOKEN)
p = subprocess.Popen([sys.executable, BOT], env=env(RSSINTEL_BOT_API_BASE="http://127.0.0.1:9"),
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(12)
alive = p.poll() is None
p.send_signal(signal.SIGTERM)
try:
    out, _ = p.communicate(timeout=15)
except subprocess.TimeoutExpired:
    p.kill(); out, _ = p.communicate()
check("non esce: continua a ritentare", alive, out[-400:])
check("nessun token nei messaggi d'errore di rete", TOKEN not in out and "TESTtest" not in out, out[-400:])

print("== credenziali assenti")
r = subprocess.run([sys.executable, BOT], env=env(CREDENTIALS_DIRECTORY=os.path.join(work, "vuota")),
                   capture_output=True, text=True, timeout=30)
check("credenziale mancante: codice 78", r.returncode == 78, r.stderr[-200:])

print("== DB sbagliato")
r = subprocess.run([sys.executable, BOT, "collegamenti"], env=env(RSSINTEL_DB=os.path.join(work, "non-esiste.db")),
                   capture_output=True, text=True)
check("CLI: errore chiaro, nessun DB vuoto creato", r.returncode == 1 and "RSSINTEL_DB" in r.stderr
      and not os.path.exists(os.path.join(work, "non-esiste.db")), r.stderr[-200:])
r = subprocess.run([sys.executable, BOT], env=env(RSSINTEL_DB=os.path.join(work, "vuoto.db")),
                   capture_output=True, text=True, timeout=30)
check("bot con DB inesistente: esce (non crea un DB vuoto)", r.returncode != 0
      and not os.path.exists(os.path.join(work, "vuoto.db")), (r.returncode, r.stderr[-200:]))

print(f"\nRisultato: {OK} OK, {KO} falliti")
sys.exit(1 if KO else 0)
