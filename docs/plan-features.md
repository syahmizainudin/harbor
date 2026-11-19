# Plan: library tabs, LAN access, OPDS, job history and smaller additions

Status: decisions settled 2026-11-19. Nothing built yet.

## What was asked

- Admins can split the library into several libraries, each with its own folders, shown as sub-tabs inside the Library tab (All · Manga · Novels).
- Admins choose which roles can see each library.
- Reading from other devices on the LAN, over HTTPS: harbor serving TLS itself, or behind a reverse proxy (both supported).
- An OPDS feed, so reader apps (Panels, Chunky, KOReader) can browse the library.
- Job history per user, with the space conversion saved.
- Smaller: a duplicate warning before downloading, `harbor backup`, `harbor doctor`, a list of your signed-in sessions, and an admin audit log.
- In the tools: download speed limit, quiet hours and URL ranges (dl-queue), and a watch folder (cbz-webp).
- Way of working: build phase by phase, commit to `main` in each repo with tests passing, and ask before every tag.

Not chosen: the "Read" button on finished downloads (download → convert → book link).

## Decisions (settled 2026-11-19)

### Libraries

1. **A library tab is a named library with one or more folders.** The admin creates "Manga" and adds `/mnt/d/Manga` and `/mnt/e/Manga2` to it, so folders on two drives can share one tab. (Not chosen: one folder per tab.)
2. **Every folder belongs to exactly one library.** Folders still can't overlap (today's rule).
3. **Migration**: cbz-shelf migration step 6 creates the `libraries` table and a library called "Library", and attaches every existing folder to it. Nothing about books, progress or edits changes.
4. **Removing a library is refused while it has folders.** Removing a folder deletes its books, progress and edits from the database, so that stays a separate and deliberate step.
5. **The "All" sub-tab** shows every library the user can see. Each library's sub-tab shows only its own books, and its Continue reading, favourites, search, tags and authors are limited to it too. With only one library, the sub-tab bar is hidden and the page looks like today.
6. **Who sees what (harbor).** By default, a library is visible to every role with `library.read`. An admin can narrow it to some roles. Admin always sees everything. Each library also has a **Visible signed out** flag for guests, which starts on so nothing changes on upgrade. With LAN access, this flag is how the admin controls what other devices can read without an account.
7. **Where the access map lives.** Roles are in harbor's database, and libraries are in cbz-shelf's. Harbor stores the map (`library_access(role_id, library_id)`, plus `guest_libraries`) as a migration in `accounts.MIGRATIONS`. cbz-shelf only takes a hook: `LibraryAPI(visible=...)`, which returns the set of library ids the request may see, or None for all. Harbor computes that set once per request, from the user dict, the same way it computes `perms`. Deleting a role drops its rows. A library id that no longer exists is ignored, and the next save of the map cleans it up.
8. **Enforcement is in cbz-shelf's queries, not only the page.** A book or series in a hidden library answers **404** (not 403, so ids don't reveal that it exists) for `series-detail`, `book`, `page`, `thumb`, `chapter`, `res`, `progress`, `favourite`, `reader-settings`, `hide-continue` and `edit-*`. `series`, `search`, `continue`, `facets` and `summary` filter by the visible libraries. Today `summary()` and `facets()` are global, and would leak hidden counts and tags.
9. **Names without paths.** A new `GET libraries` action (needs `library.read`) returns `[{id, name, books}]` for the visible libraries, for the sub-tab bar. `roots` keeps paths behind `library.manage`.
10. **Which page gets what.** In cbz-shelf's page (framed by harbor): the sub-tab bar, plus creating, renaming, reordering and removing libraries and choosing each folder's library in the Folders window (`library.manage`). In harbor's Admin tab: a Libraries section with a libraries × roles matrix and the guest column (`roles.manage`, under the no-escalation rule: you can only grant access to libraries you can see yourself).

### LAN access and HTTPS

11. **The shared server learns about hosts** (cbz-webp's `web.py` and dl-queue's identical copy, released together). `serve()` gets:
    - `host` (bind address, default `127.0.0.1`);
    - `allowed_hosts` (names accepted in `Host`, besides `127.0.0.1` and `localhost`);
    - `ssl_context` (wraps the listening socket);
    - `trust_proxy`: `X-Forwarded-Proto` and `X-Forwarded-Host` are believed only when the connection comes from `127.0.0.1`.

    The Origin check then compares scheme and host against what the request really used, `https` included. Standalone tools keep their defaults and behave exactly as today.
12. **Harbor's settings**:
    - `HARBOR_HOST` (default `127.0.0.1`), `HARBOR_ALLOWED_HOSTS` (comma-separated names);
    - `HARBOR_TLS_CERT` and `HARBOR_TLS_KEY` (both or neither, files must exist, key file mode 0600);
    - `HARBOR_TRUST_PROXY` (`true`/`false`).
13. **Harbor refuses to start** on any address other than loopback unless TLS is configured (or `HARBOR_TRUST_PROXY` is on with a loopback bind), `HARBOR_SECURE_COOKIE` is on, accounts are on, and `HARBOR_ALLOWED_HOSTS` is set. The message names what's missing. This is plan-users decision 3.
14. **WSL**: the README gets a section on making WSL reachable from the LAN (mirrored networking in `.wslconfig`, or a `netsh interface portproxy` rule and a Windows firewall rule), and on getting a certificate (mkcert, or `tailscale cert`).
15. The page and the Chrome extension don't change. The extension stays on `localhost`.

### OPDS

16. **OPDS serves feeds, page streaming and whole-file downloads.** Navigation feeds (libraries → series → books), search (OpenSearch), OPDS-PSE links to the existing `page` endpoint, and an acquisition link that downloads the `.cbz` or `.epub` for offline reading.
    - **Downloads are streamed, never read into memory.** The shared server's `get` may return a file object as the body, sent in chunks with `Content-Length`, and supports a single `Range` (resumable downloads). This is a shared-code change, so it goes into phase 3's cbz-webp and dl-queue releases.
    - The file is found by book id. A path from a request is never opened (cbz-shelf rule), and a missing file answers 410.
17. **URLs** stay flat (`/api/library/opds?feed=series&id=12`), because the shared server's `get` routes refuse actions containing `/`. OPDS clients follow the links the feed gives them, so the URL shape doesn't matter to them. The feed XML is built with `xml.etree` (standard library).
18. **Reader apps sign in with scoped tokens.** Reader apps use HTTP Basic, and the session cookie can't be used. **Tokens get a scope** (migration on `tokens`): `extension` (today's tokens) or `reader`, which is library read only. Basic auth is accepted only on OPDS and library GET paths, with your account name and a `reader` token as the password, never your account password. AGENTS.md's token rule becomes: an extension token can use the console, and a reader token can only read the library. (Not chosen: reusing extension tokens, which would let a reader app on a phone queue downloads, or the account password.)
19. OPDS obeys the same library visibility (decision 8) and the guest flag (signed out: only guest-visible libraries, if `library.read` is allowed for guests).

### Job history

20. **One record per finished job**: written once, when a job first reaches done, skipped, failed or cancelled. The record holds the kind (convert or download), the owner, the path or URL, the output, the result and message, `bytes_in`/`bytes_out`, and times. `on_change` fires on every progress tick, so the hook checks the transition. Records are written before `clear_finished` or shutdown drop the job. `QueueStore` only keeps queued and running jobs, so history is separate from it.
21. **Stored in its own database**, `HARBOR_HISTORY_DB` (default `~/.local/share/harbor/history.db`). This works the same with `HARBOR_ACCOUNTS=false`, where the owner is null. Records older than `HARBOR_HISTORY_DAYS` (default 90, 0 = keep forever) are deleted at start-up and once a day.
22. **Who sees it**: your own records. With `jobs.see_all`, everyone's. A **History** section on the Convert and Download tabs with filters, and a "Saved N GB" total on Convert (all-time and last 30 days). The API key never goes into a record, and download URLs are stored without their query string when the auth mode is `query`.

### Smaller additions (harbor only)

23. **Duplicate warning.** When a download is added, harbor checks whether the library already has a book whose file stem matches the URL's file name (also comparing as `.cbz`). The download is still queued, and the answer and the page show a warning naming the book. Only libraries the user can see are checked.
24. **`harbor backup [DIR]`.** Copies `harbor.db`, `library.db`, `history.db` and the state file using SQLite's online backup API, into a timestamped folder (default `~/.local/share/harbor/backups/`, mode 0700), and keeps the newest `--keep` (default 7). It never touches the live files. `--restore` is not part of this; restoring means stopping harbor and copying the files back, as the README will explain.
25. **`harbor doctor`.** Read-only checks: dependency versions against the pins, the `.env` file's mode (warns if not 0600), whether each database opens and its migration version, whether library folders exist, TLS files and the LAN rules in decision 13, and whether the port is free or harbor is already answering on it. Exit code 1 if anything fails.
26. **Your sessions.** A migration adds `created_at`, `last_used_at` and `user_agent` (trimmed, 200 chars) to sessions, and `last_used_at` to tokens. Your account lists sessions (this one marked) and tokens with last use, and lets you sign out any one of them. `last_used_at` is written at most once a minute per session, to keep the 1 s poll cheap.
27. **Audit log.** An `audit` table in `harbor.db`. It records:
    - sign-ins, failed sign-ins and lockouts;
    - password changes and resets;
    - users added, removed, enabled, disabled or given another role;
    - role and permission changes, and library access changes;
    - tokens created and revoked;
    - library folders and libraries added or removed.

    It records who acted, on what, when, and the client address, never a password, session id or token. Admin tab section, `users.manage`. Same retention setting as history (`HARBOR_AUDIT_DAYS`, default 365).

### Tool changes

28. **dl-queue: speed limit.** One token bucket shared by all downloads, `rate` in bytes/s (0 = off). Harbor setting `HARBOR_DOWNLOAD_RATE` (accepts `5M`, `500K`).
29. **dl-queue: quiet hours.** `active_hours`, such as `01:00-07:00` (may cross midnight). Outside them, no new download starts. Running ones finish. Harbor setting `HARBOR_DOWNLOAD_HOURS`. The page shows "Waiting until 01:00".
30. **dl-queue: URL ranges.** `DownloadAPI.add` expands `{1..20}` and `{01..20}` (zero-padded) in URLs, at most one range per URL and at most 500 URLs per add, otherwise refused. The extension benefits too.
31. **cbz-webp: watch folders.** `Watcher(queue, folders, poll=30)`, standard library polling (no inotify). A file is queued once its size and mtime haven't changed for two polls. A `(path, mtime)` that failed or was skipped is remembered and not queued again until the file changes. Harbor settings: `HARBOR_WATCH_DIRS`, and `HARBOR_WATCH_DELETE_ORIGINAL`, **default `false` whatever the Convert default is**, because nobody clicks anything before a watched file is converted. With accounts on, watch-folder jobs have no owner, so only `jobs.see_all` sees them.

## Phases (each one shippable; ask before every tag)

| # | What | Repos and releases | Harbor pins |
|---|---|---|---|
| 1 | Libraries and sub-tabs (1–10) | cbz-shelf 0.5.0, then harbor | cbz-shelf 0.5.0 |
| 2 | Job history, sessions list, audit log, backup, doctor, duplicate warning (20–27) | harbor only | — |
| 3 | LAN and HTTPS (11–15), and streamed file bodies for OPDS downloads (16) | cbz-webp 0.5.0 and dl-queue 0.5.0 together (shared code), then harbor | cbz-webp, dl-queue 0.5.0 |
| 4 | OPDS and reader tokens (16–19) | cbz-shelf 0.5.1, then harbor | cbz-shelf 0.5.1 |
| 5 | Speed limit, quiet hours, URL ranges, watch folders (28–31) | dl-queue 0.5.1, cbz-webp 0.5.1, then harbor | both |

Phase 1 doesn't depend on the server changes, so it goes first, as the first thing you asked for. OPDS (4) is most useful once LAN access (3) exists. Phase 3 is the gate for phone access. Phases 2 and 5 can be moved without affecting the others.

For each phase: tests and lint in every repo touched, the carried-over page changes, a CHANGELOG `## Unreleased` entry, the README and AGENTS.md updated, and a commit to `main`. Then I stop and ask before tagging. Harbor's pin bump waits for the tool's tag.

## Constraints

- No new dependencies anywhere. OPDS XML, TLS, rate limiting and polling all come from the standard library.
- A file download never reads the whole file into memory.
- Every schema change is a migration step, in cbz-shelf's `MIGRATIONS` or harbor's `accounts.MIGRATIONS`. Never drop user data. Test migrations on copies, never on the live databases.
- The shared server code stays identical in cbz-webp and dl-queue (`SharedCodeInStepTests`).
- Every new action that changes something shared, or reveals another user's data, gets a permission check and a case in `tests/test_permissions.py` (harbor) or cbz-shelf's tests. Visibility gets tests for every id-taking endpoint.
- `/api/state` is polled every second. Library visibility, session last-use and history totals must stay cheap per request.
- Tests use temp databases, temp folders and `127.0.0.1`. Never touch the real databases, folders, `.env` or the pm2 processes. After each phase, you run `pm2 restart harbor`.
