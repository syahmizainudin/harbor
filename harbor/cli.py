"""Command line: ``harbor ui`` starts the console; ``harbor user ...`` manages accounts;
``harbor backup`` and ``harbor doctor`` back up and check its files."""

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
    missing_dependencies,
    serve_harbor,
)
from cbzshelf.library import Library

from .accounts import Accounts, default_accounts_db
from .config import ConfigError, check_reach, host_list, load_settings, watch_list
from .history import History, default_history_db
from .perms import JobOwners
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
    a.add_argument("--admin", action="store_true", help="make it an admin (the Admin role)")
    a.add_argument("--role", help="its role (default Member; see `harbor user roles`)")
    a.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    pw = usub.add_parser("passwd", help="set an account's password (signs it out everywhere)")
    pw.add_argument("name")
    pw.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    usub.add_parser("list", help="list accounts")
    usub.add_parser("roles", help="list roles and their permissions")
    r = usub.add_parser("role", help="give an account another role")
    r.add_argument("name")
    r.add_argument("role")
    for cmd, text in (
        ("disable", "stop an account signing in (ends its sessions and tokens)"),
        ("enable", "let a disabled account sign in again"),
        ("remove", "delete an account; its reading progress stays in the library database"),
    ):
        usub.add_parser(cmd, help=text).add_argument("name")
    b = sub.add_parser(
        "backup",
        help="copy harbor's databases and saved jobs to a new backup folder (safe while harbor runs)",
    )
    b.add_argument("--env-file", help="settings file to read, for where the databases are")
    b.add_argument(
        "--to", metavar="DIR", help="where to make backups (default ~/.local/share/harbor/backups)"
    )
    b.add_argument(
        "--keep", type=int, default=7, help="newest backups to keep in that folder; 0 keeps all (default 7)"
    )
    d = sub.add_parser("doctor", help="check settings, dependencies, databases and folders (changes nothing)")
    d.add_argument("--env-file", help="settings file to read")
    return ap


def cmd_backup(args) -> int:
    from .maint import backup

    try:
        if args.keep < 0:
            raise ConfigError("--keep must be 0 or more")
        folder, done = backup(load_settings({}, args.env_file), Path(args.to) if args.to else None, args.keep)
    except (ConfigError, OSError, sqlite3.Error) as e:
        print(f"harbor: {e}", file=sys.stderr)
        return 2
    for line in done:
        print(line)
    print(f"Backed up {len(done)} file(s) to {folder}" if done else f"Nothing to back up yet; made {folder}")
    return 0


def cmd_doctor(args) -> int:
    from .maint import FAIL, doctor

    try:
        settings = load_settings({}, args.env_file)
    except (ConfigError, OSError) as e:
        print(f"FAIL  settings: {e}")
        return 1
    results = doctor(settings)
    for level, message in results:
        print(f"{level:<4}  {message}")
    failed = sum(level == FAIL for level, _ in results)
    print(f"\n{failed} problem(s) found." if failed else "\nNo problems found.")
    return 1 if failed else 0


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Again: ") != first:
        raise ValueError("the passwords don't match")
    return first


CLI = "command line"  # the audit log's actor for `harbor user ...`


def _path(setting: str, default: Path) -> Path:
    """A path setting, or ``default`` when it is empty."""
    return Path(setting).expanduser() if setting else default


def cmd_user(args) -> int:
    try:
        settings = load_settings({}, args.env_file)
        accounts = Accounts(
            _path(settings.accounts_db, default_accounts_db()), audit_days=settings.audit_days
        )
    except (ConfigError, OSError, sqlite3.Error) as e:
        print(f"harbor: {e}", file=sys.stderr)
        return 2
    try:
        c = args.user_cmd
        if c == "add":
            first = accounts.count() == 0
            if args.role:
                accounts.role(args.role)  # unknown role: fail before asking for a password
            password = _read_password(args.password_stdin)
            user = accounts.add(args.name, password, admin=args.admin, role=args.role)
            accounts.audit("user added", user["name"], f"role {user['role']}", actor=CLI)
            print(f"Added {user['name']} ({user['role']}).")
            if first and not user["admin"]:
                print("Note: no admin yet. Add one with --admin to manage users later.")
        elif c == "passwd":
            accounts.get(args.name)
            accounts.set_password(args.name, _read_password(args.password_stdin))
            accounts.audit("password reset", args.name, "signed out everywhere", actor=CLI)
            print(f"Password changed for {args.name}; signed out everywhere.")
        elif c == "list":
            for u in accounts.users():
                print(f"{u['name']}  ({u['role']}{', disabled' if u['disabled'] else ''})")
            if not accounts.count():
                print("No accounts yet: harbor user add NAME --admin")
        elif c == "roles":
            for r in accounts.roles():
                perms = "every permission" if r["locked"] else ", ".join(r["perms"]) or "nothing"
                print(f"{r['name']}  ({r['members']} member(s)): {perms}")
        elif c == "role":
            before = accounts.get(args.name)["role"]
            user = accounts.set_role(args.name, args.role)
            accounts.audit("role given", user["name"], f"{before} → {user['role']}", actor=CLI)
            print(f"{user['name']} is now {user['role']}.")
        elif c in ("disable", "enable"):
            accounts.set_disabled(args.name, c == "disable")
            accounts.audit(f"user {c}d", args.name, actor=CLI)
            print(f"{args.name} {'disabled' if c == 'disable' else 'enabled'}.")
        elif c == "remove":
            accounts.remove(args.name)
            accounts.audit("user removed", args.name, actor=CLI)
            print(f"Removed {args.name}.")
    except (ValueError, KeyError) as e:
        print(f"harbor: {str(e).strip(chr(39) + chr(34))}", file=sys.stderr)
        return 2
    finally:
        accounts.close()
    return 0


def cmd_ui(args) -> int:
    missing = missing_dependencies()
    if missing:
        print(
            f"harbor: the installed dependencies are too old (missing {', '.join(missing)}). "
            "Reinstall them (pip install --target .deps .) and start again.",
            file=sys.stderr,
        )
        return 2
    flags = {k: getattr(args, k) for k in ("port", "quality", "jobs", "threads", "downloads")}
    try:
        for k in ("jobs", "threads", "downloads"):
            if flags[k] is not None and flags[k] < 1:
                raise ConfigError(f"--{k} must be at least 1")
        settings = load_settings(flags, args.env_file)
    except (ConfigError, OSError) as e:
        print(f"harbor: {e}", file=sys.stderr)
        return 2
    problems = check_reach(settings)
    if problems:
        for problem in problems:
            print(f"harbor: {problem}", file=sys.stderr)
        print('harbor: not started; see README, "Reading from other devices".', file=sys.stderr)
        return 2

    notes: list[str] = []  # restore notes, shown on the page
    try:
        library = Library(_path(settings.library_db, default_library_db()), default_library_cache())
    except (OSError, sqlite3.Error) as e:
        print(f"harbor: cannot open the library database: {e}", file=sys.stderr)
        return 2
    store = QueueStore(_path(settings.state_file, default_state_file()))
    owners = JobOwners()  # who queued each job, shared by the hand-off, the saved state and the API
    try:
        history = History(_path(settings.history_db, default_history_db()), settings.history_days, owners)
    except (OSError, sqlite3.Error) as e:
        print(f"harbor: cannot open the history database: {e}", file=sys.stderr)
        library.close()
        return 2

    def changed(job) -> None:
        store.on_change(job)
        history.on_change(job)

    cq, dq = build(
        owners=owners,
        jobs=settings.jobs,
        image_threads=settings.threads,
        downloads=settings.downloads,
        retries=settings.retries,
        on_change=changed,
        on_convert_change=make_library_hook(library),
        on_download_kept=make_library_file_hook(library),
        rate=settings.download_rate,
        hours=settings.download_hours,
    )
    store.attach(cq, dq, owners)

    watcher = None

    def close_all(save: bool) -> None:
        """Stop everything, in this order. ``save=False`` keeps the saved jobs from the last run."""
        if watcher:
            watcher.stop()  # first: nothing new is queued while stopping
        if save:
            store.save()  # first: shutting down marks running jobs cancelled
        store.enabled = False  # no save from here on
        history.enabled = False  # jobs cancelled by the shutdown are restored, not finished
        dq.shutdown()  # before the converter, so no download hands it a new job
        cq.shutdown()
        history.close()
        library.close()  # last: a finishing conversion may still add a book

    accounts = None
    if settings.accounts:
        try:
            accounts = Accounts(
                _path(settings.accounts_db, default_accounts_db()),
                settings.session_days,
                settings.secure_cookie,
                settings.audit_days,
            )
        except (OSError, sqlite3.Error) as e:
            print(f"harbor: cannot open the accounts database: {e}", file=sys.stderr)
            close_all(save=False)
            return 2
        admin = accounts.first_admin()
        if admin is None:
            print("harbor: no admin account yet. Run: harbor user add NAME --admin", flush=True)
        elif moved := library.give_user_data(0, admin["id"]):
            # Reading progress and favourites from before accounts belong to the first admin.
            print(
                f"harbor: gave {moved} reading record(s) from before accounts to {admin['name']}", flush=True
            )
    try:
        server = serve_harbor(settings.port, cq, dq, settings, notes, library, accounts, owners, history)
    except (OSError, ValueError) as e:  # ssl.SSLError is an OSError: a bad certificate or key
        print(f"harbor: cannot listen on {settings.host}:{settings.port}: {e}", file=sys.stderr)
        close_all(save=False)  # keep the saved jobs for the next start
        return 2
    if settings.library_scan and library.roots():
        library.scan()  # picks up what changed while harbor was stopped; runs in the background
    notes[:] = store.restore(settings.api_key) if settings.restore else []
    if watch_list(settings):
        from cbzwebp.watch import Watcher

        try:
            watcher = Watcher(
                cq,
                watch_list(settings),
                settings.quality,
                method=settings.method,
                delete_original=settings.watch_delete_original,
                poll=settings.watch_poll,
            ).start()
            kept = "deleted" if settings.watch_delete_original else "kept"
            print(f"harbor: watching {', '.join(map(str, watcher.folders))} (originals {kept})", flush=True)
        except ValueError as e:
            notes.append(f"Not watching folders: {e}")
            print(f"harbor: {e}; not watching folders", file=sys.stderr, flush=True)
    for note in notes:
        print(f"harbor: {note}", flush=True)
    url = f"http{'s' if settings.tls_cert else ''}://127.0.0.1:{settings.port}/"
    if settings.host not in ("127.0.0.1", "localhost") or settings.trust_proxy:
        names = ", ".join(host_list(settings))
        print(f"harbor: reachable from other devices as {names} (listening on {settings.host})", flush=True)
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
        close_all(save=True)
        if accounts:
            accounts.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"user": cmd_user, "backup": cmd_backup, "doctor": cmd_doctor}
    return commands.get(args.cmd, cmd_ui)(args)


if __name__ == "__main__":
    sys.exit(main())
