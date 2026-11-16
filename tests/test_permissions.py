"""Tests for roles and permissions: what each role may do, job ownership, and the admin API."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cbzshelf.library import Library
from helpers import FileServer, comic_entries, make_zip

from harbor import accounts as A
from harbor.app import build, serve_harbor
from harbor.config import Settings
from harbor.perms import JobOwners
from harbor.state import QueueStore

PW = "correct horse"


class PermissionHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.acc = A.Accounts(self.dir / "h.db")
        self.acc.add("Ann", PW, admin=True)
        self.acc.add("Mo", PW)  # Member
        self.acc.add("Rea", PW, role="Reader")
        (self.dir / "Comics" / "S").mkdir(parents=True)
        make_zip(self.dir / "Comics" / "S" / "a.cbz", comic_entries())
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.dir / "Comics"))
        self.lib.wait_scan(30)
        self.owners = JobOwners()
        self.cq, self.dq = build(
            jobs=1, image_threads=1, downloads=1, timeout=5, retries=0, owners=self.owners
        )
        self.cq.set_paused(True)  # jobs stay queued, so the tests see them
        self.server = serve_harbor(
            0, self.cq, self.dq, Settings(), library=self.lib, accounts=self.acc, owners=self.owners
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sid = {}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        self.lib.close()
        self.acc.close()
        self.tmp.cleanup()

    def req(self, who, method, path, body=None):
        if who not in self.sid:
            _, sid = self.acc.login(who, PW)
            self.sid[who] = sid
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Cookie": f"harbor_session={self.sid[who]}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw

    def zip_(self, name):
        p = self.dir / "in" / name
        p.parent.mkdir(exist_ok=True)
        make_zip(p, comic_entries())
        return str(p)

    def test_reader_can_only_read_the_library(self):
        self.assertEqual(self.req("Rea", "GET", "/api/dirs")[0], 403)
        self.assertEqual(self.req("Rea", "POST", "/api/add", {"paths": [self.zip_("r.zip")]})[0], 403)
        self.assertEqual(
            self.req("Rea", "POST", "/api/download/add", {"urls": ["http://x/y"], "dest": "/tmp"})[0], 403
        )
        self.assertEqual(self.req("Rea", "GET", "/api/library/series")[0], 200)
        sid = self.req("Rea", "GET", "/api/library/series")[1]["items"][0]["id"]
        self.assertEqual(
            self.req("Rea", "POST", "/api/library/edit-series", {"id": sid, "changes": {"name": "X"}})[0], 403
        )
        self.assertEqual(
            self.req("Rea", "POST", "/api/library/reader-settings", {"id": sid, "changes": {"fit": "width"}})[
                0
            ],
            200,
        )
        self.assertEqual(self.req("Rea", "POST", "/api/library/scan", {})[0], 403)
        st = self.req("Rea", "GET", "/api/state")[1]
        self.assertEqual(st["library_can"], {"edit": False, "manage": False})
        self.assertNotIn("admin_users", st)
        self.assertEqual(
            self.req("Rea", "POST", "/api/admin/user-add", {"name": "Z", "password": PW, "role_id": 3})[0],
            403,
        )

    def test_members_see_and_cancel_only_their_own_jobs(self):
        self.assertEqual(self.req("Mo", "GET", "/api/dirs")[0], 200)
        self.assertEqual(self.req("Mo", "POST", "/api/pause", {"paused": True})[0], 403)  # queue-wide
        self.assertEqual(self.req("Ann", "POST", "/api/pause", {"paused": True})[0], 200)
        self.assertEqual(
            self.req("Mo", "POST", "/api/add", {"paths": [self.zip_("m.zip")], "delete_original": False})[0],
            200,
        )
        self.assertEqual(
            self.req("Ann", "POST", "/api/add", {"paths": [self.zip_("a.zip")], "delete_original": False})[0],
            200,
        )
        mo_jobs = self.req("Mo", "GET", "/api/state")[1]["jobs"]
        ann_jobs = self.req("Ann", "GET", "/api/state")[1]["jobs"]
        self.assertEqual([j["name"] for j in mo_jobs], ["m.zip"])
        self.assertEqual(sorted(j["name"] for j in ann_jobs), ["a.zip", "m.zip"])  # jobs.see_all
        ann_job = next(j["id"] for j in ann_jobs if j["name"] == "a.zip")
        self.assertEqual(self.req("Mo", "POST", "/api/cancel", {"id": ann_job})[0], 403)
        self.assertEqual(self.req("Mo", "POST", "/api/cancel", {"id": mo_jobs[0]["id"]})[0], 200)
        self.assertEqual(self.req("Mo", "POST", "/api/clear", {})[0], 403)

    def test_delete_original_and_convert_need_their_permissions(self):
        _, role = self.req("Ann", "POST", "/api/admin/role-add", {"name": "Fetcher", "perms": ["download"]})
        self.req("Ann", "POST", "/api/admin/user-add", {"name": "Fe", "password": PW, "role_id": role["id"]})
        add = {"urls": ["http://127.0.0.1:1/x.zip"], "dest": str(self.dir)}
        status, d = self.req("Fe", "POST", "/api/download/add", add)
        self.assertEqual(status, 403)
        self.assertIn("convert", d["error"])
        self.assertEqual(
            self.req("Fe", "POST", "/api/download/add", {**add, "after": {"convert": False}})[0], 200
        )
        self.req(
            "Ann", "POST", "/api/admin/role-update", {"id": role["id"], "perms": ["download", "convert"]}
        )
        status, d = self.req("Fe", "POST", "/api/add", {"paths": [self.zip_("f.zip")]})  # deletes by default
        self.assertEqual(status, 403)
        self.assertIn("originals", d["error"])

    def test_download_hand_off_keeps_the_owner(self):
        src = make_zip(self.dir / "src.zip", comic_entries())
        files = FileServer({"x.zip": src.read_bytes()})
        self.addCleanup(files.close)
        out = self.dir / "dl"
        out.mkdir()
        body = {
            "urls": [files.base + "/files/a/x.zip"],
            "dest": str(out),
            "after": {"delete_original": False},
        }
        self.assertEqual(self.req("Mo", "POST", "/api/download/add", body)[0], 200)
        self.dq.wait_idle()
        st = self.req("Mo", "GET", "/api/state")[1]
        self.assertEqual(len(st["downloads"]), 1)
        self.assertEqual([j["name"] for j in st["jobs"]], ["x.zip"])  # the conversion is Mo's too
        self.assertEqual(self.req("Rea", "GET", "/api/state")[1]["jobs"], [])

    def test_admin_api_users_and_roles(self):
        status, u = self.req(
            "Ann", "POST", "/api/admin/user-add", {"name": "Cy", "password": PW, "role_id": 3}
        )
        self.assertEqual((status, u["role"]), (200, "Reader"))
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/user-role", {"id": u["id"], "role_id": 2})[1]["role"],
            "Member",
        )
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/user-password", {"id": u["id"], "password": "new pass word"})[
                0
            ],
            200,
        )
        self.assertEqual(self.acc.login("Cy", "new pass word")[0]["name"], "Cy")
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/user-disable", {"id": u["id"], "disabled": True})[0], 200
        )
        ann = self.acc.get("Ann")["id"]
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/user-disable", {"id": ann})[0], 400
        )  # not yourself
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/role-update", {"id": 1, "perms": []})[0], 400
        )  # Admin is locked
        self.assertEqual(self.req("Ann", "POST", "/api/admin/role-remove", {"id": 2})[0], 400)  # has members
        _, r = self.req("Ann", "POST", "/api/admin/role-add", {"name": "Empty", "perms": []})
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/role-add", {"name": "Bad", "perms": ["fly"]})[0], 400
        )
        self.assertEqual(self.req("Ann", "POST", "/api/admin/role-remove", {"id": r["id"]})[0], 200)
        st = self.req("Ann", "GET", "/api/state")[1]
        self.assertEqual({x["name"] for x in st["admin_roles"]}, {"Admin", "Member", "Reader"})
        self.assertEqual(len(st["permissions"]), len(A.PERMISSIONS))
        self.assertTrue(all("pass_hash" not in x for x in st["admin_users"]))

    def test_managing_users_never_leads_to_admin(self):
        _, role = self.req(
            "Ann",
            "POST",
            "/api/admin/role-add",
            {"name": "Helpdesk", "perms": ["users.manage", "library.read"]},
        )
        self.req("Ann", "POST", "/api/admin/user-add", {"name": "Hd", "password": PW, "role_id": role["id"]})
        hd = lambda path, body: self.req("Hd", "POST", "/api/admin/" + path, body)  # noqa: E731
        self.assertEqual(hd("user-add", {"name": "Boss", "password": PW, "role_id": 1})[0], 403)
        self.assertEqual(
            hd("user-add", {"name": "Kid", "password": PW, "role_id": 3})[0], 200
        )  # Reader ⊆ Helpdesk
        me = self.acc.get("Hd")["id"]
        self.assertEqual(hd("user-role", {"id": me, "role_id": 1})[0], 403)
        self.assertEqual(
            hd("user-password", {"id": self.acc.get("Ann")["id"], "password": "taken over"})[0], 403
        )
        self.assertEqual(hd("user-disable", {"id": self.acc.get("Mo")["id"]})[0], 403)  # Mo has more than Hd
        self.assertEqual(hd("role-add", {"name": "X", "perms": []})[0], 403)  # no roles.manage


class OwnershipUnitTests(unittest.TestCase):
    def test_saved_jobs_keep_their_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            make_zip(tmp / "A.zip", comic_entries())
            owners = JobOwners()
            store = QueueStore(tmp / "q.json")
            cq, dq = build(jobs=1, image_threads=1, downloads=1, timeout=5, retries=0, owners=owners)
            cq.set_paused(True)
            store.attach(cq, dq, owners)
            with owners.adding("convert", cq, 7):
                cq.add([tmp / "A.zip"], 80, delete_original=False)
            store.save()
            cq.shutdown()
            dq.shutdown()
            self.assertEqual(json.loads((tmp / "q.json").read_text())["convert"][0]["owner"], 7)

            owners2 = JobOwners()
            cq2, dq2 = build(jobs=1, image_threads=1, downloads=1, timeout=5, retries=0, owners=owners2)
            cq2.set_paused(True)
            self.addCleanup(cq2.shutdown)
            self.addCleanup(dq2.shutdown)
            store2 = QueueStore(tmp / "q.json")
            store2.attach(cq2, dq2, owners2)
            store2.restore()
            self.assertEqual([owners2.get("convert", j.id) for j in cq2.jobs()], [7])


if __name__ == "__main__":
    unittest.main()
