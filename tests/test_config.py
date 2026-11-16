"""Tests for harbor's settings (.env file, environment, flags), the .env API key, and the logo."""

import contextlib
import http.client
import io
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

from helpers import FileServer

from harbor import cli
from harbor.app import build, serve_harbor
from harbor.config import ConfigError, Settings, find_env_file, load_settings, parse_env

ROOT = Path(__file__).resolve().parents[1]


class ParseEnvTests(unittest.TestCase):
    def test_formats(self):
        text = """
# comment
HARBOR_PORT=9000
export HARBOR_QUALITY = 70
HARBOR_AUTH_NAME="X-API-Key"
HARBOR_DOWNLOAD_DIR='/mnt/d/My Comics'
HARBOR_API_KEY=abc#def   # inline comment
HARBOR_JOBS=
"""
        self.assertEqual(
            parse_env(text),
            {
                "HARBOR_PORT": "9000",
                "HARBOR_QUALITY": "70",
                "HARBOR_AUTH_NAME": "X-API-Key",
                "HARBOR_DOWNLOAD_DIR": "/mnt/d/My Comics",
                "HARBOR_API_KEY": "abc#def",
                "HARBOR_JOBS": "",
            },
        )

    def test_quoted_value_keeps_hash(self):
        self.assertEqual(parse_env('HARBOR_API_KEY="a #b"'), {"HARBOR_API_KEY": "a #b"})

    def test_errors_name_the_line(self):
        with self.assertRaisesRegex(ConfigError, r"x\.env:2: expected KEY=value"):
            parse_env("A=1\nnot a setting\n", "x.env")
        with self.assertRaisesRegex(ConfigError, "unclosed quote"):
            parse_env('A="open')


class LoadSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.home = mock.patch.object(Path, "home", return_value=self.dir / "home")
        self.home.start()

    def tearDown(self):
        self.home.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def write(self, text, name=".env"):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_defaults_without_file(self):
        s = load_settings(environ={})
        self.assertIsNone(s.source)
        self.assertEqual((s.port, s.quality, s.jobs, s.downloads, s.api_key), (8765, 80, 1, 2, ""))

    def test_reads_cwd_env(self):
        self.write("HARBOR_QUALITY=65\nHARBOR_DELETE_ORIGINAL=no\nHARBOR_AUTH_MODE=header\n")
        s = load_settings(environ={})
        self.assertEqual((s.quality, s.delete_original, s.auth_mode), (65, False, "header"))
        self.assertEqual(s.source, str(self.dir / ".env"))

    def test_user_config_file(self):
        self.write("HARBOR_PORT=9100\n", "home/.config/harbor/.env")
        self.assertEqual(load_settings(environ={}).port, 9100)

    def test_precedence_flag_over_env_over_file(self):
        self.write("HARBOR_PORT=9001\nHARBOR_QUALITY=61\nHARBOR_JOBS=3\n")
        s = load_settings(
            {"port": 9003, "quality": None}, environ={"HARBOR_PORT": "9002", "HARBOR_QUALITY": "62"}
        )
        self.assertEqual((s.port, s.quality, s.jobs), (9003, 62, 3))

    def test_explicit_file_and_env_var(self):
        other = self.write("HARBOR_PORT=9200\n", "conf/harbor.env")
        self.write("HARBOR_PORT=9300\n")
        self.assertEqual(load_settings(env_file=str(other), environ={}).port, 9200)
        self.assertEqual(load_settings(environ={"HARBOR_ENV_FILE": str(other)}).port, 9200)
        with self.assertRaisesRegex(ConfigError, "env file not found"):
            load_settings(env_file=str(self.dir / "nope.env"), environ={})
        with self.assertRaisesRegex(ConfigError, "env file not found"):
            find_env_file(None, {"HARBOR_ENV_FILE": str(self.dir / "nope.env")})

    def test_bad_values(self):
        for text, msg in [
            ("HARBOR_QUALITY=0", "HARBOR_QUALITY .* must be 1-100"),
            ("HARBOR_PORT=http", "must be a whole number"),
            ("HARBOR_JOBS=0", "at least 1"),
            ("HARBOR_DELETE_ORIGINAL=maybe", "true or false"),
            ("HARBOR_AUTH_MODE=basic", "must be one of"),
            ("HARBOR_QUALTY=80", "unknown setting.*HARBOR_QUALTY"),
        ]:
            with self.subTest(text=text):
                self.write(text + "\n")
                with self.assertRaisesRegex(ConfigError, msg):
                    load_settings(environ={})

    def test_new_settings(self):
        self.write("HARBOR_METHOD=2\nHARBOR_RETRIES=0\nHARBOR_RESTORE=false\nHARBOR_STATE_FILE=/tmp/q.json\n")
        s = load_settings(environ={})
        self.assertEqual((s.method, s.retries, s.restore, s.state_file), (2, 0, False, "/tmp/q.json"))
        self.assertEqual(s.page_defaults()["method"], 2)
        self.write("HARBOR_METHOD=7\n")
        with self.assertRaisesRegex(ConfigError, "HARBOR_METHOD"):
            load_settings(environ={})

    def test_key_not_in_repr_or_page_defaults(self):
        self.write("HARBOR_API_KEY=super-secret\n")
        s = load_settings(environ={})
        self.assertEqual(s.api_key, "super-secret")
        self.assertNotIn("super-secret", repr(s))
        self.assertNotIn("super-secret", json.dumps(s.page_defaults()))
        self.assertTrue(s.page_defaults()["has_key"])

    def test_cli_reports_bad_file(self):
        self.write("HARBOR_QUALITY=500\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARBOR_ENV_FILE", None)
            self.assertEqual(cli.main(["ui"]), 2)
        self.assertIn("HARBOR_QUALITY", err.getvalue())


class EnvKeyConsoleTests(unittest.TestCase):
    KEY = FileServer.KEY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.files_server = FileServer()
        settings = Settings(
            api_key=self.KEY,
            auth_mode="header",
            auth_name="X-API-Key",
            download_dir=str(self.dir),
            quality=55,
        )
        self.cq, self.dq = build(jobs=1, image_threads=1, downloads=1, timeout=10)
        self.server = serve_harbor(0, self.cq, self.dq, settings)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        self.files_server.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(
            method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def add(self, auth):
        body = {"urls": [self.files_server.base + "/secure/a.bin"], "dest": str(self.dir), "auth": auth}
        return self.call("POST", "/api/download/add", body)

    def test_page_gets_defaults_but_not_the_key(self):
        _, st = self.call("GET", "/api/state")
        h = st["harbor"]
        self.assertEqual(
            (h["auth_mode"], h["auth_name"], h["download_dir"], h["has_key"]),
            ("header", "X-API-Key", str(self.dir), True),
        )
        self.assertEqual(st["defaults"]["quality"], 55)
        self.assertNotIn(self.KEY, json.dumps(st))

    def test_empty_key_uses_env_key(self):
        self.assertEqual(self.add({"mode": "header", "name": "X-API-Key", "key": ""})[0], 200)
        self.dq.wait_idle()
        _, st = self.call("GET", "/api/state")
        self.assertEqual(st["downloads"][0]["state"], "done", st["downloads"][0]["message"])
        self.assertNotIn(self.KEY, json.dumps(st))

    def test_typed_key_wins(self):
        self.add({"mode": "header", "name": "X-API-Key", "key": "wrong"})
        self.dq.wait_idle()
        _, st = self.call("GET", "/api/state")
        self.assertIn("HTTP 401", st["downloads"][0]["message"])

    def test_no_auth_sends_no_key(self):
        self.add({"mode": "none"})
        self.dq.wait_idle()
        req = [r for r in self.files_server.requests if r["path"].startswith("/secure")][-1]
        self.assertNotIn("X-API-Key", req["headers"])

    def test_notes_added_after_start_reach_the_page(self):
        notes = []
        cq, dq = build(jobs=1, image_threads=1, downloads=1)
        server = serve_harbor(0, cq, dq, Settings(), notes)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            notes[:] = ["restored 2 unfinished job(s) from the last run"]  # what `harbor ui` does
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/state")
            st = json.loads(conn.getresponse().read())
            conn.close()
            self.assertEqual(st["harbor"]["notes"], notes)
        finally:
            server.shutdown()
            server.server_close()
            dq.shutdown()
            cq.shutdown()

    def test_harbor_api_has_no_actions(self):
        self.assertEqual(self.call("POST", "/api/harbor/anything", {})[0], 404)


class LogoTests(unittest.TestCase):
    def test_page_uses_assets_logo(self):
        logo = (ROOT / "assets" / "logo.svg").read_text().strip()
        page = (ROOT / "harbor" / "ui.html").read_text()
        self.assertIn(logo, page, "inline header logo differs from assets/logo.svg")
        icon = re.search(r'<link rel="icon" href="data:image/svg\+xml,([^"]+)">', page)
        self.assertIsNotNone(icon)
        self.assertEqual(
            unquote(icon.group(1).replace("&quot;", '"')), logo, "favicon differs from assets/logo.svg"
        )
        login = (ROOT / "harbor" / "login.html").read_text()
        self.assertIn(logo, login, "sign-in page logo differs from assets/logo.svg")
        self.assertIn(icon.group(0), login, "sign-in page favicon differs from the console's")


if __name__ == "__main__":
    unittest.main()
