"""Finto server della Bot API di Telegram, per provare il bot senza token vero.

POST /bot<token>/<metodo>  come Telegram (risposte minime ma valide)
POST /_push                 accoda un update (JSON)
GET  /_calls                chiamate ricevute [(metodo, parametri), ...]
Il token BAD_TOKEN riceve 401, come un token revocato.
"""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# costruito a pezzi: nel sorgente non deve comparire nulla con la forma di un token
BAD_TOKEN = "999999:" + "BADbad" * 6
BOT_USER = {"id": 424242, "is_bot": True, "first_name": "RSSIntel test", "username": "rssintel_test_bot"}


class State:
    def __init__(self):
        self.lock = threading.Condition()
        self.updates = []
        self.next_id = 1
        self.calls = []
        self.msg_id = 1000


def make_handler(st: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _params(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ct = self.headers.get("Content-Type", "")
            if "json" in ct:
                return json.loads(raw or b"{}")
            out = {}
            for k, v in urllib.parse.parse_qsl(raw.decode()):
                try:
                    out[k] = json.loads(v)
                except ValueError:
                    out[k] = v
            return out

        def do_GET(self):
            if self.path == "/_calls":
                with st.lock:
                    return self._send(200, st.calls)
            self._send(404, {})

        def do_POST(self):
            if self.path == "/_revoke":          # revoca il token a bot avviato
                st.revoked = True
                return self._send(200, {"ok": True})
            if self.path == "/_push":
                upd = self._params()
                with st.lock:
                    upd["update_id"] = st.next_id
                    st.next_id += 1
                    st.updates.append(upd)
                    st.lock.notify_all()
                return self._send(200, {"ok": True})
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or not parts[0].startswith("bot"):
                return self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
            token, method = parts[0][3:], parts[1]
            p = self._params()
            if token == BAD_TOKEN or getattr(st, "revoked", False):
                return self._send(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            if method != "getUpdates":
                with st.lock:
                    st.calls.append([method, p])
            now = int(time.time())
            if method == "getMe":
                return self._send(200, {"ok": True, "result": BOT_USER})
            if method == "getUpdates":
                offset = int(p.get("offset") or 0)
                deadline = time.time() + min(float(p.get("timeout") or 0), 1.0)
                with st.lock:
                    while True:
                        res = [u for u in st.updates if u["update_id"] >= offset]
                        if res or time.time() >= deadline:
                            break
                        st.lock.wait(0.1)
                return self._send(200, {"ok": True, "result": res})
            if method in ("sendMessage", "editMessageText"):
                with st.lock:
                    st.msg_id += 1
                    mid = p.get("message_id") or st.msg_id
                return self._send(200, {"ok": True, "result": {
                    "message_id": mid, "date": now, "text": p.get("text", ""),
                    "chat": {"id": int(p.get("chat_id", 0)), "type": "private"}}})
            # deleteWebhook, setMyCommands, deleteMyCommands, answerCallbackQuery, leaveChat, ...
            return self._send(200, {"ok": True, "result": True})
    return H


def start(port=0):
    st = State()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st
