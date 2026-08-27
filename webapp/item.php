<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';
require __DIR__ . '/nav.php';
require_login();

function stopwords(): array {
  static $sw = null;
  if ($sw === null) {
    $f = __DIR__ . '/stopwords.php';
    $list = is_file($f) ? require $f : [];
    // normalizza a minuscolo per confronto coerente con extract_keywords()
    $sw = array_fill_keys(array_map(
      static fn($w) => mb_strtolower((string)$w, 'UTF-8'),
      is_array($list) ? $list : []
    ), true);
  }
  return $sw;
}

function extract_keywords(string $text, int $limit = 8): array {
  $text = mb_strtolower($text, 'UTF-8');
  $text = preg_replace('/[^\p{L}\s]/u', ' ', $text);
  $text = preg_replace('/\s+/', ' ', $text);

  $words = explode(' ', $text);
  $freq = [];
  $stop = stopwords(); // gia' come mappa [parola => true]

  foreach ($words as $w) {
    $w = trim($w);
    if ($w === '') continue;
    if (mb_strlen($w, 'UTF-8') < 4) continue;
    if (isset($stop[$w])) continue;
    $freq[$w] = ($freq[$w] ?? 0) + 1;
  }

  arsort($freq);

  // Scarta gli hapax (1 sola occorrenza): la frequenza e' significativa solo
  // se la parola ricorre. Se cosi' restano meno di $limit candidati (testi
  // brevi), ripiega sull'elenco completo per non svuotare la sezione.
  $strong = array_filter($freq, static fn($c) => $c >= 2);
  $pool = count($strong) >= $limit ? $strong : $freq;

  return array_slice(array_keys($pool), 0, $limit);
}

function read_text_maybe_gz(string $path): string {
  if ($path === '' || !is_file($path)) return '';

  if (str_ends_with($path, '.gz')) {
    $raw = @file_get_contents($path);
    if ($raw === false) return '';
    $dec = @gzdecode($raw);
    return ($dec === false) ? '' : (string)$dec;
  }

  $raw = @file_get_contents($path);
  return ($raw === false) ? '' : (string)$raw;
}

$id_raw = trim((string)($_GET['id'] ?? ''));
if ($id_raw === '' || !ctype_digit($id_raw)) die("id mancante/non valido");
$item_id = (int)$id_raw;

$db = db_ro();

$stmt = $db->prepare("
  SELECT i.id, i.feed_id, i.guid, i.title, i.link, i.author,
         i.published_at, i.fetched_at, i.raw_path, i.text_path,
         COALESCE(f.title, f.url) AS feed_title, f.url AS feed_url
  FROM items i
  JOIN feeds f ON f.id = i.feed_id
  WHERE i.id = :id
");
$stmt->bindValue(':id', $item_id, SQLITE3_INTEGER);
$row = $stmt->execute()->fetchArray(SQLITE3_ASSOC);
if (!$row) die("Item non trovato: ".h((string)$item_id));

$text = '';
if (!empty($row['text_path'])) {
  $text = read_text_maybe_gz((string)$row['text_path']);
}

$keywords = $text !== '' ? extract_keywords($text, 10) : [];
$text_html = h($text);

$may_annotate = can_annotate();

$notes = [];
$item_tags = [];

$st = $db->prepare("
  SELECT DISTINCT t.name, COUNT(*) AS cnt
  FROM tags t
  JOIN annotation_tags at ON at.tag_id = t.id
  JOIN annotations a ON a.id = at.annotation_id
  WHERE a.item_id = :iid
  GROUP BY t.name
  ORDER BY cnt DESC, t.name ASC
");
$st->bindValue(':iid', $item_id, SQLITE3_INTEGER);
$res = $st->execute();
while ($r = $res->fetchArray(SQLITE3_ASSOC)) $item_tags[] = $r;

$stmt2 = $db->prepare("
  SELECT id, item_id, note, quote, author, created_at, updated_at
  FROM annotations
  WHERE item_id = :iid
  ORDER BY created_at DESC
");
$stmt2->bindValue(':iid', $item_id, SQLITE3_INTEGER);
$res2 = $stmt2->execute();
while ($n = $res2->fetchArray(SQLITE3_ASSOC)) $notes[] = $n;

$related_items = [];
$related_map = [];

if ($item_tags) {
  $tag_names = array_column($item_tags, 'name');
  $ph = implode(',', array_fill(0, count($tag_names), '?'));

  $sql = "
    SELECT a.item_id, COUNT(DISTINCT t.name) AS shared_tags
    FROM annotations a
    JOIN annotation_tags at ON at.annotation_id = a.id
    JOIN tags t ON t.id = at.tag_id
    WHERE t.name IN ($ph)
      AND a.item_id <> ?
    GROUP BY a.item_id
    ORDER BY shared_tags DESC, a.item_id DESC
    LIMIT 10
  ";
  $stmtR = $db->prepare($sql);

  $i = 1;
  foreach ($tag_names as $tn) $stmtR->bindValue($i++, (string)$tn, SQLITE3_TEXT);
  $stmtR->bindValue($i, $item_id, SQLITE3_INTEGER);

  $resR = $stmtR->execute();
  while ($r = $resR->fetchArray(SQLITE3_ASSOC)) $related_items[] = $r;

  if ($related_items) {
    $ids = array_map(fn($x) => (int)$x['item_id'], $related_items);
    $in = implode(',', array_fill(0, count($ids), '?'));

    $sql2 = "
      SELECT i.id, i.title, COALESCE(f.title, f.url) AS feed_title
      FROM items i
      JOIN feeds f ON f.id = i.feed_id
      WHERE i.id IN ($in)
    ";
    $st2 = $db->prepare($sql2);
    $k = 1;
    foreach ($ids as $rid) $st2->bindValue($k++, $rid, SQLITE3_INTEGER);
    $res3 = $st2->execute();
    while ($rr = $res3->fetchArray(SQLITE3_ASSOC)) $related_map[(int)$rr['id']] = $rr;
  }
}
?>
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="assets/style.css">
<title><?=h((string)$item_id)?></title>

<?php render_header('Item ' . (string)$item_id . (!empty($row['feed_title']) ? ' — ' . (string)$row['feed_title'] : '')); ?>

<div class="wrap grid">
  <div class="card">
    <b>Dettagli</b>

    <?php if (!empty($row['title'])): ?>
      <div class="small" style="margin-top:8px"><b><?=h((string)$row['title'])?></b></div>
    <?php endif; ?>

    <div class="meta" style="margin-top:6px">
      Pubblicato: <?=h((string)($row['published_at'] ?: 'n/d'))?> ·
      Fetch: <?=h((string)$row['fetched_at'])?>
    </div>

    <?php if (!empty($row['author'])): ?>
      <div class="meta">Autore: <?=h((string)$row['author'])?></div>
    <?php endif; ?>

    <?php if (!empty($row['link'])): ?>
      <div class="meta" style="margin-top:6px">
        Fonte: <a href="<?=h((string)$row['link'])?>" target="_blank"><?=h((string)$row['link'])?></a>
      </div>
    <?php endif; ?>

    <div class="meta" style="margin-top:6px">
      Feed URL: <?=h((string)$row['feed_url'])?>
    </div>

    <?php if (!empty($row['guid'])): ?>
      <div class="meta">GUID: <?=h((string)$row['guid'])?></div>
    <?php endif; ?>

    <?php if ($keywords): ?>
      <hr>
      <b>Correlazioni rapide</b>
      <div class="small" style="margin-top:6px">Parole più frequenti nel testo:</div>
      <div class="row" style="margin-top:8px">
        <?php foreach ($keywords as $kw): ?>
          <a class="badge" href="search.php?q=<?=urlencode($kw)?>" title="Cerca occorrenze di &quot;<?=h($kw)?>&quot;">
            <?=h($kw)?>
          </a>
        <?php endforeach; ?>
      </div>
      <div class="meta" style="margin-top:8px">
        Correlazione principale:
        <a href="search.php?q=<?=urlencode($keywords[0])?>">cerca “<?=h($keywords[0])?>”</a>
      </div>
    <?php endif; ?>

    <hr>
    <b>Tag associati a questo item</b>

    <?php if (!$item_tags): ?>
      <div class="meta">Nessun tag associato.</div>
    <?php else: ?>
      <div class="row" style="margin-top:8px">
        <?php foreach ($item_tags as $t): ?>
          <a class="badge"
             href="notes.php?tag=<?=urlencode((string)$t['name'])?>"
             title="Mostra annotazioni con tag &quot;<?=h((string)$t['name'])?>&quot;">
            <?=h((string)$t['name'])?> <span class="meta">(<?= (int)$t['cnt'] ?>)</span>
          </a>
        <?php endforeach; ?>
      </div>
    <?php endif; ?>

    <hr>
    <b>Items correlati (per TAG)</b>

    <?php if (!$related_items): ?>
      <div class="meta">Nessuna correlazione trovata.</div>
    <?php else: ?>
      <ul class="small">
        <?php foreach ($related_items as $ri): ?>
          <?php
            $rid = (int)$ri['item_id'];
            $info = $related_map[$rid] ?? null;
          ?>
          <li>
            <a href="item.php?id=<?=urlencode((string)$rid)?>"><?=h((string)$rid)?></a>
            <?php if ($info && !empty($info['feed_title'])): ?>
              <span class="badge"><?=h((string)$info['feed_title'])?></span>
            <?php endif; ?>
            <span class="meta">— tag condivisi: <?= (int)$ri['shared_tags'] ?></span>
            <?php if ($info && !empty($info['title'])): ?>
              <div class="meta"><?=h((string)$info['title'])?></div>
            <?php endif; ?>
          </li>
        <?php endforeach; ?>
      </ul>
    <?php endif; ?>

    <hr>
    <b>Testo estratto</b>
    <?php if ($text === ''): ?>
      <div class="meta">Testo non disponibile (text_path mancante o file non presente).</div>
    <?php else: ?>
      <div class="text-scroll">
        <pre id="article-text"><?=$text_html?></pre>
      </div>

      <!-- Sezione Traduzione (motore locale EN->IT, solo sulla selezione) -->
      <div style="margin-top:10px; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
        <button id="translate-sel-btn" class="btn" type="button">Traduci selezione</button>
        <button id="translate-stop-btn" class="btn" type="button" style="display:none;">Ferma traduzione</button>
        <span id="translate-counter" class="meta" style="margin-left:8px">nessuna selezione</span>
        <span id="translate-status" class="meta" style="margin-left:8px"></span>
      </div>
      <?php $tsl = (int)(cfg()['translate_soft_limit'] ?? 2000); ?>
      <div class="meta" id="translate-note" style="margin-top:6px">
        Il motore traduce <b>solo dall'inglese all'italiano</b> e al massimo
        <b>~<span id="translate-limit-words"><?= (int)round($tsl / 6.2) ?></span> parole</b>
        (<span id="translate-limit-chars"><?= $tsl ?></span> caratteri) per volta:
        seleziona un brano nel testo qui sopra e premi <b>Traduci selezione</b>.
        L'eventuale parte eccedente il limite (in rosso nell'anteprima) non viene inviata al motore.
      </div>
      <div id="translate-preview" style="border:1px dashed var(--border); padding:8px; margin-top:8px; display:none; max-height:20vh; overflow:auto; font-size:.85rem; white-space:pre-wrap; word-wrap:break-word;"></div>
      <div id="translation-output" style="border:1px solid var(--border); padding:10px; margin-top:10px; display:none; max-height:30vh; overflow:auto; background:#fefaf2;"></div>
    <?php endif; ?>
  </div>

  <div class="card">
    <b>Annotazioni</b>

    <?php if ($may_annotate): ?>
    <hr>
    <b>Aggiungi annotazione</b>
    <form id="addForm" style="margin-top:8px">
      <input type="hidden" name="csrf" value="<?=h(csrf_token())?>">
      <input type="hidden" name="action" value="add">
      <input type="hidden" name="item_id" value="<?=h((string)$item_id)?>">

      <div class="meta">Quote (facoltativo)</div>
      <input name="quote" placeholder="incolla estratto breve…">

      <div class="meta" style="margin-top:8px">Tag (facoltativi, separati da virgola)</div>
      <input name="tags" placeholder="es: leak, source, timeline">

      <div class="meta" style="margin-top:8px">Nota (obbligatoria)</div>
      <textarea name="note" placeholder="annotazione…"></textarea>

      <button class="btn" type="submit">Salva</button>
      <span id="msg" class="meta"></span>
    </form>
    <?php else: ?>
    <div class="meta" style="margin-top:8px">Sola lettura: il tuo ruolo non consente di annotare.</div>
    <?php endif; ?>

    <hr>
    <b>Annotazioni (<?=count($notes)?>)</b>

    <?php if (!$notes): ?>
      <div class="meta">Nessuna annotazione per questo item.</div>
    <?php else: ?>
      <?php foreach ($notes as $n): ?>
        <div class="card" style="margin-top:10px" data-id="<?= (int)$n['id'] ?>">
          <div class="meta">
            <b><?=h((string)$n['author'])?></b> · <?=h((string)$n['created_at'])?>
            <?php if (!empty($n['updated_at'])): ?>
              · aggiornato: <?=h((string)$n['updated_at'])?>
            <?php endif; ?>
          </div>

          <?php if (!empty($n['quote'])): ?>
            <div class="meta">Quote: “<?=h((string)$n['quote'])?>”</div>
          <?php endif; ?>

          <div style="margin-top:8px"><?= nl2br(h((string)$n['note'])) ?></div>

          <?php if ($may_annotate && (is_admin() || (string)$n['author'] === current_user())): ?>
          <div style="margin-top:8px">
            <button class="btn" type="button" onclick="delNote(<?= (int)$n['id'] ?>)">Elimina</button>
          </div>
          <?php endif; ?>
        </div>
      <?php endforeach; ?>
    <?php endif; ?>
  </div>
</div>

<script>
// Limite pratico del motore di traduzione locale (caratteri), da config.php
window.TRANSLATE_SOFT_LIMIT = <?= (int)(cfg()['translate_soft_limit'] ?? 2000) ?>;

const form = document.getElementById('addForm');
const msg = document.getElementById('msg');

if (form) form.addEventListener('submit', async (e) => {
  e.preventDefault();
  msg.textContent = 'Salvataggio…';
  const fd = new FormData(form);
  const r = await fetch('annotations.php', { method: 'POST', body: fd });
  const j = await r.json().catch(() => null);
  if (!j || !j.ok) {
    msg.textContent = 'Errore: ' + (j?.error || 'imprevisto');
    return;
  }
  location.reload();
});

async function delNote(id) {
  if (!confirm('Eliminare annotazione #' + id + '?')) return;
  const fd = new FormData();
  fd.set('csrf', '<?=h(csrf_token())?>');
  fd.set('action', 'delete');
  fd.set('id', String(id));
  const r = await fetch('annotations.php', { method: 'POST', body: fd });
  const j = await r.json().catch(() => null);
  if (!j || !j.ok) {
    alert('Errore: ' + (j?.error || 'imprevisto'));
    return;
  }
  location.reload();
}

// --- Traduzione: motore locale EN->IT, solo sulla selezione, con limite ---
(function() {
  const btnSel   = document.getElementById('translate-sel-btn');
  const btnStop  = document.getElementById('translate-stop-btn');
  const status   = document.getElementById('translate-status');
  const counter  = document.getElementById('translate-counter');
  const preview  = document.getElementById('translate-preview');
  const output   = document.getElementById('translation-output');
  const article  = document.getElementById('article-text');

  if (!btnSel || !btnStop || !article) return;

  const LIMIT = Math.max(200, parseInt(window.TRANSLATE_SOFT_LIMIT, 10) || 2000);
  const LIMIT_WORDS = Math.round(LIMIT / 6.2);

  const lw = document.getElementById('translate-limit-words');
  const lc = document.getElementById('translate-limit-chars');
  if (lw) lw.textContent = String(LIMIT_WORDS);
  if (lc) lc.textContent = String(LIMIT);

  let currentController = null;
  let pendingSel = ''; // selezione catturata al mousedown sul bottone

  function setTranslating(isActive) {
    btnSel.disabled = isActive;
    btnStop.style.display = isActive ? '' : 'none';
  }

  // Rimuove newline/tab e collassa gli spazi multipli
  function cleanText(raw) {
    return raw.replace(/[\n\r\t]+/g, ' ').replace(/\s{2,}/g, ' ').trim();
  }

  function escapeHTML(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
              .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  function countWords(s) {
    s = s.trim();
    return s ? s.split(/\s+/).length : 0;
  }

  // Testo selezionato, solo se la selezione ricade dentro il corpo dell'articolo
  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return '';
    const r = sel.getRangeAt(0);
    if (!article.contains(r.commonAncestorContainer)) return '';
    return sel.toString();
  }

  // Aggiorna contatore + anteprima della selezione corrente
  function refresh() {
    const cleaned = cleanText(currentSelection());
    if (!cleaned) {
      counter.textContent = 'nessuna selezione';
      counter.style.color = '';
      preview.style.display = 'none';
      preview.innerHTML = '';
      return;
    }
    const chars = cleaned.length;
    const over  = chars > LIMIT;
    counter.textContent = countWords(cleaned) + ' parole · ' + chars + '/' + LIMIT +
                          ' caratteri' + (over ? ' — eccede il limite' : '');
    counter.style.color = over ? '#b13b2d' : '';

    const head = escapeHTML(cleaned.slice(0, LIMIT));
    const tail = escapeHTML(cleaned.slice(LIMIT));
    preview.innerHTML = head + (tail
      ? '<span style="background:#f6d9d4;color:#8a2e22;text-decoration:line-through;">' + tail + '</span>'
      : '');
    preview.style.display = 'block';
  }

  let debounce = null;
  document.addEventListener('selectionchange', () => {
    clearTimeout(debounce);
    debounce = setTimeout(refresh, 120);
  });

  async function translateText(text) {
    if (currentController) currentController.abort();
    currentController = new AbortController();

    setTranslating(true);
    status.textContent = 'Traduzione in corso…';
    output.style.display = 'none';
    output.innerHTML = '';

    try {
      const response = await fetch('translate.php', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q: text, source: 'en', target: 'it' }),
        credentials: 'same-origin',
        signal: currentController.signal
      });

      if (!response.ok) {
        const err = await response.text();
        throw new Error('Errore HTTP ' + response.status + ': ' + err);
      }

      const data = await response.json();
      const translated = data.translatedText || '';

      output.innerHTML = '<b>Traduzione:</b><br><pre style="white-space:pre-wrap;word-wrap:break-word;margin:0">' +
                         escapeHTML(translated) + '</pre>';
      output.style.display = 'block';
      status.textContent = 'Traduzione completata.';
    } catch (error) {
      if (error.name === 'AbortError') {
        status.textContent = 'Traduzione fermata.';
      } else {
        console.error(error);
        status.textContent = 'Errore: ' + error.message;
      }
    } finally {
      setTranslating(false);
      currentController = null;
    }
  }

  // Cattura la selezione prima che il click sul bottone possa perderla
  btnSel.addEventListener('mousedown', () => { pendingSel = cleanText(currentSelection()); });

  btnSel.addEventListener('click', () => {
    const cleaned = pendingSel || cleanText(currentSelection());
    pendingSel = '';
    if (!cleaned) {
      status.textContent = 'Seleziona del testo nel corpo dell’articolo prima di tradurre.';
      return;
    }
    const payload = cleaned.slice(0, LIMIT);
    status.textContent = (cleaned.length > LIMIT)
      ? 'Selezione troncata a ' + LIMIT + ' caratteri (≈ ' + LIMIT_WORDS +
        ' parole): la parte in rosso non e’ stata inviata.'
      : '';
    translateText(payload);
  });

  btnStop.addEventListener('click', () => {
    if (currentController) currentController.abort();
  });

  refresh();
})();
</script>
