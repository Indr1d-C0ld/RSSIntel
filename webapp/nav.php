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

  $links = [
    'browse' => ['browse.php', '📰 Lettura'],
    'search' => ['search.php', 'Ricerca'],
    'notes'  => ['notes.php',  'Annotazioni'],
    'feeds'  => ['feeds.php',  'Feeds'],
  ];
  if ($u && $u['role'] === 'admin') {
    $links['users'] = ['users.php', 'Utenti'];
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
