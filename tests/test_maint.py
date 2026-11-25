"""Tests for ``harbor backup`` and ``harbor doctor``. Every path is a temp one."""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cbzshelf.library import Library

from harbor import accounts as A
from harbor import cli, maint
from harbor.config import Settings
from harbor.history import History

PW = "correct horse"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = Settings(
            accounts_db=str(self.dir / "harbor.db"),
            library_db=str(self.dir / "library.db"),
            history_db=str(self.dir / "history.db"),
            games_db=str(self.dir / "games.db"),  # never the real ~/.local/share/harbor/games.db
            state_file=str(self.dir / "queue.json"),
            port=1,  # nothing listens there
        )

    def tearDown(self):
        self.tmp.cleanup()

    def make_files(self):
        acc = A.Accounts(self.dir / "harbor.db")
        acc.add("Ann", PW, admin=True)
        acc.close()
        (self.dir / "Comics").mkdir()
        lib = Library(self.dir / "library.db", self.dir / "thumbs")
        lib.add_root(str(self.dir / "Comics"))
        lib.close()
        History(self.dir / "history.db").close()
        (self.dir / "queue.json").write_text('{"convert": [], "download": []}')


class BackupTests(Base):
    def test_backup_copies_every_file_and_keeps_the_newest(self):
        self.make_files()
        dest = self.dir / "backups"
        stranger = dest / "keep me"
        stranger.mkdir(parents=True)
        made = []
        for n in range(3):
            with mock.patch("harbor.maint.time.strftime", return_value=f"20261119-00000{n}"):
                folder, done = maint.backup(self.settings, dest, keep=2)
            made.append(folder)
            self.assertEqual(len(done), 4)
        self.assertEqual(
            sorted(p.name for p in dest.iterdir()), ["20261119-000001", "20261119-000002", "keep me"]
        )  # only its own folders are pruned
        last = made[-1]
        self.assertEqual(
            sorted(p.name for p in last.iterdir()), ["harbor.db", "history.db", "library.db", "queue.json"]
        )
        for p in last.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600, p)
        with sqlite3.connect(last / "harbor.db") as c:
            self.assertEqual(c.execute("SELECT name FROM users").fetchall(), [("Ann",)])
        acc = A.Accounts(last / "harbor.db")  # a working copy
        self.addCleanup(acc.close)
        self.assertEqual(acc.login("Ann", PW)[0]["name"], "Ann")

    def test_secret_key_only_with_secrets(self):
        self.make_files()
        (self.dir / "secret.key").write_bytes(b"k" * 44)
        folder, _ = maint.backup(self.settings, self.dir / "b1")
        self.assertFalse((folder / "secret.key").exists())
        folder, done = maint.backup(self.settings, self.dir / "b2", with_secrets=True)
        self.assertEqual(oct((folder / "secret.key").stat().st_mode & 0o777), "0o600")
        self.assertTrue(any(x.startswith("site key secret:") for x in done))

    def test_backup_with_nothing_yet(self):
        folder, done = maint.backup(self.settings, self.dir / "b")
        self.assertEqual((done, folder.is_dir()), ([], True))
        self.assertFalse((self.dir / "harbor.db").exists())  # never created by a backup


class DoctorTests(Base):
    def levels(self):
        return {msg: level for level, msg in maint.doctor(self.settings)}

    def test_healthy(self):
        self.make_files()
        os.chmod(self.dir / "library.db", 0o600)
        out = maint.doctor(self.settings)
        self.assertEqual([m for lvl, m in out if lvl != maint.OK], [], out)

    def test_problems_are_named_and_nothing_is_changed(self):
        self.make_files()
        os.chmod(self.dir / "library.db", 0o644)
        (self.dir / "Comics").rmdir()
        (self.dir / "queue.json").write_text("{not json")
        with sqlite3.connect(self.dir / "harbor.db") as c:
            c.execute("PRAGMA user_version = 99")
        c.close()
        before = (self.dir / "library.db").read_bytes()
        out = maint.doctor(self.settings)
        text = "\n".join(f"{lvl} {m}" for lvl, m in out)
        self.assertIn("FAIL accounts database", text)  # newer than this harbor
        self.assertIn("can be read by other users", text)
        self.assertIn(f"warn library folder {self.dir / 'Comics'} is missing", text)
        self.assertIn("won't load", text)
        self.assertEqual((self.dir / "library.db").read_bytes(), before)
        self.assertEqual(
            sqlite3.connect(self.dir / "harbor.db").execute("PRAGMA user_version").fetchone()[0], 99
        )

    def test_older_database_and_wrong_dependency(self):
        self.make_files()
        with sqlite3.connect(self.dir / "library.db") as c:
            c.execute("PRAGMA user_version = 4")
        c.close()
        with mock.patch("harbor.maint._pins", return_value={"cbz-webp": "9.9.9"}):
            text = "\n".join(f"{lvl} {m}" for lvl, m in maint.doctor(self.settings))
        self.assertIn("is upgraded at the next start (schema 4", text)
        self.assertIn("FAIL cbz-webp", text)
        self.assertIn("harbor pins 9.9.9", text)

    def test_secret_key_mode(self):
        self.make_files()
        key = self.dir / "secret.key"
        key.write_bytes(b"k" * 44)
        key.chmod(0o644)
        report = maint.doctor(self.settings)
        self.assertIn(("warn", f"{key} can be read by other users (mode 644): chmod 600 {key}"), report)
        key.chmod(0o600)
        self.assertIn(("ok", f"site key secret {key}"), maint.doctor(self.settings))

    def test_no_admin_and_accounts_off(self):
        text = "\n".join(m for _, m in maint.doctor(self.settings))
        self.assertIn("no accounts yet", text)
        self.settings.accounts = False
        text = "\n".join(m for _, m in maint.doctor(self.settings))
        self.assertIn("accounts are off", text)

    def test_pins_come_from_the_checkout(self):
        pins = maint._pins()
        self.assertEqual(set(pins), {"cbz-webp", "dl-queue", "cbz-shelf", "rpgm-shelf"})

    def test_pins_from_installed_metadata(self):
        reqs = [
            "cbz-webp@ git+https://github.com/x/cbz-webp@v1.2.3",
            "dl-queue @ git+https://x/dl-queue@v0.4.2",
        ]
        with (
            mock.patch("importlib.metadata.requires", return_value=reqs),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            self.assertEqual(maint._pins(), {"cbz-webp": "1.2.3", "dl-queue": "0.4.2"})

    def test_odd_characters_in_a_database_path(self):
        self.make_files()
        odd = self.dir / "a?b#c%d"
        odd.mkdir()
        (self.dir / "library.db").rename(odd / "library.db")
        self.settings.library_db = str(odd / "library.db")
        text = "\n".join(f"{lvl} {m}" for lvl, m in maint.doctor(self.settings))
        self.assertIn(f"ok library database {odd / 'library.db'}", text)
        folder, _ = maint.backup(self.settings, self.dir / "bk")
        self.assertTrue((folder / "library.db").is_file())


class CliTests(Base):
    def run_cli(self, *argv):
        env = self.dir / "x.env"
        env.write_text(
            "".join(
                f"{var}={value}\n"
                for var, value in (
                    ("HARBOR_ACCOUNTS_DB", self.settings.accounts_db),
                    ("HARBOR_LIBRARY_DB", self.settings.library_db),
                    ("HARBOR_HISTORY_DB", self.settings.history_db),
                    ("HARBOR_GAMES_DB", self.settings.games_db),
                    ("HARBOR_STATE_FILE", self.settings.state_file),
                    ("HARBOR_PORT", "1"),
                )
            )
        )
        env.chmod(0o600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main([argv[0], "--env-file", str(env), *argv[1:]])
        return code, out.getvalue()

    def test_commands(self):
        self.make_files()
        os.chmod(self.dir / "library.db", 0o600)
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("No problems found", out)
        code, out = self.run_cli("backup", "--to", str(self.dir / "bk"), "--keep", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("Backed up 4 file(s)", out)
        self.assertEqual(self.run_cli("backup", "--keep", "-1")[0], 2)
        (self.dir / "queue.json").write_text("x")
        with sqlite3.connect(self.dir / "library.db") as c:
            c.execute("PRAGMA user_version = 99")
        c.close()
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("1 problem(s) found", out)


if __name__ == "__main__":
    unittest.main()
