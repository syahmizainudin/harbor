<p align="center"><img src="assets/logo.svg" width="96" alt="Harbor logo"></p>

# harbor

[![CI](https://github.com/syahmizainudin/harbor/actions/workflows/ci.yml/badge.svg)](https://github.com/syahmizainudin/harbor/actions/workflows/ci.yml)

Harbor puts three tools on one local web page:

- **Convert** ([cbz-webp](https://github.com/syahmizainudin/cbz-webp)) recompresses the images inside `.zip` and `.cbz` archives to WebP and saves them as `.cbz`.
- **Download** ([dl-queue](https://github.com/syahmizainudin/dl-queue)) downloads files from direct URLs, optionally with an API key.
- **Library** ([cbz-shelf](https://github.com/syahmizainudin/cbz-shelf)) scans your folders of comics and EPUB books into a searchable library with editable metadata, and has a comic reader and a text reader.

Downloaded archives can go straight into the Convert queue. A converted file that lands in a library folder joins the library right away. So a list of URLs becomes compressed `.cbz` books, ready to read.

## Install

Needs Python 3.10 or later. This repo is private, so `pip`/`pipx` need your GitHub access (for example, run `gh auth login` and then `gh auth setup-git`):

```bash
pipx install git+https://github.com/syahmizainudin/harbor
```

That also installs cbz-webp and dl-queue at the versions pinned in `pyproject.toml`, and Pillow. Each [release](https://github.com/syahmizainudin/harbor/releases) also has a wheel.

## Accounts

Anyone can browse and read the library without signing in. Converting, downloading, favourites and everything else need an account, and signed out, reading progress isn't saved. Make the first account, an admin, before starting it:

```bash
harbor user add YOUR-NAME --admin     # asks for a password (8 or more characters)
```

| Command | What it does |
|---|---|
| `harbor user add NAME [--admin]` | Adds an account. `--password-stdin` reads the password from standard input |
| `harbor user passwd NAME` | Sets a new password and signs the account out everywhere |
| `harbor user list` | Lists accounts |
| `harbor user disable NAME` / `enable NAME` | Stops or allows signing in. Disabling ends the account's sessions and extension tokens |
| `harbor user remove NAME` | Deletes the account |

| `harbor user role NAME ROLE` | Gives an account another role |
| `harbor user roles` | Lists roles and their permissions |

`harbor user add` takes `--role NAME` (default Member). The last admin who can sign in can't be disabled, removed or given another role. Each user has their own reading progress, Continue reading, favourites and reader settings in the Library tab. Book and series details are shared. Reading progress from before accounts existed goes to the first admin when harbor starts.

- **Signing in:** a signed-out visit shows the Library tab and a **Sign in** button. A session lasts `HARBOR_SESSION_DAYS` (30) days after it was last used. After 5 wrong passwords for a name, that name is locked for 15 minutes.
- **Signing in** happens in a dialog over the page (**Sign in**, bottom of the sidebar). Signing out takes you back to the home page as a guest.
- **Your account** (bottom of the sidebar), in sub-tabs: **Password** (changing it signs out your other browsers), **Defaults**, **Sessions** (every browser signed in to your account; sign any of them out) and **Tokens** (for the Chrome extension and reader apps). Sign out is at the bottom.
- **Roles:** every account has one. Admin has every permission and can't be changed. Member (convert, download, delete originals, read the library) and Reader (read the library) come built in, and admins can add more. Tabs and buttons you can't use are hidden, and harbor refuses them anyway.

  | Permission | Lets you |
  |---|---|
  | `convert` | Queue conversions, and convert downloads |
  | `download` | Queue downloads |
  | `delete_original` | Delete the original `.zip` after converting. Without it, that box is off |
  | `library.read` | Use the Library tab: read, favourites, your own progress and reader settings |
  | `library.edit` | Edit book and series details, which everyone sees |
  | `library.manage` | Add and remove library folders, rescan, forget missing books |
  | `jobs.see_all` | See and manage everyone's jobs, and pause, cancel all or clear finished. Without it you see only your own |
  | `download.lanes` | The Admin tab's Download lanes section: add, change and remove lanes, choose who can use each, and pause or resume one lane |
  | `users.manage` | The Admin tab's Users section |
  | `roles.manage` | The Admin tab's roles and permissions matrix |
  | `settings.manage` | The Admin tab's Settings section: change server settings not locked in `.env` |
  | `games.play` | The Games tab: see the games, play, keep your own saves (Member has it) |
  | `games.manage` | The Games tab's Folders: add, remove and rescan game folders |

  The folder browser needs `convert`, `download` or `library.manage`. Someone who manages users or roles but isn't an admin can only hand out permissions they have themselves, so they can't make anyone (or themselves) an admin.
- **Your defaults:** the Convert and Download fields (quality, effort, deleting originals, download folder, how the API key is sent, converting downloads) are saved with your account as you change them, so they follow you to any browser and the Chrome extension uses them. **Account → Go back to harbor's defaults** returns to the `.env` values. The text reader's size, width and colours are saved with your account too. The API key itself is never saved per user.
- **Admin tab:** one sub-tab per section: Users (add, change role, reset password, disable, remove), Roles and permissions (the roles × permissions matrix), Library access, Audit log, and Series (rename and edit the shared details of any series). You see only the sections you may use, and the page remembers the one you had open.
- **Libraries and who sees them:** in the Library tab, **Folders → Libraries…** splits the library into named libraries (Manga, Novels…), each with one or more folders. They show as tabs above the filters, next to **All**. By default everyone who can read the library sees every library, signed-out visitors included. In **Admin → Library access**, untick **Every role** to pick the roles that see a library, and untick **Guests** to hide it from signed-out visitors. Admin always sees everything. Hidden books don't appear anywhere: not in lists, search, counts or Continue reading, and opening one by its address answers "not found". Someone who manages roles but isn't an admin can only change libraries they can see, and only for roles within their own permissions.
- **What's stored:** passwords as salted scrypt hashes, and only hashes of session ids and tokens, in `HARBOR_ACCOUNTS_DB` (`~/.local/share/harbor/harbor.db`, readable only by you).
- **What it protects:** harbor still listens only on `127.0.0.1`. Accounts keep people who share this computer's browser apart. They don't protect against someone who can read your files in WSL.

## Run

```bash
harbor ui --open        # http://127.0.0.1:8765/
```

| Option | Setting in `.env` | Default | Meaning |
|---|---|---|---|
| `-p`, `--port` | `HARBOR_PORT` | 8765 | Port on `127.0.0.1` |
| `-q`, `--quality` | `HARBOR_QUALITY` | 80 | Default WebP quality on the Convert tab |
| `-j`, `--jobs` | `HARBOR_JOBS` | 1 | Archives converted at the same time |
| `-t`, `--threads` | `HARBOR_THREADS` | CPU count | Image-encoding threads, shared by all conversions |
| `-d`, `--downloads` | `HARBOR_DOWNLOADS` | 2 | Downloads at the same time |
| | `HARBOR_DELETE_ORIGINAL` | `true` | Default for "Delete original .zip" |
| | `HARBOR_DOWNLOAD_DIR` | | Default "Save to folder" on the Download tab |
| | `HARBOR_AUTH_MODE` | `bearer` | Default for "API key sent as": `none`, `bearer`, `header` or `query` |
| | `HARBOR_AUTH_NAME` | | Header or query parameter name, for `header` and `query` modes |
| | `HARBOR_API_KEY` | | API key used when the page's key field is left empty |
| | `HARBOR_METHOD` | 4 | Default encoder effort, 0 (fastest) to 6 (smallest files) |
| | `HARBOR_RETRIES` | 3 | Extra attempts after a temporary download failure |
| | `HARBOR_STATE_FILE` | `~/.local/state/harbor/queue.json` | Where unfinished jobs are saved |
| | `HARBOR_RESTORE` | `true` | Add saved jobs back at start-up |
| | `HARBOR_EXTENSION_ORIGINS` | the Harbor extension | Origins allowed besides the page: exact origins, comma-separated, or `off` |
| | `HARBOR_LIBRARY_DB` | `~/.local/share/harbor/library.db` | The Library tab's database (folders, metadata edits, reading progress) |
| | `HARBOR_LIBRARY_SCAN` | `true` | Rescan library folders when harbor starts |
| | `HARBOR_ACCOUNTS` | `true` | Require signing in for everything but reading the library. `false` turns accounts off: anyone who can reach the page can use it |
| | `HARBOR_ACCOUNTS_DB` | `~/.local/share/harbor/harbor.db` | Accounts, sessions and extension tokens |
| | `HARBOR_SESSION_DAYS` | 30 | How long a sign-in lasts after it was last used |
| | `HARBOR_SECURE_COOKIE` | `false` | Mark the session cookie `Secure`. Only when harbor is served over HTTPS |
| | `HARBOR_HISTORY_DB` | `~/.local/share/harbor/history.db` | Finished conversions and downloads (the History panels) |
| | `HARBOR_HISTORY_DAYS` | 90 | Days finished jobs are kept in the history. 0 keeps them forever |
| | `HARBOR_AUDIT_DAYS` | 365 | Days audit log events are kept. 0 keeps them forever |
| | `HARBOR_HOST` | `127.0.0.1` | Address to listen on. Anything else needs HTTPS: see [Reading from other devices](#reading-from-other-devices) |
| | `HARBOR_ALLOWED_HOSTS` | | Names or addresses other devices use to reach harbor, comma-separated |
| | `HARBOR_TLS_CERT`, `HARBOR_TLS_KEY` | | HTTPS certificate and key (PEM). The key must be readable only by you |
| | `HARBOR_TRUST_PROXY` | `false` | A reverse proxy on this computer serves harbor over HTTPS |
| | `HARBOR_LOCAL_PORT` | | With harbor's own HTTPS: a plain-HTTP port on 127.0.0.1 for the Chrome extension |
| | `HARBOR_DOWNLOAD_RATE` | no limit | Speed limit for all downloads together, such as `5M` or `500K` |
| | `HARBOR_DOWNLOAD_HOURS` | always | Start downloads only between these local times, such as `01:00-07:00` |
| | `HARBOR_WATCH_DIRS` | | Folders whose new archives are converted on their own, separated by `;` |
| | `HARBOR_WATCH_DELETE_ORIGINAL` | `false` | Delete watched originals after converting. Doesn't follow `HARBOR_DELETE_ORIGINAL` |
| | `HARBOR_WATCH_POLL` | 30 | Seconds between checks of the watch folders |
| `--env-file PATH` | | | Read settings from this file |

Ctrl+C stops the console. Running jobs are cancelled, originals are kept, and partial downloads are removed.

### Unfinished jobs survive a restart

Harbor saves the queued and running jobs of both tabs within a second of a change, and adds them back the next time it starts. The page shows a note when it does.
- A running job starts over.
- Finished and cancelled jobs aren't kept.
- **The API key is never saved.** A download that needs a key comes back only if harbor has `HARBOR_API_KEY`. Otherwise, the note asks you to add it again.

### History

Each finished conversion and download is recorded in `HARBOR_HISTORY_DB`, with its file, result, sizes and time. The **History** panel under each queue shows your own (with **Everyone's** if you have `jobs.see_all`), and stays after **Clear finished** and restarts. The Convert tab shows the space saved in total and in the last 30 days. Records older than `HARBOR_HISTORY_DAYS` are deleted.

### Audit log

**Admin → Audit log** (`users.manage`) lists sign-ins and failed sign-ins, password changes and resets, users and roles added, changed or removed, library access changes, extension tokens, and library folders and libraries added, moved or removed. Each entry names who did it (or "command line" for `harbor user`) and when. Passwords, session ids and tokens are never recorded. Entries older than `HARBOR_AUDIT_DAYS` are deleted.

### Back up and check

```bash
harbor backup                  # copies the databases and saved jobs to ~/.local/share/harbor/backups/<time>/
harbor backup --to /mnt/d/harbor-backups --keep 14
harbor doctor                  # checks settings, dependencies, databases, library folders and the port
```

`harbor backup` is safe while harbor runs: it copies each database with SQLite's backup API, and leaves the originals alone. It keeps the newest `--keep` (7) backups in that folder and deletes older ones, and only its own timestamped folders. To restore, stop harbor (`pm2 stop harbor`), copy the files from a backup folder back to where they were, and start it again.

`harbor doctor` changes nothing. It opens the databases read-only, so it never upgrades them, and exits with 1 if it finds a problem. Warnings, such as a `.env` other users can read, don't count as problems. Both commands take `--env-file`.

### Keep it running in the background

`ecosystem.config.cjs` runs harbor under [pm2](https://pm2.keymetrics.io/), as Grove does:

```bash
pm2 start ecosystem.config.cjs && pm2 save   # start now, and remember it
pm2 logs harbor                              # its output
pm2 restart harbor                           # after changing .env or updating
```

To start pm2 itself when WSL starts, run the command `pm2 startup` prints (it needs `sudo`).

## Games

The **Games** tab is [rpgm-shelf](https://github.com/syahmizainudin/rpgm-shelf): RPG Maker MV and MZ games played in the browser, with saves kept per account on this computer, so they follow you to any browser.

- Someone with `games.manage` adds folders of games under **Folders**. Each game is a folder with `index.html` and `data/System.json` (or a `www/` folder holding them).
- **Play** opens the game in a new tab. Games run on their own port (`HARBOR_GAMES_PORT`, default 8768, on 127.0.0.1), never on harbor's: a game is someone else's JavaScript, and this keeps it away from your harbor sign-in. For now games can be played only on the computer running harbor.
- Turn it off with `HARBOR_GAMES=false`. Settings: `HARBOR_GAMES_PORT`, `HARBOR_GAMES_DB` (default `~/.local/share/harbor/games.db`), `HARBOR_GAMES_SCAN` (rescan at start-up).

## Saved site keys

With accounts, each person can save API keys and tokens for the sites they download from: **Your account → Sites**. A key has a name, a site (`api.example.com`, or `*.example.com` for its subdomains), an optional path (`/files/`), and how it's sent (Bearer, a custom header, or a query parameter).

On the Download tab (or in a preset, so the Chrome extension uses it too), choose **API key sent as: Saved site key (automatic)**. Each download then uses your key whose site matches its address, most specific first, or the `.env` key when none matches.

- Keys are sent over **https only** (and to this computer). A plain-http download runs without the key, and the page says so.
- Keys are stored **encrypted**; the encryption key is `secret.key` next to harbor's accounts database (`~/.local/share/harbor/`). They're never shown again: to change one, enter it again. Your keys are used only for downloads you queue.
- Downloads keep working after a restart: the key is picked again at each try, never saved with the download.
- `harbor backup` leaves `secret.key` out unless you add `--with-secrets`, so a backup alone can't reveal your keys. Lose `secret.key` and the saved keys show "can't be read"; enter them again.

## Reader apps (OPDS)

Harbor's library is an [OPDS](https://opds.io/) catalog, so reader apps such as Panels, Chunky and KOReader can browse it, search it, stream comic pages and download books for offline reading.

1. In **Your account**, choose **Reader app (OPDS)**, give the token a name (such as "Phone") and click **Create token**. Copy it; it's shown once.
2. In the app, add an OPDS catalog: the address is harbor's address followed by `/api/library/opds` (shown under the token), the user name is your account name, and the password is the token.

A reader-app token can only read the library: it sees the libraries your role may see, and can't open the page, queue jobs or change anything. Revoke it in **Your account** if the device is lost. Your account password never works in a reader app. Reading progress stays in the app, not in harbor. To use it from a phone, harbor must be reachable from it (below).

## Reading from other devices

By default harbor is reachable only from this computer. To read on a phone or tablet on your network, harbor must be served over HTTPS, either by harbor itself or by a reverse proxy, and it refuses to start otherwise. It also needs:

- `HARBOR_SECURE_COOKIE=true`;
- accounts on (the default);
- `HARBOR_ALLOWED_HOSTS`, the names or addresses your devices use.

`harbor doctor` checks all of this and says what's missing.

**Before you open it up:**

- Signed-out visitors can read every library unless you untick **Guests** for it in **Admin → Library access**. Anyone on your network can then see those books.
- Accounts work from other devices exactly as they do here. Someone with `convert`, `download` or `library.manage` can browse the names of every folder on this computer in the folder picker. With `convert` or `download`, they can also queue conversions of any folder and downloads into any folder, and with `delete_original` the originals get deleted. Give those permissions only to people you would let use this computer.
- A series page shows its folder on this computer to everyone who can read it, and `library.manage` sees every library folder. A coming release hides these paths. Until then, open harbor only to people you'd show your folder names to.

### Option 1: harbor serves HTTPS

```bash
# .env
HARBOR_HOST=0.0.0.0
HARBOR_ALLOWED_HOSTS=my-pc.tail1234.ts.net,192.168.1.20
HARBOR_TLS_CERT=~/.config/harbor/cert.pem
HARBOR_TLS_KEY=~/.config/harbor/key.pem
HARBOR_SECURE_COOKIE=true
HARBOR_LOCAL_PORT=8766          # for the Chrome extension
```

The Chrome extension only talks to `http://localhost` and `http://127.0.0.1`, so give it a local port too: with `HARBOR_LOCAL_PORT=8766`, harbor also answers plain HTTP on `127.0.0.1:8766`, for this computer only. Set the extension's Harbor address to `http://127.0.0.1:8766`. This computer's browser can use that address as well (it signs in separately from the HTTPS address).

A certificate your devices trust without warnings is easiest to get with [Tailscale](https://tailscale.com/kb/1153/enabling-https): `tailscale cert my-pc.tail1234.ts.net` writes one for your machine's Tailscale name, and it works from any device on your tailnet. [mkcert](https://github.com/FiloSottile/mkcert) works on a home network too, but then every phone needs mkcert's root certificate installed. Keep the key private: `chmod 600 key.pem`.

### Option 2: a reverse proxy on this computer

Keep `HARBOR_HOST=127.0.0.1` and let the proxy do HTTPS. Harbor believes the proxy's `X-Forwarded-Proto`, `X-Forwarded-Host` and `X-Forwarded-For` headers only on connections from this computer. For example, with [Caddy](https://caddyserver.com/):

```
harbor.lan {
    tls internal
    reverse_proxy 127.0.0.1:8765
}
```

`tls internal` makes Caddy's own certificate authority, which each phone has to trust once (as with mkcert). A real name with a real certificate, such as a Tailscale name, avoids that.

```bash
# .env
HARBOR_TRUST_PROXY=true
HARBOR_ALLOWED_HOSTS=harbor.lan
HARBOR_SECURE_COOKIE=true
```

### WSL

WSL 2 is reachable from Windows but not from other devices, until you do one of these:

- **Mirrored networking** (Windows 11): add `networkingMode=mirrored` under `[wsl2]` in `%UserProfile%\.wslconfig`, run `wsl --shutdown`, and allow the port through the Windows firewall (`New-NetFirewallRule -DisplayName harbor -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow` in an admin PowerShell). Mirrored mode also has its own Hyper-V firewall; see [Microsoft's WSL networking page](https://learn.microsoft.com/windows/wsl/networking) if connections still don't arrive.
- **A port proxy:** `netsh interface portproxy add v4tov4 listenport=8765 listenaddress=0.0.0.0 connectport=8765 connectaddress=<WSL address>` plus the same firewall rule. The WSL address (`hostname -I` in WSL) changes when WSL restarts, so the rule needs updating then. With a proxy, harbor sees every client as the Windows machine.
- **Tailscale inside WSL** makes WSL its own device on your tailnet, with no Windows changes.

The audit log records the address each event came from.

## Settings file (`.env`)

All settings are optional. Copy [`.env.example`](.env.example) to `.env` and fill in what you need. Harbor reads the first of these files that exists:

1. the file given with `--env-file`
2. the file named in `$HARBOR_ENV_FILE`
3. `.env` in the folder you start harbor from
4. `~/.config/harbor/.env`

Without one, harbor uses its defaults. A command-line flag beats an environment variable, which beats the file. Harbor stops with a message naming the file and setting if a value is invalid or a `HARBOR_` name is misspelled. When it starts, it prints which file it used.

The file is plain `KEY=value` lines. `#` starts a comment, values may be quoted, and `export` in front is allowed. Nothing in it is run or expanded.

**The API key stays inside the harbor process.**
- The page is only told that a key exists. It shows "Using the key from .env" and uses that key whenever the key field is left empty. A key typed into the page is used instead.
- The key is never shown, never added to the queue state, and never stored in the browser.
- `.env` files are git-ignored here. Keep it that way, and keep the file private (`chmod 600 .env`).

With accounts, each user's own defaults win over these values. Without accounts, values in the file fill the Download tab's fields only when this browser hasn't remembered its own value. To go back to the file's values, clear the site's data in the browser.

### Changing settings on the Admin page

With accounts, someone with the `settings.manage` permission (Admin has it) can change about twenty settings on the Admin page's **Settings** tab: the Convert and Download defaults, downloads and conversions at once, retries, the speed limit and hours, taking turns between sites, watch folders, and how long sign-ins, history and the audit log last.

- **`.env` wins.** A setting given in `.env`, the environment or a flag is shown locked, with where it's set. To manage it on the page, remove it from `.env` and restart.
- **Most apply at once.** Conversions and downloads at once, image threads, watch folders and the start-up library scan are saved and used after a restart; the page says when a restart is needed.
- **Some stay in `.env` only:** the port and address, HTTPS and proxy settings, accounts and cookie settings, database and state file paths, the extension origins, the API key, and `HARBOR_WATCH_DELETE_ORIGINAL` (shown, not changeable), because it deletes files with nobody reviewing them.
- Watch folders keep converting with the quality and effort harbor started with until it restarts.
- Every change goes in the audit log. Values are stored in the accounts database; at start-up harbor prints which ones it used.

## The page

The tabs are in a sidebar on the left; **« Collapse** shrinks it to icons. While a book is open, the sidebar steps aside and the reader fills the window.

**Watch folders:** with `HARBOR_WATCH_DIRS`, every `.zip` or `.cbz` that appears in those folders (or their subfolders) is converted with harbor's Convert defaults, once it has stopped changing (checked every `HARBOR_WATCH_POLL` seconds). Each file is converted once, and one that fails is tried again only after it changes. Originals are **kept** unless `HARBOR_WATCH_DELETE_ORIGINAL=true`, whatever `HARBOR_DELETE_ORIGINAL` says, because nobody looks at a watched file first. With accounts, these conversions belong to nobody, so only people with `jobs.see_all` see them. The Convert tab shows how many folders are watched.

**Convert tab:** add folders or archives, or click **Open folder…** to browse WSL's folders, with shortcuts to home and each Windows drive. Set the quality and whether to delete each original `.zip` once its `.cbz` has been checked. See [cbz-webp's README](https://github.com/syahmizainudin/cbz-webp#what-happens-to-each-archive) for exactly what happens to each archive.

**Download tab:**

1. Paste file URLs and choose a folder to save to.
2. Choose how the API key is sent: Bearer, a named header, a named query parameter, or none.
3. Leave **Convert .zip / .cbz downloads** ticked to send each downloaded archive to the Convert queue, with the Convert tab's quality and delete settings. Each download then shows its convert job's progress. A file that isn't converted (an `.epub`, or with this unticked) that lands in a library folder joins the library right away.

**Presets:** **Save as preset…** keeps the folder, the API key mode and name, and the convert settings (convert or not, quality, effort, delete) under a name, such as *Manga* or *Books*. Picking a preset from the **Preset** menu fills in the form for the next downloads; your defaults don't change, and the folder you used isn't remembered as the new default. The API key is never saved in a preset. With accounts, presets belong to your account (up to 20) and the Chrome extension can use them. Without accounts, this browser keeps them.

`{1..20}` in a URL stands for 20 URLs, one per number, and `{01..20}` keeps the zero padding. One range per URL, at most 500 URLs per add. This works from the Chrome extension too.

With `HARBOR_DOWNLOAD_RATE`, all downloads together stay under that speed. With `HARBOR_DOWNLOAD_HOURS`, new downloads start only in those hours, and the tab shows "waiting until …" outside them. A download that is already running finishes.

**Download lanes** (with accounts): on the Admin page's **Download lanes** tab, someone with `download.lanes` adds lanes such as *manga* or *night*, each with how many downloads run at once, an optional speed limit and optional hours. A lane's limit caps that lane, and the overall limit above still covers all downloads; a lane without hours follows `HARBOR_DOWNLOAD_HOURS`. The Download tab then has a **Lane** menu (presets remember it, so the Chrome extension uses it too), and the queue shows each lane's running and waiting downloads. Changes apply at once. A lane can be kept to some roles (**Who can use it**; Admin always can): others don't see it in the Lane menu and can't add to it. Someone with `download.lanes` can **Pause** one lane from its chip on the Download tab or its row on the Admin page; its queued downloads wait, running ones finish, and other lanes carry on. Removing a lane lets its queued downloads run in the default lane, which keeps `HARBOR_DOWNLOADS`, `HARBOR_DOWNLOAD_HOURS` and the overall limit. Without accounts there are no lanes.

**Taking turns between sites:** with `HARBOR_DOWNLOAD_FAIR=true`, the next download to start is the one from the site with the fewest downloads running in its lane, so a long list from one site doesn't hold up a short one from another. Off by default: first queued, first started.

If the library already has a book with the same file name as a URL (ignoring `.zip`, `.cbz` and `.epub`), the message after **Add to queue** names it. The download is queued anyway. Only libraries you can see are checked, and a URL whose file name the server picks later (such as `/download?id=3`) can't be checked.

The key is never shown in the queue or sent on redirects. See [dl-queue's README](https://github.com/syahmizainudin/dl-queue#how-downloads-are-handled) for how files are named and saved.

**Library tab:**

1. Click **Folders** and add a top-level folder of comics or EPUB books. Each folder of books becomes a series (or the `Series` in an archive's `ComicInfo.xml`), and metadata is read from `ComicInfo.xml` and file names.
2. Browse by series, search, and filter by tag or author.
3. Edit any book's or series' details. Edits are kept in harbor's library database; your files are never changed.
4. Read comics and picture EPUBs in single-page, two-page or vertical-scroll layout, left to right or right to left (manga). Text EPUBs open in a text reader with contents, text size, line width and Light, Sepia or Dark colours.

[cbz-shelf's README](https://github.com/syahmizainudin/cbz-shelf#readme) covers the details. The tab uses its own database (`HARBOR_LIBRARY_DB`), separate from a standalone `cbz-shelf`'s.

For each tool on its own, use `cbz-webp`, `dl-queue` and `cbz-shelf` directly; each has its own CLI and page.

## Chrome extension

The `extension/` folder is a Chrome extension that sends links to harbor:

- **Right-click a link → Send link to Harbor.** Right-click a page → **Send this page to Harbor**.
- **The toolbar popup:**
  - shows whether harbor is running, with its active downloads and conversions;
  - sends the current tab;
  - lists the links on the page (archives by default, or all links), so you can tick some and send them together.
- **The toolbar badge** counts active jobs, and reads `off` when harbor isn't running.

Downloads sent this way use harbor's defaults: the `.env` key, folder and auth settings, and the Convert quality, effort and delete settings. The popup's Settings can change the harbor address, the save-to folder and whether archives are converted. If you have presets (made in the Download tab), the popup shows a **Preset** menu: the chosen preset's folder and convert settings are used for the popup's sends and the right-click menu. If the chosen preset is deleted in harbor, sending stops with a message until you pick another.

**Install (Chrome on Windows):**

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and choose `\\wsl.localhost\Ubuntu\app\tools\harbor\extension`.
3. Pin the Harbor icon. Harbor must be running (see above).
4. In harbor, open your account (bottom of the sidebar) and click **Create token**. Paste the token into the popup's **Settings → Token**. The popup says **Not signed in** until it has one.

The extension always gets the ID `pioghlmpnbcbgiemaphgpnendofahggb`, because its manifest carries a fixed public key. Harbor accepts requests from exactly that origin, which the browser sets itself: other websites and other extensions are still refused. The token says which user is sending. Revoke it from your account if the computer is lost. The extension never sees the API key. It sends downloads without one, and harbor adds the `.env` key.

## Development

```bash
pip install --target .deps .                         # the pinned cbz-webp and dl-queue (+ Pillow)
PYTHONPATH=.deps python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

GitHub Actions runs lint, the tests on Python 3.10 to 3.14 against the pinned versions, and a package smoke test on every push to `main` and every pull request. Pushing a `v<version>` tag publishes a GitHub release. [AGENTS.md](AGENTS.md) has the release steps and the rules for changing the code.
