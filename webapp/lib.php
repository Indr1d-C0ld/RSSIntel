<?php
declare(strict_types=1);

/**
 * Carica la configurazione da config.php (vedi config.sample.php).
 * Il risultato e' memoizzato per l'intera richiesta.
 */
function cfg(): array {
  static $c = null;
  if ($c === null) {
    $f = dirname(__DIR__) . '/config.php';
    if (!is_file($f)) {
      $f = __DIR__ . '/config.php'; // layout piatto (webapp servita dalla root)
    }
    if (!is_file($f)) {
      http_response_code(500);
      die('config.php mancante: copia config.sample.php in config.php e adatta i valori.');
    }
    $c = require $f;
  }
  return $c;
}

function db_ro(): SQLite3 {
  $db = new SQLite3(cfg()['db_path'], SQLITE3_OPEN_READONLY);
  $db->busyTimeout(3000);
  $db->exec("PRAGMA foreign_keys=ON;");
  return $db;
}

function db_rw(): SQLite3 {
  $db = new SQLite3(cfg()['db_path'], SQLITE3_OPEN_READWRITE);
  $db->busyTimeout(3000);
  $db->exec("PRAGMA foreign_keys=ON;");
  return $db;
}

function h(string $s): string {
  return htmlspecialchars($s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function current_user(): string {
  // se hai auth webserver (Basic, Digest, etc.) ti riporta l'utente
  return $_SERVER['REMOTE_USER'] ?? 'unknown';
}

function is_admin(string $u): bool {
  return in_array($u, cfg()['admins'] ?? [], true);
}
