<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

/**
 * Novità: gli articoli nuovi che corrispondono alle ricerche salvate con
 * l'allerta attiva. Le corrispondenze le trova rssintel_watch.py dopo ogni
 * giro del fetcher; qui si leggono e si segnano come viste.
 *
 * "Visto" e' un'azione esplicita, non un effetto dell'apertura della pagina:
 * altrimenti basterebbe passarci per sbaglio per perdere le segnalazioni.
 */

require_login();
$me = current_user();

/* ===== POST: segna come visto ===== */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }
  $action = (string)($_POST['action'] ?? '');
  try {
    $dbw = db_rw();
    watch_ensure($dbw);
    if ($action === 'seen_search') {
      $sid = (int)($_POST['search_id'] ?? 0);
      $st = $dbw->prepare("
        UPDATE watch_hits SET seen = 1
        WHERE seen = 0 AND search_id = :s
          AND search_id IN (SELECT id FROM saved_searches WHERE owner = :o)
      ");
      $st->bindValue(':s', $sid, SQLITE3_INTEGER);
      $st->bindValue(':o', $me, SQLITE3_TEXT);
      $st->execute();
      $_SESSION['flash'] = ['ok', $dbw->changes() . ' segnalazioni segnate come viste.'];
    } elseif ($action === 'seen_all') {
      $st = $dbw->prepare("
        UPDATE watch_hits SET seen = 1
        WHERE seen = 0 AND search_id IN (SELECT id FROM saved_searches WHERE owner = :o)
      ");
      $st->bindValue(':o', $me, SQLITE3_TEXT);
      $st->execute();
      $_SESSION['flash'] = ['ok', $dbw->changes() . ' segnalazioni segnate come viste.'];
    }
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }
  header('Location: novita.php' . (!empty($_POST['tutte']) ? '?tutte=1' : ''));
  exit;
}

$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);
$tutte = !empty($_GET['tutte']);   // includere anche le gia' viste (ultimi 7 giorni)

$db = db_ro();
$ready = watch_table_exists($db);

/* ===== allerte dell'utente ===== */
$watches = [];
if ($ready) {
  $st = $db->prepare("
    SELECT id, name, q, feed_id, last_checked_at, last_error
    FROM saved_searches WHERE owner = :o AND watch = 1
    ORDER BY name COLLATE NOCASE
  ");
  $st->bindValue(':o', $me, SQLITE3_TEXT);
  $r = $st->execute();
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $watches[(int)$x['id']] = $x;
}

/* ===== corrispondenze, in una query per tutte le allerte ===== */
$hits = [];          // search_id => [righe]
$unseen = [];        // search_id => n
if ($ready) {
  $st = $db->prepare("
    SELECT w.search_id, w.item_id, w.found_at, w.seen,
           i.title, i.link, i.published_at, i.fetched_at,
           COALESCE(f.title, f.url) AS feed_title
           " . source_select_cols($db) . "
    FROM watch_hits w
    JOIN saved_searches s ON s.id = w.search_id
    JOIN items i ON i.id = w.item_id
    JOIN feeds f ON f.id = i.feed_id
    WHERE s.owner = :o
      AND (w.seen = 0 OR (:tutte = 1 AND w.found_at >= datetime('now', '-7 days')))
    ORDER BY w.seen ASC, COALESCE(i.published_at, i.fetched_at) DESC
  ");
  $st->bindValue(':o', $me, SQLITE3_TEXT);
  $st->bindValue(':tutte', $tutte ? 1 : 0, SQLITE3_INTEGER);
  $r = $st->execute();
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) {
    $sid = (int)$x['search_id'];
    $hits[$sid][] = $x;
    if ((int)$x['seen'] === 0) $unseen[$sid] = ($unseen[$sid] ?? 0) + 1;
  }
}
$tot_unseen = array_sum($unseen);

function saved_search_url(array $s): string {
  $p = ['q' => (string)$s['q']];
  if ($s['feed_id'] !== null && $s['feed_id'] !== '') $p['feed_id'] = (int)$s['feed_id'];
  return 'search.php?' . http_build_query($p);
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
<title>RSSIntel — Novità</title>

<?php render_header('RSSIntel — Novità', 'novita'); ?>

<div class="wrap">
  <?php if ($flash): ?>
    <div class="card"><b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?></div>
  <?php endif; ?>

  <div class="card">
    <div class="row" style="justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap">
      <div class="grow">
        <b><?= $tot_unseen ?> nuove segnalazioni</b>
        <span class="meta">da <?= count($watches) ?> allerte attive</span>
      </div>
      <div class="row" style="gap:6px">
        <a class="btn" href="novita.php<?= $tutte ? '' : '?tutte=1' ?>">
          <?= $tutte ? 'Solo le nuove' : 'Anche le già viste (7 gg)' ?>
        </a>
        <?php if ($tot_unseen > 0): ?>
          <form method="post">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="seen_all">
            <input type="hidden" name="tutte" value="<?= $tutte ? 1 : 0 ?>">
            <button class="btn" type="submit">✓ Segna tutto come visto</button>
          </form>
        <?php endif; ?>
      </div>
    </div>
    <?php if (!$watches): ?>
      <hr>
      <div class="meta">
        Nessuna allerta attiva. In <a href="search.php">Ricerca</a>, salva una
        ricerca con «🔔 avvisami dei nuovi», oppure accendi l'allerta su una
        ricerca gia' salvata: dopo ogni raccolta (ogni 15 minuti) i nuovi articoli
        che corrispondono compariranno qui.
      </div>
    <?php endif; ?>
  </div>

  <?php foreach ($watches as $sid => $w): ?>
    <?php $rows = $hits[$sid] ?? []; $n = $unseen[$sid] ?? 0; ?>
    <div class="card" id="s<?= $sid ?>">
      <div class="row" style="justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap">
        <div class="grow">
          <b>🔔 <?=h((string)$w['name'])?></b>
          <?php if ($n > 0): ?><span class="badge red-stamp"><?= $n ?> nuove</span><?php endif; ?>
          <div class="meta" style="margin-top:4px">
            <code><?=h((string)$w['q'])?></code>
            · <a href="<?=h(saved_search_url($w))?>">tutti i risultati</a>
            <?php if (!empty($w['last_checked_at'])): ?>
              · ultimo controllo <?=h(fmt_dt((string)$w['last_checked_at']))?>
            <?php endif; ?>
          </div>
          <?php if (!empty($w['last_error'])): ?>
            <div class="meta" style="color:var(--red-stamp); margin-top:4px">
              ⚠ La query non e' valida per l'indice e non viene controllata:
              <?=h((string)$w['last_error'])?>
            </div>
          <?php endif; ?>
        </div>
        <?php if ($n > 0): ?>
          <form method="post">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="seen_search">
            <input type="hidden" name="search_id" value="<?= $sid ?>">
            <input type="hidden" name="tutte" value="<?= $tutte ? 1 : 0 ?>">
            <button class="btn" type="submit">✓ Viste</button>
          </form>
        <?php endif; ?>
      </div>

      <?php if (!$rows): ?>
        <div class="meta" style="margin-top:8px">Niente di nuovo.</div>
      <?php else: ?>
        <hr>
        <?php foreach ($rows as $x): ?>
          <div class="result-entry" style="<?= (int)$x['seen'] === 1 ? 'opacity:.6' : '' ?>">
            <div class="row" style="gap:6px; flex-wrap:wrap">
              <a href="item.php?id=<?= (int)$x['item_id'] ?>"><b><?= (int)$x['item_id'] ?></b></a>
              <?php if (!empty($x['feed_title'])): ?><span class="badge"><?=h((string)$x['feed_title'])?></span><?php endif; ?>
              <?= source_badge($x['src_category'] ?? null, $x['src_reliability'] ?? null) ?>
              <?php if ((int)$x['seen'] === 0): ?><span class="badge red-stamp">nuovo</span><?php endif; ?>
            </div>
            <?php if (!empty($x['title'])): ?>
              <div class="small result-title"><b><?=h((string)$x['title'])?></b></div>
            <?php endif; ?>
            <div class="meta result-meta">
              <?=h(fmt_dt((string)($x['published_at'] ?: $x['fetched_at'])))?>
              · segnalato <?=h(fmt_dt((string)$x['found_at']))?>
              <?php if ($u = safe_url((string)$x['link'])): ?>
                · <a href="<?=$u?>" target="_blank" rel="noopener noreferrer">Apri fonte</a>
              <?php endif; ?>
            </div>
          </div>
          <hr>
        <?php endforeach; ?>
      <?php endif; ?>
    </div>
  <?php endforeach; ?>
</div>
