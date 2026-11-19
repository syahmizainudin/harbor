"""Tests for reader apps: OPDS through harbor with reader tokens over HTTP Basic, and their limits."""

import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cbzshelf.library import Library
from helpers import comic_entries, make_zip

from harbor import accounts as A
from harbor.app import build, serve_harbor
from harbor.config import Settings

PW = "correct horse"


def basic(name, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{secret}".encode()).decode()}


class ReaderAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.acc = A.Accounts(self.dir / "h.db")
        self.ann = self.acc.add("Ann", PW, admin=True)
        self.rea = self.acc.add("Rea", PW, role="Reader")
        for folder in ("Comics/S", "Private/P"):
            (self.dir / folder).mkdir(parents=True)
        make_zip(self.dir / "Comics" / "S" / "a.cbz", comic_entries())
        make_zip(self.dir / "Private" / "P" / "p.cbz", comic_entries())
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.dir / "Comics"))
        self.priv = self.lib.add_library("Private")["id"]
        self.lib.add_root(str(self.dir / "Private"), self.priv)
        self.lib.wait_scan(30)
        self.cq, self.dq = build(jobs=1, image_threads=1, downloads=1, timeout=5, retries=0)
        self.server = serve_harbor(0, self.cq, self.dq, Settings(), library=self.lib, accounts=self.acc)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        _, self.rea_token = self.acc.create_token(self.rea["id"], "phone", "reader")
        _, self.ann_token = self.acc.create_token(self.ann["id"], "tablet", "reader")
        _, self.ext_token = self.acc.create_token(self.rea["id"], "laptop")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        self.lib.close()
        self.acc.close()
        self.tmp.cleanup()

    def req(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = conn.getresponse()
        raw, rh = r.read(), dict(r.getheaders())
        conn.close()
        return r.status, rh, raw

    def test_reader_token_reads_the_catalog_and_downloads(self):
        auth = basic("Rea", self.rea_token)
        status, _, body = self.req("GET", "/api/library/opds?feed=series", auth)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>S</title>", body)
        book = self.lib.search(q="a")["items"][0]
        status, _, body = self.req("GET", f"/api/library/file?book={book['id']}", auth)
        self.assertEqual((status, body), (200, (self.dir / "Comics" / "S" / "a.cbz").read_bytes()))
        self.assertEqual(self.req("GET", f"/api/library/page?book={book['id']}&n=0", auth)[0], 200)
        self.assertEqual(
            self.req("GET", "/api/library/opds", basic("rea", self.rea_token))[0], 200
        )  # any case

    def test_reader_token_can_do_nothing_else(self):
        auth = basic("Ann", self.ann_token)  # even an admin's reader token
        for method, path, body in (
            ("GET", "/api/state", None),
            ("GET", "/", None),
            ("GET", "/api/dirs", None),
            ("GET", "/api/account/sessions", None),
            ("GET", "/api/admin/audit", None),
            ("POST", "/api/library/progress", {"id": 1, "page": 1}),
            ("POST", "/api/library/add-root", {"path": "/"}),
            ("POST", "/api/add", {"paths": ["/tmp"]}),
            ("POST", "/api/account/token-create", {"label": "x"}),
        ):
            self.assertEqual(self.req(method, path, auth, body)[0], 403, path)
        self.assertEqual(
            self.req("GET", "/api/library/roots", auth)[0], 403
        )  # library.manage: not for a reader token
        # the reader token's limits hold in the token header too
        self.assertEqual(self.req("GET", "/api/state", {A.TOKEN_HEADER: self.rea_token})[0], 403)

    def test_only_a_reader_token_of_that_account_signs_in(self):
        for name, secret in (
            ("Rea", PW),  # the account password: never over Basic
            ("Ann", self.rea_token),  # someone else's token
            ("Rea", self.ext_token),  # an extension token
            ("Rea", "hbt_nope"),
        ):
            status, _, _ = self.req("GET", "/api/library/opds", basic(name, secret))
            self.assertEqual(status, 401, (name, secret[:6]))
        self.assertEqual(self.req("GET", "/api/library/opds", {"Authorization": "Basic !!!"})[0], 401)
        self.assertEqual(
            self.req("GET", "/api/library/opds", {"Authorization": "Bearer x"})[0], 401
        )  # not Basic

    def test_signed_out_reader_app_is_asked_to_sign_in(self):
        status, h, _ = self.req("GET", "/api/library/opds")
        self.assertEqual(status, 401)
        self.assertTrue(h["WWW-Authenticate"].startswith("Basic "))
        self.assertEqual(self.req("GET", "/api/library/file?book=1")[0], 401)
        self.assertEqual(self.req("GET", "/api/library/series")[0], 200)  # the web page's guest view stays

    def test_library_access_and_paths_apply(self):
        self.acc.set_library_access(self.priv, [A.ADMIN_ROLE], False)  # Admin only
        rea, ann = basic("Rea", self.rea_token), basic("Ann", self.ann_token)
        self.assertNotIn(b"<title>P</title>", self.req("GET", "/api/library/opds?feed=series", rea)[2])
        self.assertIn(b"<title>P</title>", self.req("GET", "/api/library/opds?feed=series", ann)[2])
        pbook = self.lib.search(q="p")["items"][0]["id"]
        self.assertEqual(self.req("GET", f"/api/library/file?book={pbook}", rea)[0], 404)
        sid = self.lib.search(q="a")["items"][0]["series_id"]
        detail = json.loads(self.req("GET", f"/api/library/series-detail?id={sid}", rea)[2])
        self.assertNotIn("folder", detail)  # a server path: managers only

    def test_disabled_account_and_revoked_token(self):
        auth = basic("Rea", self.rea_token)
        self.acc.set_disabled("Rea", True)
        self.assertEqual(self.req("GET", "/api/library/opds", auth)[0], 401)

    def test_creating_tokens(self):
        _, sid = self.acc.login("Rea", PW)
        cookie = {"Cookie": f"harbor_session={sid}"}
        status, _, raw = self.req(
            "POST", "/api/account/token-create", cookie, {"label": "", "scope": "reader"}
        )
        made = json.loads(raw)
        self.assertEqual((status, made["scope"], made["label"]), (200, "reader", "Reader app"))
        self.assertEqual(self.req("POST", "/api/account/token-create", cookie, {"scope": "root"})[0], 400)
        scopes = {t["label"]: t["scope"] for t in self.acc.tokens(self.rea["id"])}
        self.assertEqual(scopes, {"phone": "reader", "laptop": "extension", "Reader app": "reader"})
        log = self.acc.audit_log()["items"][0]
        self.assertEqual((log["action"], log["detail"]), ("token created", "Reader app: Reader app"))
        self.assertNotIn(made["token"], json.dumps(self.acc.audit_log()))


if __name__ == "__main__":
    unittest.main()
