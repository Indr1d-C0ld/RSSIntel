<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_login();
$me = current_user();

/** Redirect solo verso una pagina .php locale (anti open-redirect). */
function fav_safe_ret(string $n): string {
  if (preg_match('~([A-Za-z0-9_]+\.php(?:\?[^#\s]*)?)$~', trim($n), $m)) {
    return $m[1];
  }
  return 'favorites.php';
}

/* ===== POST: aggiungi / rimuovi / annota ===== */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }

  $action  = (string)($_POST['action'] ?? '');
  $item_id = (int)($_POST['item_id'] ?? 0);
  $ret     = fav_safe_ret((string)($_POST['ret'] ?? ''));

  try {
    $dbw = db_rw();
    favorites_ensure($dbw);

    if ($action === 'add' && $item_id > 0) {
      if (!$dbw->querySingle("SELECT 1 FROM items WHERE id = " . $item_id)) {
        throw new RuntimeException('Articolo inesistente.');
      }
      $note = trim((string)($_POST['note'] ?? ''));
      $st = $dbw->prepare(
        "INSERT INTO favorites(owner, item_id, note) VALUES(:o, :i, :n)
         ON CONFLICT(owner, item_id) DO NOTHING"
      );
      $st->bindValue(':o', $me, SQLITE3_TEXT);
      $st->bindValue(':i', $item_id, SQLITE3_INTEGER);
      $st->bindValue(':n', $note, SQLITE3_TEXT);
      $st->execute();
      $_SESSION['flash'] = ['ok', 'Aggiunto ai favoriti.'];
    }

    elseif ($action === 'remove' && $item_id > 0) {
      $st = $dbw->prepare("DELETE FROM favorites WHERE owner = :o AND item_id = :i");
      $st->bindValue(':o', $me, SQLITE3_TEXT);
      $st->bindValue(':i', $item_id, SQLITE3_INTEGER);
      $st->execute();
      $_SESSION['flash'] = ['ok', 'Rimosso dai favoriti.'];
    }

    elseif ($action === 'note' && $item_id > 0) {
      $note = trim((string)($_POST['note'] ?? ''));
      if (mb_strlen($note, 'UTF-8') > 4000) {
        throw new RuntimeException('Nota troppo lunga (max 4000 caratteri).');
      }
      $st = $dbw->prepare("UPDATE favorites SET note = :n WHERE owner = :o AND item_id = :i");
      $st->bindValue(':n', $note, SQLITE3_TEXT);
      $st->bindValue(':o', $me, SQLITE3_TEXT);
      $st->bindValue(':i', $item_id, SQLITE3_INTEGER);
      $st->execute();
      $_SESSION['flash'] = ['ok', $dbw->changes() ? 'Nota salvata.' : 'Articolo non tra i favoriti.'];
    }
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }

  header('Location: ' . $ret);
  exit;
}

$db = db_ro();
$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);

$favs = [];
if ($db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='favorites'")) {
  $st = $db->prepare("
    SELECT fa.item_id, fa.note, fa.created_at,
           i.title, i.link, i.published_at, i.fetched_at,
           COALESCE(f.title, f.url) AS feed_title
    FROM favorites fa
    JOIN items i ON i.id = fa.item_id
    JOIN feeds f ON f.id = i.feed_id
    WHERE fa.owner = :o
    ORDER BY fa.created_at DESC
  ");
  $st->bindValue(':o', $me, SQLITE3_TEXT);
  $rs = $st->execute();
  while ($r = $rs->fetchArray(SQLITE3_ASSOC)) $favs[] = $r;
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css?v=<?= @filemtime(__DIR__ . "/assets/style.css") ?>">
<title>RSSIntel — Favoriti</title>

<?php render_header('RSSIntel — Favoriti', 'favorites'); ?>

<div class="wrap">
  <?php if ($flash): ?>
    <div class="card"><b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?></div>
  <?php endif; ?>

  <div class="card">
    <b><?=count($favs)?> articoli nei favoriti</b>
    <hr>

    <?php if (!$favs): ?>
      <div class="meta">
        Nessun favorito. Aggiungi articoli dal loro dettaglio con il pulsante
        <b>★ Aggiungi ai favoriti</b>.
      </div>
    <?php endif; ?>

    <?php foreach ($favs as $fv): ?>
      <div class="result-entry">
        <div class="row" style="justify-content:space-between">
          <div class="grow">
            <div class="row">
              <a href="item.php?id=<?= (int)$fv['item_id'] ?>"><b><?= (int)$fv['item_id'] ?></b></a>
              <?php if (!empty($fv['feed_title'])): ?>
                <span class="badge"><?=h((string)$fv['feed_title'])?></span>
              <?php endif; ?>
            </div>
            <?php if (!empty($fv['title'])): ?>
              <div class="small result-title"><b><?=h((string)$fv['title'])?></b></div>
            <?php endif; ?>
            <div class="meta result-meta">
              <?=h(fmt_dt((string)($fv['published_at'] ?: $fv['fetched_at'])))?>
              · nei favoriti dal <?=h(fmt_dt((string)$fv['created_at']))?>
              <?php if (!empty($fv['link'])): ?>
                · <a href="<?=h((string)$fv['link'])?>" target="_blank">Apri fonte</a>
              <?php endif; ?>
            </div>
          </div>
          <form method="post" onsubmit="return confirm('Rimuovere dai favoriti?')">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="remove">
            <input type="hidden" name="item_id" value="<?= (int)$fv['item_id'] ?>">
            <input type="hidden" name="ret" value="favorites.php">
            <button class="btn" type="submit">Rimuovi</button>
          </form>
        </div>

        <form method="post" style="margin-top:8px">
          <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
          <input type="hidden" name="action" value="note">
          <input type="hidden" name="item_id" value="<?= (int)$fv['item_id'] ?>">
          <input type="hidden" name="ret" value="favorites.php">
          <textarea name="note" placeholder="nota personale su questo articolo…"
                    style="min-height:60px"><?=h((string)$fv['note'])?></textarea>
          <button class="btn" type="submit" style="margin-top:6px">Salva nota</button>
        </form>
      </div>
      <hr>
    <?php endforeach; ?>
  </div>
</div>
