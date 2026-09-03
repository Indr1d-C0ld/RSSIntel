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

/* =====================  Log accessi / attivita'  ===================== */

/** IP del client (dietro reverse proxy affidabile si potrebbe usare X-Forwarded-For). */
function client_ip(): string {
  return (string)($_SERVER['REMOTE_ADDR'] ?? '');
}

function access_log_schema(): string {
  return "
    CREATE TABLE IF NOT EXISTS access_log (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      ts           TEXT NOT NULL DEFAULT (datetime('now')),
      ip           TEXT NOT NULL DEFAULT '',
      username     TEXT,
      role         TEXT,
      path         TEXT NOT NULL DEFAULT '',
      method       TEXT NOT NULL DEFAULT '',
      query_string TEXT,
      status_code  INTEGER,
      user_agent   TEXT,
      referer      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_access_log_ts   ON access_log(ts);
    CREATE INDEX IF NOT EXISTS idx_access_log_ip   ON access_log(ip);
    CREATE INDEX IF NOT EXISTS idx_access_log_user ON access_log(username);
    CREATE INDEX IF NOT EXISTS idx_access_log_path ON access_log(path);
    CREATE TABLE IF NOT EXISTS login_attempts (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      username   TEXT NOT NULL DEFAULT '',
      ip         TEXT NOT NULL DEFAULT '',
      success    INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_login_attempts_ip   ON login_attempts(ip, created_at);
    CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at);
    CREATE TABLE IF NOT EXISTS ip_geo_cache (
      ip           TEXT PRIMARY KEY,
      country_code TEXT,
      country_name TEXT,
      resolved_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
  ";
}

function logging_ensure(SQLite3 $dbw): void {
  $dbw->exec(access_log_schema());
}

/** Registra questa richiesta nel log accessi, in shutdown (non blocca la pagina). */
function log_access(): void {
  static $registered = false;
  if ($registered || PHP_SAPI === 'cli') return;
  $registered = true;

  register_shutdown_function(function () {
    try {
      $db = db_rw();
      logging_ensure($db);
      $u = auth_user();
      $st = $db->prepare("
        INSERT INTO access_log
          (ip, username, role, path, method, query_string, status_code, user_agent, referer)
        VALUES (:ip, :un, :role, :path, :method, :qs, :sc, :ua, :ref)
      ");
      $st->bindValue(':ip',     client_ip(), SQLITE3_TEXT);
      if ($u) { $st->bindValue(':un', (string)$u['username'], SQLITE3_TEXT); }
      else    { $st->bindValue(':un', null, SQLITE3_NULL); }
      $role = current_role();
      if ($role !== '') { $st->bindValue(':role', $role, SQLITE3_TEXT); }
      else              { $st->bindValue(':role', null, SQLITE3_NULL); }
      $st->bindValue(':path',   basename((string)($_SERVER['SCRIPT_NAME'] ?? '')), SQLITE3_TEXT);
      $st->bindValue(':method', (string)($_SERVER['REQUEST_METHOD'] ?? ''), SQLITE3_TEXT);
      $st->bindValue(':qs',     (string)($_SERVER['QUERY_STRING'] ?? ''), SQLITE3_TEXT);
      $st->bindValue(':sc',     (int)(http_response_code() ?: 0), SQLITE3_INTEGER);
      $st->bindValue(':ua',     mb_substr((string)($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 500), SQLITE3_TEXT);
      $st->bindValue(':ref',    mb_substr((string)($_SERVER['HTTP_REFERER'] ?? ''), 0, 500), SQLITE3_TEXT);
      $st->execute();
    } catch (Throwable $e) {
      // il logging non deve mai rompere la risposta
    }
  });
}

/** Registra un tentativo di login (riuscito o fallito). */
function record_login_attempt(string $username, string $ip, bool $success): void {
  try {
    $db = db_rw();
    logging_ensure($db);
    $st = $db->prepare("INSERT INTO login_attempts (username, ip, success) VALUES (:u, :i, :s)");
    $st->bindValue(':u', mb_substr($username, 0, 64), SQLITE3_TEXT);
    $st->bindValue(':i', $ip, SQLITE3_TEXT);
    $st->bindValue(':s', $success ? 1 : 0, SQLITE3_INTEGER);
    $st->execute();
  } catch (Throwable $e) {
    // non bloccante
  }
}

/** Tentativi di login falliti da questo IP negli ultimi $minutes minuti. */
function recent_failed_logins(SQLite3 $db, string $ip, int $minutes = 15): int {
  if (!$db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_attempts'")) {
    return 0;
  }
  $st = $db->prepare("
    SELECT COUNT(*) c FROM login_attempts
    WHERE ip = :ip AND success = 0 AND created_at >= datetime('now', :win)
  ");
  $st->bindValue(':ip', $ip, SQLITE3_TEXT);
  $st->bindValue(':win', '-' . $minutes . ' minutes', SQLITE3_TEXT);
  return (int)$st->execute()->fetchArray(SQLITE3_ASSOC)['c'];
}

/* --- Geolocalizzazione IP (opzionale, cache-first) --- */

function ipgeo_enabled(): bool {
  return !empty(cfg()['ipgeo']);
}

/** Bandiera emoji da codice ISO alpha-2 (nessuna chiamata esterna). */
function flag_emoji(?string $cc): string {
  $cc = strtoupper(trim((string)$cc));
  if (!preg_match('/^[A-Z]{2}$/', $cc)) return '';
  $base = 0x1F1E6 - 65;
  return mb_chr(ord($cc[0]) + $base, 'UTF-8') . mb_chr(ord($cc[1]) + $base, 'UTF-8');
}

/** Paese di un IP: cache-first; risolve via ip-api.com solo se cfg()['ipgeo']. */
function resolve_ip_geo(SQLite3 $dbw, string $ip): array {
  $none = ['country_code' => null, 'country_name' => null];
  if (!filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE)) {
    return $none; // IP privato/riservato o non valido
  }
  try {
    logging_ensure($dbw);
    $st = $dbw->prepare("SELECT country_code, country_name, resolved_at FROM ip_geo_cache WHERE ip = :ip");
    $st->bindValue(':ip', $ip, SQLITE3_TEXT);
    $row = $st->execute()->fetchArray(SQLITE3_ASSOC);
    if ($row && (time() - strtotime((string)$row['resolved_at'])) < 30 * 86400) {
      return ['country_code' => $row['country_code'], 'country_name' => $row['country_name']];
    }
    if (!ipgeo_enabled()) {
      return $row ? ['country_code' => $row['country_code'], 'country_name' => $row['country_name']] : $none;
    }
    $ctx = stream_context_create(['http' => ['timeout' => 4, 'ignore_errors' => true]]);
    $raw = @file_get_contents('http://ip-api.com/json/' . urlencode($ip) . '?fields=status,countryCode,country', false, $ctx);
    $cc = null; $cn = null;
    if ($raw && ($j = json_decode($raw, true)) && ($j['status'] ?? '') === 'success') {
      $cc = $j['countryCode'] ?? null;
      $cn = $j['country'] ?? null;
    }
    $up = $dbw->prepare("
      INSERT INTO ip_geo_cache (ip, country_code, country_name, resolved_at)
      VALUES (:ip, :cc, :cn, datetime('now'))
      ON CONFLICT(ip) DO UPDATE SET country_code = excluded.country_code,
        country_name = excluded.country_name, resolved_at = datetime('now')
    ");
    $up->bindValue(':ip', $ip, SQLITE3_TEXT);
    $up->bindValue(':cc', $cc, $cc === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $up->bindValue(':cn', $cn, $cn === null ? SQLITE3_NULL : SQLITE3_TEXT);
    $up->execute();
    return ['country_code' => $cc, 'country_name' => $cn];
  } catch (Throwable $e) {
    return $none;
  }
}

// Ogni pagina che include lib.php viene registrata nel log accessi.
log_access();
