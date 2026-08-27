<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_role('admin');

$me  = auth_user();
$dbw = db_rw();
users_ensure($dbw);

function active_admins(SQLite3 $db, int $except = 0): int {
  $st = $db->prepare("SELECT COUNT(*) c FROM users WHERE role='admin' AND disabled=0 AND id <> :x");
  $st->bindValue(':x', $except, SQLITE3_INTEGER);
  return (int)$st->execute()->fetchArray(SQLITE3_ASSOC)['c'];
}

$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }
  $action = (string)($_POST['action'] ?? '');
  $id     = (int)($_POST['id'] ?? 0);

  try {
    if ($action === 'create') {
      $username = trim((string)($_POST['username'] ?? ''));
      $password = (string)($_POST['password'] ?? '');
      $role     = (string)($_POST['role'] ?? 'reader');

      if (!preg_match('~^[A-Za-z0-9._-]{2,32}$~', $username)) {
        throw new RuntimeException('Username non valido (2-32: lettere, cifre, . _ -).');
      }
      if (strlen($password) < 8) {
        throw new RuntimeException('Password troppo corta (minimo 8 caratteri).');
      }
      if (!in_array($role, RSSINTEL_ROLES, true)) {
        throw new RuntimeException('Ruolo non valido.');
      }
      $st = $dbw->prepare(
        "INSERT INTO users(username, password_hash, role, created_by)
         VALUES(:u, :h, :r, :by)"
      );
      $st->bindValue(':u', $username, SQLITE3_TEXT);
      $st->bindValue(':h', password_hash($password, PASSWORD_DEFAULT), SQLITE3_TEXT);
      $st->bindValue(':r', $role, SQLITE3_TEXT);
      $st->bindValue(':by', current_user(), SQLITE3_TEXT);
      $st->execute();
      $_SESSION['flash'] = ['ok', 'Utente «' . $username . '» creato (' . $role . ').'];
    }

    elseif ($action === 'set_role') {
      $role = (string)($_POST['role'] ?? '');
      if (!in_array($role, RSSINTEL_ROLES, true)) {
        throw new RuntimeException('Ruolo non valido.');
      }
      if ($id === (int)$me['id'] && $role !== 'admin') {
        throw new RuntimeException('Non puoi togliere a te stesso il ruolo admin.');
      }
      if ($role !== 'admin' && active_admins($dbw, $id) === 0) {
        $cur = $dbw->querySingle("SELECT role FROM users WHERE id=" . $id);
        if ($cur === 'admin') {
          throw new RuntimeException('Deve restare almeno un amministratore attivo.');
        }
      }
      $st = $dbw->prepare("UPDATE users SET role=:r WHERE id=:id");
      $st->bindValue(':r', $role, SQLITE3_TEXT);
      $st->bindValue(':id', $id, SQLITE3_INTEGER);
      $st->execute();
      $_SESSION['flash'] = ['ok', 'Ruolo aggiornato.'];
    }

    elseif ($action === 'toggle_disabled') {
      if ($id === (int)$me['id']) {
        throw new RuntimeException('Non puoi disabilitare te stesso.');
      }
      $row = $dbw->querySingle("SELECT id, role, disabled FROM users WHERE id=" . $id, true);
      if (!$row) throw new RuntimeException('Utente non trovato.');
      $new = ((int)$row['disabled'] === 1) ? 0 : 1;
      if ($new === 1 && $row['role'] === 'admin' && active_admins($dbw, $id) === 0) {
        throw new RuntimeException('Deve restare almeno un amministratore attivo.');
      }
      $dbw->exec("UPDATE users SET disabled=$new WHERE id=" . $id);
      $_SESSION['flash'] = ['ok', $new ? 'Utente disabilitato.' : 'Utente riabilitato.'];
    }

    elseif ($action === 'reset_password') {
      $password = (string)($_POST['password'] ?? '');
      if (strlen($password) < 8) {
        throw new RuntimeException('Password troppo corta (minimo 8 caratteri).');
      }
      $st = $dbw->prepare("UPDATE users SET password_hash=:h WHERE id=:id");
      $st->bindValue(':h', password_hash($password, PASSWORD_DEFAULT), SQLITE3_TEXT);
      $st->bindValue(':id', $id, SQLITE3_INTEGER);
      $st->execute();
      $_SESSION['flash'] = ['ok', 'Password reimpostata.'];
    }

    elseif ($action === 'delete') {
      if ($id === (int)$me['id']) {
        throw new RuntimeException('Non puoi eliminare te stesso.');
      }
      $row = $dbw->querySingle("SELECT role, disabled FROM users WHERE id=" . $id, true);
      if (!$row) throw new RuntimeException('Utente non trovato.');
      if ($row['role'] === 'admin' && (int)$row['disabled'] === 0 && active_admins($dbw, $id) === 0) {
        throw new RuntimeException('Deve restare almeno un amministratore attivo.');
      }
      $dbw->exec("DELETE FROM users WHERE id=" . $id);
      $_SESSION['flash'] = ['ok', 'Utente eliminato.'];
    }
  } catch (Throwable $e) {
    $_SESSION['flash'] = ['err', $e->getMessage()];
  }

  header('Location: users.php');
  exit;
}

$users = [];
$res = $dbw->query("SELECT id, username, role, disabled, created_by, created_at, last_login_at
                    FROM users ORDER BY username COLLATE NOCASE ASC");
while ($r = $res->fetchArray(SQLITE3_ASSOC)) $users[] = $r;
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css">
<title>RSSIntel — Utenti</title>

<?php render_header('RSSIntel — Utenti', 'users'); ?>

<div class="wrap">
  <?php if ($flash): ?>
    <div class="card"><b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?></div>
  <?php endif; ?>

  <div class="card">
    <b>Nuovo utente</b>
    <form method="post" class="row" style="margin-top:10px; gap:8px; flex-wrap:wrap">
      <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
      <input type="hidden" name="action" value="create">
      <input name="username" placeholder="username" pattern="[A-Za-z0-9._-]{2,32}" required>
      <input type="password" name="password" placeholder="password (min 8)" minlength="8" required>
      <select name="role">
        <?php foreach (RSSINTEL_ROLES as $r): ?>
          <option value="<?=$r?>" <?= $r === 'reader' ? 'selected' : '' ?>><?=$r?></option>
        <?php endforeach; ?>
      </select>
      <button class="btn" type="submit">Crea</button>
    </form>
    <div class="meta" style="margin-top:8px">
      <b>reader</b>: sola lettura · <b>collaborator</b>: legge + annota/tagga ·
      <b>admin</b>: tutto + gestione feed e utenti.
    </div>
  </div>

  <div class="card">
    <b><?=count($users)?> utenti</b>
    <hr>
    <?php foreach ($users as $u): ?>
      <?php $self = ((int)$u['id'] === (int)$me['id']); ?>
      <div class="feed-entry">
        <div class="row" style="justify-content:space-between">
          <div class="grow">
            <b><?=h((string)$u['username'])?></b>
            <span class="badge"><?=h((string)$u['role'])?></span>
            <?php if ((int)$u['disabled'] === 1): ?><span class="badge">disabilitato</span><?php endif; ?>
            <?php if ($self): ?><span class="badge">tu</span><?php endif; ?>
            <div class="meta" style="margin-top:6px">
              creato: <?=h((string)($u['created_at'] ?? 'n/d'))?>
              <?php if (!empty($u['created_by'])): ?> da <?=h((string)$u['created_by'])?><?php endif; ?>
              · ultimo accesso: <?=h((string)($u['last_login_at'] ?: 'mai'))?>
            </div>
          </div>
        </div>

        <div class="row" style="margin-top:8px; gap:8px; flex-wrap:wrap">
          <form method="post" class="row" style="gap:4px">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="set_role">
            <input type="hidden" name="id" value="<?= (int)$u['id'] ?>">
            <select name="role">
              <?php foreach (RSSINTEL_ROLES as $r): ?>
                <option value="<?=$r?>" <?= $r === $u['role'] ? 'selected' : '' ?>><?=$r?></option>
              <?php endforeach; ?>
            </select>
            <button class="btn" type="submit"<?= $self ? ' disabled title="non su di te"' : '' ?>>Ruolo</button>
          </form>

          <form method="post" class="row" style="gap:4px">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="reset_password">
            <input type="hidden" name="id" value="<?= (int)$u['id'] ?>">
            <input type="password" name="password" placeholder="nuova password" minlength="8" required>
            <button class="btn" type="submit">Reset</button>
          </form>

          <form method="post">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="toggle_disabled">
            <input type="hidden" name="id" value="<?= (int)$u['id'] ?>">
            <button class="btn" type="submit"<?= $self ? ' disabled' : '' ?>>
              <?= (int)$u['disabled'] === 1 ? 'Riabilita' : 'Disabilita' ?>
            </button>
          </form>

          <form method="post" onsubmit="return confirm('Eliminare l\'utente «<?=h((string)$u['username'])?>»?')">
            <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
            <input type="hidden" name="action" value="delete">
            <input type="hidden" name="id" value="<?= (int)$u['id'] ?>">
            <button class="btn" type="submit"<?= $self ? ' disabled' : '' ?>>Elimina</button>
          </form>
        </div>
      </div>
      <hr>
    <?php endforeach; ?>
  </div>
</div>
