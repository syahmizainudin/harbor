"""Keep unfinished jobs across restarts.

When a job changes, the queued and running jobs of both queues are written to a small JSON file
(``HARBOR_STATE_FILE``, default ``$XDG_STATE_HOME/harbor/queue.json``). Changes are batched: the
first one schedules a save ``SAVE_DELAY`` seconds later, which writes everything changed by then.
At start-up the jobs are added again through the queues' public ``add`` methods.

  * The API key is never written. A restored download that needs a key uses the ``.env`` key;
    without one it is skipped with a note.
  * A running job comes back as queued and starts over. A download's leftover ``.part`` (from
    a hard stop) is removed first, so it isn't mistaken for another file.
  * Finished and cancelled jobs are not kept, and a download's link to its convert job is not kept.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from cbzwebp.jobqueue import ACTIVE, JobQueue
from dlqueue.download import Auth, DownloadQueue
from dlqueue.paths import PART_SUFFIX

FORMAT = 1


def default_state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "harbor" / "queue.json"


SAVE_DELAY = 0.5  # seconds from the first unsaved change to the save that writes it


class QueueStore:
    def __init__(self, path: Path, delay: float = SAVE_DELAY):
        self.path = Path(path)
        self.delay = delay
        self._enabled = True
        self._lock = threading.Lock()  # one snapshot-and-write at a time, and ``enabled`` changes
        self._sched = threading.Lock()  # ``_dirty`` and ``_timer``; never held while saving
        self._dirty = False
        self._timer: threading.Timer | None = None
        self._queues: tuple[JobQueue, DownloadQueue] | None = None
        self.owners = None  # perms.JobOwners: who queued each job, kept across restarts

    def attach(self, convert_queue: JobQueue, download_queue: DownloadQueue, owners=None) -> None:
        self._queues = (convert_queue, download_queue)
        self.owners = owners

    def _owner(self, kind: str, job_id: int) -> int | None:
        return self.owners.get(kind, job_id) if self.owners is not None else None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Off: changes are no longer saved, and a scheduled save won't write. Once this returns,
        no save from a change is writing either (shutdown relies on that: it marks running jobs
        cancelled, and a save after that would drop them)."""
        with self._lock:
            self._enabled = bool(value)
        if not value:
            with self._sched:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None

    # ------------------------------------------------------------ saving
    def on_change(self, _job=None) -> None:
        """Queue ``on_change`` hook: mark the state changed and schedule one save for the batch."""
        if not (self._enabled and self._queues):
            return
        with self._sched:
            self._dirty = True
            if self._timer is None:
                self._timer = threading.Timer(self.delay, self._scheduled_save)
                self._timer.daemon = True
                self._timer.start()

    def _scheduled_save(self) -> None:
        with self._sched:
            self._timer = None
        self.flush()

    def flush(self) -> None:
        """Save now if a change hasn't been saved yet (and saving is on)."""
        with self._lock:
            if self._dirty and self._enabled:
                self._write()

    def save(self) -> None:
        """Save now, whatever has changed (shutdown and restore)."""
        with self._lock:
            self._write()

    def snapshot(self) -> dict:
        cq, dq = self._queues
        return {
            "format": FORMAT,
            "convert": [
                {
                    "path": j.path,
                    "quality": j.quality,
                    "delete_original": j.delete_original,
                    "overwrite": j.overwrite,
                    "method": j.method,
                    "owner": self._owner("convert", j.id),
                }
                for j in cq.jobs()
                if j.state in ACTIVE
            ],
            "downloads": [
                {
                    "url": j.url,  # never contains the key (dl-queue strips it in query mode)
                    "dest_dir": j.dest_dir,
                    "after": j.after,
                    "auth_mode": j.auth.mode,
                    "auth_name": j.auth.name,
                    "filename": j.filename,
                    "owner": self._owner("download", j.id),
                }
                for j in dq.jobs()
                if j.state in ACTIVE
            ],
        }

    def _write(self) -> None:
        """Snapshot and write, under ``_lock`` so snapshots reach the file in the order taken. A
        change from here on marks the state changed again and schedules another save."""
        with self._sched:
            self._dirty = False
        data = json.dumps(self.snapshot())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ restoring
    def load(self) -> dict | None:
        """The saved state, or None. An unreadable file is moved aside, not deleted."""
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("format") != FORMAT:
                raise ValueError("unknown format")
            return data
        except (OSError, ValueError):
            self.path.replace(self.path.with_name(self.path.name + ".bad"))
            return None

    def restore(self, api_key: str = "") -> list[str]:
        """Add saved jobs back. Returns notes for anything that couldn't be restored."""
        data = self.load()
        if not data:
            return []
        notes: list[str] = []
        restored = 0
        was_enabled, self.enabled = self.enabled, False  # save once at the end, not per job
        try:
            restored = self._restore_jobs(data, api_key, notes)
        finally:
            self.enabled = was_enabled
        if was_enabled:
            self.save()
        if restored:
            notes.insert(0, f"restored {restored} unfinished job(s) from the last run")
        return notes

    def _restore_jobs(self, data: dict, api_key: str, notes: list[str]) -> int:
        cq, dq = self._queues
        restored = 0
        for c in data.get("convert", []):
            try:
                added, _ = cq.add(
                    [c["path"]],
                    int(c["quality"]),
                    delete_original=bool(c["delete_original"]),
                    overwrite=bool(c.get("overwrite", False)),
                    recursive=False,
                    method=int(c.get("method", 4)),
                )
                restored += len(added)
                for j in added:
                    if self.owners is not None:
                        self.owners.set("convert", j.id, c.get("owner"))
            except (KeyError, TypeError, ValueError, OSError) as e:
                notes.append(f"not restored: conversion of {c.get('path', '?')}: {e}")
        for d in data.get("downloads", []):
            try:
                mode = d.get("auth_mode", "none")
                if mode != "none" and not api_key:
                    notes.append(
                        f"not restored: {d['url']}: its API key isn't kept across restarts; add it again"
                    )
                    continue
                if d.get("filename"):
                    (Path(d["dest_dir"]) / (d["filename"] + PART_SUFFIX)).unlink(missing_ok=True)
                auth = Auth(mode, api_key if mode != "none" else "", d.get("auth_name", ""))
                added, _ = dq.add([d["url"]], d["dest_dir"], auth, after=d.get("after") or {})
                restored += len(added)
                for j in added:
                    if self.owners is not None:
                        self.owners.set("download", j.id, d.get("owner"))
            except (KeyError, TypeError, ValueError, OSError) as e:
                notes.append(f"not restored: {d.get('url', '?')}: {e}")
        return restored
