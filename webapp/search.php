<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

$db = db_ro();

$q = trim((string)($_GET['q'] ?? ''));
$feed_id = trim((string)($_GET['feed_id'] ?? ''));
$limit = (int)($_GET['limit'] ?? 50);
if ($limit <= 0) $limit = 50;
if ($limit > 200) $limit = 200;

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

    $sql = "
      SELECT i.id, i.title, i.link, i.published_at, i.fetched_at,
             COALESCE(f.title, f.url) AS feed_title,
             snippet(items_fts, 1, '<mark>', '</mark>', '…', 18) AS snip
      FROM items_fts
      JOIN items i ON i.id = items_fts.rowid
      JOIN feeds f ON f.id = i.feed_id
      WHERE items_fts MATCH :q
    ";

    if ($feed_id !== '' && ctype_digit($feed_id)) {
      $sql .= " AND i.feed_id = :fid ";
    }

    $sql .= " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT :limit ";

    $stmt = $db->prepare($sql);
    if ($stmt === false) {
      throw new RuntimeException("Prepare fallita: " . $db->lastErrorMsg());
    }

    $stmt->bindValue(':q', $q, SQLITE3_TEXT);
    if ($feed_id !== '' && ctype_digit($feed_id)) {
      $stmt->bindValue(':fid', (int)$feed_id, SQLITE3_INTEGER);
    }
    $stmt->bindValue(':limit', $limit, SQLITE3_INTEGER);

    $res = $stmt->execute();

    if ($res === false) {
      $sqlite_err = $db->lastErrorMsg();

      if (stripos($sqlite_err, 'snippet') !== false) {
        $sql2 = "
          SELECT i.id, i.title, i.link, i.published_at, i.fetched_at,
                 COALESCE(f.title, f.url) AS feed_title,
                 '' AS snip
          FROM items_fts
          JOIN items i ON i.id = items_fts.rowid
          JOIN feeds f ON f.id = i.feed_id
          WHERE items_fts MATCH :q
        ";

        if ($feed_id !== '' && ctype_digit($feed_id)) {
          $sql2 .= " AND i.feed_id = :fid ";
        }

        $sql2 .= " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT :limit ";

        $stmt2 = $db->prepare($sql2);
        if ($stmt2 === false) {
          throw new RuntimeException("Prepare fallback fallita: " . $db->lastErrorMsg());
        }

        $stmt2->bindValue(':q', $q, SQLITE3_TEXT);
        if ($feed_id !== '' && ctype_digit($feed_id)) {
          $stmt2->bindValue(':fid', (int)$feed_id, SQLITE3_INTEGER);
        }
        $stmt2->bindValue(':limit', $limit, SQLITE3_INTEGER);

        $res2 = $stmt2->execute();
        if ($res2 === false) {
          throw new RuntimeException("Execute fallback fallita: " . $db->lastErrorMsg());
        }

        while ($r = $res2->fetchArray(SQLITE3_ASSOC)) {
          $rows[] = $r;
        }
      } else {
        throw new RuntimeException("Execute fallita: " . $sqlite_err);
      }
    } else {
      while ($r = $res->fetchArray(SQLITE3_ASSOC)) {
        $rows[] = $r;
      }
    }
  } catch (Throwable $e) {
    $err = $e->getMessage();
  }
}

$me = current_user();
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css">
<title>RSSIntel — Ricerca</title>

<header>
  <b>RSSIntel — Ricerca</b>
  <div class="meta">
    utente: <?=h($me)?> ·
    <a href="browse.php">📰 Lettura</a> ·
    <a href="search.php">Ricerca</a> ·
    <a href="notes.php">Annotazioni</a> ·
    <a href="feeds.php">Feeds</a>
  </div>
</header>

<div class="wrap">
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
      <b><?=count($rows)?> risultati</b>
      <div class="meta">
        limite: <?=$limit?>
        <?= ($feed_id !== '' && ctype_digit($feed_id)) ? ' · feed_id: ' . h($feed_id) : '' ?>
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
                <?=h((string)($r['published_at'] ?: $r['fetched_at']))?>
                <?php if (!empty($r['link'])): ?>
                  · <a href="<?=h((string)$r['link'])?>" target="_blank">Apri fonte</a>
                <?php endif; ?>
              </div>

              <?php if (!empty($r['snip'])): ?>
                <div class="small result-snippet"><?=$r['snip']?></div>
              <?php endif; ?>
            </div>
          </div>
        </div>
        <hr>
      <?php endforeach; ?>
    </div>
  <?php endif; ?>
</div>
