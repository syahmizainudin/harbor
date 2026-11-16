"""Command line: ``harbor ui`` starts the two-tab console."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import webbrowser

from . import __version__
from .app import build, serve_harbor
from .config import ConfigError, load_settings
from .state import QueueStore, default_state_file


def _quality(v: str) -> int:
    q = int(v)
    if not 1 <= q <= 100:
        raise argparse.ArgumentTypeError("quality must be 1-100")
    return q


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="harbor",
        description="Download files and convert .zip/.cbz archives to WebP .cbz, from one local web page.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser(
        "ui",
        help="start the local web console",
        description="Flags override environment variables, which override the .env file. "
        "Without --env-file, harbor reads $HARBOR_ENV_FILE, ./.env or ~/.config/harbor/.env if present.",
    )
    # Defaults are None so a flag that wasn't given doesn't override the .env file.
    u.add_argument("--env-file", help="settings file to read (default: see above)")
    u.add_argument("-p", "--port", type=int, help="port on 127.0.0.1 (default 8765)")
    u.add_argument("-q", "--quality", type=_quality, help="default WebP quality 1-100 (default 80)")
    u.add_argument("-j", "--jobs", type=int, help="archives converted at the same time (default 1)")
    u.add_argument(
        "-t",
        "--threads",
        type=int,
        help=f"image-encoding threads for all conversions (default {os.cpu_count()})",
    )
    u.add_argument("-d", "--downloads", type=int, help="downloads at the same time (default 2)")
    u.add_argument("--open", action="store_true", help="open the console in a browser")
    return ap


def cmd_ui(args) -> int:
    flags = {k: getattr(args, k) for k in ("port", "quality", "jobs", "threads", "downloads")}
    try:
        for k in ("jobs", "threads", "downloads"):
            if flags[k] is not None and flags[k] < 1:
                raise ConfigError(f"--{k} must be at least 1")
        settings = load_settings(flags, args.env_file)
    except (ConfigError, OSError) as e:
        print(f"harbor: {e}", file=sys.stderr)
        return 2

    notes: list[str] = []  # restore notes, shown on the page
    store = QueueStore(
        Path(settings.state_file).expanduser() if settings.state_file else default_state_file()
    )
    cq, dq = build(
        jobs=settings.jobs,
        image_threads=settings.threads,
        downloads=settings.downloads,
        retries=settings.retries,
        on_change=store.on_change,
    )
    store.attach(cq, dq)

    def stop_queues() -> None:
        # Save what's unfinished first: shutting down marks running jobs cancelled.
        store.save()
        store.enabled = False
        dq.shutdown()  # before the converter, so no download hands it a new job
        cq.shutdown()

    try:
        server = serve_harbor(settings.port, cq, dq, settings, notes)
    except OSError as e:
        print(f"harbor: cannot listen on 127.0.0.1:{settings.port}: {e}", file=sys.stderr)
        store.enabled = False  # keep the saved jobs for the next start
        dq.shutdown()
        cq.shutdown()
        return 2
    notes[:] = store.restore(settings.api_key) if settings.restore else []
    for note in notes:
        print(f"harbor: {note}", flush=True)
    url = f"http://127.0.0.1:{settings.port}/"
    print(f"harbor console: {url}  (Ctrl+C to stop; running jobs are cancelled, originals kept)", flush=True)
    if settings.source:
        print(
            f"settings from {settings.source}{' (includes an API key)' if settings.api_key else ''}",
            flush=True,
        )

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    finally:
        server.server_close()
        stop_queues()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_ui(args)


if __name__ == "__main__":
    sys.exit(main())
