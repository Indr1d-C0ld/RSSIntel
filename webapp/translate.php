<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Solo richieste POST con JSON
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Method not allowed']);
    exit;
}

$raw = file_get_contents('php://input');
$data = json_decode($raw, true);

if (!$data || !isset($data['q'], $data['source'], $data['target'])) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Missing parameters']);
    exit;
}

$text   = (string)$data['q'];
$source = (string)$data['source'];
$target = (string)$data['target'];

$maxChars = (int)(cfg()['translate_max_chars'] ?? 50000);
if ($maxChars > 0 && strlen($text) > $maxChars) {
    http_response_code(413);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Text too long (max ' . $maxChars . ' bytes)']);
    exit;
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
    CURLOPT_TIMEOUT        => 120,       // testi lunghi possono richiedere tempo
    CURLOPT_CONNECTTIMEOUT => 5,
]);

$response = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$error    = curl_error($ch);
curl_close($ch);

if ($error) {
    http_response_code(502);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'Translation service unreachable: ' . $error]);
    exit;
}

http_response_code($httpCode);
header('Content-Type: application/json');
echo $response;
