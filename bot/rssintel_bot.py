#!/usr/bin/env python3
"""
RSSIntel — bot Telegram.

Lotto L1: scheletro e identita'. Collegamento dell'account con un codice
monouso, /start /aiuto /stato /esci, lista bianca, solo chat private, token
mai nei log. Ricerca, allerte, notifiche e bollettino arrivano nei lotti
successivi.

Avvio (servizio systemd rssintel-bot.service):
    rssintel_bot.py
Riga di comando, come utente rssintel-bot (non serve il token):
    rssintel_bot.py codice <utente>      codice monouso per /start, valido 15 minuti
    rssintel_bot.py collegamenti         account Telegram collegati
    rssintel_bot.py revoca <utente>      scollega tutti gli account di un utente

Il token e gli ID autorizzati arrivano come credenziali systemd
($CREDENTIALS_DIRECTORY/bot_token e allowed_ids), mai dall'ambiente: le
variabili d'ambiente si leggono da /proc e passano ai processi figli.

Scelte, e perche':
  - La libreria scrive il token in chiaro nel testo di InvalidToken, e
    abbassare il log di httpx non basta: il mascheramento si applica al testo
    FINALE di ogni riga di log (tracce d'errore comprese) e alle eccezioni non
    intercettate. Token rifiutato = codice d'uscita 78, che la unit non
    riavvia (RestartPreventExitStatus): un token sbagliato non deve girare in
    ciclo.
  - run_polling ritenta la connessione iniziale senza limite: dopo un riavvio
    del server con la rete lenta il servizio non deve finire in "failed".
  - Chi non e' nella lista bianca non riceve risposta e non causa accessi al
    database. Per gli altri, utente, ruolo e disattivazione si rileggono dal
    DB a ogni comando e a ogni bottone; i dati dei bottoni non sono fidati.
  - I log contengono solo nomi dei comandi e tempi, mai testo dei messaggi,
    query o ID: /logs del bot di amministrazione rimanda il journal in chat.
"""
from __future__ import annotations

import asyncio
import collections
import html
import logging
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rssintel_core as core  # noqa: E402

VERSION = "L3"
EXIT_CONFIG = 78                    # EX_CONFIG: la unit non riavvia
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
RATE_PER_MIN = int(os.environ.get("RSSINTEL_BOT_RATE_PER_MIN", "30"))   # comandi e bottoni al minuto
WATCHDOG_EVERY = float(os.environ.get("RSSINTEL_BOT_WATCHDOG_S", "300"))   # controllo del token
WATCH_JOBS_EVERY = float(os.environ.get("RSSINTEL_BOT_WATCH_JOBS_S", "600"))   # scadenze delle sentinelle
LINK_FAILS_MAX = 5                  # tentativi di codice sbagliati ...
LINK_FAILS_WINDOW = 15 * 60         # ... in 15 minuti

log = logging.getLogger("rssintel-bot")


def esc(s: str) -> str:
    """Escape per il testo dei messaggi (HTML di Telegram): gli apici non servono fuori dagli attributi."""
    return html.escape(s, quote=False)


# ------------------------------------------------------------------ log sicuri

def redact(text: str) -> str:
    return TOKEN_RE.sub("<TOKEN>", text)


class RedactingFormatter(logging.Formatter):
    """Maschera il token sul testo finale, traceback compresi.

    Un logging.Filter non basterebbe: lavora prima della formattazione e non
    vede exc_text.
    """
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)
    # httpx/httpcore registrano ogni URL, che contiene il token
    for name in ("httpx", "httpcore", "apscheduler", "telegram.ext.Updater"):
        logging.getLogger(name).setLevel(logging.WARNING)

    def hook(t, v, tb):
        sys.stderr.write(redact("".join(traceback.format_exception(t, v, tb))))
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


# ---------------------------------------------------------------- credenziali

def read_credentials() -> tuple[str, frozenset[int]]:
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    if not d:
        log.error("CREDENTIALS_DIRECTORY assente: il bot va avviato dalla unit systemd")
        sys.exit(EXIT_CONFIG)
    try:
        token = (Path(d) / "bot_token").read_text().strip()
        raw = (Path(d) / "allowed_ids").read_text()
    except OSError as e:
        log.error("credenziale mancante: %s", e.filename and os.path.basename(e.filename))
        sys.exit(EXIT_CONFIG)
    ids = frozenset(int(x) for x in re.split(r"[\s,;]+", raw) if x.strip().lstrip("-").isdigit())
    if not TOKEN_RE.fullmatch(token):
        log.error("bot_token non ha la forma di un token Telegram")
        sys.exit(EXIT_CONFIG)
    if not ids:
        log.error("allowed_ids vuoto: nessuno potrebbe usare il bot")
        sys.exit(EXIT_CONFIG)
    return token, ids


# ------------------------------------------------------------- riga di comando

def cli(argv: list[str]) -> int:
    cmd = argv[0]
    try:
        missing = core.missing_tables()
    except Exception as e:      # sqlite3.OperationalError: percorso sbagliato o permessi
        print(f"Errore: impossibile aprire {core.DB_PATH} ({e}).\n"
              "Imposta RSSINTEL_DB, per esempio:\n"
              "  sudo -u rssintel-bot env RSSINTEL_DB=/percorso/rssintel.db "
              f"{sys.executable} {os.path.abspath(__file__)} {' '.join(argv)}", file=sys.stderr)
        return 1
    if missing:
        print(f"Errore: tabelle del portale mancanti ({', '.join(missing)}): controlla RSSINTEL_DB",
              file=sys.stderr)
        return 1
    core.ensure_schema(with_state=False)
    if cmd == "codice" and len(argv) == 2:
        try:
            code = core.new_link_code(argv[1])
        except ValueError as e:
            print(f"Errore: {e}", file=sys.stderr)
            return 1
        print(f"Codice per {argv[1]}: {code}  (valido {core.CODE_TTL_MIN} minuti, una sola volta)")
        print(f"Su Telegram, invia al bot:  /start {code}")
        return 0
    if cmd == "collegamenti" and len(argv) == 1:
        links = core.active_links()
        if not links:
            print("Nessun account Telegram collegato.")
        for l in links:
            print(f"#{l['id']}  {l['username']:<20} collegato {l['linked_at']} UTC · "
                  f"ultimo uso {l['last_seen_at'] or '-'}" + ("  [bot bloccato]" if l["blocked_at"] else ""))
        return 0
    if cmd == "revoca" and len(argv) == 2:
        n = core.revoke(username=argv[1])
        print(f"Collegamenti revocati: {n}")
        return 0
    print(__doc__.split("Il token")[0].strip(), file=sys.stderr)
    return 2


# ===================================================================== il bot

def run_bot() -> int:
    from telegram import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CopyTextButton,
                          InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update)
    from telegram.constants import ChatAction, ChatMemberStatus, ChatType, ParseMode
    from telegram.error import BadRequest, Forbidden, InvalidToken, TelegramError
    from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler,
                              CommandHandler, ContextTypes, Defaults, MessageHandler, filters)

    token, allowed = read_credentials()

    missing = core.missing_tables()
    if missing:
        log.error("tabelle del portale mancanti (%s): il DB non e' quello di RSSIntel", ", ".join(missing))
        return EXIT_CONFIG
    core.ensure_schema()

    hidden = [p for p in os.environ.get("RSSINTEL_BOT_HIDDEN", "").split(":") if p]
    readonly = [p for p in os.environ.get("RSSINTEL_BOT_READONLY", "").split(":") if p]
    perm_problems = core.db_permissions()
    sandbox = core.sandbox_report(hidden, readonly)
    for p in perm_problems:
        log.warning("permessi: %s", p)
    if any(not ok for _, ok in sandbox):
        log.warning("sandbox: " + ", ".join(f"{d} {'ok' if ok else 'NO'}" for d, ok in sandbox))
    else:
        log.info("sandbox: " + ", ".join(f"{d} {'ok' if ok else 'NO'}" for d, ok in sandbox))
    started = time.time()

    commands = [
        BotCommand("cerca", "cerca nell'archivio (oppure scrivi direttamente)"),
        BotCommand("segui", "crea una sentinella sui nuovi articoli"),
        BotCommand("allerte", "le tue sentinelle"),
        BotCommand("novita", "segnalazioni non ancora viste"),
        BotCommand("aiuto", "cosa sa fare il bot"),
        BotCommand("stato", "stato della raccolta e del bot"),
        BotCommand("esci", "scollega questo account"),
    ]

    rate: dict[int, collections.deque] = collections.defaultdict(collections.deque)
    link_fails: dict[int, collections.deque] = collections.defaultdict(collections.deque)
    ignored = {"last_log": -1e9}

    def over_rate(uid: int, bucket: dict, limit: int, window: float) -> bool:
        q, now = bucket[uid], time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    async def reply(update: Update, text: str, **kw) -> None:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, **kw)
        else:
            await update.effective_message.reply_text(text, **kw)

    def guarded(name: str, need_link: bool = True):
        """Porta d'ingresso unica: lista bianca, chat privata, utente dal DB."""
        def deco(fn):
            async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
                user, chat, q = update.effective_user, update.effective_chat, update.callback_query
                try:
                    if user is None or user.id not in allowed:
                        now = time.monotonic()
                        if now - ignored["last_log"] > 60:      # al massimo una riga al minuto
                            log.info("ignorati aggiornamenti da utenti non autorizzati")
                            ignored["last_log"] = now
                        return
                    if chat is None or chat.type != ChatType.PRIVATE:
                        return
                    if over_rate(user.id, rate, RATE_PER_MIN, 60):
                        if q:                    # sui bottoni un avviso breve, non il silenzio
                            await q.answer("Troppe richieste in un minuto: riprova tra poco.")
                        else:
                            await update.effective_message.reply_text("Troppi comandi in un minuto: riprova tra poco.")
                        return
                    rate[user.id].append(time.monotonic())
                    link = await asyncio.to_thread(core.resolve_link, user.id)
                    if link and link["disabled"] and not (name == "start" and context.args):
                        await reply(update, "Il tuo utente RSSIntel è disattivato.")
                        return
                    if need_link and not link:
                        await reply(update, "Questo account Telegram non è collegato a RSSIntel.\n"
                                            "Genera un codice sul server e invia <code>/start CODICE</code>.")
                        return
                    t0 = time.monotonic()
                    try:
                        await fn(update, context, link)
                    finally:
                        log.info("%s %d ms", name, (time.monotonic() - t0) * 1000)
                        await asyncio.to_thread(core.count_usage, name)
                        if link:
                            await asyncio.to_thread(core.touch_link, link["link_id"])
                finally:
                    if q:
                        try:
                            await q.answer()
                        except TelegramError:
                            pass
            return wrapper
        return deco

    async def set_chat_commands(bot, chat_id: int) -> None:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id))

    # ---------------------------------------------------------------- comandi

    @guarded("start", need_link=False)
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        uid = update.effective_user.id
        if context.args:
            if over_rate(uid, link_fails, LINK_FAILS_MAX, LINK_FAILS_WINDOW):
                await reply(update, "Troppi codici sbagliati: riprova tra 15 minuti.")
                return
            username = await asyncio.to_thread(
                core.redeem_link_code, context.args[0], uid, update.effective_chat.id)
            if not username:
                link_fails[uid].append(time.monotonic())
                await reply(update, "Codice non valido, già usato o scaduto.")
                return
            link_fails.pop(uid, None)
            log.info("nuovo collegamento creato")
            try:
                await set_chat_commands(context.bot, update.effective_chat.id)
            except TelegramError:
                log.warning("impossibile impostare il menu dei comandi")
            await reply(update, f"✅ Collegato a RSSIntel come <b>{esc(username)}</b>.\n\n"
                                "Scrivi /aiuto per vedere cosa posso fare.")
            ws = await asyncio.to_thread(core.list_watches, username)
            if not any(w["watch"] for w in ws):
                await show_suggestions(update, "Vuoi partire da qualche sentinella? Tocca per vedere quanti "
                                               "articoli troverebbe; non creo nulla senza la tua conferma.")
            return
        if link:
            await reply(update, f"Sei collegato come <b>{esc(link['username'])}</b>. /aiuto")
        else:
            await reply(update, "Per collegare questo account a RSSIntel genera un codice sul server:\n"
                                "<code>sudo -u rssintel-bot env RSSINTEL_DB=… "
                                "/opt/rssintel-bot/venv/bin/python /opt/rssintel-bot/rssintel_bot.py codice UTENTE</code>\n"
                                "poi invia qui <code>/start CODICE</code>.")

    @guarded("aiuto")
    async def cmd_aiuto(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        await reply(update,
                    "<b>RSSIntel</b>\n\n"
                    "<b>Cercare</b>: scrivi una o più parole, oppure <code>/cerca parole</code>.\n"
                    "Risultati dal più recente, 6 per pagina; tocca 📄 per la scheda dell'articolo.\n"
                    "Sintassi come sul portale: <code>Iran OR Israele</code>, "
                    "<code>\"Mar Rosso\"</code>, <code>Hezbollah*</code>, <code>title:gaza</code>. "
                    "Se una ricerca non è valida la reinterpreto e te lo dico.\n\n"
                    "<b>Sentinelle</b>: <code>/segui parole</code> crea un'allerta sui nuovi articoli "
                    "(prima ti dico quanti ne avrebbe trovati negli ultimi 14 giorni); "
                    "/allerte per gestirle, /novita per le segnalazioni non viste. "
                    "Sono le stesse ricerche salvate del portale.\n\n"
                    "/stato — ultimo giro di raccolta e stato del bot\n"
                    "/esci — scollega questo account\n\n"
                    "<i>In arrivo: notifiche automatiche delle sentinelle, bollettino del giorno.</i>")

    @guarded("stato")
    async def cmd_stato(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        s = await asyncio.to_thread(core.status)
        up = int(time.time() - started)
        lines = [
            "<b>Stato di RSSIntel</b>",
            f"Ultimo giro di raccolta: {core.fmt_dt(s['last_fetch'])}",
            f"Fonti attive: {s['feeds']}" + (f" ({s['feeds_err']} con errore)" if s["feeds_err"] else ""),
            f"Articoli nelle ultime 24 ore: {s['items_24h']}",
            f"Sentinelle attive: {s['watches']}",
            f"Bot: versione {VERSION}, attivo da {up // 3600} h {up % 3600 // 60} min",
        ]
        if link["role"] == "admin":
            probs = await asyncio.to_thread(core.db_permissions)
            lines.append("Permessi del database: " + ("ok" if not probs else "⚠️ " + esc("; ".join(probs))))
            if sandbox:
                bad = [d for d, ok in sandbox if not ok]
                lines.append("Sandbox: " + ("ok" if not bad else "⚠️ " + esc(", ".join(bad))))
            n = len(await asyncio.to_thread(core.active_links))
            lines.append(f"Account collegati: {n}")
        await reply(update, "\n".join(lines))

    @guarded("esci")
    async def cmd_esci(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Sì, scollega", callback_data="esci:si"),
                                    InlineKeyboardButton("Annulla", callback_data="esci:no")]])
        await reply(update, "Scollegare questo account Telegram da RSSIntel?", reply_markup=kb)

    @guarded("esci_conferma")
    async def cb_esci(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        if update.callback_query.data != "esci:si":
            await reply(update, "Nessuna modifica.")
            return
        await asyncio.to_thread(core.revoke, link["link_id"])
        log.info("collegamento revocato dal bot")
        try:
            await context.bot.delete_my_commands(scope=BotCommandScopeChat(update.effective_chat.id))
        except TelegramError:
            pass
        await reply(update, "Account scollegato. Per ricollegarlo serve un codice nuovo.")

    # ------------------------------------------------- ricerca e lettura (L2)

    RE_PAGE = re.compile(r"^s:([a-z2-9]{8}):(\d{1,5})$")
    RE_CAT = re.compile(r"^sc:([a-z2-9]{8})(?::(\d))?$")
    RE_ITEM = re.compile(r"^i:(\d{1,10})$")
    RE_TEXT = re.compile(r"^t:(\d{1,10}):(\d{1,3})$")
    CAT_LABEL = {"": "tutte le fonti", "__none": "fonti non classificate",
                 **{k: v for k, v in core.CATEGORIES.items()}}
    MAX_Q = 200
    EXCERPT = 700

    def clean_query(raw: str) -> str:
        # niente caratteri di controllo o di formato (copia-incolla dal telefono)
        # prima ogni spazio (a capo compresi) diventa uno spazio, poi via i caratteri
        # di controllo e di formato: «Iran⏎Gaza» resta «Iran Gaza», non «IranGaza»
        q = "".join(" " if ch.isspace() else ch for ch in raw)
        q = "".join(ch for ch in q if unicodedata.category(ch)[0] != "C")
        return " ".join(q.split())[:MAX_Q]

    def render_results(r: dict, tok: str, with_snippets: bool = True):
        n = r["total"]
        head = f"🔎 <code>{esc(r['q'])}</code> — {n} risultat{'o' if n == 1 else 'i'}"
        if r["pages"] > 1:
            head += f" · pag. {r['page']}/{r['pages']}"
        lines = [head]
        if r["reinterpreted"]:
            lines.append(f"<i>interpretata come</i> <code>{esc(r['effective'])}</code>")
        if r["cat"]:
            lines.append(f"🗂 solo {esc(CAT_LABEL.get(r['cat'], r['cat']))}")
        if not r["rows"]:
            lines.append("\nNessun articolo." + ("" if r["cat"] else
                         " Prova con meno parole, con <code>OR</code> o con un prefisso come <code>Iran*</code>."))
        first = (r["page"] - 1) * core.PER_PAGE + 1
        for k, x in enumerate(r["rows"], start=first):
            meta = [esc(x["feed"] or "")]
            b = core.badge(x["category"], x["reliability"])
            if b:
                meta.append(b)
            meta.append(core.fmt_dt(x["published_at"] or x["fetched_at"]))
            h = core.host(core.safe_url(x["link"]))
            if h:
                meta.append("🌐 " + esc(h))
            block = f"\n<b>{k}.</b> {esc(core.clean_title(x['title']) or '(senza titolo)')}\n<i>{' · '.join(meta)}</i>"
            if with_snippets and x["snippet"]:
                block += "\n" + core.snippet_html(x["snippet"])
            lines.append(block)
        text = "\n".join(lines)
        rows = []
        btn = [InlineKeyboardButton(f"📄{k}", callback_data=f"i:{x['id']}")
               for k, x in enumerate(r["rows"], start=first)]
        if btn:
            rows += [btn[:3], btn[3:]] if len(btn) > 3 else [btn]
        if r["pages"] > 1:
            nav = []
            if r["page"] > 1:
                nav.append(InlineKeyboardButton("◀", callback_data=f"s:{tok}:{r['page'] - 1}"))
            nav.append(InlineKeyboardButton(f"{r['page']}/{r['pages']}", callback_data="-"))
            if r["page"] < r["pages"]:
                nav.append(InlineKeyboardButton("▶", callback_data=f"s:{tok}:{r['page'] + 1}"))
            rows.append(nav)
        rows.append([InlineKeyboardButton("🗂 Categoria", callback_data=f"sc:{tok}"),
                     InlineKeyboardButton("🔔 Segui", callback_data=f"sw:{tok}")])
        return text, InlineKeyboardMarkup(rows)

    async def show_results(update: Update, r: dict, tok: str, edit: bool) -> None:
        text, kb = render_results(r, tok)
        if len(text) > 4000:                     # limite di Telegram: 4096
            text, kb = render_results(r, tok, with_snippets=False)
        if edit:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=kb)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
        else:
            await update.effective_message.reply_text(text, reply_markup=kb)

    async def run_search(update: Update, link, q: str, cat: str = "", page: int = 1,
                         tok: str | None = None, orig: str | None = None) -> None:
        try:
            await update.effective_chat.send_action(ChatAction.TYPING)   # «sta scrivendo…» mentre cerca
        except TelegramError:
            pass
        try:
            r = await asyncio.to_thread(core.search, q, cat, page)
        except core.SearchTooBroad:
            await reply(update, f"Ricerca troppo larga: l'ho interrotta dopo {core.SEARCH_BUDGET_S:.0f} secondi. "
                                "Restringila con più parole o un prefisso più lungo.")
            return
        except core.SearchError:
            await reply(update, "Non riesco a interpretare questa ricerca. /aiuto per la sintassi.")
            return
        except Exception as e:                   # DB occupato o non raggiungibile: lo si dice
            log.warning("ricerca non riuscita: %s", type(e).__name__)
            await reply(update, "Errore temporaneo nella ricerca: riprova tra poco.")
            return
        if orig is not None:                     # paginazione: si esegue gia' la forma che funziona
            r["q"], r["reinterpreted"] = orig, orig != r["effective"]
        if tok is None:
            tok = await asyncio.to_thread(core.ctx_new, link["link_id"], r["effective"], r["q"], cat)
        await show_results(update, r, tok, edit=update.callback_query is not None)

    @guarded("cerca")
    async def cmd_cerca(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        q = clean_query(" ".join(context.args or []))
        if not q:
            await reply(update, "Scrivi <code>/cerca</code> seguito dalle parole, "
                                "oppure scrivi direttamente le parole.")
            return
        await run_search(update, link, q)

    @guarded("cerca")
    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        q = clean_query(update.effective_message.text or "")
        if q:
            await run_search(update, link, q)

    async def expired(update: Update) -> None:
        await update.callback_query.answer("Ricerca scaduta: rifalla scrivendo le parole.", show_alert=False)

    @guarded("pagina")
    async def cb_page(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        m = RE_PAGE.match(update.callback_query.data)
        c = await asyncio.to_thread(core.ctx_get, m.group(1), link["link_id"])
        if not c:
            return await expired(update)
        await run_search(update, link, c["q"], c["cat"], int(m.group(2)), tok=m.group(1), orig=c["orig"])

    @guarded("categoria")
    async def cb_cat(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        m = RE_CAT.match(update.callback_query.data)
        tok = m.group(1)
        c = await asyncio.to_thread(core.ctx_get, tok, link["link_id"])
        if not c:
            return await expired(update)
        if m.group(2) is None:                   # mostra la scelta
            keys = core.CATEGORY_KEYS
            btn = [InlineKeyboardButton(("✓ " if k == c["cat"] else "") + CAT_LABEL[k].capitalize(),
                                        callback_data=f"sc:{tok}:{i}") for i, k in enumerate(keys)]
            kb = InlineKeyboardMarkup([btn[i:i + 2] for i in range(0, len(btn), 2)])
            await update.callback_query.edit_message_reply_markup(reply_markup=kb)
            return
        idx = int(m.group(2))
        if idx >= len(core.CATEGORY_KEYS):
            return
        cat = core.CATEGORY_KEYS[idx]
        await asyncio.to_thread(core.ctx_set_cat, tok, link["link_id"], cat)
        await run_search(update, link, c["q"], cat, 1, tok=tok, orig=c["orig"])

    @guarded("scheda")
    async def cb_item(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        item_id = int(RE_ITEM.match(update.callback_query.data).group(1))
        it = await asyncio.to_thread(core.get_item, item_id)
        chat = update.effective_chat
        if not it:
            await chat.send_message("Articolo non trovato (forse la fonte è stata eliminata).")
            return
        body = await asyncio.to_thread(core.read_text, it["text_path"])
        meta = [esc(it["feed"] or "")]
        b = core.badge(it["category"], it["reliability"])
        if b:
            meta.append(b)
        url = core.safe_url(it["link"])
        when = core.fmt_dt(it["published_at"] or it["fetched_at"])
        lines = [f"<b>{esc(core.clean_title(it['title']) or '(senza titolo)')}</b>",
                 f"<i>{' · '.join(meta)}</i>",
                 when + (f" · 🌐 {esc(core.host(url))}" if url else "")]
        if body:
            ex = body if len(body) <= EXCERPT else body[:EXCERPT].rsplit(" ", 1)[0] + " …"
            lines.append(f"<blockquote expandable>{esc(ex)}</blockquote>")
        else:
            lines.append("<i>Testo non disponibile.</i>")
        rows, first = [], []
        if url:
            first.append(InlineKeyboardButton("🌐 Apri fonte", url=url))
            if core.utf16_len(url) <= 256:       # limite di CopyTextButton
                first.append(InlineKeyboardButton("📋 Link", copy_text=CopyTextButton(url)))
        if first:
            rows.append(first)
        if body:
            rows.append([InlineKeyboardButton(f"📄 Testo completo ({len(body):,} caratteri)".replace(",", "."),
                                              callback_data=f"t:{item_id}:1")])
        text = "\n".join(lines)
        try:
            await chat.send_message(text, reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        except BadRequest:
            # un URL che Telegram rifiuta fa rifiutare l'intero messaggio: si rinuncia ai bottoni-link
            rows = [r for r in rows if not any(getattr(x, "url", None) or getattr(x, "copy_text", None) for x in r)]
            await chat.send_message(text + (f"\n{esc(url)}" if url else ""),
                                    reply_markup=InlineKeyboardMarkup(rows) if rows else None)

    @guarded("testo")
    async def cb_text(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        m = RE_TEXT.match(update.callback_query.data)
        item_id, n = int(m.group(1)), int(m.group(2))
        it = await asyncio.to_thread(core.get_item, item_id)
        body = await asyncio.to_thread(core.read_text, it["text_path"]) if it else ""
        chat = update.effective_chat
        if not body:
            await chat.send_message("Testo non disponibile.")
            return
        title = core.clean_title(it["title"]) or f"articolo {item_id}"
        parts = core.split_text(body)
        if len(parts) > 4:                       # troppo lungo per la chat: un file
            await chat.send_document(document=body.encode("utf-8"), filename=f"rssintel-{item_id}.txt",
                                     caption=f"📄 {esc(title[:900])}")
            return
        n = max(1, min(n, len(parts)))
        head = f"📄 {title} ({n}/{len(parts)})\n\n" if len(parts) > 1 else f"📄 {title}\n\n"
        kb = None
        if n < len(parts):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"▶ Continua ({n + 1}/{len(parts)})",
                                                             callback_data=f"t:{item_id}:{n + 1}")]])
        # testo semplice: niente HTML, niente escape che allungherebbe il messaggio
        await chat.send_message(head + parts[n - 1], parse_mode=None, reply_markup=kb)

    async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except TelegramError:
                pass

    # ------------------------------------------------------ sentinelle (L3)

    RE_W = re.compile(r"^w:([a-z2-9]{8}):(m:[idn]|e:(?:0|7|14)|ok|no)$")
    RE_SW = re.compile(r"^sw:([a-z2-9]{8})$")
    RE_G = re.compile(r"^g:(\d)$")
    RE_A = re.compile(r"^(al|a:\d{1,9}|am:\d{1,9}:[idn]|ap:\d{1,9}:(?:0|1|7)|aw:\d{1,9}:[01]|"
                      r"ae:\d{1,9}:7|ad:\d{1,9}|ay:\d{1,9})$")
    RE_N = re.compile(r"^(nva|nv:\d{1,9})$")
    SUGGEST = core.load_suggestions(os.environ.get("RSSINTEL_BOT_SUGGEST_FILE"))
    NOTIFY_NOTE = ("<i>Le notifiche automatiche arrivano con il prossimo aggiornamento del bot; "
                   "intanto le segnalazioni sono in /novita e nella pagina Novità del portale.</i>")

    def fmt_rate(x: float) -> str:
        return f"{x:.1f}".replace(".", ",")

    def days_label(d) -> str:
        return "mai" if not d else f"{d} giorni"

    async def show_pending(update: Update, tok: str, pd: dict, est: dict, edit: bool) -> None:
        lines = [f"🔔 <b>Nuova sentinella</b>: {esc(pd['name'])}",
                 f"Query: <code>{esc(pd['q'])}</code>"]
        if pd["orig"] and pd["orig"] != pd["q"]:
            lines.append(f"<i>interpretata da</i> <code>{esc(pd['orig'])}</code>")
        lines.append(f"Negli ultimi {est['days']} giorni: <b>{fmt_rate(est['per_day'])} articoli al giorno</b>"
                     + (f" (massimo {est['max_day']} in un giorno)" if est["max_day"] else "") + ".")
        lines.append("Consiglio: " + core.MODES[core.suggest_mode(est["per_day"])] + ".")
        lines.append(f"\nModalità: <b>{core.MODES[pd['mode']]}</b> · scadenza: <b>{days_label(pd['days'])}</b>")
        lines.append(NOTIFY_NOTE)
        mrow = [InlineKeyboardButton(("✓ " if pd["mode"] == m else "") + core.MODES[m], callback_data=f"w:{tok}:m:{c}")
                for c, m in core.MODE_CODE.items()]
        erow = [InlineKeyboardButton(("✓ " if (pd["days"] or 0) == d else "") + days_label(d), callback_data=f"w:{tok}:e:{d}")
                for d in (7, 14, 0)]
        kb = InlineKeyboardMarkup([mrow[:2], mrow[2:], erow,
                                   [InlineKeyboardButton("✅ Crea", callback_data=f"w:{tok}:ok"),
                                    InlineKeyboardButton("Annulla", callback_data=f"w:{tok}:no")]])
        text = "\n".join(lines)
        if edit:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=kb)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
        else:
            await update.effective_chat.send_message(text, reply_markup=kb)

    async def start_pending(update: Update, link, q: str, orig: str | None = None, name: str | None = None,
                            mode: str | None = None, days: int | None = 14) -> None:
        try:
            await update.effective_chat.send_action(ChatAction.TYPING)
        except TelegramError:
            pass
        try:
            eff, _ = await asyncio.to_thread(core.resolve_query, q)
            est = await asyncio.to_thread(core.estimate, eff)
        except core.SearchTooBroad:
            await update.effective_chat.send_message("Ricerca troppo larga per una sentinella: restringila.")
            return
        except core.SearchError:
            await update.effective_chat.send_message("Non riesco a interpretare questa ricerca. /aiuto")
            return
        free, same = await asyncio.to_thread(core.free_name, link["username"], name or orig or q, eff)
        pd = {"q": eff, "orig": orig if orig is not None else q, "name": free,
              "mode": mode or core.suggest_mode(est["per_day"]), "days": days}
        tok = await asyncio.to_thread(core.pend_new, link["link_id"], pd["q"], pd["orig"], pd["name"], pd["mode"], pd["days"])
        await show_pending(update, tok, pd, est, edit=False)

    async def show_suggestions(update: Update, intro: str) -> None:
        if not SUGGEST:
            return
        btn = [[InlineKeyboardButton(f"🔔 {x['name']}", callback_data=f"g:{i}")] for i, x in enumerate(SUGGEST)]
        await update.effective_chat.send_message(intro, reply_markup=InlineKeyboardMarkup(btn))

    @guarded("segui")
    async def cmd_segui(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        q = clean_query(" ".join(context.args or []))
        if not q:
            await reply(update, "Scrivi <code>/segui</code> seguito dalle parole da sorvegliare, "
                                "per esempio <code>/segui Groenlandia</code>. Oppure, da una ricerca, tocca 🔔 Segui.")
            await show_suggestions(update, "Oppure scegli una di queste:")
            return
        await start_pending(update, link, q)

    @guarded("segui")
    async def cb_sw(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        c = await asyncio.to_thread(core.ctx_get, RE_SW.match(update.callback_query.data).group(1), link["link_id"])
        if not c:
            return await expired(update)
        await start_pending(update, link, c["q"], orig=c["orig"])

    @guarded("suggerita")
    async def cb_g(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        i = int(RE_G.match(update.callback_query.data).group(1))
        if i >= len(SUGGEST):
            return
        x = SUGGEST[i]
        await start_pending(update, link, x["q"], name=x["name"], mode=x["mode"], days=None)

    @guarded("segui_scelta")
    async def cb_w(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        m = RE_W.match(update.callback_query.data)
        tok, act = m.group(1), m.group(2)
        pd = await asyncio.to_thread(core.pend_get, tok, link["link_id"])
        if not pd:
            await update.callback_query.answer("Richiesta scaduta: rifai /segui.")
            return
        if act == "no":
            await asyncio.to_thread(core.pend_drop, tok, link["link_id"])
            await update.callback_query.edit_message_text("Nessuna sentinella creata.")
            return
        if act == "ok":
            if not await asyncio.to_thread(core.pend_drop, tok, link["link_id"]):
                return                            # gia' creata da un tocco precedente
            sid, replaced = await asyncio.to_thread(core.create_watch, link["username"], pd["name"], pd["q"],
                                                    pd["mode"], pd["days"] or None)
            msg = (f"✅ Sentinella <b>{esc(pd['name'])}</b> attiva: {core.MODES[pd['mode']]}, "
                   f"scadenza {days_label(pd['days'])}.\n"
                   + ("<i>Esisteva già questa ricerca salvata: l'ho aggiornata.</i>\n" if replaced else "")
                   + "Da ora segnalo i nuovi articoli; la vedi anche nel portale con 🔔. /allerte per gestirla.")
            await update.callback_query.edit_message_text(msg)
            return
        if act.startswith("m:"):
            await asyncio.to_thread(core.pend_set, tok, link["link_id"], mode=core.MODE_CODE[act[2]])
        else:
            await asyncio.to_thread(core.pend_set, tok, link["link_id"], days=int(act[2:]) or None)
        pd = await asyncio.to_thread(core.pend_get, tok, link["link_id"])
        if not pd:
            await update.callback_query.answer("Richiesta scaduta: rifai /segui.")
            return
        try:
            est = await asyncio.to_thread(core.estimate, pd["q"])
        except (core.SearchTooBroad, core.SearchError):
            est = {"days": core.ESTIMATE_DAYS, "total": 0, "per_day": 0.0, "max_day": 0}
        await show_pending(update, tok, pd, est, edit=True)

    def watch_line(w: dict) -> str:
        bits = [core.MODES.get(w["mode"], w["mode"]) if w["watch"] else "🔕 spenta"]
        if w["unseen"]:
            bits.append(f"{w['unseen']} non viste")
        if w["paused_until"]:
            bits.append("⏸ in pausa fino al " + core.fmt_dt(w["paused_until"]))
        if w["expires_at"]:
            bits.append("scade il " + core.fmt_dt(w["expires_at"]))
        if w["last_error"]:
            bits.append("⚠️ query non valida")
        name = w["name"] if len(w["name"]) <= 60 else w["name"][:59] + "…"
        return f"{'🔔' if w['watch'] else '🔕'} <b>{esc(name)}</b> — {' · '.join(bits)}"

    async def show_watch_list(update: Update, link, edit: bool) -> None:
        ws = await asyncio.to_thread(core.list_watches, link["username"])
        if not ws:
            text = "Non hai sentinelle. Crea la prima con <code>/segui parole</code>."
            kb = None
        else:
            lines, shown = ["<b>Le tue sentinelle</b>\n"], 0
            for w in ws[:30]:
                line = watch_line(w)
                if sum(len(x) + 1 for x in lines) + len(line) > 3800:
                    break
                lines.append(line); shown += 1
            if shown < len(ws):
                lines.append(f"\n… e altre {len(ws) - shown} nel portale.")
            text = "\n".join(lines)
            ws = ws[:shown]
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(w["name"][:40], callback_data=f"a:{w['id']}")]
                                       for w in ws[:30]])
        if edit:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            await update.effective_chat.send_message(text, reply_markup=kb)

    async def show_watch(update: Update, link, sid: int, note: str = "") -> None:
        w = await asyncio.to_thread(core.get_watch, link["username"], sid)
        if not w:
            await update.callback_query.edit_message_text("Sentinella non trovata (forse eliminata).")
            return
        qq = w["q"] if len(w["q"]) <= 300 else w["q"][:299] + "…"
        lines = [watch_line(w), f"Query: <code>{esc(qq)}</code>"]
        if note:
            lines.append(note)
        rows = [[InlineKeyboardButton(("✓ " if w["mode"] == m else "") + core.MODES[m], callback_data=f"am:{sid}:{c}")
                 for c, m in list(core.MODE_CODE.items())[:2]],
                [InlineKeyboardButton(("✓ " if w["mode"] == "novita" else "") + core.MODES["novita"], callback_data=f"am:{sid}:n")]]
        if w["paused_until"]:
            rows.append([InlineKeyboardButton("▶ Riprendi le notifiche", callback_data=f"ap:{sid}:0")])
        else:
            rows.append([InlineKeyboardButton("⏸ Pausa 24 ore", callback_data=f"ap:{sid}:1"),
                         InlineKeyboardButton("⏸ Pausa 7 giorni", callback_data=f"ap:{sid}:7")])
        row = [InlineKeyboardButton("🔕 Spegni" if w["watch"] else "🔔 Accendi", callback_data=f"aw:{sid}:{0 if w['watch'] else 1}")]
        if w["expires_at"]:
            row.append(InlineKeyboardButton("⏳ Prolunga 7 giorni", callback_data=f"ae:{sid}:7"))
        rows.append(row)
        rows.append([InlineKeyboardButton("🗑 Elimina", callback_data=f"ad:{sid}"),
                     InlineKeyboardButton("« Elenco", callback_data="al")])
        try:
            await update.callback_query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    @guarded("allerte")
    async def cmd_allerte(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        await show_watch_list(update, link, edit=False)

    @guarded("allerte_azione")
    async def cb_a(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        data, owner = update.callback_query.data, link["username"]
        if data == "al":
            return await show_watch_list(update, link, edit=True)
        parts = data.split(":")
        verb, sid = parts[0], int(parts[1])
        if verb == "a":
            return await show_watch(update, link, sid)
        if verb == "am":
            await asyncio.to_thread(core.set_mode, owner, sid, core.MODE_CODE[parts[2]])
            return await show_watch(update, link, sid)
        if verb == "ap":
            await asyncio.to_thread(core.set_pause, owner, sid, int(parts[2]))
            return await show_watch(update, link, sid)
        if verb == "aw":
            await asyncio.to_thread(core.set_watch, owner, sid, parts[2] == "1")
            return await show_watch(update, link, sid, "" if parts[2] == "1" else
                                    "<i>Spenta: niente più segnalazioni nuove; quelle trovate restano in /novita.</i>")
        if verb == "ae":
            await asyncio.to_thread(core.extend_watch, owner, sid, 7)
            return await show_watch(update, link, sid, "<i>Scadenza prolungata di 7 giorni.</i>")
        if verb == "ad":
            w = await asyncio.to_thread(core.get_watch, owner, sid)
            if not w:
                return await show_watch(update, link, sid)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Sì, elimina", callback_data=f"ay:{sid}"),
                                        InlineKeyboardButton("Annulla", callback_data=f"a:{sid}")]])
            await update.callback_query.edit_message_text(
                f"Eliminare la sentinella <b>{esc(w['name'])}</b>? Sparisce anche dal portale, "
                "con le sue segnalazioni.", reply_markup=kb)
            return
        if verb == "ay":
            ok = await asyncio.to_thread(core.delete_watch, owner, sid)
            await update.callback_query.edit_message_text("Sentinella eliminata." if ok else "Già eliminata.")

    async def show_novita(update: Update, link, edit: bool) -> None:
        groups = await asyncio.to_thread(core.unseen, link["username"])
        if not groups:
            text, kb = "Nessuna segnalazione da vedere. ✓", None
        else:
            tot = sum(g["n"] for g in groups)
            lines, rows, items = [f"🔔 <b>{tot} segnalazion{'e' if tot == 1 else 'i'} da vedere</b>"], [], []
            for g in groups[:10]:
                lines.append(f"\n<b>{esc(g['name'])}</b> — {g['n']}")
                for x in g["items"]:
                    items.append(x["id"])
                    lines.append(f"<b>{len(items)}.</b> {esc(core.clean_title(x['title']) or '(senza titolo)')} "
                                 f"<i>· {esc(x['feed'] or '')} · {core.fmt_dt(x['published_at'] or x['fetched_at'])}</i>")
                if g["n"] > len(g["items"]):
                    lines.append(f"<i>… e altre {g['n'] - len(g['items'])}</i>")
                rows.append([InlineKeyboardButton(f"✓ Viste: {g['name'][:30]} ({g['n']})", callback_data=f"nv:{g['id']}")])
            if len(groups) > 10:
                lines.append(f"\n<i>… e altre {len(groups) - 10} sentinelle con segnalazioni.</i>")
            btn = [InlineKeyboardButton(f"📄{k}", callback_data=f"i:{iid}") for k, iid in enumerate(items[:8], 1)]
            rows = [btn[i:i + 4] for i in range(0, len(btn), 4)] + rows
            rows.append([InlineKeyboardButton("✓ Segna tutto come visto", callback_data="nva")])
            text = "\n".join(lines)
            if len(text) > 4000:
                text = text[:3990].rsplit("\n", 1)[0] + "\n…"
            kb = InlineKeyboardMarkup(rows)
        if edit:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=kb)
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    raise
        else:
            await update.effective_chat.send_message(text, reply_markup=kb)

    @guarded("novita")
    async def cmd_novita(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        await show_novita(update, link, edit=False)

    @guarded("novita_viste")
    async def cb_n(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        data = update.callback_query.data
        n = await asyncio.to_thread(core.mark_seen, link["username"], None if data == "nva" else int(data[3:]))
        try:
            await update.callback_query.answer(f"{n} segnate come viste")
        except TelegramError:
            pass
        await show_novita(update, link, edit=True)

    async def watch_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Scadenze delle sentinelle: promemoria il giorno prima, poi spegnimento (una volta sola)."""
        async def deliver(owner: str, text: str, kb=None) -> bool:
            sent = False
            for chat in await asyncio.to_thread(core.chats_of, owner):
                try:
                    await context.bot.send_message(chat, text, reply_markup=kb)
                    sent = True
                except Forbidden:                  # bot bloccato: lo si annota e si passa oltre
                    await asyncio.to_thread(core.set_blocked, chat, True)
                except TelegramError as e:
                    log.warning("avviso di scadenza non consegnato: %s", type(e).__name__)
            return sent

        try:
            for r in await asyncio.to_thread(core.reminders_due):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Prolunga 7 giorni", callback_data=f"ae:{r['id']}:7")]])
                if await deliver(r["owner"], f"⏳ La sentinella <b>{esc(r['name'])}</b> scade il "
                                             f"{core.fmt_dt(r['expires_at'])}.", kb):
                    await asyncio.to_thread(core.mark_reminded, r["id"])
            for r in await asyncio.to_thread(core.expire_due):
                await deliver(r["owner"], f"⌛ La sentinella <b>{esc(r['name'])}</b> è scaduta ed è stata spenta. "
                                          "Puoi riaccenderla da /allerte o dal portale.")
        except Exception as e:
            log.warning("controllo scadenze non riuscito: %s", type(e).__name__)

    @guarded("sconosciuto")
    async def on_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        await reply(update, "Comando sconosciuto. /aiuto")

    # --------------------------------------------------- gruppi e blocchi

    async def on_my_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
        m = update.my_chat_member
        if m is None:
            return
        if m.chat.type != ChatType.PRIVATE:
            if m.new_chat_member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                log.info("aggiunto a una chat di gruppo: esco")
                try:
                    await context.bot.leave_chat(m.chat.id)
                except TelegramError:
                    pass
            return
        if m.from_user is None or m.from_user.id not in allowed:
            return
        blocked = m.new_chat_member.status == ChatMemberStatus.BANNED
        await asyncio.to_thread(core.set_blocked, m.from_user.id, blocked)
        log.info("bot %s dall'utente", "bloccato" if blocked else "sbloccato")

    async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat and chat.type != ChatType.PRIVATE:
            try:
                await context.bot.leave_chat(chat.id)
            except TelegramError:
                pass

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        if isinstance(err, Forbidden):
            log.info("Telegram: utente non raggiungibile (bot bloccato?)")
            return
        # niente messaggio ne' traccia completa: possono contenere testo dell'utente
        # (una query, un titolo), e /logs del bot di amministrazione li rimanda in chat
        where = ""
        if err is not None and err.__traceback__ is not None:
            fr = traceback.extract_tb(err.__traceback__)[-1]
            where = f" in {os.path.basename(fr.filename)}:{fr.lineno} ({fr.name})"
        log.error("errore non gestito: %s%s", type(err).__name__, where)

    # ------------------------------------------------------------ avvio

    async def post_init(app: Application) -> None:
        # Solo /start nel menu predefinito; i comandi completi solo nelle
        # chat collegate. Gli errori di rete qui non devono fermare il bot.
        try:
            await app.bot.set_my_commands([BotCommand("start", "collega il tuo account")],
                                          scope=BotCommandScopeDefault())
        except TelegramError as e:
            log.warning("menu predefinito non impostato: %s", type(e).__name__)
        links = await asyncio.to_thread(core.active_links)
        for l in links:
            try:
                await set_chat_commands(app.bot, l["chat_id"])
            except TelegramError as e:
                log.warning("menu di una chat non impostato: %s", type(e).__name__)
        log.info("avviato (%s): %d account collegati", VERSION, len(links))
        app.job_queue.run_repeating(token_watchdog, interval=WATCHDOG_EVERY, first=WATCHDOG_EVERY,
                                    name="token_watchdog")
        app.job_queue.run_repeating(watch_jobs, interval=WATCH_JOBS_EVERY, first=min(30.0, WATCH_JOBS_EVERY),
                                    name="watch_jobs")

    async def token_watchdog(context: ContextTypes.DEFAULT_TYPE) -> None:
        # Un token revocato a bot avviato non arriva a run_polling: la libreria
        # ferma il polling in un task di sfondo e il processo resterebbe vivo
        # ma sordo, senza codice 78 ne' avviso. Lo si controlla da qui.
        try:
            await context.bot.get_me()
        except InvalidToken:
            log.error("Telegram ha rifiutato il token durante il funzionamento")
            token_rejected.set()
            context.job.schedule_removal()
            context.application.stop_running()
        except TelegramError:
            pass                # rete o Telegram: ci pensa il polling a ritentare

    builder = Application.builder().token(token)
    api_base = os.environ.get("RSSINTEL_BOT_API_BASE")      # solo per i test con un finto server
    if api_base:
        builder = builder.base_url(api_base.rstrip("/") + "/bot").base_file_url(api_base.rstrip("/") + "/file/bot")
    app = (builder
           .defaults(Defaults(parse_mode=ParseMode.HTML,
                              link_preview_options=LinkPreviewOptions(is_disabled=True)))
           .post_init(post_init)
           .concurrent_updates(4)            # una ricerca lenta non deve bloccare il resto
           .build())
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler(["aiuto", "help"], cmd_aiuto, filters=private))
    app.add_handler(CommandHandler("stato", cmd_stato, filters=private))
    app.add_handler(CommandHandler("esci", cmd_esci, filters=private))
    app.add_handler(CommandHandler("cerca", cmd_cerca, filters=private))
    app.add_handler(CallbackQueryHandler(cb_esci, pattern=r"^esci:(si|no)$"))
    app.add_handler(CallbackQueryHandler(cb_page, pattern=RE_PAGE))
    app.add_handler(CallbackQueryHandler(cb_cat, pattern=RE_CAT))
    app.add_handler(CallbackQueryHandler(cb_item, pattern=RE_ITEM))
    app.add_handler(CallbackQueryHandler(cb_text, pattern=RE_TEXT))
    app.add_handler(CommandHandler("segui", cmd_segui, filters=private))
    app.add_handler(CommandHandler("allerte", cmd_allerte, filters=private))
    app.add_handler(CommandHandler("novita", cmd_novita, filters=private))
    app.add_handler(CallbackQueryHandler(cb_sw, pattern=RE_SW))
    app.add_handler(CallbackQueryHandler(cb_g, pattern=RE_G))
    app.add_handler(CallbackQueryHandler(cb_w, pattern=RE_W))
    app.add_handler(CallbackQueryHandler(cb_a, pattern=RE_A))
    app.add_handler(CallbackQueryHandler(cb_n, pattern=RE_N))
    app.add_handler(CallbackQueryHandler(cb_noop))           # "-" e qualunque dato non riconosciuto
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_handler(MessageHandler(filters.COMMAND & private, on_unknown))
    app.add_handler(MessageHandler(~private, on_group_message))
    app.add_handler(ChatMemberHandler(on_my_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)

    token_rejected = threading.Event()
    try:
        app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"],
                        drop_pending_updates=True, bootstrap_retries=-1)
    except InvalidToken:
        token_rejected.set()
    if token_rejected.is_set():
        log.error("Telegram ha rifiutato il token: servizio fermato (rigenera il token e la credenziale)")
        return EXIT_CONFIG
    return 0


def main() -> int:
    setup_logging()
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    return run_bot()


if __name__ == "__main__":
    sys.exit(main())
