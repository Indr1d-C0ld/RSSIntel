<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_role('admin');

$db = db_rw();          // rw: serve alla cache geo
logging_ensure($db);

$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);

/* ---------------- potatura manuale del log ---------------- */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }
  if (($_POST['action'] ?? '') === 'prune') {
    $days = (int)($_POST['days'] ?? access_log_retention_days());
    try {
      $n = access_log_prune($db, max(1, $days));
      $_SESSION['flash'] = ['ok', sprintf(
        '%s righe eliminate dal log (piu' . "'" . ' vecchie di %d giorni).',
        number_format($n, 0, ',', '.'), max(1, $days)
      )];
    } catch (Throwable $e) {
      $_SESSION['flash'] = ['err', $e->getMessage()];
    }
  }
  header('Location: accessi.php');
  exit;
}

$has_log = (bool)$db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_log'");

/* ---------------- filtri log ---------------- */
$f_from = trim((string)($_GET['date_from'] ?? ''));
$f_to   = trim((string)($_GET['date_to'] ?? ''));
$f_user = trim((string)($_GET['user'] ?? ''));
$f_path = trim((string)($_GET['path'] ?? ''));
$f_q    = trim((string)($_GET['q'] ?? ''));
$page   = (isset($_GET['page']) && ctype_digit((string)$_GET['page'])) ? max(1, (int)$_GET['page']) : 1;
$per    = 50;

$where = [];
$bind  = [];
// ts e' UTC; le date scelte qui sono giorni di calendario italiano. Prima
// venivano confrontate direttamente, sbagliando di 1-2 ore agli estremi.
if ($f_from !== '') { $where[] = "ts >= :from"; $bind[':from'] = day_bounds_utc($f_from)[0]; }
if ($f_to   !== '') { $where[] = "ts <= :to";   $bind[':to']   = day_bounds_utc($f_to)[1]; }
if ($f_user !== '') { $where[] = "COALESCE(username,'') = :u"; $bind[':u'] = $f_user; }
if ($f_path !== '') { $where[] = "path = :p"; $bind[':p'] = $f_path; }
if ($f_q    !== '') {
  $where[] = "(ip LIKE :q OR COALESCE(username,'') LIKE :q OR COALESCE(user_agent,'') LIKE :q OR COALESCE(referer,'') LIKE :q)";
  $bind[':q'] = '%' . $f_q . '%';
}
$wsql = $where ? ('WHERE ' . implode(' AND ', $where)) : '';

$total = 0; $pages = 0; $rows = [];
if ($has_log) {
  $cst = $db->prepare("SELECT COUNT(*) c FROM access_log $wsql");
  foreach ($bind as $k => $v) $cst->bindValue($k, $v, SQLITE3_TEXT);
  $total = (int)$cst->execute()->fetchArray(SQLITE3_ASSOC)['c'];
  $pages = (int)ceil($total / $per);
  if ($pages > 0 && $page > $pages) $page = $pages;
  $off = ($page - 1) * $per;

  $st = $db->prepare("
    SELECT ts, ip, username, role, path, method, query_string, status_code, user_agent, referer
    FROM access_log $wsql
    ORDER BY ts DESC, id DESC
    LIMIT :lim OFFSET :off
  ");
  foreach ($bind as $k => $v) $st->bindValue($k, $v, SQLITE3_TEXT);
  $st->bindValue(':lim', $per, SQLITE3_INTEGER);
  $st->bindValue(':off', $off, SQLITE3_INTEGER);
  $r = $st->execute();
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $rows[] = $x;
}

/* ---------------- riepilogo ---------------- */
$sum = ['req' => 0, 'ips' => 0, 'today' => 0, 'users' => 0, 'd24' => 0];
if ($has_log) {
  $sum['req']   = (int)$db->querySingle("SELECT COUNT(*) FROM access_log");
  $sum['ips']   = (int)$db->querySingle("SELECT COUNT(DISTINCT ip) FROM access_log");
  // Niente 'localtime' (dipende dal fuso del sistema): estremi calcolati in PHP.
  [$today_a, $today_b] = day_bounds_utc(date('Y-m-d'));
  $ts = $db->prepare("SELECT COUNT(*) c FROM access_log WHERE ts BETWEEN :a AND :b");
  $ts->bindValue(':a', $today_a, SQLITE3_TEXT);
  $ts->bindValue(':b', $today_b, SQLITE3_TEXT);
  $sum['today'] = (int)$ts->execute()->fetchArray(SQLITE3_ASSOC)['c'];
  $sum['users'] = (int)$db->querySingle("SELECT COUNT(DISTINCT username) FROM access_log WHERE username IS NOT NULL AND username <> ''");
  $sum['d24']   = (int)$db->querySingle("SELECT COUNT(*) FROM access_log WHERE ts >= datetime('now','-1 day')");
}

/* ---------------- attività in corso (ultimi 5 min) ---------------- */
$active = [];
if ($has_log) {
  $r = $db->query("
    SELECT * FROM (
      SELECT username, ip, role, ts, path, method,
             ROW_NUMBER() OVER (PARTITION BY COALESCE(username,''), ip ORDER BY ts DESC) rn,
             COUNT(*)     OVER (PARTITION BY COALESCE(username,''), ip) reqs
      FROM access_log
      WHERE ts >= datetime('now','-5 minutes')
    ) WHERE rn = 1
    ORDER BY ts DESC
  ");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $active[] = $x;
}

/* ---------------- login ---------------- */
$has_att = (bool)$db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_attempts'");
$attempts = []; $fail24 = 0; $failByIp = [];
if ($has_att) {
  $r = $db->query("SELECT created_at, username, ip, success FROM login_attempts ORDER BY created_at DESC LIMIT 60");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $attempts[] = $x;
  $fail24 = (int)$db->querySingle("SELECT COUNT(*) FROM login_attempts WHERE success=0 AND created_at >= datetime('now','-1 day')");
  $r = $db->query("
    SELECT ip, COUNT(*) c FROM login_attempts
    WHERE success=0 AND created_at >= datetime('now','-1 day')
    GROUP BY ip ORDER BY c DESC LIMIT 10
  ");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $failByIp[] = $x;
}

/* ---------------- per utente ---------------- */
$per_user = [];
if ((bool)$db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'")) {
  $r = $db->query("
    SELECT u.username, u.role, u.disabled, u.last_login_at,
           (SELECT ip FROM login_attempts la WHERE la.username=u.username AND la.success=1 ORDER BY created_at DESC LIMIT 1) last_ip,
           (SELECT COUNT(*) FROM access_log al WHERE al.username=u.username) reqs,
           (SELECT COUNT(DISTINCT ip) FROM access_log al WHERE al.username=u.username) ips,
           (SELECT MAX(ts) FROM access_log al WHERE al.username=u.username) last_seen
    FROM users u
    ORDER BY last_seen DESC NULLS LAST, u.username COLLATE NOCASE ASC
  ");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $per_user[] = $x;
}

/* ---------------- geolocalizzazione IP ---------------- */
$geo = [];                 // ip => "IT Italia" (con bandiera) per le tabelle
$geo_budget = 10;          // max risoluzioni nuove (ip-api.com) per caricamento
$geo_pending = 0;

if ($has_log) {
  $ips_seen = [];
  foreach ($rows as $x)     $ips_seen[(string)$x['ip']] = true;
  foreach ($active as $x)   $ips_seen[(string)$x['ip']] = true;
  foreach ($per_user as $x) if (!empty($x['last_ip'])) $ips_seen[(string)$x['last_ip']] = true;

  // Con ipgeo attivo, includi anche qualche IP non ancora in cache per
  // completare il quadro "per paese" (a caricamenti successivi la cache si riempie).
  if (ipgeo_enabled()) {
    $r = $db->query("SELECT DISTINCT a.ip FROM access_log a
                     LEFT JOIN ip_geo_cache g ON g.ip = a.ip WHERE g.ip IS NULL");
    while ($x = $r->fetchArray(SQLITE3_ASSOC)) $ips_seen[(string)$x['ip']] = true;
  }

  // Carica l'intera cache in una sola query. Prima si chiamava resolve_ip_geo()
  // per OGNI ip noto, cioe' una query preparata a testa: il costo cresceva con
  // il numero di IP distinti nel log a ogni caricamento di pagina.
  $cached = [];
  $r = $db->query("SELECT ip, country_code, country_name FROM ip_geo_cache");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $cached[(string)$x['ip']] = $x;

  foreach (array_keys($ips_seen) as $ip) {
    if (isset($cached[$ip])) {
      $cc = $cached[$ip]['country_code'];
      $geo[$ip] = $cc ? (flag_emoji((string)$cc) . ' ' . ($cached[$ip]['country_name'] ?: $cc)) : '';
      continue;
    }
    // Non in cache: risolvibile solo se ipgeo e' attivo e c'e' budget.
    if (!ipgeo_enabled() || $geo_budget <= 0) { $geo_pending++; continue; }
    $geo_budget--;
    $g = resolve_ip_geo($db, $ip);
    $geo[$ip] = $g['country_code']
      ? (flag_emoji((string)$g['country_code']) . ' ' . ($g['country_name'] ?: $g['country_code']))
      : '';
  }
}

/* aggregato "per paese" (solo IP gia' risolti in cache) */
$by_country = [];
$geo_nocountry = 0;
if ($has_log) {
  $r = $db->query("
    SELECT g.country_code cc, g.country_name cn, COUNT(*) reqs, COUNT(DISTINCT a.ip) ips
    FROM access_log a JOIN ip_geo_cache g ON g.ip = a.ip
    WHERE g.country_code IS NOT NULL AND g.country_code <> ''
    GROUP BY g.country_code ORDER BY reqs DESC
  ");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $by_country[] = $x;
  $geo_nocountry = (int)$db->querySingle("
    SELECT COUNT(*) FROM access_log a
    LEFT JOIN ip_geo_cache g ON g.ip = a.ip
    WHERE g.country_code IS NULL OR g.country_code = ''
  ");
}
$country_max = $by_country ? (int)$by_country[0]['reqs'] : 0;

/* distinct per i filtri */
$dUsers = []; $dPaths = [];
if ($has_log) {
  $r = $db->query("SELECT DISTINCT username FROM access_log WHERE username IS NOT NULL AND username <> '' ORDER BY username COLLATE NOCASE");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $dUsers[] = $x['username'];
  $r = $db->query("SELECT DISTINCT path FROM access_log ORDER BY path");
  while ($x = $r->fetchArray(SQLITE3_ASSOC)) $dPaths[] = $x['path'];
}

function acc_page_url(int $n): string {
  $p = array_intersect_key($_GET, array_flip(['date_from','date_to','user','path','q']));
  $p['page'] = $n;
  return 'accessi.php?' . http_build_query(array_filter($p, static fn($v) => $v !== '' && $v !== null));
}
function sc_style(int $c): string {
  if ($c >= 500) return 'color:var(--red-stamp);font-weight:bold';
  if ($c >= 400) return 'color:var(--red-stamp)';
  if ($c >= 300) return 'color:var(--ink-muted)';
  return '';
}
function bar_pct(int $v, int $max): string {
  $p = ($max > 0 && $v > 0) ? max(1, (int)round($v * 100 / $max)) : 0;
  return '<div style="background:var(--border);border-radius:3px;height:14px;overflow:hidden">'
       . '<div style="background:var(--red-stamp);height:14px;width:' . $p . '%"></div></div>';
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
<title>RSSIntel — Accessi</title>

<?php render_header('RSSIntel — Accessi', 'accessi'); ?>

<div class="wrap">

  <?php if ($flash): ?>
    <div class="card"><b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?></div>
  <?php endif; ?>

  <div class="card">
    <b>Attività in corso</b> <span class="meta">(ultimi 5 minuti)</span>
    <hr>
    <?php if (!$active): ?>
      <div class="meta">Nessuna attività negli ultimi 5 minuti.</div>
    <?php else: ?>
      <div class="dtable">
        <table>
          <thead><tr>
            <th>Utente</th><th class="col-sec">Ruolo</th>
            <th>IP</th><th class="col-sec">Paese</th>
            <th>Ultima pagina</th><th class="col-sec">Quando</th>
            <th class="col-sec">Richieste</th>
          </tr></thead>
          <tbody>
          <?php foreach ($active as $a): ?>
            <tr>
              <td><?=h((string)($a['username'] ?: '—'))?></td>
              <td class="col-sec"><?php if ($a['role']): ?><span class="badge"><?=h((string)$a['role'])?></span><?php endif; ?></td>
              <td class="nowrap">
                <?=h((string)$a['ip'])?>
                <a href="https://ipinfo.io/<?=urlencode((string)$a['ip'])?>" target="_blank" rel="noopener noreferrer" title="ipinfo.io">🔍</a>
              </td>
              <td class="col-sec"><?=h((string)($geo[$a['ip']] ?? ''))?></td>
              <td class="meta"><?=h((string)$a['method'])?> <?=h((string)$a['path'])?></td>
              <td class="meta col-sec"><?=h(fmt_dt((string)$a['ts']))?></td>
              <td class="col-sec"><?= (int)$a['reqs'] ?></td>
            </tr>
          <?php endforeach; ?>
          </tbody>
        </table>
      </div>
    <?php endif; ?>
  </div>

  <div class="card">
    <b>Riepilogo accessi</b>
    <hr>
    <div class="row" style="gap:24px; flex-wrap:wrap">
      <?php foreach ([
        'Richieste totali'  => $sum['req'],
        'IP unici'          => $sum['ips'],
        'Richieste oggi'    => $sum['today'],
        'Ultime 24h'        => $sum['d24'],
        'Utenti distinti'   => $sum['users'],
        'Login falliti 24h' => $fail24,
      ] as $k => $v): ?>
        <div style="min-width:120px">
          <div style="font-size:1.5rem"><b><?= number_format((int)$v, 0, ',', '.') ?></b></div>
          <div class="meta"><?=h($k)?></div>
        </div>
      <?php endforeach; ?>
    </div>
    <?php if (!ipgeo_enabled()): ?>
      <div class="meta" style="margin-top:8px">Geolocalizzazione IP disattivata (imposta <code>'ipgeo' =&gt; true</code> in config.php).</div>
    <?php endif; ?>
    <hr>
    <div class="row" style="gap:10px; flex-wrap:wrap; align-items:center">
      <div class="meta grow">
        <b>Conservazione</b>:
        <?php $ret = access_log_retention_days(); ?>
        <?php if ($ret > 0): ?>
          le righe piu' vecchie di <b><?= $ret ?> giorni</b> vengono eliminate da sole.
        <?php else: ?>
          <b>illimitata</b> (<code>'access_log_retention_days' =&gt; 0</code>).
        <?php endif; ?>
        Il log contiene IP, user-agent, referer e query string &mdash; per
        <code>search.php</code> quest'ultima e' il termine cercato.
      </div>
      <form method="post" class="row" style="gap:6px"
            onsubmit="return confirm('Eliminare definitivamente le righe di log piu\u2019 vecchie del numero di giorni indicato?')">
        <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
        <input type="hidden" name="action" value="prune">
        <input type="number" name="days" min="1" max="3650" value="<?= $ret > 0 ? $ret : 90 ?>"
               style="width:6em" title="giorni da conservare">
        <button class="btn" type="submit">Pota ora</button>
      </form>
    </div>
  </div>

  <div class="card">
    <b>Accessi per paese</b>
    <?php if (!ipgeo_enabled()): ?>
      <span class="meta">— geolocalizzazione disattivata</span>
    <?php elseif ($geo_pending > 0): ?>
      <span class="meta">— <?= (int)$geo_pending ?> IP ancora da risolvere: ricarica la pagina</span>
    <?php endif; ?>
    <hr>
    <?php if (!$by_country): ?>
      <div class="meta">
        <?php if (!ipgeo_enabled()): ?>
          Imposta <code>'ipgeo' =&gt; true</code> in <code>config.php</code> per risolvere i paesi.
        <?php else: ?>
          Nessun IP pubblico ancora geolocalizzato (ricarica finché <code>IP da risolvere</code> arriva a 0).
        <?php endif; ?>
      </div>
    <?php else: ?>
      <?php foreach ($by_country as $c): ?>
        <div class="row" style="gap:8px; margin:3px 0">
          <span class="meta" style="width:160px; flex:none">
            <?=h(flag_emoji((string)$c['cc']))?> <?=h((string)($c['cn'] ?: $c['cc']))?>
          </span>
          <span class="grow"><?= bar_pct((int)$c['reqs'], $country_max) ?></span>
          <span class="meta" style="flex:none; white-space:nowrap"><?= (int)$c['reqs'] ?> req · <?= (int)$c['ips'] ?> IP</span>
        </div>
      <?php endforeach; ?>
      <?php if ($geo_nocountry > 0): ?>
        <div class="meta" style="margin-top:8px">
          <?= number_format($geo_nocountry, 0, ',', '.') ?> richieste da IP di rete locale o non geolocalizzabili.
        </div>
      <?php endif; ?>
    <?php endif; ?>
  </div>

  <div class="card">
    <b>Log accessi</b> <span class="meta">(<?= number_format($total, 0, ',', '.') ?> righe nel filtro)</span>
    <hr>
    <form method="get" class="row" style="gap:8px; flex-wrap:wrap">
      <label class="meta">Dal <input type="date" name="date_from" value="<?=h($f_from)?>"></label>
      <label class="meta">Al <input type="date" name="date_to" value="<?=h($f_to)?>"></label>
      <select name="user">
        <option value="">tutti gli utenti</option>
        <?php foreach ($dUsers as $u): ?>
          <option value="<?=h((string)$u)?>" <?= $f_user === $u ? 'selected' : '' ?>><?=h((string)$u)?></option>
        <?php endforeach; ?>
      </select>
      <select name="path">
        <option value="">tutte le pagine</option>
        <?php foreach ($dPaths as $p): ?>
          <option value="<?=h((string)$p)?>" <?= $f_path === $p ? 'selected' : '' ?>><?=h((string)$p)?></option>
        <?php endforeach; ?>
      </select>
      <input name="q" value="<?=h($f_q)?>" placeholder="IP / user-agent / referer">
      <button class="btn" type="submit">Filtra</button>
      <a class="btn" href="accessi.php">Reset</a>
    </form>

    <hr>
    <div class="dtable">
      <table>
        <thead><tr>
          <th>Data/ora</th>
          <th>IP</th>
          <th class="col-sec">Paese</th>
          <th>Utente</th>
          <th class="col-sec">Ruolo</th>
          <th>Pagina</th>
          <th class="col-sec">M.</th>
          <th>St.</th>
          <th class="col-sec">User agent</th>
        </tr></thead>
        <tbody>
        <?php foreach ($rows as $x): ?>
          <tr>
            <td style="white-space:nowrap"><?=h(fmt_dt((string)$x['ts']))?></td>
            <td style="white-space:nowrap">
              <?=h((string)$x['ip'])?>
              <a href="https://ipinfo.io/<?=urlencode((string)$x['ip'])?>" target="_blank" rel="noopener noreferrer" title="ipinfo.io">🔍</a>
            </td>
            <td class="col-sec"><?=h((string)($geo[$x['ip']] ?? ''))?></td>
            <td><?=h((string)($x['username'] ?: '—'))?></td>
            <td class="col-sec"><?php if ($x['role']): ?><span class="badge"><?=h((string)$x['role'])?></span><?php endif; ?></td>
            <td><?=h((string)$x['path'])?><?= $x['query_string'] ? h('?' . $x['query_string']) : '' ?></td>
            <td class="col-sec"><?=h((string)$x['method'])?></td>
            <td style="<?= sc_style((int)$x['status_code']) ?>"><?= (int)$x['status_code'] ?: '—' ?></td>
            <td class="ua-cell col-sec meta" title="<?=h((string)$x['user_agent'])?>"><?=h((string)($x['user_agent'] ?: '—'))?></td>
          </tr>
        <?php endforeach; ?>
        <?php if (!$rows): ?>
          <tr><td colspan="9" class="meta" style="text-align:center">Nessun accesso registrato per il filtro corrente.</td></tr>
        <?php endif; ?>
        </tbody>
      </table>
    </div>

    <?php if ($pages > 1): ?>
      <div class="btns" style="justify-content:center; margin-top:12px">
        <?php if ($page > 1): ?><a class="btn" href="<?=h(acc_page_url($page - 1))?>">◀</a><?php endif; ?>
        <?php for ($p = max(1, $page - 2); $p <= min($pages, $page + 2); $p++): ?>
          <a class="btn <?= $p === $page ? 'active' : '' ?>" href="<?=h(acc_page_url($p))?>"><?=$p?></a>
        <?php endfor; ?>
        <?php if ($page < $pages): ?><a class="btn" href="<?=h(acc_page_url($page + 1))?>">▶</a><?php endif; ?>
        <span class="meta">pag. <?=$page?>/<?=$pages?></span>
      </div>
    <?php endif; ?>
  </div>

  <div class="card">
    <b>Login recenti</b>
    <hr>
    <?php if ($failByIp): ?>
      <div class="meta" style="margin-bottom:8px">
        Falliti nelle ultime 24h per IP:
        <?php foreach ($failByIp as $fi): ?>
          <span class="badge"><?=h((string)$fi['ip'])?> ×<?= (int)$fi['c'] ?></span>
        <?php endforeach; ?>
      </div>
    <?php endif; ?>
    <div class="dtable">
      <table>
        <thead><tr>
          <th>Data/ora</th><th>Esito</th>
          <th>Utente</th><th>IP</th>
        </tr></thead>
        <tbody>
        <?php foreach ($attempts as $a): ?>
          <tr>
            <td style="white-space:nowrap"><?=h(fmt_dt((string)$a['created_at']))?></td>
            <td>
              <?= (int)$a['success'] === 1
                ? '<span style="color:green">ok</span>'
                : '<span style="color:var(--red-stamp)">fallito</span>' ?>
            </td>
            <td><?=h((string)($a['username'] ?: '—'))?></td>
            <td style="white-space:nowrap"><?=h((string)$a['ip'])?></td>
          </tr>
        <?php endforeach; ?>
        <?php if (!$attempts): ?>
          <tr><td colspan="4" class="meta" style="text-align:center">Nessun tentativo di login registrato.</td></tr>
        <?php endif; ?>
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <b>Per utente</b>
    <hr>
    <div class="dtable">
      <table>
        <thead><tr>
          <th>Utente</th><th>Ruolo</th>
          <th>Ultimo accesso</th><th class="col-sec">Ultimo IP</th>
          <th class="col-sec">Richieste</th><th class="col-sec">IP distinti</th>
          <th>Ultima attività</th>
        </tr></thead>
        <tbody>
        <?php foreach ($per_user as $u): ?>
          <tr>
            <td><?=h((string)$u['username'])?><?php if ((int)$u['disabled'] === 1): ?> <span class="badge">off</span><?php endif; ?></td>
            <td><span class="badge"><?=h((string)$u['role'])?></span></td>
            <td class="meta"><?=h(fmt_dt((string)$u['last_login_at']) ?: 'mai')?></td>
            <td class="col-sec meta" style="white-space:nowrap">
              <?=h((string)($u['last_ip'] ?: '—'))?>
              <?php if (!empty($u['last_ip']) && !empty($geo[$u['last_ip']])): ?> <?=h((string)$geo[$u['last_ip']])?><?php endif; ?>
            </td>
            <td class="col-sec"><?= (int)$u['reqs'] ?></td>
            <td class="col-sec"><?= (int)$u['ips'] ?></td>
            <td class="meta"><?=h(fmt_dt((string)$u['last_seen']) ?: '—')?></td>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
  </div>

</div>
