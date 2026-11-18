"""Permissions at the edge of the queue APIs, and who queued each job.

The two tools' APIs know nothing about users. harbor wraps them (``GuardedQueueAPI``) so that:

  * adding needs ``convert`` or ``download`` (and ``delete_original`` to delete originals, and
    ``convert`` for a download that is converted afterwards);
  * each job remembers who queued it (``JobOwners``): through the API, a download's hand-off to the
    converter, a retry, or a restore after restart;
  * without ``jobs.see_all`` a user sees, cancels and retries only their own jobs, and can't use
    the queue-wide actions (pause, cancel all, clear finished).

Without accounts there is no current user, and everything passes straight through. With accounts,
no current user means a signed-out visitor (a guest): they may read the library and nothing else
(``guest_may``, ``guest_can``), and see no jobs.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from cbzwebp.web import current_user

SEE_ALL = "jobs.see_all"
QUEUE_WIDE = ("cancel-all", "clear", "pause")
DENIED = {"error": "you don't have permission for this"}
SIGN_IN = {"error": "sign in first"}

# What a guest (signed out, with accounts on) may do: open the page, read its state (with no jobs
# and no settings) and read the library. Their reading progress and reader settings are accepted
# but not saved (``GuestSafeLibraryAPI``), so turning a page never sends them to sign in.
# Favourites, editing and everything else need an account.
GUEST_PERMS = frozenset({"library.read"})
GUEST_PAGES = frozenset({"/", "/library", "/api/state"})
LIBRARY_PREFIX = "/api/library/"
GUEST_LIBRARY_POSTS = frozenset(
    {"progress", "clear-progress", "reader-settings", "prefs", "hide-continue", "clear-continue"}
)


def can(permission: str) -> bool:
    """Whether the request's user has a permission. True without accounts (no user)."""
    user = current_user()
    return user is None or permission in user.get("perms", ())


def guest_can(permission: str) -> bool:
    """``can`` with accounts on: a guest has only ``GUEST_PERMS``."""
    user = current_user()
    return permission in (user.get("perms", ()) if user else GUEST_PERMS)


def guest_may(method: str, path: str) -> bool:
    """Whether a signed-out request may go through (see ``GUEST_PERMS``)."""
    action = path[len(LIBRARY_PREFIX) :] if path.startswith(LIBRARY_PREFIX) else None
    if method == "GET":
        return path in GUEST_PAGES or (action is not None and "/" not in action)
    return method == "POST" and action in GUEST_LIBRARY_POSTS


class GuestSafeLibraryAPI:
    """cbz-shelf's LibraryAPI with accounts on: a guest's reading progress, reader settings and
    Continue-reading changes are answered but not saved (no shared guest data)."""

    def __init__(self, inner):
        self.inner = inner

    def state(self) -> dict:
        return self.inner.state()

    def get(self, action: str, query: dict):
        return self.inner.get(action, query)

    def post(self, action: str, body: dict):
        if current_user() is None:
            if action in GUEST_LIBRARY_POSTS:
                return 200, {"ok": True, "saved": False}
            return 401, SIGN_IN
        return self.inner.post(action, body)


def may_browse_folders(user) -> bool:
    """``/api/dirs`` (the folder picker) is for people who convert, download or manage the library."""
    perms = set(user.get("perms", ())) if user else set()
    return bool(perms & {"convert", "download", "library.manage"})


class JobOwners:
    """Job id -> user id, per queue ("convert" or "download"). In memory; ``QueueStore`` saves it.

    ``_lock`` guards only the dict and is held for short moments, so workers, the saved state and
    the hand-off never wait on an add. ``_add_locks`` serialize the adds to each queue (see
    ``adding``), which can take a while: a convert add scans folders."""

    def __init__(self):
        self._owners: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()
        self._add_locks = {"convert": threading.Lock(), "download": threading.Lock()}

    def get(self, kind: str, job_id: int) -> int | None:
        with self._lock:
            return self._owners.get((kind, int(job_id)))

    def get_settled(self, kind: str, job_id: int) -> int | None:
        """``get``, but when the job has no owner yet, first wait for an add to that queue that is
        still going: it may have queued the job and not recorded its owner yet."""
        owner = self.get(kind, job_id)
        if owner is None:
            with self._add_locks[kind]:
                owner = self.get(kind, job_id)
        return owner

    def set(self, kind: str, job_id: int, user_id: int | None) -> None:
        if user_id is not None:
            with self._lock:
                self._owners[(kind, int(job_id))] = int(user_id)

    @contextmanager
    def adding(self, kind: str, queue, user_id: int | None):
        """Give every job that appears in ``queue`` during the block to ``user_id``. Adds to one
        queue are serialized here, so another user's add can't be counted as this one's. A job that
        already has an owner keeps it: the download hand-off may queue a conversion meanwhile."""
        with self._add_locks[kind]:
            before = {j.id for j in queue.jobs()}
            yield
            if user_id is None:
                return
            new = [j.id for j in queue.jobs() if j.id not in before]
            with self._lock:
                for job_id in new:
                    self._owners.setdefault((kind, int(job_id)), int(user_id))


class GuardedQueueAPI:
    """Wraps ConvertAPI (``kind="convert"``) or DownloadAPI (``kind="download"``)."""

    def __init__(self, inner, queue, kind: str, owners: JobOwners, guests: bool = False):
        """``guests``: accounts are on, so no current user means a signed-out visitor."""
        self.inner, self.queue, self.kind, self.owners = inner, queue, kind, owners
        self.guests = guests
        self.list_key = "jobs" if kind == "convert" else "downloads"

    def state(self) -> dict:
        st = self.inner.state()
        user = current_user()
        if user is None and self.guests:  # a guest sees no jobs (names, URLs, folders)
            st[self.list_key] = []
            return st
        if user is not None and self.kind == "convert" and "defaults" in st:  # their own quality and effort
            prefs = user.get("prefs", {})
            st["defaults"] = {**st["defaults"], **{k: prefs[k] for k in ("quality", "method") if k in prefs}}
        if user is not None and SEE_ALL not in user.get("perms", ()):
            st[self.list_key] = [
                j for j in st[self.list_key] if self.owners.get(self.kind, j["id"]) == user["id"]
            ]
        return st

    def _add_refusal(self, body: dict, perms: set) -> str | None:
        if self.kind not in perms:
            return f"you don't have permission to {'convert' if self.kind == 'convert' else 'download'}"
        if self.kind == "convert":
            if body.get("delete_original", True) and "delete_original" not in perms:
                return "you may not delete originals: untick “Delete original .zip”"
        else:
            after = body.get("after") or {}
            if isinstance(after, dict) and after.get("convert", True):
                if "convert" not in perms:
                    return "you may not convert: untick “Convert .zip / .cbz downloads”"
                if after.get("delete_original", True) and "delete_original" not in perms:
                    return "you may not delete originals: untick “Delete original .zip” on the Convert tab"
        return None

    def post(self, action: str, body: dict):
        user = current_user()
        if user is None:
            return (401, SIGN_IN) if self.guests else self.inner.post(action, body)
        perms = set(user.get("perms", ()))
        if action == "add":
            refusal = self._add_refusal(body, perms)
            if refusal:
                return 403, {"error": refusal}
            with self.owners.adding(self.kind, self.queue, user["id"]):
                return self.inner.post(action, body)
        if action in ("cancel", "retry"):
            job_id = int(body["id"])
            owner = self.owners.get(self.kind, job_id)
            if SEE_ALL not in perms and owner != user["id"]:
                return 403, {"error": "that isn't your job"}
            if action == "retry":  # a retry is a new job: it keeps the original's owner
                with self.owners.adding(self.kind, self.queue, owner if owner is not None else user["id"]):
                    return self.inner.post(action, body)
            return self.inner.post(action, body)
        if action in QUEUE_WIDE and SEE_ALL not in perms:
            return 403, DENIED
        return self.inner.post(action, body)
