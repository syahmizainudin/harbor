"""Saved site keys: encryption at rest, matching, the Sites tab's API, and downloads that use them."""

import json
import stat
import tempfile
import time
import unittest
from pathlib import Path

from helpers import FileServer
from test_permissions import PW, HttpBase

from harbor import accounts as A
from harbor.sitekeys import SITE_KEYS_MAX, SiteKeys, Vault

SECRET = "tok-123-very-secret"


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_file_is_private_and_round_trips(self):
        v = Vault(self.dir / "sub" / "secret.key")
        self.assertIsNone(v.decrypt(b"anything"))  # no key file yet: nothing can be read
        blob = v.encrypt(SECRET)
        self.assertNotIn(SECRET.encode(), blob)
        self.assertEqual(stat.S_IMODE((self.dir / "sub" / "secret.key").stat().st_mode), 0o600)
        self.assertEqual(Vault(self.dir / "sub" / "secret.key").decrypt(blob), SECRET)

    def test_other_key_cant_read(self):
        blob = Vault(self.dir / "a.key").encrypt(SECRET)
        other = Vault(self.dir / "b.key")
        other.encrypt("x")
        self.assertIsNone(other.decrypt(blob))


class SiteKeysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.acc = A.Accounts(self.dir / "h.db")
        self.ann = self.acc.add("Ann", PW, admin=True)["id"]
        self.mo = self.acc.add("Mo", PW)["id"]
        self.keys = SiteKeys(self.acc, Vault(self.dir / "secret.key"))

    def tearDown(self):
        self.acc.close()
        self.tmp.cleanup()

    def add(self, user, host, path="", secret=SECRET, mode="bearer", name="", label=None):
        return self.keys.set(
            user,
            {
                "label": label or host + path,
                "host": host,
                "path": path,
                "mode": mode,
                "name": name,
                "secret": secret,
            },
        )

    def test_matching_rules(self):
        self.add(self.ann, "*.example.com", secret="wild")
        self.add(self.ann, "api.example.com", secret="exact")
        self.add(self.ann, "api.example.com", "/files/", secret="path", mode="header", name="X-API-Key")
        m = self.keys.match
        self.assertEqual(m(self.ann, "https://api.example.com/x.zip").key, "exact")
        found = m(self.ann, "https://api.example.com/files/x.zip")
        self.assertEqual((found.key, found.mode, found.name), ("path", "header", "X-API-Key"))
        self.assertEqual(m(self.ann, "https://cdn.example.com/x.zip").key, "wild")
        self.assertIsNone(m(self.ann, "https://example.com/x.zip"))  # a wildcard is subdomains only
        self.assertIsNone(m(self.ann, "https://badexample.com/x.zip"))
        self.assertIsNone(m(self.mo, "https://api.example.com/x.zip"))  # someone else's keys
        self.assertIsNone(m(self.ann, "http://api.example.com/x.zip"))  # never over plain http
        self.assertTrue(self.keys.matches_insecure(self.ann, "http://api.example.com/x.zip"))
        self.assertFalse(self.keys.matches_insecure(self.ann, "http://other.org/x.zip"))
        self.add(self.ann, "127.0.0.1", secret="local")
        self.assertEqual(m(self.ann, "http://127.0.0.1:8000/x.zip").key, "local")  # this machine

    def test_list_never_shows_the_secret_and_edit_keeps_it(self):
        self.add(self.ann, "api.example.com")
        (row,) = self.keys.list(self.ann)
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertTrue(row["readable"])
        self.keys.set(
            self.ann,
            {"id": row["id"], "label": "Renamed", "host": "api.example.com", "mode": "bearer", "secret": ""},
        )
        self.assertEqual(self.keys.list(self.ann)[0]["label"], "Renamed")
        self.assertEqual(self.keys.match(self.ann, "https://api.example.com/").key, SECRET)
        with self.assertRaises(KeyError):  # not Mo's to change
            self.keys.set(
                self.mo, {"id": row["id"], "label": "x", "host": "a.com", "mode": "bearer", "secret": "y"}
            )
        self.assertIsNone(self.keys.remove(self.mo, row["id"]))
        raw = self.acc._q("SELECT secret FROM site_keys")[0]["secret"]
        self.assertNotIn(SECRET.encode(), bytes(raw))  # encrypted in the database

    def test_lost_key_file(self):
        self.add(self.ann, "api.example.com")
        (self.dir / "secret.key").unlink()
        keys = SiteKeys(self.acc, Vault(self.dir / "secret.key"))
        self.assertFalse(keys.list(self.ann)[0]["readable"])
        self.assertIsNone(keys.match(self.ann, "https://api.example.com/"))
        row_id = keys.list(self.ann)[0]["id"]
        body = {"id": row_id, "label": "k", "host": "api.example.com", "mode": "bearer", "secret": ""}
        with self.assertRaisesRegex(ValueError, "enter the secret again"):
            keys.set(self.ann, body)
        keys.set(self.ann, {**body, "secret": "new"})
        self.assertEqual(keys.match(self.ann, "https://api.example.com/").key, "new")

    def test_checks(self):
        good = {"label": "k", "host": "api.example.com", "mode": "bearer", "secret": "s"}
        for bad in (
            {**good, "label": ""},
            {**good, "host": "https://api.example.com"},
            {**good, "host": "a b.com"},
            {**good, "host": "*"},
            {**good, "path": "files/"},
            {**good, "mode": "none"},
            {**good, "mode": "header", "name": "bad name"},
            {**good, "secret": "two\nlines"},
            {**good, "secret": ""},
            {**good, "secret": 5},
        ):
            with self.assertRaises(ValueError, msg=bad):
                self.keys.set(self.ann, bad)
        for i in range(SITE_KEYS_MAX):
            self.keys.set(self.ann, {**good, "label": f"k{i}"})
        with self.assertRaisesRegex(ValueError, "at most"):
            self.keys.set(self.ann, good)

    def test_removing_a_user_removes_their_keys(self):
        self.add(self.mo, "api.example.com")
        self.acc.remove("Mo")
        self.assertEqual(self.acc._q("SELECT count(*) AS n FROM site_keys")[0]["n"], 0)


class SitesTabTests(HttpBase):
    """The Account dialog's Sites tab, and downloads with key mode "auto"."""

    SITE_KEYS = True

    def setUp(self):
        super().setUp()
        self.files = FileServer()
        self.dest = self.dir / "dl"
        self.dest.mkdir()

    def tearDown(self):
        self.files.close()
        super().tearDown()

    def save(self, who="Ann", **kw):
        body = {"label": "Local", "host": "127.0.0.1", "mode": "bearer", "secret": FileServer.KEY, **kw}
        return self.req(who, "POST", "/api/account/site-key-set", body)

    def add(self, who, name, mode="auto"):
        body = {
            "urls": [self.files.base + "/secure/" + name],
            "dest": str(self.dest),
            "auth": {"mode": mode, "key": "", "name": ""},
            "after": {"convert": False},
        }
        return self.req(who, "POST", "/api/download/add", body)

    def wait(self, name):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            jobs = [j for j in self.dq.jobs() if j.url.endswith(name)]
            if jobs and jobs[0].state not in ("queued", "running"):
                return jobs[0]
            time.sleep(0.05)
        self.fail(f"{name} didn't finish")

    def test_saved_key_used_for_the_owner_only(self):
        status, res = self.save()
        self.assertEqual(status, 200, res)
        self.assertNotIn(FileServer.KEY, json.dumps(res))
        self.assertEqual(self.add("Ann", "a.zip")[0], 200)
        self.assertEqual(self.wait("a.zip").state, "done")
        self.assertEqual(self.add("Mo", "b.zip")[0], 200)  # Mo has no saved key, and there's no .env key
        job = self.wait("b.zip")
        self.assertEqual(job.state, "failed")
        self.assertIn("401", job.message)
        # the key never shows: state, the job, the audit log
        for who in ("Ann", "Mo"):
            self.assertNotIn(FileServer.KEY, json.dumps(self.req(who, "GET", "/api/state")[1]))
        self.assertEqual(self.req("Ann", "GET", "/api/state")[1]["downloads"][0]["auth_ref"], "auto")
        log = self.acc.audit_log(20, 0, "")["items"]
        self.assertIn(
            ("site key saved", "Ann", "Local: 127.0.0.1"),
            [(x["action"], x["target"], x["detail"]) for x in log],
        )
        self.assertNotIn(FileServer.KEY, json.dumps(log))

    def test_client_cant_choose_a_reference(self):
        self.save()
        body = {
            "urls": [self.files.base + "/secure/c.zip"],
            "dest": str(self.dest),
            "auth": {"mode": "none"},
            "auth_ref": "auto",
            "after": {"convert": False},
        }
        self.assertEqual(self.req("Ann", "POST", "/api/download/add", body)[0], 200)
        job = self.wait("c.zip")
        self.assertEqual((job.auth_ref, job.state), ("", "failed"))

    def test_who_may_manage(self):
        self.assertIsNone(self.req("Rea", "GET", "/api/state")[1]["account"]["site_keys"])  # no download
        self.assertEqual(self.save("Rea")[0], 403)
        self.assertEqual(self.save()[0], 200)
        (key,) = self.req("Ann", "GET", "/api/state")[1]["account"]["site_keys"]
        self.assertEqual(self.req("Mo", "POST", "/api/account/site-key-remove", {"id": key["id"]})[0], 404)
        self.assertEqual(self.req("Ann", "POST", "/api/account/site-key-remove", {"id": key["id"]})[0], 200)
        self.assertEqual(self.req("Ann", "GET", "/api/state")[1]["account"]["site_keys"], [])

    def test_extension_token_cant_manage_keys(self):
        _, token = self.acc.create_token(self.acc.get("Ann")["id"], "ext")
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"label": "x", "host": "a.com", "mode": "bearer", "secret": "s"}).encode()
        conn.request(
            "POST",
            "/api/account/site-key-set",
            body=body,
            headers={"Content-Type": "application/json", "X-Harbor-Token": token},
        )
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_plain_http_gets_a_note(self):
        self.save(host="example.org")
        body = {"urls": ["http://example.org/x.zip"], "dest": str(self.dest), "auth": {"mode": "auto"}}
        status, res = self.req("Ann", "POST", "/api/download/add", {**body, "after": {"convert": False}})
        self.assertEqual(status, 200, res)
        self.assertIn("only sent over https", res["key_note"])
        self.dq.cancel_all()

    def test_saved_job_keeps_the_reference_not_the_key(self):
        from harbor.state import QueueStore

        self.save()
        self.dq.set_paused(True)
        self.assertEqual(self.add("Ann", "d.zip")[0], 200)
        store = QueueStore(self.dir / "state.json")
        store.attach(self.cq, self.dq, self.owners)
        store.save()
        text = (self.dir / "state.json").read_text()
        self.assertNotIn(FileServer.KEY, text)
        (saved,) = json.loads(text)["downloads"]
        self.assertEqual((saved["auth_ref"], saved["auth_mode"]), ("auto", "none"))
        self.dq.cancel_all()
        self.dq.clear_finished()
        notes = store.restore("")
        self.assertEqual(
            notes, ["restored 1 unfinished job(s) from the last run"]
        )  # nothing skipped for want of a key
        self.dq.set_paused(False)
        job = self.wait("d.zip")
        self.assertEqual((job.auth_ref, job.state), ("auto", "done"))
        self.assertEqual(self.owners.get("download", job.id), self.acc.get("Ann")["id"])


if __name__ == "__main__":
    unittest.main()
