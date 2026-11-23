"""Server settings an admin changes on the Admin page's Settings tab (``settings.manage``).

Which settings: ``EDITABLE``. Start-up, security, path and secret settings (port, host, HTTPS,
accounts, databases, the API key...) stay in ``.env`` only, and ``READ_ONLY`` ones are shown but not
changed here (``HARBOR_WATCH_DELETE_ORIGINAL`` deletes files with nobody reviewing them).

Precedence: a setting given in ``.env``, the environment or a flag (``Settings.explicit``) is locked
here; otherwise a value saved here replaces the default. Values are stored as the text a ``.env``
would hold and checked with the same ``config.VARS`` validators.

``now`` settings change the running harbor at once; ``restart`` ones are saved and used from the
next start (the queues fix their workers when they start). The watch folders' ``Watcher`` is built
at start-up too, so it keeps the quality and effort harbor started with until a restart.
"""

from __future__ import annotations

from .config import VARS, ConfigError, Settings

# name -> (group, label, applies)
EDITABLE: dict[str, tuple[str, str, str]] = {
    "quality": ("Convert", "Quality (1-100)", "now"),
    "method": ("Convert", "Effort (0-6)", "now"),
    "delete_original": ("Convert", "Delete the original .zip after converting (true/false)", "now"),
    "jobs": ("Convert", "Conversions at once", "restart"),
    "threads": ("Convert", "Image threads per conversion", "restart"),
    "download_dir": ("Download", "Default download folder", "now"),
    "auth_mode": ("Download", "API key sent as (none, bearer, header, query)", "now"),
    "auth_name": ("Download", "Header or parameter name for the key", "now"),
    "downloads": ("Download", "Downloads at once", "restart"),
    "retries": ("Download", "Extra attempts after a temporary failure", "now"),
    "download_rate": ("Download", "Speed limit for all downloads, such as 5M (0: none)", "now"),
    "download_hours": ("Download", "Hours new downloads may start, such as 01:00-07:00", "now"),
    "download_fair": ("Download", "Take turns between sites (true/false)", "now"),
    "watch_dirs": ("Watch folders", "Folders, separated by ;", "restart"),
    "watch_poll": ("Watch folders", "Seconds between checks", "restart"),
    "session_days": ("Accounts and records", "Days a sign-in lasts", "now"),
    "history_days": ("Accounts and records", "Days finished jobs are kept in history (0: forever)", "now"),
    "audit_days": ("Accounts and records", "Days the audit log is kept (0: forever)", "now"),
    "library_scan": ("Accounts and records", "Rescan the library at start-up (true/false)", "restart"),
}
READ_ONLY: dict[str, tuple[str, str]] = {
    "watch_delete_original": ("Watch folders", "Delete originals of watched archives (only in .env)"),
}


def text(value) -> str:
    """A setting's value as .env text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def convert(name: str, raw: str):
    """Check ``raw`` with the setting's own validator; raises ValueError with a readable message."""
    var, conv = VARS[name]
    raw = str(raw).strip()
    if any(c in raw for c in "\r\n\0"):
        raise ValueError(f"{var} must be one line")
    if raw == "":
        return getattr(Settings(), name)
    try:
        return conv(raw, var)
    except ConfigError as e:
        raise ValueError(str(e)) from None


def apply_saved(settings: Settings, saved: dict[str, str]) -> list[str]:
    """At start-up: put saved values into ``settings``, skipping locked ones. Returns the names used.
    A saved value that no longer passes its check is skipped with a message, not a failed start."""
    used = []
    for name, raw in saved.items():
        if name not in EDITABLE or name in settings.explicit:
            continue
        try:
            setattr(settings, name, convert(name, raw))
            used.append(name)
        except ValueError as e:
            print(f"harbor: ignoring the saved setting {VARS[name][0]}: {e}", flush=True)
    return used


class LiveSettings:
    """The Settings tab's view of the running harbor, and the changes it makes."""

    def __init__(
        self, settings: Settings, accounts, convert_defaults=None, download_queue=None, history=None
    ):
        self.settings = settings
        self.accounts = accounts
        self.convert_defaults = convert_defaults
        self.download_queue = download_queue
        self.history = history
        # What the running harbor started with, for "applies after restart" settings.
        self.running = {name: getattr(settings, name) for name in EDITABLE}

    def rows(self) -> list[dict]:
        saved = self.accounts.saved_settings()
        defaults = Settings()
        out = []
        for name, (group, label, applies) in EDITABLE.items():
            value = getattr(self.settings, name)
            out.append(
                {
                    "name": name,
                    "env": VARS[name][0],
                    "group": group,
                    "label": label,
                    "applies": applies,
                    "value": text(value),
                    "running": text(self.running[name]),
                    "saved": saved.get(name),
                    "default": text(getattr(defaults, name)),
                    "locked": self.settings.explicit.get(name),
                }
            )
        for name, (group, label) in READ_ONLY.items():
            value = text(getattr(self.settings, name))
            out.append(
                {
                    "name": name,
                    "env": VARS[name][0],
                    "group": group,
                    "label": label,
                    "applies": "env",
                    "value": value,
                    "running": value,
                    "saved": None,
                    "default": text(getattr(defaults, name)),
                    "locked": self.settings.explicit.get(name) or ".env only",
                }
            )
        return out

    def set(self, name: str, raw) -> tuple[str, str]:
        """Save a setting ("" goes back to the default) and apply it if it applies now.
        Returns (old text, new text)."""
        if name not in EDITABLE:
            if name in READ_ONLY:
                raise ValueError(f"{VARS[name][0]} can only be set in .env")
            raise KeyError(f"no setting {name!r}")
        if name in self.settings.explicit:
            where = self.settings.explicit[name]
            raise ValueError(f"{VARS[name][0]} is set in {where}; remove it there to manage it here")
        if not isinstance(raw, str):
            raise ValueError("value must be text")
        value = convert(name, raw)  # raises ValueError
        old = text(getattr(self.settings, name))
        self.accounts.save_setting(name, text(value) if raw.strip() else None)
        setattr(self.settings, name, value)
        if EDITABLE[name][2] == "now":
            self._apply(name, value)
        return old, text(value)

    def _apply(self, name: str, value) -> None:
        """Change the running harbor. Settings the page reads (download folder, key mode, delete
        original) come from ``settings.page_defaults()`` on every request already."""
        dq = self.download_queue
        if name in ("quality", "method") and self.convert_defaults is not None:
            self.convert_defaults[name] = value
        elif dq is not None and name == "retries":
            dq.retries = value
        elif dq is not None and name == "download_rate":
            dq.limiter.rate = value
        elif dq is not None and name == "download_hours":
            from dlqueue.download import parse_hours

            dq.hours = parse_hours(value) if value else None
            dq.set_paused(dq.paused)  # wakes the workers, so a lifted window starts downloads now
        elif dq is not None and name == "download_fair":
            dq.fair = value
        elif name == "session_days":
            self.accounts.session_seconds = int(value) * 86400
        elif name == "audit_days":
            self.accounts.audit_days = value
        elif name == "history_days" and self.history is not None:
            self.history.keep_days = value
