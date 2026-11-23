# Harbor design

How harbor looks and how a request flows. New features, and later modules, follow it. The rules that keep files and secrets safe live in `AGENTS.md`; this file links to them rather than repeating them.

The four pages (harbor's `ui.html`, and the pages of cbz-webp, dl-queue and cbz-shelf) share these colours and pieces. A change here is made in all four.

## Front end

### Ground rules

- **One file per page, no build step, no CDN.** CSS and JS are inline, and the page works offline.
- **Dark mode comes from the colour tokens.** Never write a colour outside `:root`; use `var(--…)`. Every new piece is checked in both themes.
- **System font:** `14px/1.45 system-ui`. Monospace (`.mono`, 13px) for paths, URLs, tokens and code.
- **Redraw only what changes.** `render()` runs every poll. It rewrites stats and table bodies through `setHTML`/`setText`, which skip unchanged content. Forms, dialogs and anything holding focus stay outside what it rewrites.
- **The server decides, the page follows.** `applyPerms` and `has(perm)` hide what you can't use, but the check that counts is on the server (`AGENTS.md`, "Permissions").

### Colours

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--bg` | `#f6f7f9` | `#12151b` | Page background, inputs |
| `--panel` | `#ffffff` | `#1a1f27` | Panels, sidebar, dialogs, buttons |
| `--text` | `#1d2330` | `#e6e9ef` | Body text |
| `--muted` | `#667085` | `#98a2b3` | Notes, table headings, inactive tabs |
| `--line` | `#e3e6eb` | `#2a313c` | Borders and row lines |
| `--accent` | `#2f6fed` | `#5b8cff` | Primary buttons, links, selected tab, badges |
| `--accent-text` | `#fff` | `#0b1020` | Text on accent |
| `--ok` / `--warn` / `--bad` | `#16794c` / `#a15c00` / `#c0362c` | `#4cc38a` / `#f0a742` / `#ff6b5e` | Done, skipped, failed and errors |
| `--run` | `#2f6fed` | `#5b8cff` | Running state, progress bars |
| `--chip` / `--hover` / `--bar` | `#eef1f5` / `#f1f4f9` / `#e8edf7` | `#232a35` / `#222833` / `#252c38` | Chips, hover, progress track |

### Layout

- **Sidebar** (`nav.side`, 200px, collapses to 58px icons): the logo, one `.tab` per section with an icon and an optional `.badge` count, and the account or **Sign in** button at the bottom.
- **The open tab** fills the rest. Its content sits in `.pane-inner` (at most 1100px wide, centred), as a stack of `.panel`s.
- The Library tab frames cbz-shelf's page (`/library`). While a book is open the reader takes the whole window (`body.reading`).

### Pieces

| Piece | Markup | Use it for |
|---|---|---|
| Panel | `section.panel` | Each block of a tab |
| Panel heading | `.bar-head` with `<strong>` and a `.note` | A panel's title, one line on what it does, and its buttons on the right |
| Sub-tabs | `nav.subtabs` of `button[role=tab][aria-selected]` | Sections of one place (Admin, Account). Offer only the ones the user may use, hide the bar when there's one, remember the last (`store`), load data when a sub-tab opens |
| Chips | `.stats` of `span.chip`, `<b>` for the number | Live counts and settings ("2 download(s) at a time", "night 0/1 · waiting"). A chip may hold one `button.link` action |
| Table | `table`, `th` (muted, 12px), `td` | Lists of jobs, users, lanes. Actions go in the last cell as `.actions` of small buttons. An empty table shows one `td.empty` row saying what will appear |
| State label | `span.state.s-<state>` | Queued, running, done, skipped, failed, cancelled, uppercase in their colour |
| Progress | `.prog > i` (width %), `.prog.indet` when size is unknown | Running jobs |
| Buttons | `button` default, `.primary` (one per form), `.small` (in tables), `.link` (inline text) | Labels are verbs: "Save lane", "Add to queue", "Sign out" |
| Form, stacked | `label` then input; `.field` (input + button); `.grid` (columns); `.row` (wrapping line); `.check` (tick box + label) | Tab forms such as Convert and Download |
| Form, one line | `form.adm-form` | Admin "add or edit" rows: inputs wrap, the primary button ends the row, extras take a full line below |
| Message | `div.msg`, filled with `say(el, text, err)` | The result of an action, under its form. Errors in `--bad` |
| Dialog | `<dialog>` with `.dlg-head` (title, Close link on the right), `.dlg-body`, `.dlg-foot` (note on the left, main action on the right) | Account, sign-in, folder picker, series edit. Open with `showModal()`; size with `width: min(<px>, calc(100vw - 32px))` |
| Badge | `.badge` (hidden when empty) | Queued and running counts on a sidebar tab |
| List with actions | `ul.tokens` of `li` (text left, button right) | Tokens, sessions |

### Behaviour

- **Destructive or wide actions ask first** with `confirm()`, naming the thing and the result: "Remove the lane “night”? Its queued downloads run with the Download settings instead."
- **Messages say what happened**, in a sentence: "Saved the lane “manga”. It applies now." Errors show the server's own message.
- **Remembered in the browser** (`store`, `localStorage`, prefix `cbzwebp.`): the open tab and sub-tabs, a collapsed sidebar, form values that aren't account defaults. Never a secret unless the user ticks a box that says so (the API key).
- **Saved with the account:** defaults and presets, through `POST /api/account/prefs`; see `AGENTS.md`, "Per-user defaults".
- **Polling:** one `refresh()` at a time, every second on Convert and Download, every 5 seconds elsewhere or in a hidden tab, and at once after an action.
- **Signed out:** on harbor's page a 401 opens the sign-in dialog without leaving the page. The Library frame (cbz-shelf) sends the window to `/login`, which comes back to the dialog.
- **Phone width:** panels and forms wrap. A wide table goes in a wrapper with `overflow-x: auto` (as the roles matrix does), so it scrolls inside its panel, never the page.

### Words

Plain and short. Name things as the user sees them ("Download lanes", "Who can use it"), never by code names. Say "downloads at once", not "workers"; "speed limit", not "rate". Notes say what something does and what happens next, in one or two sentences.

## Back end

### A request's path

Every page and API goes through cbz-webp's shared server (`serve`), in this order:

1. **Host check.** Only `127.0.0.1`, `localhost` or `HARBOR_ALLOWED_HOSTS`, which stops DNS rebinding.
2. **POST only:**
    1. The `Origin` must be the page's own or the Chrome extension's.
    2. The body must be JSON (`Content-Type: application/json`), so a cross-site form can't post.
3. **Sign-in** (`HarborSignIn.user`): a session cookie, an extension token, or a reader token (HTTP Basic, library only).
4. **Signed in:** `HarborSignIn.allows` can refuse with 403. **Signed out:** public paths and what a guest may do (`perms.guest_may`) go on. A page gets a redirect to `/login`, which opens the sign-in dialog. Anything else gets 401.
5. **The API object** for the path's prefix: `/api/` (convert), `/api/download/`, `/api/harbor/`, `/api/account/`, `/api/admin/`, `/api/history/`, `/api/library/`. Each has:
    - `state()`: its part of `GET /api/state`, merged from every API object
    - `get(action, query)`
    - `post(action, body)`, returning `(status, object)`
6. **Errors.** A `ValueError`, `KeyError`, `TypeError` or `OSError` raised inside `get` or `post` becomes 400 with its message. An API returns 403 or 404 itself where that's the meaning.

### Where code goes

| Change | Where |
|---|---|
| Queue, file, download or server logic | The tool (cbz-webp, dl-queue, cbz-shelf), released and pinned (`AGENTS.md`, "Where a change belongs") |
| A permission check on a queue action | `perms.GuardedQueueAPI`. Actions it doesn't list pass through, so a new one needs its check here |
| An admin action | `admin.AdminAPI`: check the permission, keep the no-escalation rule, write an audit line |
| A per-user setting | `accounts.PREFS` or `PRESET_FIELDS`, validated, never a secret |
| A server setting | `config.VARS` with a validator, plus `.env.example` |
| A schema change | One new step in `accounts.MIGRATIONS`; never drop user data |
| A new `/api/state` key the page reads | Pinned in `test_page_and_merged_state` and listed in `AGENTS.md` |

### The checklist for a new action

1. Permission checked on the server, with a test in `tests/test_permissions.py`.
2. An audit line if it changes something shared (users, roles, libraries, lanes).
3. Errors as `ValueError` with a message a person can act on.
4. Nothing secret in the answer, the state, a log or an audit line.
5. The page hides it from people who can't use it, and shows the server's message when it fails.
6. Checked in a browser in both themes, against a throwaway harbor.
