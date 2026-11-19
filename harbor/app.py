"""Harbor: the cbz-webp convert queue and the dl-queue download queue on one page.

All the queue, file and server logic lives in those two packages. Harbor adds only:
  * ``check_after``: validates the convert options sent with each download;
  * ``make_on_saved``: after a download, queues a downloaded .zip/.cbz for conversion;
  * the API key and page defaults from its settings (``config.py``; the key stays here);
  * the two-tab page, and ``build`` / ``serve_harbor`` to wire it together.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar
from http.server import ThreadingHTTPServer
from pathlib import Path

from cbzshelf.library import Library
from cbzshelf.web import UI_HTML as LIBRARY_HTML
from cbzshelf.web import LibraryAPI
from cbzwebp.core import ARCHIVE_EXTS
from cbzwebp.jobqueue import DONE, JobQueue
from cbzwebp.web import ConvertAPI, serve
from dlqueue.download import Download, DownloadQueue, parse_hours
from dlqueue.web import DownloadAPI

from . import __version__
from .config import Settings, xdg_path

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()
LOGIN_HTML = (Path(__file__).parent / "login.html").read_bytes()


def missing_dependencies() -> list[str]:
    """What the installed cbz-webp and cbz-shelf lack of the API harbor needs (empty when all is
    there). Harbor never runs with less: no sign-in or no library permissions would open it up."""
    import inspect

    missing = []
    for param in ("auth", "ssl_context", "trust_proxy"):
        if param not in inspect.signature(serve).parameters:
            missing.append(f"cbz-webp serve({param}=...)")
    for param in ("can", "visible"):
        if param not in inspect.signature(LibraryAPI).parameters:
            missing.append(f"cbz-shelf LibraryAPI({param}=...)")
    if "rate" not in inspect.signature(DownloadQueue).parameters:
        missing.append("dl-queue DownloadQueue(rate=...)")
    try:
        import cbzwebp.watch  # noqa: F401
    except ImportError:
        missing.append("cbz-webp watch folders")
    for name in ("give_user_data", "scan_file", "libraries", "book_file"):
        if not hasattr(Library, name):
            missing.append(f"cbz-shelf Library.{name}")
    return missing


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


def make_on_saved(convert_queue: JobQueue, on_kept: Callable[[Path], None] | None = None, owners=None):
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
            if owners is not None:  # the conversion belongs to whoever queued the download
                owners.set("convert", added[0].id, owners.get_settled("download", job.id))

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
    owners=None,
    rate: int = 0,
    hours: str = "",
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
        on_saved=make_on_saved(convert_queue, on_download_kept, owners),
        check_after=check_after,
        timeout=timeout,
        retries=retries,
        on_change=on_change,
        rate=rate,
        hours=parse_hours(hours) if hours else None,
    ).start()
    return convert_queue, download_queue


def default_library_db() -> Path:
    return xdg_path("XDG_DATA_HOME", "harbor", "library.db")


def default_library_cache() -> Path:
    return xdg_path("XDG_CACHE_HOME", "harbor", "thumbs")


def make_library_file_hook(library: Library):
    """When a file lands inside a library folder (a download kept as it is, an .epub say), add it now."""

    def add(path: Path) -> None:
        try:
            library.scan_file(path)  # no-op outside the library's folders or for other file types
        except Exception as e:  # noqa: BLE001 - the next full scan picks it up anyway
            print(f"harbor: couldn't add {path} to the library: {e}", flush=True)

    return add


def make_library_hook(library: Library):
    """When a conversion finishes inside a library folder, add the new .cbz to the library now."""
    add = make_library_file_hook(library)

    def on_convert_change(job) -> None:
        if job.state == DONE and job.output:
            add(job.output)

    return on_convert_change


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


# Settings a user's own defaults replace in the "harbor" state (quality and method go in "defaults").
USER_PAGE_PREFS = ("download_dir", "auth_mode", "auth_name", "delete_original", "method", "convert_downloads")


class HarborAPI:
    """Page defaults from settings, at /api/harbor/ (state only; no actions)."""

    def __init__(self, settings: Settings, notes: list[str] | None = None, guests: bool = False):
        self.settings = settings
        self.notes = notes if notes is not None else []  # the same list: the CLI fills it after start
        self.guests = guests  # accounts are on: no current user is a signed-out visitor

    def state(self) -> dict:
        from cbzwebp.web import current_user

        user = current_user()
        if self.guests and user is None:  # no folders, key names or notes for guests
            return {"harbor": {"version": __version__}}
        from .config import watch_list

        defaults = {
            **self.settings.page_defaults(),
            "convert_downloads": True,
            "watching": len(watch_list(self.settings)),
        }
        if user:  # the signed-in user's own defaults win over the .env ones
            prefs = user.get("prefs", {})
            defaults.update({k: prefs[k] for k in USER_PAGE_PREFS if k in prefs})
        return {"harbor": {"version": __version__, **defaults, "notes": self.notes}}

    def post(self, action: str, body: dict) -> tuple[int, dict]:
        return 404, {"error": "not found"}


BOOK_SUFFIXES = (".zip", ".cbz", ".epub")


def library_matches(library: Library, urls: list, libraries=None) -> list[dict]:
    """Books already in the library (in ``libraries``, None for all) whose file name, ignoring
    .zip/.cbz/.epub, is the one a URL would be saved as. Only a warning: the download still runs."""
    from urllib.parse import urlsplit

    from dlqueue.download import safe_name

    found = []
    for url in urls:
        name = Path(safe_name(urlsplit(str(url)).path))
        if name.suffix.lower() not in BOOK_SUFFIXES or not name.stem.strip():
            continue  # a name the server will choose, such as /download?id=3: can't tell yet
        try:
            items = library.search(q=name.stem, limit=50, libraries=libraries)["items"]
        except ValueError:  # no letters or digits to search for
            continue
        for b in items:
            if Path(b["file"]).stem.lower() == name.stem.lower():
                found.append({"url": str(url), "book": b["title"], "file": b["file"], "id": b["id"]})
                break
    return found


class LibraryCheckDownloadAPI:
    """The download API, adding ``in_library`` to an add's answer: the URLs whose file the library
    already has, among the libraries the user may see."""

    def __init__(self, inner, library: Library, visible=None):
        self.inner, self.library, self.visible = inner, library, visible

    def state(self) -> dict:
        return self.inner.state()

    def post(self, action: str, body: dict):
        status, result, *extra = self.inner.post(action, body)
        if action == "add" and status == 200 and isinstance(body.get("urls"), list):
            try:
                libraries = self.visible() if self.visible else None
                result = {
                    **result,
                    "in_library": library_matches(self.library, body["urls"][:500], libraries),
                }
            except Exception as e:  # noqa: BLE001 - a warning must never fail an add
                print(f"harbor: couldn't check the library for duplicates: {e}", flush=True)
        return (status, result, *extra)


class AuditedLibraryAPI:
    """cbz-shelf's LibraryAPI, recording folder and library changes in the audit log."""

    EVENTS: ClassVar[dict[str, str]] = {
        "add-root": "library folder added",
        "remove-root": "library folder removed",
        "move-root": "library folder moved",
        "remove-missing": "missing books forgotten",
        "add-library": "library added",
        "rename-library": "library renamed",
        "remove-library": "library removed",
    }

    def __init__(self, inner, accounts, library: Library):
        self.inner, self.accounts, self.library = inner, accounts, library

    def state(self) -> dict:
        return self.inner.state()

    def get(self, action: str, query: dict):
        return self.inner.get(action, query)

    def post(self, action: str, body: dict):
        from cbzwebp.web import current_user

        event = self.EVENTS.get(action)
        if event is None:
            return self.inner.post(action, body)
        roots = {r["id"]: r["path"] for r in self.library.roots()}
        libs = {x["id"]: x["name"] for x in self.library.libraries(counts=False)}
        status, result, *extra = self.inner.post(action, body)
        if status == 200:
            if action == "add-root":
                target, detail = result.get("path", ""), libs.get(result.get("library_id"), "")
            elif action in ("remove-root", "move-root"):
                target = roots.get(_int_or(body.get("id")), "")
                detail = libs.get(_int_or(body.get("library_id")), "") if action == "move-root" else ""
            elif action == "remove-missing":
                target, detail = (
                    roots.get(_int_or(body.get("root_id")), "all folders"),
                    f"{result.get('removed')} book(s)",
                )
            elif action == "add-library":
                target, detail = result.get("name", ""), ""
            elif action == "rename-library":
                target, detail = libs.get(_int_or(body.get("id")), ""), f"now {result.get('name', '')}"
            else:
                target, detail = libs.get(_int_or(body.get("id")), ""), ""
            self.accounts.audit(event, target, detail, actor=current_user())
        return (status, result, *extra)


def _int_or(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


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
    owners=None,
    history=None,
) -> ThreadingHTTPServer:
    """The page: convert at /api/, downloads at /api/download/, page defaults at /api/harbor/, and
    with a library, cbz-shelf's API at /api/library/ and its page at /library (the Library tab).
    With ``accounts``, converting, downloading, favourites and the rest need a signed-in user
    (``/api/account/``); signed out, the page and the library can be read (``perms.guest_may``)."""
    from .accounts import AccountAPI, HarborSignIn
    from .admin import AdminAPI
    from .perms import GuardedQueueAPI, GuestSafeLibraryAPI, JobOwners, can, guest_can, make_visible

    settings = settings or Settings()
    owners = owners if owners is not None else JobOwners()
    guests = accounts is not None
    apis = {
        "/api/": GuardedQueueAPI(
            ConvertAPI(convert_queue, {"quality": settings.quality, "method": settings.method}),
            convert_queue,
            "convert",
            owners,
            guests,
        ),
        "/api/download/": GuardedQueueAPI(
            EnvKeyDownloadAPI(download_queue, settings.api_key), download_queue, "download", owners, guests
        ),
        "/api/harbor/": HarborAPI(settings, notes, guests),
    }
    if history is not None:
        from .history import HistoryAPI

        apis["/api/history/"] = HistoryAPI(history, guests)
    pages = {}
    if library is not None:
        visible = make_visible(accounts, library) if accounts is not None else None
        apis["/api/download/"] = LibraryCheckDownloadAPI(apis["/api/download/"], library, visible)
        library_api = LibraryAPI(library, user_of=_user_id, can=guest_can if guests else can, visible=visible)
        if guests:
            library_api = GuestSafeLibraryAPI(AuditedLibraryAPI(library_api, accounts, library))
        apis["/api/library/"] = library_api
        pages["/library"] = LIBRARY_HTML
    auth = None
    if accounts is not None:
        apis["/api/account/"] = AccountAPI(accounts)
        apis["/api/admin/"] = AdminAPI(accounts, library)
        pages["/login"] = LOGIN_HTML
        auth = HarborSignIn(accounts)
    from .config import host_list

    return serve(
        port,
        UI_HTML,
        apis,
        ARCHIVE_EXTS,
        settings.allowed_origins(),
        pages,
        auth=auth,
        host=settings.host,
        allowed_hosts=host_list(settings),
        ssl_context=tls_context(settings),
        trust_proxy=settings.trust_proxy,
    )


def tls_context(settings: Settings):
    """The HTTPS context from HARBOR_TLS_CERT/KEY, or None."""
    if not settings.tls_cert:
        return None
    import ssl

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(Path(settings.tls_cert).expanduser(), Path(settings.tls_key).expanduser())
    return ctx
