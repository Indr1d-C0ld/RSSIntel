<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_login();
$db = db_ro();

function tbl(SQLite3 $db, string $name): bool {
  return (bool)$db->querySingle(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='" . SQLite3::escapeString($name) . "'"
  );
}
function q1(SQLite3 $db, string $sql): int {
  $v = $db->querySingle($sql);
  return is_numeric($v) ? (int)$v : 0;
}
/** Barra proporzionale (CSS inline, nessuna dipendenza). */
function bar(int $value, int $max, string $label = ''): string {
  $pct = ($max > 0 && $value > 0) ? max(1, (int)round($value * 100 / $max)) : 0;
  $lab = $label !== '' ? $label : (string)$value;
  return '<div style="background:var(--border);border-radius:3px;overflow:hidden;height:16px;position:relative">'
       . '<div style="background:var(--red-stamp);height:16px;width:' . $pct . '%"></div>'
       . '<span style="position:absolute;left:6px;top:0;font-size:.72rem;line-height:16px;color:var(--ink)">'
       . h($lab) . '</span></div>';
}

/* ---------- Riepilogo ---------- */
$tot_items   = q1($db, "SELECT COUNT(*) FROM items");
$tot_feeds   = q1($db, "SELECT COUNT(*) FROM feeds");
$feeds_on    = q1($db, "SELECT COUNT(*) FROM feeds WHERE enabled=1");
$tot_ann     = tbl($db,'annotations')     ? q1($db, "SELECT COUNT(*) FROM annotations") : 0;
$tot_tags    = tbl($db,'tags')            ? q1($db, "SELECT COUNT(*) FROM tags") : 0;
$tot_fav     = tbl($db,'favorites')       ? q1($db, "SELECT COUNT(*) FROM favorites") : 0;
$tot_saved   = tbl($db,'saved_searches')  ? q1($db, "SELECT COUNT(*) FROM saved_searches") : 0;
$tot_users   = tbl($db,'users')           ? q1($db, "SELECT COUNT(*) FROM users") : 0;
$db_bytes    = @filesize(cfg()['db_path']) ?: 0;

$range = $db->querySingle("SELECT MIN(COALESCE(published_at,fetched_at)) || '|' || MAX(COALESCE(published_at,fetched_at)) FROM items");
[$first_item, $last_item] = array_pad(explode('|', (string)$range, 2), 2, '');
$last_fetch = (string)$db->querySingle("SELECT MAX(last_fetch_at) FROM feeds");
$items_24h  = q1($db, "SELECT COUNT(*) FROM items WHERE COALESCE(published_at,fetched_at) >= datetime('now','-1 day')");
$items_7d   = q1($db, "SELECT COUNT(*) FROM items WHERE COALESCE(published_at,fetched_at) >= datetime('now','-7 day')");
$span_days  = ($first_item !== '') ? max(1, (int)round((time() - strtotime($first_item)) / 86400)) : 1;
$avg_day    = round($tot_items / $span_days, 1);

/* ---------- Raccolta per giorno (ultimi 30) — bucket in ora di Roma ---------- */
$by_day = [];
$res = $db->query("
  SELECT date(COALESCE(published_at,fetched_at),'localtime') d, COUNT(*) c
  FROM items
  WHERE COALESCE(published_at,fetched_at) >= datetime('now','-30 day')
  GROUP BY d ORDER BY d
");
while ($r = $res->fetchArray(SQLITE3_ASSOC)) $by_day[$r['d']] = (int)$r['c'];
$day_max = $by_day ? max($by_day) : 0;

/* ---------- Raccolta per mese (ultimi 12) ---------- */
$by_month = [];
$res = $db->query("
  SELECT strftime('%Y-%m', COALESCE(published_at,fetched_at),'localtime') m, COUNT(*) c
  FROM items GROUP BY m ORDER BY m DESC LIMIT 12
");
while ($r = $res->fetchArray(SQLITE3_ASSOC)) $by_month[$r['m']] = (int)$r['c'];
$by_month = array_reverse($by_month, true);
$month_max = $by_month ? max($by_month) : 0;

/* ---------- Per feed ---------- */
$per_feed = [];
$res = $db->query("
  SELECT f.id, COALESCE(f.title,f.url) name, f.enabled, f.last_status, f.last_error, f.last_fetch_at,
         COUNT(i.id) n, MAX(COALESCE(i.published_at,i.fetched_at)) last_item
  FROM feeds f LEFT JOIN items i ON i.feed_id=f.id
  GROUP BY f.id ORDER BY n DESC, name COLLATE NOCASE ASC
");
while ($r = $res->fetchArray(SQLITE3_ASSOC)) $per_feed[] = $r;
$feed_max = $per_feed ? max(array_map(fn($x) => (int)$x['n'], $per_feed)) : 0;

/* ---------- Tag piu' usati ---------- */
$top_tags = [];
if (tbl($db,'tags') && tbl($db,'annotation_tags')) {
  $res = $db->query("
    SELECT t.name, COUNT(*) c
    FROM tags t JOIN annotation_tags at ON at.tag_id=t.id
    GROUP BY t.name ORDER BY c DESC, t.name COLLATE NOCASE ASC LIMIT 30
  ");
  while ($r = $res->fetchArray(SQLITE3_ASSOC)) $top_tags[] = $r;
}
$tag_max = $top_tags ? (int)$top_tags[0]['c'] : 0;

/* ---------- Annotazioni per autore ---------- */
$ann_by_author = [];
if (tbl($db,'annotations')) {
  $res = $db->query("SELECT author, COUNT(*) c FROM annotations GROUP BY author ORDER BY c DESC");
  while ($r = $res->fetchArray(SQLITE3_ASSOC)) $ann_by_author[] = $r;
}

function human_bytes(int $b): string {
  $u = ['B','KB','MB','GB','TB']; $i = 0;
  while ($b >= 1024 && $i < count($u) - 1) { $b /= 1024; $i++; }
  return round($b, $i ? 1 : 0) . ' ' . $u[$i];
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css?v=<?= @filemtime(__DIR__ . "/assets/style.css") ?>">
<title>RSSIntel — Statistiche</title>

<?php render_header('RSSIntel — Statistiche', 'stats'); ?>

<div class="wrap">

  <div class="card">
    <b>Riepilogo</b>
    <hr>
    <div class="row" style="gap:20px; flex-wrap:wrap">
      <?php
      $cards = [
        'Articoli'        => number_format($tot_items, 0, ',', '.'),
        'Feed'            => $feeds_on . ' / ' . $tot_feeds . ' attivi',
        'Annotazioni'     => (string)$tot_ann,
        'Tag'             => (string)$tot_tags,
        'Favoriti'        => (string)$tot_fav,
        'Ricerche salv.'  => (string)$tot_saved,
        'Utenti'          => (string)$tot_users,
        'Dimensione DB'   => human_bytes($db_bytes),
      ];
      foreach ($cards as $k => $v): ?>
        <div style="min-width:130px">
          <div style="font-size:1.5rem"><b><?=h($v)?></b></div>
          <div class="meta"><?=h($k)?></div>
        </div>
      <?php endforeach; ?>
    </div>
    <hr>
    <div class="meta">
      Primo articolo: <?=h(fmt_dt($first_item) ?: 'n/d')?> ·
      Ultimo: <?=h(fmt_dt($last_item) ?: 'n/d')?> ·
      Ultimo fetch: <?=h(fmt_dt($last_fetch) ?: 'n/d')?><br>
      Ultime 24h: <b><?=$items_24h?></b> articoli ·
      Ultimi 7 giorni: <b><?=$items_7d?></b> ·
      Media: <b><?=$avg_day?></b>/giorno (su <?=$span_days?> giorni)
    </div>
  </div>

  <div class="card">
    <b>Articoli raccolti — ultimi 30 giorni</b>
    <hr>
    <?php if (!$by_day): ?>
      <div class="meta">Nessun articolo nel periodo.</div>
    <?php else: ?>
      <?php
      // riempi i giorni mancanti con 0
      $cur = new DateTime('-29 days');
      for ($i = 0; $i < 30; $i++, $cur->modify('+1 day')):
        $d = $cur->format('Y-m-d'); $c = $by_day[$d] ?? 0;
      ?>
        <div class="row" style="gap:8px; margin:2px 0">
          <span class="meta" style="width:92px; flex:none"><?=h(fmt_day($d))?></span>
          <span class="grow"><?= bar($c, $day_max, $c > 0 ? (string)$c : '') ?></span>
        </div>
      <?php endfor; ?>
    <?php endif; ?>
  </div>

  <div class="card">
    <b>Articoli raccolti — per mese (ultimi 12)</b>
    <hr>
    <?php foreach ($by_month as $m => $c): ?>
      <div class="row" style="gap:8px; margin:2px 0">
        <span class="meta" style="width:92px; flex:none"><?=h($m)?></span>
        <span class="grow"><?= bar($c, $month_max, (string)$c) ?></span>
      </div>
    <?php endforeach; ?>
    <?php if (!$by_month): ?><div class="meta">Nessun dato.</div><?php endif; ?>
  </div>

  <div class="card">
    <b>Per feed</b> <span class="meta">(<?=count($per_feed)?> feed)</span>
    <hr>
    <div class="dtable">
      <table>
        <thead><tr>
          <th>Feed</th>
          <th>Articoli</th>
          <th style="min-width:160px">Quota</th>
          <th>Ultimo articolo</th>
          <th>Stato</th>
        </tr></thead>
        <tbody>
        <?php foreach ($per_feed as $f): ?>
          <tr>
            <td>
              <a href="search.php?feed_id=<?= (int)$f['id'] ?>&q="><?=h((string)$f['name'])?></a>
              <?php if ((int)$f['enabled'] === 0): ?><span class="badge">off</span><?php endif; ?>
            </td>
            <td><?= (int)$f['n'] ?></td>
            <td><?= bar((int)$f['n'], $feed_max, '') ?></td>
            <td class="meta"><?=h(fmt_dt((string)$f['last_item']) ?: '—')?></td>
            <td class="meta">
              <?php $st = (string)($f['last_status'] ?? ''); ?>
              <?php if ($f['last_error']): ?>
                <span style="color:var(--red-stamp)" title="<?=h((string)$f['last_error'])?>">errore (<?=h($st ?: '?')?>)</span>
              <?php elseif ($st === '200' || $st === '304'): ?>
                ok <?=h($st)?>
              <?php elseif ($st !== ''): ?>
                <?=h($st)?>
              <?php else: ?>—<?php endif; ?>
            </td>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
  </div>

  <?php if ($top_tags): ?>
  <div class="card">
    <b>Tag più usati</b>
    <hr>
    <?php foreach ($top_tags as $t): ?>
      <div class="row" style="gap:8px; margin:2px 0">
        <span class="meta" style="width:140px; flex:none">
          <a href="notes.php?tag=<?=urlencode((string)$t['name'])?>"><?=h((string)$t['name'])?></a>
        </span>
        <span class="grow"><?= bar((int)$t['c'], $tag_max, (string)$t['c']) ?></span>
      </div>
    <?php endforeach; ?>
  </div>
  <?php endif; ?>

  <?php if ($ann_by_author): ?>
  <div class="card">
    <b>Annotazioni per autore</b>
    <hr>
    <?php foreach ($ann_by_author as $a): ?>
      <div class="meta"><?=h((string)$a['author'])?>: <b><?= (int)$a['c'] ?></b></div>
    <?php endforeach; ?>
  </div>
  <?php endif; ?>

</div>
