"""Tests for roles and permissions: what each role may do, job ownership, and the admin API."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from cbzshelf.library import Library
from helpers import FileServer, comic_entries, make_zip

from harbor import accounts as A
from harbor.app import build, make_on_saved, serve_harbor
from harbor.config import Settings
from harbor.perms import JobOwners
from harbor.state import QueueStore

PW = "correct horse"


class HttpBase(unittest.TestCase):
    """A harbor with three accounts (Ann admin, Mo member, Rea reader) and a one-book library."""

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


class PermissionHttpTests(HttpBase):
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


class LibraryAccessTests(HttpBase):
    """Who sees which library: the Admin tab's matrix, enforced through cbz-shelf's ``visible``."""

    def setUp(self):
        super().setUp()
        (self.dir / "Private" / "P").mkdir(parents=True)
        make_zip(self.dir / "Private" / "P" / "p.cbz", comic_entries())
        self.priv = self.lib.add_library("Private")["id"]
        self.lib.add_root(str(self.dir / "Private"), self.priv)
        self.lib.wait_scan(30)
        self.pbook = self.lib.search(q="p")["items"][0]

    def guest(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        return resp.status, body

    def series_names(self, who):
        _, body = (
            self.guest("/api/library/series") if who is None else self.req(who, "GET", "/api/library/series")
        )
        return sorted(x["name"] for x in body["items"])

    def test_open_to_everyone_by_default(self):
        for who in ("Ann", "Mo", "Rea", None):
            self.assertEqual(self.series_names(who), ["P", "S"], who)
        st = self.req("Ann", "GET", "/api/state")[1]
        self.assertEqual(
            st["admin_libraries"],
            [
                {"id": 1, "name": "Library", "roles": None, "guests": True},
                {"id": self.priv, "name": "Private", "roles": None, "guests": True},
            ],
        )

    def test_restricted_library_is_hidden_and_refused(self):
        body = {"id": self.priv, "roles": [2], "guests": False}  # Members only
        self.assertEqual(self.req("Ann", "POST", "/api/admin/library-access", body)[1]["roles"], [2])
        self.assertEqual(self.series_names("Ann"), ["P", "S"])  # Admin always
        self.assertEqual(self.series_names("Mo"), ["P", "S"])
        self.assertEqual(self.series_names("Rea"), ["S"])
        self.assertEqual(self.series_names(None), ["S"])
        bid = self.pbook["id"]
        for path in (
            f"/api/library/book?id={bid}",
            f"/api/library/page?book={bid}&n=0",
            f"/api/library/thumb?book={bid}",
        ):
            self.assertEqual(self.req("Rea", "GET", path)[0], 404, path)
            self.assertEqual(self.guest(path)[0], 404, path)
        self.assertEqual(self.req("Rea", "POST", "/api/library/progress", {"id": bid, "page": 1})[0], 404)
        st = self.req("Rea", "GET", "/api/state")[1]
        self.assertEqual([x["name"] for x in st["library_list"]], ["Library"])
        self.assertEqual(self.guest("/api/state")[1]["library"]["books"], 1)
        self.assertEqual(self.req("Mo", "GET", f"/api/library/book?id={bid}")[0], 200)
        # guests back on, roles still Members only
        self.req("Ann", "POST", "/api/admin/library-access", {**body, "guests": True})
        self.assertEqual(self.series_names(None), ["P", "S"])
        self.assertEqual(self.series_names("Rea"), ["S"])
        # removing a role takes it off every library's list
        _, role = self.req("Ann", "POST", "/api/admin/role-add", {"name": "Tmp", "perms": ["library.read"]})
        self.req(
            "Ann",
            "POST",
            "/api/admin/library-access",
            {"id": self.priv, "roles": [2, role["id"]], "guests": True},
        )
        self.req("Ann", "POST", "/api/admin/role-remove", {"id": role["id"]})
        self.assertEqual(self.acc.library_rules()[self.priv]["roles"], [2])
        # back to everyone: the rule row goes
        self.req("Ann", "POST", "/api/admin/library-access", {"id": self.priv, "roles": None, "guests": True})
        self.assertEqual(self.acc.library_rules(), {})

    def test_bad_library_access_requests(self):
        post = lambda body: self.req("Ann", "POST", "/api/admin/library-access", body)  # noqa: E731
        self.assertEqual(post({"id": 999, "roles": None, "guests": True})[0], 404)
        self.assertEqual(post({"id": self.priv, "roles": [77], "guests": True})[0], 404)
        self.assertEqual(post({"id": self.priv, "roles": "2", "guests": True})[0], 400)
        self.assertEqual(post({"id": self.priv, "roles": None, "guests": "no"})[0], 400)
        self.assertEqual(
            self.req("Mo", "POST", "/api/admin/library-access", {"id": self.priv, "roles": []})[0], 403
        )

    def test_download_add_warns_about_books_already_in_visible_libraries(self):
        self.dq.set_paused(True)
        urls = [
            "http://127.0.0.1:1/files/a.zip?x=1",  # a.cbz is in "Library"
            "http://127.0.0.1:1/files/p.cbz",  # p.cbz is in "Private"
            "http://127.0.0.1:1/files/new.cbz",
            "http://127.0.0.1:1/download?id=3",  # name unknown until the server answers
        ]
        body = {"urls": urls, "dest": str(self.dir), "after": {"convert": False}}
        status, r = self.req("Mo", "POST", "/api/download/add", body)
        self.assertEqual((status, r["added"]), (200, 4))
        self.assertEqual(
            [(x["url"], x["file"]) for x in r["in_library"]], [(urls[0], "a.cbz"), (urls[1], "p.cbz")]
        )
        self.dq.cancel_all()
        self.req("Ann", "POST", "/api/admin/library-access", {"id": self.priv, "roles": [3], "guests": True})
        status, r = self.req("Mo", "POST", "/api/download/add", body)  # Private is hidden from Mo now
        self.assertEqual([x["file"] for x in r["in_library"]], ["a.cbz"])

    def test_non_admin_role_manager_stays_within_their_own(self):
        _, role = self.req(
            "Ann",
            "POST",
            "/api/admin/role-add",
            {"name": "Keeper", "perms": ["roles.manage", "library.read"]},
        )
        self.req("Ann", "POST", "/api/admin/user-add", {"name": "Kee", "password": PW, "role_id": role["id"]})
        kee = lambda body: self.req("Kee", "POST", "/api/admin/library-access", body)  # noqa: E731
        # Members have permissions Kee lacks: taking the library from them is refused
        self.assertEqual(kee({"id": self.priv, "roles": [3, role["id"]], "guests": True})[0], 403)
        # Readers and Keepers are within Kee's permissions; Members keep their access
        self.req(
            "Ann",
            "POST",
            "/api/admin/library-access",
            {"id": self.priv, "roles": [2, 3, role["id"]], "guests": True},
        )
        self.assertEqual(kee({"id": self.priv, "roles": [2, role["id"]], "guests": False})[0], 200)
        self.assertEqual(self.series_names("Rea"), ["S"])
        # a library Kee can't see doesn't exist for Kee
        self.req("Ann", "POST", "/api/admin/library-access", {"id": self.priv, "roles": [2], "guests": False})
        self.assertEqual(kee({"id": self.priv, "roles": None, "guests": True})[0], 404)
        names = [x["name"] for x in self.req("Kee", "GET", "/api/state")[1]["admin_libraries"]]
        self.assertEqual(names, ["Library"])


class OwnershipUnitTests(unittest.TestCase):
    def test_add_and_download_hand_off_at_the_same_time(self):
        """A user's add (still scanning) and a download's hand-off to the converter at once: the
        hand-off doesn't wait for the add, and each conversion gets the right owner."""
        mo, ann = 2, 1
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = Path(tmp_dir.name)
        make_zip(tmp / "mine.zip", comic_entries())
        make_zip(tmp / "downloaded.zip", comic_entries())
        owners = JobOwners()
        cq, dq = build(jobs=1, image_threads=1, downloads=1, timeout=5, retries=0, owners=owners)
        cq.set_paused(True)
        self.addCleanup(cq.shutdown)
        self.addCleanup(dq.shutdown)
        owners.set("download", 41, ann)  # Ann queued download 41
        inside, release = threading.Event(), threading.Event()

        def mo_adds():
            with owners.adding("convert", cq, mo):
                cq.add([tmp / "mine.zip"], 80, delete_original=False)
                inside.set()
                release.wait(10)  # the add is still going, like a long folder scan

        adder = threading.Thread(target=mo_adds)
        adder.start()
        self.assertTrue(inside.wait(10))
        download = SimpleNamespace(
            id=41,
            after={"convert": True, "quality": 80, "delete_original": False, "method": 4},
            linked_id=None,
            message="",
        )
        on_saved = make_on_saved(cq, owners=owners)
        hand_off = threading.Thread(target=on_saved, args=(download, tmp / "downloaded.zip"))
        hand_off.start()
        hand_off.join(5)
        self.assertFalse(hand_off.is_alive(), "the hand-off waited for another user's add")
        release.set()
        adder.join(10)
        by_name = {Path(j.path).name: owners.get("convert", j.id) for j in cq.jobs()}
        self.assertEqual(by_name, {"mine.zip": mo, "downloaded.zip": ann})

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
