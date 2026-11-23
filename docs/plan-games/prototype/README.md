# Games phase 0 prototype

The scripts behind the phase 0 results in [plan-games.md](../../plan-games.md). They're kept so the result can be rerun. When `rpgm-shelf` is created, `make_game.py` moves into its `tests/helpers.py` as the test fixture, and this folder is deleted.

| File | What it does |
|---|---|
| `make_game.py` | Builds a tiny RPG Maker MV game: the MIT-licensed official corescript, generated `data/*.json` and PNGs, and any TTF as the game font. |
| `proto.py` | A **throwaway** server for option A (games on a second port) or B (sandboxed iframe on the page's port), with the save shim injected into `index.html`. Not a base for the real server (see its docstring). |
| `drive.cjs` | Plays the game in headless Chromium with WebGL: title, New Game, walk, menu, Save, reload, Continue, Load. Prints what it saw as JSON. |

## Run it

Keep downloaded code in its own folder, and don't run its build (`npm install`); `make_game.py` concatenates the engine files itself.

```bash
W=$(mktemp -d)
mkdir "$W/src"
gh api repos/rpgtkoolmv/corescript/tarball/master | tar xz -C "$W/src" --strip-components=1
# Pillow is needed; harbor's .deps has it through cbz-webp
PYTHONPATH=/app/tools/harbor/.deps python3 make_game.py "$W/src" "$W/game" /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf

# two free ports, then option A (use B for the sandboxed iframe)
python3 -I proto.py "$W/game" A 18801 18802 &
node drive.cjs 18801 A
kill %1
```

`drive.cjs` needs Playwright and a Chromium binary: set `PLAYWRIGHT` (path to the `playwright` package) and `CHROME` (for example `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`). It launches with SwiftShader so WebGL is on; check `"webgl": true` in the output before trusting a result. Screenshots go next to the script (`shot-*.png`, git-ignored).

## Expected result (2026-11-23)

- **A:** `webgl: true`, the player moves from x 8 to 10, the saves list holds `0` and `1`, `continueEnabled: true`, `loadedPos` equals `pos`, and the game's credentialed `fetch` to the page port is blocked.
- **B:** fails at the title with `SecurityError: The canvas has been tainted by cross-origin data` from `Bitmap.getPixel`.
- Both: a 404 for `img/system/Loading.png`, which the generated game doesn't include.
