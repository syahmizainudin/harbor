"""Tests for accounts: passwords, sessions, tokens, signing in over HTTP, per-user library data, CLI."""

import contextlib
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cbzshelf.library import Library
from helpers import comic_entries, make_zip

from harbor import accounts as A
from harbor import cli
from harbor.app import build, serve_harbor
from harbor.config import Settings
from harbor.extension import EXTENSION_ORIGIN

PW = "correct horse"


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.acc = A.Accounts(Path(self.tmp.name) / "h.db")

    def tearDown(self):
        self.acc.close()
        self.tmp.cleanup()

    def test_passwords_are_hashed_and_checked(self):
        h = A.hash_password(PW)
        self.assertTrue(h.startswith("scrypt$") and PW not in h)
        self.assertNotEqual(h, A.hash_password(PW))  # salted
        self.assertTrue(A.verify_password(PW, h))
        self.assertFalse(A.verify_password("wrong", h))
        self.assertFalse(A.verify_password(PW, "garbage"))

    def test_add_and_rules(self):
        ann = self.acc.add("Ann", PW, admin=True)
        self.assertEqual((ann["name"], ann["admin"]), ("Ann", True))
        with self.assertRaises(ValueError):
            self.acc.add("ann", PW)  # names are unique ignoring case
        with self.assertRaises(ValueError):
            self.acc.add("Bo", "short")
        with self.assertRaises(ValueError):
            self.acc.add("bad\nname", PW)
        stored = sqlite3.connect(self.acc.db_path).execute("SELECT pass_hash FROM users").fetchone()[0]
        self.assertNotIn(PW, stored)

    def test_login_sessions_and_logout(self):
        self.acc.add("Ann", PW)
        _, sid = self.acc.login("ann", PW)
        self.assertEqual(self.acc.session_user(sid)["name"], "Ann")
        stored = [r[0] for r in sqlite3.connect(self.acc.db_path).execute("SELECT id_hash FROM sessions")]
        self.assertNotIn(sid, stored)  # only the hash is kept
        self.acc.logout(sid)
        self.assertIsNone(self.acc.session_user(sid))
        with self.assertRaises(ValueError):
            self.acc.login("Ann", "nope")
        with self.assertRaises(ValueError):
            self.acc.login("Nobody", PW)  # same answer as a wrong password

    def test_lockout_after_repeated_wrong_passwords(self):
        self.acc.add("Ann", PW)
        for _ in range(A.LOCK_AFTER):
            with self.assertRaises(ValueError):
                self.acc.login("Ann", "nope")
        with self.assertRaises(A.LockedOut):
            self.acc.login("Ann", PW)  # even the right password, until the window passes
        later = time.time() + A.LOCK_WINDOW + 1
        with mock.patch("harbor.accounts.time.time", return_value=later):
            self.assertEqual(self.acc.login("Ann", PW)[0]["name"], "Ann")

    def test_expired_and_disabled(self):
        self.acc.add("Ann", PW, admin=True)
        self.acc.add("Bo", PW)
        _, sid = self.acc.login("Bo", PW)
        with mock.patch("harbor.accounts.time.time", return_value=time.time() + 31 * 86400):
            self.assertIsNone(self.acc.session_user(sid))
        _, sid = self.acc.login("Bo", PW)
        self.acc.set_disabled("Bo", True)
        self.assertIsNone(self.acc.session_user(sid))
        with self.assertRaises(ValueError):
            self.acc.login("Bo", PW)
        with self.assertRaises(ValueError):
            self.acc.set_disabled("Ann", True)  # the only admin
        with self.assertRaises(ValueError):
            self.acc.remove("Ann")

    def test_password_change_ends_other_sessions(self):
        self.acc.add("Ann", PW)
        _, keep = self.acc.login("Ann", PW)
        _, other = self.acc.login("Ann", PW)
        self.acc.set_password("Ann", "new password!", keep_session=keep)
        self.assertIsNotNone(self.acc.session_user(keep))
        self.assertIsNone(self.acc.session_user(other))
        self.assertEqual(self.acc.login("Ann", "new password!")[0]["name"], "Ann")

    def test_tokens(self):
        ann = self.acc.add("Ann", PW)
        info, token = self.acc.create_token(ann["id"], "laptop")
        self.assertTrue(token.startswith("hbt_"))
        self.assertEqual(self.acc.token_user(token)["name"], "Ann")
        self.assertEqual([t["label"] for t in self.acc.tokens(ann["id"])], ["laptop"])
        self.assertIsNone(self.acc.token_user("hbt_wrong"))
        self.assertIsNone(self.acc.token_user(token[4:]))
        self.assertTrue(self.acc.revoke_token(ann["id"], info["id"]))
        self.assertIsNone(self.acc.token_user(token))

    def test_cookie(self):
        c = self.acc.cookie("abc")
        for part in ("harbor_session=abc", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, c)
        self.assertNotIn("Secure", c)
        self.assertIn("Max-Age=0", self.acc.cookie(None))
        self.assertEqual(A.session_id({"Cookie": "x=1; harbor_session=abc"}), "abc")
        self.assertEqual(A.session_id({"Cookie": "bad\x00cookie"}), "")


class SignInHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.acc = A.Accounts(self.dir / "h.db")
        self.ann = self.acc.add("Ann", PW, admin=True)
        self.bo = self.acc.add("Bo", PW)
        comics = self.dir / "Comics" / "S"
        comics.mkdir(parents=True)
        make_zip(comics / "a.cbz", comic_entries())
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.dir / "Comics"))
        self.lib.wait_scan(30)
        self.cq, self.dq = build(jobs=1, image_threads=1, downloads=1, timeout=5, retries=0)
        self.server = serve_harbor(0, self.cq, self.dq, Settings(), library=self.lib, accounts=self.acc)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        self.lib.close()
        self.acc.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = f"harbor_session={cookie}"
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        resp = conn.getresponse()
        raw, rh = resp.read(), dict(resp.getheaders())
        conn.close()
        ctype = rh.get("Content-Type", "")
        return resp.status, rh, json.loads(raw) if ctype.startswith("application/json") and raw else raw

    def login(self, name="Ann", password=PW):
        status, h, d = self.req("POST", "/api/account/login", {"name": name, "password": password})
        self.assertEqual(status, 200, d)
        return h["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_signed_out_can_read_the_library_and_nothing_else(self):
        for path in ("/", "/library", "/login"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        self.assertIn(b"Sign in", self.req("GET", "/login")[2])
        self.assertEqual(self.req("GET", "/api/account/status")[2], {"accounts": True, "signed_in": False})
        self.assertEqual(self.req("GET", "/api/library/series")[0], 200)
        self.assertEqual(self.req("GET", "/api/library/roots")[0], 403)  # library.manage
        self.assertEqual(self.req("GET", "/api/dirs")[0], 401)
        for path, body in (
            ("/api/add", {"paths": ["/tmp"]}),
            ("/api/download/add", {"urls": ["http://127.0.0.1:1/a.zip"], "dest_dir": "/tmp"}),
            ("/api/clear", {}),
            ("/api/library/favourite", {"kind": "book", "id": 1}),
            ("/api/library/edit-book", {"id": 1, "changes": {}}),
            ("/api/library/scan", {}),
            ("/api/account/prefs", {"changes": {}}),
            ("/api/account/password", {"current": PW, "new": "x" * 9}),
            ("/api/admin/user-add", {"name": "Eve", "password": PW}),
        ):
            self.assertEqual(self.req("POST", path, body)[0], 401, path)

    def test_signed_out_state_hides_jobs_and_settings(self):
        sid = self.login()
        src = self.dir / "in" / "x.zip"
        src.parent.mkdir()
        make_zip(src, comic_entries())
        self.cq.set_paused(True)
        self.assertEqual(self.req("POST", "/api/add", {"paths": [str(src)]}, cookie=sid)[0], 200)
        status, _, st = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual((st["account"], st["jobs"], st["downloads"]), (None, [], []))
        self.assertEqual(st["library_can"], {"edit": False, "manage": False})
        self.assertEqual(set(st["harbor"]), {"version"})
        self.assertNotIn("x.zip", json.dumps(st))
        self.assertNotIn(str(self.dir), json.dumps(st))  # no folder names at all
        self.assertNotIn("admin_users", st)
        # Signed in, the job is there.
        self.assertEqual(len(self.req("GET", "/api/state", cookie=sid)[2]["jobs"]), 1)

    def test_signed_out_reading_is_not_saved(self):
        bid = self.req("GET", "/api/library/search")[2]["items"][0]["id"]
        for action, body in (
            ("progress", {"id": bid, "page": 1}),
            ("prefs", {"changes": {"text_size": 120}}),
            ("hide-continue", {"id": bid}),
        ):
            status, _, answer = self.req("POST", f"/api/library/{action}", body)
            self.assertEqual((status, answer), (200, {"ok": True, "saved": False}), action)
        self.assertIsNone(self.req("GET", f"/api/library/book?id={bid}")[2]["progress"])
        self.assertEqual(self.req("GET", "/api/library/continue")[2], [])
        ann = self.login()
        self.assertEqual(self.req("GET", "/api/library/continue", cookie=ann)[2], [])

    def test_a_bad_token_is_refused_not_a_guest(self):
        # The extension learns it needs a token from a 401 on /api/state.
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Harbor-Token": "nope"})[0], 401)
        self.assertEqual(self.req("GET", "/api/state", headers={"Origin": EXTENSION_ORIGIN})[0], 401)

    def test_sign_in_cookie_and_state(self):
        status, h, d = self.req("POST", "/api/account/login", {"name": "ann", "password": PW})
        self.assertEqual((status, d), (200, {"name": "Ann"}))
        cookie = h["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        sid = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(self.req("GET", "/", cookie=sid)[0], 200)
        _, _, st = self.req("GET", "/api/state", cookie=sid)
        acct = st["account"]
        self.assertEqual(
            (acct["name"], acct["admin"], acct["role"], acct["browser"], acct["tokens"]),
            ("Ann", True, "Admin", True, []),
        )
        self.assertIn("users.manage", acct["perms"])
        self.assertNotIn(sid, json.dumps(st))
        self.assertNotIn("_sid", json.dumps(st))

    def test_wrong_password_and_lockout_over_http(self):
        for _ in range(A.LOCK_AFTER):
            self.assertEqual(self.req("POST", "/api/account/login", {"name": "Bo", "password": "x"})[0], 401)
        self.assertEqual(self.req("POST", "/api/account/login", {"name": "Bo", "password": PW})[0], 429)

    def test_login_from_another_site_is_refused(self):
        status, _, _ = self.req(
            "POST",
            "/api/account/login",
            {"name": "Ann", "password": PW},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_logout(self):
        sid = self.login()
        status, h, _ = self.req("POST", "/api/account/logout", {}, cookie=sid)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", h["Set-Cookie"])
        self.assertIsNone(self.req("GET", "/api/state", cookie=sid)[2]["account"])  # a guest now
        self.assertEqual(self.req("POST", "/api/clear", {}, cookie=sid)[0], 401)

    def test_library_data_is_per_user(self):
        ann, bo = self.login("Ann"), self.login("Bo")
        bid = self.req("GET", "/api/library/search", cookie=ann)[2]["items"][0]["id"]
        self.req("POST", "/api/library/progress", {"id": bid, "page": 1}, cookie=ann)
        self.req("POST", "/api/library/favourite", {"kind": "book", "id": bid}, cookie=ann)
        self.assertEqual([b["id"] for b in self.req("GET", "/api/library/continue", cookie=ann)[2]], [bid])
        self.assertEqual(self.req("GET", "/api/library/continue", cookie=bo)[2], [])
        self.assertFalse(self.req("GET", f"/api/library/book?id={bid}", cookie=bo)[2]["favourite"])
        self.assertTrue(self.req("GET", f"/api/library/book?id={bid}", cookie=ann)[2]["favourite"])

    def test_extension_token(self):
        sid = self.login()
        _, _, t = self.req("POST", "/api/account/token-create", {"label": "laptop"}, cookie=sid)
        token = t["token"]
        ext = {"X-Harbor-Token": token, "Origin": EXTENSION_ORIGIN}
        status, _, st = self.req("GET", "/api/state", headers=ext)
        self.assertEqual((status, st["account"]["browser"], st["account"]["tokens"]), (200, False, []))
        self.assertEqual(self.req("POST", "/api/clear", {}, headers=ext)[0], 200)
        # A token can't manage the account.
        self.assertEqual(
            self.req("POST", "/api/account/password", {"current": PW, "new": "x" * 9}, headers=ext)[0], 403
        )
        self.assertEqual(self.req("POST", "/api/account/token-create", {}, headers=ext)[0], 403)
        self.req("POST", "/api/account/token-revoke", {"id": t["id"]}, cookie=sid)
        self.assertEqual(self.req("GET", "/api/state", headers=ext)[0], 401)

    def test_change_password(self):
        sid = self.login("Bo")
        other = self.login("Bo")
        bad = self.req(
            "POST", "/api/account/password", {"current": "nope", "new": "new password"}, cookie=sid
        )
        self.assertEqual(bad[0], 400)
        ok = self.req("POST", "/api/account/password", {"current": PW, "new": "new password"}, cookie=sid)
        self.assertEqual(ok[0], 200)
        self.assertEqual(self.req("GET", "/api/state", cookie=sid)[2]["account"]["name"], "Bo")
        self.assertIsNone(self.req("GET", "/api/state", cookie=other)[2]["account"])

    def test_own_defaults(self):
        ann, bo = self.login("Ann"), self.login("Bo")
        prefs = {
            "quality": 55,
            "method": 6,
            "download_dir": "/mnt/d/In",
            "auth_mode": "none",
            "convert_downloads": False,
        }
        status, _, saved = self.req("POST", "/api/account/prefs", {"changes": prefs}, cookie=ann)
        self.assertEqual((status, saved), (200, prefs))
        st = self.req("GET", "/api/state", cookie=ann)[2]
        self.assertEqual((st["defaults"]["quality"], st["defaults"]["method"]), (55, 6))
        h = st["harbor"]
        self.assertEqual(
            (h["download_dir"], h["auth_mode"], h["convert_downloads"], h["method"]),
            ("/mnt/d/In", "none", False, 6),
        )
        self.assertEqual(st["account"]["prefs"], prefs)
        other = self.req("GET", "/api/state", cookie=bo)[2]
        self.assertEqual(
            (other["defaults"]["quality"], other["harbor"]["download_dir"]), (80, "")
        )  # .env defaults
        for bad in (
            {"quality": 0},
            {"quality": "80"},
            {"api_key": "x"},
            {"download_dir": "a\nb"},
            {"auth_mode": "magic"},
        ):
            self.assertEqual(
                self.req("POST", "/api/account/prefs", {"changes": bad}, cookie=ann)[0], 400, bad
            )
        self.req("POST", "/api/account/prefs", {"changes": {"quality": None}}, cookie=ann)
        self.assertEqual(self.req("GET", "/api/state", cookie=ann)[2]["defaults"]["quality"], 80)

    def test_no_accounts_yet(self):
        empty = A.Accounts(self.dir / "empty.db")
        self.addCleanup(empty.close)
        self.assertEqual(A.AccountAPI(empty).get("status", {})[2], b'{"accounts": false, "signed_in": false}')


class UserCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / "x.env"
        self.env.write_text(f"HARBOR_ACCOUNTS_DB={Path(self.tmp.name) / 'h.db'}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            code = cli.main(["user", "--env-file", str(self.env), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_user_commands(self):
        self.assertIn("No accounts yet", self.run_cli("list")[1])
        self.assertEqual(self.run_cli("add", "Ann", "--admin", "--password-stdin", stdin=PW + "\n")[0], 0)
        self.assertEqual(self.run_cli("add", "Bo", "--password-stdin", stdin=PW + "\n")[0], 0)
        code, _, err = self.run_cli("add", "Cy", "--password-stdin", stdin="short\n")
        self.assertEqual(code, 2)
        self.assertIn("at least", err)
        self.assertEqual(self.run_cli("list")[1], "Ann  (Admin)\nBo  (Member)\n")
        self.assertEqual(self.run_cli("disable", "Bo")[0], 0)
        self.assertIn("Bo  (Member, disabled)", self.run_cli("list")[1])
        self.assertEqual(self.run_cli("disable", "Ann")[0], 2)  # the only admin
        self.assertEqual(self.run_cli("passwd", "Bo", "--password-stdin", stdin="another one\n")[0], 0)
        self.assertEqual(self.run_cli("remove", "Bo")[0], 0)
        self.assertEqual(self.run_cli("remove", "Nobody")[0], 2)
        acc = A.Accounts(Path(self.tmp.name) / "h.db")
        self.addCleanup(acc.close)
        self.assertEqual(acc.login("Ann", PW)[0]["name"], "Ann")


if __name__ == "__main__":
    unittest.main()
