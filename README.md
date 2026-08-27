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
