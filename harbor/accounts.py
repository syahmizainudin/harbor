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
]
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
    ("users.manage", "Manage users"),
    ("roles.manage", "Manage roles and permissions"),
]
ALL_PERMS = frozenset(p for p, _ in PERMISSIONS)


def _text(limit: int):
    return lambda v: isinstance(v, str) and len(v) <= limit and not any(c in v for c in "\r\n\0")


def _int_in(lo: int, hi: int):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


# A user's own defaults for the Convert and Download tabs: name -> check. Never the API key.
PREFS = {
    "quality": _int_in(1, 100),
    "method": _int_in(0, 6),
    "delete_original": lambda v: isinstance(v, bool),
    "download_dir": _text(4096),
    "auth_mode": lambda v: v in ("none", "bearer", "header", "query"),
    "auth_name": _text(200),
    "convert_downloads": lambda v: isinstance(v, bool),
}


def clean_perms(perms) -> list[str]:
    if not isinstance(perms, list) or any(p not in ALL_PERMS for p in perms):
        raise ValueError(f"permissions must be a list of: {', '.join(sorted(ALL_PERMS))}")
    return [p for p, _ in PERMISSIONS if p in perms]


def default_accounts_db() -> Path:
    import os

    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "harbor" / "harbor.db"


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
    def __init__(self, db_path: str | Path, session_days: int = 30, secure_cookie: bool = False):
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
        perms = sorted(ALL_PERMS) if r["locked"] else json.loads(r["perms_json"] or "[]")
        return {
            "id": r["id"],
            "name": r["name"],
            "admin": r["role_id"] == ADMIN_ROLE,
            "disabled": bool(r["disabled"]),
            "role_id": r["role_id"],
            "role": r["role"],
            "perms": perms,
        }

    def count(self) -> int:
        return self._q("SELECT count(*) FROM users")[0][0]

    def users(self) -> list[dict]:
        return [self._public(r) for r in self._q(f"{self.USER_SQL} ORDER BY lower(u.name)")]

    def get(self, name: str) -> dict:
        rows = self._q(f"{self.USER_SQL} WHERE u.name = ?", (name.strip(),))
        if not rows:
            raise KeyError(f"no user {name!r}")
        return self._public(rows[0])

    def get_id(self, user_id: int) -> dict:
        rows = self._q(f"{self.USER_SQL} WHERE u.id = ?", (int(user_id),))
        if not rows:
            raise KeyError(f"no user {user_id}")
        return self._public(rows[0])

    def add(self, name: str, password: str, admin: bool = False, role: int | str | None = None) -> dict:
        name, password = check_name(name), check_new_password(password)
        role_id = ADMIN_ROLE if admin else self.role(role)["id"] if role is not None else MEMBER_ROLE
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO users(name, pass_hash, is_admin, role_id, created_at) VALUES (?,?,?,?,?)",
                    (name, hash_password(password), int(role_id == ADMIN_ROLE), role_id, time.time()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"there is already a user called {name!r}") from None
        return self.get(name)

    def set_role(self, name: str, role: int | str) -> dict:
        user, target = self.get(name), self.role(role)
        if (
            user["admin"]
            and target["id"] != ADMIN_ROLE
            and not user["disabled"]
            and self._active_admins() <= 1
        ):
            raise ValueError("this is the only admin who can sign in; make another admin first")
        with self._lock:
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
        self._x("DELETE FROM roles WHERE id = ?", (r["id"],))

    def set_password(self, name: str, password: str, keep_session: str | None = None) -> None:
        """Change a password and end the user's other sessions (all of them unless ``keep_session``)."""
        user = self.get(name)
        with self._lock:
            self._db.execute(
                "UPDATE users SET pass_hash = ? WHERE id = ?",
                (hash_password(check_new_password(password)), user["id"]),
            )
            keep = _sha(keep_session) if keep_session else ""
            self._db.execute("DELETE FROM sessions WHERE user_id = ? AND id_hash != ?", (user["id"], keep))

    def set_disabled(self, name: str, disabled: bool) -> None:
        user = self.get(name)
        if disabled and user["admin"] and self._active_admins() <= 1:
            raise ValueError("this is the only admin who can sign in; add or enable another admin first")
        with self._lock:
            self._db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user["id"]))
            if disabled:
                self._db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
                self._db.execute("DELETE FROM tokens WHERE user_id = ?", (user["id"],))

    def remove(self, name: str) -> None:
        user = self.get(name)
        if user["admin"] and self._active_admins() <= 1 and not user["disabled"]:
            raise ValueError("this is the only admin who can sign in; add or enable another admin first")
        self._x("DELETE FROM users WHERE id = ?", (user["id"],))

    def _active_admins(self) -> int:
        return self._q("SELECT count(*) FROM users WHERE role_id = ? AND disabled = 0", (ADMIN_ROLE,))[0][0]

    def first_admin(self) -> dict | None:
        rows = self._q(f"{self.USER_SQL} WHERE u.role_id = ? ORDER BY u.id LIMIT 1", (ADMIN_ROLE,))
        return self._public(rows[0]) if rows else None

    # ------------------------------------------------------------ signing in
    def _check_lock(self, key: str, now: float) -> None:
        recent = [t for t in self._failures.get(key, []) if now - t < LOCK_WINDOW]
        self._failures[key] = recent
        if len(recent) >= LOCK_AFTER:
            raise LockedOut(int(LOCK_WINDOW - (now - recent[0])))

    def login(self, name: str, password: str) -> tuple[dict, str]:
        """Check a name and password; returns (user, new session id). Raises ValueError or LockedOut."""
        key, now = str(name or "").strip().lower(), time.time()
        with self._lock:
            self._check_lock(key, now)
        rows = self._q(f"{self.USER_SQL} WHERE u.name = ?", (str(name or "").strip(),))
        stored = rows[0]["pass_hash"] if rows else _DUMMY_HASH
        ok = (
            verify_password(str(password or "")[:MAX_PASSWORD], stored)
            and bool(rows)
            and not rows[0]["disabled"]
        )
        if not ok:
            with self._lock:
                self._failures.setdefault(key, []).append(now)
            raise ValueError("wrong name or password")
        with self._lock:
            self._failures.pop(key, None)
            sid = secrets.token_urlsafe(32)
            self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self._db.execute(
                "INSERT INTO sessions(id_hash, user_id, created_at, last_seen, expires_at)"
                " VALUES (?,?,?,?,?)",
                (_sha(sid), rows[0]["id"], now, now, now + self.session_seconds),
            )
        return self._public(rows[0]), sid

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
            """SELECT u.*, r.name AS role, r.perms_json, r.locked, s.last_seen
               FROM sessions s JOIN users u ON u.id = s.user_id LEFT JOIN roles r ON r.id = u.role_id
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

    # ------------------------------------------------------------ extension tokens
    def create_token(self, user_id: int, label: str) -> tuple[dict, str]:
        label = (label or "").strip()[:80] or "Chrome extension"
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            tid = self._db.execute(
                "INSERT INTO tokens(hash, user_id, label, created_at) VALUES (?,?,?,?)",
                (_sha(token), int(user_id), label, time.time()),
            ).lastrowid
        return {"id": tid, "label": label}, token

    def tokens(self, user_id: int) -> list[dict]:
        return [
            {"id": r["id"], "label": r["label"], "created_at": r["created_at"], "last_used": r["last_used"]}
            for r in self._q("SELECT * FROM tokens WHERE user_id = ? ORDER BY id", (int(user_id),))
        ]

    def revoke_token(self, user_id: int, token_id: int) -> bool:
        return bool(self._x("DELETE FROM tokens WHERE id = ? AND user_id = ?", (int(token_id), int(user_id))))

    def token_user(self, token: str) -> dict | None:
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        rows = self._q(
            """SELECT u.*, r.name AS role, r.perms_json, r.locked, t.id AS tid, t.last_used
               FROM tokens t JOIN users u ON u.id = t.user_id LEFT JOIN roles r ON r.id = u.role_id
               WHERE t.hash = ? AND u.disabled = 0""",
            (_sha(token),),
        )
        if not rows:
            return None
        if (rows[0]["last_used"] or 0) < time.time() - TOUCH_EVERY:
            self._x("UPDATE tokens SET last_used = ? WHERE id = ?", (time.time(), rows[0]["tid"]))
        return self._public(rows[0])


def session_id(headers) -> str:
    raw = headers.get("Cookie", "") if headers else ""
    try:
        jar = SimpleCookie(raw)
    except CookieError:
        return ""
    return jar[COOKIE].value if COOKIE in jar else ""


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
        """Permissions on what the console server serves itself."""
        from .perms import may_browse_folders

        return may_browse_folders(user) if path == "/api/dirs" else True

    def user(self, headers) -> dict | None:
        token = headers.get(TOKEN_HEADER, "") if headers else ""
        origin = headers.get("Origin", "") if headers else ""
        self._request.machine = bool(token) or origin.startswith("chrome-extension://")
        if token:
            user = self.accounts.token_user(token)
            return user and {**user, "prefs": self.accounts.prefs(user["id"])}
        sid = session_id(headers)
        user = self.accounts.session_user(sid)
        # The session id rides along for sign-out and password changes; AccountAPI never shows it.
        return user and {**user, "prefs": self.accounts.prefs(user["id"]), "_sid": sid}


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
        import json

        if action == "status":  # public: what the sign-in page needs to know
            me = self._me()
            body = {"accounts": self.accounts.count() > 0, "signed_in": bool(me)}
            return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
        return 404, {"Content-Type": "application/json"}, b'{"error": "not found"}'

    def post(self, action: str, body: dict):
        me = self._me()
        if action == "login":
            try:
                user, sid = self.accounts.login(str(body.get("name", "")), str(body.get("password", "")))
            except LockedOut as e:
                return 429, {"error": str(e)}
            except ValueError as e:
                return 401, {"error": str(e)}
            return 200, {"name": user["name"]}, {"Set-Cookie": self.accounts.cookie(sid)}
        if not me:
            return 401, {"error": "sign in first"}
        if not me.get("_sid"):  # an extension token can't manage the account
            return 403, {"error": "sign in from the browser to change your account"}
        if action == "logout":
            self.accounts.logout(me["_sid"])
            return 200, {"ok": True}, {"Set-Cookie": self.accounts.cookie(None)}
        if action == "password":
            rows = self.accounts._q("SELECT pass_hash FROM users WHERE id = ?", (me["id"],))
            if not rows or not verify_password(
                str(body.get("current", ""))[:MAX_PASSWORD], rows[0]["pass_hash"]
            ):
                return 400, {"error": "your current password is wrong"}
            self.accounts.set_password(me["name"], str(body.get("new", "")), keep_session=me["_sid"])
            return 200, {"ok": True}
        if action == "prefs":
            return 200, self.accounts.set_prefs(me["id"], body.get("changes") or {})
        if action == "token-create":
            info, token = self.accounts.create_token(me["id"], str(body.get("label", "")))
            return 200, {**info, "token": token}
        if action == "token-revoke":
            return 200, {"ok": self.accounts.revoke_token(me["id"], int(body["id"]))}
        return 404, {"error": "not found"}
