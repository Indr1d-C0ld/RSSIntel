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

VERSION = "L2"
EXIT_CONFIG = 78                    # EX_CONFIG: la unit non riavvia
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
RATE_PER_MIN = int(os.environ.get("RSSINTEL_BOT_RATE_PER_MIN", "30"))   # comandi e bottoni al minuto
WATCHDOG_EVERY = float(os.environ.get("RSSINTEL_BOT_WATCHDOG_S", "300"))   # controllo del token
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
                    "<code>\"valle del Sacco\"</code>, <code>Hezbollah*</code>, <code>title:gaza</code>. "
                    "Se una ricerca non è valida la reinterpreto e te lo dico.\n\n"
                    "/stato — ultimo giro di raccolta e stato del bot\n"
                    "/esci — scollega questo account\n\n"
                    "<i>In arrivo: sentinelle con notifiche, bollettino del giorno.</i>")

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
        rows.append([InlineKeyboardButton("🗂 Categoria", callback_data=f"sc:{tok}")])
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
