# RSSIntel

Aggregatore e archivio OSINT self-hosted. Recupera feed RSS/Atom a intervalli
regolari, ne estrae il testo completo e lo indicizza per la ricerca full-text.
Sopra l'archivio: un **bollettino** quotidiano ordinato per tipo di fonte,
**allerte** sulle ricerche salvate, **classificazione delle fonti** con il
codice dell'Ammiragliato, annotazioni e tag, favoriti con note, **catture
visive** a pagina intera della pagina originale, e un ponte verso un servizio
di traduzione (NLLB / LibreTranslate compatibile). Multi-utente con ruoli.

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

## Installazione

```bash
git clone https://github.com/Indr1d-C0ld/RSSIntel.git
cd RSSIntel

# 1. Configurazione
cp config.sample.php config.php
$EDITOR config.php          # percorso DB, endpoint traduzione, utenti admin

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

Adatta e installa le unit di esempio:

```bash
cp deploy/rssintel-fetch.service.sample /etc/systemd/system/rssintel-fetch.service
cp deploy/rssintel-fetch.timer.sample   /etc/systemd/system/rssintel-fetch.timer
systemctl enable --now rssintel-fetch.timer
```

Il fetcher legge i percorsi anche da variabili d'ambiente
(`RSSINTEL_DB`, `RSSINTEL_TXT_DIR`, `RSSINTEL_RAW_DIR`, `RSSINTEL_UA`).

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
una **cattura a pagina intera** della pagina originale: un PNG con miniatura,
impronta SHA-256, URL finale dopo i redirect e data. Le catture sono versionate
(ricatturare aggiunge, non sostituisce) ed eliminabili da chi le ha richieste o
da un admin.

- La pagina **accoda** soltanto: il rendering (10-30 s di Chromium) lo esegue
  `fetcher/rssintel_capture.py`, avviato da un timer systemd
  (`deploy/rssintel-capture.*.sample`), una cattura alla volta.
- Il lavoro passa dallo stesso filtro anti-SSRF del fetcher: solo http/https e
  solo indirizzi pubblici, redirect rivalidati a ogni salto.
- `chromium --screenshot` cattura solo il viewport: la pagina intera si ottiene
  via protocollo DevTools (`Page.captureScreenshot` con `captureBeyondViewport`),
  con un client minimo in `fetcher/cdp.py` (solo libreria standard).
- I file stanno **fuori dalla webroot** (`captures_dir`, default
  `<cartella del db>/captures`) e li serve `webapp/capture.php` dopo
  l'autenticazione.

Requisiti: `chromium` e Pillow nel Python che esegue il worker.

## Configurazione (`config.php`)

| Chiave | Significato |
|---|---|
| `db_path` | percorso assoluto del file SQLite |
| `translate_url` | endpoint del servizio di traduzione (`POST {q,source,target}` → `{translatedText}`) |
| `translate_max_chars` | tetto in byte al testo inviato a `translate_url` |
| `admins` | valori di `REMOTE_USER` abilitati alla gestione dei feed |

## Traduzione

`webapp/translate.php` è un semplice proxy verso `translate_url`. Qualunque
servizio che rispetti il contratto `POST {q, source, target}` → `{translatedText}`
va bene (LibreTranslate, o un wrapper HTTP attorno a un modello NLLB).

## Licenza

GPL-3.0-or-later — vedi [`LICENSE`](LICENSE).
