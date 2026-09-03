<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';
require_login();

$db = db_ro();

$author   = trim((string)($_GET['author'] ?? ''));
$contains = trim((string)($_GET['contains'] ?? ''));
$item_raw = trim((string)($_GET['item'] ?? ''));
$tag      = trim((string)($_GET['tag'] ?? ''));
$limit = (int)($_GET['limit'] ?? 200);
if ($limit <= 0) $limit = 200;
if ($limit > 1000) $limit = 1000;

$where = [];
$params = [];

if ($author !== '') { $where[] = "a.author = :author"; $params[':author'] = [$author, SQLITE3_TEXT]; }
if ($item_raw !== '') { $where[] = "CAST(a.item_id AS TEXT) LIKE :item"; $params[':item'] = ['%'.$item_raw.'%', SQLITE3_TEXT]; }
if ($contains !== '') { $where[] = "(a.note LIKE :c OR a.quote LIKE :c)"; $params[':c'] = ['%'.$contains.'%', SQLITE3_TEXT]; }

$from = "annotations a
         JOIN items i ON i.id = a.item_id
         JOIN feeds f ON f.id = i.feed_id";

if ($tag !== '') {
  $from .= "
    JOIN annotation_tags at ON at.annotation_id = a.id
    JOIN tags t ON t.id = at.tag_id
  ";
  $where[] = "t.name = :tag";
  $params[':tag'] = [$tag, SQLITE3_TEXT];
}

$sql = "
  SELECT a.id, a.item_id, a.author, a.created_at, a.updated_at, a.quote, a.note,
         i.title AS item_title, i.link AS item_link,
         COALESCE(f.title, f.url) AS feed_title
  FROM $from
";

if ($where) $sql .= " WHERE " . implode(" AND ", $where);
$sql .= " ORDER BY a.created_at DESC LIMIT :limit";

$stmt = $db->prepare($sql);
foreach ($params as $k => [$v,$t]) $stmt->bindValue($k, $v, $t);
$stmt->bindValue(':limit', $limit, SQLITE3_INTEGER);

$rows = [];
$res = $stmt->execute();
while ($r = $res->fetchArray(SQLITE3_ASSOC)) $rows[] = $r;
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css?v=<?= @filemtime(__DIR__ . "/assets/style.css") ?>">
<title>Annotazioni</title>

<?php render_header('Annotazioni', 'notes'); ?>

<div class="wrap">
  <form method="get" class="card">
    <div class="row">
      <input class="grow" name="contains" value="<?=h($contains)?>" placeholder="Contiene testo (note/quote)">
      <input name="author" value="<?=h($author)?>" placeholder="Autore">
      <input name="tag" value="<?=h($tag)?>" placeholder="Tag">
      <input name="item" value="<?=h($item_raw)?>" placeholder="item_id">
      <select name="limit">
        <?php foreach ([50,200,500,1000] as $n): ?>
          <option value="<?=$n?>" <?= $limit===$n?'selected':'' ?>><?=$n?></option>
        <?php endforeach; ?>
      </select>
      <button class="btn">Filtra</button>
    </div>
  </form>

  <div class="card">
    <b><?=count($rows)?> risultati</b>
    <hr>

    <?php if (!$rows): ?>
      <div class="meta">Nessuna annotazione trovata.</div>
    <?php endif; ?>

    <?php foreach ($rows as $r): ?>
      <div style="margin-bottom:14px">
        <div style="display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
          <a href="item.php?id=<?=urlencode((string)$r['item_id'])?>"><b><?=h((string)$r['item_id'])?></b></a>
          <?php if (!empty($r['feed_title'])): ?><span class="badge"><?=h((string)$r['feed_title'])?></span><?php endif; ?>
          <span class="badge"><?=h((string)$r['author'])?></span>
        </div>

        <?php if (!empty($r['item_title'])): ?>
          <div class="small" style="margin-top:6px"><b><?=h((string)$r['item_title'])?></b></div>
        <?php endif; ?>

        <div class="meta" style="margin-top:6px">
          <?=h(fmt_dt((string)$r['created_at']))?>
          <?php if (!empty($r['updated_at'])): ?> · aggiornato: <?=h(fmt_dt((string)$r['updated_at']))?><?php endif; ?>
          <?php if (!empty($r['item_link'])): ?> · <a href="<?=h((string)$r['item_link'])?>" target="_blank">Apri fonte</a><?php endif; ?>
        </div>

        <?php if (!empty($r['quote'])): ?>
          <div class="meta" style="margin-top:6px">Quote: “<?=h((string)$r['quote'])?>”</div>
        <?php endif; ?>

        <div style="margin-top:8px"><?= nl2br(h((string)$r['note'])) ?></div>
      </div>
      <hr>
    <?php endforeach; ?>
  </div>
</div>
