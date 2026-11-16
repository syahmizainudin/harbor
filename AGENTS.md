# AGENTS.md

Instructions for coding agents working in this repository. Read `README.md` for what harbor does for its users.

## What this repo is

Harbor serves two public tools on one two-tab local page and connects them: a finished download can be queued for conversion.

| Repo | What it is |
|---|---|
| `harbor` (this one, private) | The combined page, the download → convert hand-off, and the `harbor` command |
| `cbz-webp` (public) | The converter: `JobQueue`, `ConvertAPI`, and the generic server `serve` that harbor uses |
| `dl-queue` (public) | The downloader: `DownloadQueue` with the `on_saved` and `check_after` hooks, `Auth`, `DownloadAPI` |

Both are dependencies in `pyproject.toml`, pinned by git tag.

| Path | What it is |
|---|---|
| `harbor/app.py` | `check_after` (validates a download's convert options), `make_on_saved` (the hand-off), `build` (starts both queues) and `serve_harbor` (mounts `ConvertAPI` at `/api/`, `EnvKeyDownloadAPI` at `/api/download/` and `HarborAPI`, page defaults only, at `/api/harbor/`) |
| `harbor/config.py` | Settings: `.env` parsing, file lookup, precedence (flag > environment > file > default), validation |
| `harbor/ui.html` | The two-tab page: one file, CSS and JS inline (including the logo and favicon), no external requests |
| `assets/logo.svg` | The logo. The page inlines it twice (header and favicon data URI); `LogoTests` checks they match |
| `.env.example` | Every setting, commented. Real `.env` files are git-ignored |
| `harbor/cli.py` | `harbor ui` |
| `bin/harbor` | Runs the CLI from a checkout |
| `tests/` | `test_harbor.py` (hand-off, console, CLI, shared-code check), `test_config.py` (settings, `.env` key, logo), `helpers.py` (archive builders and `FileServer`, copied from the two tools' tests) |
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

## Never touch the user's real files while testing

- Run against temp folders and `helpers.FileServer` on `127.0.0.1` only, never a real URL, folder or API key.
- To try the console, start your own copy on a free port (`bin/harbor ui -p <port>`, with the dependencies on `PYTHONPATH`) and stop it by PID when done. Do not stop a console you did not start.

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
