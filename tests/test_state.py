"""Tests for keeping unfinished jobs across restarts (harbor/state.py)."""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from helpers import FileServer, comic_entries, make_zip

from dlqueue.download import Auth
from harbor.app import build
from harbor.perms import JobOwners
from harbor.state import QueueStore

KEY = FileServer.KEY


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_file = self.dir / "state" / "queue.json"
        self.files_server = FileServer()
        self.queues = []
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.enabled = False  # no scheduled save after the folder is gone
        for q in self.queues:
            q.shutdown()
        self.files_server.close()
        self.tmp.cleanup()

    def start(self, paused=True, owners=None, delay=None):
        store = QueueStore(self.state_file) if delay is None else QueueStore(self.state_file, delay)
        cq, dq = build(jobs=1, image_threads=1, downloads=1, timeout=10, retries=0, on_change=store.on_change)
        store.attach(cq, dq, owners)
        self.stores.append(store)
        self.queues += [dq, cq]
        cq.set_paused(paused)
        dq.set_paused(paused)
        return store, cq, dq

    def stop(self, store, cq, dq):
        """What `harbor ui` does on shutdown."""
        store.save()
        store.enabled = False
        dq.shutdown()
        cq.shutdown()

    def saved(self):
        return json.loads(self.state_file.read_text())

    def test_saves_unfinished_jobs_without_the_key(self):
        owners = JobOwners()
        store, cq, dq = self.start(owners=owners)
        make_zip(self.dir / "A.zip", comic_entries())
        with owners.adding("convert", cq, 7):  # what an add through the API does
            cq.add([self.dir / "A.zip"], 70, delete_original=False, method=2)
        with owners.adding("download", dq, 8):
            dq.add(
                [self.files_server.base + "/secure/x.zip"],
                self.dir,
                Auth("bearer", KEY),
                after={"quality": 60},
            )
        dq.add([self.files_server.base + "/secure/y.zip"], self.dir, Auth("query", KEY, "api_key"))
        store.flush()
        data = self.saved()
        self.assertEqual(
            data["convert"],
            [
                {
                    "path": str((self.dir / "A.zip").resolve()),
                    "quality": 70,
                    "delete_original": False,
                    "overwrite": False,
                    "method": 2,
                    "owner": 7,
                }
            ],
        )
        self.assertEqual([d["auth_mode"] for d in data["downloads"]], ["bearer", "query"])
        self.assertEqual([d["owner"] for d in data["downloads"]], [8, None])
        self.assertEqual(data["downloads"][0]["after"]["quality"], 60)
        self.assertNotIn(KEY, self.state_file.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(self.state_file).st_mode), 0o600)

    def test_changes_are_saved_in_batches_with_their_owner(self):
        owners = JobOwners()
        _store, cq, _dq = self.start(owners=owners, delay=2)  # long enough to see nothing saved yet
        for name in ("A", "B"):
            make_zip(self.dir / f"{name}.zip", comic_entries())
        with owners.adding("convert", cq, 7):  # the add's change is seen before its owner is set
            cq.add([self.dir / "A.zip", self.dir / "B.zip"], 70)
        self.assertFalse(self.state_file.exists())  # not saved per change
        deadline = time.monotonic() + 10
        while not self.state_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([c["owner"] for c in self.saved()["convert"]], [7, 7])

    def test_no_save_once_disabled(self):
        store, cq, _dq = self.start()
        make_zip(self.dir / "A.zip", comic_entries())
        cq.add([self.dir / "A.zip"], 70)  # schedules a save
        store.enabled = False  # what shutdown does before stopping the queues
        store.flush()
        time.sleep(store.delay + 0.3)
        self.assertFalse(self.state_file.exists())

    def test_finished_and_cancelled_jobs_are_dropped(self):
        store, _cq, dq = self.start(paused=False)
        (_job,), _ = dq.add([self.files_server.base + "/files/x/a.bin"], self.dir)
        dq.wait_idle()
        dq.set_paused(True)
        (job2,), _ = dq.add([self.files_server.base + "/files/x/b.bin"], self.dir)
        dq.cancel(job2.id)
        store.flush()
        self.assertEqual(self.saved()["downloads"], [])

    def test_restore_after_restart(self):
        store, cq, dq = self.start()
        make_zip(self.dir / "A.zip", comic_entries())
        cq.add([self.dir / "A.zip"], 70, delete_original=False, method=1)
        dq.add([self.files_server.base + "/secure/Ch.zip"], self.dir, Auth("header", KEY, "X-API-Key"))
        dq.add([self.files_server.base + "/files/x/free.zip"], self.dir, after={"convert": False})
        self.stop(store, cq, dq)

        store2, cq2, dq2 = self.start()
        notes = store2.restore(api_key=KEY)
        self.assertEqual(notes, ["restored 3 unfinished job(s) from the last run"])
        (conv,) = cq2.jobs()
        self.assertEqual((conv.quality, conv.method, conv.delete_original), (70, 1, False))
        dls = dq2.jobs()
        self.assertEqual([d.auth.mode for d in dls], ["header", "none"])
        self.assertEqual(dls[0].auth.key, KEY)
        self.assertEqual(dls[1].after["convert"], False)
        dq2.set_paused(False)
        dq2.wait_idle()
        self.assertEqual([d.state for d in dls], ["done", "done"])

    def test_restore_keeps_the_lane(self):
        from dlqueue.download import Lane

        store, cq, dq = self.start()
        dq.set_lanes([Lane("manga"), Lane("books")])
        dq.add([self.files_server.base + "/files/x/a.zip"], self.dir, lane="manga")
        dq.add([self.files_server.base + "/files/x/b.zip"], self.dir, lane="books")
        self.stop(store, cq, dq)
        self.assertEqual([d["lane"] for d in self.saved()["downloads"]], ["manga", "books"])

        store2, _cq2, dq2 = self.start()
        dq2.set_lanes([Lane("manga")])  # "books" was removed while harbor was stopped
        notes = store2.restore()
        self.assertEqual([d.lane for d in dq2.jobs()], ["manga", ""])
        self.assertTrue(
            any("download lane 'books' is gone; queued without a lane" in n for n in notes), notes
        )

    def test_running_job_comes_back_and_stale_part_is_removed(self):
        store, _cq, dq = self.start(paused=False)
        (job,), _ = dq.add([self.files_server.base + "/slow"], self.dir)
        while job.done == 0:
            time.sleep(0.05)
        store.save()  # a hard kill now would leave this file and a .part
        (self.dir / "slow.part").write_bytes(b"stale")  # simulate the .part a hard kill leaves
        store.enabled = False
        dq.shutdown()
        self.assertEqual(self.saved()["downloads"][0]["filename"], "slow")

        store2, _cq2, dq2 = self.start()
        store2.restore()
        self.assertFalse((self.dir / "slow.part").exists())
        self.assertEqual(len(dq2.jobs()), 1)

    def test_download_needing_a_key_without_env_key(self):
        store, cq, dq = self.start()
        dq.add([self.files_server.base + "/secure/x.zip"], self.dir, Auth("bearer", "typed-in-page"))
        self.stop(store, cq, dq)
        store2, _cq2, dq2 = self.start()
        notes = store2.restore(api_key="")
        self.assertEqual(dq2.jobs(), [])
        self.assertIn("isn't kept across restarts", notes[0])
        self.assertEqual(self.saved()["downloads"], [])  # saved again after restoring

    def test_missing_archive_is_a_note(self):
        store, cq, dq = self.start()
        make_zip(self.dir / "A.zip", comic_entries())
        cq.add([self.dir / "A.zip"], 80)
        self.stop(store, cq, dq)
        (self.dir / "A.zip").unlink()
        store2, cq2, _ = self.start()
        notes = store2.restore()
        self.assertEqual(cq2.jobs(), [])
        self.assertIn("not restored: conversion of", notes[0])

    def test_unreadable_file_is_moved_aside(self):
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text("{not json")
        store, _, _ = self.start()
        self.assertEqual(store.restore(), [])
        self.assertTrue(self.state_file.with_name("queue.json.bad").exists())

    def test_no_file_is_fine(self):
        store, _, _ = self.start()
        self.assertEqual(store.restore(), [])


if __name__ == "__main__":
    unittest.main()
