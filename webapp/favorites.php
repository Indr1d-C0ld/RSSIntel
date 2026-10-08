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
      // Stesso tetto del percorso 'note': prima 'add' non ne aveva nessuno.
      if (mb_strlen($note, 'UTF-8') > FAVORITE_MAX_NOTE) {
        throw new RuntimeException('Nota troppo lunga (max ' . FAVORITE_MAX_NOTE . ' caratteri).');
      }
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
      if (mb_strlen($note, 'UTF-8') > FAVORITE_MAX_NOTE) {
        throw new RuntimeException('Nota troppo lunga (max ' . FAVORITE_MAX_NOTE . ' caratteri).');
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

// Catture di tutti i favoriti in una query sola (niente N+1 nell'elenco).
$caps = captures_for_items($db, array_column($favs, 'item_id'));
$may_capture = can_annotate();
// Gli id con una cattura non ancora conclusa: servono all'aggiornamento in pagina.
$waiting = [];
foreach ($caps as $iid => $rows) {
  if (in_array((string)$rows[0]['status'], ['pending', 'running'], true)) $waiting[] = (int)$iid;
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
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
                <?php if ($u = safe_url((string)$fv['link'])): ?>
                  · <a href="<?=$u?>" target="_blank" rel="noopener noreferrer">Apri fonte</a>
                <?php endif; ?>
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

        <?php
          $iid = (int)$fv['item_id'];
          $cap = $caps[$iid][0] ?? null;
          $ncap = count($caps[$iid] ?? []);
        ?>
        <div class="row cap-row" data-item="<?=$iid?>" style="margin-top:8px; gap:10px; align-items:flex-start">
          <?php if ($cap && (string)$cap['status'] === 'done'): ?>
            <a href="capture.php?id=<?= (int)$cap['id'] ?>" target="_blank" rel="noopener"
               title="Apri la cattura a piena pagina">
              <img src="capture.php?id=<?= (int)$cap['id'] ?>&amp;thumb=1" alt="cattura"
                   style="width:110px; border:1px solid var(--border); display:block">
            </a>
          <?php endif; ?>
          <div class="grow">
            <div class="meta cap-state">
              <?php if (!$cap): ?>
                Nessuna cattura della pagina originale.
              <?php elseif ((string)$cap['status'] === 'done'): ?>
                📷 Cattura del <?=h(fmt_dt((string)$cap['finished_at']))?>
                <?php if ($ncap > 1): ?> · <?= $ncap ?> versioni<?php endif; ?>
                <?php if (!empty($cap['width'])): ?>
                  · <?= (int)$cap['width'] ?>×<?= (int)$cap['height'] ?>
                <?php endif; ?>
                <?php if (!empty($cap['error'])): ?>
                  · <span style="color:var(--red-stamp)"><?=h((string)$cap['error'])?></span>
                <?php endif; ?>
              <?php elseif ((string)$cap['status'] === 'error'): ?>
                <span style="color:var(--red-stamp)">⚠ Cattura fallita</span>
                — <?=h((string)($cap['error'] ?: 'motivo non registrato'))?>
              <?php else: ?>
                ⏳ Cattura <?=h(capture_badge($cap))?>…
              <?php endif; ?>
            </div>
            <?php if ($may_capture): ?>
              <div class="row" style="margin-top:6px; gap:6px; flex-wrap:wrap">
                <form method="post" action="capture.php">
                  <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
                  <input type="hidden" name="action" value="enqueue">
                  <input type="hidden" name="item_id" value="<?=$iid?>">
                  <input type="hidden" name="ret" value="favorites.php">
                  <button class="btn" type="submit"
                    <?= ($cap && in_array((string)$cap['status'], ['pending','running'], true)) ? 'disabled' : '' ?>>
                    <?= $cap && (string)$cap['status'] === 'done' ? '↻ Ricattura' : '📷 Cattura' ?>
                  </button>
                </form>
                <?php
                  // Eliminabile dall'autore della richiesta o da un admin,
                  // stessa regola delle annotazioni. Non mentre e' in corso.
                  $puo_eliminare = $cap
                    && (string)$cap['status'] !== 'running'
                    && (is_admin() || (string)$cap['requested_by'] === $me);
                ?>
                <?php if ($puo_eliminare): ?>
                  <form method="post" action="capture.php"
                        onsubmit="return confirm('Eliminare questa cattura? Il file viene rimosso dal disco.')">
                    <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="capture_id" value="<?= (int)$cap['id'] ?>">
                    <input type="hidden" name="ret" value="favorites.php">
                    <button class="btn" type="submit">🗑 Elimina cattura</button>
                  </form>
                <?php endif; ?>
                <?php if (($caps[$iid] ?? null) && count($caps[$iid]) > 1): ?>
                  <a class="btn" href="item.php?id=<?=$iid?>">Tutte le versioni (<?= count($caps[$iid]) ?>)</a>
                <?php endif; ?>
              </div>
            <?php endif; ?>
          </div>
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

<?php if ($waiting): ?>
<script>
// Aggiorna in posto le sole righe in attesa. Volutamente NIENTE reload e
// niente meta-refresh: in questa pagina ci sono le textarea delle note, e un
// ricaricamento periodico cancellerebbe quello che si sta scrivendo.
(function () {
  let restanti = new Set(<?= json_encode($waiting) ?>);

  function aggiorna(c) {
    const riga = document.querySelector('.cap-row[data-item="' + c.item_id + '"]');
    if (!riga) return;
    const stato = riga.querySelector('.cap-state');
    const btn = riga.querySelector('button');

    if (c.status === 'done') {
      stato.textContent = '📷 Cattura del ' + c.when;
      if (!riga.querySelector('img')) {
        const a = document.createElement('a');
        a.href = 'capture.php?id=' + c.id;
        a.target = '_blank';
        a.rel = 'noopener';
        a.title = 'Apri la cattura a piena pagina';
        const img = document.createElement('img');
        img.src = 'capture.php?id=' + c.id + '&thumb=1';
        img.alt = 'cattura';
        img.style.cssText = 'width:110px;border:1px solid var(--border);display:block';
        a.appendChild(img);
        riga.insertBefore(a, riga.firstElementChild);
      }
      if (btn) { btn.disabled = false; btn.textContent = '↻ Ricattura'; }
    } else if (c.status === 'error') {
      stato.textContent = '⚠ Cattura fallita — ' + (c.error || 'motivo non registrato');
      stato.style.color = 'var(--red-stamp)';
      if (btn) { btn.disabled = false; btn.textContent = '📷 Cattura'; }
    }
  }

  const tick = async () => {
    if (!restanti.size) return;
    try {
      const r = await fetch('capture.php?status=1&items=' + [...restanti].join(','),
                            { credentials: 'same-origin' });
      const j = await r.json();
      for (const c of (j.captures || [])) {
        if (c.status === 'pending' || c.status === 'running') continue;
        restanti.delete(c.item_id);
        aggiorna(c);
      }
    } catch (e) { /* rete instabile: si riprova al giro dopo */ }
    if (restanti.size) setTimeout(tick, 8000);
  };
  setTimeout(tick, 8000);
})();
</script>
<?php endif; ?>
