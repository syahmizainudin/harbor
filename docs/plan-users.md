# Plan: accounts, permissions, favourites and per-user settings

Status: phase 1 released (cbz-shelf 0.3.0). Phase 2 released (cbz-webp 0.4.0, dl-queue 0.4.0, cbz-shelf 0.3.2, harbor). Phase 3 released (cbz-webp 0.4.1, dl-queue 0.4.1, cbz-shelf 0.4.0, harbor), with phase 4's per-user reader settings pulled in. Phase 4 released (cbz-shelf 0.4.1, harbor). All four phases done. Decisions settled 2026-11-16, below.

## What was asked

- Accounts with passwords. harbor stays on 127.0.0.1 (this computer only).
- Per user: reading progress and Continue reading, reader settings, Convert/Download defaults, metadata edits.
- An admin page: user management, a permission (security) matrix, and series management.
- Favourites on series and books.
- Continue reading: a × on each card hides it from the row and keeps the progress. Also a "Clear all".

## Decisions (settled 2026-11-16)

1. **Metadata edits are shared, and only admins edit them.** Book and series details (titles, tags, series names and so on) are one shared layer, so search, filters and sorting stay as they are. Editing is the `library.edit` permission, which only the admin role has by default. The admin's series management works on that layer. Per-user edits are not planned.
2. **Sign-in is checked in the console server.** cbz-webp's generic part (copied unchanged into dl-queue) gets an optional `auth` hook and a per-request `current_user()`. Standalone cbz-webp, dl-queue and cbz-shelf see user 0 and behave as today. This needs releases of cbz-webp and dl-queue, and harbor's shared-code test moves with them.
3. **Designed for LAN access later.** harbor stays on 127.0.0.1 for now, but sign-in is built so that binding to the network later is safe:
   - sessions in an `HttpOnly; SameSite=Strict` cookie, plus the existing Origin and JSON-only checks on every POST;
   - the Host check accepts only configured names;
   - when harbor is reachable from other devices, it must be behind HTTPS (cookie `Secure`), and passwords are required for every account.
   On 127.0.0.1 alone, passwords separate people sharing this computer's browser. They don't protect against anyone who can read `~/.local/share/harbor/`.

## Phases (each one shippable)

### Phase 1: favourites and Continue reading (cbz-shelf 0.3.0)

- Migration 2: `progress` gains `user_id` (existing rows become user 0) and `hidden`. New `favourites(user_id, kind, item_id)` for kind `series` or `book`.
- `continue_reading` leaves out hidden rows. Reading a book again un-hides it.
- Endpoints: `favourite` / `unfavourite`, `hide-continue`, `clear-continue`. `series_list` and `search` take `favourite=1`.
- Page: a star on series and book cards and in book details. A Favourites row on Home, a favourites filter, × on Continue reading cards, and "Clear all".
- Everything keyed by `user_id` from the start, so phase 2 only has to supply the user.

### Phase 2: accounts and sign-in (cbz-webp 0.4.0, dl-queue 0.4.0, cbz-shelf 0.4.0, harbor)

- Generic server: `serve(..., auth=...)`. The hook returns a user or refuses. `current_user()` is a thread-local (one thread per request, `ThreadingHTTPServer`).
- harbor's own database (`~/.local/share/harbor/harbor.db`, versioned migrations like cbz-shelf): `users(id, name, pass_hash, role_id, disabled)`, `sessions`, `tokens`.
- Passwords: `hashlib.scrypt` with a per-user salt. Sessions: random token in an `HttpOnly; SameSite=Strict` cookie, stored hashed, expiring. Sign-in attempts are rate-limited.
- Every page and API needs a session, GETs included. `/` and `/library` go to the sign-in page.
  - Changed later: signed out, the page and the library can be read (not favourites; progress isn't saved). Converting, downloading and the rest still need a session. See AGENTS.md, "Guests".
- First admin from the command line: `harbor user add NAME --admin`. No open setup page, so there is never an unauthenticated window.
- Existing progress (user 0) is given to the first admin.
- The browser extension can't use the cookie. Each user can create a token on their account page and paste it into the extension, which sends it as a header. The extension's origin allowlist stays.

### Phase 3: roles, the permission matrix and the admin tab (harbor, cbz-shelf)

- Fixed permissions: `convert`, `download`, `delete_original`, `library.read`, `library.edit`, `library.manage` (folders, rescans, forget missing), `jobs.see_all` (see and cancel other users' jobs), `users.manage`, `roles.manage`.
- Roles are editable sets of permissions. `admin` has all of them and can't be changed or left without a member.
- Admin tab: users (add, disable, reset password, role), the roles × permissions matrix, and series management (rename, merge-free edits of the shared series metadata, reader defaults).
- Jobs record who queued them; without `jobs.see_all` a user sees only their own.

### Phase 4: per-user settings (harbor, cbz-shelf)

- Server-side `prefs` per user: Convert/Download defaults (quality, effort, delete original, download folder, auth mode) and the text reader's size, width and theme. `.env` values stay the fallback. The API key stays server-side and shared.
- Reader settings per series (layout, fit, direction) move from the shared series row to per user, with the shared value as the default.

## Constraints

- Every schema change is a migration step; never drop user data. Test migrations on copies, never on the live databases.
- `/api/state` is polled every 1.5 s by the page and the extension, so per-user filtering must stay cheap.
- Tests never touch the real databases, folders or the pm2 processes.
