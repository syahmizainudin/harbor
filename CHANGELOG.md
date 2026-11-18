# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

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
