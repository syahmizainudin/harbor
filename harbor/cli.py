"""Command line: ``harbor ui`` starts the console; ``harbor user ...`` manages accounts."""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
from pathlib import Path
import signal
import sys
import webbrowser

from . import __version__
from .app import (
    build,
    default_library_cache,
    default_library_db,
    make_library_file_hook,
    make_library_hook,
    serve_harbor,
)
from cbzshelf.library import Library

from .accounts import Accounts, default_accounts_db
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

    us = sub.add_parser("user", help="manage accounts (who can sign in to the console)")
    us.add_argument("--env-file", help="settings file to read, for HARBOR_ACCOUNTS_DB")
    usub = us.add_subparsers(dest="user_cmd", required=True)
    a = usub.add_parser("add", help="add an account (asks for its password)")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true", help="make it an admin")
    a.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    pw = usub.add_parser("passwd", help="set an account's password (signs it out everywhere)")
    pw.add_argument("name")
    pw.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    usub.add_parser("list", help="list accounts")
    for cmd, text in (
        ("disable", "stop an account signing in (ends its sessions and tokens)"),
        ("enable", "let a disabled account sign in again"),
        ("remove", "delete an account; its reading progress stays in the library database"),
    ):
        usub.add_parser(cmd, help=text).add_argument("name")
    return ap


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Again: ") != first:
        raise ValueError("the passwords don't match")
    return first


def _accounts_path(settings) -> Path:
    return Path(settings.accounts_db).expanduser() if settings.accounts_db else default_accounts_db()


def cmd_user(args) -> int:
    try:
        settings = load_settings({}, args.env_file)
        accounts = Accounts(_accounts_path(settings))
    except (ConfigError, OSError, sqlite3.Error) as e:
        print(f"harbor: {e}", file=sys.stderr)
        return 2
    try:
        c = args.user_cmd
        if c == "add":
            first = accounts.count() == 0
            user = accounts.add(args.name, _read_password(args.password_stdin), admin=args.admin)
            print(f"Added {user['name']}{' (admin)' if user['admin'] else ''}.")
            if first and not user["admin"]:
                print("Note: no admin yet. Add one with --admin to manage users later.")
        elif c == "passwd":
            accounts.get(args.name)
            accounts.set_password(args.name, _read_password(args.password_stdin))
            print(f"Password changed for {args.name}; signed out everywhere.")
        elif c == "list":
            for u in accounts.users():
                flags = [f for f, on in (("admin", u["admin"]), ("disabled", u["disabled"])) if on]
                print(u["name"] + (f"  ({', '.join(flags)})" if flags else ""))
            if not accounts.count():
                print("No accounts yet: harbor user add NAME --admin")
        elif c in ("disable", "enable"):
            accounts.set_disabled(args.name, c == "disable")
            print(f"{args.name} {'disabled' if c == 'disable' else 'enabled'}.")
        elif c == "remove":
            accounts.remove(args.name)
            print(f"Removed {args.name}.")
    except (ValueError, KeyError) as e:
        print(f"harbor: {str(e).strip(chr(39) + chr(34))}", file=sys.stderr)
        return 2
    finally:
        accounts.close()
    return 0


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
    try:
        library = Library(
            Path(settings.library_db).expanduser() if settings.library_db else default_library_db(),
            default_library_cache(),
        )
    except (OSError, sqlite3.Error) as e:
        print(f"harbor: cannot open the library database: {e}", file=sys.stderr)
        return 2
    store = QueueStore(
        Path(settings.state_file).expanduser() if settings.state_file else default_state_file()
    )
    cq, dq = build(
        jobs=settings.jobs,
        image_threads=settings.threads,
        downloads=settings.downloads,
        retries=settings.retries,
        on_change=store.on_change,
        on_convert_change=make_library_hook(library),
        on_download_kept=make_library_file_hook(library),
    )
    store.attach(cq, dq)

    def stop_queues() -> None:
        # Save what's unfinished first: shutting down marks running jobs cancelled.
        store.save()
        store.enabled = False
        dq.shutdown()  # before the converter, so no download hands it a new job
        cq.shutdown()
        library.close()  # last: a finishing conversion may still add a book

    accounts = None
    import cbzwebp.web

    if settings.accounts and not hasattr(cbzwebp.web, "current_user"):
        # Dependencies older than the pins (cbz-webp < 0.4.0): no sign-in support. Say so loudly
        # rather than crash-looping under pm2; reinstalling the dependencies fixes it.
        print(
            "harbor: WARNING: the installed cbz-webp can't require sign-in, so accounts are OFF. "
            "Reinstall the dependencies (pip install --target .deps .) and restart.",
            flush=True,
        )
    elif settings.accounts:
        try:
            accounts = Accounts(_accounts_path(settings), settings.session_days, settings.secure_cookie)
        except (OSError, sqlite3.Error) as e:
            print(f"harbor: cannot open the accounts database: {e}", file=sys.stderr)
            dq.shutdown()
            cq.shutdown()
            library.close()
            return 2
        admin = accounts.first_admin()
        if admin is None:
            print("harbor: no admin account yet. Run: harbor user add NAME --admin", flush=True)
        elif hasattr(library, "give_user_data") and (moved := library.give_user_data(0, admin["id"])):
            # Reading progress and favourites from before accounts belong to the first admin.
            print(
                f"harbor: gave {moved} reading record(s) from before accounts to {admin['name']}", flush=True
            )
    try:
        server = serve_harbor(settings.port, cq, dq, settings, notes, library, accounts)
    except OSError as e:
        print(f"harbor: cannot listen on 127.0.0.1:{settings.port}: {e}", file=sys.stderr)
        store.enabled = False  # keep the saved jobs for the next start
        dq.shutdown()
        cq.shutdown()
        library.close()
        return 2
    if settings.library_scan and library.roots():
        library.scan()  # picks up what changed while harbor was stopped; runs in the background
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
        if accounts:
            accounts.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_user(args) if args.cmd == "user" else cmd_ui(args)


if __name__ == "__main__":
    sys.exit(main())
