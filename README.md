# RSSIntel

Aggregatore e archivio OSINT self-hosted. Recupera feed RSS/Atom a intervalli
regolari, ne estrae il testo completo e lo indicizza per la ricerca full-text.
Sopra l'archivio: un **bollettino** quotidiano ordinato per tipo di fonte,
**allerte** sulle ricerche salvate, **classificazione delle fonti** con il
codice dell'Ammiragliato, annotazioni e tag, favoriti con note, **catture
visive** a pagina intera della pagina originale. Multi-utente con ruoli.
Pensato per macchine piccole: raccolta a intervalli lunghi e a bassa priorità,
poche scritture su disco (vedi «Carico e scritture»).

Stack: **PHP 8.1+** e **SQLite** per la webapp, **Python 3.11+** per fetcher e
worker. Nessun framework, nessun database server, nessuna build.

## Componenti

| Percorso | Ruolo |
|---|---|
| `webapp/bollettino.php` | pagina d'ingresso: la giornata per tipo di fonte, allerte, salute dei feed |
| `webapp/novita.php` | corrispondenze delle allerte, da segnare come viste |
| `webapp/search.php`, `browse.php`, `item.php` | ricerca FTS, lettura cronologica, dettaglio articolo (filtri per categoria di fonte) |
| `webapp/feeds.php` | gestione feed, classificazione e grado di affidabilità, import/export |
| `webapp/favorites.php`, `notes.php`, `capture.php` | favoriti con note, annotazioni, catture visive |
| `webapp/stats.php`, `accessi.php`, `users.php`, `theme.php` | statistiche, log accessi, utenti e ruoli, tema grafico |
| `fetcher/rssintel_fetch.py` | scarica i feed abilitati, estrae il testo con `trafilatura`, popola DB e indice FTS5 |
| `fetcher/rssintel_watch.py` | allerte: riesegue le ricerche salvate sui nuovi articoli dopo ogni raccolta |
| `fetcher/rssintel_capture.py`, `cdp.py`, `shot.py` | catture visive via Chromium headless |
| `fetcher/rssintel_rebuild_fts.py` | ricostruzione integrale dell'indice FTS dai testi estratti |
| `bot/rssintel_bot.py`, `rssintel_core.py` | bot Telegram (in costruzione a lotti, vedi sotto) |
| `schema.sql` | schema completo del database SQLite |
| `deploy/` | `.htaccess`, header di sicurezza, unit `systemd`, migrazioni |
| `config.sample.php` | modello di configurazione |

## Schema dati

- `feeds` — sorgenti RSS/Atom, stato dell'ultimo fetch, categoria e affidabilità della fonte
- `items` — articoli: metadati + percorso del testo estratto (`text_path`) + `content_hash`
- `items_fts` — indice FTS5 autonomo (`title`, `body`, `link`, `feed`): conserva il testo, cosi' `snippet()` produce gli estratti evidenziati
- `annotations` / `tags` / `annotation_tags` — note e tag per articolo
- `users` — utenti e ruoli (`reader`, `collaborator`, `admin`)
- `saved_searches` / `watch_hits` — ricerche salvate, allerte e loro corrispondenze
- `favorites` — favoriti per utente, con nota personale
- `captures` — catture visive (i file stanno fuori dalla webroot)
- `access_log` / `login_attempts` / `ip_geo_cache` — log accessi e tentativi di login
- `site_settings` — impostazioni globali (tema)
- `tg_links` / `tg_link_codes` / `tg_watch_prefs` — account Telegram collegati, codici di collegamento, preferenze delle sentinelle (le crea il bot)

## Installazione

```bash
git clone https://github.com/Indr1d-C0ld/RSSIntel.git
cd RSSIntel

# 1. Configurazione
cp config.sample.php config.php
$EDITOR config.php          # percorso DB, conservazione del log accessi

# 2. Database
mkdir -p /var/lib/rssintel/text
sqlite3 /var/lib/rssintel/rssintel.db < schema.sql

# 3. Fetcher
python3 -m venv /opt/rssintel-venv
/opt/rssintel-venv/bin/pip install -r fetcher/requirements.txt

# 4. Webapp: servi la cartella webapp/ con PHP (Apache/nginx/php -S) avendo cura
#    che config.php sia raggiungibile un livello sopra la document root, oppure
#    copialo dentro webapp/. Proteggi l'accesso (vedi deploy/htaccess.sample).
```

### Fetch periodico

Adatta e installa le unit di esempio (il timer raccoglie ogni 3 ore, a
priorità minima di CPU e disco):

```bash
cp deploy/rssintel-fetch.service.sample /etc/systemd/system/rssintel-fetch.service
cp deploy/rssintel-fetch.timer.sample   /etc/systemd/system/rssintel-fetch.timer
systemctl enable --now rssintel-fetch.timer
```

Il fetcher legge i percorsi da variabili d'ambiente (`RSSINTEL_DB`,
`RSSINTEL_TXT_DIR`, `RSSINTEL_RAW_DIR`), con `/var/lib/rssintel` come
predefinito, e l'user-agent da `RSSINTEL_UA`: mettici l'indirizzo del tuo sito.
Tre ore bastano per la maggior parte dei feed, che tengono gli articoli
dell'ultimo giorno o più; per un feed che ne tiene solo poche ore conviene un
intervallo più breve, altrimenti qualche articolo esce dal feed prima del giro.

## Bollettino, allerte e fonti

**Bollettino** (`bollettino.php`, pagina d'ingresso). La giornata in una pagina,
ordinata per tipo di fonte invece che per ora: in testa le corrispondenze delle
allerte, poi gli articoli delle fonti specialistiche, istituzionali e d'analisi,
in fondo e compressi il flusso generalista e le fonti non classificate. In coda la
salute dei feed: errori, feed non aggiornati da ore e feed **silenziosi rispetto
al proprio ritmo** — si segnala una settimana senza articoli solo quando, al ritmo
abituale della fonte (stimato sui 23 giorni precedenti), avrebbe avuto meno del 5%
di probabilita'. Un feed che risponde sempre `304` senza errori, ma ha smesso di
pubblicare, altrimenti non lo segnalerebbe nessuno.

**Allerte** (`novita.php`). Una ricerca salvata con l'allerta attiva viene
rieseguita dopo ogni raccolta sui soli articoli nuovi
(`fetcher/rssintel_watch.py`, avviato da `ExecStartPost` nella unit del fetcher:
vedi `deploy/rssintel-fetch.service.sample`). Le corrispondenze compaiono in
«Novità», con un contatore nella navigazione; «visto» e' un comando esplicito.
All'attivazione non si carica lo storico. Il riferimento d'avanzamento e'
`items.fetched_at`, aggiornato anche quando un articolo viene reindicizzato.

**Classificazione delle fonti** (`feeds.php`). Per ogni feed: categoria (tipo di
testata: generalista, specialistica, istituzionale, analisi, blog, advocacy — non
l'orientamento) e affidabilita' secondo il **codice dell'Ammiragliato** (A–F), con
una nota di motivazione. La lettera valuta la *fonte*; la cifra 1–6 del codice,
che valuta la singola informazione, non appartiene al feed e non e' gestita qui.
Categoria e grado compaiono come distintivo («B · specialistica») e sono filtri in
Lettura e Ricerca.

Aggiornando un'installazione esistente vedi `deploy/migrations/`.

## Catture visive

Da un articolo (o dai Favoriti) un utente `collaborator`/`admin` può chiedere
una **cattura a pagina intera** della pagina originale: un JPEG (altezza massima
12.000 px, di solito qualche centinaio di KB) con miniatura,
impronta SHA-256, URL finale dopo i redirect e data. Le catture sono versionate
(ricatturare aggiunge, non sostituisce) ed eliminabili da chi le ha richieste o
da un admin.

- La pagina **accoda** soltanto e crea un file-segnale
  (`<captures_dir>/.richiesta`). Un'unità `.path` di systemd lo vede (inotify,
  nessun polling) e avvia `fetcher/rssintel_capture.py`, che svuota la coda,
  una cattura alla volta (`deploy/rssintel-capture.{service,path}.sample`).
  Senza richieste Chromium non parte mai.
- Prima dello scatto si nascondono i banner di consenso più diffusi
  (OneTrust, Didomi, Usercentrics, Quantcast, Cookiebot, Google Funding
  Choices, Sourcepoint, iubenda…).
- Il lavoro passa dallo stesso filtro anti-SSRF del fetcher: solo http/https e
  solo indirizzi pubblici, redirect rivalidati a ogni salto.
- `chromium --screenshot` cattura solo il viewport: la pagina intera si ottiene
  via protocollo DevTools (`Page.captureScreenshot` con `captureBeyondViewport`),
  con un client minimo in `fetcher/cdp.py` (solo libreria standard).
- I file stanno **fuori dalla webroot** (`captures_dir`, default
  `<cartella del db>/captures`) e li serve `webapp/capture.php` dopo
  l'autenticazione.

Requisiti: `chromium` e Pillow nel Python che esegue il worker.

## Bot Telegram

`bot/rssintel_bot.py` porta RSSIntel sul telefono senza passare dal portale:
un servizio Python separato (`python-telegram-bot` 22.8, long polling, nessuna
porta in ingresso) che legge e scrive lo stesso database. Lo si costruisce a
lotti; questa versione (L5) collega l'account, cerca, fa leggere, gestisce le
sentinelle e ne manda le notifiche, e porta il bollettino del giorno.

- **Ricerca.** Si scrive una parola, oppure `/cerca`.
  - I risultati sono **gli stessi di `search.php`**: stessa query FTS, stesso
    ordine per data, stesso filtro per categoria. Lo verifica un confronto con
    il portale.
  - 6 risultati per pagina, con fonte, grado, data, dominio ed estratto
    evidenziato.
  - Se FTS rifiuta la query (`l'Iran`, `Al-Aqsa`, `iran-israel`), il bot la
    riprova con le parole tra virgolette e dice come l'ha interpretata.
  - Gli estratti si calcolano solo per la pagina mostrata, e una query troppo
    larga si interrompe dopo 8 secondi.
- **Lettura.** Ogni risultato apre una scheda: estratto, «Apri fonte», «Link»
  da copiare e il testo completo, in messaggi da circa 3.500 caratteri oppure
  come file `.txt` se è lungo.
- **Sentinelle.** `/segui parole` crea un'allerta sui nuovi articoli.
  - Prima di crearla mostra quanti ne avrebbe trovati negli ultimi 14 giorni,
    con la stessa condizione del worker, e suggerisce la modalita':
    istantanea, riepilogo oppure solo Novità.
  - Si puo' dare una scadenza, utile per seguire un evento.
  - E' la stessa ricerca salvata del portale: compare con 🔔 su `search.php` e
    la riesegue `rssintel_watch.py`.
  - `/allerte` le gestisce; `/novita` mostra le segnalazioni non viste. «Visto»
    e' la stessa colonna di `novita.php`.
  - Le sentinelle da proporre al primo collegamento si leggono da un file
    indicato da `RSSINTEL_BOT_SUGGEST_FILE`, una per riga:
    `Nome | query | istantanea|riepilogo|novita`.
- **Notifiche.**
  - ⚡ Istantanee: un messaggio per giro di raccolta. Al massimo un suono
    all'ora e otto al giorno; le altre arrivano silenziose.
  - 📅 Riepiloghi silenziosi agli orari di `RSSINTEL_BOT_DIGEST_AT` (predefiniti
    08:00, 13:00, 19:00).
  - Niente invii nelle ore di `RSSINTEL_BOT_QUIET` (02:00-08:00).
  - Cio' che e' gia' stato visto sul portale non viene mandato.
  - La coda sta nel database di stato del bot, non in quello condiviso; la
    consegna e' «almeno una volta», con i reinvii marcati.
  - Per ogni sentinella si puo' scegliere «solo conteggi»: niente nomi ne'
    titoli in chat.
- **Bollettino.** `/oggi` (oppure `ieri`, o una data) mostra la giornata come
  `bollettino.php`: stesse sezioni, stessi limiti e stessa salute delle fonti,
  con allerte e sezioni da sfogliare.
  - Facoltativo: il bollettino di ieri ogni mattina, in silenzio (orario in
    `RSSINTEL_BOT_BULLETIN_AT`).
  - Gli amministratori ricevono un avviso quando una fonte va in errore, smette
    di aggiornarsi o di pubblicare, e quando torna regolare.
- **Bottoni.** Portano solo un gettone corto: la query vera resta nel database
  di stato del bot, legata all'account e valida 48 ore.

- **Segreti.** Token e ID Telegram autorizzati arrivano come credenziali
  `systemd` (`LoadCredentialEncrypted=`, `LoadCredential=`), mai da variabili
  d'ambiente né da `config.php`. La libreria scrive il token in chiaro nel
  messaggio d'errore quando Telegram lo rifiuta: il bot lo maschera sul testo
  finale di ogni riga di log, tracce comprese, ed esce con codice 78, che la unit
  non riavvia.
- **Accesso.** Solo gli ID in lista bianca ricevono risposta; gli altri non
  causano nemmeno un accesso al database. L'account si collega a un utente
  RSSIntel con un codice monouso (15 minuti, conservato solo come impronta);
  utente, ruolo e disattivazione si rileggono dal database a ogni comando. Solo
  chat private: aggiunto a un gruppo, il bot esce. Ogni utente può scollegare il
  proprio account dal Profilo.
- **Database condiviso.** Il bot gira con un utente proprio nel gruppo del
  server web. La cartella del database deve essere **setgid e scrivibile dal
  gruppo (2775)** e il database 664, così chi apre per primo crea `-wal`/`-shm`
  utilizzabili da tutti. **Niente sticky bit**: con `fs.protected_regular=2`
  (predefinito su Debian) il kernel impedirebbe al server web di aprire i file
  creati dal bot, e il database gli diventerebbe di sola lettura. Connessione
  per operazione, nessuna transazione di lettura lasciata aperta.

```bash
useradd --system --gid www-data --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin rssintel-bot
chmod 2775 /var/lib/rssintel && chmod 664 /var/lib/rssintel/rssintel.db
install -d -o root -g root -m 755 /opt/rssintel-bot      # codice di root: non modificabile dal server web
install -m 644 bot/rssintel_bot.py bot/rssintel_core.py bot/requirements.txt /opt/rssintel-bot/
python3 -m venv /opt/rssintel-bot/venv
/opt/rssintel-bot/venv/bin/pip install --require-hashes -r /opt/rssintel-bot/requirements.txt
systemd-ask-password -n "Token:" | \
  systemd-creds encrypt --with-key=host --name=bot_token - /etc/credstore.encrypted/rssintel-bot.token
(umask 077; echo "<ID Telegram>" > /etc/credstore/rssintel-bot.allowed)
cp deploy/rssintel-bot.service.sample /etc/systemd/system/rssintel-bot.service
systemctl daemon-reload && systemctl enable --now rssintel-bot
# collegamento: codice monouso, poi /start CODICE al bot
sudo -u rssintel-bot env RSSINTEL_DB=/var/lib/rssintel/rssintel.db \
  /opt/rssintel-bot/venv/bin/python /opt/rssintel-bot/rssintel_bot.py codice <utente>
```

La unit di esempio è blindata (`ProtectSystem=strict`, solo la cartella del
database scrivibile, testi e catture in sola lettura). Il gruppo del server web
può leggere anche file di altri siti: elencali in un'estensione della unit
(`InaccessiblePaths=`) e in `RSSINTEL_BOT_HIDDEN`, che il bot verifica all'avvio.

## Configurazione (`config.php`)

| Chiave | Significato |
|---|---|
| `db_path` | percorso assoluto del file SQLite |
| `access_log_retention_days` | giorni di conservazione del log accessi (0 = per sempre) |
| `ipgeo` | risolve il paese degli IP del log accessi via ip-api.com |
| `captures_dir`, `text_dir` | cartelle delle catture e dei testi, se non accanto al DB |
| `admins` | legacy, non più necessario con la tabella `users` |

## Carico e scritture

RSSIntel gira bene anche su una macchina piccola con un solo SSD:

- **Raccolta** ogni 3 ore, con `Nice=19` e `IOSchedulingClass=idle`; un giro
  di una decina di feed dura pochi secondi se nessuno ha novità.
- **Stato dei feed** (ultimo controllo, esito, ETag) scritto in una sola
  transazione a fine giro; un feed che risponde `304` non scrive nulla da sé.
  Un solo checkpoint del WAL per giro.
- **Indice di ricerca**: ogni articolo nuovo vi entra una volta sola, già con
  il testo (nessun trigger che inserisce una riga vuota da riscrivere).
- **Testi** compressi (`.txt.gz`), uno per articolo; l'HTML grezzo non si
  salva (`SAVE_RAW_HTML = False`).
- **Catture** solo su richiesta, in JPEG; nessun timer.
- **Log accessi**: niente righe per le miniature e i controlli automatici
  delle catture; conservazione impostabile (`access_log_retention_days`).

La traduzione automatica (proxy verso un motore locale tipo NLLB) è stata
tolta: un modello in locale pesa troppo per questo tipo di macchina.

## Licenza

GPL-3.0-or-later — vedi [`LICENSE`](LICENSE).
