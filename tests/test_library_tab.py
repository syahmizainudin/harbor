"""Tests for harbor's Library tab: cbz-shelf mounted in the console, and conversions landing in it."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cbzshelf.library import Library
from helpers import FileServer, comic_entries, make_zip

from harbor.app import build, make_library_file_hook, make_library_hook, serve_harbor
from harbor.config import Settings, load_settings


class LibraryTabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.comics = self.dir / "Comics"
        (self.comics / "Series A").mkdir(parents=True)
        self.lib = Library(self.dir / "lib.db", self.dir / "thumbs")
        self.lib.add_root(str(self.comics))
        self.lib.wait_scan(30)
        self.cq, self.dq = build(
            jobs=1,
            image_threads=2,
            downloads=1,
            timeout=10,
            retries=0,
            on_convert_change=make_library_hook(self.lib),
            on_download_kept=make_library_file_hook(self.lib),
        )
        self.server = serve_harbor(0, self.cq, self.dq, Settings(), library=self.lib)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        self.lib.close()
        self.tmp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        body, headers = resp.read(), dict(resp.getheaders())
        conn.close()
        return resp.status, headers, body

    def books(self, expect=0, timeout=10):
        """The library's books; with ``expect``, wait for the post-conversion hook to add them.

        The queue is idle as soon as a job is marked done; its change hook runs just after."""
        deadline = time.monotonic() + timeout
        while True:
            items = self.lib.search(limit=50)["items"]
            if len(items) >= expect or time.monotonic() > deadline:
                return items
            time.sleep(0.05)

    def test_library_page_and_api_are_mounted(self):
        status, headers, body = self.get("/library")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Shelf</title>", body)
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")  # harbor's tab frames it
        status, _, body = self.get("/")
        self.assertIn(b'id="libFrame"', body)
        self.assertIn(b'"shelf-reader"', body)  # hides the sidebar while the library's reader is open
        state = json.loads(self.get("/api/state")[2])
        self.assertEqual(state["library"]["roots"], 1)
        self.assertEqual(json.loads(self.get("/api/library/series")[2])["total"], 0)

    def test_finished_conversion_in_a_library_folder_is_added(self):
        make_zip(self.comics / "Series A" / "Series A v01.zip", comic_entries())
        self.cq.add([self.comics / "Series A"], 80)
        self.cq.wait_idle()
        files = [b["file"] for b in self.books(expect=1)]
        self.assertEqual(files, ["Series A v01.cbz"])  # the .cbz, not the deleted .zip

    def test_conversion_outside_the_library_is_ignored(self):
        outside = self.dir / "Elsewhere"
        outside.mkdir()
        make_zip(outside / "x.zip", comic_entries())
        self.cq.add([outside], 80)
        self.cq.wait_idle()
        time.sleep(0.3)  # give the change hook time to (not) act
        self.assertEqual(self.books(), [])

    def test_download_into_library_ends_up_as_a_book(self):
        src = make_zip(self.dir / "src.zip", comic_entries())
        files = FileServer({"Chapter 01.zip": src.read_bytes()})
        self.addCleanup(files.close)
        self.dq.add([files.base + "/files/x/Chapter%2001.zip"], self.comics / "Series A")
        self.dq.wait_idle()
        self.cq.wait_idle()
        self.assertEqual([b["file"] for b in self.books(expect=1)], ["Chapter 01.cbz"])

    def test_kept_download_into_library_is_added(self):
        src = make_zip(self.dir / "src.zip", comic_entries())
        files = FileServer({"Kept.cbz": src.read_bytes()})
        self.addCleanup(files.close)
        # Conversion off: the file is kept as downloaded, and still lands in the library.
        self.dq.add([files.base + "/files/x/Kept.cbz"], self.comics / "Series A", after={"convert": False})
        self.dq.wait_idle()
        self.assertEqual([b["file"] for b in self.books(expect=1)], ["Kept.cbz"])
        self.assertEqual(self.cq.snapshot(), [])  # nothing was converted

    def test_library_settings(self):
        env = self.dir / "x.env"
        env.write_text("HARBOR_LIBRARY_DB=/tmp/h.db\nHARBOR_LIBRARY_SCAN=no\n")
        s = load_settings(env_file=str(env), environ={})
        self.assertEqual((s.library_db, s.library_scan), ("/tmp/h.db", False))


if __name__ == "__main__":
    unittest.main()
