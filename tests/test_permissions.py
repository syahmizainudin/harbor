"""Tests for roles and permissions: what each role may do, job ownership, and the admin API."""

from typing import ClassVar
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

    EXPLICIT: ClassVar[dict] = {}  # settings "given in .env", locked on the Settings tab
    SITE_KEYS = False  # saved site keys on (secret.key in the temp folder)

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
        self.settings = Settings(explicit=dict(self.EXPLICIT))
        self.site_keys = None
        if self.SITE_KEYS:
            from harbor.app import make_auth_for
            from harbor.sitekeys import SiteKeys, Vault

            self.site_keys = SiteKeys(self.acc, Vault(self.dir / "secret.key"))
            self.dq.auth_for = make_auth_for(self.settings, self.owners, self.site_keys)
        self.server = serve_harbor(
            0,
            self.cq,
            self.dq,
            self.settings,
            library=self.lib,
            accounts=self.acc,
            owners=self.owners,
            site_keys=self.site_keys,
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
        compilation = {"id": sid, "on": True}
        self.assertEqual(self.req("Rea", "POST", "/api/library/set-compilation", compilation)[0], 403)
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


class DownloadLaneTests(HttpBase):
    """Download lanes: managed with download.lanes on the Admin page, applied live, audited."""

    def test_admin_manages_lanes_live(self):
        body = {"name": "manga", "workers": 2, "rate": "5M", "hours": "01:00-07:00"}
        status, res = self.req("Ann", "POST", "/api/admin/lane-set", body)
        self.assertEqual(status, 200, res)
        self.assertEqual(
            res["lanes"],
            [{"name": "manga", "workers": 2, "rate": 5 * 1024**2, "hours": "01:00-07:00", "roles": None}],
        )
        (lane,) = self.dq.lanes()  # live, no restart
        self.assertEqual(
            (lane.name, lane.workers, lane.rate, lane.hours), ("manga", 2, 5 * 1024**2, (60, 420))
        )
        st = self.req("Ann", "GET", "/api/state")[1]
        self.assertEqual(st["admin_lanes"][0]["name"], "manga")
        self.assertEqual(st["download_lanes"][0]["hours"], "01:00-07:00")
        # the same name in another case edits it
        self.req(
            "Ann", "POST", "/api/admin/lane-set", {"name": "MANGA", "workers": 1, "rate": "", "hours": ""}
        )
        (lane,) = self.dq.lanes()
        self.assertEqual((lane.name, lane.workers, lane.rate, lane.hours), ("MANGA", 1, 0, None))
        # a member downloads into it, sees it, but can't change it
        dest = self.dir / "dl"
        dest.mkdir()
        add = {
            "urls": ["http://127.0.0.1:9/a.zip"],
            "dest": str(dest),
            "lane": "MANGA",
            "after": {"convert": False},
        }
        self.assertEqual(self.req("Mo", "POST", "/api/download/add", add)[0], 200)
        self.assertEqual([d["lane"] for d in self.req("Mo", "GET", "/api/state")[1]["downloads"]], ["MANGA"])
        mo = self.req("Mo", "GET", "/api/state")[1]
        self.assertEqual(mo["download_lanes"][0]["name"], "MANGA")
        self.assertNotIn("admin_lanes", mo)
        for action, b in (("lane-set", body), ("lane-remove", {"name": "MANGA"})):
            self.assertEqual(self.req("Mo", "POST", f"/api/admin/{action}", b)[0], 403)
        self.assertEqual(self.req("Ann", "POST", "/api/download/add", {**add, "lane": "nope"})[0], 400)
        for bad in (
            {"name": "", "workers": 1},
            {"name": "x", "workers": 0},
            {"name": "x", "workers": "2"},
            {"name": "x", "rate": "fast"},
            {"name": "x", "hours": "1-7"},
        ):
            self.assertEqual(self.req("Ann", "POST", "/api/admin/lane-set", bad)[0], 400, bad)
        self.assertEqual(self.req("Ann", "POST", "/api/admin/lane-remove", {"name": "manga"})[0], 200)
        self.assertEqual(self.dq.lanes(), [])
        self.assertEqual(self.req("Ann", "POST", "/api/admin/lane-remove", {"name": "manga"})[0], 404)
        log = [(x["action"], x["target"]) for x in self.acc.audit_log(10, 0, "")["items"]]
        self.assertIn(("download lane saved", "manga"), log)
        self.assertIn(("download lane removed", "manga"), log)

    def lane_add(self, lane):
        dest = self.dir / "dl"
        dest.mkdir(exist_ok=True)
        url = "http://127.0.0.1:9/a.zip"
        return {"urls": [url], "dest": str(dest), "lane": lane, "after": {"convert": False}}

    def test_lane_kept_to_some_roles(self):
        roles = {r["name"]: r["id"] for r in self.acc.roles()}
        body = {"name": "vip", "workers": 1, "roles": [roles["Reader"]]}
        status, res = self.req("Ann", "POST", "/api/admin/lane-set", body)
        self.assertEqual((status, res["lanes"][0]["roles"]), (200, [roles["Reader"]]), res)
        self.req("Ann", "POST", "/api/admin/lane-set", {"name": "open", "workers": 1})
        ann = self.req("Ann", "GET", "/api/state")[1]
        self.assertEqual([x["name"] for x in ann["download_lanes"]], ["vip", "open"])  # Admin always may
        self.assertNotIn(roles["Admin"], [x["id"] for x in ann["lane_roles"]])
        mo = self.req("Mo", "GET", "/api/state")[1]
        self.assertEqual([x["name"] for x in mo["download_lanes"]], ["open"])  # Member isn't on the list
        status, err = self.req("Mo", "POST", "/api/download/add", self.lane_add("vip"))
        self.assertEqual((status, err), (403, {"error": "you can't use the download lane 'vip'"}))
        self.assertEqual(self.req("Mo", "POST", "/api/download/add", self.lane_add("open"))[0], 200)
        self.assertEqual(self.req("Ann", "POST", "/api/download/add", self.lane_add("vip"))[0], 200)
        # Admin only: an empty list
        self.req("Ann", "POST", "/api/admin/lane-set", {**body, "roles": [roles["Admin"]]})
        self.assertEqual(self.acc.lanes()[0]["roles"], [])
        log = self.acc.audit_log(10, 0, "download lane saved")["items"]
        self.assertTrue(log[0]["detail"].endswith("for Admin only"), log[0])
        for bad in ({**body, "roles": "Reader"}, {**body, "roles": [True]}):
            self.assertEqual(self.req("Ann", "POST", "/api/admin/lane-set", bad)[0], 400, bad)
        self.assertEqual(self.req("Ann", "POST", "/api/admin/lane-set", {**body, "roles": [999]})[0], 404)

    def test_lane_managers_see_every_lane(self):
        role = self.acc.add_role("Lane keepers", ["download", "download.lanes"])
        self.acc.add("Kit", PW, role=role["id"])
        self.req("Ann", "POST", "/api/admin/lane-set", {"name": "vip", "workers": 1, "roles": []})
        kit = self.req("Kit", "GET", "/api/state")[1]
        self.assertEqual([x["name"] for x in kit["download_lanes"]], ["vip"])  # to pause it
        self.assertEqual(self.req("Kit", "POST", "/api/download/add", self.lane_add("vip"))[0], 403)
        self.assertEqual(self.req("Kit", "POST", "/api/download/lane-pause", {"name": "vip"})[0], 200)

    def test_removed_role_leaves_lane_lists(self):
        role = self.acc.add_role("Helpers", ["download"])
        self.acc.set_lane("vip", 1, "", "", [role["id"]])
        self.acc.remove_role(role["id"])
        self.assertEqual(self.acc.lanes()[0]["roles"], [])

    def test_lane_pause_needs_download_lanes(self):
        self.req("Ann", "POST", "/api/admin/lane-set", {"name": "manga", "workers": 1})
        pause = {"name": "manga", "paused": True}
        self.assertEqual(self.req("Mo", "POST", "/api/download/lane-pause", pause)[0], 403)
        self.assertFalse(self.dq.lanes_state()[0]["paused"])
        status, res = self.req("Ann", "POST", "/api/download/lane-pause", pause)
        self.assertEqual((status, res["lanes"][0]["paused"]), (200, True))
        self.assertTrue(self.req("Mo", "GET", "/api/state")[1]["download_lanes"][0]["paused"])
        self.assertEqual(self.req("Ann", "POST", "/api/download/lane-pause", {"name": "nope"})[0], 400)

    def test_guests_see_no_lanes(self):
        self.req("Ann", "POST", "/api/admin/lane-set", {"name": "manga", "workers": 1})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/state")
        st = json.loads(conn.getresponse().read())
        conn.close()
        self.assertEqual(st["download_lanes"], [])


class ServerSettingsTests(HttpBase):
    """The Admin page's Settings tab: settings.manage, .env locks, live and after-restart settings."""

    EXPLICIT: ClassVar[dict] = {"jobs": "/x/.env"}

    def rows(self, who="Ann"):
        return {r["name"]: r for r in self.req(who, "GET", "/api/state")[1]["admin_settings"]}

    def set(self, name, value, who="Ann"):
        return self.req(who, "POST", "/api/admin/setting-set", {"name": name, "value": value})

    def test_only_with_settings_manage(self):
        self.assertNotIn("admin_settings", self.req("Mo", "GET", "/api/state")[1])
        self.assertEqual(self.set("quality", "60", "Mo")[0], 403)
        self.assertEqual(self.settings.quality, 80)

    def test_live_settings_apply_at_once(self):
        status, res = self.set("quality", "65")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.req("Mo", "GET", "/api/state")[1]["defaults"]["quality"], 65)
        self.assertEqual(self.set("download_rate", "5M")[0], 200)
        self.assertEqual(self.dq.rate, 5 * 1024**2)
        self.assertEqual(self.set("download_hours", "01:00-07:00")[0], 200)
        self.assertEqual(self.dq.hours, (60, 420))
        self.assertEqual(self.set("download_fair", "true")[0], 200)
        self.assertTrue(self.dq.fair)
        self.assertEqual(self.set("download_dir", str(self.dir))[0], 200)
        self.assertEqual(self.req("Mo", "GET", "/api/state")[1]["harbor"]["download_dir"], str(self.dir))
        self.assertEqual(self.set("session_days", "7")[0], 200)
        self.assertEqual(self.acc.session_seconds, 7 * 86400)
        self.assertEqual(self.acc.saved_settings()["quality"], "65")
        row = self.rows()["quality"]
        self.assertEqual((row["value"], row["saved"], row["default"]), ("65", "65", "80"))
        log = self.acc.audit_log(10, 0, "setting changed")["items"]
        self.assertIn(("HARBOR_QUALITY", "80 → 65"), [(x["target"], x["detail"]) for x in log])
        # back to the default
        self.assertEqual(self.set("quality", "")[0], 200)
        self.assertNotIn("quality", self.acc.saved_settings())
        self.assertEqual(self.req("Ann", "GET", "/api/state")[1]["defaults"]["quality"], 80)
        self.assertEqual(self.set("download_hours", "")[0], 200)
        self.assertIsNone(self.dq.hours)

    def test_restart_settings_are_saved_not_applied(self):
        self.assertEqual(self.set("downloads", "3")[0], 200)
        row = self.rows()["downloads"]
        self.assertEqual((row["value"], row["running"], row["applies"]), ("3", "2", "restart"))
        self.assertEqual(self.dq.workers, 1)

    def test_refused_changes(self):
        status, err = self.set("jobs", "2")  # given in .env
        self.assertEqual(status, 400)
        self.assertIn("HARBOR_JOBS is set in /x/.env", err["error"])
        self.assertEqual(self.rows()["jobs"]["locked"], "/x/.env")
        status, err = self.set("watch_delete_original", "true")
        self.assertEqual(
            (status, err), (400, {"error": "HARBOR_WATCH_DELETE_ORIGINAL can only be set in .env"})
        )
        self.assertEqual(self.set("api_key", "secret")[0], 404)
        self.assertEqual(self.set("port", "1")[0], 404)
        for name, bad in (
            ("quality", "101"),
            ("download_rate", "fast"),
            ("download_hours", "1-7"),
            ("quality", "6\n0"),
        ):
            self.assertEqual(self.set(name, bad)[0], 400, (name, bad))
        self.assertEqual(
            self.req("Ann", "POST", "/api/admin/setting-set", {"name": "quality", "value": 6})[0], 400
        )
        self.assertEqual(self.acc.saved_settings(), {})
        names = set(self.rows())
        self.assertFalse(names & {"api_key", "port", "host", "accounts", "tls_key", "accounts_db"})


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
