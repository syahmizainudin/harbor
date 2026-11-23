"""Phase 0 prototype: serve one MV game under option A (second port) or B (sandboxed iframe).

usage: python3 -I proto.py <game dir> <mode A|B> <page port> <game port>

THROWAWAY. Not a base for the real games server: no Host check, a fixed token, saves in memory
for anyone, CORS "*" everywhere, and it reads cookies (only to log that they arrive). Bind it to
127.0.0.1 only, as it does, and stop it when done. GET /__log shows the log and saved slot ids.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GAME = Path(sys.argv[1]).resolve()
MODE, P1, P2 = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
TOKEN = "tok123"
SAVES: dict[str, str] = {}
LOG: list[str] = []
TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".css": "text/css",
    ".ttf": "font/ttf",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}

SHIM = r"""
(function () {
  var base = location.pathname.replace(/[^/]*$/, "");
  var cache = {}, ready = false;
  fetch(base + "__saves").then(function (r) { return r.json(); }).then(function (d) {
    cache = d; ready = true; ConfigManager.load();
  });
  var isReady = Scene_Boot.prototype.isReady;
  Scene_Boot.prototype.isReady = function () { return ready && isReady.call(this); };
  function put(id, body) {
    body.id = String(id);
    fetch(base + "__save", { method: "POST", body: JSON.stringify(body), keepalive: true });
  }
  StorageManager.isLocalMode = function () { return false; };
  StorageManager.save = function (id, json) { cache[id] = json; put(id, { data: json }); };
  StorageManager.load = function (id) { return cache.hasOwnProperty(id) ? cache[id] : null; };
  StorageManager.exists = function (id) { return cache.hasOwnProperty(id); };
  StorageManager.remove = function (id) { delete cache[id]; put(id, { remove: true }); };
  StorageManager.backup = StorageManager.cleanBackup = StorageManager.restoreBackup = function () {};
})();
"""


def page():
    if MODE == "A":
        frame = f'<iframe id="g" src="http://127.0.0.1:{P2}/play/{TOKEN}/index.html"></iframe>'
    else:
        frame = f'<iframe id="g" sandbox="allow-scripts" src="/play/{TOKEN}/index.html"></iframe>'
    style = "<style>body{margin:0}iframe{width:816px;height:624px;border:0}</style>"
    return f"<!doctype html><title>harbor</title>{style}<body>{frame}"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get("Cookie"):
            LOG.append(f"cookie on :{port} {self.path} -> {self.headers['Cookie']}")
        if self.path == "/" and port == P1:
            return self.send(
                200,
                page().encode(),
                "text/html",
                {"Set-Cookie": "sid=SECRET; HttpOnly; SameSite=Strict; Path=/"},
            )
        if self.path == "/__log":
            return self.send(
                200, json.dumps({"log": LOG, "saves": sorted(SAVES)}).encode(), "application/json"
            )
        prefix = f"/play/{TOKEN}/"
        if not self.path.startswith(prefix) or (MODE == "A" and port != P2):
            return self.send(404, b"no")
        rel = self.path[len(prefix) :].split("?")[0]
        if rel == "__saves":
            return self.send(200, json.dumps(SAVES).encode(), "application/json")
        if rel == "__shim.js":
            return self.send(200, SHIM.encode(), "text/javascript")
        f = (GAME / rel).resolve()
        if GAME not in f.parents or not f.is_file():
            LOG.append("404 " + rel)
            return self.send(404, b"no")
        body = f.read_bytes()
        if rel == "index.html":
            body = body.replace(
                b'<script type="text/javascript" src="js/main.js">',
                b'<script src="__shim.js"></script><script type="text/javascript" src="js/main.js">',
            )
        self.send(200, body, TYPES.get(f.suffix, "application/octet-stream"))

    def do_POST(self):
        if self.path == f"/play/{TOKEN}/__save":
            d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if d.get("remove"):
                SAVES.pop(d["id"], None)
            else:
                SAVES[d["id"]] = d["data"]
            return self.send(200, b"{}", "application/json")
        self.send(404, b"no")


servers = [ThreadingHTTPServer(("127.0.0.1", P1), H)]
if MODE == "A":
    servers.append(ThreadingHTTPServer(("127.0.0.1", P2), H))
for s in servers[1:]:
    threading.Thread(target=s.serve_forever, daemon=True).start()
print("ready", flush=True)
servers[0].serve_forever()
