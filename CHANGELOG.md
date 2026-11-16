# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

- A logo, shown in the page header and as the browser tab icon (`assets/logo.svg`).
- Settings from a `.env` file: `--env-file`, `$HARBOR_ENV_FILE`, `./.env` or `~/.config/harbor/.env`. Covers the port, worker counts, Convert defaults, Download defaults and an API key. Flags override environment variables, which override the file. See `.env.example`.
- An API key in `.env` (`HARBOR_API_KEY`) stays on the server. The page only knows one exists and uses it when its key field is left empty.

## 0.1.0

First release.

- `harbor ui` serves one local page with two tabs: **Convert** (cbz-webp v0.1.0) and **Download** (dl-queue v0.1.0).
- Downloaded `.zip` and `.cbz` files go straight into the convert queue (optional, on by default), using the Convert tab's quality and delete settings. Each download links to its convert job.
- Tests check that the code cbz-webp and dl-queue share by copy hasn't drifted apart.
