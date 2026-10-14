<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_login();
$me = auth_user();

$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);

/** La tabella dei collegamenti la crea il bot Telegram: senza bot non c'e'. */
function tg_links_exist(SQLite3 $db): bool {
  return (bool)$db->querySingle("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_links'");
}

if ($_SERVER['REQUEST_METHOD'] === 'POST'
    && (string)($_POST['action'] ?? '') === 'tg_unlink') {
  // Scollega un account Telegram: unica via di revoca dal web (l'unico
  // amministratore non si puo' disattivare). Il bot rilegge il collegamento a
  // ogni comando, quindi vale dal messaggio successivo.
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }
  try {
    $dbw = db_rw();
    if (!tg_links_exist($dbw)) throw new RuntimeException('Nessun account Telegram collegato.');
    $st = $dbw->prepare("
      UPDATE tg_links SET revoked_at = datetime('now')
      WHERE id = :id AND username = :u AND revoked_at IS NULL
    ");
    $st->bindValue(':id', (int)($_POST['link_id'] ?? 0), SQLITE3_INTEGER);
    $st->bindValue(':u', (string)$me['username'], SQLITE3_TEXT);
    $st->execute();
    $_SESSION['flash'] = $dbw->changes() > 0
      ? ['ok', "Account Telegram scollegato: il bot non risponde piu' a quell'account."]
      : ['err', "Collegamento non trovato o gia' revocato."];
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }
  header('Location: profile.php');
  exit;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }

  $cur  = (string)($_POST['current'] ?? '');
  $new  = (string)($_POST['new'] ?? '');
  $new2 = (string)($_POST['new2'] ?? '');

  try {
    $dbw = db_rw();
    $hash = (string)$dbw->querySingle(
      "SELECT password_hash FROM users WHERE id=" . (int)$me['id']
    );
    if (!$hash || !password_verify($cur, $hash)) {
      throw new RuntimeException('Password attuale errata.');
    }
    if ($e = password_length_error($new)) {
      throw new RuntimeException($e);
    }
    if ($new !== $new2) {
      throw new RuntimeException('Le due nuove password non coincidono.');
    }
    $st = $dbw->prepare("UPDATE users SET password_hash=:h WHERE id=:id");
    $st->bindValue(':h', password_hash($new, PASSWORD_DEFAULT), SQLITE3_TEXT);
    $st->bindValue(':id', (int)$me['id'], SQLITE3_INTEGER);
    $st->execute();
    $_SESSION['flash'] = ['ok', 'Password aggiornata.'];
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }
  header('Location: profile.php');
  exit;
}

$tg_links = null;   // null = bot mai avviato (tabella assente)
try {
  $db = db_ro();
  if (tg_links_exist($db)) {
    $tg_links = [];
    $st = $db->prepare("
      SELECT id, linked_at, last_seen_at, blocked_at FROM tg_links
      WHERE username = :u AND revoked_at IS NULL ORDER BY id
    ");
    $st->bindValue(':u', (string)$me['username'], SQLITE3_TEXT);
    $r = $st->execute();
    while ($x = $r->fetchArray(SQLITE3_ASSOC)) $tg_links[] = $x;
  }
} catch (Throwable $e) {
  $tg_links = null;
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
<title>RSSIntel — Profilo</title>

<?php render_header('RSSIntel — Profilo', 'profile'); ?>

<div class="wrap">
  <?php if ($flash): ?>
    <div class="card"><b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?></div>
  <?php endif; ?>

  <div class="card">
    <b>Account</b>
    <div class="meta" style="margin-top:6px">
      utente: <?=h((string)$me['username'])?> · ruolo: <?=h((string)$me['role'])?>
    </div>
  </div>

  <?php if ($tg_links !== null): ?>
  <div class="card">
    <b>Telegram</b>
    <?php if (!$tg_links): ?>
      <div class="meta" style="margin-top:6px">Nessun account Telegram collegato.</div>
      <?php if (is_admin()): ?>
        <div class="meta" style="margin-top:6px">
          Per collegarne uno genera un codice monouso sul server:
          <code>sudo -u rssintel-bot env RSSINTEL_DB=… /opt/rssintel-bot/venv/bin/python
          /opt/rssintel-bot/rssintel_bot.py codice <?=h((string)$me['username'])?></code>,
          poi invialo al bot con <code>/start CODICE</code> (vale 15 minuti).
        </div>
      <?php endif; ?>
    <?php else: ?>
      <?php foreach ($tg_links as $l): ?>
        <div class="row" style="justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; margin-top:8px">
          <div class="meta">
            collegato il <?=h(fmt_dt((string)$l['linked_at']))?>
            · ultimo uso <?=h($l['last_seen_at'] ? fmt_dt((string)$l['last_seen_at']) : '—')?>
            <?php if (!empty($l['blocked_at'])): ?> · <b>bot bloccato dall'utente</b><?php endif; ?>
          </div>
          <form method="post">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="tg_unlink">
            <input type="hidden" name="link_id" value="<?= (int)$l['id'] ?>">
            <button class="btn" type="submit">Scollega Telegram</button>
          </form>
        </div>
      <?php endforeach; ?>
      <div class="meta" style="margin-top:8px">
        Scollegando, il bot smette subito di rispondere a quell'account. Per
        ricollegarlo serve un codice nuovo.
      </div>
    <?php endif; ?>
  </div>
  <?php endif; ?>

  <div class="card">
    <b>Cambia password</b>
    <form method="post" style="margin-top:10px" autocomplete="off">
      <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
      <div class="meta">Password attuale</div>
      <input type="password" name="current" required>
      <div class="meta" style="margin-top:8px">Nuova password (min 8 caratteri)</div>
      <input type="password" name="new" minlength="8" required>
      <div class="meta" style="margin-top:8px">Ripeti nuova password</div>
      <input type="password" name="new2" minlength="8" required>
      <div style="margin-top:12px"><button class="btn" type="submit">Aggiorna</button></div>
    </form>
  </div>
</div>
