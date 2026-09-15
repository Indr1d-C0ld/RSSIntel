<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

/** Risposta JSON + uscita. */
function tr_fail(int $code, string $msg): never {
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('X-Content-Type-Options: nosniff');
    echo json_encode(['error' => $msg], JSON_UNESCAPED_UNICODE);
    exit;
}

// Solo richieste POST con JSON
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    tr_fail(405, 'Method not allowed');
}

// Gate di autenticazione: senza questo l'endpoint e' un proxy aperto verso il
// servizio di traduzione su loopback, e ogni richiesta anonima tiene occupato
// un worker Apache fino al timeout (DoS banale con MaxRequestWorkers 150).
if (auth_user() === null) {
    tr_fail(401, 'Non autenticato');
}

// CSRF: il client manda JSON, quindi il token non e' in $_POST e csrf_check()
// non si applica. item.php lo passa nell'header X-CSRF-Token.
$sent_csrf = (string)($_SERVER['HTTP_X_CSRF_TOKEN'] ?? '');
if ($sent_csrf === '' || !hash_equals(csrf_token(), $sent_csrf)) {
    tr_fail(403, 'CSRF non valido');
}

$raw = file_get_contents('php://input');
$data = json_decode($raw, true);

if (!$data || !isset($data['q'], $data['source'], $data['target'])) {
    tr_fail(400, 'Missing parameters');
}

$text   = (string)$data['q'];
$source = (string)$data['source'];
$target = (string)$data['target'];

// Codici lingua: solo ISO 639-1/639-3 (niente stringhe arbitrarie al motore).
if (!preg_match('~^[a-z]{2,3}(_[A-Za-z]{4})?$~', $source)
 || !preg_match('~^[a-z]{2,3}(_[A-Za-z]{4})?$~', $target)) {
    tr_fail(400, 'Codice lingua non valido');
}

$maxChars = (int)(cfg()['translate_max_chars'] ?? 50000);
if ($maxChars > 0 && strlen($text) > $maxChars) {
    tr_fail(413, 'Text too long (max ' . $maxChars . ' bytes)');
}

// Limite pratico del motore (token): tronca comunque alla soglia "soft" cosi'
// non riceve mai piu' testo di quanto sappia tradurre in una volta. Il client
// (item.php) tronca gia' e lo segnala; questa e' solo una rete di sicurezza.
$softLimit = (int)(cfg()['translate_soft_limit'] ?? 2000);
if ($softLimit > 0 && mb_strlen($text, 'UTF-8') > $softLimit) {
    $text = mb_substr($text, 0, $softLimit, 'UTF-8');
}

// Chiamata al servizio di traduzione (vedi 'translate_url' in config.php)
$ch = curl_init((string)(cfg()['translate_url'] ?? 'http://127.0.0.1:5000/translate'));
curl_setopt_array($ch, [
    CURLOPT_POST           => true,
    CURLOPT_POSTFIELDS     => json_encode([
        'q'      => $text,
        'source' => $source,
        'target' => $target
    ]),
    CURLOPT_HTTPHEADER     => ['Content-Type: application/json'],
    CURLOPT_RETURNTRANSFER => true,
    // 30s (era 120): ogni secondo qui e' un worker Apache bloccato. Il limite
    // soft di ~2000 caratteri e' ampiamente traducibile entro questa soglia.
    CURLOPT_TIMEOUT        => 30,
    CURLOPT_CONNECTTIMEOUT => 5,
]);

$response = curl_exec($ch);
$httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
$error    = curl_error($ch);
curl_close($ch);

if ($error || $response === false) {
    tr_fail(502, 'Translation service unreachable: ' . $error);
}

http_response_code($httpCode > 0 ? $httpCode : 502);
header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');
echo $response;
