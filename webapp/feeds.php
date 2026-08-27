<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_role('admin');

$db = db_rw();
$db->exec("PRAGMA journal_mode=WAL;");
$db->exec("PRAGMA foreign_keys=ON;");

function norm_url(string $u): string {
  $u = trim($u);
  $u = preg_replace('/\s+/', '', $u);
  return $u;
}

function import_feed_row(SQLite3 $db, string $url, ?string $title, int $enabled = 1): bool {
  $url = norm_url($url);
  if ($url === '' || !preg_match('~^https?://~i', $url)) {
    return false;
  }

  $stmt = $db->prepare("
    INSERT OR IGNORE INTO feeds(url, title, enabled, created_at)
    VALUES(:url, :title, :enabled, datetime('now'))
  ");
  $stmt->bindValue(':url', $url, SQLITE3_TEXT);

  if ($title === null || trim($title) === '') {
    $stmt->bindValue(':title', null, SQLITE3_NULL);
  } else {
    $stmt->bindValue(':title', trim($title), SQLITE3_TEXT);
  }

  $stmt->bindValue(':enabled', $enabled ? 1 : 0, SQLITE3_INTEGER);
  $stmt->execute();

  return $db->changes() > 0;
}

/* ===== EXPORT ===== */
$export = trim((string)($_GET['export'] ?? ''));
if ($export === 'json' || $export === 'csv') {
  $rows = [];
  $res = $db->query("
    SELECT id, url, title, enabled, created_at, updated_at
    FROM feeds
    ORDER BY enabled DESC, COALESCE(title, url) ASC
  ");
  while ($r = $res->fetchArray(SQLITE3_ASSOC)) {
    $rows[] = $r;
  }

  $stamp = gmdate('Ymd_His');

  if ($export === 'json') {
    header('Content-Type: application/json; charset=utf-8');
    header('Content-Disposition: attachment; filename="rssintel_feeds_' . $stamp . '.json"');
    echo json_encode([
      'exported_at_utc' => gmdate('c'),
      'count' => count($rows),
      'feeds' => $rows,
    ], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    exit;
  }

  header('Content-Type: text/csv; charset=utf-8');
  header('Content-Disposition: attachment; filename="rssintel_feeds_' . $stamp . '.csv"');

  $out = fopen('php://output', 'w');
  fwrite($out, "\xEF\xBB\xBF");
  fputcsv($out, ['id', 'url', 'title', 'enabled', 'created_at', 'updated_at']);

  foreach ($rows as $r) {
    fputcsv($out, [
      (string)$r['id'],
      (string)$r['url'],
      (string)($r['title'] ?? ''),
      (string)$r['enabled'],
      (string)($r['created_at'] ?? ''),
      (string)($r['updated_at'] ?? ''),
    ]);
  }

  fclose($out);
  exit;
}

/* ===== POST actions ===== */
$msg = '';
$err = '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) {
    $err = 'CSRF non valido';
  } else {
    $action = (string)($_POST['action'] ?? '');

    try {
      if ($action === 'add') {
        $url = norm_url((string)($_POST['url'] ?? ''));
        if ($url === '') {
          throw new RuntimeException('URL mancante');
        }
        if (!preg_match('~^https?://~i', $url)) {
          throw new RuntimeException('URL non valido (usa http/https)');
        }

        $st = $db->prepare("INSERT OR IGNORE INTO feeds(url, enabled) VALUES(:u, 1)");
        $st->bindValue(':u', $url, SQLITE3_TEXT);
        $st->execute();

        $msg = 'Feed aggiunto (o già presente).';
      }

      elseif ($action === 'toggle') {
        $id = (int)($_POST['id'] ?? 0);
        if ($id <= 0) {
          throw new RuntimeException('id non valido');
        }

        $cur = $db->querySingle("SELECT enabled FROM feeds WHERE id=" . (int)$id, true);
        if (!$cur) {
          throw new RuntimeException('feed non trovato');
        }

        $new = ((int)$cur['enabled'] === 1) ? 0 : 1;
        $db->exec("UPDATE feeds SET enabled=" . $new . ", updated_at=datetime('now') WHERE id=" . (int)$id);

        $msg = $new ? 'Feed abilitato.' : 'Feed disabilitato.';
      }

      elseif ($action === 'delete') {
        $id = (int)($_POST['id'] ?? 0);
        if ($id <= 0) {
          throw new RuntimeException('id non valido');
        }

        $db->exec("DELETE FROM feeds WHERE id=" . (int)$id);
        $msg = 'Feed eliminato.';
      }

      elseif ($action === 'import') {
        if (!isset($_FILES['import_file']) || !is_uploaded_file($_FILES['import_file']['tmp_name'])) {
          throw new RuntimeException('File mancante');
        }

        $name = (string)($_FILES['import_file']['name'] ?? '');
        $tmp  = (string)($_FILES['import_file']['tmp_name'] ?? '');
        $ext  = strtolower(pathinfo($name, PATHINFO_EXTENSION));

        $inserted = 0;
        $skipped = 0;

        if ($ext === 'json') {
          $raw = file_get_contents($tmp);
          if ($raw === false) {
            throw new RuntimeException('Impossibile leggere il file JSON');
          }

          $data = json_decode($raw, true);
          if (!is_array($data)) {
            throw new RuntimeException('JSON non valido');
          }

          $feeds = [];
          if (isset($data['feeds']) && is_array($data['feeds'])) {
            $feeds = $data['feeds'];
          } elseif (array_is_list($data)) {
            $feeds = $data;
          } else {
            throw new RuntimeException('Formato JSON non riconosciuto');
          }

          foreach ($feeds as $row) {
            if (!is_array($row)) {
              $skipped++;
              continue;
            }

            $url = (string)($row['url'] ?? '');
            $title = isset($row['title']) ? (string)$row['title'] : null;
            $enabled = isset($row['enabled']) ? (int)$row['enabled'] : 1;

            if (import_feed_row($db, $url, $title, $enabled)) {
              $inserted++;
            } else {
              $skipped++;
            }
          }
        }

        elseif ($ext === 'csv') {
          $fh = fopen($tmp, 'r');
          if (!$fh) {
            throw new RuntimeException('Impossibile leggere il file CSV');
          }

          $headers = fgetcsv($fh);
          if (!$headers) {
            throw new RuntimeException('CSV vuoto o non valido');
          }

          $headers = array_map(static fn($v) => strtolower(trim((string)$v)), $headers);
          $idxUrl = array_search('url', $headers, true);
          $idxTitle = array_search('title', $headers, true);
          $idxEnabled = array_search('enabled', $headers, true);

          if ($idxUrl === false) {
            throw new RuntimeException("CSV privo della colonna 'url'");
          }

          while (($row = fgetcsv($fh)) !== false) {
            $url = (string)($row[$idxUrl] ?? '');
            $title = ($idxTitle !== false) ? (string)($row[$idxTitle] ?? '') : null;
            $enabled = ($idxEnabled !== false) ? (int)($row[$idxEnabled] ?? 1) : 1;

            if (import_feed_row($db, $url, $title, $enabled)) {
              $inserted++;
            } else {
              $skipped++;
            }
          }

          fclose($fh);
        }

        else {
          throw new RuntimeException('Formato non supportato: usa .json oppure .csv');
        }

        $msg = "Import completato. Inseriti: $inserted · Ignorati/non validi: $skipped";
      }
    } catch (Throwable $e) {
      $err = $e->getMessage();
    }
  }
}

/* ===== list ===== */
$rows = [];
$res = $db->query("
  SELECT id, url, title, enabled, last_fetch_at, last_status, last_error, created_at, updated_at
  FROM feeds
  ORDER BY enabled DESC, COALESCE(title, url) ASC
");
while ($r = $res->fetchArray(SQLITE3_ASSOC)) {
  $rows[] = $r;
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css">
<title>Feeds</title>

<?php render_header('Feeds', 'feeds'); ?>

<div class="wrap">
  <?php if ($msg): ?>
    <div class="card">
      <b>OK:</b> <?=h($msg)?>
    </div>
  <?php endif; ?>

  <?php if ($err): ?>
    <div class="card">
      <b>Errore:</b> <?=h($err)?>
    </div>
  <?php endif; ?>

  <div class="card">
    <div class="row">
      <div class="grow"><b>Aggiungi feed</b></div>
      <div class="btns">
        <a class="btn" href="feeds.php?export=json" rel="noopener">Export JSON</a>
        <a class="btn" href="feeds.php?export=csv" rel="noopener">Export CSV</a>
      </div>
    </div>

    <form method="post" class="row" style="margin-top:12px;">
      <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
      <input type="hidden" name="action" value="add">
      <input class="grow" name="url" placeholder="https://... (RSS/Atom)">
      <button class="btn" type="submit">Aggiungi</button>
    </form>

    <hr>

    <b>Importa feeds da file</b>

    <form method="post" enctype="multipart/form-data" class="row" style="margin-top:12px;">
      <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
      <input type="hidden" name="action" value="import">
      <input type="file" name="import_file" accept=".json,.csv,application/json,text/csv">
      <button class="btn" type="submit">Importa</button>
    </form>

    <div class="meta" style="margin-top:8px;">
      Formati supportati: <b>JSON</b> esportato da RSSIntel oppure <b>CSV</b> con almeno la colonna <b>url</b>.
    </div>

    <div class="meta" style="margin-top:8px;">
      Il fetcher Python indicizza solo i feed <b>abilitati</b>.
    </div>
  </div>

  <div class="card">
    <b><?=count($rows)?> feeds</b>
    <hr>

    <?php if (!$rows): ?>
      <div class="meta">Nessun feed configurato.</div>
    <?php endif; ?>

    <?php foreach ($rows as $r): ?>
      <div class="feed-entry">
        <div class="row">
          <div class="grow">
            <b><?=h((string)($r['title'] ?: $r['url']))?></b>
            <div class="row" style="margin-top:6px;">
              <span class="badge">#<?= (int)$r['id'] ?></span>
              <?php if ((int)$r['enabled'] === 1): ?>
                <span class="badge">abilitato</span>
              <?php else: ?>
                <span class="badge">disabilitato</span>
              <?php endif; ?>
            </div>
          </div>

          <div class="btns">
            <form method="post">
              <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
              <input type="hidden" name="action" value="toggle">
              <input type="hidden" name="id" value="<?= (int)$r['id'] ?>">
              <button class="btn" type="submit">
                <?= ((int)$r['enabled'] === 1) ? 'Disabilita' : 'Abilita' ?>
              </button>
            </form>

            <form method="post" onsubmit="return confirm('Eliminare feed #<?= (int)$r['id'] ?>?')">
              <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
              <input type="hidden" name="action" value="delete">
              <input type="hidden" name="id" value="<?= (int)$r['id'] ?>">
              <button class="btn" type="submit">Elimina</button>
            </form>
          </div>
        </div>

        <div class="meta" style="margin-top:8px;">
          <?=h((string)$r['url'])?>
        </div>

        <div class="meta" style="margin-top:8px;">
          last_fetch: <?=h(fmt_dt((string)$r['last_fetch_at']) ?: 'n/d')?> ·
          status: <?=h((string)($r['last_status'] ?? 'n/d'))?>
          <?php if (!empty($r['last_error'])): ?>
            · errore: <?=h((string)$r['last_error'])?>
          <?php endif; ?>
        </div>
      </div>
      <hr>
    <?php endforeach; ?>
  </div>
</div>
