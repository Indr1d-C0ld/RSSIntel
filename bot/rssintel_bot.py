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
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rssintel_core as core  # noqa: E402

VERSION = "L1"
EXIT_CONFIG = 78                    # EX_CONFIG: la unit non riavvia
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
RATE_PER_MIN = 20
WATCHDOG_EVERY = float(os.environ.get("RSSINTEL_BOT_WATCHDOG_S", "300"))   # controllo del token
LINK_FAILS_MAX = 5                  # tentativi di codice sbagliati ...
LINK_FAILS_WINDOW = 15 * 60         # ... in 15 minuti

log = logging.getLogger("rssintel-bot")


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
    from telegram import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
                          InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update)
    from telegram.constants import ChatMemberStatus, ChatType, ParseMode
    from telegram.error import Forbidden, InvalidToken, TelegramError
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
                        if not q:
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
            await reply(update, f"✅ Collegato a RSSIntel come <b>{html.escape(username)}</b>.\n\n"
                                "Scrivi /aiuto per vedere cosa posso fare.")
            return
        if link:
            await reply(update, f"Sei collegato come <b>{html.escape(link['username'])}</b>. /aiuto")
        else:
            await reply(update, "Per collegare questo account a RSSIntel genera un codice sul server:\n"
                                "<code>sudo -u rssintel-bot env RSSINTEL_DB=… "
                                "/opt/rssintel-bot/venv/bin/python /opt/rssintel-bot/rssintel_bot.py codice UTENTE</code>\n"
                                "poi invia qui <code>/start CODICE</code>.")

    @guarded("aiuto")
    async def cmd_aiuto(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        await reply(update,
                    "<b>RSSIntel</b> — comandi disponibili\n\n"
                    "/stato — ultimo giro di raccolta e stato del bot\n"
                    "/esci — scollega questo account\n"
                    "/aiuto — questo messaggio\n\n"
                    "<i>In arrivo: ricerca scrivendo una parola, scheda e testo degli articoli, "
                    "sentinelle con notifiche, bollettino del giorno.</i>")

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
            lines.append("Permessi del database: " + ("ok" if not probs else "⚠️ " + html.escape("; ".join(probs))))
            if sandbox:
                bad = [d for d, ok in sandbox if not ok]
                lines.append("Sandbox: " + ("ok" if not bad else "⚠️ " + html.escape(", ".join(bad))))
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

    @guarded("testo")
    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE, link):
        await reply(update, "La ricerca scrivendo una parola arriva con il prossimo aggiornamento. /aiuto")

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
        log.error("errore non gestito: %s", type(err).__name__, exc_info=err)

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
           .build())
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler(["aiuto", "help"], cmd_aiuto, filters=private))
    app.add_handler(CommandHandler("stato", cmd_stato, filters=private))
    app.add_handler(CommandHandler("esci", cmd_esci, filters=private))
    app.add_handler(CallbackQueryHandler(cb_esci, pattern=r"^esci:(si|no)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
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
