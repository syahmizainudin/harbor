"""Tests for reaching harbor from other devices: the startup rules, HTTPS, a trusted proxy, and
the client address in the audit log. Everything listens on 127.0.0.1 with temp files."""

import contextlib
import http.client
import io
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from harbor import accounts as A
from harbor import cli, maint
from harbor.app import build, serve_harbor
from harbor.config import ConfigError, Settings, check_reach, load_settings

PW = "correct horse"


def make_cert(folder: Path) -> tuple[Path, Path]:
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/CN=harbor.test",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return cert, key


class ReachRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cert, self.key = self.dir / "c.pem", self.dir / "k.pem"
        self.cert.write_text("x")
        self.key.write_text("x")
        self.key.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_local_and_fine(self):
        self.assertEqual(check_reach(Settings()), [])

    def test_lan_needs_https_cookie_accounts_and_names(self):
        text = " | ".join(check_reach(Settings(host="0.0.0.0", accounts=False)))
        for words in (
            "HTTPS is required",
            "HARBOR_ACCOUNTS must be true",
            "HARBOR_ALLOWED_HOSTS",
            "HARBOR_SECURE_COOKIE",
        ):
            self.assertIn(words, text)
        ok = Settings(
            host="0.0.0.0",
            tls_cert=str(self.cert),
            tls_key=str(self.key),
            secure_cookie=True,
            allowed_hosts="harbor.lan",
        )
        self.assertEqual(check_reach(ok), [])

    def test_proxy_rules(self):
        self.assertIn(
            "HARBOR_HOST=127.0.0.1", " ".join(check_reach(Settings(host="0.0.0.0", trust_proxy=True)))
        )
        text = " ".join(check_reach(Settings(trust_proxy=True)))
        self.assertIn("HARBOR_ALLOWED_HOSTS", text)
        self.assertIn("HARBOR_SECURE_COOKIE", text)
        self.assertEqual(
            check_reach(Settings(trust_proxy=True, allowed_hosts="books.example", secure_cookie=True)), []
        )

    def test_local_port_rules(self):
        self.assertIn("only for when harbor serves HTTPS", " ".join(check_reach(Settings(local_port=9000))))
        same = Settings(tls_cert=str(self.cert), tls_key=str(self.key), secure_cookie=True, local_port=8765)
        self.assertIn("must differ", " ".join(check_reach(same)))
        ok = Settings(tls_cert=str(self.cert), tls_key=str(self.key), secure_cookie=True, local_port=8766)
        self.assertEqual(check_reach(ok), [])
        warned = [
            m
            for lvl, m in maint.doctor(
                Settings(tls_cert=str(self.cert), tls_key=str(self.key), secure_cookie=True, port=1)
            )
            if "Chrome extension" in m
        ]
        self.assertEqual(len(warned), 1)

    def test_certificate_files(self):
        self.assertIn("both", " ".join(check_reach(Settings(tls_cert=str(self.cert), secure_cookie=True))))
        missing = Settings(tls_cert=str(self.dir / "no.pem"), tls_key=str(self.key), secure_cookie=True)
        self.assertIn("no such file", " ".join(check_reach(missing)))
        self.key.chmod(0o644)
        loose = Settings(tls_cert=str(self.cert), tls_key=str(self.key), secure_cookie=True)
        self.assertIn("chmod 600", " ".join(check_reach(loose)))
        self.key.chmod(0o600)
        self.assertEqual(check_reach(loose), [])
        self.assertIn(
            "HARBOR_SECURE_COOKIE",
            " ".join(check_reach(Settings(tls_cert=str(self.cert), tls_key=str(self.key)))),
        )

    def test_setting_values_are_checked(self):
        for var, value in (
            ("HARBOR_HOST", "http://0.0.0.0"),
            ("HARBOR_HOST", "my laptop"),
            ("HARBOR_ALLOWED_HOSTS", "*"),
            ("HARBOR_ALLOWED_HOSTS", "harbor.lan:8765"),
            ("HARBOR_ALLOWED_HOSTS", "https://harbor.lan"),
            ("HARBOR_TRUST_PROXY", "maybe"),
        ):
            with self.subTest(var=var, value=value), self.assertRaises(ConfigError):
                load_settings({}, None, {var: value, "HARBOR_ENV_FILE": ""})
        s = load_settings(
            {}, None, {"HARBOR_HOST": "0.0.0.0", "HARBOR_ALLOWED_HOSTS": "harbor.lan, 192.168.1.20"}
        )
        self.assertEqual((s.host, s.allowed_hosts), ("0.0.0.0", "harbor.lan, 192.168.1.20"))

    def test_cli_refuses_to_start_and_doctor_says_why(self):
        env = self.dir / "x.env"
        env.write_text(
            "HARBOR_HOST=0.0.0.0\nHARBOR_PORT=1\n"
            f"HARBOR_LIBRARY_DB={self.dir / 'lib.db'}\nHARBOR_ACCOUNTS_DB={self.dir / 'h.db'}\n"
            f"HARBOR_HISTORY_DB={self.dir / 'hi.db'}\nHARBOR_STATE_FILE={self.dir / 'q.json'}\n"
        )
        env.chmod(0o600)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["ui", "--env-file", str(env)])
        self.assertEqual(code, 2)
        self.assertIn("HTTPS is required", err.getvalue())
        self.assertFalse((self.dir / "lib.db").exists())  # refused before opening anything
        out = maint.doctor(load_settings({}, str(env)))
        self.assertIn(
            (
                maint.FAIL,
                "set HARBOR_ALLOWED_HOSTS to the name(s) or address(es) devices use to reach harbor",
            ),
            out,
        )


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate")
class HttpsHarborTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cert, self.key = make_cert(self.dir)
        self.acc = A.Accounts(self.dir / "h.db", secure_cookie=True)
        self.acc.add("Ann", PW, admin=True)
        self.cq, self.dq = build(jobs=1, image_threads=1, downloads=1, timeout=5, retries=0)
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        self.acc.close()
        self.tmp.cleanup()

    def start(self, **settings):
        s = Settings(allowed_hosts="harbor.test", secure_cookie=True, **settings)
        self.assertEqual(check_reach(s), [])
        server = serve_harbor(0, self.cq, self.dq, s, accounts=self.acc)
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if server.local_server is not None:
            self.servers.append(server.local_server)
            threading.Thread(target=server.local_server.serve_forever, daemon=True).start()
        return server.server_address[1]

    def req(self, port, method, path, body=None, headers=None, tls=True):
        if tls:
            ctx = ssl.create_default_context(cafile=str(self.cert))
            ctx.check_hostname = False
            conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=ctx)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": "harbor.test", **(headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        raw, rh = resp.read(), dict(resp.getheaders())
        conn.close()
        return resp.status, rh, raw

    def test_sign_in_over_https(self):
        port = self.start(tls_cert=str(self.cert), tls_key=str(self.key))
        login = {"name": "Ann", "password": PW}
        status, h, _ = self.req(port, "POST", "/api/account/login", login, {"Origin": "https://harbor.test"})
        self.assertEqual(status, 200)
        self.assertIn("Secure", h["Set-Cookie"])
        self.assertIn("HttpOnly", h["Set-Cookie"])
        # another site, or the page over plain http, can't post
        self.assertEqual(
            self.req(port, "POST", "/api/account/login", login, {"Origin": "http://harbor.test"})[0], 403
        )
        self.assertEqual(self.req(port, "GET", "/login", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.acc.audit_log()["items"][0]["client"], "127.0.0.1")
        # doctor recognises a harbor answering over HTTPS
        self.assertEqual(maint._port_check(port, https=True), (maint.OK, f"harbor is running on port {port}"))
        self.assertEqual(maint._port_check(port)[0], maint.WARN)  # asked in plain HTTP: can't tell

    def test_local_port_for_the_extension(self):
        from harbor.extension import EXTENSION_ORIGIN

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            local = sock.getsockname()[1]
        port = self.start(tls_cert=str(self.cert), tls_key=str(self.key), local_port=local)
        here = {"Host": f"127.0.0.1:{local}"}
        self.assertEqual(self.req(local, "GET", "/login", headers=here, tls=False)[0], 200)
        _, token = self.acc.create_token(1, "ext")
        ext = {**here, "Origin": EXTENSION_ORIGIN, A.TOKEN_HEADER: token}
        status, _, raw = self.req(local, "GET", "/api/state", headers=ext, tls=False)
        self.assertEqual(status, 200)
        self.assertIn("jobs", json.loads(raw))
        page = {**here, "Origin": f"http://127.0.0.1:{local}"}
        login = {"name": "Ann", "password": PW}
        self.assertEqual(self.req(local, "POST", "/api/account/login", login, page, tls=False)[0], 200)
        # only this computer's names on the local port; and HTTPS keeps working on the main one
        self.assertEqual(self.req(local, "GET", "/login", headers={"Host": "harbor.test"}, tls=False)[0], 403)
        self.assertEqual(self.req(port, "GET", "/login")[0], 200)
        self.assertEqual(self.servers[-1].server_address[0], "127.0.0.1")

    def test_behind_a_proxy(self):
        port = self.start(trust_proxy=True)
        fwd = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "harbor.test",
            "X-Forwarded-For": "192.168.1.44",
            "Origin": "https://harbor.test",
        }
        status, h, _ = self.req(
            port, "POST", "/api/account/login", {"name": "Ann", "password": PW}, fwd, tls=False
        )
        self.assertEqual(status, 200)
        self.assertIn("Secure", h["Set-Cookie"])
        self.assertEqual(self.acc.audit_log()["items"][0]["client"], "192.168.1.44")


if __name__ == "__main__":
    unittest.main()
