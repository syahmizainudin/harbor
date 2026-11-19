"""Job history: one record per finished conversion or download, kept for ``keep_days``.

The queues' ``on_change`` fires on every progress tick. ``History.on_change`` only notes a job the
first time it reaches a finished state (keyed by its ``finished_at``, since a retry reuses the id)
and hands it to a writer thread, so workers never wait on SQLite or on an add recording its owner.
Records hold paths, URLs, sizes and the result; never the API key (``Download.url`` never has it,
and ``auth`` isn't read).

Set ``enabled = False`` before shutting the queues down: shutdown cancels running jobs, which are
saved and restored, not finished.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path

from .config import xdg_path

FINISHED = ("done", "skipped", "failed", "cancelled")
DAY = 86400
MAX_LIMIT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('convert', 'download')),
    job_id INTEGER NOT NULL,
    owner INTEGER,                 -- harbor user id; NULL without accounts or for unowned jobs
    name TEXT NOT NULL,
    source TEXT NOT NULL,          -- the archive's path, or the download's URL
    output TEXT,
    state TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    bytes_in INTEGER NOT NULL DEFAULT 0,
    bytes_out INTEGER NOT NULL DEFAULT 0,
    linked_id INTEGER,             -- a download's convert job
    added_at REAL, started_at REAL, finished_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS history_owner ON history(owner, finished_at);
CREATE INDEX IF NOT EXISTS history_finished ON history(finished_at);
"""


def default_history_db() -> Path:
    return xdg_path("XDG_DATA_HOME", "harbor", "history.db")


def job_kind(job) -> str:
    return "download" if hasattr(job, "url") else "convert"


def _record(kind: str, job) -> dict:
    """What is kept of a finished job (read now: the job object keeps changing on retry)."""
    if kind == "download":
        name = job.filename or Path(job.url.split("?", 1)[0]).name or job.url
        source, bytes_in, bytes_out = job.url, job.done, job.done
    else:
        name, source, bytes_in, bytes_out = Path(job.path).name, job.path, job.bytes_in, job.bytes_out
    return {
        "kind": kind,
        "job_id": job.id,
        "name": name,
        "source": source,
        "output": job.output,
        "state": job.state,
        "message": job.message or "",
        "bytes_in": int(bytes_in or 0),
        "bytes_out": int(bytes_out or 0),
        "linked_id": getattr(job, "linked_id", None),
        "added_at": job.added_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at or time.time(),
    }


class History:
    def __init__(self, db_path: str | Path, keep_days: int = 90, owners=None):
        """``keep_days`` 0 keeps records forever. ``owners`` (``perms.JobOwners``) names who queued
        each job."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days
        self.owners = owners
        self.enabled = True
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        self._seen: dict[tuple[str, int], float] = {}  # (kind, job id) -> finished_at recorded
        self._seen_lock = threading.Lock()
        self._totals: dict = {}  # cached "saved" sums, dropped on every write
        self._queue: queue.Queue = queue.Queue()
        self._pruned_at = 0.0
        self.prune()
        self._thread = threading.Thread(target=self._writer, name="history", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ recording
    def on_change(self, job) -> None:
        """The queues' ``on_change``: notes a job once, when it first reaches a finished state."""
        if not self.enabled or job.state not in FINISHED:
            return
        kind = job_kind(job)
        key, at = (kind, int(job.id)), job.finished_at
        with self._seen_lock:
            if self._seen.get(key) == at:
                return
            self._seen[key] = at
        self._queue.put(_record(kind, job))

    def _writer(self) -> None:
        while True:
            try:
                rec = self._queue.get(timeout=60)
            except queue.Empty:
                self.prune()
                continue
            if rec is None:
                return
            try:
                if self.owners is not None and rec.get("owner") is None:
                    rec["owner"] = self.owners.get_settled(rec["kind"], rec["job_id"])
                self.add(rec)
            except Exception as e:  # noqa: BLE001 - history must never stop the queues
                print(f"harbor: couldn't record {rec.get('name')} in the history: {e}", flush=True)
            finally:
                self._queue.task_done()
            if time.time() - self._pruned_at > DAY:
                self.prune()

    def add(self, rec: dict) -> None:
        cols = ", ".join(rec)
        with self._lock:
            self._db.execute(
                f"INSERT INTO history({cols}) VALUES ({', '.join('?' * len(rec))})", tuple(rec.values())
            )
            self._totals.clear()

    def flush(self, timeout: float = 10) -> None:
        """Wait until everything noted so far is written (tests, and shutdown)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)

    def prune(self) -> int:
        """Delete records older than ``keep_days``."""
        self._pruned_at = time.time()
        if not self.keep_days:
            return 0
        with self._lock:
            n = self._db.execute(
                "DELETE FROM history WHERE finished_at < ?", (time.time() - self.keep_days * DAY,)
            ).rowcount
            if n:
                self._totals.clear()
        return n

    def close(self) -> None:
        self.enabled = False
        self.flush()
        self._queue.put(None)
        self._thread.join(timeout=5)
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ reading
    @staticmethod
    def _who(owner) -> tuple[str, list]:
        """SQL for one user's records, or everyone's (``owner`` None)."""
        return ("1=1", []) if owner is None else ("owner = ?", [int(owner)])

    def list(
        self, owner: int | None = None, kind: str = "", state: str = "", limit: int = 50, offset: int = 0
    ) -> dict:
        where, args = self._who(owner)
        where = [where]
        if kind:
            if kind not in ("convert", "download"):
                raise ValueError("kind must be convert or download")
            where.append("kind = ?")
            args.append(kind)
        if state:
            if state not in FINISHED:
                raise ValueError(f"state must be one of {', '.join(FINISHED)}")
            where.append("state = ?")
            args.append(state)
        w = " AND ".join(where)
        limit, offset = max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM history WHERE {w} ORDER BY finished_at DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            total = self._db.execute(f"SELECT count(*) FROM history WHERE {w}", args).fetchone()[0]
        return {"items": [dict(r) for r in rows], "total": total}

    def saved(self, owner: int | None = None) -> dict:
        """Bytes saved by finished conversions: all kept history and the last 30 days."""
        key = ("saved", owner)
        with self._lock:
            if key not in self._totals:
                where, args = self._who(owner)
                since = time.time() - 30 * DAY
                r = self._db.execute(
                    f"""SELECT coalesce(sum(bytes_in - bytes_out), 0),
                               coalesce(sum(CASE WHEN finished_at >= ? THEN bytes_in - bytes_out END), 0),
                               count(*)
                        FROM history
                        WHERE kind = 'convert' AND state = 'done' AND bytes_out > 0 AND {where}""",
                    (since, *args),
                ).fetchone()
                self._totals[key] = {"all": r[0], "last_30_days": r[1], "conversions": r[2]}
            return dict(self._totals[key])


class HistoryAPI:
    """``/api/history/``: your own finished jobs, or everyone's with ``jobs.see_all``. Signed-out
    visitors never get here (``perms.guest_may`` leaves it out)."""

    def __init__(self, history: History, guests: bool = False):
        """``guests``: accounts are on, so no current user is a signed-out visitor (who sees nothing)."""
        self.history = history
        self.guests = guests

    def _scope(self, want_all: bool):
        """(owner to filter by or None for all, whether allowed)."""
        from cbzwebp.web import current_user

        user = current_user()
        if user is None:  # no accounts: one person, everything is theirs; a guest: nothing
            return None, not self.guests
        if want_all:
            return None, "jobs.see_all" in user.get("perms", ())
        return user["id"], True

    def state(self) -> dict:
        owner, ok = self._scope(False)
        return {"history_saved": self.history.saved(owner)} if ok else {}

    def get(self, action: str, query: dict):
        import json

        def out(status, obj):
            return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()

        if action != "list":
            return out(404, {"error": "not found"})
        owner, ok = self._scope(str(query.get("all", "")) in ("1", "true"))
        if not ok:
            return out(403, {"error": "you don't have permission for this"})
        return out(
            200,
            self.history.list(
                owner,
                query.get("kind", ""),
                query.get("state", ""),
                int(query.get("limit", 50)),
                int(query.get("offset", 0)),
            ),
        )

    def post(self, action: str, body: dict):
        return 404, {"error": "not found"}
