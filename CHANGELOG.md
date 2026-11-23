# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## 0.2.5

Pins cbz-webp 0.5.1, dl-queue 0.6.0 and cbz-shelf 0.6.0 (unchanged).

- **Sign in in a dialog:** the **Sign in** button opens a sign-in dialog over the page instead of a separate, empty page. `/login` (the server's redirect, and the Library tab when a session ends) forwards to it and still returns you to where you came from. When your session ends while the page is open, the dialog opens with a note instead of the page jumping away.
- **Signing out** takes you to the home page as a guest instead of the sign-in page.
- **Account dialog sub-tabs:** Password, Defaults, Sessions and Tokens, like the Admin tab. The last one you opened is remembered.

## 0.2.4

Pins cbz-webp 0.5.1, dl-queue 0.6.0 and cbz-shelf 0.6.0.

- **Download lanes:** named lanes, each with its own downloads at once, speed limit and hours (dl-queue 0.6.0 `Lane`). Managed live on the Admin page's new **Download lanes** tab with the new `download.lanes` permission, stored in the accounts database (migration 7) and recorded in the audit log. The Download tab gets a **Lane** menu and per-lane chips, rows show their lane, presets can remember a lane, and saved jobs keep it across restarts. Without accounts there are no lanes. `/api/state` gains `download_lanes`.
- **Chrome extension 0.3.1:** sends a preset's lane. Reload the extension from `chrome://extensions`.
- The planned-features list moved to a Claude Doc; `docs/planned-features.md` links to it.

## 0.2.3

Pins cbz-webp 0.5.1, dl-queue 0.5.1 and cbz-shelf 0.6.0.

- **Download presets:** on the Download tab, **Save as preset…** keeps the folder, API key mode and name, and convert settings (convert, quality, effort, delete) under a name; the **Preset** menu fills them in. Picking one doesn't change your defaults. Never stores the API key. Per account (up to 20), saved with `POST /api/account/prefs` (`download_presets`) and shown in `/api/state` as `harbor.download_presets`; without accounts, the browser keeps them.
- **Chrome extension 0.3.0:** a **Preset** menu in the popup; the chosen preset's folder and convert settings are used for sends from the popup and the right-click menu. Reload the extension from `chrome://extensions`.
- **Compilation folders** in the Library tab (cbz-shelf 0.6.0): a folder of unrelated books no longer has to be one series. On its series page, **Folder is a compilation** (library managers) groups its books by the series name in each file name; **Folder is one series** undoes it. The change is recorded in the audit log ("library folder grouping changed").

## 0.2.2

Pins cbz-webp 0.5.1, dl-queue 0.5.1 and cbz-shelf 0.5.3 (unchanged).

- **Admin tab sub-tabs:** Users, Roles and permissions, Library access, Audit log and Series each get their own sub-tab instead of one long page. Only the sections you may use are offered, the last one you opened is remembered, and the audit log and series list load when their sub-tab opens.

## 0.2.1

Pins cbz-webp 0.5.1, dl-queue 0.5.1 and cbz-shelf 0.5.3 (unchanged).

- **The Chrome extension works when harbor serves HTTPS itself:** `HARBOR_LOCAL_PORT` adds a plain-HTTP listener on 127.0.0.1 only, with the same pages and checks, for the extension and this computer's browser. `harbor doctor` warns when harbor serves HTTPS without one.

## 0.2.0

Pins cbz-webp 0.5.1, dl-queue 0.5.1 and cbz-shelf 0.5.3.

Accounts and library databases are upgraded when harbor starts (accounts migrations 3–6, library migration 6), and nothing is dropped. Run `harbor backup` before you upgrade.

### Libraries

- **Libraries as tabs** (cbz-shelf 0.5.0): in the Library tab, **Folders → Libraries…** splits the library into named libraries, each holding one or more folders, shown as tabs next to **All**. Existing folders stay in one library called "Library".
- **Library access:** a new section in the Admin tab says which roles, and whether signed-out visitors, can see each library. Admin always sees all. Hidden books are left out of every list, search, count and Continue reading, and answer 404 by id, through cbz-shelf's `LibraryAPI(visible=)`. Non-admins who manage roles can change only libraries they can see, and only for roles within their own permissions. Accounts migration 3 adds the `library_access` table; with no rules every library is open, as before.

### History, sessions and maintenance

- **History:** every finished conversion and download is kept in `history.db` (`HARBOR_HISTORY_DB`, `HARBOR_HISTORY_DAYS`, default 90 days), and a **History** panel under each queue shows yours, or everyone's with `jobs.see_all`. The Convert tab shows the space saved in total and in the last 30 days.
- **Where you're signed in:** Your account lists the browsers signed in to your account (with when they signed in and were last used), and signs any of them out. Accounts migration 4 records each session's browser.
- **Audit log** in the Admin tab (`users.manage`): sign-ins and failed sign-ins, password changes, users, roles, library access, tokens, and library folders and libraries. Never passwords or tokens. `HARBOR_AUDIT_DAYS` (default 365).
- **`harbor backup`** copies the databases and saved jobs to a timestamped folder, safely while harbor runs, keeping the newest 7. **`harbor doctor`** checks settings, file permissions, dependency versions, databases, library folders and the port, without changing anything.
- **Already in the library:** adding downloads whose file name matches a book you can see in the library says so. The downloads are still queued.

### Reading from other devices

- **Reading from other devices:** `HARBOR_HOST`, `HARBOR_ALLOWED_HOSTS`, `HARBOR_TLS_CERT`/`HARBOR_TLS_KEY` (harbor serves HTTPS) or `HARBOR_TRUST_PROXY` (a reverse proxy on this computer does). Harbor refuses to start reachable from other devices without HTTPS, the Secure cookie, accounts and the allowed host names, and says what's missing; `harbor doctor` checks the same and when the certificate expires. The README covers WSL networking and certificates.
- The audit log records the address each event came from (accounts migration 5).

### Reader apps

- **Reader apps (OPDS):** the library is an OPDS catalog at `/api/library/opds` (cbz-shelf 0.5.2) for Panels, Chunky, KOReader and others: browse, search, stream pages, download books. They sign in with your account name and a **reader-app token** from Your account (accounts migration 6 adds token kinds), which can only read the libraries you may see. Your password never works there.
- Series folder paths are shown only to people with `library.manage` (cbz-shelf 0.5.2).

### Downloads and watch folders

- **Download speed limit and quiet hours** (dl-queue 0.5.1): `HARBOR_DOWNLOAD_RATE` (such as `5M`) for all downloads together, and `HARBOR_DOWNLOAD_HOURS` (such as `01:00-07:00`) for when new downloads may start. The Download tab shows both.
- **URL ranges** (dl-queue 0.5.1): `{1..20}` or `{01..20}` in a URL adds one download per number.
- **Watch folders** (cbz-webp 0.5.1): `HARBOR_WATCH_DIRS` converts archives that appear in those folders with the Convert defaults. Originals are kept unless `HARBOR_WATCH_DELETE_ORIGINAL=true`. `HARBOR_WATCH_POLL` sets how often they're checked.

## 0.1.2

Pins cbz-webp 0.4.2, dl-queue 0.4.2 and cbz-shelf 0.4.3 (unchanged).

- Signed-out visitors no longer see the Convert and Download tabs for a moment while the page loads: the tabs and the pane appear once the server has said what the visitor may use.

## 0.1.1

Pins cbz-webp 0.4.2, dl-queue 0.4.2 and cbz-shelf 0.4.3.

- **Library permissions can't be switched off by old dependencies:** `harbor ui` no longer falls back to a library without permission checks (or to no sign-in) when cbz-shelf or cbz-webp are older than the pins. It stops at start-up and says to reinstall the dependencies.
- The last admin who can sign in is protected by one check, made under the accounts lock. Disabling an admin who is already disabled is no longer refused.
- Adding a job no longer holds up the workers, the saved state and the download hand-off while it scans folders.
- Unfinished jobs are saved in batches, within about half a second of a change, instead of on every change. A job added from the page is saved with its owner (it was saved without one until the next change).
- The page polls once a second on the Convert and Download tabs, every 5 seconds on Library and Admin or when the browser tab is hidden, and refreshes at once when you come back. Stats and tables are redrawn only when they change.
- Signing in and checking a session or token take fewer database queries, and adding a user or setting a password no longer blocks sign-ins while the password is hashed.
- Pins cbz-webp 0.4.2, dl-queue 0.4.2 and cbz-shelf 0.4.3: a download never overwrites a file that appears at its name while it runs, resumes work when the server doesn't send the total size, the check before an original is replaced runs on the image threads, library scans write in batches and stop reading a text EPUB once its type is known, new library indexes, and the Library tab stops polling while it can't be seen.

- **The library is open without signing in:** a signed-out visitor gets the Library tab and can browse and read. Converting, downloading, favourites, editing and the other tabs still need an account (a star sends you to sign in). Signed out, reading progress and reader settings aren't saved, and `/api/state` shows no jobs and no settings. A request with an extension token that doesn't work still gets 401.
- **Your own defaults:** Convert and Download settings (quality, effort, delete original, download folder, auth mode and name, convert downloads) are saved per account as you change them, and `/api/state` gives each user theirs, so the Chrome extension uses them too. **Account → Go back to harbor's defaults** clears them. The API key stays server-wide.
- Text reader settings are saved per account (cbz-shelf 0.4.1).
- Pins cbz-shelf 0.4.1.
- **Roles and permissions:** Admin (everything, locked), Member and Reader built in, and more from the Admin tab. Nine permissions, from converting to managing roles. Without `jobs.see_all` you see and manage only your own jobs. Downloads hand their owner on to the conversion, and saved jobs keep it across restarts. `harbor user add --role`, `harbor user role`, `harbor user roles`.
- **Admin tab:** users (add, role, reset password, disable, remove), the roles × permissions matrix, and series editing. Nobody can hand out permissions they don't have.
- Reader settings are per user (cbz-shelf 0.4.0). The folder browser needs a permission (cbz-webp and dl-queue 0.4.1).
- Pins cbz-webp 0.4.1, dl-queue 0.4.1 and cbz-shelf 0.4.0.
- **Accounts:** everyone signs in. Make accounts with `harbor user add NAME [--admin]`, and manage them with `passwd`, `list`, `disable`, `enable` and `remove`. Passwords are scrypt hashes. Sessions are an `HttpOnly; SameSite=Strict` cookie that lasts `HARBOR_SESSION_DAYS`. Repeated wrong passwords lock a name for 15 minutes. Settings: `HARBOR_ACCOUNTS`, `HARBOR_ACCOUNTS_DB`, `HARBOR_SESSION_DAYS`, `HARBOR_SECURE_COOKIE`.
- **Your account** at the bottom of the sidebar: change your password, make and revoke Chrome extension tokens, sign out.
- Library progress, Continue reading and favourites are per user. Progress from before accounts goes to the first admin.
- Pins cbz-webp 0.4.0, dl-queue 0.4.0 (sign-in support in the console server) and cbz-shelf 0.3.2.
- **Chrome extension 0.2.0:** paste a token from your account into the popup's Settings. It says **Not signed in** without one.
- **Library tab:** [cbz-shelf](https://github.com/syahmizainudin/cbz-shelf) in harbor. Add folders and every `.cbz` inside is grouped into series and indexed for search. Edit metadata (authors, tags, and more) and read in the browser: single page, two pages, or vertical scroll, left to right or right to left. Progress is saved. Settings: `HARBOR_LIBRARY_DB`, `HARBOR_LIBRARY_SCAN`.
- A conversion that finishes inside a library folder is added to the library right away, so a download saved into a library folder shows up as a book.
- The folder pickers (harbor's, and the Library's Folders window from cbz-shelf v0.1.1) keep one size instead of resizing as you browse. Scrollbars follow the dark theme.
- **Sidebar:** the tabs move to a sidebar (**« Collapse** shrinks it to icons), and the open tab fills the window. While a book is open, the sidebar steps aside and the reader gets the whole window.
- **EPUB books** in the Library (cbz-shelf 0.2.0): picture EPUBs in the comic reader, text EPUBs in a text reader with contents, text size, line width and Light, Sepia or Dark colours. `.epub` downloads are never converted.
- Text EPUBs (cbz-shelf 0.2.1): tap the sides of the page, ‹ ›, ← → or swipe to turn a screen, and each chapter ends with a **Next** button.
- **Favourites and Continue reading** (cbz-shelf 0.3.0): star series and books, a Favourites row and filter, and × / Clear all on Continue reading (your page is kept).
- A download kept as it is (an `.epub`, or with conversion off) that lands in a library folder joins the library right away.

- **Chrome extension** (`extension/`): right-click links or pages to send them, and a popup that shows harbor's status, sends the current tab, and lists the page's links to pick from. A badge counts active jobs. Harbor accepts exactly the extension's fixed origin (`HARBOR_EXTENSION_ORIGINS` to change).
- **Unfinished jobs survive a restart**: saved to `HARBOR_STATE_FILE` on every change and added back at start-up (`HARBOR_RESTORE`). The key is never saved.
- **Encoder effort** on the Convert tab and for downloads (`HARBOR_METHOD`). **Download retries and resume** (`HARBOR_RETRIES`). Both come from the updated tools.
- `ecosystem.config.cjs` to keep harbor running with pm2.
- Requires cbz-webp v0.2.0 and dl-queue v0.2.0.

- A logo, shown in the page header and as the browser tab icon (`assets/logo.svg`).
- Settings from a `.env` file: `--env-file`, `$HARBOR_ENV_FILE`, `./.env` or `~/.config/harbor/.env`. Covers the port, worker counts, Convert defaults, Download defaults and an API key. Flags override environment variables, which override the file. See `.env.example`.
- An API key in `.env` (`HARBOR_API_KEY`) stays on the server. The page only knows one exists and uses it when its key field is left empty.

## 0.1.0

First release.

- `harbor ui` serves one local page with two tabs: **Convert** (cbz-webp v0.1.0) and **Download** (dl-queue v0.1.0).
- Downloaded `.zip` and `.cbz` files go straight into the convert queue (optional, on by default), using the Convert tab's quality and delete settings. Each download links to its convert job.
- Tests check that the code cbz-webp and dl-queue share by copy hasn't drifted apart.
