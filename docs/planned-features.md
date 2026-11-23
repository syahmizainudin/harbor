# Planned features

Ideas and planned work not started yet. Each entry says what it is, what's settled, and what's still open. A feature that gets a detailed plan links to its own `docs/plan-*.md`. Move an entry to `CHANGELOG.md` when it ships, and take it out of this list.

## RPG Maker MV/MZ library and player

A browsable library of RPG Maker games, played in the browser, with saves kept on the server per harbor user.

- **Plan:** [plan-games.md](plan-games.md). Phase 0 (prototype) is done.
- **Settled:** a new public tool, `rpgm-shelf`, mounted by harbor as a Games tab. Games run on a separate games server (a second port), never on harbor's origin, because a game is untrusted JavaScript. A sandboxed iframe doesn't work: MV reads pixels back from its images and fails with a tainted-canvas error. A small script added to each game's page sends the engine's saves to the server, authorised by a play token tied to one user and one game.
- **Open:** MZ support waits for a real MZ game to test with (MZ saves through IndexedDB, so it needs its own save script). Tailscale access later needs the games port served through `tailscale serve` as well.

## More download queue profiles

Download **presets** (named sets of folder, API key mode and convert settings, per account, in the page and the Chrome extension) are done; see `CHANGELOG.md`. Two other readings of "profiles" are still open:

- **Separate queues:** each profile its own queue with its own workers, speed limit and quiet hours, so one site's slow downloads don't hold up another. `DownloadQueue` is one queue with one rate and hours today, so this changes dl-queue first.
- **Site credentials:** saved sign-in details per site, picked by URL. These are secrets: anything stored must follow the API key rules (never in `/api/state`, never logged, never in a preset), so it needs its own design.

## Harbor as a skeleton with extensions

Turn harbor into a small core that loads features as extensions (Convert, Download, Library, Games and later ones), so a feature can be added or left out without changing the core.

- **Naming:** "extension" already means the Chrome extension here (`extension/`, `harbor/extension.py`). Use another word in the code, such as **module** or **plugin**, to keep them apart.
- **What the core would keep:** the shared server and its guards (Host, Origin, JSON-only POST, `check_reach`), sign-in and the permission checks, settings validation (`config.VARS`), the page shell with its tabs, history, audit log, backup and doctor.
- **What a module would declare:** its API prefix and object, its page or tab, its settings (with validators), its permissions, what it adds to `/api/state`, and start-up and shutdown steps in a defined order (shutdown order matters today: downloads before conversions, the library last).
- **Open:**
  - Are modules only the ones in these repos, chosen in `.env`, or also third-party packages installed separately? Third-party modules run inside harbor with full access, so loading them needs an explicit allow list.
  - The Download → Convert hand-off and the conversion → library indexing connect two modules. Either the core offers events that modules subscribe to, or these links stay in the core.
  - The page is one file today (`ui.html`, the Convert and Download tabs copied from the tools' pages). Modules would need to supply their own tab content, as the Library tab already does through an iframe.

## User management as an extension

Move accounts, sign-in, roles and the admin page out of the core into a module (depends on the skeleton above).

- **Why it's plausible:** accounts are already optional (`HARBOR_ACCOUNTS=false`), and the shared server takes sign-in through one hook (`auth=`).
- **Caution:** permissions are checked in many places outside `accounts.py`: `GuardedQueueAPI`, cbz-shelf's `can`/`visible`, guest rules, the audit log, `check_reach` (remote access requires accounts). Moving the *management* (users, roles, tokens, the admin page) into a module is straightforward. The *checks* have to stay in the core, behind a fixed interface (`current_user()`, `can(permission)`), or every module would depend on the accounts module directly. Remote access must still refuse to start without an auth module loaded.
- **Open:** is the goal to make accounts optional to install (smaller core), or to allow other sign-in methods (for example, Tailscale identity headers instead of passwords)? The second argues for a sign-in interface with more than one provider.

## Should harbor run in a container?

Run harbor (and later the games server) from a Docker image instead of a checkout under pm2.

- **What it would give:** one image with Python and the pinned tools, so a new machine (or a NAS) needs Docker and a compose file, not a checkout, `.deps` and pm2. Tailscale fits well as a sidecar container later. Upgrades become "pull the new tag".
- **What gets in the way today:**
  - **The reach rules.** Inside a container harbor must listen on `0.0.0.0` for Docker's port mapping to reach it, and `config.check_reach` then refuses to start without HTTPS, accounts and allowed hosts, because harbor can't see that the host only publishes the port on `127.0.0.1`. Requests also arrive from Docker's bridge address, not loopback, so `X-Forwarded-*` from a proxy isn't trusted. A container mode needs its own, tested rule (for example "published on loopback only" declared in settings), never a quiet bypass.
  - **Paths.** Users type host paths (`/mnt/d/Comics`, `C:\Comics`), the Chrome extension sends them, and the folder picker browses WSL's drives. In a container those paths exist only where they're mounted. Mounting each folder at the *same* path inside the container keeps everything working; any other layout breaks saved jobs, library folders, presets and watch folders.
  - **File ownership.** Downloads and conversions write and delete the user's files. The container has to run as the user's uid/gid, or files on `/mnt/d` end up owned by root (or writes fail).
  - **Data.** `~/.local/share/harbor/` (accounts, history, library, state) needs a volume, and `harbor backup` has to reach it.
  - **The Chrome extension** only talks to `localhost`/`127.0.0.1`, which still works if the port is published on the host's loopback.
- **Recommendation:** not now. On this one WSL machine, pm2 and a checkout are simpler and every path works as typed. Revisit when harbor moves to another machine (a NAS or server), or together with Tailscale, and start with an image plus a compose file that mounts folders at identical paths and runs as the user's uid.
- **Open:** Is there a second machine in view? If not, this stays parked.
