# Changelog

Each release has a `## <version>` section. The release pipeline publishes that section as the GitHub release notes, and refuses to release a version without one.

## Unreleased

## 0.1.0

First release.

- `harbor ui` serves one local page with two tabs: **Convert** (cbz-webp v0.1.0) and **Download** (dl-queue v0.1.0).
- Downloaded `.zip` and `.cbz` files go straight into the convert queue (optional, on by default), using the Convert tab's quality and delete settings. Each download links to its convert job.
- Tests check that the code cbz-webp and dl-queue share by copy hasn't drifted apart.
