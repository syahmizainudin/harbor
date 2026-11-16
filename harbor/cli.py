"""Command line: ``harbor ui`` starts the two-tab console."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import webbrowser

from . import __version__
from .app import build, serve_harbor


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
    u = sub.add_parser("ui", help="start the local web console")
    u.add_argument("-p", "--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    u.add_argument(
        "-q", "--quality", type=_quality, default=80, help="default WebP quality 1-100 (default 80)"
    )
    u.add_argument(
        "-j", "--jobs", type=int, default=1, help="archives converted at the same time (default 1)"
    )
    u.add_argument(
        "-t",
        "--threads",
        type=int,
        default=os.cpu_count(),
        help=f"image-encoding threads shared by all conversions (default {os.cpu_count()})",
    )
    u.add_argument("-d", "--downloads", type=int, default=2, help="downloads at the same time (default 2)")
    u.add_argument("--open", action="store_true", help="open the console in a browser")
    return ap


def cmd_ui(args) -> int:
    if min(args.jobs, args.threads or 1, args.downloads) < 1:
        print("harbor: --jobs, --threads and --downloads must be at least 1", file=sys.stderr)
        return 2
    cq, dq = build(jobs=args.jobs, image_threads=args.threads, downloads=args.downloads)
    try:
        server = serve_harbor(args.port, cq, dq, {"quality": args.quality})
    except OSError as e:
        print(f"harbor: cannot listen on 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        dq.shutdown()
        cq.shutdown()
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    print(f"harbor console: {url}  (Ctrl+C to stop; running jobs are cancelled, originals kept)", flush=True)

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
        dq.shutdown()  # first, so no download hands a new job to a stopping converter
        cq.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_ui(args)


if __name__ == "__main__":
    sys.exit(main())
