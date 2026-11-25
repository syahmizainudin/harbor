"""Tests for the download speed limit, quiet hours and watch folders, through harbor's settings and
a real ``harbor ui`` started as a subprocess on a free port with temp files only."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import comic_entries, make_zip

from harbor.app import build
from harbor.config import ConfigError, load_settings, watch_list

ROOT = Path(__file__).resolve().parents[1]


class SettingTests(unittest.TestCase):
    def test_values(self):
        s = load_settings(
            {},
            None,
            {
                "HARBOR_DOWNLOAD_RATE": "1.5M",
                "HARBOR_DOWNLOAD_HOURS": "22:00-06:00",
                "HARBOR_WATCH_DIRS": "/a b;/c ; ",
                "HARBOR_WATCH_POLL": "10",
            },
        )
        self.assertEqual((s.download_rate, s.download_hours, s.watch_poll), (1572864, "22:00-06:00", 10))
        self.assertEqual(watch_list(s), ["/a b", "/c"])
        self.assertFalse(s.watch_delete_original)  # whatever HARBOR_DELETE_ORIGINAL says
        for var, value in (
            ("HARBOR_DOWNLOAD_RATE", "fast"),
            ("HARBOR_DOWNLOAD_HOURS", "9-5"),
            ("HARBOR_WATCH_POLL", "1"),
        ):
            with self.subTest(var=var), self.assertRaises(ConfigError):
                load_settings({}, None, {var: value})

    def test_build_passes_them_to_the_queue(self):
        cq, dq = build(jobs=1, image_threads=1, downloads=1, rate=2048, hours="01:00-02:00")
        try:
            self.assertEqual((dq.rate, dq.hours), (2048, (60, 120)))
        finally:
            dq.shutdown()
            cq.shutdown()


class HarborUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir()
        with socket.socket() as s, socket.socket() as g:
            s.bind(("127.0.0.1", 0))
            g.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
            games_port = g.getsockname()[1]
        env_file = self.dir / "x.env"
        env_file.write_text(
            "\n".join(
                [
                    f"HARBOR_PORT={self.port}",
                    "HARBOR_ACCOUNTS=false",
                    f"HARBOR_LIBRARY_DB={self.dir / 'lib.db'}",
                    f"HARBOR_HISTORY_DB={self.dir / 'hist.db'}",
                    f"HARBOR_GAMES_DB={self.dir / 'games.db'}",
                    f"HARBOR_GAMES_PORT={games_port}",  # not 8768, which the user's harbor may hold
                    f"HARBOR_STATE_FILE={self.dir / 'q.json'}",
                    f"HARBOR_WATCH_DIRS={self.inbox}",
                    "HARBOR_WATCH_POLL=5",
                    "HARBOR_DOWNLOAD_RATE=500K",
                    "HARBOR_DOWNLOAD_HOURS=00:00-24:00",
                    "HARBOR_JOBS=1",
                    "HARBOR_THREADS=1",
                ]
            )
            + "\n"
        )
        env_file.chmod(0o600)
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(ROOT / ".deps"), str(ROOT)]),
            "XDG_CACHE_HOME": str(self.dir / "cache"),
            "HOME": str(self.dir),  # nothing may land in the real home folder
        }
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "harbor"), "ui", "--env-file", str(env_file)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.output = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.output.append(line)
            if "harbor console:" in line:
                return
        self.fail("harbor didn't start:\n" + "".join(self.output))

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.stdout.close()
        self.tmp.cleanup()

    def state(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/state")
        body = json.loads(conn.getresponse().read())
        conn.close()
        return body

    def test_watch_folder_and_limits(self):
        self.assertTrue(
            any("watching" in line and "originals kept" in line for line in self.output), self.output
        )
        st = self.state()
        self.assertEqual(
            (st["download_rate"], st["download_hours"], st["download_waiting"]),
            (512000, "00:00-24:00", False),
        )
        self.assertEqual(st["harbor"]["watching"], 1)
        src = self.inbox / "dropped.zip"
        make_zip(src, comic_entries())
        deadline = time.monotonic() + 40  # two polls 5 s apart, then the conversion
        jobs = []
        while time.monotonic() < deadline:
            jobs = self.state()["jobs"]
            if jobs and jobs[0]["state"] == "done":
                break
            time.sleep(0.5)
        self.assertEqual(
            [(j["name"], j["state"], j["delete_original"]) for j in jobs], [("dropped.zip", "done", False)]
        )
        self.assertTrue(src.exists())  # kept
        self.assertTrue((self.inbox / "dropped.cbz").exists())
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(20), 0)


if __name__ == "__main__":
    unittest.main()
