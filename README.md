# RSSIntel

Aggregatore e archivio OSINT self-hosted. Recupera feed RSS/Atom a intervalli
regolari, ne estrae il testo completo, lo indicizza per la ricerca full-text e
permette di annotare i singoli articoli con note e tag. Include un ponte verso
un servizio di traduzione (NLLB / LibreTranslate compatibile).

Stack: **PHP 8.1+** e **SQLite** per la webapp, **Python 3.11+** per il fetcher.
Nessun framework, nessun database server, nessuna build.

## Componenti

| Percorso | Ruolo |
|---|---|
| `webapp/` | interfaccia web: ricerca, lettura cronologica, dettaglio articolo, annotazioni, gestione feed |
| `fetcher/rssintel_fetch.py` | scarica i feed abilitati, estrae il testo con `trafilatura`, popola DB e indice FTS5 |
| `fetcher/rssintel_rebuild_fts.py` | ricostruzione integrale dell'indice FTS dai testi estratti |
| `schema.sql` | schema del database SQLite |
| `deploy/` | esempi di `.htaccess`, unit `systemd` e timer |
| `config.sample.php` | modello di configurazione |

## Schema dati

- `feeds` — sorgenti RSS/Atom, con stato dell'ultimo fetch
- `items` — articoli: metadati + percorso del testo estratto (`text_path`) + `content_hash`
- `items_fts` — indice FTS5 (`title`, `body`, `link`, `feed`), contenuto esterno
- `annotations` / `tags` / `annotation_tags` — note e tag per articolo

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
