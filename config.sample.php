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

    // Legacy: usato solo dal vecchio is_admin($u) prima dell'auth applicativa.
    // Con la tabella `users` non serve piu' (i ruoli stanno nel DB); lasciato
    // per retrocompatibilita'.
    'admins' => ['admin'],

    // Cartella dei testi estratti, se diversa da <cartella del db>/text (deve
    // coincidere con RSSINTEL_TXT_DIR del fetcher). Serve all'eliminazione di un
    // feed in feeds.php, che rimuove anche i file di testo dei suoi articoli.
    // 'text_dir' => '/var/lib/rssintel/text',

    // Cartella delle catture visive (JPEG a piena pagina degli articoli).
    // DEVE stare fuori dal webroot: i file sono serviti da capture.php dopo
    // require_login(), mentre sotto la webroot sarebbero scaricabili da
    // chiunque. Se omessa: <cartella del db>/captures.
    // Le catture le produce rssintel_capture.py, avviato da
    // rssintel-capture.path quando capture.php crea il file-segnale
    // <cartella>/.richiesta; richiede chromium installato.
    // 'captures_dir' => '/var/lib/rssintel/captures',

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
