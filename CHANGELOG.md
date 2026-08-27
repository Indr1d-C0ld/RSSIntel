# Changelog

## 2026-08-27 — Fix sicurezza (2)

- `webapp/search.php`: escaping difensivo dell'output di `snippet()`. La query
  usa ora i delimitatori `char(2)`/`char(3)` invece di `<mark>`/`</mark>`;
  l'output passa da `h()` completo e solo dopo i delimitatori diventano tag
  `<mark>` reali. Nota: la tabella FTS e' `content=''` (contentless), quindi
  `snippet()` restituisce sempre stringa vuota e il blocco non renderizza —
  il fix mette in sicurezza il punto se in futuro si abilita la conservazione
  del testo nell'indice.

## 2026-08-27 — Fix sicurezza

- `webapp/browse.php`: `$date_mode` (da `$_GET['date']`) non era validato e
  veniva interpolato grezzo negli attributi `href` dei link di paginazione →
  XSS riflesso. Aggiunta whitelist `{day, week, month}` subito dopo la lettura
  del parametro; tutti gli usi a valle (chiave del `match`, confronti nelle
  `<option>`, href) sono ora su valori sicuri.

## 2026-08-27 — Primo rilascio pubblico

Versione neutra derivata dal deployment live, ripulita da dati e configurazioni.

- `webapp/lib.php`: configurazione spostata da valori hardcoded a `config.php`
  (funzione `cfg()`); nuovo `config.sample.php` con `db_path`, `translate_url`,
  `translate_max_chars`, `admins`.
- `webapp/translate.php`: endpoint di traduzione letto da `config.php`
  (`translate_url`); aggiunto tetto alla lunghezza del testo (`translate_max_chars`,
  HTTP 413 oltre soglia).
- `fetcher/rssintel_fetch.py`, `fetcher/rssintel_rebuild_fts.py`: percorsi e
  User-Agent letti da variabili d'ambiente (`RSSINTEL_DB`, `RSSINTEL_RAW_DIR`,
  `RSSINTEL_TXT_DIR`, `RSSINTEL_UA`) con default generici.
- `webapp/assets/style.css`: sostituito il tema personale con uno stylesheet
  neutro (light/dark), stesse classi dei template.
- `fetcher/requirements.txt`: dipendenze dirette del fetcher.
- `deploy/`: esempi di `.htaccess`, unit `systemd` e timer.
- Aggiunti `README.md`, `LICENSE` (GPL-3.0-or-later), `.gitignore`.
- Esclusi: database, testi estratti, dump HTML grezzi, log, feed, annotazioni,
  tag, `config.php`, `.htaccess` reale, `item.php_notrad`.
