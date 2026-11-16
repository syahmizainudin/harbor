<p align="center"><img src="assets/logo.svg" width="96" alt="Harbor logo"></p>

# harbor

[![CI](https://github.com/syahmizainudin/harbor/actions/workflows/ci.yml/badge.svg)](https://github.com/syahmizainudin/harbor/actions/workflows/ci.yml)

Harbor puts two tools on one local web page:

- **Convert** ([cbz-webp](https://github.com/syahmizainudin/cbz-webp)) recompresses the images inside `.zip` and `.cbz` archives to WebP and saves them as `.cbz`.
- **Download** ([dl-queue](https://github.com/syahmizainudin/dl-queue)) downloads files from direct URLs, optionally with an API key.

Downloaded archives can go straight into the Convert queue, so a list of URLs becomes a folder of compressed `.cbz` files.

## Install

Needs Python 3.10 or later. This repo is private, so `pip`/`pipx` need your GitHub access (for example, run `gh auth login` and then `gh auth setup-git`):

```bash
pipx install git+https://github.com/syahmizainudin/harbor
```

That also installs cbz-webp and dl-queue at the versions pinned in `pyproject.toml`, and Pillow. Each [release](https://github.com/syahmizainudin/harbor/releases) also has a wheel.

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
| `--env-file PATH` | | | Read settings from this file |

Ctrl+C stops the console. Running jobs are cancelled, originals are kept, and partial downloads are removed.

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

Values in the file fill the Download tab's fields only when this browser hasn't remembered its own value. To go back to the file's values, clear the site's data in the browser.

## The page

**Convert tab:** add folders or archives, or click **Open folder…** to browse WSL's folders, with shortcuts to home and each Windows drive. Set the quality and whether to delete each original `.zip` once its `.cbz` has been checked. See [cbz-webp's README](https://github.com/syahmizainudin/cbz-webp#what-happens-to-each-archive) for exactly what happens to each archive.

**Download tab:**

1. Paste file URLs and choose a folder to save to.
2. Choose how the API key is sent: Bearer, a named header, a named query parameter, or none.
3. Leave **Convert .zip / .cbz downloads** ticked to send each downloaded archive to the Convert queue, with the Convert tab's quality and delete settings. Each download then shows its convert job's progress.

The key is never shown in the queue or sent on redirects. See [dl-queue's README](https://github.com/syahmizainudin/dl-queue#how-downloads-are-handled) for how files are named and saved.

For each tool on its own, use `cbz-webp` and `dl-queue` directly; both have their own CLI and page.

## Development

```bash
pip install --target .deps .                         # the pinned cbz-webp and dl-queue (+ Pillow)
PYTHONPATH=.deps python3 -m unittest discover -s tests
ruff check . && ruff format --check .
```

GitHub Actions runs lint, the tests on Python 3.10 to 3.14 against the pinned versions, and a package smoke test on every push to `main` and every pull request. Pushing a `v<version>` tag publishes a GitHub release. [AGENTS.md](AGENTS.md) has the release steps and the rules for changing the code.
