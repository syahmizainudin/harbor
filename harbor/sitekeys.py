"""Saved site keys: each account's own API keys and tokens, picked by a download's URL.

Plan: ``docs/plan-site-credentials.md``. Managed in the Account dialog's Sites tab
(``AccountAPI`` ``site-key-set`` / ``site-key-remove``, browser sessions with ``download`` only).

* **Encrypted at rest** with Fernet (the ``cryptography`` package). The encryption key is its own
  file, ``secret.key`` next to the accounts database, made on first use with mode 0600 and never
  stored in the database. ``harbor backup`` copies it only with ``--with-secrets``. If it's lost,
  saved keys can't be read: they show as unreadable and are skipped until their secret is entered
  again.
* **Never shown again.** ``list`` gives label, site, kind and name, and whether the secret can be
  read; never the secret. Saving with an empty secret keeps the old one.
* **Picked at each attempt.** A download added with key mode "auto" carries ``auth_ref="auto"``;
  dl-queue asks ``app.make_auth_for`` for the key at each attempt, which calls ``match`` for the
  download's owner. The secret never sits on the job or in the saved-jobs file.
* **HTTPS only:** a key goes only to ``https://`` URLs or to this machine (loopback, for tests).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

SITE_KEYS_MAX = 50
MODES = ("bearer", "header", "query")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
_HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_PATH = re.compile(r"^/[^\s\x00-\x1f]{0,199}$")


def key_file(accounts_db: Path) -> Path:
    """Where the encryption key lives: next to the accounts database."""
    return Path(accounts_db).parent / "secret.key"


class Vault:
    """Fernet encryption with a key kept in its own 0600 file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fernet = None

    def _get(self, create: bool):
        if self._fernet is not None:
            return self._fernet
        from cryptography.fernet import Fernet

        if not self.path.exists():
            if not create:
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # made by another thread meanwhile
                pass
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
        self._fernet = Fernet(self.path.read_bytes().strip())
        return self._fernet

    def encrypt(self, text: str) -> bytes:
        return self._get(create=True).encrypt(text.encode())

    def decrypt(self, blob: bytes) -> str | None:
        """The text, or None when it can't be read (no key file, or a different key)."""
        from cryptography.fernet import InvalidToken

        try:
            f = self._get(create=False)
            return None if f is None else f.decrypt(bytes(blob)).decode()
        except (InvalidToken, ValueError, OSError):
            return None


def _usable_url(url: str) -> tuple[str, str] | None:
    """(host, path) when a key may be sent to ``url``: https, or a loopback host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK):
        return host, parts.path or "/"
    return None


def _site_matches(row, host: str, path: str) -> bool:
    site = row["host"]
    host_ok = host.endswith(site[1:]) if site.startswith("*.") else host == site
    return host_ok and (not row["path"] or path.startswith(row["path"]))


class SiteKeys:
    """Each account's saved site keys, stored in the accounts database (migration 10)."""

    def __init__(self, accounts, vault: Vault):
        self.accounts = accounts
        self.vault = vault

    def _rows(self, user_id: int):
        return self.accounts._q(
            "SELECT * FROM site_keys WHERE user_id = ? ORDER BY lower(label), id", (int(user_id),)
        )

    def list(self, user_id: int) -> list[dict]:
        return [
            {
                "id": r["id"],
                "label": r["label"],
                "host": r["host"],
                "path": r["path"],
                "mode": r["mode"],
                "name": r["name"],
                "readable": self.vault.decrypt(r["secret"]) is not None,
                "updated_at": r["updated_at"],
            }
            for r in self._rows(user_id)
        ]

    @staticmethod
    def _check(body: dict) -> dict:
        from dlqueue.download import Auth

        label = str(body.get("label", "")).strip()
        host = str(body.get("host", "")).strip().lower()
        path = str(body.get("path", "") or "").strip()
        mode = str(body.get("mode", ""))
        name = str(body.get("name", "") or "").strip()
        if not 1 <= len(label) <= 60 or any(ord(c) < 32 for c in label):
            raise ValueError("give the key a name of 1-60 characters")
        if len(host) > 253 or not _HOST.match(host):
            raise ValueError("the site is a host name such as api.example.com or *.example.com")
        if path and not _PATH.match(path):
            raise ValueError("the path starts with / and has no spaces, such as /files/")
        if mode not in MODES:
            raise ValueError(f"the key is sent as one of {', '.join(MODES)}")
        if mode == "bearer":
            name = ""
        else:
            Auth(mode, "x", name).validate()  # the header or parameter name
        return {"label": label, "host": host, "path": path, "mode": mode, "name": name}

    def set(self, user_id: int, body: dict) -> dict:
        """Add a key, or change one of the user's own (``id``). An empty secret keeps the old one."""
        from dlqueue.download import Auth

        fields = self._check(body)
        secret = body.get("secret") or ""
        if not isinstance(secret, str):
            raise ValueError("the secret must be text")
        if secret:
            Auth(fields["mode"], secret, fields["name"] or "x").validate()  # one line, not empty
            if len(secret) > 4096:
                raise ValueError("the secret is at most 4096 characters")
        now = time.time()
        key_id = body.get("id")
        if key_id is not None:
            if isinstance(key_id, bool) or not isinstance(key_id, int):
                raise ValueError("id must be a number")
            row = self.accounts._q(
                "SELECT * FROM site_keys WHERE id = ? AND user_id = ?", (key_id, int(user_id))
            )
            if not row:
                raise KeyError(f"no saved key {key_id}")
            blob = self.vault.encrypt(secret) if secret else row[0]["secret"]
            if not secret and self.vault.decrypt(blob) is None:
                raise ValueError("enter the secret again: the saved one can't be read")
            self.accounts._x(
                """UPDATE site_keys SET label = ?, host = ?, path = ?, mode = ?, name = ?, secret = ?,
                   updated_at = ? WHERE id = ? AND user_id = ?""",
                (*fields.values(), blob, now, key_id, int(user_id)),
            )
        else:
            if not secret:
                raise ValueError("enter the key or token")
            if len(self._rows(user_id)) >= SITE_KEYS_MAX:
                raise ValueError(f"you can save at most {SITE_KEYS_MAX} site keys")
            self.accounts._x(
                """INSERT INTO site_keys(
                       user_id, label, host, path, mode, name, secret, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(user_id), *fields.values(), self.vault.encrypt(secret), now, now),
            )
        return fields

    def remove(self, user_id: int, key_id: int) -> dict | None:
        row = self.accounts._q(
            "SELECT * FROM site_keys WHERE id = ? AND user_id = ?", (int(key_id), int(user_id))
        )
        if not row:
            return None
        self.accounts._x("DELETE FROM site_keys WHERE id = ? AND user_id = ?", (int(key_id), int(user_id)))
        return {"label": row[0]["label"], "host": row[0]["host"], "path": row[0]["path"]}

    def _candidates(self, user_id: int, host: str, path: str) -> list:
        rows = [r for r in self._rows(user_id) if _site_matches(r, host, path)]
        # Most specific first: an exact host, then the longest path, then the oldest.
        return sorted(rows, key=lambda r: (r["host"].startswith("*."), -len(r["path"]), r["id"]))

    def match(self, user_id: int, url: str):
        """The dl-queue ``Auth`` for ``url`` from the user's saved keys, or None."""
        from dlqueue.download import Auth

        site = _usable_url(url)
        if site is None:
            return None
        for row in self._candidates(user_id, *site):
            secret = self.vault.decrypt(row["secret"])
            if secret is not None:
                return Auth(row["mode"], secret, row["name"])
        return None

    def matches_insecure(self, user_id: int, url: str) -> bool:
        """Whether a saved key would match ``url`` but isn't sent because it isn't https."""
        parts = urlsplit(url)
        if _usable_url(url) is not None or parts.scheme != "http":
            return False
        return bool(self._candidates(user_id, (parts.hostname or "").lower(), parts.path or "/"))
