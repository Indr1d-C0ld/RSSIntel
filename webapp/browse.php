<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';
require_login();

$db = db_ro();

// Parametri GET
$feed_id = isset($_GET['feed']) && ctype_digit($_GET['feed']) ? (int)$_GET['feed'] : null;
$date_mode = $_GET['date'] ?? 'day'; // day, week, month
if (!in_array($date_mode, ['day', 'week', 'month'], true)) $date_mode = 'day'; // whitelist: evita XSS riflesso negli href
$day = $_GET['day'] ?? date('Y-m-d');
$page = isset($_GET['page']) && ctype_digit($_GET['page']) ? (int)$_GET['page'] : 1;
$per_page = 30;

// Calcola intervallo data
$range = match($date_mode) {
    'week'  => ['start' => date('Y-m-d', strtotime($day . ' -' . (date('N', strtotime($day))-1) . ' days')), 'end' => date('Y-m-d', strtotime($day . ' +' . (7 - date('N', strtotime($day))) . ' days'))],
    'month' => ['start' => date('Y-m-01', strtotime($day)), 'end' => date('Y-m-t', strtotime($day))],
    default => ['start' => $day, 'end' => $day],
};
$date_start = $range['start'];
$date_end   = $range['end'];

// I giorni scelti dall'utente sono di calendario italiano (Europe/Rome):
// converto gli estremi in UTC per confrontarli con le date del DB (che sono UTC).
$tz_rome = new DateTimeZone('Europe/Rome');
$tz_utc  = new DateTimeZone('UTC');
$start_utc = (new DateTime($date_start . ' 00:00:00', $tz_rome))->setTimezone($tz_utc)->format('Y-m-d H:i:s');
$end_utc   = (new DateTime($date_end   . ' 23:59:59', $tz_rome))->setTimezone($tz_utc)->format('Y-m-d H:i:s');

// Costruzione query con paginazione
// COALESCE: gli item senza published_at (feed che non lo espone) usano fetched_at,
// altrimenti sparirebbero dalla vista cronologica.
$where = "COALESCE(i.published_at, i.fetched_at) BETWEEN :start AND :end";
$params = [':start' => $start_utc, ':end' => $end_utc];
if ($feed_id) {
    $where .= " AND i.feed_id = :feed";
    $params[':feed'] = $feed_id;
}

// Conta totale
$count_stmt = $db->prepare("
    SELECT COUNT(*) as cnt
    FROM items i
    WHERE $where
");
foreach ($params as $k => $v) $count_stmt->bindValue($k, $v, is_int($v) ? SQLITE3_INTEGER : SQLITE3_TEXT);
$total = (int)$count_stmt->execute()->fetchArray(SQLITE3_ASSOC)['cnt'];
$pages = ceil($total / $per_page);
$offset = ($page - 1) * $per_page;

// Estrai articoli
$sql = "
    SELECT i.id, i.title, i.link, i.published_at, i.fetched_at,
           COALESCE(f.title, f.url) AS feed_title, f.url AS feed_url
    FROM items i
    JOIN feeds f ON f.id = i.feed_id
    WHERE $where
    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC
    LIMIT :limit OFFSET :offset
";
$stmt = $db->prepare($sql);
foreach ($params as $k => $v) $stmt->bindValue($k, $v, is_int($v) ? SQLITE3_INTEGER : SQLITE3_TEXT);
$stmt->bindValue(':limit', $per_page, SQLITE3_INTEGER);
$stmt->bindValue(':offset', $offset, SQLITE3_INTEGER);
$res = $stmt->execute();
$items = [];
while ($row = $res->fetchArray(SQLITE3_ASSOC)) $items[] = $row;

// Lista feed per il menu a tendina
$feeds = [];
$resf = $db->query("SELECT id, COALESCE(title, url) AS name FROM feeds ORDER BY name ASC");
while ($f = $resf->fetchArray(SQLITE3_ASSOC)) $feeds[] = $f;

?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css">
<title>RSSIntel — Leggi i feed</title>

<?php render_header('📰 Lettura cronologica', 'browse'); ?>

<div class="wrap">
  <form method="get" class="card">
    <div class="row">
      <select name="feed">
        <option value="">📡 Tutti i feed</option>
        <?php foreach ($feeds as $f): ?>
          <option value="<?=$f['id']?>" <?= $feed_id == $f['id'] ? 'selected' : '' ?>>
            <?=h($f['name'])?>
          </option>
        <?php endforeach; ?>
      </select>

      <select name="date">
        <option value="day" <?= $date_mode === 'day' ? 'selected' : '' ?>>Giorno</option>
        <option value="week" <?= $date_mode === 'week' ? 'selected' : '' ?>>Settimana</option>
        <option value="month" <?= $date_mode === 'month' ? 'selected' : '' ?>>Mese</option>
      </select>

      <input type="date" name="day" value="<?=h($day)?>">

      <button class="btn" type="submit">Filtra</button>
    </div>
  </form>

  <div class="card">
    <div class="row">
      <div class="grow">
        <b><?=$total?> articoli</b>
        <span class="meta">
          dal <?=h(fmt_day($date_start))?> al <?=h(fmt_day($date_end))?>
          <?php if ($feed_id): ?> · feed filtrato<?php endif; ?>
        </span>
      </div>
      <?php if ($pages > 1): ?>
        <div class="btns">
          <?php for ($p = max(1, $page-2); $p <= min($pages, $page+2); $p++): ?>
            <a href="?feed=<?=$feed_id?>&date=<?=$date_mode?>&day=<?=urlencode($day)?>&page=<?=$p?>"
               class="btn <?= $p == $page ? 'active' : '' ?>"><?=$p?></a>
          <?php endfor; ?>
        </div>
      <?php endif; ?>
    </div>

    <hr>

    <?php if (!$items): ?>
      <div class="meta">Nessun articolo nel periodo selezionato.</div>
    <?php endif; ?>

    <?php foreach ($items as $item): ?>
      <div class="result-entry">
        <div class="row">
          <div class="grow">
            <div class="row">
              <a href="item.php?id=<?=urlencode((string)$item['id'])?>"><b><?=h((string)$item['id'])?></b></a>
              <span class="badge"><?=h($item['feed_title'])?></span>
            </div>

            <?php if (!empty($item['title'])): ?>
              <div class="small result-title"><b><?=h($item['title'])?></b></div>
            <?php endif; ?>

            <div class="meta result-meta">
              <?=h(fmt_dt($item['published_at'] ?: $item['fetched_at']))?>
              <?php if (!empty($item['link'])): ?>
                · <a href="<?=h($item['link'])?>" target="_blank">Apri fonte</a>
              <?php endif; ?>
            </div>
          </div>
        </div>
      </div>
      <hr>
    <?php endforeach; ?>

    <?php if ($pages > 1): ?>
      <div class="btns" style="justify-content:center; margin-top:16px">
        <?php if ($page > 1): ?>
          <a class="btn" href="?feed=<?=$feed_id?>&date=<?=$date_mode?>&day=<?=urlencode($day)?>&page=<?=$page-1?>">◀ Prec</a>
        <?php endif; ?>
        <span class="meta">Pagina <?=$page?> di <?=$pages?></span>
        <?php if ($page < $pages): ?>
          <a class="btn" href="?feed=<?=$feed_id?>&date=<?=$date_mode?>&day=<?=urlencode($day)?>&page=<?=$page+1?>">Succ ▶</a>
        <?php endif; ?>
      </div>
    <?php endif; ?>
  </div>
</div>
