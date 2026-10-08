<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

/**
 * Catture visive: accodamento, stato e consegna dei file.
 *
 * I PNG stanno fuori dal webroot e solo questo script li serve, dopo
 * require_login(). Sotto /data/html/ sarebbero scaricabili da chiunque.
 */

require_login();

/** Redirect solo verso una pagina .php locale (come safe_next in login.php). */
function cap_safe_ret(string $n): string {
  if (preg_match('~([A-Za-z0-9_]+\.php(?:\?[^#\s]*)?)$~', trim($n), $m)) return $m[1];
  return 'favorites.php';
}

function cap_json(array $x, int $code = 200): never {
  http_response_code($code);
  header('Content-Type: application/json; charset=utf-8');
  header('X-Content-Type-Options: nosniff');
  echo json_encode($x, JSON_UNESCAPED_UNICODE);
  exit;
}

/* ===================== POST: accoda una cattura ===================== */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }
  if (!can_annotate()) {
    http_response_code(403);
    die('403 — la cattura richiede il ruolo collaborator o admin.');
  }

  $item_id = (int)($_POST['item_id'] ?? 0);
  $ret     = cap_safe_ret((string)($_POST['ret'] ?? ''));
  $action  = (string)($_POST['action'] ?? 'enqueue');

  /* ---- Eliminazione di una cattura ---- */
  if ($action === 'delete') {
    $cap_id = (int)($_POST['capture_id'] ?? 0);
    try {
      $dbw = db_rw();
      captures_ensure($dbw);

      $st = $dbw->prepare("SELECT id, item_id, status, png_path, requested_by
                           FROM captures WHERE id = :i");
      $st->bindValue(':i', $cap_id, SQLITE3_INTEGER);
      $c = $st->execute()->fetchArray(SQLITE3_ASSOC);
      if (!$c) throw new RuntimeException('Cattura inesistente.');

      // Stessa regola delle annotazioni: l'autore, oppure un admin.
      if (!is_admin() && (string)$c['requested_by'] !== current_user()) {
        throw new RuntimeException('Puoi eliminare solo le catture che hai richiesto.');
      }
      // Una cattura in corso e' in mano al worker: eliminarla ora lascerebbe
      // il file a meta' scrittura. Si aspetta che finisca.
      if ((string)$c['status'] === 'running') {
        throw new RuntimeException('Cattura in corso: riprova fra qualche istante.');
      }

      // Prima la riga, poi i file. Se il processo cade fra i due passi
      // restano file senza riga, e sweep_orphans() del worker li rimuove al
      // giro successivo: si ripara da solo. Nell'ordine inverso resterebbe
      // una riga che punta a file inesistenti, cioe' una miniatura rotta che
      // nessuna pulizia automatica corregge (lo sweep guarda i file, non le righe).
      $del = $dbw->prepare("DELETE FROM captures WHERE id = :i");
      $del->bindValue(':i', $cap_id, SQLITE3_INTEGER);
      $del->execute();

      if (!empty($c['png_path'])) {
        $root = realpath(captures_dir());
        foreach ([(string)$c['png_path'],
                  preg_replace('~\.png$~', '_thumb.jpg', (string)$c['png_path'])] as $rel) {
          $full = realpath(captures_dir() . '/' . $rel);
          if ($full !== false && $root !== false && str_starts_with($full, $root . '/')) {
            @unlink($full);
          }
        }
      }

      $_SESSION['flash'] = ['ok', 'Cattura eliminata.'];
    } catch (Throwable $e) {
      $_SESSION['flash'] = ['err', $e->getMessage()];
    }
    header('Location: ' . $ret);
    exit;
  }

  /* ---- Accodamento ---- */
  try {
    $dbw = db_rw();
    captures_ensure($dbw);

    $st = $dbw->prepare("SELECT link FROM items WHERE id = :i");
    $st->bindValue(':i', $item_id, SQLITE3_INTEGER);
    $row = $st->execute()->fetchArray(SQLITE3_ASSOC);

    if (!$row) {
      throw new RuntimeException('Articolo inesistente.');
    }
    // Stesso criterio del worker: se non e' http/https non ha senso accodarlo.
    if (safe_url((string)$row['link']) === '') {
      throw new RuntimeException("L'articolo non ha un link http/https utilizzabile.");
    }
    if (capture_in_flight($dbw, $item_id)) {
      throw new RuntimeException('Per questo articolo c\'e\' gia\' una cattura in corso.');
    }

    $ins = $dbw->prepare(
      "INSERT INTO captures(item_id, requested_by, url) VALUES(:i, :u, :l)"
    );
    $ins->bindValue(':i', $item_id, SQLITE3_INTEGER);
    $ins->bindValue(':u', current_user(), SQLITE3_TEXT);
    $ins->bindValue(':l', (string)$row['link'], SQLITE3_TEXT);
    $ins->execute();

    $_SESSION['flash'] = ['ok', 'Cattura accodata: viene eseguita entro pochi minuti.'];
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }

  header('Location: ' . $ret);
  exit;
}

/* ===================== GET ?status : stato per l'aggiornamento in pagina ===================== */
if (isset($_GET['status'])) {
  $ids = array_filter(array_map('intval', explode(',', (string)($_GET['items'] ?? ''))));
  if (!$ids) cap_json(['captures' => []]);
  if (count($ids) > 200) $ids = array_slice($ids, 0, 200);

  $db  = db_ro();
  $out = [];
  foreach (captures_for_items($db, $ids) as $item_id => $rows) {
    $c = $rows[0];
    $out[] = [
      'item_id' => (int)$item_id,
      'id'      => (int)$c['id'],
      'status'  => (string)$c['status'],
      'label'   => capture_badge($c),
      'error'   => (string)($c['error'] ?? ''),
      'when'    => fmt_dt((string)($c['finished_at'] ?: $c['requested_at'])),
    ];
  }
  cap_json(['captures' => $out]);
}

/* ===================== GET ?id : consegna il file ===================== */
$id = (int)($_GET['id'] ?? 0);
if ($id <= 0) { http_response_code(400); die('id mancante'); }
$thumb = !empty($_GET['thumb']);

$db = db_ro();
if (!captures_table_exists($db)) { http_response_code(404); die('nessuna cattura'); }

$st = $db->prepare("SELECT id, item_id, status, png_path FROM captures WHERE id = :i");
$st->bindValue(':i', $id, SQLITE3_INTEGER);
$c = $st->execute()->fetchArray(SQLITE3_ASSOC);

if (!$c || (string)$c['status'] !== 'done' || empty($c['png_path'])) {
  http_response_code(404);
  die('Cattura non disponibile.');
}

$base = captures_dir();
$rel  = (string)$c['png_path'];
if ($thumb) $rel = preg_replace('~\.png$~', '_thumb.jpg', $rel);

// png_path lo scrive il worker, ma la verifica resta: il percorso risolto deve
// cadere dentro la cartella delle catture, altrimenti un valore anomalo nel DB
// diventerebbe una lettura arbitraria di file.
$full = realpath($base . '/' . $rel);
$root = realpath($base);
if ($full === false || $root === false || !str_starts_with($full, $root . '/')) {
  http_response_code(404);
  die('File non trovato.');
}

$type = $thumb ? 'image/jpeg' : 'image/png';
$name = 'rssintel-item' . (int)$c['item_id'] . '-cattura' . (int)$c['id']
      . ($thumb ? '.jpg' : '.png');

header('Content-Type: ' . $type);
header('Content-Length: ' . (string)filesize($full));
header('Content-Disposition: inline; filename="' . $name . '"');
header('X-Content-Type-Options: nosniff');
// Niente Cache-Control qui: il vhost impone `no-store` su tutti i .php con
// `Header always set`, che sovrascriverebbe qualunque valore impostato da PHP.
// Va bene cosi': sono immagini dietro autenticazione, non tenerle in cache su
// disco e' la scelta prudente. Le miniature pesano poche decine di KB e il PNG
// intero si scarica solo quando lo si apre.
readfile($full);
