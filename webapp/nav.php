<?php
declare(strict_types=1);

/**
 * Header + navigazione condivisi da tutte le pagine HTML.
 * Richiede che lib.php sia gia' stato incluso.
 *
 *   render_header('RSSIntel — Ricerca', 'search');
 */
function render_header(string $title, string $active = ''): void {
  $u = auth_user();

  // Contatore delle corrispondenze non viste delle allerte. Se qualcosa va
  // storto (tabella assente, DB occupato) la navigazione deve comparire lo
  // stesso: il contatore e' un'informazione in piu', non una dipendenza.
  $novita = 0;
  if ($u) {
    try { $novita = watch_unseen_count(db_ro(), (string)$u['username']); }
    catch (Throwable $e) { $novita = 0; }
  }

  $links = [
    'bollettino'=> ['bollettino.php','🗞 Bollettino'],
    'novita'    => ['novita.php',    '🔔 Novità' . ($novita > 0 ? ' (' . $novita . ')' : '')],
    'browse'    => ['browse.php',    '📰 Lettura'],
    'search'    => ['search.php',    'Ricerca'],
    'favorites' => ['favorites.php', '★ Favoriti'],
    'notes'     => ['notes.php',     'Annotazioni'],
    'stats'     => ['stats.php',     '📊 Statistiche'],
    'feeds'     => ['feeds.php',     'Feeds'],
  ];
  if ($u && $u['role'] === 'admin') {
    $links['users']   = ['users.php',   'Utenti'];
    $links['accessi'] = ['accessi.php', '🔐 Accessi'];
    $links['theme']   = ['theme.php',   '🎨 Tema'];
  }
  ?>
  <header>
    <b><?=h($title)?></b>
    <div class="meta">
      <?php if ($u): ?>
        utente: <?=h((string)$u['username'])?> (<?=h((string)$u['role'])?>)
        <?php foreach ($links as $key => [$href, $label]): ?>
          · <a href="<?=h($href)?>"<?= $key === $active ? ' style="font-weight:bold"' : '' ?>><?=h($label)?></a>
        <?php endforeach; ?>
        · <a href="profile.php"<?= $active === 'profile' ? ' style="font-weight:bold"' : '' ?>>Profilo</a>
        · <a href="logout.php">Esci</a>
      <?php else: ?>
        <a href="login.php">Accedi</a>
      <?php endif; ?>
    </div>
  </header>
  <?php
}
