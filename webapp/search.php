<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';
require_login();

$me = current_user();

function saved_searches_table(SQLite3 $db): bool {
  return (bool)$db->querySingle(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='saved_searches'"
  );
}

function saved_searches_schema(): string {
  return "
    CREATE TABLE IF NOT EXISTS saved_searches (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      owner        TEXT NOT NULL,
      name         TEXT NOT NULL,
      q            TEXT NOT NULL,
      feed_id      INTEGER,
      result_limit INTEGER,
      created_at   TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE(owner, name)
    );
    CREATE INDEX IF NOT EXISTS idx_saved_searches_owner ON saved_searches(owner, name);
  ";
}

/* ===== POST: salva / elimina una ricerca ===== */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) {
    http_response_code(403);
    die('CSRF non valido');
  }

  $action    = (string)($_POST['action'] ?? '');
  $ret_q     = trim((string)($_POST['ret_q'] ?? ''));
  $ret_feed  = trim((string)($_POST['ret_feed'] ?? ''));
  $ret_limit = (int)($_POST['ret_limit'] ?? 50);
  if ($ret_limit <= 0) $ret_limit = 50;
  if ($ret_limit > 200) $ret_limit = 200;

  try {
    $dbw = db_rw();
    $dbw->exec(saved_searches_schema());

    if ($action === 'save') {
      $name = trim((string)($_POST['name'] ?? ''));
      $sq   = trim((string)($_POST['q'] ?? ''));
      $sfid = trim((string)($_POST['feed_id'] ?? ''));
      $slim = (int)($_POST['limit'] ?? 50);
      if ($slim <= 0) $slim = 50;
      if ($slim > 200) $slim = 200;

      if ($name === '' || $sq === '') {
        $_SESSION['flash'] = ['err', 'Nome e query sono obbligatori.'];
      } elseif (mb_strlen($name, 'UTF-8') > 80) {
        $_SESSION['flash'] = ['err', 'Nome troppo lungo (max 80 caratteri).'];
      } else {
        $st = $dbw->prepare("
          INSERT INTO saved_searches(owner, name, q, feed_id, result_limit)
          VALUES(:o, :n, :q, :f, :l)
          ON CONFLICT(owner, name) DO UPDATE SET
            q = excluded.q,
            feed_id = excluded.feed_id,
            result_limit = excluded.result_limit,
            created_at = datetime('now')
        ");
        $st->bindValue(':o', $me, SQLITE3_TEXT);
        $st->bindValue(':n', $name, SQLITE3_TEXT);
        $st->bindValue(':q', $sq, SQLITE3_TEXT);
        if ($sfid !== '' && ctype_digit($sfid)) {
          $st->bindValue(':f', (int)$sfid, SQLITE3_INTEGER);
        } else {
          $st->bindValue(':f', null, SQLITE3_NULL);
        }
        $st->bindValue(':l', $slim, SQLITE3_INTEGER);
        $st->execute();
        $_SESSION['flash'] = ['ok', 'Ricerca «' . $name . '» salvata.'];
      }
    } elseif ($action === 'delete') {
      $id = (int)($_POST['id'] ?? 0);
      if ($id > 0) {
        $st = $dbw->prepare("DELETE FROM saved_searches WHERE id = :id AND owner = :o");
        $st->bindValue(':id', $id, SQLITE3_INTEGER);
        $st->bindValue(':o', $me, SQLITE3_TEXT);
        $st->execute();
        $_SESSION['flash'] = ['ok', 'Ricerca eliminata.'];
      }
    }
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }

  // PRG: torna alla stessa ricerca in GET
  $qs = http_build_query(array_filter([
    'q'       => $ret_q,
    'feed_id' => ($ret_feed !== '' && ctype_digit($ret_feed)) ? $ret_feed : '',
    'limit'   => $ret_limit,
  ], static fn($v) => $v !== '' && $v !== null));
  header('Location: search.php' . ($qs !== '' ? '?' . $qs : ''));
  exit;
}

$db = db_ro();

$q = trim((string)($_GET['q'] ?? ''));
$feed_id = trim((string)($_GET['feed_id'] ?? ''));
$limit = (int)($_GET['limit'] ?? 50);
if ($limit <= 0) $limit = 50;
if ($limit > 200) $limit = 200;
$page = (isset($_GET['page']) && ctype_digit((string)$_GET['page'])) ? max(1, (int)$_GET['page']) : 1;
$total = 0;
$pages = 0;
$has_feed = ($feed_id !== '' && ctype_digit($feed_id));

$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);

/* ===== ricerche salvate dell'utente corrente ===== */
$saved = [];
if (saved_searches_table($db)) {
  $st = $db->prepare("
    SELECT id, name, q, feed_id, result_limit, created_at
    FROM saved_searches
    WHERE owner = :o
    ORDER BY name COLLATE NOCASE ASC
  ");
  $st->bindValue(':o', $me, SQLITE3_TEXT);
  $rs = $st->execute();
  while ($row = $rs->fetchArray(SQLITE3_ASSOC)) $saved[] = $row;
}

$feeds = [];
$resf = $db->query("
  SELECT id, COALESCE(title, url) AS name, url, enabled
  FROM feeds
  ORDER BY enabled DESC, name ASC
");
if ($resf !== false) {
  while ($r = $resf->fetchArray(SQLITE3_ASSOC)) {
    $feeds[] = $r;
  }
}

$rows = [];
$err = '';

if ($q !== '') {
  try {
    $test = $db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items_fts'");
    if (!$test) {
      throw new RuntimeException("Tabella FTS items_fts non trovata nel DB.");
    }

    $feed_clause = $has_feed ? " AND i.feed_id = :fid " : "";

    // Conteggio totale (per la paginazione).
    $cst = $db->prepare("
      SELECT COUNT(*) AS c
      FROM items_fts JOIN items i ON i.id = items_fts.rowid
      WHERE items_fts MATCH :q $feed_clause
    ");
    $cst->bindValue(':q', $q, SQLITE3_TEXT);
    if ($has_feed) $cst->bindValue(':fid', (int)$feed_id, SQLITE3_INTEGER);
    $cres = $cst->execute();
    $total = ($cres === false) ? 0 : (int)$cres->fetchArray(SQLITE3_ASSOC)['c'];

    $pages = ($limit > 0) ? (int)ceil($total / $limit) : 0;
    if ($pages > 0 && $page > $pages) $page = $pages;
    $offset = ($page - 1) * $limit;

    // Esegue la SELECT dei risultati con l'espressione snippet passata.
    $run = function (string $snip_expr)
        use ($db, $q, $has_feed, $feed_id, $feed_clause, $limit, $offset) {
      $sql = "
        SELECT i.id, i.title, i.link, i.published_at, i.fetched_at,
               COALESCE(f.title, f.url) AS feed_title,
               $snip_expr AS snip
        FROM items_fts
        JOIN items i ON i.id = items_fts.rowid
        JOIN feeds f ON f.id = i.feed_id
        WHERE items_fts MATCH :q $feed_clause
        ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
        LIMIT :limit OFFSET :offset
      ";
      $st = $db->prepare($sql);
      if ($st === false) throw new RuntimeException("Prepare fallita: " . $db->lastErrorMsg());
      $st->bindValue(':q', $q, SQLITE3_TEXT);
      if ($has_feed) $st->bindValue(':fid', (int)$feed_id, SQLITE3_INTEGER);
      $st->bindValue(':limit', $limit, SQLITE3_INTEGER);
      $st->bindValue(':offset', $offset, SQLITE3_INTEGER);
      return $st->execute();
    };

    $res = $run("snippet(items_fts, 1, char(2), char(3), '…', 24)");
    if ($res === false && stripos($db->lastErrorMsg(), 'snippet') !== false) {
      $res = $run("''"); // FTS senza testo conservato: nessun estratto
    }
    if ($res === false) {
      throw new RuntimeException("Execute fallita: " . $db->lastErrorMsg());
    }

    while ($r = $res->fetchArray(SQLITE3_ASSOC)) $rows[] = $r;
  } catch (Throwable $e) {
    $err = $e->getMessage();
  }
}

/** URL di una pagina dei risultati, preservando q / feed / limite. */
function page_url(int $n, string $q, string $feed_id, int $limit): string {
  return 'search.php?' . http_build_query(array_filter([
    'q'       => $q,
    'feed_id' => ($feed_id !== '' && ctype_digit($feed_id)) ? $feed_id : '',
    'limit'   => $limit,
    'page'    => $n,
  ], static fn($v) => $v !== '' && $v !== null));
}

/** URL di richiamo di una ricerca salvata */
function saved_url(array $s): string {
  $p = ['q' => (string)$s['q']];
  if ($s['feed_id'] !== null && $s['feed_id'] !== '') $p['feed_id'] = (int)$s['feed_id'];
  if (!empty($s['result_limit'])) $p['limit'] = (int)$s['result_limit'];
  return 'search.php?' . http_build_query($p);
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css?v=<?= @filemtime(__DIR__ . "/assets/style.css") ?>">
<title>RSSIntel — Ricerca</title>

<?php render_header('RSSIntel — Ricerca', 'search'); ?>

<div class="wrap">
  <?php if ($flash): ?>
    <div class="card">
      <b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?>
    </div>
  <?php endif; ?>

  <?php if ($saved): ?>
    <div class="card">
      <b>Ricerche salvate</b>
      <hr>
      <?php foreach ($saved as $s): ?>
        <div class="row" style="justify-content:space-between; margin:6px 0">
          <div class="grow">
            <a href="<?=h(saved_url($s))?>"><b><?=h((string)$s['name'])?></b></a>
            <span class="meta">
              — <code><?=h((string)$s['q'])?></code>
              <?php if ($s['feed_id'] !== null): ?> · feed #<?= (int)$s['feed_id'] ?><?php endif; ?>
              <?php if (!empty($s['result_limit'])): ?> · limite <?= (int)$s['result_limit'] ?><?php endif; ?>
            </span>
          </div>
          <form method="post" onsubmit="return confirm('Eliminare la ricerca «<?=h((string)$s['name'])?>»?')">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="delete">
            <input type="hidden" name="id" value="<?= (int)$s['id'] ?>">
            <input type="hidden" name="ret_q" value="<?=h($q)?>">
            <input type="hidden" name="ret_feed" value="<?=h($feed_id)?>">
            <input type="hidden" name="ret_limit" value="<?=$limit?>">
            <button class="btn" type="submit">Elimina</button>
          </form>
        </div>
      <?php endforeach; ?>
    </div>
  <?php endif; ?>

  <form method="get" class="card">
    <div class="row">
      <input
        class="grow"
        name="q"
        value="<?=h($q)?>"
        placeholder='Query FTS5 (es: "black sea" OR drone AND strike)'
        autofocus
      >

      <select name="feed_id">
        <option value="" <?= $feed_id === '' ? 'selected' : '' ?>>tutti i feed</option>
        <?php foreach ($feeds as $f): ?>
          <option value="<?= (int)$f['id'] ?>" <?= ($feed_id !== '' && (int)$feed_id === (int)$f['id']) ? 'selected' : '' ?>>
            <?=h((string)$f['name'])?><?= ((int)$f['enabled'] === 0) ? ' (disabilitato)' : '' ?>
          </option>
        <?php endforeach; ?>
      </select>

      <select name="limit">
        <?php foreach ([25, 50, 100, 200] as $n): ?>
          <option value="<?=$n?>" <?= $limit === $n ? 'selected' : '' ?>><?=$n?></option>
        <?php endforeach; ?>
      </select>

      <button class="btn" type="submit">Cerca</button>
    </div>

    <div class="meta form-help">
      Esempi:
      <span class="badge">title:ukraine</span>
      <span class="badge">nato AND baltic</span>
      <span class="badge">"black sea"</span>
      <span class="badge">feed:reuters</span>
    </div>
  </form>

  <?php if ($err): ?>
    <div class="card">
      <b>Errore query:</b> <?=h($err)?>
    </div>
  <?php endif; ?>

  <?php if ($q !== ''): ?>
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div class="grow">
          <b><?=$total?> risultati</b>
          <div class="meta">
            <?php if ($pages > 1): ?>pagina <?=$page?> di <?=$pages?> · <?php endif; ?>
            <?=$limit?> per pagina
            <?= $has_feed ? ' · feed_id: ' . h($feed_id) : '' ?>
          </div>
        </div>

        <form method="post" class="row" style="gap:6px">
          <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
          <input type="hidden" name="action" value="save">
          <input type="hidden" name="q" value="<?=h($q)?>">
          <input type="hidden" name="feed_id" value="<?=h($feed_id)?>">
          <input type="hidden" name="limit" value="<?=$limit?>">
          <input type="hidden" name="ret_q" value="<?=h($q)?>">
          <input type="hidden" name="ret_feed" value="<?=h($feed_id)?>">
          <input type="hidden" name="ret_limit" value="<?=$limit?>">
          <input name="name" maxlength="80" placeholder="nome ricerca…" required>
          <button class="btn" type="submit">Salva questa ricerca</button>
        </form>
      </div>

      <hr>

      <?php if (!$rows): ?>
        <div class="meta">Nessun risultato.</div>
      <?php endif; ?>

      <?php foreach ($rows as $r): ?>
        <div class="result-entry">
          <div class="row">
            <div class="grow">
              <div class="row">
                <a href="item.php?id=<?=urlencode((string)$r['id'])?>"><b><?=h((string)$r['id'])?></b></a>
                <?php if (!empty($r['feed_title'])): ?>
                  <span class="badge"><?=h((string)$r['feed_title'])?></span>
                <?php endif; ?>
              </div>

              <?php if (!empty($r['title'])): ?>
                <div class="small result-title"><b><?=h((string)$r['title'])?></b></div>
              <?php endif; ?>

              <div class="meta result-meta">
                <?=h(fmt_dt((string)($r['published_at'] ?: $r['fetched_at'])))?>
                <?php if (!empty($r['link'])): ?>
                  · <a href="<?=h((string)$r['link'])?>" target="_blank">Apri fonte</a>
                <?php endif; ?>
              </div>

              <?php if (!empty($r['snip'])): ?>
                <?php // snippet(): delimitatori char(2)/char(3), escape completo, poi <mark> reali ?>
                <div class="small result-snippet"><?= str_replace(["\x02", "\x03"], ['<mark>', '</mark>'], h((string)$r['snip'])) ?></div>
              <?php endif; ?>
            </div>
          </div>
        </div>
        <hr>
      <?php endforeach; ?>

      <?php if ($pages > 1): ?>
        <div class="btns" style="justify-content:center; margin-top:16px">
          <?php if ($page > 1): ?>
            <a class="btn" href="<?=h(page_url($page - 1, $q, $feed_id, $limit))?>">◀ Prec</a>
          <?php endif; ?>
          <?php for ($p = max(1, $page - 2); $p <= min($pages, $page + 2); $p++): ?>
            <a class="btn <?= $p === $page ? 'active' : '' ?>"
               href="<?=h(page_url($p, $q, $feed_id, $limit))?>"><?=$p?></a>
          <?php endfor; ?>
          <?php if ($page < $pages): ?>
            <a class="btn" href="<?=h(page_url($page + 1, $q, $feed_id, $limit))?>">Succ ▶</a>
          <?php endif; ?>
        </div>
      <?php endif; ?>
    </div>
  <?php endif; ?>
</div>
