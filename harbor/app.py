"""Harbor: the cbz-webp convert queue and the dl-queue download queue on one page.

All the queue, file and server logic lives in those two packages. Harbor adds only:
  * ``check_after``: validates the convert options sent with each download;
  * ``make_on_saved``: after a download, queues a downloaded .zip/.cbz for conversion;
  * the API key and page defaults from its settings (``config.py``; the key stays here);
  * the two-tab page, and ``build`` / ``serve_harbor`` to wire it together.
"""

from __future__ import annotations

from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path

import os

from cbzshelf.library import Library
from cbzshelf.web import UI_HTML as LIBRARY_HTML
from cbzshelf.web import LibraryAPI
from cbzwebp.core import ARCHIVE_EXTS
from cbzwebp.jobqueue import DONE, JobQueue
from cbzwebp.web import ConvertAPI, serve
from dlqueue.download import Download, DownloadQueue
from dlqueue.web import DownloadAPI

from . import __version__
from .config import Settings

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()
LOGIN_HTML = (Path(__file__).parent / "login.html").read_bytes()


def check_after(after: dict) -> dict:
    """Validate and fill in the convert options a download carries (raises ValueError)."""
    convert = bool(after.get("convert", True))
    quality = int(after.get("quality", 80))
    if not 1 <= quality <= 100:
        raise ValueError("quality must be 1-100")
    method = int(after.get("method", 4))
    if not 0 <= method <= 6:
        raise ValueError("method must be 0-6")
    return {
        "convert": convert,
        "quality": quality,
        "delete_original": bool(after.get("delete_original", True)),
        "method": method,
    }


def make_on_saved(convert_queue: JobQueue, on_kept: Callable[[Path], None] | None = None):
    """After a download is saved, queue it for conversion if it is an archive and asked for.

    ``on_kept(path)`` is called for a file that isn't converted (an .epub, or with conversion off),
    so harbor can add it to the library at once."""

    def on_saved(job: Download, path: Path) -> None:
        after = job.after
        if not after.get("convert") or path.suffix.lower() not in ARCHIVE_EXTS:
            if on_kept:
                on_kept(path)
            return
        added, _ = convert_queue.add(
            [path],
            after["quality"],
            delete_original=after["delete_original"],
            recursive=False,
            method=after.get("method", 4),
        )
        if added:
            job.linked_id = added[0].id
            job.message = "saved; queued for conversion"

    return on_saved


def build(
    *,
    jobs: int = 1,
    image_threads: int | None = None,
    downloads: int = 2,
    timeout: float = 60,
    retries: int = 3,
    on_change: Callable | None = None,
    on_convert_change: Callable | None = None,
    on_download_kept: Callable[[Path], None] | None = None,
) -> tuple[JobQueue, DownloadQueue]:
    """Start both queues, with downloads handing archives to the convert queue.

    ``on_change`` is called after any job in either queue changes (harbor saves its state there);
    ``on_convert_change`` only for conversions (harbor adds finished ones to the library);
    ``on_download_kept`` with the path of a download that isn't converted.
    """

    def convert_changed(job) -> None:
        for hook in (on_change, on_convert_change):
            if hook:
                hook(job)

    convert_queue = JobQueue(workers=jobs, image_threads=image_threads, on_change=convert_changed).start()
    download_queue = DownloadQueue(
        workers=downloads,
        on_saved=make_on_saved(convert_queue, on_download_kept),
        check_after=check_after,
        timeout=timeout,
        retries=retries,
        on_change=on_change,
    ).start()
    return convert_queue, download_queue


def default_library_db() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "harbor" / "library.db"


def default_library_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "harbor" / "thumbs"


def make_library_hook(library: Library):
    """When a conversion finishes inside a library folder, add the new .cbz to the library now."""

    def on_convert_change(job) -> None:
        if job.state == DONE and job.output:
            try:
                library.scan_file(job.output)  # no-op outside the library's folders
            except Exception as e:  # noqa: BLE001 - the next full scan picks it up anyway
                print(f"harbor: couldn't add {job.output} to the library: {e}", flush=True)

    return on_convert_change


def make_library_file_hook(library: Library):
    """When a download is kept as it is (an .epub, say) inside a library folder, add it now."""

    def add(path: Path) -> None:
        try:
            library.scan_file(path)  # no-op outside the library's folders or for other file types
        except Exception as e:  # noqa: BLE001 - the next full scan picks it up anyway
            print(f"harbor: couldn't add {path} to the library: {e}", flush=True)

    return add


class EnvKeyDownloadAPI(DownloadAPI):
    """DownloadAPI that uses the key from harbor's settings when the page sends none."""

    def __init__(self, queue: DownloadQueue, api_key: str = ""):
        super().__init__(queue)
        self._api_key = api_key

    def post(self, action: str, body: dict) -> tuple[int, dict]:
        auth = body.get("auth")
        if action == "add" and self._api_key and isinstance(auth, dict):
            if auth.get("mode", "none") != "none" and not auth.get("key"):
                body = {**body, "auth": {**auth, "key": self._api_key}}
        return super().post(action, body)


class HarborAPI:
    """Page defaults from settings, at /api/harbor/ (state only; no actions)."""

    def __init__(self, settings: Settings, notes: list[str] | None = None):
        self.settings = settings
        self.notes = notes if notes is not None else []  # the same list: the CLI fills it after start

    def state(self) -> dict:
        return {"harbor": {"version": __version__, **self.settings.page_defaults(), "notes": self.notes}}

    def post(self, action: str, body: dict) -> tuple[int, dict]:
        return 404, {"error": "not found"}


def _user_id() -> int:
    """The signed-in user's id for the library (0 without accounts)."""
    from cbzwebp.web import current_user

    user = current_user()
    return int(user["id"]) if user else 0


def serve_harbor(
    port: int,
    convert_queue: JobQueue,
    download_queue: DownloadQueue,
    settings: Settings | None = None,
    notes: list[str] | None = None,
    library: Library | None = None,
    accounts=None,
) -> ThreadingHTTPServer:
    """The page: convert at /api/, downloads at /api/download/, page defaults at /api/harbor/, and
    with a library, cbz-shelf's API at /api/library/ and its page at /library (the Library tab).
    With ``accounts``, everything but the sign-in page needs a signed-in user (``/api/account/``)."""
    from .accounts import AccountAPI, HarborSignIn

    settings = settings or Settings()
    apis = {
        "/api/": ConvertAPI(convert_queue, {"quality": settings.quality, "method": settings.method}),
        "/api/download/": EnvKeyDownloadAPI(download_queue, settings.api_key),
        "/api/harbor/": HarborAPI(settings, notes),
    }
    pages = {}
    if library is not None:
        apis["/api/library/"] = LibraryAPI(library, user_of=_user_id)
        pages["/library"] = LIBRARY_HTML
    auth = None
    if accounts is not None:
        apis["/api/account/"] = AccountAPI(accounts)
        pages["/login"] = LOGIN_HTML
        auth = HarborSignIn(accounts)
    extra = {"auth": auth} if auth else {}  # only when needed: works with a cbz-webp that predates sign-in
    return serve(port, UI_HTML, apis, ARCHIVE_EXTS, settings.allowed_origins(), pages, **extra)
