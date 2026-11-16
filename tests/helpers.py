"""Test fixtures: archive builders (from cbz-webp) and ``FileServer`` (from dl-queue)."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image


def img_bytes(fmt, mode="RGB", size=(300, 400), **kw):
    im = (
        Image.effect_noise(size, 40).convert(mode)
        if mode != "P"
        else Image.effect_noise(size, 40).convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE)
    )
    b = io.BytesIO()
    im.save(b, fmt, **kw)
    return b.getvalue()


def png_transparent():
    im = Image.effect_noise((300, 400), 40).convert("RGBA")
    im.putalpha(128)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def gif_animated():
    frames = [Image.new("RGB", (50, 50), c) for c in ("red", "green", "blue")]
    b = io.BytesIO()
    frames[0].save(b, "GIF", save_all=True, append_images=frames[1:])
    return b.getvalue()


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def comic_entries():
    """Every case the converter must handle, in one archive."""
    return {
        "001.jpg": img_bytes("JPEG", quality=95),
        "002.png": png_transparent(),
        "003.png": img_bytes("PNG", "P"),
        "001.png": img_bytes("PNG", "L"),  # collides with 001.jpg -> 001.webp
        "004.webp": img_bytes("WEBP"),
        "005.gif": gif_animated(),
        "ComicInfo.xml": b"<ComicInfo><Title>x</Title></ComicInfo>",
        "__MACOSX/._001.jpg": b"junk",
        ".DS_Store": b"junk",
    }


class FileServer:
    """Local HTTP server for download tests. Records every request's path and headers.

    Routes:
      /files/<any>/<name>    200, body = ``files[name]`` (or b"data:<name>")
      /secure/<name>         like /files, but 401 unless the key arrives as Bearer, X-API-Key or ?api_key
      /cd?name=<raw>         200 with Content-Disposition filename="<raw>"
      /cdstar                200 with an RFC 5987 filename* (UTF-8)
      /redirect              302 to http://localhost:<port>/landing/file.bin (a different host name)
      /nolength              200 without Content-Length
      /short                 Content-Length 1000, sends 10 bytes, closes
      /slow                  sends 64 KB every 50 ms for ~30 s (for cancel tests)
      /flaky/<n>/<name>      503 for the first <n> requests to that path, then 200
      /busy                  429 with Retry-After: 1 the first time, then 200
      /always500             500 every time
      /resume/<name>         RESUME_BODY with ETag "v1". First request: sends half, then closes.
                             Range + If-Range "v1" requests get a 206 with the rest
      /noresume/<name>       like /resume, but ignores Range and always sends the whole body (200)
      anything else          404
    """

    RESUME_BODY = bytes(range(256)) * 1200  # 300 KB, easy to check byte for byte

    KEY = "s3cret-key"

    def __init__(self, files: dict[str, bytes] | None = None):
        import re
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, unquote, urlsplit

        self.files = files or {}
        self.requests: list[dict] = []
        self.counts: dict[str, int] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self, data: bytes, headers=None, length=True):
                self.send_response(200)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if length:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlsplit(self.path)
                q = parse_qs(url.query)
                outer.requests.append({"path": url.path, "query": q, "headers": dict(self.headers)})
                parts = unquote(url.path).strip("/").split("/")
                name = parts[-1]
                n = outer.counts[url.path] = outer.counts.get(url.path, 0) + 1
                try:
                    if parts[0] == "flaky":
                        if n <= int(parts[1]):
                            self.send_error(503, "Busy")
                            return None
                        return self._body(f"data:{name}".encode())
                    if parts[0] == "busy":
                        if n == 1:
                            self.send_response(429)
                            self.send_header("Retry-After", "1")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return None
                        return self._body(b"not busy now")
                    if parts[0] == "always500":
                        self.send_error(500, "Broken")
                        return None
                    if parts[0] in ("resume", "noresume"):
                        body = outer.RESUME_BODY
                        rng = re.match(r"bytes=(\d+)-$", self.headers.get("Range", ""))
                        if parts[0] == "resume" and rng and self.headers.get("If-Range") == '"v1"':
                            start = int(rng.group(1))
                            self.send_response(206)
                            self.send_header("ETag", '"v1"')
                            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                            self.send_header("Content-Length", str(len(body) - start))
                            self.end_headers()
                            self.wfile.write(body[start:])
                            return None
                        self.send_response(200)
                        self.send_header("ETag", '"v1"')
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        if n == 1:  # first request: cut off half way
                            self.wfile.write(body[: len(body) // 2])
                            self.close_connection = True
                            return None
                        self.wfile.write(body)
                        return None
                    if parts[0] == "files":
                        return self._body(outer.files.get(name, f"data:{name}".encode()))
                    if parts[0] == "secure":
                        ok = (
                            self.headers.get("Authorization") == f"Bearer {outer.KEY}"
                            or self.headers.get("X-API-Key") == outer.KEY
                            or q.get("api_key") == [outer.KEY]
                        )
                        if not ok:
                            self.send_error(401, "Unauthorized")
                            return None
                        return self._body(outer.files.get(name, f"data:{name}".encode()))
                    if parts[0] == "cd":
                        raw = q.get("name", ["x.bin"])[0]
                        return self._body(b"cd", {"Content-Disposition": f'attachment; filename="{raw}"'})
                    if parts[0] == "cdstar":
                        cd = "attachment; filename=\"fallback.zip\"; filename*=UTF-8''Vol%C3%A9%2001.zip"
                        return self._body(b"cdstar", {"Content-Disposition": cd})
                    if parts[0] == "redirect":
                        self.send_response(302)
                        self.send_header("Location", f"http://localhost:{outer.port}/landing/file.bin")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    if parts[0] == "landing":
                        return self._body(b"landed")
                    if parts[0] == "nolength":
                        self.close_connection = True
                        return self._body(b"no length here", length=False)
                    if parts[0] == "short":
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b"0123456789")
                        self.close_connection = True
                        return None
                    if parts[0] == "slow":
                        import time

                        self.send_response(200)
                        self.send_header("Content-Length", str(64 * 1024 * 600))
                        self.end_headers()
                        for _ in range(600):
                            self.wfile.write(b"x" * 64 * 1024)
                            self.wfile.flush()
                            time.sleep(0.05)
                        return None
                    self.send_error(404)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
