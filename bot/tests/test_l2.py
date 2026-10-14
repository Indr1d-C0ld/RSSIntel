"""Collaudo L2 (ricerca e lettura) del bot contro un finto server Telegram.

Uso: <venv con PTB>/bin/python test_l2.py <copia di rssintel.db>
     (RSSINTEL_TXT_DIR = cartella dei testi a cui puntano i text_path della copia)
"""
import gzip
import os
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (BOT, ME, OTHER, STRANGER, calls, cbq, check, cli, env, msg, push, q,  # noqa: E402
                     results, roundtrip, sent_to, work)

sys.path.insert(0, os.path.join(os.path.dirname(BOT)))
os.environ.setdefault("RSSINTEL_DB", os.path.abspath(sys.argv[1]))
import rssintel_core as core  # noqa: E402

admin = q("SELECT username FROM users WHERE role='admin' AND COALESCE(disabled,0)=0 LIMIT 1")[0][0]


def last_call(method, chat_id, since):
    out = [p for m, p in calls()[since:] if m == method and int(p.get("chat_id", 0)) == chat_id]
    return out[-1] if out else None


def wait_call(method, chat_id, since, wait=15.0):
    t = time.time()
    while time.time() - t < wait:
        c = last_call(method, chat_id, since)
        if c:
            return c
        time.sleep(0.1)
    return None


def answers(since):
    return [p for m, p in calls()[since:] if m == "answerCallbackQuery"]


def buttons(p):
    kb = (p or {}).get("reply_markup") or {}
    return [b for row in kb.get("inline_keyboard", []) for b in row]


def tok_of(p):
    for b in buttons(p):
        m = re.match(r"^(?:s|sc):([a-z2-9]{8})", b.get("callback_data", ""))
        if m:
            return m.group(1)
    return None


def visible_len(t):
    return len(re.sub(r"<[^>]+>", "", t or ""))


def search_msg(text, uid=ME):
    n0 = len(calls())
    push(msg(uid, text))
    return wait_call("sendMessage", uid, n0), n0


def click(data, uid=ME, method="editMessageText"):
    n0 = len(calls())
    push(cbq(uid, data))
    return wait_call(method, uid, n0), n0


# ---------------------------------------------------------------- fixture
fx_feed = q("INSERT INTO feeds(url, title, enabled) VALUES ('https://fixture.invalid/feed', "
            "'Fixture <i>feed</i>', 0) RETURNING id")[0][0]
fx_item = q("INSERT INTO items(feed_id, guid, title, link, fetched_at) VALUES (?, 'fx-1', "
            "'<b>&amp;<img src=x onerror=alert(1)>', 'https://example.org/a?x=1&y=2', datetime('now')) RETURNING id",
            fx_feed)[0][0]

# articoli reali con proprieta' utili
long_link = q("SELECT id FROM items WHERE length(link) > 256 AND text_path IS NOT NULL ORDER BY id DESC LIMIT 1")
amp_link = q("SELECT id, link FROM items WHERE link LIKE '%&%' AND text_path IS NOT NULL ORDER BY id DESC LIMIT 1")
mid_text = big_text = None
for (iid, tp) in q("SELECT id, text_path FROM items WHERE text_path LIKE '%.gz' ORDER BY id DESC LIMIT 4000"):
    try:
        n = len(gzip.open(tp).read().decode("utf-8", "replace"))
    except OSError:
        continue
    if mid_text is None and 8000 < n < 11000:
        mid_text = iid
    if big_text is None and n > 20000:
        big_text = iid
    if mid_text and big_text:
        break

print("== preparazione")
_, out = cli("codice", admin)
code = out.split("Codice per")[1].split(":")[1].split()[0]
logf = open(os.path.join(work, "bot2.log"), "w+")
bot = subprocess.Popen([sys.executable, BOT], env=env(), stdout=logf, stderr=subprocess.STDOUT)
t = time.time()
while time.time() - t < 15 and not any(m == "deleteWebhook" for m, _ in calls()):
    time.sleep(0.2)
time.sleep(1.5)
r, _ = roundtrip(msg(ME, f"/start {code}"), ME)
check("collegato", "Collegato" in r, r)
_, out = cli("codice", admin)
code_b = out.split("Codice per")[1].split(":")[1].split()[0]
r, _ = roundtrip(msg(OTHER, f"/start {code_b}"), OTHER)
check("secondo account collegato (per le prove di gettoni altrui)", "Collegato" in r, r)

print("== ricerca")
p, _ = search_msg("Groenlandia")
ref = core.search("Groenlandia")
check("testo libero = ricerca, totale come il core", p and f"{ref['total']} risultati" in p["text"], (p or {}).get("text", "")[:200])
check("6 risultati per pagina con i bottoni scheda", p and sum(1 for b in buttons(p) if b.get("callback_data", "").startswith("i:")) == 6)
check("primi 6 id come il core", p and [b["callback_data"][2:] for b in buttons(p) if b.get("callback_data", "").startswith("i:")]
      == [str(x["id"]) for x in ref["rows"]])
check("estratti con l'evidenziazione in <b>", p and "<b>groenlandia</b>" in p["text"].lower())
check("nessuna anteprima dei link", p and (p.get("link_preview_options") or {}).get("is_disabled") is True)
tok = tok_of(p)
pe, _ = click(f"s:{tok}:2")
check("▶ pagina 2 modificando lo stesso messaggio", pe and "pag. 2/" in pe["text"], (pe or {}).get("text", "")[:120])
pe, _ = click(f"s:{tok}:999")
check("oltre la fine: ultima pagina (come il web)", pe and f"pag. {ref['pages']}/{ref['pages']}" in pe["text"])
p2, n0 = click("s:zzzzzzzz:2")
check("gettone inesistente: «scaduta», nessuna modifica",
      p2 is None and any("scaduta" in (a.get("text") or "") for a in answers(n0)))
p3, n0 = click(f"s:{tok}:2", uid=OTHER)
check("gettone di un altro account: rifiutato",
      p3 is None and any("scaduta" in (a.get("text") or "") for a in answers(n0)))

print("== query ostili")
p, _ = search_msg("l'iran")
check("«l'iran»: risultati e nota «interpretata come»", p and "interpretata come" in p["text"] and "risultat" in p["text"])
tk = tok_of(p)
pe, _ = click(f"s:{tk}:2")
check("la nota resta nelle pagine successive", pe and "interpretata come" in pe["text"])
for qq in ("Al-Aqsa", "iran-israel", "Biden OR", "Iran?"):
    p, _ = search_msg(qq)
    check(f"«{qq}»: nessun errore", p and "risultat" in p["text"] and "Non riesco" not in p["text"], (p or {}).get("text", "")[:150])
p, _ = search_msg("a*")
check("«a*» (37.000 risultati): risponde entro il limite", p and ("risultat" in p["text"] or "troppo larga" in p["text"]),
      (p or {}).get("text", "")[:150])
p, _ = search_msg("Iran\nGaza")
check("ricerca su piu' righe: le righe diventano spazi", p and "<code>Iran Gaza</code>" in p["text"], (p or {}).get("text", "")[:80])
p, _ = search_msg("NEAR(iran israele, dieci)")
check("errore FTS non elencato («expected integer»): reinterpretata, non silenzio",
      p and "risultat" in p["text"], (p or {}).get("text", "")[:150])
p, _ = search_msg("xqzvwkkjhh")
check("nessun risultato: lo dice con un suggerimento", p and "Nessun articolo" in p["text"])
p, _ = search_msg("/cerca")
check("/cerca senza parole: istruzioni", p and "seguito dalle parole" in p["text"])
p, _ = search_msg("/cerca Tel Aviv")
check("/cerca con parole", p and "Tel Aviv" in p["text"] and "risultat" in p["text"])
p, _ = search_msg("/pippo")
check("comando sconosciuto", p and "sconosciuto" in p["text"])
p, _ = search_msg("Iran​\u0000 ‮")
check("caratteri invisibili/di controllo tolti", p and "<code>Iran</code>" in p["text"], (p or {}).get("text", "")[:80])

print("== categoria")
p, _ = search_msg("Iran")
tk = tok_of(p)
pm, _ = click(f"sc:{tk}", method="editMessageReplyMarkup")
check("🗂 mostra le categorie", pm and len(buttons(pm)) == len(core.CATEGORY_KEYS))
idx = core.CATEGORY_KEYS.index("analisi")
pe, _ = click(f"sc:{tk}:{idx}")
ref = core.search("Iran", "analisi")
check("filtro «analisi»: totale come il core", pe and f"{ref['total']} risultati" in pe["text"] and "solo analisi" in pe["text"],
      (pe or {}).get("text", "")[:120])
pe, _ = click(f"s:{tk}:2")
check("la categoria resta nelle pagine successive", pe and "solo analisi" in pe["text"])
pe, n0 = click(f"sc:{tk}:9")
check("indice di categoria fuori scala ignorato", pe is None)

print("== scheda")
first = core.search("Groenlandia")["rows"][0]["id"]
p, _ = click(f"i:{first}", method="sendMessage")
bt = buttons(p)
check("scheda: estratto espandibile", p and "<blockquote expandable>" in p["text"])
check("scheda: Apri fonte, Link e Testo",
      any(b.get("url") for b in bt) and any(b.get("copy_text") for b in bt)
      and any(b.get("callback_data", "").startswith(f"t:{first}:") for b in bt))
check("scheda: dominio visibile", p and "🌐" in p["text"])
if amp_link:
    p, _ = click(f"i:{amp_link[0][0]}", method="sendMessage")
    urls = [b.get("url") for b in buttons(p) if b.get("url")]
    check("link con «&»: URL del bottone intatto (niente &amp;)", urls and urls[0] == amp_link[0][1] and "&amp;" not in urls[0])
if long_link:
    p, _ = click(f"i:{long_link[0][0]}", method="sendMessage")
    check("link oltre 256 caratteri: niente bottone «Link», resta «Apri fonte»",
          p and not any(b.get("copy_text") for b in buttons(p)) and any(b.get("url") for b in buttons(p)))
p, _ = click(f"i:{fx_item}", method="sendMessage")
check("titolo e fonte con HTML ostile mostrati come testo",
      p and "&lt;img" in p["text"] and "<img" not in p["text"] and "&lt;i&gt;feed" in p["text"], (p or {}).get("text", "")[:200])
check("articolo senza testo: lo dice, niente bottone Testo",
      p and "Testo non disponibile" in p["text"] and not any(b.get("callback_data", "").startswith("t:") for b in buttons(p)))
p, _ = click("i:999999999", method="sendMessage")
check("articolo inesistente", p and "non trovato" in p["text"])

print("== testo completo")
if mid_text:
    p, _ = click(f"t:{mid_text}:1", method="sendMessage")
    check("testo medio: a pezzi sotto i 4096, testo semplice", p and len(p["text"]) <= 4096 and "parse_mode" not in p,
          (len((p or {}).get("text", "")), (p or {}).get("parse_mode")))
    nxt = [b for b in buttons(p) if b.get("callback_data", "").startswith(f"t:{mid_text}:2")]
    check("bottone «Continua»", bool(nxt))
    p, _ = click(f"t:{mid_text}:2", method="sendMessage")
    check("secondo pezzo", p and "(2/" in p["text"], (p or {}).get("text", "")[:120])
else:
    check("articolo di prova da 8-11 mila caratteri trovato", False)
if big_text:
    p, _ = click(f"t:{big_text}:1", method="sendDocument")
    check("testo lungo: arriva come file .txt", p and str(p.get("document", {}).get("filename", "")).endswith(".txt"), p)
else:
    check("articolo di prova oltre 20 mila caratteri trovato", False)

print("== modulo comune")
_b = core.SEARCH_BUDGET_S
core.SEARCH_BUDGET_S = 0.02
try:
    core.search("a*"); check("query lenta interrotta dal limite di tempo", False)
except core.SearchTooBroad:
    check("query lenta interrotta dal limite di tempo", True)
core.SEARCH_BUDGET_S = _b
_ref = core.search("Iran", page=3)
check("estratti calcolati solo per la pagina, uno per riga", len(_ref["rows"]) == 6 and all(x["snippet"] for x in _ref["rows"]))
import tempfile as _tf
_d = _tf.mkdtemp(dir=work)
import random as _rnd
_rnd.seed(7)
_gz = gzip.compress(bytes(_rnd.getrandbits(8) for _ in range(20000)))
open(os.path.join(_d, "rotto.txt.gz"), "wb").write(_gz[:30] + bytes(200) + _gz[230:])   # deflate danneggiato
open(os.path.join(_d, "altro.php"), "w").write("<?php")
_old = core.TEXT_DIR; core.TEXT_DIR = _d
check(".gz danneggiato: testo vuoto, nessun crash", core.read_text(os.path.join(_d, "rotto.txt.gz")) == "")
check("estensione diversa da .txt/.txt.gz: non letta", core.read_text(os.path.join(_d, "altro.php")) == "")
core.TEXT_DIR = _old

print("== limiti e log")
lens = [visible_len(p.get("text")) for m, p in calls() if m in ("sendMessage", "editMessageText")]
check("nessun messaggio oltre 4096 caratteri", max(lens) <= 4096, max(lens))
n0 = len(calls()); push(cbq(ME, "-")); time.sleep(1)
check("bottone «n/N» (nessuna azione) risponde senza errori", bool(answers(n0)))
n0 = len(calls()); push(msg(STRANGER, "Iran")); time.sleep(1.5)
check("estraneo: nessuna risposta alla ricerca", not sent_to(STRANGER, n0))
bot.send_signal(signal.SIGTERM)
try:
    rc = bot.wait(15)
except subprocess.TimeoutExpired:
    bot.kill(); rc = "timeout"
check("arresto pulito", rc == 0, rc)
logf.seek(0); log = logf.read()
check("nel log nessuna query (Groenlandia, l'iran, Tel Aviv)", all(s not in log.lower() for s in ("groenlandia", "l'iran", "tel aviv")))
check("nel log niente errori non gestiti", "errore non gestito" not in log and "Traceback" not in log, log[-800:])

q("DELETE FROM items WHERE id = ?", fx_item); q("DELETE FROM feeds WHERE id = ?", fx_feed)
ok, ko = results()
print(f"\nRisultato: {ok} OK, {ko} falliti")
sys.exit(1 if ko else 0)
