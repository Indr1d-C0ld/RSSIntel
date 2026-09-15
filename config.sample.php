<?php
declare(strict_types=1);

/**
 * RSSIntel - configurazione.
 *
 * Copia questo file in `config.php` (stessa cartella) e adatta i valori.
 * `config.php` NON va versionato: e' gia' in .gitignore.
 *
 * Lo stesso file e' letto sia dalla webapp PHP sia, per i percorsi, dal
 * fetcher Python tramite le variabili d'ambiente equivalenti
 * (RSSINTEL_DB, RSSINTEL_RAW_DIR, RSSINTEL_TXT_DIR, RSSINTEL_UA).
 */

return [
    // Percorso assoluto del database SQLite.
    // Deve essere leggibile/scrivibile dall'utente del webserver e da quello del fetcher.
    'db_path' => '/var/lib/rssintel/rssintel.db',

    // Endpoint del servizio di traduzione.
    // Contratto: POST JSON {q, source, target} -> risposta JSON {translatedText}
    // Compatibile con LibreTranslate e con un wrapper HTTP su modelli NLLB.
    'translate_url' => 'http://127.0.0.1:5000/translate',

    // Tetto rigido (byte): oltre questa soglia translate.php risponde 413.
    'translate_max_chars' => 50000,

    // Limite pratico del motore (caratteri): quanto testo traduce bene in una
    // volta (NLLB ~512 token). Mostrato nella sezione traduzione di item.php e
    // usato per troncare lato client e lato server. ~2000 ≈ 350 parole.
    'translate_soft_limit' => 2000,

    // Legacy: usato solo dal vecchio is_admin($u) prima dell'auth applicativa.
    // Con la tabella `users` non serve piu' (i ruoli stanno nel DB); lasciato
    // per retrocompatibilita'.
    'admins' => ['admin'],

    // Giorni di conservazione del log accessi (accessi.php).
    // 0 (default) = conservazione illimitata, nessuna cancellazione automatica.
    // Un valore positivo attiva la potatura: avviene da sola, saltuariamente,
    // durante il normale traffico; da accessi.php si puo' anche forzare subito.
    //
    // Da valutare consapevolmente: access_log registra una riga per richiesta
    // HTTP con IP, user-agent, referer e query string, e quest'ultima per
    // search.php contiene i termini cercati dagli utenti.
    'access_log_retention_days' => 0,

    // Geolocalizzazione degli IP nel log accessi (accessi.php): se true, gli IP
    // vengono risolti a paese via ip-api.com (gratuito, senza API key), con
    // cache di 30 giorni in ip_geo_cache. Chiamata fatta dal server.
    'ipgeo' => false,
];
