"""Harbor: the cbz-webp convert queue and the dl-queue download queue on one page.

All the queue, file and server logic lives in those two packages. Harbor adds only:
  * ``check_after``: validates the convert options sent with each download;
  * ``make_on_saved``: after a download, queues a downloaded .zip/.cbz for conversion;
  * the two-tab page, and ``build`` / ``serve_harbor`` to wire it together.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
from pathlib import Path

from cbzwebp.core import ARCHIVE_EXTS
from cbzwebp.jobqueue import JobQueue
from cbzwebp.web import ConvertAPI, serve
from dlqueue.download import Download, DownloadQueue
from dlqueue.web import DownloadAPI

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()


def check_after(after: dict) -> dict:
    """Validate and fill in the convert options a download carries (raises ValueError)."""
    convert = bool(after.get("convert", True))
    quality = int(after.get("quality", 80))
    if not 1 <= quality <= 100:
        raise ValueError("quality must be 1-100")
    return {
        "convert": convert,
        "quality": quality,
        "delete_original": bool(after.get("delete_original", True)),
    }


def make_on_saved(convert_queue: JobQueue):
    """After a download is saved, queue it for conversion if it is an archive and asked for."""

    def on_saved(job: Download, path: Path) -> None:
        after = job.after
        if not after.get("convert") or path.suffix.lower() not in ARCHIVE_EXTS:
            return
        added, _ = convert_queue.add(
            [path],
            after["quality"],
            delete_original=after["delete_original"],
            recursive=False,
        )
        if added:
            job.linked_id = added[0].id
            job.message = "saved; queued for conversion"

    return on_saved


def build(
    *, jobs: int = 1, image_threads: int | None = None, downloads: int = 2, timeout: float = 60
) -> tuple[JobQueue, DownloadQueue]:
    """Start both queues, with downloads handing archives to the convert queue."""
    convert_queue = JobQueue(workers=jobs, image_threads=image_threads).start()
    download_queue = DownloadQueue(
        workers=downloads,
        on_saved=make_on_saved(convert_queue),
        check_after=check_after,
        timeout=timeout,
    ).start()
    return convert_queue, download_queue


def serve_harbor(
    port: int, convert_queue: JobQueue, download_queue: DownloadQueue, defaults: dict
) -> ThreadingHTTPServer:
    """The two-tab page: convert at /api/, downloads at /api/download/."""
    apis = {
        "/api/": ConvertAPI(convert_queue, defaults),
        "/api/download/": DownloadAPI(download_queue),
    }
    return serve(port, UI_HTML, apis, ARCHIVE_EXTS)
