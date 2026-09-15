<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Gia' autenticato: vai alla ricerca.
if (auth_user() !== null) {
  header('Location: search.php');
  exit;
}

/** Consente solo redirect verso una pagina .php locale (anti open-redirect). */
function safe_next(string $n): string {
  if (preg_match('~([A-Za-z0-9_]+\.php(?:\?[^#\s]*)?)$~', trim($n), $m)) {
    return $m[1];
  }
  return 'search.php';
}

$dbw = db_rw();
users_ensure($dbw);
$nUsers = (int)$dbw->querySingle("SELECT COUNT(*) FROM users");
$bootstrap = ($nUsers === 0);

$err  = '';
$next = safe_next((string)($_POST['next'] ?? $_GET['next'] ?? ''));

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) {
    http_response_code(403);
    die('CSRF non valido');
  }

  $username = trim((string)($_POST['username'] ?? ''));
  $password = (string)($_POST['password'] ?? '');
  $ip = client_ip();

  // Rate-limit morbido: troppi tentativi falliti dallo stesso IP -> attesa.
  // Vale anche in bootstrap: li' la creazione del primo admin e' anonima, e
  // senza limite sarebbe un bersaglio comodo se la tabella users si svuotasse.
  if (recent_failed_logins($dbw, $ip, 15) >= 10) {
    $err = 'Troppi tentativi falliti. Riprova tra qualche minuto.';
    record_login_attempt($username, $ip, false);
  } elseif ($bootstrap) {
    $password2 = (string)($_POST['password2'] ?? '');
    if (!preg_match('~^[A-Za-z0-9._-]{2,32}$~', $username)) {
      $err = 'Username non valido (2-32 caratteri: lettere, cifre, . _ -).';
    } elseif ($e = password_length_error($password)) {
      $err = $e;
    } elseif ($password !== $password2) {
      $err = 'Le due password non coincidono.';
    } else {
      $st = $dbw->prepare(
        "INSERT INTO users(username, password_hash, role, created_by)
         VALUES(:u, :h, 'admin', '(bootstrap)')"
      );
      $st->bindValue(':u', $username, SQLITE3_TEXT);
      $st->bindValue(':h', password_hash($password, PASSWORD_DEFAULT), SQLITE3_TEXT);
      $st->execute();
      $uid = (int)$dbw->lastInsertRowID();

      session_regenerate_id(true);
      $_SESSION['uid']   = $uid;
      $_SESSION['uname'] = $username;
      $_SESSION['role']  = 'admin';
      $dbw->exec("UPDATE users SET last_login_at=datetime('now') WHERE id=" . $uid);

      header('Location: users.php');
      exit;
    }
  } else {
    $st = $dbw->prepare(
      "SELECT id, username, password_hash, role, disabled FROM users WHERE username = :u"
    );
    $st->bindValue(':u', $username, SQLITE3_TEXT);
    $row = $st->execute()->fetchArray(SQLITE3_ASSOC);

    // Verifica la password SEMPRE, anche quando l'utente non esiste: il
    // corto-circuito `!$row || !password_verify(...)` faceva rispondere subito
    // per un utente inesistente e dopo ~80ms di bcrypt per uno esistente,
    // rivelando quali username sono validi. L'hash fittizio ha lo stesso
    // costo (bcrypt 12, ~290ms), quindi i due rami sono indistinguibili.
    $dummy_hash = '$2y$12$9yivyWVJC118NZI6TCIo2O.65wADgKPbN3SC9yhMWjtbYecchtuku';
    $hash  = $row ? (string)$row['password_hash'] : $dummy_hash;
    $ok_pw = password_verify($password, $hash);

    if (!$row || (int)$row['disabled'] === 1 || !$ok_pw) {
      $err = 'Credenziali non valide.';
      record_login_attempt($username, $ip, false);
      usleep(300000); // piccolo rallentamento anti brute-force
    } else {
      record_login_attempt($username, $ip, true);
      session_regenerate_id(true);
      $_SESSION['uid']   = (int)$row['id'];
      $_SESSION['uname'] = (string)$row['username'];
      $_SESSION['role']  = (string)$row['role'];
      $dbw->exec("UPDATE users SET last_login_at=datetime('now') WHERE id=" . (int)$row['id']);

      if (password_needs_rehash((string)$row['password_hash'], PASSWORD_DEFAULT)) {
        $u = $dbw->prepare("UPDATE users SET password_hash=:h WHERE id=:id");
        $u->bindValue(':h', password_hash($password, PASSWORD_DEFAULT), SQLITE3_TEXT);
        $u->bindValue(':id', (int)$row['id'], SQLITE3_INTEGER);
        $u->execute();
      }

      header('Location: ' . safe_next((string)($_POST['next'] ?? '')));
      exit;
    }
  }
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
<title>RSSIntel — <?= $bootstrap ? 'Primo accesso' : 'Accesso' ?></title>

<header>
  <b>RSSIntel</b>
  <div class="meta"><?= $bootstrap ? 'Creazione del primo amministratore' : 'Accesso riservato' ?></div>
</header>

<div class="wrap">
  <?php if ($err): ?>
    <div class="card"><b>Errore:</b> <?=h($err)?></div>
  <?php endif; ?>

  <form method="post" class="card" autocomplete="off">
    <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
    <input type="hidden" name="next" value="<?=h($next)?>">

    <?php if ($bootstrap): ?>
      <div class="meta" style="margin-bottom:10px">
        Non esiste ancora nessun utente. Crea l'account amministratore: da qui
        potrai poi creare gli altri utenti con i rispettivi ruoli.
      </div>
    <?php endif; ?>

    <div class="meta">Username</div>
    <input name="username" autofocus required
           value="<?=h((string)($_POST['username'] ?? ''))?>"
           <?= $bootstrap ? 'pattern="[A-Za-z0-9._-]{2,32}"' : '' ?>>

    <div class="meta" style="margin-top:8px">Password<?= $bootstrap ? ' (min 8 caratteri)' : '' ?></div>
    <input type="password" name="password" required <?= $bootstrap ? 'minlength="8"' : '' ?>>

    <?php if ($bootstrap): ?>
      <div class="meta" style="margin-top:8px">Ripeti password</div>
      <input type="password" name="password2" required minlength="8">
    <?php endif; ?>

    <div style="margin-top:12px">
      <button class="btn" type="submit"><?= $bootstrap ? 'Crea amministratore' : 'Accedi' ?></button>
    </div>
  </form>
</div>
