#!/usr/bin/env python3
"""
Client minimo del Chrome DevTools Protocol, solo stdlib.

Perche' non una libreria: serve un sottoinsieme piccolissimo del protocollo
WebSocket (localhost, niente TLS, niente compressione, niente estensioni) e
aggiungere una dipendenza al venv di produzione per ~80 righe non vale.
Perche' non Playwright: scaricherebbe un secondo Chromium da centinaia di MB
quando quello di sistema c'e' gia'.

Perche' serve il CDP e non basta `chromium --screenshot`: quel flag cattura solo
il viewport. Allargare la finestra non aiuta (riempie di bianco fino all'altezza
richiesta) e ritagliare il bianco a valle e' un'euristica che su pagine reali
fallisce. `Page.captureScreenshot` con captureBeyondViewport=True e' l'unico
modo corretto di ottenere la pagina intera.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


class WSError(RuntimeError):
    pass


class WebSocket:
    """Client WebSocket per il solo caso d'uso CDP su loopback."""

    def __init__(self, url: str, timeout: float = 60.0):
        if not url.startswith("ws://"):
            raise WSError(f"schema non supportato: {url}")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        # Consuma gli header della risposta fino alla riga vuota
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("connessione chiusa durante l'handshake")
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise WSError(f"handshake rifiutato: {head.splitlines()[0]!r}")
        self._buf = leftover

    # --- I/O di basso livello ---

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connessione chiusa")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])           # FIN + opcode testo
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        """Legge un messaggio completo, riassemblando i frammenti."""
        chunks = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            # il server non maschera mai (RFC 6455)
            data = self._recv_exact(length) if length else b""
            if opcode == 0x8:
                raise WSError("il server ha chiuso la connessione")
            if opcode == 0x9:               # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:               # pong
                continue
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class Chromium:
    """Avvia Chromium headless e ne guida una scheda via CDP."""

    def __init__(self, binary: str = "/usr/bin/chromium", user_agent: str = "",
                 width: int = 1280, height: int = 1696, timeout: float = 60.0):
        self.timeout = timeout
        self.port = self._free_port()
        self.profile = tempfile.mkdtemp(prefix="rssintel-cap-")
        args = [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--mute-audio",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            # niente servizi Google: senza, il log si riempie di errori GCM
            "--disable-features=OptimizationHints,MediaRouter,Translate",
            f"--user-data-dir={self.profile}",
            f"--window-size={width},{height}",
            f"--remote-debugging-port={self.port}",
        ]
        if user_agent:
            args.append(f"--user-agent={user_agent}")
        args.append("about:blank")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.ws = WebSocket(self._debugger_url(), timeout=timeout)
        self._id = 0

    @staticmethod
    def _free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def _debugger_url(self) -> str:
        """Attende che Chromium apra la porta e restituisce il ws:// della scheda."""
        deadline = time.time() + 20
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise WSError(f"chromium terminato subito (codice {self.proc.returncode})")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
                    tabs = json.load(r)
                for t in tabs:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception as ex:      # porta non ancora pronta
                last = ex
            time.sleep(0.25)
        raise WSError(f"CDP non raggiungibile sulla porta {self.port} ({last})")

    def call(self, method: str, **params) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise WSError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            # gli eventi non richiesti si ignorano
        raise WSError(f"{method}: nessuna risposta entro {self.timeout}s")

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            import shutil
            shutil.rmtree(self.profile, ignore_errors=True)
