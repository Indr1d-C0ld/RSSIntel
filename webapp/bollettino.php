<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

/**
 * Bollettino: la giornata in una pagina.
 *
 * Il portale raccoglie ~190 articoli al giorno, e quasi l'80% viene da tre
 * testate generaliste. Elencati in ordine cronologico, come in Lettura, le
 * poche fonti specialistiche finiscono sepolte. Qui l'ordine e' per tipo di
 * fonte: prima cio' che e' raro e mirato, in fondo e compresso il flusso
 * generalista. In testa le corrispondenze delle allerte, in coda la salute
 * dei feed.
 *
 * E' la pagina d'ingresso del portale (index.php e il login portano qui).
 */

require_login();
$me = current_user();
$db = db_ro();

/* ---------- giorno (calendario italiano), validato come in browse.php ---------- */
$day = (string)($_GET['day'] ?? '');
$dchk = DateTime::createFromFormat('!Y-m-d', $day);
if (!$dchk || $dchk->format('Y-m-d') !== $day) $day = date('Y-m-d');
[$a, $b] = day_bounds_utc($day);
$today = date('Y-m-d');
$prev  = (new DateTime($day))->modify('-1 day')->format('Y-m-d');
$next  = (new DateTime($day))->modify('+1 day')->format('Y-m-d');

/* ---------- ordine delle sezioni: il raro e mirato prima, il flusso dopo ---------- */
$order = ['specialistica', 'istituzionale', 'analisi', 'blog', 'advocacy', 'generalista', '__none'];
$cats  = source_categories();
$label = static fn(string $k): string => $k === '__none' ? 'non classificate' : ($cats[$k][0] ?? $k);
// Oltre questa soglia una sezione mostra i primi N e rimanda a Lettura.
$CAP = ['generalista' => 12, '__none' => 12];
$CAP_DEFAULT = 40;

/* ---------- articoli del giorno, con la loro categoria ---------- */
$items = [];
$st = $db->prepare("
  SELECT i.id, i.title, i.link, i.published_at, i.fetched_at,
         COALESCE(f.title, f.url) AS feed_title
         " . source_select_cols($db) . "
  FROM items i JOIN feeds f ON f.id = i.feed_id
  WHERE COALESCE(i.published_at, i.fetched_at) BETWEEN :a AND :b
  ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.id DESC
");
$st->bindValue(':a', $a, SQLITE3_TEXT);
$st->bindValue(':b', $b, SQLITE3_TEXT);
$r = $st->execute();
$by_cat = [];
while ($x = $r->fetchArray(SQLITE3_ASSOC)) {
  $k = isset($cats[(string)$x['src_category']]) ? (string)$x['src_category'] : '__none';
  $by_cat[$k][] = $x;
  $items[] = $x;
}
$n_items  = count($items);
$n_sources = count(array_unique(array_column($items, 'feed_title')));

/* ---------- allerte: corrispondenze trovate in questa giornata ---------- */
$alerts = [];
if (watch_table_exists($db)) {
  $st = $db->prepare("
    SELECT s.id AS sid, s.name, COUNT(*) AS n, SUM(w.seen = 0) AS unseen
    FROM watch_hits w JOIN saved_searches s ON s.id = w.search_id
    WHERE s.owner = :o AND w.found_at BETWEEN :a AND :b
    GROUP BY s.id ORDER BY n DESC, s.name
  ");
  $st->bindValue(':o', $me, SQLITE3_TEXT);
  $st->bindValue(':a', $a, SQLITE3_TEXT);
  $st->bindValue(':b', $b, SQLITE3_TEXT);
  $r = $st->execute();
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $alerts[] = $x;
}
$n_watches = 0;
if (watch_table_exists($db)) {
  $st = $db->prepare("SELECT COUNT(*) FROM saved_searches WHERE watch = 1 AND owner = :o");
  $st->bindValue(':o', $me, SQLITE3_TEXT);
  $n_watches = (int)$st->execute()->fetchArray()[0];
}

/* ---------- salute dei feed ---------- */
// Tre segnali diversi: un errore esplicito; un feed che il fetcher non tocca da
// ore (il timer gira ogni 15 minuti); un feed che risponde ma ha smesso di
// portare articoli, cioe' probabilmente morto o spostato (un 304 continuo non
// genera errori, quindi senza questo controllo nessuno se ne accorgerebbe).
//
// Il silenzio si giudica rispetto al ritmo della fonte, non in assoluto: per
// un blog che pubblica due volte al mese una settimana vuota e' normale. Si
// stima il ritmo sui 23 giorni PRIMA della settimana in esame (includerla
// abbasserebbe le attese proprio per il feed che si e' fermato) e si segnala
// quando, a quel ritmo, una settimana senza articoli avrebbe avuto meno del 5%
// di probabilita' (Poisson: e^-attesi <= 0,05, cioe' attesi >= 3).
// Misurato l'08/10/2026: la regola "zero articoli in 7 giorni" dava 6 falsi
// allarmi su fonti a bassa frequenza; questa ne da' uno, ed e' un feed vero
// fermo da 9 giorni dietro risposte 304.
$health = [];
$st = $db->prepare("
  SELECT f.id, COALESCE(f.title, f.url) AS name, f.last_fetch_at, f.last_status, f.last_error,
         (SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id
            AND COALESCE(i.published_at, i.fetched_at) BETWEEN :a AND :b) AS n_day,
         (SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id
            AND COALESCE(i.published_at, i.fetched_at) >  datetime(:b, '-7 days')
            AND COALESCE(i.published_at, i.fetched_at) <= :b) AS n_7d,
         (SELECT COUNT(*) FROM items i WHERE i.feed_id = f.id
            AND COALESCE(i.published_at, i.fetched_at) >  datetime(:b, '-30 days')
            AND COALESCE(i.published_at, i.fetched_at) <= datetime(:b, '-7 days')) AS n_base
  FROM feeds f WHERE f.enabled = 1
  ORDER BY n_day DESC, name COLLATE NOCASE
");
$st->bindValue(':a', $a, SQLITE3_TEXT);
$st->bindValue(':b', $b, SQLITE3_TEXT);
$r = $st->execute();
$problems = [];
while ($x = $r->fetchArray(SQLITE3_ASSOC)) {
  $health[] = $x;
  $why = [];
  if (!empty($x['last_error'])) $why[] = 'errore: ' . mb_substr((string)$x['last_error'], 0, 120);
  if (!empty($x['last_fetch_at']) && strtotime((string)$x['last_fetch_at'] . ' UTC') < time() - 2 * 3600) {
    $why[] = 'non aggiornato dal ' . fmt_dt((string)$x['last_fetch_at']);
  }
  $expected = (int)$x['n_base'] / 23 * 7;   // articoli attesi in 7 giorni al ritmo abituale
  if ((int)$x['n_7d'] === 0 && $expected >= 3) {
    $why[] = sprintf('nessun articolo in 7 giorni, contro ~%s attesi al suo ritmo abituale',
                     number_format($expected, 1, ',', '.'));
  }
  if ($why) $problems[] = ['name' => (string)$x['name'], 'why' => $why];
}
$last_fetch = (string)$db->querySingle("SELECT MAX(last_fetch_at) FROM feeds WHERE enabled = 1");

/* ---------- lavoro d'analisi del giorno ---------- */
$st = $db->prepare("SELECT COUNT(*) FROM annotations WHERE created_at BETWEEN :a AND :b");
$st->bindValue(':a', $a, SQLITE3_TEXT); $st->bindValue(':b', $b, SQLITE3_TEXT);
$n_ann = (int)$st->execute()->fetchArray()[0];

$classified = feeds_classified($db)
  ? (int)$db->querySingle("SELECT COUNT(*) FROM feeds WHERE enabled = 1 AND category IS NOT NULL")
  : 0;
$n_feeds = count($health);
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
<title>RSSIntel — Bollettino del <?=h(fmt_day($day))?></title>

<?php render_header('RSSIntel — Bollettino', 'bollettino'); ?>

<div class="wrap">

  <div class="card">
    <div class="row" style="justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap">
      <div class="grow">
        <b style="font-size:1.15rem">Bollettino del <?=h(fmt_day($day))?></b>
        <?php if ($day === $today): ?><span class="badge">oggi, in corso</span><?php endif; ?>
        <div class="meta" style="margin-top:4px">
          <?= number_format($n_items, 0, ',', '.') ?> articoli da <?= $n_sources ?> fonti
          · ultima raccolta <?=h(fmt_dt($last_fetch) ?: 'n/d')?>
          <?php if ($n_ann > 0): ?> · <?= $n_ann ?> annotazioni<?php endif; ?>
        </div>
      </div>
      <div class="btns">
        <a class="btn" href="bollettino.php?day=<?=h($prev)?>">◀ <?=h(fmt_day($prev))?></a>
        <?php if ($day !== $today): ?>
          <a class="btn" href="bollettino.php">Oggi</a>
          <?php if ($next <= $today): ?><a class="btn" href="bollettino.php?day=<?=h($next)?>"><?=h(fmt_day($next))?> ▶</a><?php endif; ?>
        <?php endif; ?>
      </div>
    </div>
  </div>

  <!-- ===== Allerte ===== -->
  <div class="card">
    <b>🔔 Allerte</b>
    <hr>
    <?php if (!$n_watches): ?>
      <div class="meta">
        Nessuna allerta attiva. Le allerte rieseguono una ricerca salvata dopo
        ogni raccolta e segnalano qui i nuovi articoli: accendile da
        <a href="search.php">Ricerca</a>.
      </div>
    <?php elseif (!$alerts): ?>
      <div class="meta">Nessuna corrispondenza in questa giornata (<?= $n_watches ?> allerte attive).</div>
    <?php else: ?>
      <?php foreach ($alerts as $al): ?>
        <div class="row" style="gap:8px; margin:4px 0">
          <a href="novita.php#s<?= (int)$al['sid'] ?>"><b><?=h((string)$al['name'])?></b></a>
          <span class="meta"><?= (int)$al['n'] ?> corrispondenze</span>
          <?php if ((int)$al['unseen'] > 0): ?><span class="badge red-stamp"><?= (int)$al['unseen'] ?> da vedere</span><?php endif; ?>
        </div>
      <?php endforeach; ?>
    <?php endif; ?>
  </div>

  <?php if ($classified === 0 && $n_feeds > 0): ?>
    <div class="card">
      <div class="meta">
        <b>Le fonti non sono ancora classificate</b>: per questo tutto il materiale
        compare sotto «non classificate». Assegnando una categoria ai feed in
        <a href="feeds.php">Feeds</a>, il bollettino mette in testa le fonti
        specialistiche e comprime in fondo il flusso generalista.
      </div>
    </div>
  <?php endif; ?>

  <!-- ===== Articoli per tipo di fonte ===== -->
  <?php foreach ($order as $k): ?>
    <?php if (empty($by_cat[$k])) continue; ?>
    <?php
      $rows = $by_cat[$k];
      $cap  = $CAP[$k] ?? $CAP_DEFAULT;
      $more = count($rows) - $cap;
      $browse = 'browse.php?' . http_build_query(['cat' => $k, 'date' => 'day', 'day' => $day]);
    ?>
    <div class="card">
      <div class="row" style="justify-content:space-between; align-items:center">
        <b><?=h(ucfirst($label($k)))?></b>
        <span class="meta"><?= count($rows) ?> articoli</span>
      </div>
      <?php if ($k !== '__none' && isset($cats[$k])): ?>
        <div class="meta"><?=h($cats[$k][1])?></div>
      <?php endif; ?>
      <hr>
      <?php foreach (array_slice($rows, 0, $cap) as $x): ?>
        <div style="margin:6px 0">
          <div class="row" style="gap:6px; flex-wrap:wrap; align-items:baseline">
            <a href="item.php?id=<?= (int)$x['id'] ?>"><b><?=h((string)($x['title'] ?: '(senza titolo)'))?></b></a>
          </div>
          <div class="meta">
            <span class="badge"><?=h((string)$x['feed_title'])?></span>
            <?= source_badge($x['src_category'], $x['src_reliability']) ?>
            <?=h(fmt_dt((string)($x['published_at'] ?: $x['fetched_at'])))?>
            <?php if ($u = safe_url((string)$x['link'])): ?>
              · <a href="<?=$u?>" target="_blank" rel="noopener noreferrer">fonte</a>
            <?php endif; ?>
          </div>
        </div>
      <?php endforeach; ?>
      <?php if ($more > 0): ?>
        <div class="meta" style="margin-top:8px">
          … e altri <?= $more ?>: <a href="<?=h($browse)?>">tutti in Lettura</a>
        </div>
      <?php endif; ?>
    </div>
  <?php endforeach; ?>

  <?php if (!$items): ?>
    <div class="card"><div class="meta">Nessun articolo in questa giornata.</div></div>
  <?php endif; ?>

  <!-- ===== Salute dei feed ===== -->
  <div class="card">
    <b>Salute dei feed</b>
    <span class="meta">(<?= $n_feeds ?> attivi)</span>
    <hr>
    <?php if (!$problems): ?>
      <div class="meta">Nessun feed in errore, fermo o silenzioso rispetto al proprio ritmo abituale.</div>
    <?php else: ?>
      <?php foreach ($problems as $p): ?>
        <div style="margin:4px 0">
          <b><?=h($p['name'])?></b>
          <span class="meta" style="color:var(--red-stamp)">— <?=h(implode(' · ', $p['why']))?></span>
        </div>
      <?php endforeach; ?>
    <?php endif; ?>
    <details style="margin-top:8px">
      <summary class="meta">Articoli per fonte in questa giornata</summary>
      <div class="dtable" style="margin-top:6px">
        <table>
          <thead><tr><th>Fonte</th><th>Giorno</th><th class="col-sec">7 giorni</th><th class="col-sec">23 gg prima</th></tr></thead>
          <tbody>
          <?php foreach ($health as $x): ?>
            <tr>
              <td><?=h((string)$x['name'])?></td>
              <td><?= (int)$x['n_day'] ?></td>
              <td class="col-sec"><?= (int)$x['n_7d'] ?></td>
              <td class="col-sec"><?= (int)$x['n_base'] ?></td>
            </tr>
          <?php endforeach; ?>
          </tbody>
        </table>
      </div>
    </details>
  </div>

</div>
