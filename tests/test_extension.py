"""Tests for the Chrome extension's files and harbor accepting exactly its origin."""

import base64
import hashlib
import http.client
import json
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from harbor.app import build, serve_harbor
from harbor.config import ConfigError, Settings, load_settings
from harbor.extension import EXTENSION_ID, EXTENSION_ORIGIN

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"


class ExtensionFilesTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((EXT / "manifest.json").read_text())

    def test_id_matches_manifest_key(self):
        # Chrome's extension ID: sha256 of the public key, first 32 hex digits mapped 0-f -> a-p.
        digest = hashlib.sha256(base64.b64decode(self.manifest["key"])).hexdigest()[:32]
        self.assertEqual("".join(chr(ord("a") + int(c, 16)) for c in digest), EXTENSION_ID)

    def test_manifest(self):
        m = self.manifest
        self.assertEqual(m["manifest_version"], 3)
        self.assertEqual(sorted(m["host_permissions"]), ["http://127.0.0.1/*", "http://localhost/*"])
        for f in [m["background"]["service_worker"], m["action"]["default_popup"], "popup.js", "harbor.js"]:
            self.assertTrue((EXT / f).is_file(), f)

    def test_icons(self):
        for size, path in self.manifest["icons"].items():
            data = (EXT / path).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), (int(size), int(size)))

    def test_no_private_key_in_repo(self):
        for p in ROOT.rglob("*"):
            if (
                p.is_file()
                and ".git" not in p.parts
                and ".deps" not in p.parts
                and p.suffix in ("", ".pem", ".key")
            ):
                self.assertNotIn(b"PRIVATE KEY", p.read_bytes(), p)


class OriginTests(unittest.TestCase):
    def serve(self, settings):
        cq, dq = build(jobs=1, image_threads=1, downloads=1)
        server = serve_harbor(0, cq, dq, settings)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(cq.shutdown)
        self.addCleanup(dq.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def post(self, port, origin):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/download/pause",
            body=b'{"paused": true}',
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        status = conn.getresponse().status
        conn.close()
        return status

    def test_default_allows_only_harbors_extension(self):
        port = self.serve(Settings())
        self.assertEqual(self.post(port, EXTENSION_ORIGIN), 200)
        self.assertEqual(self.post(port, "chrome-extension://" + "a" * 32), 403)
        self.assertEqual(self.post(port, "https://evil.example"), 403)

    def test_off(self):
        port = self.serve(Settings(extension_origins="off"))
        self.assertEqual(self.post(port, EXTENSION_ORIGIN), 403)

    def test_custom_list(self):
        other = "chrome-extension://" + "b" * 32
        port = self.serve(Settings(extension_origins=f"{other}, {EXTENSION_ORIGIN}"))
        self.assertEqual(self.post(port, other), 200)
        self.assertEqual(self.post(port, EXTENSION_ORIGIN), 200)

    def test_setting_validation(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "x.env"
            for bad in ("chrome-extension://*", "*", "chrome-extension://abc"):
                env.write_text(f"HARBOR_EXTENSION_ORIGINS={bad}\n")
                with self.subTest(bad=bad), self.assertRaises(ConfigError):
                    load_settings(env_file=str(env), environ={})
            env.write_text("HARBOR_EXTENSION_ORIGINS=OFF\n")
            self.assertEqual(load_settings(env_file=str(env), environ={}).allowed_origins(), set())


if __name__ == "__main__":
    unittest.main()
