# AGENTS.md

Instructions for coding agents working in this repository. Read `README.md` for what harbor does for its users.

## What this repo is

Harbor serves two public tools on one two-tab local page and connects them: a finished download can be queued for conversion.

| Repo | What it is |
|---|---|
| `harbor` (this one, private) | The combined page, the download → convert hand-off, and the `harbor` command |
| `cbz-webp` (public) | The converter: `JobQueue`, `ConvertAPI`, and the generic server `serve` that harbor uses |
| `cbz-shelf` (public) | The library and reader: `Library`, `LibraryAPI` and its page, served at `/library` in harbor's Library tab |
| `dl-queue` (public) | The downloader: `DownloadQueue` with the `on_saved` and `check_after` hooks, `Auth`, `DownloadAPI` |

Both are dependencies in `pyproject.toml`, pinned by git tag.

| Path | What it is |
|---|---|
| `harbor/app.py` | `check_after` (validates a download's convert options), `make_on_saved` (the hand-off), `build` (starts both queues) and `serve_harbor` (mounts `ConvertAPI` at `/api/`, `EnvKeyDownloadAPI` at `/api/download/` and `HarborAPI`, page defaults only, at `/api/harbor/`) |
| `harbor/config.py` | Settings: `.env` parsing, file lookup, precedence (flag > environment > file > default), validation |
| `harbor/history.py` | `History` (finished jobs, `history.db`, written by its own thread) and `HistoryAPI` at `/api/history/` |
| `harbor/maint.py` | `harbor backup` and `harbor doctor` |
| `harbor/state.py` | `QueueStore`: saves unfinished jobs shortly after they change (changes batched, at most one save per `SAVE_DELAY`), restores them at start-up through the queues' `add` |
| `harbor/accounts.py` | `Accounts` (users, sessions, extension tokens), `HarborSignIn` (the server's `auth`), `AccountAPI` at `/api/account/` |
| `harbor/login.html` | The sign-in page, served at `/login` |
| `harbor/perms.py` | `GuardedQueueAPI` (permissions on the queue APIs), `JobOwners` (who queued each job), `can` |
| `harbor/admin.py` | `AdminAPI` at `/api/admin/`: users and roles, with the no-escalation rule |
| `docs/plan-users.md` | The plan for accounts, permissions and per-user settings, phase by phase |
| `harbor/extension.py` | The Chrome extension's fixed ID and origin |
| `extension/` | The Chrome extension (Manifest V3): `background.js` (menus, badge), `popup.*`, `harbor.js` (talks to harbor), icons rendered from the logo |
| `ecosystem.config.cjs` | pm2 config that keeps harbor running |
| `harbor/ui.html` | The two-tab page: one file, CSS and JS inline (including the logo and favicon), no external requests |
| `assets/logo.svg` | The logo. The page inlines it twice (header and favicon data URI); `LogoTests` checks they match |
| `.env.example` | Every setting, commented. Real `.env` files are git-ignored |
| `harbor/cli.py` | `harbor ui`, and `harbor user add/passwd/list/disable/enable/remove` |
| `bin/harbor` | Runs the CLI from a checkout |
| `tests/` | `test_harbor.py` (hand-off, console, CLI, shared-code check), `test_config.py` (settings, `.env` key, logo), `test_state.py` (saved jobs), `test_extension.py` (manifest, ID, allowed origin), `test_library_tab.py` (library mounted, conversions landing in it), `test_history.py`, `test_maint.py` (backup, doctor), `helpers.py` (archive builders and `FileServer`, copied from the two tools' tests) |
| `ci/release_notes.py`, `.github/workflows/` | Release gate, CI and release pipelines, as in the other two repos |

## Where a change belongs

- **Queue, file, download or server logic belongs in cbz-webp or dl-queue, not here.** Harbor holds only what connects the two. If a fix needs code in either tool:
  1. Make it there, with its tests.
  2. Release it there (tag `v<version>`).
  3. Bump the tag in this repo's `pyproject.toml`.
- **The page is harbor's own.** It is the union of the two tools' pages: the Convert tab is `cbzwebp/ui.html`'s body and the Download tab is `dlqueue/ui.html`'s, plus the tabs and the "Convert downloads" option. When either tool's page changes, carry the change over here by hand.
- **The page's JS reads these `/api/state` keys:** `jobs`, `paused`, `workers`, `image_threads`, `defaults` (from `ConvertAPI`) and `downloads`, `downloads_paused`, `download_workers` (from `DownloadAPI`). `test_page_and_merged_state` pins them. A download sends its convert options as `after: {convert, quality, delete_original}`, which `check_after` validates; `linked_id` on a download is its convert job's id.

## The rules of the two tools still apply

Downloads and conversions write and delete the user's files, and the API key is secret. Read the "deletes user files" rules in cbz-webp's AGENTS.md and the download safety rules in dl-queue's. In harbor specifically:

- The hand-off runs only after the download is complete and in place. A hand-off that fails (bad archive, duplicate) leaves the downloaded file where it is.
- On shutdown, stop the download queue before the convert queue, so no download hands work to a converter that is stopping.
- Never add the API key to `after`, a message or anything else that reaches `/api/state`.
- The `.env` key (`HARBOR_API_KEY`) stays on the server. `Settings.page_defaults()` exposes only `has_key`, and `Settings` hides `api_key` from `repr`. `EnvKeyDownloadAPI` adds it only when the page sends a mode other than `none` with an empty key. Tests check that the key never shows up in `/api/state`.
- Every setting goes through `config.VARS` with a validator. A bad value or a misspelled `HARBOR_` name stops startup with the file and setting named, rather than being silently ignored.
- Never commit a `.env` or a real key. Never read the user's real `.env` in tests; `LoadSettingsTests` changes into a temp folder and redirects `Path.home()`.

## The Library tab

- cbz-shelf's `LibraryAPI` is mounted at `/api/library/`, and its page at `/library`. Harbor's tab frames that page (`<iframe>`, loaded the first time the tab opens). This works because the shared server sends `X-Frame-Options: SAMEORIGIN`.
- `make_library_hook` is the convert queue's `on_convert_change`: a conversion that finishes inside a library folder is indexed at once with `Library.scan_file`. Conversions elsewhere are ignored. A full rescan runs at start-up (`HARBOR_LIBRARY_SCAN`).
- On shutdown, close the library last, after both queues.
- Library behaviour (scanning, metadata layers, the reader) belongs in cbz-shelf, not here; follow its AGENTS.md rules. Never point a test at the user's library database (`HARBOR_LIBRARY_DB`, default `~/.local/share/harbor/library.db`).

## Accounts (`accounts.py`)

- **Where it's checked.** cbz-webp's shared server takes `auth=HarborSignIn(accounts)`, so every page and API needs a signed-in user except `HarborSignIn.PUBLIC` (the sign-in page, `POST /api/account/login`, `GET /api/account/status`) and what a guest may do (`perms.guest_may`). `current_user()` (from `cbzwebp.web`) names the user inside an API. Never take a user id from a request body or query.
- **Guests** (signed out, accounts on) may read the library and nothing else: `GET /`, `/library`, `/api/state` and `/api/library/*`, with `perms.GUEST_PERMS` (`library.read`) through `guest_can`. Their reading progress, reader settings and Continue-reading changes are answered 200 but not saved (`GuestSafeLibraryAPI`), because a 401 there sends the reader to the sign-in page. Favourites and everything else answer 401. With accounts on, no current user means a guest, not "no accounts": `GuardedQueueAPI` and `HarborAPI` get `guests=True`, so a guest's `/api/state` has empty `jobs`/`downloads`, `harbor` holds only `version`, and `account` is `null`. A request carrying a token or an extension `Origin` is never a guest (the extension learns it needs a token from a 401). Don't widen the guest set without a test in `tests/test_accounts.py`.
- **Secrets.**
  - Passwords are stored only as scrypt hashes (`hash_password`). Session ids and extension tokens are random, and only their SHA-256 is stored.
  - Never log, print or put into `/api/state` a password, session id or token. `HarborSignIn.user` adds `_sid` to the user for sign-out and password changes; `AccountAPI.state` must never include it.
  - A token is shown once, in the answer to `token-create`.
- **Cookie.** `HttpOnly; SameSite=Strict; Path=/`, plus `Secure` with `HARBOR_SECURE_COOKIE` (HTTPS only). The Origin and JSON-only checks on POST still apply, the sign-in POST included.
- **Sign-in failures.** Wrong names and wrong passwords get the same answer, and an unknown name is checked against a dummy hash so timing doesn't tell. `LOCK_AFTER` failures for a name in `LOCK_WINDOW` lock it.
- **Tokens** are for the Chrome extension (`X-Harbor-Token` header). A token can use the console but can't manage the account: no password change, no new tokens.
- **No sign-up page.** The first account comes from `harbor user add NAME --admin`. The last enabled admin can't be disabled or removed.
- **Permissions** (`accounts.PERMISSIONS`, fixed list; roles in the `roles` table, Admin id 1 locked with all of them). `current_user()["perms"]` holds the request user's. Where each is checked:
  - `perms.GuardedQueueAPI` wraps ConvertAPI and DownloadAPI: `convert`/`download` to add, `delete_original` and `convert` for what an add would do afterwards, and own-jobs-only for cancel and retry without `jobs.see_all`, which also gates pause, cancel all and clear. `JobOwners` records who queued each job: API adds, the download → convert hand-off (`make_on_saved`), retries (they keep the original owner) and restores (`QueueStore` saves `owner`).
  - cbz-shelf's `LibraryAPI(can=perms.can)`: `library.read`, `library.edit`, `library.manage`.
  - `HarborSignIn.allows`: `/api/dirs` needs `convert`, `download` or `library.manage`.
  - `admin.AdminAPI`: `users.manage` and `roles.manage`, with the no-escalation rule: a non-admin hands out, edits and acts on only what is within their own permissions.
  - The page hides what you can't use (`applyPerms`), but the checks above are what count. A new action that changes something shared needs a permission check here, with a test in `tests/test_permissions.py`.
- **Per-user defaults** (`accounts.PREFS`, table `prefs`): validated, never the API key. `HarborSignIn.user` adds them to the user as `prefs`. `HarborAPI.state` merges `USER_PAGE_PREFS` over `page_defaults()`, and `GuardedQueueAPI` merges `quality` and `method` into the convert `defaults`, so the page and the extension get them without knowing about accounts. The page saves field changes with `POST /api/account/prefs` (browser sessions only).
- **Library access** (migration 3, `library_access`): no row means a library is open to every role with `library.read` and to guests. `roles_json` NULL is every role, a list names the only roles (Admin always sees all), and `guests` 0 hides it from guests. `Accounts.library_rules()` is cached in memory and dropped on every change; `remove_role` takes the role off every list. `perms.make_visible` is cbz-shelf's `LibraryAPI(visible=)`: it returns None when nothing is hidden from the request's user, so the common case costs no query. cbz-shelf enforces it (404 by id, filtered lists). `AdminAPI` `library-access` needs `roles.manage`; a non-admin may change only libraries they can see, and only roles within their own permissions. Library names and folders are managed in cbz-shelf's page (`library.manage`), not here. A new library is open to everyone until the admin narrows it.
- **Library data per user.** `LibraryAPI` gets `user_of=_user_id`. At start-up, `Library.give_user_data(0, first_admin)` hands pre-accounts progress and favourites to the first admin.
- **Schema changes** go in `accounts.MIGRATIONS`, one step each; never drop user data.
- **Tests** use a temp accounts database. Never point one at `~/.local/share/harbor/harbor.db`.

## History, audit log, backup and doctor

- **History** (`history.py`, its own database so it works with accounts off): one record per job, written when the job first reaches done, skipped, failed or cancelled. `on_change` fires on every progress tick, so `History.on_change` dedupes by `(kind, id) → finished_at` (a retry reuses the id) and queues the record for a writer thread, which waits for the owner with `JobOwners.get_settled`. A worker never waits on SQLite. `cli.close_all` sets `history.enabled = False` before shutting the queues down (cancelled-by-shutdown jobs are restored, not finished) and closes it after them. Never record `auth`. `HistoryAPI` shows your own records, everyone's with `jobs.see_all` (`all=1`), and nothing to guests (`guests=True`, and `/api/history/` isn't in `guest_may`). `history_saved` in `/api/state` is cached and dropped on every write.
- **Audit log** (`Accounts.audit`, table `audit`, migration 4): written by the API layers, which know the actor: `AccountAPI` (sign-in, failed sign-in, lockout, password, tokens, sessions), `AdminAPI` (every successful action), `AuditedLibraryAPI` (folders and libraries) and `cli.cmd_user` (actor "command line"). Never pass a password, session id or token as a target or detail. A new admin or account action gets an audit line and a check in `SessionsAndAuditTests`. Pruned once a day on write (`HARBOR_AUDIT_DAYS`).
- **Sessions**: `HarborSignIn.user` records the request's User-Agent in a thread-local (`request_agent()`), and `login` stores it. A session is named to its owner by the first `SESSION_HANDLE` hex characters of its id's hash; `revoke_session` matches that and the user id.
- **In-library warning** (`LibraryCheckDownloadAPI`, `library_matches`): adds `in_library` to a download add's answer, checking only the libraries the user can see. It never refuses an add, and a failure only logs.
- **`harbor backup`** only reads the live files (SQLite backup API from a read-only connection) and only deletes its own `YYYYmmdd-HHMMSS` folders beyond `--keep`. **`harbor doctor`** opens databases with `mode=ro`, so it never migrates them; it compares the dependency modules' `__version__` with the checkout's pins.

## Saved jobs (`state.py`)

- Never write the API key. Only `auth_mode` and `auth_name` are saved; restore uses the `.env` key or skips the download with a note.
- The shutdown order in `cli.cmd_ui` matters: save, disable the store, then stop the download queue, then the convert queue. `shutdown()` marks running jobs cancelled, and saving after that would drop them.
- Saves are batched: `on_change` only marks the store changed and schedules one save `SAVE_DELAY` later. That save re-checks `enabled` under the store's lock, and setting `enabled = False` cancels it, so nothing writes after shutdown disables the store. The snapshot is taken under the same lock as the write. `save()` (shutdown, restore) writes at once; `flush()` writes now if something changed, for tests that read the file right after a change.
- A job is saved with its owner. The batch delay lets an API add record its owners (`JobOwners.adding`) before the save runs.
- Only queued and running jobs are saved. Restoring removes a download's recorded `.part` first; resuming across restarts is not supported.
- Save with a temp file and `os.replace`, mode 0600. A file that won't parse is moved to `.bad`, never deleted.

## The Chrome extension

- Its ID comes from the `key` in `extension/manifest.json`, so it's the same wherever it's loaded. `harbor/extension.py` holds that ID, and `test_extension.py` recomputes it from the key. The private key was thrown away; it's only needed to pack a `.crx`, which isn't done. Never commit a private key.
- Harbor trusts the extension because of its exact `Origin`, which Chrome sets. Never widen `allowed_origins` to a pattern.
- The extension never handles the API key: it sends `key: ""` and harbor fills in the `.env` key.
- `host_permissions` are `http://localhost/*` and `http://127.0.0.1/*` (any port) and nothing more, and the popup refuses any other harbor address.
- Bump `version` in the manifest when you change the extension. A user reloads it from `chrome://extensions`.
- To test it in a browser, use Playwright's Chromium (`~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`) with `--load-extension`; branded Chrome ignores that flag. Run it against a throwaway harbor on a free port, never the user's.

## Never touch the user's real files while testing

- Run against temp folders and `helpers.FileServer` on `127.0.0.1` only, never a real URL, folder or API key.
- To try the console, start your own copy on a free port (`bin/harbor ui -p <port>`, with the dependencies on `PYTHONPATH`) and a temp `HARBOR_STATE_FILE`, and stop it by PID when done. Never point a test at the user's state file.
- The user's harbor runs under pm2 as `harbor` on 8765. Don't stop, restart or delete it, and never touch the `grove` pm2 process. After changing harbor, tell the user to run `pm2 restart harbor`.

## Test and lint

```bash
pip install --target .deps .        # once, or after bumping a pin; .deps is git-ignored
PYTHONPATH=.deps python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

To test against local checkouts of the two tools instead of the pinned tags: `pip install --target .deps ../cbz-webp ../dl-queue`.

`SharedCodeInStepTests` compares the code cbz-webp and dl-queue share by copy: `BaseQueue`, the generic server part of `web.py`, and `normalize_path`. If it fails after a bump, the two tools have drifted. Fix them there, not here.

## Rules for the code

- **No dependencies besides the two tools** (and Pillow through cbz-webp). Standard library otherwise; the page is plain JS with no CDN.
- **Python 3.10+.** CI tests 3.10 through 3.14.
- **The console stays local and guarded.** The guards live in the shared `serve` from cbz-webp. Don't add a handler here; add an action to an API object in the right tool instead.
- **The page must work offline and in dark mode.** `render()` rewrites only stats and table bodies every second; keep forms and the folder-picker `<dialog>` outside those.
- **To change the logo**, edit `assets/logo.svg`, then replace the inline `<svg>` in the page's `<h1>` and the favicon `href` (URL-encoded) with it. `LogoTests` fails until all three match.

## Releasing

1. Set `__version__` in `harbor/__init__.py`.
2. Move the `## Unreleased` notes in `CHANGELOG.md` under a new `## <version>` heading, and name the cbz-webp and dl-queue versions it pins.
3. Commit, push to `main`, wait for CI to pass.
4. `git tag v<version> && git push origin v<version>`.

`release.yml` checks the tag against `__version__` and the CHANGELOG, runs CI, builds the wheel and publishes a GitHub release. The repo is private, so the release is visible only to people with access. Tag only when the user asks for a release.
