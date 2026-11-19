"""``harbor backup`` and ``harbor doctor``.

``backup`` copies harbor's databases with SQLite's online backup API (safe while harbor runs) and
the saved-jobs file, into a new timestamped folder. It only reads the live files, and only ever
deletes its own older backup folders (named like ``20261119-021800``) beyond ``keep``.

``doctor`` only reads: it opens the databases read-only (so it never runs a migration), and
checks settings, file modes, dependency versions, library folders and the port.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import socket
import sqlite3
import stat
import time
from pathlib import Path

from .config import Settings, xdg_path

BACKUP_NAME = re.compile(r"^\d{8}-\d{6}$")
PINNED = {"cbz-webp": "cbzwebp", "dl-queue": "dlqueue", "cbz-shelf": "cbzshelf"}


def data_files(settings: Settings) -> dict[str, Path]:
    """harbor's databases and saved-jobs file, by label, as the settings place them."""
    from .accounts import default_accounts_db
    from .app import default_library_db
    from .history import default_history_db
    from .state import default_state_file

    def path(setting: str, default: Path) -> Path:
        return Path(setting).expanduser() if setting else default

    return {
        "accounts": path(settings.accounts_db, default_accounts_db()),
        "library": path(settings.library_db, default_library_db()),
        "history": path(settings.history_db, default_history_db()),
        "saved jobs": path(settings.state_file, default_state_file()),
    }


def default_backup_dir() -> Path:
    return xdg_path("XDG_DATA_HOME", "harbor", "backups")


def backup(settings: Settings, dest: Path | None = None, keep: int = 7) -> tuple[Path, list[str]]:
    """Copy the files into ``dest/<timestamp>/``. Returns the folder and what was copied."""
    root = Path(dest).expanduser() if dest else default_backup_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = root / time.strftime("%Y%m%d-%H%M%S")
    while folder.exists():  # two backups in one second
        time.sleep(1)
        folder = root / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(mode=0o700)
    done = []
    for label, src in data_files(settings).items():
        if not src.is_file():
            continue
        target = folder / src.name
        if src.suffix == ".db":
            with _ro(src) as s, sqlite3.connect(target) as t:
                s.backup(t)
            s.close()
            t.close()
        else:
            shutil.copyfile(src, target)
        target.chmod(0o600)
        done.append(f"{label}: {src} → {target}")
    if keep > 0:
        mine = sorted(p for p in root.iterdir() if p.is_dir() and BACKUP_NAME.match(p.name))
        for old in mine[:-keep]:
            shutil.rmtree(old)
    return folder, done


# ---------------------------------------------------------------- doctor
OK, WARN, FAIL = "ok", "warn", "FAIL"


def _mode_note(path: Path) -> str | None:
    """A warning when a private file can be read by other users."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    return f"{path} can be read by other users (chmod 600 {path})" if mode & 0o077 else None


def _pins() -> dict[str, str]:
    """Dependency name -> pinned version, from the installed harbor or this checkout."""
    reqs: list[str] = []
    try:
        from importlib.metadata import requires

        reqs = requires("harbor") or []
    except Exception:  # noqa: BLE001 - not installed: read the checkout's pyproject.toml
        pass
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pyproject.is_file():  # a checkout: its own pins are the ones that count
        reqs = re.findall(r'"([a-z-]+\s*@\s*git\+[^"]+)"', pyproject.read_text(encoding="utf-8"))
    pins = {}
    for r in reqs:
        m = re.match(r"([a-z-]+)\s*@\s*git\+\S+@v([\w.]+)", r)
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


def _ro(path: Path) -> sqlite3.Connection:
    """A read-only connection: never creates, writes or migrates the file."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _db_version(path: Path) -> int:
    with _ro(path) as c:
        v = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    return v


def doctor(settings: Settings) -> list[tuple[str, str]]:
    """(level, message) for each check."""
    import importlib

    from . import accounts as A
    from .app import missing_dependencies

    out: list[tuple[str, str]] = []
    add = out.append
    if settings.source:
        add((OK, f"settings from {settings.source}"))
        note = _mode_note(Path(settings.source))
        if note and settings.api_key:
            add((WARN, f"{note}: it holds the API key"))
        elif note:
            add((WARN, note))
    else:
        add((OK, "no .env file; using the defaults"))

    pins = _pins()
    for name, module in PINNED.items():
        try:
            have = importlib.import_module(module).__version__
        except ImportError:
            add((FAIL, f"{name} is not installed (pip install --target .deps .)"))
            continue
        want = pins.get(name)
        if want and have != want:
            add((FAIL, f"{name} {have} is installed, harbor pins {want} (pip install --target .deps .)"))
        else:
            add((OK, f"{name} {have}"))
    for m in missing_dependencies():
        add((FAIL, f"the installed dependencies lack {m}"))

    files = data_files(settings)
    expected = {"accounts": len(A.MIGRATIONS)}
    try:
        from cbzshelf import library as L

        expected["library"] = len(L.MIGRATIONS)
    except ImportError:  # already reported above; the library's schema can't be compared
        pass
    for label in ("accounts", "library", "history"):
        path = files[label]
        if label == "accounts" and not settings.accounts:
            continue
        if not path.is_file():
            add((OK, f"{label} database not created yet ({path})"))
            continue
        try:
            version = _db_version(path)
        except sqlite3.Error as e:
            add((FAIL, f"{label} database {path} can't be read: {e}"))
            continue
        want = expected.get(label)
        if want is not None and version > want:
            add(
                (
                    FAIL,
                    f"{label} database {path} is newer than this harbor (schema {version}, expects {want})",
                )
            )
        elif want is not None and version < want:
            add((OK, f"{label} database {path} is upgraded at the next start (schema {version} → {want})"))
        else:
            add((OK, f"{label} database {path}"))
        if note := _mode_note(path):
            add((WARN, note))

    lib = files["library"]
    if lib.is_file():
        try:
            with _ro(lib) as c:
                roots = [r[0] for r in c.execute("SELECT path FROM roots ORDER BY path")]
            c.close()
            for r in roots:
                add(
                    (OK, f"library folder {r}")
                    if Path(r).is_dir()
                    else (WARN, f"library folder {r} is missing")
                )
        except sqlite3.Error as e:
            add((WARN, f"couldn't read the library folders: {e}"))

    acc = files["accounts"]
    if settings.accounts and acc.is_file():
        try:
            with _ro(acc) as c:
                admins = c.execute(
                    "SELECT count(*) FROM users WHERE disabled = 0 AND (is_admin = 1 OR role_id = 1)"
                ).fetchone()[0]
            c.close()
            if not admins:
                add((WARN, "no admin who can sign in: harbor user add NAME --admin"))
        except sqlite3.Error:
            pass
    elif settings.accounts:
        add((WARN, "no accounts yet: harbor user add NAME --admin"))
    else:
        add((WARN, "accounts are off (HARBOR_ACCOUNTS=false): anyone who can reach the page can use it"))

    state = files["saved jobs"]
    if state.is_file():
        try:
            json.loads(state.read_text(encoding="utf-8"))
            add((OK, f"saved jobs {state}"))
        except (OSError, ValueError) as e:
            add((WARN, f"saved jobs {state} won't load ({e}); harbor moves it to .bad at start"))

    from .config import check_reach, host_list

    for problem in check_reach(settings):
        add((FAIL, problem))
    if settings.host not in ("127.0.0.1", "localhost", "::1") or settings.trust_proxy:
        how = "a reverse proxy" if settings.trust_proxy else f"HTTPS on {settings.host}"
        add((OK, f"reachable from other devices as {', '.join(host_list(settings))}, through {how}"))
    if settings.tls_cert and Path(settings.tls_cert).expanduser().is_file():
        add(_cert_check(Path(settings.tls_cert).expanduser()))
    if settings.tls_cert and not settings.local_port:
        add(
            (
                WARN,
                "the Chrome extension can't reach harbor over HTTPS: set HARBOR_LOCAL_PORT for it",
            )
        )
    if settings.local_port and settings.tls_cert:
        add(_port_check(settings.local_port))
    add(_port_check(settings.port, https=bool(settings.tls_cert)))
    return out


def _cert_check(path: Path) -> tuple[str, str]:
    """When the certificate expires (read with the ssl module's own decoder)."""
    import ssl

    try:
        info = ssl._ssl._test_decode_cert(str(path))  # the stdlib's only certificate reader
        left = (ssl.cert_time_to_seconds(info["notAfter"]) - time.time()) / 86400
    except (OSError, ValueError, KeyError, AttributeError) as e:
        return WARN, f"couldn't read the certificate {path}: {e}"
    if left < 0:
        return FAIL, f"the certificate {path} expired {int(-left)} day(s) ago"
    if left < 14:
        return WARN, f"the certificate {path} expires in {int(left)} day(s)"
    return OK, f"certificate {path}, valid for {int(left)} more day(s)"


def _port_check(port: int, https: bool = False) -> tuple[str, str]:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return OK, f"port {port} is free"
    try:
        if https:  # only asking who answers: the certificate is checked above, not here
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=2, context=ctx)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/account/status", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.getheader("Server", "").startswith("local-console"):
            return OK, f"harbor is running on port {port}"
    except (OSError, http.client.HTTPException):
        pass
    return WARN, f"something else is listening on port {port}"
