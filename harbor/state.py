"""Keep unfinished jobs across restarts.

Every time a job changes, the queued and running jobs of both queues are written to a small
JSON file (``HARBOR_STATE_FILE``, default ``$XDG_STATE_HOME/harbor/queue.json``). At start-up
they are added again through the queues' public ``add`` methods.

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


class QueueStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.enabled = True
        self._lock = threading.Lock()
        self._queues: tuple[JobQueue, DownloadQueue] | None = None

    def attach(self, convert_queue: JobQueue, download_queue: DownloadQueue) -> None:
        self._queues = (convert_queue, download_queue)

    # ------------------------------------------------------------ saving
    def on_change(self, _job=None) -> None:
        """Queue ``on_change`` hook: save after every job change."""
        if self.enabled and self._queues:
            self.save()

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
                }
                for j in dq.jobs()
                if j.state in ACTIVE
            ],
        }

    def save(self) -> None:
        data = json.dumps(self.snapshot(), indent=1)
        with self._lock:
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
            except (KeyError, TypeError, ValueError, OSError) as e:
                notes.append(f"not restored: {d.get('url', '?')}: {e}")
        return restored
