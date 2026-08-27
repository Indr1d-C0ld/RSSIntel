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

    // Username (valore di REMOTE_USER fornito dalla Basic Auth del webserver)
    // abilitati alla gestione dei feed in feeds.php.
    'admins' => ['admin'],
];
