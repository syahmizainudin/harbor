# Plan: site credentials

Status: design agreed 2026-11-23, not started. Decisions are under "Decided" at the end.

## What was asked

Save sign-in details per site, so a download picks the right one from its URL. Today there is one key: typed into the Download tab for each add, or `HARBOR_API_KEY` in `.env` for everything.

## How it works today

- A download carries a dl-queue `Auth(mode, key, name)`. The modes are `none`, `bearer` (`Authorization: Bearer <key>`), `header` (`<name>: <key>`) and `query` (`?<name>=<key>`).
- `EnvKeyDownloadAPI` puts in the `.env` key when the page sends a mode other than `none` with an empty key. The Chrome extension always sends an empty key.
- Header credentials go out with `add_unredirected_header`, so a redirect to another host (a CDN) doesn't get them. A query key is added to the request, never to the job's `url`.
- Saved jobs (`state.py`) keep only `auth_mode` and `auth_name`. After a restart, a download whose key was typed in is skipped with a note, because the key is gone.

## Design

### What a credential is

| Field | Meaning |
|---|---|
| Label | A name for people, such as "Example API" |
| Site | A host (`api.example.com`) or a wildcard (`*.example.com`), plus an optional path prefix (`/files/`) |
| Kind | dl-queue's `bearer`, `header` or `query`, plus a header or parameter name where needed |
| Secret | The key or token. Write-only: never sent back to the page |

### Picking one for a download

1. A key typed on the Download tab wins, as it does today.
2. Otherwise the most specific saved credential whose site matches the URL. An exact host beats a wildcard, and a longer path prefix beats a shorter one.
3. Otherwise the `.env` key, as today, for any mode other than `none`.

Credentials are matched for each URL, so one add with links from two sites uses two credentials. The page and the Chrome extension need no change to benefit: a preset with mode "Automatic" (new) leaves the choice to harbor.

### Where the code goes

- **dl-queue:** a hook `DownloadQueue(auth_for=)`, called when a job starts (and on each retry) with the job. It returns an `Auth` or None. The job stores only a reference, never the secret. As a result, **saved jobs restore with their credential** after a restart. Today they're skipped. Matching rules are harbor's, so dl-queue only gains the hook.
- **harbor:** storage, the API, matching, the page.

### Keeping secrets secret

These rules are the API key rules, applied to every credential:

- Never in `/api/state`, a log, a message, history, the audit log, a preset or the saved-jobs file. The page sees label, site, kind and name, plus "secret set".
- Changing a credential with the secret left blank keeps the old one. There is no "show" button.
- A credential is sent only over HTTPS, or to a loopback host (tests). A plain `http://` URL to another host downloads without it, with a note.
- Every add, change and removal goes in the audit log, with label and site only.

### Who has them, and where you manage them

- **Your own:** a **Sites** sub-tab in the Account dialog. Anyone with `download` can manage their own, and they're used only for downloads they queue.

## Steps

1. dl-queue: the `auth_for` hook with tests, then a release.
2. harbor: migration and storage, matching, the Account dialog's **Sites** sub-tab, restore through the hook, audit, and tests that the secret never appears in state, logs or the saved-jobs file.

## Decided (2026-11-23)

1. **Own only.** Each account keeps its own in the Account dialog's **Sites** sub-tab, used only for downloads it queues. No shared credentials and no `download.credentials` permission for now (step 3 is dropped).
2. **Encrypted at rest with the `cryptography` package** (Fernet). This is the first dependency besides the tools, so the "no dependencies" rule in `AGENTS.md` changes to allow it, pinned in `pyproject.toml`. The encryption key lives in its own file, `~/.local/share/harbor/secret.key` (mode 0600, made on first use), never in `harbor.db`. `harbor backup` copies the encrypted secrets with the database, but copies the key only with `--with-secrets`: a backup without it can't reveal them. `harbor doctor` checks the key file exists and is 0600. If the key file is lost, saved secrets can't be read: the page says so, and each credential asks for its secret again.
3. **API keys and tokens only:** dl-queue's `bearer`, `header` and `query`. No HTTP Basic, no cookies.
4. **HTTPS only.** Credentials go only to `https://` URLs, or to a loopback host (tests). A plain-HTTP download runs without the credential, with a note.

## Questions that were asked

1. Whose are they: own, shared, or both?
2. How are secrets stored: plain 0600 file, the `cryptography` package, or `.env` references only?
3. Which kinds of sign-in: API keys and tokens, HTTP Basic, cookies?
4. Plain HTTP: refuse, or allow per credential?
