<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';

require_role('admin');

$themes = available_themes();
$flash = $_SESSION['flash'] ?? null;
unset($_SESSION['flash']);

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
  if (!csrf_check()) { http_response_code(403); die('CSRF non valido'); }

  $theme = (string)($_POST['theme'] ?? '');
  if (!isset($themes[$theme])) {
    $_SESSION['flash'] = ['err', 'Tema sconosciuto.'];
  } else {
    try {
      $dbw = db_rw();
      set_active_theme($dbw, $theme);
      $_SESSION['flash'] = ['ok', 'Tema «' . $themes[$theme][0] . '» attivato per tutti gli utenti.'];
    } catch (Throwable $e) {
      $_SESSION['flash'] = ['err', $e->getMessage()];
    }
  }
  header('Location: theme.php');
  exit;
}

$current = active_theme();
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="<?= h(theme_href()) ?>">
<title>RSSIntel — Tema</title>

<?php render_header('RSSIntel — Tema grafico', 'theme'); ?>

<div class="wrap">
  <?php if ($flash): ?>
    <div class="card"><b><?= $flash[0] === 'ok' ? 'OK:' : 'Errore:' ?></b> <?=h((string)$flash[1])?></div>
  <?php endif; ?>

  <div class="card">
    <b>Tema grafico del portale</b>
    <div class="meta" style="margin-top:6px">
      Vale per <b>tutti gli utenti</b>: solo l'admin può cambiarlo. Anteprima indicativa
      dei colori — apri una pagina qualsiasi dopo l'attivazione per vederlo per intero.
    </div>
    <hr>

    <div class="theme-grid">
      <?php foreach ($themes as $key => [$name, $desc, $paperC, $inkC, $stampC, $mutedC]): ?>
        <div class="theme-card<?= $key === $current ? ' is-active' : '' ?>">
          <div class="theme-preview"
               style="--tp-typewriter:'Courier New',monospace; --tp-stamp:<?=h($stampC)?>; --tp-muted:<?=h($mutedC)?>;
                      background:<?=h($paperC)?>; color:<?=h($inkC)?>;">
            <span class="tp-head"><?=h($name)?></span>
            <div class="tp-line"></div>
            <div class="tp-line short"></div>
            <div class="tp-line"></div>
            <div class="tp-stamp">Riservato</div>
          </div>
          <div class="theme-card-foot">
            <div class="row" style="justify-content:space-between; align-items:flex-start">
              <div class="grow">
                <b><?=h($name)?></b>
                <?php if ($key === $current): ?><span class="badge red-stamp">attivo</span><?php endif; ?>
                <div class="meta" style="margin-top:4px"><?=h($desc)?></div>
              </div>
            </div>
            <?php if ($key !== $current): ?>
              <form method="post" style="margin-top:8px">
                <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
                <input type="hidden" name="theme" value="<?=h($key)?>">
                <button class="btn" type="submit">Attiva</button>
              </form>
            <?php endif; ?>
          </div>
        </div>
      <?php endforeach; ?>
    </div>
  </div>
</div>
