# Changelog

## 2026-10-08 (2) — Fase 1: bollettino, allerte, classificazione delle fonti

Prima fase del piano nato dalla revisione dell'08/10: i dati d'uso mostravano una
raccolta solida (~190 articoli al giorno) e una consultazione quasi nulla (un
articolo aperto in cinque settimane). Direzione: e' la piattaforma a portare
all'analista cio' che conta, invece di aspettare che lo cerchi.

### Allerte sulle ricerche salvate
- `fetcher/rssintel_watch.py` (nuovo): riesegue le ricerche salvate con allerta
  attiva sui soli articoli indicizzati dopo l'ultimo controllo. Avviato da
  `ExecStartPost` nella unit del fetcher, cosi' le corrispondenze compaiono
  subito dopo ogni raccolta. Solo libreria standard. Riferimento d'avanzamento:
  `items.fetched_at` (aggiornato anche quando un articolo viene reindicizzato),
  con 10 minuti di sovrapposizione e UNIQUE contro i doppioni; letture fuori
  transazione, scritture in transazioni brevi.
- `webapp/novita.php` (nuovo): corrispondenze per ricerca, «visto» come comando
  esplicito (passarci per sbaglio non fa perdere segnalazioni), storico di 7 giorni.
- `webapp/search.php`: casella «avvisami dei nuovi» al salvataggio, pulsante per
  accendere/spegnere l'allerta, stato, ultimo controllo, errori della query.
  All'attivazione niente storico: un'allerta su «Iran» non porta migliaia di
  vecchi articoli.
- `webapp/nav.php`: voce «🔔 Novità» con il contatore delle non viste.
- `webapp/lib.php`: `watch_ensure()` (idempotente, a differenza di ALTER TABLE),
  `watch_unseen_by_search()`, `watch_unseen_count()`.
- Misura: una prima versione della query sembrava impiegare 67 s; ripetuta a
  cache calda impiega 27-35 ms, e l'alternativa "ottimizzata" era piu' lenta.
  Tenuta la query semplice. Collaudo: 45 corrispondenze per «Iran», identiche a
  una query indipendente; seconda esecuzione 0 doppioni; query FTS non valida
  registrata senza fermare le altre; isolamento fra utenti verificato.

### Classificazione delle fonti
- `webapp/feeds.php`: per ogni feed categoria (tipo di testata, non orientamento),
  affidabilita' secondo il **codice dell'Ammiragliato** (A-F) e nota di
  motivazione; riepilogo e legenda; import/export con la classificazione.
  Solo la lettera: la cifra 1-6 valuta la singola informazione, non la fonte.
- `webapp/lib.php`: `source_categories()`, `source_reliability_scale()`,
  `source_badge()`, `source_category_filter()`, `source_select_cols()` (le
  pagine funzionano anche prima della migrazione, solo senza distintivi),
  `feeds_classification_ensure()`.
- `webapp/browse.php`, `search.php`, `item.php`, `novita.php`: distintivo
  «B · specialistica», filtro per categoria (anche «non classificate») conservato
  dalla paginazione; in `item.php` anche la motivazione del grado.
- Collaudo: conteggi per categoria identici alle query indipendenti
  (165 + 1.569 = 1.734 su «drone»); valori non validi scartati.

### Bollettino
- `webapp/bollettino.php` (nuovo), **pagina d'ingresso** (`index.php` e login):
  la giornata ordinata per tipo di fonte, con le specialistiche in testa e il
  flusso generalista compresso in fondo; allerte della giornata; salute dei feed.
- Salute dei feed: il silenzio si giudica sul ritmo della singola fonte (stimato
  sui 23 giorni PRIMA della settimana in esame) e si segnala quando una settimana
  vuota avrebbe avuto meno del 5% di probabilita'. La regola ovvia ("zero articoli
  in 7 giorni") sui dati reali dava 6 falsi allarmi, tutti su blog a bassa
  frequenza; questa ne da' uno, ed e' un feed vero fermo dal 29/09 dietro
  risposte `304` senza errori.

### Schema e distribuzione
- `schema.sql`: colonne di allerta su `saved_searches`, tabella `watch_hits`,
  colonne di classificazione su `feeds`.
- `deploy/migrations/` (nuovo): SQL per aggiornare un DB esistente, con README.
- `deploy/rssintel-fetch.service.sample`: `ExecStartPost` per le allerte.
- `README.md`: componenti e schema aggiornati (erano fermi a prima di utenti, temi,
  catture; l'indice FTS era ancora descritto come contenuto esterno), sezione su
  bollettino, allerte e fonti.

## 2026-10-08 — Catture visive

- **Catture visive degli articoli** (in produzione dal 16/09/2026, qui versionate).
  Da `item.php` o dai Favoriti un `collaborator`/`admin` accoda una cattura a
  pagina intera della pagina originale (PNG + miniatura, SHA-256, URL finale,
  dimensioni). Versionate, eliminabili da chi le ha richieste o da un admin.
  - `webapp/capture.php` (nuovo): accodamento (POST, CSRF, ruolo), stato in JSON
    per l'aggiornamento in pagina, consegna dei file dopo `require_login()` con
    verifica che il percorso risolto cada nella cartella delle catture, ed
    eliminazione (prima la riga, poi i file: se il processo cade in mezzo restano
    solo file orfani, che il worker rimuove da solo).
  - `webapp/lib.php`: `captures_dir()`, schema, `captures_for_items()` (una query
    per tutto l'elenco), `capture_in_flight()`, `capture_badge()`.
  - `webapp/favorites.php`, `webapp/item.php`: pulsanti, miniature, versioni.
    L'aggiornamento dello stato avviene in posto, senza ricaricare la pagina:
    nei Favoriti un reload cancellerebbe le note in corso di scrittura.
  - `fetcher/rssintel_capture.py` (worker), `fetcher/cdp.py`, `fetcher/shot.py`.
    `chromium --screenshot` cattura solo il viewport, e ritagliare il bianco di
    una finestra molto alta e' stato provato e fallisce sulle pagine reali; la
    pagina intera richiede il protocollo DevTools, parlato da `cdp.py` con la
    sola libreria standard. Il rendering non avviene mai nella richiesta HTTP
    (terrebbe un worker Apache per 10-30 s), ma in coda: `flock`, transazioni
    brevi, filtro anti-SSRF importato dal fetcher, file 0644, pulizia orfani.
  - `schema.sql`: tabella `captures`. Era stata creata in produzione con una
    migrazione ma mancava dallo schema: un'installazione nuova non l'avrebbe avuta.
- `deploy/rssintel-capture.service.sample`, `.timer.sample` (nuovi).
- `README.md`: sezione sulle catture e requisiti (`chromium`, Pillow).

## 2026-09-15 (2) — Audit: chiusi i punti rimanenti (lotti A, B, C)

Seguito dell'audit del 15/09: i reperti oltre i primi cinque, applicati in tre
lotti verificabili separatamente. Ogni fix e' stato misurato prima e dopo.

### Sicurezza (lotto A)

- **SSRF nel fetcher** (`fetcher/rssintel_fetch.py`). Gli URL degli articoli
  arrivano dal contenuto dei feed remoti, cioe' da input non fidato, e venivano
  scaricati senza alcun filtro con `allow_redirects=True`. Nuove
  `is_public_url()` (solo http/https, host risolto e ogni indirizzo dev'essere
  pubblico secondo `ipaddress.is_global`) e `get_checked()` (redirect seguiti a
  mano, rivalidando ogni salto: con i redirect automatici un 302 verso la rete
  interna aggirava il controllo). Applicato anche agli URL dei feed.
  Verificato: bloccati loopback, `169.254.169.254`, LAN, `file://`, `ftp://`;
  i 24 feed attivi e 200 link reali passano invariati.
- **Schema `javascript:` negli href** (`webapp/lib.php` + 5 template).
  `h()` impedisce di uscire dall'attributo ma non filtra lo schema: un item con
  `link = javascript:...` produceva un'ancora che, se cliccata, eseguiva script
  nell'origine del portale. Nuova `safe_url()`; se lo schema non e' http/https
  il link diventa testo. Aggiunto `rel="noopener noreferrer"`.
- **Enumerazione utenti per timing** (`webapp/login.php`). Il corto-circuito
  `!$row || !password_verify(...)` saltava bcrypt per gli utenti inesistenti,
  rendendo misurabile quali username esistono. Ora la verifica avviene sempre,
  contro un hash fittizio di pari costo. Misurato: 283 ms contro 277 ms.
- **Bootstrap senza rate-limit** (`webapp/login.php`): la condizione
  `!$bootstrap &&` escludeva proprio il percorso anonimo di creazione del primo
  amministratore.

### Integrita' dei dati (lotto B)

- **Transazione del fetcher aperta durante l'I/O di rete**
  (`fetcher/rssintel_fetch.py`). Il `BEGIN` prendeva il lock di scrittura alla
  prima INSERT e lo teneva fino a fine ciclo, inclusi fino a 50 download da 25s.
  In WAL i lettori non si bloccano ma gli scrittori si': la webapp
  (`busy_timeout` 3s) perdeva scritture, e `log_access()` ingoia l'eccezione,
  quindi sparivano righe di access_log in silenzio a ogni fetch. Ora due
  transazioni brevi per item con la rete fuori da entrambe.
  Misurato su fetch reale di 8 articoli, con uno scrittore concorrente:
  **prima 4 riuscite / 3 fallite, dopo 49 riuscite / 0 fallite**.
- **Eliminazione utente** (`webapp/users.php`). `favorites`, `saved_searches` e
  `annotations` sono legati allo username, non a `users.id`: eliminando la sola
  riga utente restavano orfani, e un utente ricreato con lo stesso nome ne
  ereditava favoriti, ricerche salvate e paternita' delle annotazioni. Ora, in
  transazione: dati personali rimossi, annotazioni conservate (sono lavoro
  d'archivio condiviso) ma riattribuite a «nome (eliminato)».
- **Eliminazione feed** (`webapp/feeds.php`): `DELETE` cascata su items e da li'
  su annotazioni e favoriti. La conferma diceva solo "Eliminare feed #N?". Ora
  ogni feed mostra il conteggio articoli e la conferma dichiara cosa va perso,
  indicando «Disabilita» come alternativa reversibile.
- **`day` non validato** (`webapp/browse.php`): finiva in `new DateTime()` e un
  valore non conforme (`?day=pwned`) faceva rispondere 500. Ora solo date ISO
  reali, altrimenti si ricade su oggi.
- **CSV injection** (`webapp/feeds.php`): i titoli dei feed vengono dal feed
  remoto; se iniziano con `= + - @` venivano interpretati come formule
  all'apertura del CSV esportato.
- **Tetti di lunghezza** (`webapp/annotations.php`, `webapp/favorites.php`):
  note e quote erano illimitate; il percorso `add` dei favoriti saltava il
  limite che `note` applicava.
- **Password oltre 72 byte** (`webapp/lib.php` + login/users/profile): bcrypt le
  troncava in silenzio. Nuova `password_length_error()`, rifiutate.
- **Prepared statement** per la lookup dei tag (`webapp/annotations.php`), unico
  punto rimasto con una query costruita per concatenazione.

### Prestazioni e igiene (lotto C)

- **Query cronologica** (`schema.sql`, `webapp/browse.php`).
  `COALESCE(published_at, fetched_at)` non e' sargable: nessun indice esistente
  poteva servire e il piano era SCAN di tutte le righe di `items` piu' un
  B-tree temporaneo, a ogni caricamento. Nuovo indice d'espressione
  `idx_items_when`: **da 1,022s a 0,004s**.
- **Indice duplicato** rimosso: `idx_items_pub` e `idx_items_published_at` erano
  lo stesso indice su `published_at` (per una colonna sola SQLite scorre in
  entrambe le direzioni; piani verificati identici). Costavano spazio e una
  scrittura in piu' a ogni articolo.
- **Fusi orari** (`webapp/lib.php`, `webapp/accessi.php`, `webapp/stats.php`).
  `date.timezone` era UTC mentre il sistema (e quindi il `'localtime'` di
  SQLite) e' su Europe/Rome: i filtri data di Accessi confrontavano giorni
  italiani con timestamp UTC, e Statistiche generava le etichette con PHP e i
  bucket con SQL, sfasati di due ore. Ora `lib.php` fissa `RSSINTEL_TZ`, nuova
  `day_bounds_utc()` per i confronti, e Statistiche segnala in pagina se
  database e applicazione dovessero divergere.
- **Ciclo geo** (`webapp/accessi.php`): faceva una query per ogni IP conosciuto
  a ogni caricamento; ora la cache si legge in una sola query.
- **Referrer verso ipinfo.io** (`webapp/accessi.php`): i link inviavano a terzi
  l'URL di Accessi, che puo' contenere username e filtri. Aggiunto
  `rel="noopener noreferrer"`.
- **Conservazione del log accessi** (`webapp/lib.php`, `webapp/accessi.php`,
  `config.sample.php`): `access_log` cresceva senza limite e senza modo di
  potarlo. Nuova `access_log_retention_days` — **default 0, cioe' illimitata**:
  un default che cancella dati senza che siano stati chiesti e' la scelta
  sbagliata, quindi la potatura si attiva solo se configurata. Con un valore
  positivo avviene da sola durante il traffico; in Accessi c'e' comunque un
  comando per potare a mano indicando i giorni.

## 2026-09-15 — Audit di sicurezza: 5 correzioni (2 critiche)

Esito di un audit completo della piattaforma. Tutti i reperti sono stati
riprodotti sul deployment live prima della correzione e verificati dopo.

- **`deploy/htaccess.sample` — sorgente e configurazione servite in chiaro
  (critico).** Apache non passa a mod_php i file che non finiscono in `.php`
  (l'handler richiede `\.php$`): li serve come testo. Erano quindi scaricabili
  senza autenticazione tutti i backup dell'editor (`*.php~`, incluso il
  `config.php~` con la configurazione reale e il `lib.php~` con l'intera
  logica di autenticazione) e `item.php_notrad`. Nuove regole `FilesMatch` per
  backup, estensioni di lavoro e file PHP con estensione non standard.
- **`webapp/translate.php` — endpoint senza autenticazione (critico).** Era
  l'unica pagina priva di gate: includeva `lib.php` ma non chiamava mai
  `require_login()`. Una POST anonima raggiungeva il servizio di traduzione su
  loopback e teneva occupato un worker Apache per i 120 s del timeout — con
  MPM prefork e `MaxRequestWorkers 150` bastavano ~150 richieste anonime per
  rendere irraggiungibile l'intero portale. Aggiunti: gate `auth_user()`,
  verifica CSRF via header `X-CSRF-Token` (il client manda JSON, quindi
  `csrf_check()` su `$_POST` non era applicabile), validazione dei codici
  lingua, timeout ridotto da 120 s a 30 s. Misurato dopo: 401 in 14 ms.
- **`webapp/item.php`**: la fetch verso `translate.php` manda ora il token CSRF
  nell'header `X-CSRF-Token`. Va in coppia con la modifica sopra.
- **`webapp/lib.php` — cookie di sessione senza `Secure`.** Il flag era
  commentato ("abilita se il sito e' servito solo via HTTPS") mentre il sito e'
  su HTTPS: su una prima richiesta in chiaro il `PHPSESSID` viaggiava in
  cleartext prima che scattasse il redirect 301. Ora `'secure' => true`.
- **`deploy/security-headers.conf.sample` — riscritto.** Lo snippet precedente
  applicava `Cache-Control: immutable, max-age=1 anno` a *tutti* i `.css`, ma
  `base.css` non e' versionato: i temi lo includono con `@import "../base.css"`
  senza query string. Installato cosi' avrebbe congelato per un anno il file
  che contiene tutte le regole responsive. Ora: `immutable` solo per
  `assets/themes/` (che `theme_href()` versiona davvero), `no-cache` per
  `base.css` (con l'ETag gia' presente costa un 304), `no-store` per le pagine
  PHP. Corretto anche un `<Directory>` annidato, che Apache non ammette.
  Aggiunto `Options -Indexes` (le cartelle senza index erano elencabili).
- **`fetcher/rssintel_fetch.py` — permessi dei file di testo.**
  `tempfile.mkstemp()` crea sempre con modo `0600`, per progetto e a
  prescindere dalla umask, e `os.replace()` preserva quel modo: ogni file di
  testo nasceva leggibile solo dall'utente del fetcher, e qualunque
  backup/sync eseguito da un altro utente falliva in lettura. Aggiunto
  `os.chmod(tmp, 0o644)` prima di `os.replace()`.
- **`deploy/allowoverride.conf.sample`** (nuovo): documenta perche'
  `AllowOverride` resta ad `AuthConfig` e perche' `Header` e `Options` non
  vanno nel `.htaccess` — e' la causa dell'interruzione totale del 03/09/2026.
- **`deploy/no-editor-backups.conf.sample`** (nuovo): rete di sicurezza a
  livello di server che nega i backup degli editor su tutti i vhost, cosi' il
  prossimo file dimenticato non torna leggibile.

## 2026-09-13 — Sistema multi-tema (6 stili, scelta riservata all'admin)

- **`webapp/assets/base.css`** (nuovo): tutto il CSS strutturale/responsive
  estratto dal vecchio `style.css` unico, parametrizzato su un contratto di
  variabili comune (`--paper`, `--paper-dark`, `--paper-panel`, `--ink`,
  `--ink-muted`, `--red-stamp`, `--red-stamp-light`, `--border`,
  `--border-heavy`, `--typewriter`, `--radius`, `--texture`, `--stripe`).
  Include anche le nuove classi `.theme-grid` / `.theme-card` /
  `.theme-preview` per la pagina di selezione tema.
- **`webapp/assets/themes/*.css`** (6 nuovi file, ognuno `@import
  "../base.css"` + variabili + piccoli ritocchi decorativi): `dossier-
  vintage.css` (l'estetica originale, resta il default), `samizdat.css`,
  `archivio-stato.css`, `redacted.css`, `telex.css`, `neutro.css` — sei
  varianti dello stesso registro grafico (dossier/samizdat/archivio
  d'intelligence/macchina da scrivere guerra fredda).
- **`webapp/theme.php`** (nuovo, solo admin): galleria dei 6 temi con
  anteprima colori, form per attivarne uno — vale per **tutti gli utenti**,
  scelta riservata all'admin.
- **`webapp/lib.php`**: `available_themes()` (catalogo dei temi), tabella
  `site_settings` (chiave/valore, creata anche a runtime) con
  `site_settings_ensure()`, `active_theme()` (letto da `site_settings`,
  cache statica, default `dossier-vintage`), `set_active_theme()`,
  `theme_href()` (URL del CSS del tema attivo con cache-busting su
  `filemtime()` del tema + di `base.css`).
- **`webapp/nav.php`**: nuova voce `🎨 Tema` nel blocco riservato all'admin.
- Tutte le pagine con `<link>` (`index`, `browse`, `search`, `favorites`,
  `feeds`, `item`, `notes`, `stats`, `accessi`, `login`, `profile`, più il
  nuovo `theme.php`) passano da `assets/style.css?v=...` a
  `<?= h(theme_href()) ?>`, che risolve al tema attivo.
- **`schema.sql`**: nuova tabella `site_settings`.
- **Rimosso** `webapp/assets/style.css`: superato dal sistema di temi
  (`base.css` + `themes/dossier-vintage.css` ne prendono il posto 1:1,
  stesso aspetto di default).

## 2026-09-03 (2) — Fix .htaccess, cache-bust CSS, responsive, bandiere

- **Fix 500 da `.htaccess`**: rimosso il blocco `<IfModule mod_headers.c> Header
  ... </IfModule>` — le direttive `Header` in `.htaccess` richiedono
  `AllowOverride FileInfo` e, dopo un cambio di config Apache a livello server,
  facevano andare in 500 ogni richiesta sotto `/rssintel/`. Gli header di
  sicurezza (X-Content-Type-Options, X-Frame-Options, Referrer-Policy, CSP)
  vanno ora nel vhost: nuovo `deploy/security-headers.conf.sample`.
- **Cache-busting del CSS**: il `<link>` di ogni pagina diventa
  `assets/style.css?v=<?= filemtime(...) ?>`. Apache serve `style.css` senza
  `Cache-Control`, quindi i browser (in particolare i telefoni) tenevano in
  cache una copia vecchia e non vedevano il layout responsive. Ora l'URL
  cambia a ogni modifica del file.
- **Responsive tablet/smartphone** (senza toccare il desktop): tabelle dati in
  `.dtable` con scroll orizzontale interno; colonne secondarie (`.col-sec`)
  nascoste sotto i 640px; tap target piu' ampi; `html { overflow-x: hidden }`;
  date/IP che vanno a capo su schermo stretto. `stats.php` e `accessi.php`:
  tabelle rifattorizzate da stili inline a classi `.dtable`. In `accessi.php`
  la sezione "Attivita' in corso" passa a 3 colonne sul telefono.
- **Accessi per paese**: nuova scheda in `accessi.php` (bandiera + nome paese +
  barra per numero richieste + IP distinti; riga "richieste da IP di rete
  locale"). Bandiere anche nella colonna Paese delle tabelle. Risoluzione geo
  degli IP limitata a 10 nuovi per caricamento (evita di saturare ip-api.com);
  helper `bar_pct()`.
- `webapp/assets/style.css` (tema neutro): stesso blocco responsive; aggiunta
  variabile `--red-stamp` usata dalle barre di stats/accessi.

## 2026-09-03 — Sezioni Statistiche e Accessi

- **Sezione Statistiche** (`webapp/stats.php`, tutti i ruoli): riepilogo
  (articoli, feed attivi/totali, annotazioni, tag, favoriti, ricerche salvate,
  utenti, dimensione DB, primo/ultimo articolo, ultimo fetch, media/giorno),
  raccolta articoli per giorno (ultimi 30) e per mese (ultimi 12) con barre CSS
  in ora di Roma, tabella per-feed con quota % e stato di salute
  (last_status / last_error), tag piu' usati, annotazioni per autore.
- **Sezione Accessi** (`webapp/accessi.php`, solo admin): attivita' in corso
  negli ultimi 5 minuti (utente/IP attivi, ruolo, ultima pagina, n. richieste
  — via window function su `access_log`); riepilogo (richieste totali, IP unici,
  oggi, 24h, utenti distinti, login falliti 24h); log accessi filtrabile
  (data da/a, utente, pagina, IP/UA) e paginato, con stato HTTP colorato e link
  ipinfo.io; login recenti da `login_attempts` + falliti-24h per IP; scheda
  per-utente (ultimo accesso/IP, richieste totali, IP distinti, ultima attivita').
- **`lib.php`**: tabelle `access_log`, `login_attempts`, `ip_geo_cache` (create
  anche a runtime); `log_access()` in `register_shutdown_function`, chiamata
  automaticamente da `lib.php` (una riga per richiesta HTTP);
  `record_login_attempt()`, `recent_failed_logins()`; geolocalizzazione IP
  opzionale (`cfg()['ipgeo']`, default off) via ip-api.com con cache 30 giorni,
  `flag_emoji()`.
- **`login.php`**: registra ogni tentativo (riuscito/fallito); rate-limit
  morbido — ≥10 tentativi falliti dallo stesso IP in 15 minuti bloccano
  temporaneamente il login.
- **`nav.php`**: nuove voci `📊 Statistiche` (tutti) e `🔐 Accessi` (admin).
- **`schema.sql`**: tabelle `access_log`, `login_attempts`, `ip_geo_cache`.
- **`config.sample.php`**: nuovo flag `ipgeo` (default false, documentato);
  commento di `admins` aggiornato (legacy, i ruoli stanno nella tabella users).

## 2026-08-28 — Data IT, paginazione, snippet FTS, CSP, Favoriti

- **Formato data italiano** ovunque nel portale: nuovi `fmt_dt()` / `fmt_day()`
  in `lib.php` (le date del DB sono UTC -> mostrate come `GG/MM/AAAA HH:MM`
  nel fuso `Europe/Rome`). Applicato in `browse.php`, `search.php`, `item.php`,
  `notes.php`, `feeds.php`, `users.php`. `browse.php` converte anche gli estremi
  del filtro giorno/settimana/mese da giorno di calendario italiano a intervallo
  UTC per la query.
- **Paginazione in `search.php`**: conteggio totale dei risultati + `?page=N`
  con navigazione (Prec / numeri / Succ), preservando `q` / `feed_id` / limite;
  il selettore diventa "risultati per pagina". Blocco query rifattorizzato
  (closure condivisa, meno duplicazione).
- **FTS con testo conservato**: `items_fts` non e' piu' `content=''`
  (self-contained) -> `snippet()` restituisce estratti reali con le parole
  evidenziate nei risultati di ricerca. `schema.sql` aggiornato. I file del
  fetcher usano ora `DELETE FROM items_fts WHERE rowid = ?` invece del comando
  `'delete'` (valido solo per le tabelle contentless);
  `rssintel_rebuild_fts.py` ora decomprime i `.txt.gz` (prima leggeva i byte
  gzip come testo).
- **CSP** (`.htaccess` / `deploy/htaccess.sample`): aggiunto
  `Content-Security-Policy` moderata — `default-src 'self'`; blocca risorse
  esterne, framing, plugin ed esfiltrazione via form; `'unsafe-inline'` resta
  per script/handler/stili inline.
- **Sezione Favoriti**: nuova `webapp/favorites.php` (elenco per-utente degli
  articoli salvati, con nota personale modificabile in linea; azioni POST
  add/remove/note con pattern PRG). `webapp/item.php`: pulsante toggle
  `Aggiungi ai favoriti` / `rimuovi` nella card Dettagli. `webapp/nav.php`:
  nuova voce **★ Favoriti**. `lib.php`: `favorites_schema()`,
  `favorites_ensure()`, `is_favorite()`. `schema.sql`: tabella `favorites`
  (`owner`, `item_id` con `ON DELETE CASCADE`, `note`, `created_at`,
  `UNIQUE(owner, item_id)`). Disponibile a tutti i ruoli (favoriti personali).

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
