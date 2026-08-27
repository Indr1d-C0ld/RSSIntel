<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

session_start();
if (!isset($_SESSION['csrf'])) $_SESSION['csrf'] = bin2hex(random_bytes(16));

function out(array $x, int $code=200): void {
  http_response_code($code);
  header('Content-Type: application/json; charset=utf-8');
  echo json_encode($x, JSON_UNESCAPED_UNICODE);
  exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') out(['ok'=>false,'error'=>'POST richiesto'], 405);

$csrf = (string)($_POST['csrf'] ?? '');
if (!$csrf || !hash_equals($_SESSION['csrf'], $csrf)) out(['ok'=>false,'error'=>'CSRF non valido'], 403);

$action = (string)($_POST['action'] ?? '');
$me = current_user();
$admin = is_admin($me);

$db = db_rw();
$db->exec("PRAGMA journal_mode=WAL;");
$db->exec("PRAGMA foreign_keys=ON;");

if ($action === 'add') {
  $item_id_raw = trim((string)($_POST['item_id'] ?? ''));
  $note = trim((string)($_POST['note'] ?? ''));
  $quote = trim((string)($_POST['quote'] ?? ''));
  $tags_raw = trim((string)($_POST['tags'] ?? ''));

  if ($item_id_raw === '' || !ctype_digit($item_id_raw) || $note === '') {
    out(['ok'=>false,'error'=>'item_id/note mancanti o non validi'], 400);
  }
  $item_id = (int)$item_id_raw;

  // opzionale: verifica esistenza item
  $exists = $db->querySingle("SELECT 1 FROM items WHERE id=".(int)$item_id);
  if (!$exists) out(['ok'=>false,'error'=>'item non trovato'], 404);

  $stmt = $db->prepare("INSERT INTO annotations(item_id, note, quote, author) VALUES(:i,:n,:q,:a)");
  $stmt->bindValue(':i', $item_id, SQLITE3_INTEGER);
  $stmt->bindValue(':n', $note, SQLITE3_TEXT);
  if ($quote === '') $stmt->bindValue(':q', null, SQLITE3_NULL);
  else $stmt->bindValue(':q', $quote, SQLITE3_TEXT);
  $stmt->bindValue(':a', $me, SQLITE3_TEXT);
  $stmt->execute();

  $annotation_id = (int)$db->lastInsertRowID();

  // parsing tags: "a, b, c" -> ["a","b","c"], normalizzati
  if ($tags_raw !== '') {
    $parts = preg_split('/,/', $tags_raw);
    $tags = [];
    foreach ($parts as $t) {
      $t = trim(mb_strtolower($t, 'UTF-8'));
      $t = preg_replace('/[^\p{L}\p{N}\s\-_]/u', '', $t);
      $t = preg_replace('/\s+/', ' ', $t);
      if ($t !== '' && mb_strlen($t, 'UTF-8') <= 40) {
        $tags[$t] = true; // uniq
      }
    }

    if ($tags) {
      foreach (array_keys($tags) as $tag_name) {
        $st = $db->prepare("INSERT OR IGNORE INTO tags(name) VALUES(:n)");
        $st->bindValue(':n', $tag_name, SQLITE3_TEXT);
        $st->execute();

        $tag_id = (int)$db->querySingle(
          "SELECT id FROM tags WHERE name=" . "'" . SQLite3::escapeString($tag_name) . "'"
        );

        if ($tag_id > 0) {
          $st2 = $db->prepare("INSERT OR IGNORE INTO annotation_tags(annotation_id, tag_id) VALUES(:a,:t)");
          $st2->bindValue(':a', $annotation_id, SQLITE3_INTEGER);
          $st2->bindValue(':t', $tag_id, SQLITE3_INTEGER);
          $st2->execute();
        }
      }
    }
  }

  out(['ok'=>true]);
}

if ($action === 'delete') {
  $id = (int)($_POST['id'] ?? 0);
  if ($id <= 0) out(['ok'=>false,'error'=>'id non valido'], 400);

  $row = $db->querySingle("SELECT author FROM annotations WHERE id=".(int)$id, true);
  if (!$row) out(['ok'=>false,'error'=>'annotazione non trovata'], 404);
  if (!$admin && (string)$row['author'] !== $me) out(['ok'=>false,'error'=>'non autorizzato'], 403);

  $db->exec("DELETE FROM annotations WHERE id=".(int)$id);
  out(['ok'=>true]);
}

if ($action === 'edit') {
  $id = (int)($_POST['id'] ?? 0);
  $note = trim((string)($_POST['note'] ?? ''));
  $quote = trim((string)($_POST['quote'] ?? ''));

  if ($id <= 0 || $note === '') out(['ok'=>false,'error'=>'id/note non validi'], 400);

  $row = $db->querySingle("SELECT author FROM annotations WHERE id=".(int)$id, true);
  if (!$row) out(['ok'=>false,'error'=>'annotazione non trovata'], 404);
  if (!$admin && (string)$row['author'] !== $me) out(['ok'=>false,'error'=>'non autorizzato'], 403);

  $stmt = $db->prepare("UPDATE annotations SET note=:n, quote=:q, updated_at=datetime('now') WHERE id=:id");
  $stmt->bindValue(':n', $note, SQLITE3_TEXT);
  if ($quote === '') $stmt->bindValue(':q', null, SQLITE3_NULL);
  else $stmt->bindValue(':q', $quote, SQLITE3_TEXT);
  $stmt->bindValue(':id', $id, SQLITE3_INTEGER);
  $stmt->execute();

  out(['ok'=>true]);
}

out(['ok'=>false,'error'=>'action non valida'], 400);
