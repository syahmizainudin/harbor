# Plan: an RPG Maker MV/MZ library and player

Status: phase 1 released 2026-11-23 (`rpgm-shelf` 0.1.0, public). Phase 2 (harbor's Games tab) released 2026-11-23 in harbor 0.2.9 with rpgm-shelf 0.1.1. MZ released 2026-11-25 (`rpgm-shelf` 0.2.0, harbor 0.2.11). Option A (a separate games server) was chosen in phase 0.

## What was asked

- A browsable library of RPG Maker games in harbor, played in the browser.
- MV and MZ first. 2000/2003 (EasyRPG's web player) maybe later. XP/VX/VX Ace are out: they are Ruby and Windows programs.
- Saves are kept on the server, per harbor user.

## Where it lives

A new public tool, like cbz-shelf (working name `rpgm-shelf`): the scanner, the database, the player page, the save shim and the game file server. Harbor only mounts it as a **Games** tab, mints play tokens, adds permissions and audit lines, and later hands finished downloads to it. Harbor's rule holds: behaviour belongs in the tool, harbor holds the glue.

## The hard part: a game is untrusted code

An MV/MZ game is a folder of JavaScript (`js/rpg_*.js`, `js/plugins/*.js`). If harbor served it from harbor's own origin, the game's code could call harbor's APIs as the signed-in user: queue downloads, delete originals, use admin actions. So **games never run on harbor's origin.** There are two ways to keep them off it, and they need a prototype with a real game before we pick one.

### Option A: a separate origin (second listener)

A small read-only server, its own port, serving only game files and the save endpoints.

- **A port alone is not a cookie fence.** Browsers scope cookies by host, not port, so harbor's session cookie for `127.0.0.1:8765` is also sent to `127.0.0.1:8766`. Harbor itself stays safe (Origin check on POST, `X-Frame-Options: SAMEORIGIN`, GETs without side effects), but the games server must **never read cookies or authenticate by them**, with a test that a request carrying harbor's cookie gets nothing extra. A different hostname (`localhost` vs `127.0.0.1`) gives separate cookie jars and is better where possible.
- Games run as a normal origin, so engine compatibility is best.
- Remote use needs a second address through the proxy (`HARBOR_GAMES_URL`), and it falls under `config.check_reach` like everything else: HTTPS, allowed hosts, accounts.

### Option B: sandboxed iframe on harbor's port

`<iframe sandbox="allow-scripts">` without `allow-same-origin`: the game gets an opaque origin, so it can't read harbor's cookies or pass the Origin check.

- One port, no proxy changes.
- **Risk:** in an opaque origin every request is cross-origin. JSON loads need CORS headers (we control those), but MV/MZ draw with PIXI/WebGL, and an image loaded without `crossOrigin` from a "foreign" origin taints the canvas, which makes the WebGL upload fail. Whether PIXI handles this inside an opaque frame has to be tested, not assumed. Cookies aren't sent either, so asset URLs carry the play token.

**Recommendation:** prototype both against one real MV game and one real MZ game, then choose. Option A is the likely winner.

### Serving files

cbz-webp's shared server routes `<prefix><action>` with no `/` in the action, but a game loads nested relative paths (`data/Map001.json`, `img/characters/x.png`, `audio/bgm/y.ogg`). The game file server is therefore its own small handler in the new tool. It must: check Host, serve read-only, resolve every path inside the game folder and refuse escapes (`..`, absolute paths, symlinks pointing out), support `Range` for audio, and set a strict CSP (no requests outside the games origin). Harbor's "no handlers here" rule stays: the handler lives in the tool.

## Saves, per user, on the server

1. The user opens a game in harbor (their own signed-in origin). Harbor mints a short-lived **play token** bound to `(user, game)`.
2. The player URL on the games origin carries the token.
3. A shim, injected before the engine's own code, replaces `StorageManager` (MV and MZ differ here) so saves, config and global info go to `save`/`load` endpoints on the games origin, authorised by the token only.
4. The token reaches only that game's saves for that user. That bounds what malicious game code can do to "spoil its own saves".

Saves are stored in the tool's database (or files under its data folder), never in the game folder. Archives and game folders are only read, as in cbz-shelf. Export and import of a save slot are optional extras.

## Phases

### Phase 0: prototype (done 2026-11-23, MV only)

No real game was available, so the prototype used a generated MV game: the MIT-licensed official corescript (`rpgtkoolmv/corescript`), generated `data/*.json`, generated PNGs and DejaVu Sans Mono standing in for the game font. That builder becomes the new repo's test fixture (commercial game assets can't go in a public repo). The builder and the browser driver now live in `rpgm-shelf`'s `tests/e2e/` (the prototype folder was removed). Headless Chromium with SwiftShader, WebGL confirmed on (`Graphics.isWebGL()`).

- **Option B fails.** In `<iframe sandbox="allow-scripts">`, the title screen throws `SecurityError: The canvas has been tainted by cross-origin data` from `Bitmap.getPixel` (the window skin's text colours). MV reads pixels back from images it loads without `crossOrigin`, so an opaque origin can't run it. Not fixable without patching the engine.
- **Option A works end to end:** title, New Game, walking, menu, save to the server, reload, Continue enabled from the server's saves, load, player back where they saved. Map tiles and sprites draw through WebGL.
- **Save shim:** MV's `StorageManager` is synchronous, so the shim fetches all of the game's saves at boot, holds `Scene_Boot.isReady` until they arrive, re-runs `ConfigManager.load()`, then serves `load`/`exists` from memory and sends `save`/`remove` to the server in the background (`keepalive`). It replaces `isLocalMode`, `save`, `load`, `exists`, `remove`, `backup`, `cleanBackup`, `restoreBackup`. Saved: slot files and the global info (id 0); config is id -1.
- **Confirmed:** harbor's session cookie reaches the games port on every request (same host, other port). A game's `fetch` to harbor with credentials is blocked by the browser (no CORS on harbor).
- Headless key presses into a cross-origin frame are unreliable; tests drive the game through `frame.press` and the scenes' own handlers.
- Still to check with a real game: encrypted assets, audio, plugins, a real font. **MZ was untested** here: its web saves go through localforage (IndexedDB), so it needs its own shim (done, see MZ below).

### Phase 1: player over a folder (new tool, first release)

- Library folders like cbz-shelf's roots. A game is a folder with `www/index.html` (MV) or `index.html` + `data/System.json` (MZ). Read the title, engine and version from `System.json` and `package.json`.
- Cover: the title screen image (`img/titles1/<System.json title1Name>`), decrypted if needed. MV/MZ encrypt images with the key in `System.json`; the engine decrypts in the browser, so serving stays read-only.
- Grid, search, game page, Play button, server saves per user.
- Games that call NW.js-only features (`require(`, `nw.gui`, `process.`) are flagged "may not run in a browser" on their card. No shims in phase 1.

**Built 2026-11-23** (`rpgm-shelf` 0.1.0), checked with the real MV engine through `tests/e2e`:

- `Library` (folders, games, saves per user and game), `GameServer` (play tokens, read-only files, save endpoints), `shim_mv.js`, `GamesAPI(user_of=, can=)` with `games.play` and `games.manage`, the page and the `rpgm-shelf` command.
- Saves go with a plain `fetch` (a `keepalive` body is limited to 64 KiB), and a failed save shows a note in the game.
- **The CSP needs `'unsafe-eval'`**: without it PIXI gets no WebGL renderer. Other origins stay blocked.
- The page opens games under the other loopback name, so the two origins don't share cookies on this computer. With one shared host name (Tailscale) they do, and the games server's rule of never reading cookies is what protects harbor.
- `GameServer(frame_ancestors=)` is ready for phase 2, where harbor frames the player.

### Phase 2: harbor tab

- Games tab framing the tool's library page (like the Library tab).
- Permissions: `games.play` (open games, own saves), `games.manage` (folders, scans). Guests: none, at least at first.
- Play-token minting, audit lines for folder changes, tests in `test_permissions.py` and `test_accounts.py`.
- Games server start and stop in `cli.cmd_ui`, settings through `config.VARS`, `check_reach` cases in `tests/test_reach.py`, `harbor doctor` and `harbor backup` covering the new database.

**Phase 2 built 2026-11-23:** the Games tab framing `/games`, `GamesAPI(user_of=, can=)` with harbor's users and permissions (`games.play` given to Member by migration 11, `games.manage`), audit lines for folder changes, the games server started and stopped by `cli.cmd_ui` on `HARBOR_GAMES_PORT` (127.0.0.1 only), `HARBOR_GAMES`/`_DB`/`_SCAN`, doctor and backup covering `games.db`, and decision 4's cookie rule: harbor tries every `harbor_session` value, so a planted cookie can't hide the real one. Not done: remote play (needs the games server served through the proxy or `tailscale serve`, and `check_reach` rules).

### MZ (done 2026-11-25, rpgm-shelf 0.2.0)

Still no real MZ game, so it was built against the MZ 1.1.0 engine scripts from a GitHub mirror (not MIT like MV's corescript, so kept out of the repo; `make_game.py --mz` takes a path) and a generated MZ game.

- **Where MZ differs:** `index.html` loads only `js/main.js`, which adds the engine's scripts, so the save script can't patch at load. `shim_mz.js` patches as soon as `rmmz_managers.js` has run, before `main.js` loads plugins (a plugin that replaces `StorageManager` still wins, as with MV).
- **Saves:** MZ's `StorageManager` is promise-based and `Scene_Boot` already waits for `updateForageKeys()`, so that is where the saves are fetched. Only `saveZip`/`loadZip`/`exists`/`remove` change; the engine's JSON and zip steps stay. The zip is stored base64. Names map to the library's ids: `config` -1, `global` 0, `fileN` N + 1 (`file0` is MZ's autosave). A save the server refuses rejects, so the game shows its own "save failed".
- **Checked in `tests/e2e`** with both engines: WebGL on, save and load through the server with a new token, nothing in IndexedDB, no cookies, the library page unreadable. MZ's game loop stops while its page isn't focused; the driver focuses it after every open.
- Still to check with a real MZ game: encrypted assets (`.png_`, `.ogg_`), audio, effekseer animations, plugins, a newer engine version (1.9).

### Phase 3: manager extras

Tags, last played and play time, favourites, unpacking a game zip into a library folder, and the download → games hand-off (a finished download that is a game zip goes to the Games library instead of the converter).

## Decisions (2026-11-23)

1. **New public repo `rpgm-shelf`.** Created when phase 1 starts.
2. **Option A**, by phase 0's result.
3. **No real games yet.** Phase 1 is built and tested against the generated MV fixture, MZ (2026-11-25) against a generated MZ game on the MZ 1.1.0 engine.
4. **Local only for now; Tailscale later.** Tailscale gives one hostname per machine, so the games server will share harbor's host and cookie jar there too. So:
   - the games server never reads, logs or trusts cookies, and a test sends harbor's cookie to it and checks it gains nothing;
   - a game can also *set* cookies for the host that harbor then receives (cookie tossing): harbor's session parsing must ignore a duplicate or unexpected `sid` it didn't issue, with a test;
   - when Tailscale comes, the games port is served through `tailscale serve` with its own HTTPS port, and `check_reach` gains the matching rules.
