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

/**
 * Data/ora del DB (UTC) formattata all'italiana nel fuso di Roma.
 *   fmt_dt('2026-08-27 21:14:34')        -> '27/08/2026 23:14'
 *   fmt_dt('2026-08-27 21:14:34', false) -> '27/08/2026'
 * Stringa vuota o non parsabile -> ritorna il valore grezzo (o '').
 */
function fmt_dt(?string $s, bool $with_time = true): string {
  $s = trim((string)$s);
  if ($s === '' || str_starts_with($s, '0000-00-00')) return '';
  try {
    $dt = new DateTime($s, new DateTimeZone('UTC'));
    $dt->setTimezone(new DateTimeZone('Europe/Rome'));
    return $dt->format($with_time ? 'd/m/Y H:i' : 'd/m/Y');
  } catch (Throwable $e) {
    return $s;
  }
}

/** Giorno di calendario 'AAAA-MM-GG' -> 'GG/MM/AAAA' (nessuna conversione di fuso). */
function fmt_day(string $s): string {
  return preg_match('~^(\d{4})-(\d{2})-(\d{2})~', $s, $m) ? "$m[3]/$m[2]/$m[1]" : $s;
}

/* =====================  Sessione + CSRF  ===================== */

if (session_status() === PHP_SESSION_NONE) {
  session_set_cookie_params([
    'lifetime' => 0,
    'path'     => '/',
    'httponly' => true,
    'samesite' => 'Lax',
    // 'secure' => true,  // abilita se il sito e' servito solo via HTTPS
  ]);
  session_start();
}
if (empty($_SESSION['csrf'])) {
  $_SESSION['csrf'] = bin2hex(random_bytes(16));
}

function csrf_token(): string {
  return (string)($_SESSION['csrf'] ?? '');
}

function csrf_check(): bool {
  $t = (string)($_POST['csrf'] ?? '');
  return $t !== '' && hash_equals((string)($_SESSION['csrf'] ?? ''), $t);
}

/* =====================  Utenti / ruoli  ===================== */

/** Ruoli validi, dal meno al piu' privilegiato. */
const RSSINTEL_ROLES = ['reader', 'collaborator', 'admin'];

/** DDL della tabella utenti (usata anche a runtime da login.php / users.php). */
function users_schema(): string {
  return "
    CREATE TABLE IF NOT EXISTS users (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      username      TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      role          TEXT NOT NULL DEFAULT 'reader',
      disabled      INTEGER NOT NULL DEFAULT 0,
      created_by    TEXT,
      created_at    TEXT NOT NULL DEFAULT (datetime('now')),
      last_login_at TEXT
    );
  ";
}

function users_ensure(SQLite3 $dbw): void {
  $dbw->exec(users_schema());
}

/** DDL della tabella favoriti (creata anche a runtime da favorites.php). */
function favorites_schema(): string {
  return "
    CREATE TABLE IF NOT EXISTS favorites (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      owner      TEXT NOT NULL,
      item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
      note       TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(owner, item_id)
    );
    CREATE INDEX IF NOT EXISTS idx_favorites_owner ON favorites(owner, created_at DESC);
  ";
}

function favorites_ensure(SQLite3 $dbw): void {
  $dbw->exec(favorites_schema());
}

/** true se l'utente corrente ha gia' nei favoriti l'item indicato. */
function is_favorite(SQLite3 $db, int $item_id): bool {
  if (!$db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='favorites'")) {
    return false;
  }
  $st = $db->prepare("SELECT 1 FROM favorites WHERE owner = :o AND item_id = :i");
  $st->bindValue(':o', current_user(), SQLITE3_TEXT);
  $st->bindValue(':i', $item_id, SQLITE3_INTEGER);
  return (bool)$st->execute()->fetchArray(SQLITE3_ASSOC);
}

/** true se la tabella users esiste. */
function users_table_exists(SQLite3 $db): bool {
  return (bool)$db->querySingle(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
  );
}

/**
 * Riga dell'utente autenticato (id, username, role, disabled) o null.
 * Riallinea username/role nella sessione ad ogni richiesta e invalida la
 * sessione se l'utente e' stato disabilitato o rimosso.
 */
function auth_user(): ?array {
  static $cache = null;
  if ($cache !== null) {
    return $cache ?: null;
  }
  if (empty($_SESSION['uid'])) {
    $cache = false;
    return null;
  }
  try {
    $db = db_ro();
    $st = $db->prepare("SELECT id, username, role, disabled FROM users WHERE id = :id");
    $st->bindValue(':id', (int)$_SESSION['uid'], SQLITE3_INTEGER);
    $r = $st->execute()->fetchArray(SQLITE3_ASSOC);
  } catch (Throwable $e) {
    $r = false;
  }
  if (!$r || (int)$r['disabled'] === 1) {
    $_SESSION = [];
    $cache = false;
    return null;
  }
  $_SESSION['uname'] = $r['username'];
  $_SESSION['role']  = $r['role'];
  $cache = $r;
  return $r;
}

function current_user(): string {
  return (string)($_SESSION['uname'] ?? '');
}

function current_role(): string {
  return (string)($_SESSION['role'] ?? '');
}

/** Compat: firma storica is_admin($u); ora conta solo il ruolo di sessione. */
function is_admin(string $u = ''): bool {
  return current_role() === 'admin';
}

/** reader = sola lettura; collaborator/admin possono annotare e taggare. */
function can_annotate(): bool {
  return in_array(current_role(), ['collaborator', 'admin'], true);
}

/** Redirige a login.php se non autenticato. Da chiamare in cima alle pagine HTML. */
function require_login(): void {
  if (auth_user() === null) {
    $next = (string)($_SERVER['REQUEST_URI'] ?? '');
    header('Location: login.php' . ($next !== '' ? '?next=' . urlencode($next) : ''));
    exit;
  }
}

/** require_login + ruolo tra quelli indicati, altrimenti 403. */
function require_role(string ...$roles): void {
  require_login();
  if (!in_array(current_role(), $roles, true)) {
    http_response_code(403);
    die('403 — permesso negato (ruolo: ' . h(current_role() ?: 'nessuno') . ').');
  }
}
