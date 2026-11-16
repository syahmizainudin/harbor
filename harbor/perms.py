"""Permissions at the edge of the queue APIs, and who queued each job.

The two tools' APIs know nothing about users. harbor wraps them (``GuardedQueueAPI``) so that:

  * adding needs ``convert`` or ``download`` (and ``delete_original`` to delete originals, and
    ``convert`` for a download that is converted afterwards);
  * each job remembers who queued it (``JobOwners``): through the API, a download's hand-off to the
    converter, a retry, or a restore after restart;
  * without ``jobs.see_all`` a user sees, cancels and retries only their own jobs, and can't use
    the queue-wide actions (pause, cancel all, clear finished).

Without accounts there is no current user, and everything passes straight through.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from cbzwebp.web import current_user

SEE_ALL = "jobs.see_all"
QUEUE_WIDE = ("cancel-all", "clear", "pause")
DENIED = {"error": "you don't have permission for this"}


def can(permission: str) -> bool:
    """Whether the request's user has a permission. True without accounts (no user)."""
    user = current_user()
    return user is None or permission in user.get("perms", ())


def may_browse_folders(user) -> bool:
    """``/api/dirs`` (the folder picker) is for people who convert, download or manage the library."""
    perms = set(user.get("perms", ())) if user else set()
    return bool(perms & {"convert", "download", "library.manage"})


class JobOwners:
    """Job id -> user id, per queue ("convert" or "download"). In memory; ``QueueStore`` saves it."""

    def __init__(self):
        self._owners: dict[tuple[str, int], int] = {}
        self._lock = threading.RLock()

    def get(self, kind: str, job_id: int) -> int | None:
        with self._lock:
            return self._owners.get((kind, int(job_id)))

    def set(self, kind: str, job_id: int, user_id: int | None) -> None:
        if user_id is not None:
            with self._lock:
                self._owners[(kind, int(job_id))] = int(user_id)

    @contextmanager
    def adding(self, kind: str, queue, user_id: int | None):
        """Give every job that appears in ``queue`` during the block to ``user_id``. Adds are
        serialized here, so another user's add can't be counted as this one's."""
        with self._lock:
            before = {j.id for j in queue.jobs()}
            yield
            for j in queue.jobs():
                if j.id not in before:
                    self.set(kind, j.id, user_id)


class GuardedQueueAPI:
    """Wraps ConvertAPI (``kind="convert"``) or DownloadAPI (``kind="download"``)."""

    def __init__(self, inner, queue, kind: str, owners: JobOwners):
        self.inner, self.queue, self.kind, self.owners = inner, queue, kind, owners
        self.list_key = "jobs" if kind == "convert" else "downloads"

    def state(self) -> dict:
        st = self.inner.state()
        user = current_user()
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
            return self.inner.post(action, body)
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
