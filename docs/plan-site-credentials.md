# Plan: site credentials

Status: design, not started (2026-11-23). Four questions at the end need answers before building.

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
2. Otherwise the most specific saved credential whose site matches the URL. An exact host beats a wildcard, and a longer path prefix beats a shorter one. Your own credential beats a shared one.
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
- **Shared** (optional, see question 1): a **Site credentials** sub-tab on the Admin page, with a new `download.credentials` permission. Used for everyone's downloads, after their own.

## Steps

1. dl-queue: the `auth_for` hook with tests, then a release.
2. harbor: migration and storage, matching, the Account dialog's **Sites** sub-tab, restore through the hook, audit, and tests that the secret never appears in state, logs or the saved-jobs file.
3. If wanted: shared credentials on the Admin page.

## Questions before building

1. **Whose are they?** Each account its own (recommended to start), shared ones managed by an admin, or both?
2. **How are secrets stored?** Python's standard library has no encryption, and harbor allows no other dependencies. The options:
    - **(a) Recommended:** plain text in their own file, `credentials.db` (mode 0600, next to `harbor.db`). That's the same protection the `.env` key has today. `harbor backup` would copy it only with `--with-secrets`.
    - **(b)** Add the `cryptography` package to encrypt secrets at rest, with the encryption key in a separate 0600 file. This breaks the "no dependencies besides the tools" rule, and the key still lives on the same disk.
    - **(c)** Don't store secrets at all, only references to `HARBOR_KEY_<NAME>` entries in `.env`. This is the safest option, but you edit `.env` and restart to add one.
3. **Which kinds of sign-in?** API keys and tokens only (the existing modes; recommended), or also user name and password (HTTP Basic) and pasted browser cookies? Cookies expire and are tied to a browser session, so they bring support questions.
4. **Plain HTTP:** refuse to send a credential to a non-HTTPS site (recommended), or allow it per credential with a warning?
