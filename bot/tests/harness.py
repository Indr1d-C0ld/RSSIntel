"""Strumenti comuni dei collaudi del bot (finto server Telegram, copia del DB)."""
import json
import os
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

DB = os.path.abspath(os.environ.get("RSSINTEL_TEST_DB") or sys.argv[1])
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
             RSSINTEL_BOT_HIDDEN="/nonexistent-hidden", RSSINTEL_BOT_READONLY="/proc", RSSINTEL_BOT_SEARCH_BUDGET_S="4", RSSINTEL_BOT_RATE_PER_MIN="1000",
             RSSINTEL_TXT_DIR=os.environ.get("RSSINTEL_TXT_DIR", "/var/lib/rssintel/text"))
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




def results():
    return OK, KO
