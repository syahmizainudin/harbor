"""Tests for the job history: what is recorded, when, for whom, and who may read it."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import comic_entries, make_zip

from harbor import accounts as A
from harbor.app import build, serve_harbor
from harbor.config import Settings
from harbor.history import History
from harbor.perms import JobOwners

PW = "correct horse"


def fake_convert(job_id=1, state="done", finished_at=1000.0, **kw):
    return SimpleNamespace(
        id=job_id,
        path="/in/Blue v01.zip",
        state=state,
        message=kw.get("message", "ok"),
        output="/in/Blue v01.cbz",
        bytes_in=kw.get("bytes_in", 1000),
        bytes_out=kw.get("bytes_out", 400),
        added_at=900.0,
        started_at=950.0,
        finished_at=finished_at,
    )


def fake_download(job_id=1, state="done", finished_at=1000.0):
    return SimpleNamespace(
        id=job_id,
        url="http://127.0.0.1/x/Red%201.zip?page=2",
        filename="Red 1.zip",
        state=state,
        message="saved",
        output="/dl/Red 1.zip",
        done=500,
        linked_id=7,
        added_at=900.0,
        started_at=950.0,
        finished_at=finished_at,
    )


class HistoryUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.owners = JobOwners()
        self.h = History(Path(self.tmp.name) / "history.db", keep_days=90, owners=self.owners)

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def items(self, **kw):
        self.h.flush()
        return self.h.list(**kw)["items"]

    def test_records_a_job_once_when_it_finishes(self):
        now = time.time()
        self.h.on_change(fake_convert(state="queued", finished_at=None))
        self.h.on_change(fake_convert(state="running", finished_at=None))
        self.assertEqual(self.items(), [])
        self.owners.set("convert", 1, 5)
        for _ in range(3):  # progress ticks after the finish don't add more
            self.h.on_change(fake_convert(finished_at=now))
        (rec,) = self.items()
        self.assertEqual(
            (rec["kind"], rec["name"], rec["owner"], rec["state"], rec["bytes_in"], rec["bytes_out"]),
            ("convert", "Blue v01.zip", 5, "done", 1000, 400),
        )
        # a retry reuses the id; finishing again is a new record
        self.h.on_change(fake_convert(state="failed", finished_at=now + 5, message="bad zip"))
        self.assertEqual([r["state"] for r in self.items()], ["failed", "done"])

    def test_download_record(self):
        self.h.on_change(fake_download(finished_at=time.time()))
        (rec,) = self.items()
        self.assertEqual(
            (rec["kind"], rec["name"], rec["source"], rec["bytes_in"], rec["linked_id"], rec["owner"]),
            ("download", "Red 1.zip", "http://127.0.0.1/x/Red%201.zip?page=2", 500, 7, None),
        )
        self.assertEqual(self.h.list(kind="convert")["total"], 0)
        self.assertEqual(self.h.list(owner=3)["total"], 0)

    def test_disabled_records_nothing(self):
        self.h.enabled = False  # set before shutdown: cancelled-by-shutdown jobs are restored, not finished
        self.h.on_change(fake_convert(state="cancelled", finished_at=time.time()))
        self.assertEqual(self.items(), [])

    def test_saved_totals_and_pruning(self):
        now = time.time()
        self.h.on_change(fake_convert(1, finished_at=now))
        self.h.on_change(fake_convert(2, finished_at=now - 40 * 86400, bytes_in=2000, bytes_out=1000))
        self.h.on_change(fake_convert(3, state="failed", finished_at=now))
        self.owners.set("convert", 1, 5)
        self.h.flush()
        self.assertEqual(self.h.saved(), {"all": 1600, "last_30_days": 600, "conversions": 2})
        self.h.keep_days = 30
        self.assertEqual(self.h.prune(), 1)
        self.assertEqual(self.h.saved(), {"all": 600, "last_30_days": 600, "conversions": 1})
        with self.assertRaises(ValueError):
            self.h.list(kind="other")
        with self.assertRaises(ValueError):
            self.h.list(state="running")

    def test_a_broken_record_does_not_stop_the_writer(self):
        bad = fake_convert(finished_at=time.time())
        bad.path = None  # Path(None) raises in the record, before it is queued
        with self.assertRaises(TypeError):
            self.h.on_change(bad)
        self.h.on_change(fake_convert(2, finished_at=time.time()))
        self.assertEqual(len(self.items()), 1)


class HistoryHttpTests(unittest.TestCase):
    """Real queues, accounts and the HTTP API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.acc = A.Accounts(self.dir / "h.db")
        self.acc.add("Ann", PW, admin=True)
        self.acc.add("Mo", PW)
        self.acc.add("Rea", PW, role="Reader")
        self.owners = JobOwners()
        self.history = History(self.dir / "history.db", owners=self.owners)
        self.cq, self.dq = build(
            jobs=1,
            image_threads=1,
            downloads=1,
            timeout=5,
            retries=0,
            owners=self.owners,
            on_change=self.history.on_change,
        )
        self.server = serve_harbor(
            0, self.cq, self.dq, Settings(), accounts=self.acc, owners=self.owners, history=self.history
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sid = {}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.history.enabled = False
        self.dq.shutdown()
        self.cq.shutdown()
        self.history.close()
        self.acc.close()
        self.tmp.cleanup()

    def req(self, who, method, path, body=None):
        h = {}
        if who is not None:
            if who not in self.sid:
                self.sid[who] = self.acc.login(who, PW)[1]
            h["Cookie"] = f"harbor_session={self.sid[who]}"
        if body is not None:
            h["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw

    def test_conversions_land_in_their_owners_history(self):
        src = self.dir / "in" / "a.zip"
        src.parent.mkdir()
        make_zip(src, comic_entries())
        status, body = self.req("Mo", "POST", "/api/add", {"paths": [str(src)], "delete_original": False})
        self.assertEqual((status, body["added"]), (200, 1))
        self.cq.wait_idle()
        self.history.flush()
        status, mine = self.req("Mo", "GET", "/api/history/list?kind=convert")
        self.assertEqual((status, mine["total"]), (200, 1))
        rec = mine["items"][0]
        self.assertEqual((rec["name"], rec["state"], rec["source"]), ("a.zip", "done", str(src)))
        self.assertGreater(rec["bytes_in"], 0)
        self.assertEqual(self.req("Rea", "GET", "/api/history/list")[1]["total"], 0)  # not theirs
        self.assertEqual(self.req("Mo", "GET", "/api/history/list?all=1")[0], 403)  # no jobs.see_all
        self.assertEqual(self.req("Ann", "GET", "/api/history/list?all=1")[1]["total"], 1)
        self.assertEqual(self.req("Ann", "GET", "/api/history/list")[1]["total"], 0)  # Ann's own
        st = self.req("Mo", "GET", "/api/state")[1]
        self.assertEqual(st["history_saved"]["conversions"], 1 if rec["bytes_out"] else 0)
        self.assertEqual(self.req("Mo", "GET", "/api/history/list?kind=x")[0], 400)

    def test_guests_get_no_history(self):
        self.assertEqual(self.req(None, "GET", "/api/history/list")[0], 401)
        st = self.req(None, "GET", "/api/state")[1]
        self.assertNotIn("history_saved", st)


if __name__ == "__main__":
    unittest.main()
