"""Accounts: users with passwords, browser sessions, and tokens for the Chrome extension.

Everything is in harbor's own SQLite database (``HARBOR_ACCOUNTS_DB``, default
``~/.local/share/harbor/harbor.db``). Rules (see AGENTS.md):

  * Passwords are stored only as scrypt hashes with a per-user salt.
  * Session ids and extension tokens are random; only their SHA-256 is stored, so a copy of the
    database can't be used to sign in.
  * The session cookie is ``HttpOnly; SameSite=Strict`` (``Secure`` too when harbor is served over
    HTTPS). POSTs keep the console's Origin and JSON-only checks on top.
  * Repeated wrong passwords for a name are slowed down; the answer never says whether a name exists.
  * The first account is made from the command line (``harbor user add NAME --admin``); there is no
    sign-up page, so the console is never open to whoever gets there first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from .config import xdg_path

COOKIE = "harbor_session"
TOKEN_HEADER = "X-Harbor-Token"
TOKEN_PREFIX = "hbt_"
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
LOCK_AFTER = 5  # wrong passwords for one name within LOCK_WINDOW ...
LOCK_WINDOW = 15 * 60  # ... locks that name for the rest of the window
TOUCH_EVERY = 300  # seconds between session "last seen" writes

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pass_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    created_at REAL NOT NULL, last_used REAL);
"""
# Later schema changes go here, one step each, run once in order (PRAGMA user_version counts them).
# Statements are split on semicolons, so none may appear inside one.
MIGRATIONS: list[str] = [
    # 1: roles. Admin (id 1) is locked and always has every permission. Accounts made with
    # --admin become Admin, the others Member.
    """CREATE TABLE roles (
           id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
           perms_json TEXT NOT NULL DEFAULT '[]', locked INTEGER NOT NULL DEFAULT 0);
       INSERT INTO roles(id, name, perms_json, locked) VALUES (1, 'Admin', '[]', 1);
       INSERT INTO roles(id, name, perms_json) VALUES
           (2, 'Member', '["convert", "download", "delete_original", "library.read"]');
       INSERT INTO roles(id, name, perms_json) VALUES (3, 'Reader', '["library.read"]');
       ALTER TABLE users ADD COLUMN role_id INTEGER NOT NULL DEFAULT 2;
       UPDATE users SET role_id = 1 WHERE is_admin = 1""",
    # 2: each user's own Convert and Download defaults (never the API key).
    """CREATE TABLE prefs (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                          prefs_json TEXT NOT NULL DEFAULT '{}')""",
    # 3: who may see each library (cbz-shelf's library ids). No row: every role with library.read,
    # and guests. roles_json NULL is every role too; a list names the only roles (Admin always sees all).
    """CREATE TABLE library_access (
           library_id INTEGER PRIMARY KEY, roles_json TEXT, guests INTEGER NOT NULL DEFAULT 1)""",
    # 4: the browser each session signed in from (shown in Your account), and the audit log of
    # account and admin events. ``actor`` is a name, kept when the account is removed.
    """ALTER TABLE sessions ADD COLUMN user_agent TEXT NOT NULL DEFAULT '';
       CREATE TABLE audit (
           id INTEGER PRIMARY KEY, at REAL NOT NULL, actor_id INTEGER, actor TEXT NOT NULL DEFAULT '',
           action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
       CREATE INDEX audit_at ON audit(at)""",
    # 5: the address each audit event came from (127.0.0.1 unless harbor is reachable from the LAN).
    """ALTER TABLE audit ADD COLUMN client TEXT NOT NULL DEFAULT ''""",
    # 6: what a token is for. "extension": the Chrome extension, as before. "reader": a reader app
    # (OPDS) signing in with HTTP Basic, which may only read the library.
    """ALTER TABLE tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'extension'""",
    # 7: download lanes (dl-queue's Lane): each its own downloads at once, speed limit (bytes a
    # second, 0 none) and quiet hours ("HH:MM-HH:MM", '' for the queue's). Managed on the Admin page.
    """CREATE TABLE download_lanes (
           name TEXT PRIMARY KEY COLLATE NOCASE, workers INTEGER NOT NULL DEFAULT 1,
           rate INTEGER NOT NULL DEFAULT 0, hours TEXT NOT NULL DEFAULT '',
           position INTEGER NOT NULL DEFAULT 0)""",
    # 8: which roles may use a download lane: NULL every role, or a JSON list of role ids (Admin
    # always may).
    """ALTER TABLE download_lanes ADD COLUMN roles_json TEXT""",
]
LANES_MAX = 20
TOKEN_SCOPES = {"extension": "Chrome extension", "reader": "Reader app"}
READER_PERMS = ("library.read",)  # all a reader token can do, if its account may
AUDIT_DAY = 86400
SESSION_HANDLE = 16  # hex characters of a session's id hash shown to its owner, to sign it out
_req = threading.local()  # per request thread: the browser's User-Agent, set by HarborSignIn.user


def request_agent() -> str:
    """The User-Agent of the request being handled (trimmed), or ''."""
    return getattr(_req, "agent", "")


ADMIN_ROLE, MEMBER_ROLE = 1, 2

# Every permission, in the order the admin page shows them. Admin always has all of them.
PERMISSIONS = [
    ("convert", "Convert archives"),
    ("download", "Download files"),
    ("delete_original", "Delete the original .zip after converting"),
    ("library.read", "Read the library"),
    ("library.edit", "Edit book and series details"),
    ("library.manage", "Manage library folders and scans"),
    ("jobs.see_all", "See and manage everyone's jobs"),
    ("download.lanes", "Manage download lanes"),
    ("users.manage", "Manage users"),
    ("roles.manage", "Manage roles and permissions"),
]
ALL_PERMS = frozenset(p for p, _ in PERMISSIONS)


def _text(limit: int):
    return lambda v: isinstance(v, str) and len(v) <= limit and not any(c in v for c in "\r\n\0")


def _int_in(lo: int, hi: int):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


AUTH_MODES = ("none", "bearer", "header", "query")
PRESETS_MAX = 20
# One download preset's fields: name -> check. Never the API key.
PRESET_FIELDS = {
    "name": lambda v: _text(60)(v) and bool(v.strip()),
    "download_dir": _text(4096),
    "auth_mode": lambda v: v in AUTH_MODES,
    "auth_name": _text(200),
    "convert": lambda v: isinstance(v, bool),
    "quality": _int_in(1, 100),
    "method": _int_in(0, 6),
    "delete_original": lambda v: isinstance(v, bool),
    "lane": _text(40),
}


def _presets(v) -> bool:
    """Download presets: a list of named sets of download settings, names unique (any case)."""
    if not isinstance(v, list) or len(v) > PRESETS_MAX:
        return False
    names = set()
    for p in v:
        if not isinstance(p, dict) or "name" not in p or set(p) - set(PRESET_FIELDS):
            return False
        if not all(PRESET_FIELDS[k](x) for k, x in p.items()):
            return False
        key = p["name"].strip().lower()
        if key in names:
            return False
        names.add(key)
    return True


# A user's own defaults for the Convert and Download tabs: name -> check. Never the API key.
PREFS = {
    "quality": _int_in(1, 100),
    "method": _int_in(0, 6),
    "delete_original": lambda v: isinstance(v, bool),
    "download_dir": _text(4096),
    "auth_mode": lambda v: v in AUTH_MODES,
    "auth_name": _text(200),
    "convert_downloads": lambda v: isinstance(v, bool),
    "download_presets": _presets,
}


def clean_perms(perms) -> list[str]:
    if not isinstance(perms, list) or any(p not in ALL_PERMS for p in perms):
        raise ValueError(f"permissions must be a list of: {', '.join(sorted(ALL_PERMS))}")
    return [p for p, _ in PERMISSIONS if p in perms]


def default_accounts_db() -> Path:
    return xdg_path("XDG_DATA_HOME", "harbor", "harbor.db")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        want = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(want),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # checked for unknown names, so timing doesn't tell


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= 64 or any(c in name for c in "\r\n\t\0") or name != " ".join(name.split()):
        raise ValueError("a name is 1-64 characters, without line breaks or doubled spaces")
    return name


def check_new_password(password: str) -> str:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValueError(f"a password needs at least {MIN_PASSWORD} characters")
    return password


class LockedOut(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"too many wrong passwords; try again in {max(1, seconds // 60)} minute(s)")
        self.seconds = seconds


class Accounts:
    def __init__(
        self, db_path: str | Path, session_days: int = 30, secure_cookie: bool = False, audit_days: int = 365
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_seconds = int(session_days) * 86400
        self.secure_cookie = secure_cookie
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for n, step in enumerate(MIGRATIONS[version:], start=version + 1):
                self._db.execute("BEGIN")
                for stmt in (x for x in step.split(";") if x.strip()):
                    self._db.execute(stmt)
                self._db.execute(f"PRAGMA user_version = {n}")
                self._db.execute("COMMIT")
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        self._failures: dict[str, list[float]] = {}
        self._rules: dict[int, dict] | None = None  # library_access, read once, dropped on change
        self.audit_days = audit_days
        self._audit_pruned = 0.0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # ------------------------------------------------------------ users
    USER_SQL = """SELECT u.*, r.name AS role, r.perms_json, r.locked FROM users u
                  LEFT JOIN roles r ON r.id = u.role_id"""

    @staticmethod
    def _public(r: sqlite3.Row) -> dict:
        """What may be shown about a user: never the password hash. A row from a sign-in lookup
        (with ``prefs_json``) adds the user's own defaults as ``prefs``."""
        perms = sorted(ALL_PERMS) if r["locked"] else json.loads(r["perms_json"] or "[]")
        user = {
            "id": r["id"],
            "name": r["name"],
            "admin": r["role_id"] == ADMIN_ROLE,
            "disabled": bool(r["disabled"]),
            "role_id": r["role_id"],
            "role": r["role"],
            "perms": perms,
        }
        if "prefs_json" in r.keys():
            user["prefs"] = json.loads(r["prefs_json"] or "{}")
        return user

    def count(self) -> int:
        return self._q("SELECT count(*) FROM users")[0][0]

    def users(self) -> list[dict]:
        return [self._public(r) for r in self._q(f"{self.USER_SQL} ORDER BY lower(u.name)")]

    def _row(self, name: str) -> sqlite3.Row | None:
        rows = self._q(f"{self.USER_SQL} WHERE u.name = ?", (str(name or "").strip(),))
        return rows[0] if rows else None

    def get(self, name: str) -> dict:
        row = self._row(name)
        if row is None:
            raise KeyError(f"no user {name!r}")
        return self._public(row)

    def get_id(self, user_id: int) -> dict:
        rows = self._q(f"{self.USER_SQL} WHERE u.id = ?", (int(user_id),))
        if not rows:
            raise KeyError(f"no user {user_id}")
        return self._public(rows[0])

    def add(self, name: str, password: str, admin: bool = False, role: int | str | None = None) -> dict:
        name, password = check_name(name), check_new_password(password)
        role_id = ADMIN_ROLE if admin else self.role(role)["id"] if role is not None else MEMBER_ROLE
        pass_hash = hash_password(password)  # slow on purpose: not under the lock
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO users(name, pass_hash, is_admin, role_id, created_at) VALUES (?,?,?,?,?)",
                    (name, pass_hash, int(role_id == ADMIN_ROLE), role_id, time.time()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"there is already a user called {name!r}") from None
        return self.get(name)

    def set_role(self, name: str, role: int | str) -> dict:
        target = self.role(role)
        with self._lock:
            user = self.get(name)
            if target["id"] != ADMIN_ROLE and self._would_leave_no_admin(user):
                raise ValueError("this is the only admin who can sign in; make another admin first")
            self._db.execute(
                "UPDATE users SET role_id = ?, is_admin = ? WHERE id = ?",
                (target["id"], int(target["id"] == ADMIN_ROLE), user["id"]),
            )
        return self.get(name)

    # ------------------------------------------------------------ preferences
    def prefs(self, user_id: int) -> dict:
        rows = self._q("SELECT prefs_json FROM prefs WHERE user_id = ?", (int(user_id),))
        return json.loads(rows[0]["prefs_json"]) if rows else {}

    def set_prefs(self, user_id: int, changes: dict) -> dict:
        """Change some of a user's defaults; None removes one (back to harbor's .env default)."""
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        prefs = self.prefs(user_id)
        for key, value in changes.items():
            if key not in PREFS:
                raise ValueError(f"unknown setting: {key}")
            if value is None:
                prefs.pop(key, None)
            elif not PREFS[key](value):
                raise ValueError(f"bad value for {key}")
            else:
                prefs[key] = value.strip() if isinstance(value, str) else value
        with self._lock:
            self._db.execute(
                """INSERT INTO prefs(user_id, prefs_json) VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET prefs_json = excluded.prefs_json""",
                (int(user_id), json.dumps(prefs)),
            )
        return prefs

    # ------------------------------------------------------------ roles
    def roles(self) -> list[dict]:
        rows = self._q(
            """SELECT r.*, (SELECT count(*) FROM users u WHERE u.role_id = r.id) AS members
               FROM roles r ORDER BY r.id"""
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "locked": bool(r["locked"]),
                "perms": sorted(ALL_PERMS) if r["locked"] else json.loads(r["perms_json"]),
                "members": r["members"],
            }
            for r in rows
        ]

    def role(self, role: int | str) -> dict:
        for r in self.roles():
            if r["id"] == role or (isinstance(role, str) and r["name"].lower() == role.strip().lower()):
                return r
        raise KeyError(f"no role {role!r}")

    def add_role(self, name: str, perms: list[str]) -> dict:
        name = check_name(name)
        try:
            with self._lock:
                rid = self._db.execute(
                    "INSERT INTO roles(name, perms_json) VALUES (?, ?)",
                    (name, json.dumps(clean_perms(perms))),
                ).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"there is already a role called {name!r}") from None
        return self.role(rid)

    def update_role(self, role_id: int, name: str | None = None, perms: list[str] | None = None) -> dict:
        r = self.role(int(role_id))
        if r["locked"]:
            raise ValueError("the Admin role always has every permission and can't be changed")
        try:
            with self._lock:
                if name is not None:
                    self._db.execute("UPDATE roles SET name = ? WHERE id = ?", (check_name(name), r["id"]))
                if perms is not None:
                    self._db.execute(
                        "UPDATE roles SET perms_json = ? WHERE id = ?",
                        (json.dumps(clean_perms(perms)), r["id"]),
                    )
        except sqlite3.IntegrityError:
            raise ValueError(f"there is already a role called {name!r}") from None
        return self.role(r["id"])

    def remove_role(self, role_id: int) -> None:
        r = self.role(int(role_id))
        if r["locked"]:
            raise ValueError("the Admin role can't be removed")
        if r["members"]:
            raise ValueError(f"{r['name']} still has {r['members']} member(s); give them another role first")
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM roles WHERE id = ?", (r["id"],))
                for lid, rule in self.library_rules().items():  # a removed role sees nothing anyway
                    if rule["roles"] is not None and r["id"] in rule["roles"]:
                        roles = [x for x in rule["roles"] if x != r["id"]]
                        self._db.execute(
                            "UPDATE library_access SET roles_json = ? WHERE library_id = ?",
                            (json.dumps(roles), lid),
                        )
                for row in self._db.execute(
                    "SELECT name, roles_json FROM download_lanes WHERE roles_json IS NOT NULL"
                ).fetchall():
                    lane_roles = json.loads(row["roles_json"])
                    if r["id"] in lane_roles:
                        self._db.execute(
                            "UPDATE download_lanes SET roles_json = ? WHERE name = ?",
                            (json.dumps([x for x in lane_roles if x != r["id"]]), row["name"]),
                        )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            finally:
                self._rules = None

    # ------------------------------------------------------------ library access
    # ------------------------------------------------------------ download lanes
    def lanes(self) -> list[dict]:
        """Download lanes in order: name, workers, rate (bytes a second), hours ('' for none) and
        roles (None: every role; else the role ids that may use it besides Admin)."""
        return [
            {
                "name": r["name"],
                "workers": r["workers"],
                "rate": r["rate"],
                "hours": r["hours"],
                "roles": None if r["roles_json"] is None else json.loads(r["roles_json"]),
            }
            for r in self._q("SELECT * FROM download_lanes ORDER BY position, lower(name)")
        ]

    @staticmethod
    def lane_allowed(lane: dict, user) -> bool:
        """Whether ``user`` may put downloads in ``lane`` (one of ``lanes()``)."""
        if lane["roles"] is None or (user and user.get("admin")):
            return True
        return bool(user) and user.get("role_id") in lane["roles"]

    def _role_ids(self, roles) -> list | None:
        """None (every role), or a checked, sorted list of role ids without Admin."""
        if roles is None:
            return None
        if not isinstance(roles, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in roles):
            raise ValueError("roles must be null (everyone) or a list of role ids")
        known = {r["id"] for r in self.roles()}
        if set(roles) - known:
            raise KeyError(f"no role {sorted(set(roles) - known)[0]}")
        return sorted(set(roles) - {ADMIN_ROLE})

    def set_lane(self, name: str, workers, rate: str, hours: str, roles: list | None = None) -> dict:
        """Add a lane, or change the one with this name (any case). ``rate`` like "5M" ("" or "0":
        no limit of its own), ``hours`` like "01:00-07:00" ("": the queue's), ``roles`` None for
        every role or the role ids that may use it (Admin always may). Checked with dl-queue's own
        rules."""
        from dlqueue.download import Lane, parse_hours, parse_size

        rate_n = parse_size(str(rate or "0"))
        hours = str(hours or "").strip()
        lane = Lane(str(name), workers, rate_n, parse_hours(hours))  # raises ValueError
        roles = self._role_ids(roles)
        roles_json = None if roles is None else json.dumps(roles)
        with self._lock:
            row = self._db.execute("SELECT name FROM download_lanes WHERE name = ?", (lane.name,)).fetchone()
            if row:
                self._db.execute(
                    """UPDATE download_lanes SET name = ?, workers = ?, rate = ?, hours = ?, roles_json = ?
                       WHERE name = ?""",
                    (lane.name, lane.workers, lane.rate, hours, roles_json, row["name"]),
                )
            else:
                n = self._db.execute(
                    "SELECT count(*), coalesce(max(position), 0) FROM download_lanes"
                ).fetchone()
                if n[0] >= LANES_MAX:
                    raise ValueError(f"there can be at most {LANES_MAX} download lanes")
                self._db.execute(
                    """INSERT INTO download_lanes(name, workers, rate, hours, position, roles_json)
                       VALUES (?,?,?,?,?,?)""",
                    (lane.name, lane.workers, lane.rate, hours, n[1] + 1, roles_json),
                )
        return {"name": lane.name, "workers": lane.workers, "rate": lane.rate, "hours": hours, "roles": roles}

    def remove_lane(self, name: str) -> bool:
        with self._lock:
            return bool(
                self._db.execute("DELETE FROM download_lanes WHERE name = ?", (str(name).strip(),)).rowcount
            )

    def library_rules(self) -> dict[int, dict]:
        """Library id -> ``{"roles": None or [role ids], "guests": bool}``, for libraries that
        aren't open to everyone. Kept in memory: it's read on every library request."""
        with self._lock:
            if self._rules is None:
                self._rules = {
                    r["library_id"]: {
                        "roles": None if r["roles_json"] is None else json.loads(r["roles_json"]),
                        "guests": bool(r["guests"]),
                    }
                    for r in self._db.execute("SELECT * FROM library_access").fetchall()
                }
            return self._rules

    def set_library_access(self, library_id: int, roles: list | None, guests: bool) -> dict:
        """Who may see a library: ``roles`` None for every role with library.read, or a list of
        role ids (Admin always sees it); ``guests`` whether signed-out visitors may."""
        if not isinstance(guests, bool):
            raise ValueError("guests must be true or false")
        if roles is not None:
            if not isinstance(roles, list) or any(
                isinstance(x, bool) or not isinstance(x, int) for x in roles
            ):
                raise ValueError("roles must be null (everyone) or a list of role ids")
            known = {r["id"] for r in self.roles()}
            if set(roles) - known:
                raise KeyError(f"no role {sorted(set(roles) - known)[0]}")
            roles = sorted(set(roles) - {ADMIN_ROLE})
        with self._lock:
            if roles is None and guests:
                self._db.execute("DELETE FROM library_access WHERE library_id = ?", (int(library_id),))
            else:
                self._db.execute(
                    """INSERT INTO library_access(library_id, roles_json, guests) VALUES (?, ?, ?)
                       ON CONFLICT(library_id) DO UPDATE
                       SET roles_json = excluded.roles_json, guests = excluded.guests""",
                    (int(library_id), None if roles is None else json.dumps(roles), int(guests)),
                )
            self._rules = None
        return {"id": int(library_id), "roles": roles, "guests": guests}

    def forget_libraries(self, keep: set[int]) -> None:
        """Drop the rules of libraries that no longer exist."""
        with self._lock:
            gone = [lid for lid in self.library_rules() if lid not in keep]
            for lid in gone:
                self._db.execute("DELETE FROM library_access WHERE library_id = ?", (lid,))
            if gone:
                self._rules = None

    def set_password(self, name: str, password: str, keep_session: str | None = None) -> None:
        """Change a password and end the user's other sessions (all of them unless ``keep_session``)."""
        user = self.get(name)
        pass_hash = hash_password(check_new_password(password))  # slow on purpose: not under the lock
        with self._lock:
            self._db.execute("UPDATE users SET pass_hash = ? WHERE id = ?", (pass_hash, user["id"]))
            keep = _sha(keep_session) if keep_session else ""
            self._db.execute("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?", (user["id"], keep))

    def set_disabled(self, name: str, disabled: bool) -> None:
        with self._lock:
            user = self.get(name)
            if disabled and self._would_leave_no_admin(user):
                raise ValueError("this is the only admin who can sign in; add or enable another admin first")
            self._db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user["id"]))
            if disabled:
                self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
                self._db.execute("DELETE FROM tokens WHERE user_id = ?", (user["id"],))

    def remove(self, name: str) -> None:
        with self._lock:
            user = self.get(name)
            if self._would_leave_no_admin(user):
                raise ValueError("this is the only admin who can sign in; add or enable another admin first")
            self._x("DELETE FROM users WHERE id = ?", (user["id"],))

    def _active_admins(self) -> int:
        return self._q("SELECT count(*) FROM users WHERE role_id = ? AND disabled = 0", (ADMIN_ROLE,))[0][0]

    def _would_leave_no_admin(self, user: dict) -> bool:
        """Whether taking admin away from ``user`` (a role change, disabling, removal) would leave no
        admin who can sign in. Call it under ``_lock``, with ``user`` read under the same lock."""
        return user["admin"] and not user["disabled"] and self._active_admins() <= 1

    def first_admin(self) -> dict | None:
        rows = self._q(f"{self.USER_SQL} WHERE u.role_id = ? ORDER BY u.id LIMIT 1", (ADMIN_ROLE,))
        return self._public(rows[0]) if rows else None

    # ------------------------------------------------------------ signing in
    def _check_lock(self, key: str, now: float) -> None:
        recent = [t for t in self._failures.get(key, []) if now - t < LOCK_WINDOW]
        self._failures[key] = recent
        if len(recent) >= LOCK_AFTER:
            raise LockedOut(int(LOCK_WINDOW - (now - recent[0])))

    def login(self, name: str, password: str, user_agent: str = "") -> tuple[dict, str]:
        """Check a name and password; returns (user, new session id). Raises ValueError or LockedOut."""
        key, now = str(name or "").strip().lower(), time.time()
        with self._lock:
            self._check_lock(key, now)
        row = self._row(name)
        stored = row["pass_hash"] if row is not None else _DUMMY_HASH  # same work for unknown names
        ok = verify_password(str(password or "")[:MAX_PASSWORD], stored)
        if not ok or row is None or row["disabled"]:
            with self._lock:
                self._failures.setdefault(key, []).append(now)
            raise ValueError("wrong name or password")
        with self._lock:
            self._failures.pop(key, None)
            sid = secrets.token_urlsafe(32)
            self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self._db.execute(
                "INSERT INTO sessions(id_hash, user_id, created_at, last_seen, expires_at, user_agent)"
                " VALUES (?,?,?,?,?,?)",
                (_sha(sid), row["id"], now, now, now + self.session_seconds, str(user_agent or "")[:200]),
            )
        return self._public(row), sid

    def check_password(self, user_id: int, password: str) -> bool:
        """Whether ``password`` is the user's current one (for changing it)."""
        rows = self._q("SELECT pass_hash FROM users WHERE id = ?", (int(user_id),))
        stored = rows[0]["pass_hash"] if rows else _DUMMY_HASH
        return verify_password(str(password or "")[:MAX_PASSWORD], stored) and bool(rows)

    def logout(self, sid: str) -> None:
        if sid:
            self._x("DELETE FROM sessions WHERE id_hash = ?", (_sha(sid),))

    def cookie(self, sid: str | None) -> str:
        """The Set-Cookie value for a new session, or for clearing it (``None``)."""
        attrs = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.secure_cookie else "")
        if sid is None:
            return f"{COOKIE}=; Max-Age=0; {attrs}"
        return f"{COOKIE}={sid}; Max-Age={self.session_seconds}; {attrs}"

    def session_user(self, sid: str) -> dict | None:
        if not sid:
            return None
        now = time.time()
        rows = self._q(
            """SELECT u.*, r.name AS role, r.perms_json, r.locked, p.prefs_json, s.last_seen
               FROM sessions s JOIN users u ON u.id = s.user_id LEFT JOIN roles r ON r.id = u.role_id
               LEFT JOIN prefs p ON p.user_id = u.id
               WHERE s.id_hash = ? AND s.expires_at > ? AND u.disabled = 0""",
            (_sha(sid), now),
        )
        if not rows:
            return None
        if now - rows[0]["last_seen"] > TOUCH_EVERY:  # sliding expiry, written now and then
            self._x(
                "UPDATE sessions SET last_seen = ?, expires_at = ? WHERE id_hash = ?",
                (now, now + self.session_seconds, _sha(sid)),
            )
        return self._public(rows[0])

    def sessions(self, user_id: int, current: str = "") -> list[dict]:
        """A user's signed-in browsers, newest use first. ``id`` is a short piece of the session id's
        hash, enough to sign that session out and useless for signing in."""
        cur = _sha(current)[:SESSION_HANDLE] if current else ""
        rows = self._q(
            """SELECT id_hash, created_at, last_seen, user_agent FROM sessions
               WHERE user_id = ? AND expires_at > ? ORDER BY last_seen DESC""",
            (int(user_id), time.time()),
        )
        return [
            {
                "id": r["id_hash"][:SESSION_HANDLE],
                "created_at": r["created_at"],
                "last_seen": r["last_seen"],
                "user_agent": r["user_agent"],
                "current": r["id_hash"][:SESSION_HANDLE] == cur,
            }
            for r in rows
        ]

    def revoke_session(self, user_id: int, handle: str) -> bool:
        handle = str(handle or "")
        if len(handle) != SESSION_HANDLE or any(c not in "0123456789abcdef" for c in handle):
            raise ValueError("unknown session")
        return bool(
            self._x(
                "DELETE FROM sessions WHERE user_id = ? AND substr(id_hash, 1, ?) = ?",
                (int(user_id), SESSION_HANDLE, handle),
            )
        )

    # ------------------------------------------------------------ audit log
    def audit(self, action: str, target: str = "", detail: str = "", actor=None) -> None:
        """Record an account or admin event. ``actor`` is a user dict, a label (such as "command
        line"), or None for someone not signed in. Never pass a password, session id or token."""
        if isinstance(actor, dict):
            actor_id, actor_name = actor.get("id"), actor.get("name", "")
        else:
            actor_id, actor_name = None, str(actor or "")
        try:
            from cbzwebp.web import current_client

            client = current_client()  # '' outside a request (the command line)
        except ImportError:
            client = ""
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO audit(at, actor_id, actor, action, target, detail, client)"
                " VALUES (?,?,?,?,?,?,?)",
                (now, actor_id, actor_name[:80], action, str(target)[:200], str(detail)[:500], client[:64]),
            )
            if self.audit_days and now - self._audit_pruned > AUDIT_DAY:
                self._audit_pruned = now
                self._db.execute("DELETE FROM audit WHERE at < ?", (now - self.audit_days * AUDIT_DAY,))

    def audit_log(self, limit: int = 100, offset: int = 0, action: str = "") -> dict:
        where, args = ("action = ?", [action]) if action else ("1=1", [])
        limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
        rows = self._q(
            f"SELECT * FROM audit WHERE {where} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        total = self._q(f"SELECT count(*) FROM audit WHERE {where}", args)[0][0]
        return {"items": [dict(r) for r in rows], "total": total}

    # ------------------------------------------------------------ extension tokens
    def create_token(self, user_id: int, label: str, scope: str = "extension") -> tuple[dict, str]:
        if scope not in TOKEN_SCOPES:
            raise ValueError(f"a token is for one of: {', '.join(TOKEN_SCOPES)}")
        label = (label or "").strip()[:80] or TOKEN_SCOPES[scope]
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            tid = self._db.execute(
                "INSERT INTO tokens(hash, user_id, label, created_at, scope) VALUES (?,?,?,?,?)",
                (_sha(token), int(user_id), label, time.time(), scope),
            ).lastrowid
        return {"id": tid, "label": label, "scope": scope}, token

    def tokens(self, user_id: int) -> list[dict]:
        return [
            {
                "id": r["id"],
                "label": r["label"],
                "scope": r["scope"],
                "created_at": r["created_at"],
                "last_used": r["last_used"],
            }
            for r in self._q("SELECT * FROM tokens WHERE user_id = ? ORDER BY id", (int(user_id),))
        ]

    def revoke_token(self, user_id: int, token_id: int) -> bool:
        return bool(self._x("DELETE FROM tokens WHERE id = ? AND user_id = ?", (int(token_id), int(user_id))))

    def token_user(self, token: str, scope: str | None = None, name: str | None = None) -> dict | None:
        """The user a token belongs to, with ``token_scope``. A reader token's user has at most
        ``READER_PERMS``. ``scope``/``name``: accept only a token of that scope, of that account."""
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        rows = self._q(
            """SELECT u.*, r.name AS role, r.perms_json, r.locked, p.prefs_json,
                      t.id AS tid, t.last_used, t.scope
               FROM tokens t JOIN users u ON u.id = t.user_id LEFT JOIN roles r ON r.id = u.role_id
               LEFT JOIN prefs p ON p.user_id = u.id
               WHERE t.hash = ? AND u.disabled = 0""",
            (_sha(token),),
        )
        if not rows:
            return None
        row = rows[0]
        if scope is not None and row["scope"] != scope:
            return None
        if name is not None and row["name"].lower() != name.strip().lower():
            return None
        if (row["last_used"] or 0) < time.time() - TOUCH_EVERY:
            self._x("UPDATE tokens SET last_used = ? WHERE id = ?", (time.time(), row["tid"]))
        user = {**self._public(row), "token_scope": row["scope"]}
        if row["scope"] == "reader":
            user["perms"] = [p for p in user["perms"] if p in READER_PERMS]
        return user


def session_id(headers) -> str:
    raw = headers.get("Cookie", "") if headers else ""
    try:
        jar = SimpleCookie(raw)
    except CookieError:
        return ""
    return jar[COOKIE].value if COOKIE in jar else ""


def basic_credentials(header: str) -> tuple[str, str] | None:
    """(name, secret) from an ``Authorization: Basic`` header, or None."""
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        name, sep, secret = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return (name, secret) if sep else None


class HarborSignIn:
    """The console server's ``auth``: who sent a request, and what is open to the signed-out."""

    login_path = "/login"
    PUBLIC = frozenset({("GET", "/login"), ("POST", "/api/account/login"), ("GET", "/api/account/status")})

    def __init__(self, accounts: Accounts):
        self.accounts = accounts
        self._request = threading.local()  # one thread per request: what ``user`` saw

    def public(self, method: str, path: str) -> bool:
        """The sign-in page and action, and what a guest may read (``perms.guest_may``). A request
        with a token or from the extension that isn't signed in is refused, never a guest, so the
        extension still learns it needs a token."""
        from .perms import guest_may

        if (method, path) in self.PUBLIC:
            return True
        return not getattr(self._request, "machine", False) and guest_may(method, path)

    def allows(self, user, method: str, path: str) -> bool:
        """Permissions on what the console server serves itself, and a reader token's limit: it
        may only read the library (``GET /api/library/...``), never the page, state or anything else."""
        from .perms import LIBRARY_PREFIX, may_browse_folders

        if user and user.get("token_scope") == "reader":
            return method == "GET" and path.startswith(LIBRARY_PREFIX)
        return may_browse_folders(user) if path == "/api/dirs" else True

    def user(self, headers) -> dict | None:
        _req.agent = (headers.get("User-Agent", "") if headers else "")[:200]
        token = headers.get(TOKEN_HEADER, "") if headers else ""
        origin = headers.get("Origin", "") if headers else ""
        basic = basic_credentials(headers.get("Authorization", "") if headers else "")
        self._request.machine = bool(token) or bool(basic) or origin.startswith("chrome-extension://")
        if basic:  # a reader app: account name and a reader token, never the account's password
            return self.accounts.token_user(basic[1], scope="reader", name=basic[0])
        if token:
            return self.accounts.token_user(token)  # with "prefs", from the same query
        sid = session_id(headers)
        user = self.accounts.session_user(sid)
        # The session id rides along for sign-out and password changes; AccountAPI never shows it.
        return user and {**user, "_sid": sid}


class AccountAPI:
    """Signing in and out, and the signed-in user's own account, at ``/api/account/``."""

    def __init__(self, accounts: Accounts):
        self.accounts = accounts

    @staticmethod
    def _me() -> dict | None:
        from cbzwebp.web import current_user

        return current_user()

    def state(self) -> dict:
        me = self._me()
        if not me:  # a guest: the page shows "Sign in" and the library
            return {"account": None}
        return {
            "account": {
                "name": me["name"],
                "admin": me["admin"],
                "role": me.get("role"),
                "perms": me.get("perms", []),
                "browser": bool(me.get("_sid")),
                "tokens": self.accounts.tokens(me["id"]) if me.get("_sid") else [],
                "prefs": me.get("prefs", {}),
            }
        }

    def get(self, action: str, query: dict):
        def out(status, obj):
            return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()

        me = self._me()
        if action == "status":  # public: what the sign-in page needs to know
            return out(200, {"accounts": self.accounts.count() > 0, "signed_in": bool(me)})
        if action == "sessions":
            if not me:
                return out(401, {"error": "sign in first"})
            if not me.get("_sid"):
                return out(403, {"error": "sign in from the browser to see your sessions"})
            return out(200, self.accounts.sessions(me["id"], me["_sid"]))
        return out(404, {"error": "not found"})

    def post(self, action: str, body: dict):
        me, acc = self._me(), self.accounts
        if action == "login":
            name = str(body.get("name", ""))
            try:
                user, sid = acc.login(name, str(body.get("password", "")), request_agent())
            except LockedOut as e:
                known = acc._row(name) is not None
                acc.audit(
                    "sign-in refused", name.strip()[:80] if known else "", "locked after repeated wrong tries"
                )
                return 429, {"error": str(e)}
            except ValueError as e:
                # Only a real account's name: text typed into the name field may be a password.
                if acc._row(name) is not None:
                    acc.audit("sign-in failed", name.strip()[:80])
                else:
                    acc.audit("sign-in failed", "", "unknown name")
                return 401, {"error": str(e)}
            acc.audit("sign-in", user["name"], request_agent(), actor=user)
            return 200, {"name": user["name"]}, {"Set-Cookie": acc.cookie(sid)}
        if not me:
            return 401, {"error": "sign in first"}
        if not me.get("_sid"):  # an extension token can't manage the account
            return 403, {"error": "sign in from the browser to change your account"}
        if action == "logout":
            acc.logout(me["_sid"])
            acc.audit("sign-out", me["name"], actor=me)
            return 200, {"ok": True}, {"Set-Cookie": acc.cookie(None)}
        if action == "password":
            if not acc.check_password(me["id"], str(body.get("current", ""))):
                acc.audit("password change refused", me["name"], "wrong current password", actor=me)
                return 400, {"error": "your current password is wrong"}
            acc.set_password(me["name"], str(body.get("new", "")), keep_session=me["_sid"])
            acc.audit("password changed", me["name"], "other sessions signed out", actor=me)
            return 200, {"ok": True}
        if action == "prefs":
            return 200, acc.set_prefs(me["id"], body.get("changes") or {})
        if action == "token-create":
            info, token = acc.create_token(
                me["id"], str(body.get("label", "")), str(body.get("scope", "extension"))
            )
            acc.audit(
                "token created", me["name"], f"{TOKEN_SCOPES[info['scope']]}: {info['label']}", actor=me
            )
            return 200, {**info, "token": token}
        if action == "token-revoke":
            ok = acc.revoke_token(me["id"], int(body["id"]))
            if ok:
                acc.audit("token revoked", me["name"], f"token {int(body['id'])}", actor=me)
            return 200, {"ok": ok}
        if action == "session-revoke":
            ok = acc.revoke_session(me["id"], str(body.get("id", "")))
            if ok:
                acc.audit("session signed out", me["name"], actor=me)
            return 200, {"ok": ok}
        return 404, {"error": "not found"}
