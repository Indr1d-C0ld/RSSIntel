# Changelog

## 2026-08-27 — Multi-utente e ruoli

- Autenticazione applicativa (niente piu' Basic Auth). Nuova tabella
  `users(username, password_hash, role, disabled, created_by, created_at,
  last_login_at)`, ruoli **reader** / **collaborator** / **admin**.
- `webapp/lib.php`: sessione + CSRF centralizzati (cookie HttpOnly, SameSite=Lax);
  helper `auth_user()`, `require_login()`, `require_role()`, `current_user()`
  (ora dalla sessione), `current_role()`, `can_annotate()`, `csrf_token()`,
  `csrf_check()`. `is_admin()` ora conta il ruolo di sessione.
- Nuovi: `webapp/nav.php` (`render_header()` condiviso — elimina l'header
  duplicato in 5 pagine), `webapp/login.php` (+ creazione del primo
  amministratore quando la tabella e' vuota), `webapp/logout.php`,
  `webapp/users.php` (admin: crea / cambia ruolo / reset password / disabilita /
  elimina; non ci si puo' auto-declassare ne' lasciare zero admin attivi),
  `webapp/profile.php` (cambio password proprio, min 8).
- `webapp/browse.php` `notes.php` `search.php` `item.php`: `require_login()` +
  `render_header()`. `item.php` nasconde il form annotazioni e i pulsanti
  Elimina a chi non puo' annotare.
- `webapp/feeds.php`: gate `require_role('admin')`.
- `webapp/annotations.php`: 401 se non autenticato, 403 se ruolo `reader`.
- `schema.sql`: tabella `users` (creata anche a runtime da login.php/users.php).
- `.htaccess` / `deploy/htaccess.sample`: rimossa la Basic Auth; negato
  l'accesso HTTP diretto a `config.php` / `*.db` / `*.sql`; aggiunti header
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`.

## 2026-08-27 — Ricerche salvate

- `webapp/search.php`: sessione + CSRF; handler POST `save`/`delete` con pattern PRG; riquadro "Ricerche salvate" per-utente in cima; form "Salva questa ricerca" accanto al conteggio risultati. La tabella si auto-crea al primo salvataggio (`CREATE TABLE IF NOT EXISTS` sul path di scrittura); il path di lettura e' protetto da un controllo su `sqlite_master`.
- `schema.sql`: nuova tabella `saved_searches(owner, name, q, feed_id, result_limit, created_at)` con `UNIQUE(owner, name)` + indice `idx_saved_searches_owner`.
- Delete vincolato a `WHERE id=:id AND owner=:me`: non si possono eliminare ricerche di altri utenti.

## 2026-08-27 — Traduzione: solo selezione, con limite visibile

- `webapp/item.php`: rimosso il pulsante "Traduci tutto" e la sua logica (`window.ITEM_TEXT`, `btnAll`) — con un motore a limite di token la traduzione integrale non e' mai affidabile. Riscritto il blocco traduzione:
  - contatore live della selezione (`N parole · C/limite caratteri`), rosso oltre soglia;
  - anteprima della selezione con la coda eccedente il limite in rosso barrato (non inviata al motore);
  - al clic invia solo la parte entro il limite e avvisa del troncamento;
  - nota fissa: motore solo EN→IT, ~N parole/M caratteri per volta;
  - la selezione conta solo se dentro `#article-text`, catturata al `mousedown`.
- `webapp/translate.php`: rete di sicurezza — tronca `q` a `translate_soft_limit` lato server prima di chiamare il motore.
- `config.sample.php`: nuovo campo `translate_soft_limit` (default 2000 caratteri, ~350 parole) documentato; `translate_max_chars` ridefinito come tetto rigido (413).

## 2026-08-27 — Keyword extraction: stoplist EN+IT

- `webapp/stopwords.php` (nuovo): stoplist di ~1050 voci uniche (inglese + italiano) — articoli, preposizioni semplici e articolate, pronomi, congiunzioni, ausiliari/modali, avverbi di discorso, giorni/mesi, boilerplate web. Nessuna parola di contenuto.
- `webapp/item.php`: `stopwords()` ora carica `stopwords.php` (memoizzato, normalizzato a minuscolo); `extract_keywords()` scarta le parole con una sola occorrenza (hapax), con ripiego all'elenco completo sui testi brevi. La sezione "Parole piu' frequenti" non mostra piu' particelle grammaticali.

## 2026-08-27 — browse.php: item senza published_at

- `webapp/browse.php`: filtro e ordinamento della vista cronologica passano da
  `i.published_at` a `COALESCE(i.published_at, i.fetched_at)`. Gli item il cui
  feed non espone la data di pubblicazione non vengono piu' esclusi dalla vista
  (usano `fetched_at` come ripiego, coerente col template che gia' mostra
  `published_at ?: fetched_at`). Nessun effetto sul dataset attuale (0 item con
  `published_at` NULL); modifica difensiva.

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
