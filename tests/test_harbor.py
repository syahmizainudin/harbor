"""Tests for what harbor adds: the download -> convert hand-off, the combined page, and that
the code cbz-webp and dl-queue share by copy hasn't drifted apart."""

import contextlib
import http.client
import inspect
import io
import json
import tempfile
import threading
import unittest
import zipfile
from unittest import mock
from pathlib import Path
from urllib.parse import quote

import cbzwebp.core
import cbzwebp.jobqueue
import cbzwebp.web
import dlqueue.basequeue
import dlqueue.paths
import dlqueue.web
from helpers import FileServer, comic_entries, make_zip

from cbzwebp.jobqueue import DONE, FAILED, JobQueue
from dlqueue.download import Auth
from harbor import __version__, cli
from harbor.app import build, check_after, make_on_saved, missing_dependencies, serve_harbor
from harbor.config import Settings

KEY = FileServer.KEY


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        src = make_zip(self.dir / "src.zip", comic_entries())
        self.files_server = FileServer({"Chapter 01.zip": src.read_bytes()})
        src.unlink()

    def tearDown(self):
        self.files_server.close()
        self.tmp.cleanup()

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def url(self, path):
        return self.files_server.base + path


class HandOffTests(Base):
    def setUp(self):
        super().setUp()
        self.cq, self.dq = build(jobs=1, image_threads=2, downloads=2, timeout=10)

    def tearDown(self):
        self.dq.shutdown()
        self.cq.shutdown()
        super().tearDown()

    def wait(self):
        self.dq.wait_idle()
        self.cq.wait_idle()

    def test_archive_is_converted_and_linked(self):
        (job,), _ = self.dq.add(
            [self.url("/secure/Chapter%2001.zip")],
            self.dir,
            Auth("bearer", KEY),
            after={"quality": 60, "method": 1},
        )
        self.wait()
        (conv,) = self.cq.jobs()
        self.assertEqual(
            (job.state, job.linked_id, job.message), (DONE, conv.id, "saved; queued for conversion")
        )
        self.assertEqual((conv.state, conv.quality, conv.method, conv.delete_original), (DONE, 60, 1, True))
        self.assertEqual(self.files(), ["Chapter 01.cbz"])
        with zipfile.ZipFile(self.dir / "Chapter 01.cbz") as z:
            self.assertIn("001.webp", z.namelist())

    def test_keep_downloaded_zip(self):
        self.dq.add([self.url("/files/x/Chapter%2001.zip")], self.dir, after={"delete_original": False})
        self.wait()
        self.assertEqual(self.files(), ["Chapter 01.cbz", "Chapter 01.zip"])

    def test_convert_off(self):
        (job,), _ = self.dq.add([self.url("/files/x/Chapter%2001.zip")], self.dir, after={"convert": False})
        self.wait()
        self.assertIsNone(job.linked_id)
        self.assertEqual(self.cq.jobs(), [])
        self.assertEqual(self.files(), ["Chapter 01.zip"])

    def test_non_archive_only_saved(self):
        (job,), _ = self.dq.add([self.url("/files/x/notes.txt")], self.dir)
        self.wait()
        self.assertEqual((job.state, job.message, job.linked_id), (DONE, "saved", None))
        self.assertEqual(self.cq.jobs(), [])

    def test_bad_archive_fails_conversion_keeps_download(self):
        self.dq.add([self.url("/files/x/broken.zip")], self.dir)  # body is not a zip
        self.wait()
        (conv,) = self.cq.jobs()
        self.assertEqual(conv.state, FAILED)
        self.assertEqual(self.files(), ["broken.zip"])

    def test_check_after(self):
        self.assertEqual(
            check_after({}), {"convert": True, "quality": 80, "delete_original": True, "method": 4}
        )
        with self.assertRaises(ValueError):
            check_after({"method": 9})
        self.assertEqual(
            check_after({"convert": 0, "quality": "55", "delete_original": False}),
            {"convert": False, "quality": 55, "delete_original": False, "method": 4},
        )
        for bad in ({"quality": 0}, {"quality": 101}, {"quality": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_after(bad)
        with self.assertRaises(ValueError):
            self.dq.add([self.url("/files/x/a.zip")], self.dir, after={"quality": 500})

    def test_on_saved_skips_when_converter_already_has_it(self):
        cq = JobQueue(workers=1, image_threads=1)  # not started: the job stays queued
        hook = make_on_saved(cq)
        path = make_zip(self.dir / "a.zip", comic_entries())
        cq.add([path], 80)
        dl = type("Dl", (), {"after": check_after({}), "linked_id": None, "message": "saved"})()
        hook(dl, path)
        self.assertIsNone(dl.linked_id)  # duplicate in the convert queue: nothing new linked
        self.assertEqual(dl.message, "saved")


class ConsoleTests(Base):
    def setUp(self):
        super().setUp()
        self.cq, self.dq = build(jobs=1, image_threads=2, downloads=1, timeout=10)
        self.server = serve_harbor(0, self.cq, self.dq, Settings(quality=80))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dq.shutdown()
        self.cq.shutdown()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Content-Type": "application/json"} if body is not None else {}
        hdrs.update(headers or {})
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw

    def state(self):
        return self.request("GET", "/api/state")[1]

    def test_page_and_merged_state(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Harbor</title>", page)
        self.assertEqual(
            set(self.state()),
            {
                "jobs",
                "paused",
                "workers",
                "image_threads",
                "defaults",
                "downloads",
                "downloads_paused",
                "download_workers",
                "download_rate",
                "download_hours",
                "download_waiting",
                "download_lanes",
                "harbor",
            },
        )  # the page's JS reads exactly these keys

    def test_download_then_convert_through_the_api(self):
        body = {
            "urls": [self.url("/secure/Chapter%2001.zip")],
            "dest": str(self.dir),
            "auth": {"mode": "query", "key": KEY, "name": "api_key"},
            "after": {"convert": True, "quality": 65, "delete_original": True},
        }
        self.assertEqual(
            self.request("POST", "/api/download/add", body), (200, {"added": 1, "duplicates": 0})
        )
        self.dq.wait_idle()
        self.cq.wait_idle()
        st = self.state()
        self.assertNotIn(KEY, json.dumps(st))
        (dl,) = st["downloads"]
        (conv,) = st["jobs"]
        self.assertEqual((dl["state"], dl["linked_id"]), (DONE, conv["id"]))
        self.assertEqual((conv["state"], conv["quality"]), (DONE, 65))
        self.assertEqual(self.files(), ["Chapter 01.cbz"])

    def test_convert_endpoints_still_work(self):
        make_zip(self.dir / "A.zip", comic_entries())
        self.assertEqual(self.request("POST", "/api/add", {"paths": [str(self.dir)]})[0], 200)
        self.cq.wait_idle()
        self.assertEqual(self.state()["jobs"][0]["state"], DONE)

    def test_bad_after_is_400(self):
        body = {"urls": [self.url("/files/x/a.zip")], "dest": str(self.dir), "after": {"quality": 0}}
        self.assertEqual(self.request("POST", "/api/download/add", body)[0], 400)

    def test_picker_counts_archives(self):
        (self.dir / "a.zip").write_text("x")
        (self.dir / "b.txt").write_text("x")
        status, r = self.request("GET", "/api/dirs?path=" + quote(str(self.dir)))
        self.assertEqual((status, r["files"]), (200, 1))

    def test_guards(self):
        self.assertEqual(self.request("POST", "/api/clear", {}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                return cli.main(list(argv)), out.getvalue(), err.getvalue()
            except SystemExit as e:
                return e.code, out.getvalue(), err.getvalue()

    def test_version_and_bad_arguments(self):
        code, out, _ = self.run_cli("--version")
        self.assertEqual((code, __version__ in out), (0, True))
        self.assertEqual(self.run_cli("ui", "-q", "0")[0], 2)
        self.assertEqual(self.run_cli("ui", "-d", "0")[0], 2)
        self.assertEqual(self.run_cli()[0], 2)

    def test_too_old_dependencies_stop_startup(self):
        self.assertEqual(missing_dependencies(), [])  # the pinned versions have everything
        with mock.patch.object(cli, "missing_dependencies", return_value=["cbz-shelf LibraryAPI(can=...)"]):
            code, _, err = self.run_cli("ui")
        self.assertEqual(code, 2)
        self.assertIn("Reinstall", err)


class SharedCodeInStepTests(unittest.TestCase):
    """cbz-webp and dl-queue carry copies of the same code; a drift fails here first."""

    @staticmethod
    def generic_part(module):
        src = Path(module.__file__).read_text()
        start = src.index(
            "# ---------------------------------------------------------------- generic server part"
        )
        end = src.index(
            "# ---------------------------------------------------------------- end of generic server part"
        )
        return src[start:end]

    def test_base_queue(self):
        self.assertEqual(
            inspect.getsource(cbzwebp.jobqueue.BaseQueue), inspect.getsource(dlqueue.basequeue.BaseQueue)
        )
        self.assertEqual(cbzwebp.jobqueue.ACTIVE, dlqueue.basequeue.ACTIVE)
        self.assertEqual(cbzwebp.jobqueue.FINISHED, dlqueue.basequeue.FINISHED)

    def test_generic_server_part(self):
        self.assertEqual(self.generic_part(cbzwebp.web), self.generic_part(dlqueue.web))

    def test_normalize_path(self):
        self.assertEqual(
            inspect.getsource(cbzwebp.core.normalize_path), inspect.getsource(dlqueue.paths.normalize_path)
        )


if __name__ == "__main__":
    unittest.main()
