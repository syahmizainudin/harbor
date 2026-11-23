"""Settings from a ``.env`` file, environment variables and command-line flags.

Precedence, highest first: command-line flag > environment variable > ``.env`` file > default.

The ``.env`` file is the first that exists of:
  1. ``--env-file PATH`` (must exist when given);
  2. ``$HARBOR_ENV_FILE`` (must exist when set);
  3. ``./.env`` in the current folder;
  4. ``~/.config/harbor/.env``.
A missing file is fine: harbor runs on defaults. The API key, if set, stays on the server; the
page is only told that one exists.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

AUTH_MODES = ("none", "bearer", "header", "query")
LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class ConfigError(ValueError):
    pass


def parse_env(text: str, source: str = ".env") -> dict[str, str]:
    """Parse ``KEY=value`` lines. Blank lines and ``#`` comments are skipped; values may be quoted.

    Unquoted values end at `` #`` (an inline comment). Nothing is expanded or executed.
    """
    values: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(raw)
        if not m:
            raise ConfigError(f"{source}:{n}: expected KEY=value")
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value and value[0] in "\"'":
            raise ConfigError(f"{source}:{n}: unclosed quote")
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


XDG_DEFAULTS = {"XDG_DATA_HOME": ".local/share", "XDG_STATE_HOME": ".local/state", "XDG_CACHE_HOME": ".cache"}


def xdg_path(var: str, *parts: str) -> Path:
    """``$var/parts`` (``var`` is one of ``XDG_DEFAULTS``), or under its default in the home folder."""
    return Path(os.environ.get(var) or Path.home() / XDG_DEFAULTS[var], *parts)


def find_env_file(explicit: str | None, environ: dict[str, str] | None = None) -> Path | None:
    environ = os.environ if environ is None else environ
    for given in (explicit, environ.get("HARBOR_ENV_FILE")):
        if given:
            p = Path(os.path.expanduser(given))
            if not p.is_file():
                raise ConfigError(f"env file not found: {p}")
            return p
    for p in (Path.cwd() / ".env", Path.home() / ".config" / "harbor" / ".env"):
        if p.is_file():
            return p
    return None


@dataclass
class Settings:
    port: int = 8765
    quality: int = 80
    jobs: int = 1
    threads: int = field(default_factory=lambda: os.cpu_count() or 2)
    downloads: int = 2
    delete_original: bool = True
    download_dir: str = ""
    auth_mode: str = "bearer"
    auth_name: str = ""
    api_key: str = field(default="", repr=False)
    method: int = 4
    retries: int = 3
    state_file: str = ""  # empty: default_state_file()
    restore: bool = True
    extension_origins: str = ""  # empty: harbor's own Chrome extension; "off": none
    library_db: str = ""  # empty: app.default_library_db()
    library_scan: bool = True  # rescan library folders at start-up
    accounts: bool = True  # require signing in (accounts in accounts_db)
    accounts_db: str = ""  # empty: accounts.default_accounts_db()
    session_days: int = 30  # how long a sign-in lasts without use
    secure_cookie: bool = False  # mark the session cookie Secure: only when harbor is behind HTTPS
    history_db: str = ""  # empty: history.default_history_db()
    history_days: int = 90  # finished jobs kept in the history; 0 = forever
    audit_days: int = 365  # account and admin events kept in the audit log; 0 = forever
    host: str = "127.0.0.1"  # address to listen on; anything but loopback needs HTTPS (check_reach)
    allowed_hosts: str = ""  # host names the page may be reached by, comma-separated
    tls_cert: str = ""  # HTTPS certificate (PEM), with tls_key
    tls_key: str = ""
    trust_proxy: bool = False  # a reverse proxy on this machine serves harbor over HTTPS
    local_port: int = 0  # with harbor's own HTTPS: a plain-HTTP port on 127.0.0.1 for the extension
    download_rate: int = 0  # bytes a second for all downloads together; 0 = no limit
    download_hours: str = ""  # "01:00-07:00": new downloads start only then
    download_fair: bool = False  # take turns between sites instead of first queued, first started
    watch_dirs: str = ""  # folders whose new archives are converted on their own, ";"-separated
    watch_delete_original: bool = False  # never follows HARBOR_DELETE_ORIGINAL: nobody reviews these
    watch_poll: int = 30  # seconds between checks of the watch folders
    source: str | None = None  # the .env file used, for the startup message

    def page_defaults(self) -> dict:
        """What the page may know. Never includes the key itself."""
        return {
            "download_dir": self.download_dir,
            "auth_mode": self.auth_mode,
            "auth_name": self.auth_name,
            "has_key": bool(self.api_key),
            "delete_original": self.delete_original,
            "method": self.method,
        }

    def allowed_origins(self) -> set[str]:
        """Origins (besides the page) allowed to send requests: harbor's Chrome extension by default."""
        from .extension import EXTENSION_ORIGIN

        value = self.extension_origins.strip()
        if value.lower() == "off":
            return set()
        if not value:
            return {EXTENSION_ORIGIN}
        return {o.strip() for o in value.split(",") if o.strip()}


# setting name -> (environment variable, converter)
def _int(lo: int, hi: int | None = None):
    def conv(v: str, name: str) -> int:
        try:
            n = int(v)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number, got {v!r}") from None
        if n < lo or (hi is not None and n > hi):
            raise ConfigError(f"{name} must be {lo}-{hi}" if hi else f"{name} must be at least {lo}")
        return n

    return conv


def _bool(v: str, name: str) -> bool:
    low = v.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {v!r}")


def _auth_mode(v: str, name: str) -> str:
    if v not in AUTH_MODES:
        raise ConfigError(f"{name} must be one of {', '.join(AUTH_MODES)}, got {v!r}")
    return v


def _origins(v: str, name: str) -> str:
    if v.strip().lower() == "off":
        return "off"
    for o in (x.strip() for x in v.split(",")):
        if o and not re.fullmatch(r"(chrome-extension://[a-p]{32}|https?://[A-Za-z0-9.-]+(:\d+)?)", o):
            raise ConfigError(f"{name}: {o!r} is not an exact origin like chrome-extension://<id>")
    return v


def _host(v: str, name: str) -> str:
    v = v.strip()
    if not re.fullmatch(r"[0-9.]+|[0-9A-Fa-f:]+|localhost", v):
        raise ConfigError(f"{name} must be an IP address to listen on (such as 0.0.0.0), got {v!r}")
    return v


def _host_names(v: str, name: str) -> str:
    for h in (x.strip() for x in v.split(",")):
        if h and not re.fullmatch(r"[A-Za-z0-9.-]+|[0-9A-Fa-f:]+", h):
            raise ConfigError(f"{name}: {h!r} is not a host name or address (no scheme, port or path)")
        if h in ("*", "0.0.0.0"):
            raise ConfigError(f"{name}: list real names, not {h!r}")
    return v


def _rate(v: str, name: str) -> int:
    from dlqueue.download import parse_size

    try:
        return parse_size(v)
    except ValueError as e:
        raise ConfigError(f"{name}: {e}") from None


def _hours(v: str, name: str) -> str:
    from dlqueue.download import parse_hours

    try:
        parse_hours(v)
    except ValueError as e:
        raise ConfigError(f"{name}: {e}") from None
    return v.strip()


def _text(v: str, name: str) -> str:
    if any(c in v for c in "\r\n\0"):
        raise ConfigError(f"{name} contains a line break")
    return v


VARS = {
    "port": ("HARBOR_PORT", _int(1, 65535)),
    "quality": ("HARBOR_QUALITY", _int(1, 100)),
    "jobs": ("HARBOR_JOBS", _int(1)),
    "threads": ("HARBOR_THREADS", _int(1)),
    "downloads": ("HARBOR_DOWNLOADS", _int(1)),
    "delete_original": ("HARBOR_DELETE_ORIGINAL", _bool),
    "download_dir": ("HARBOR_DOWNLOAD_DIR", _text),
    "auth_mode": ("HARBOR_AUTH_MODE", _auth_mode),
    "auth_name": ("HARBOR_AUTH_NAME", _text),
    "api_key": ("HARBOR_API_KEY", _text),
    "method": ("HARBOR_METHOD", _int(0, 6)),
    "retries": ("HARBOR_RETRIES", _int(0)),
    "state_file": ("HARBOR_STATE_FILE", _text),
    "restore": ("HARBOR_RESTORE", _bool),
    "extension_origins": ("HARBOR_EXTENSION_ORIGINS", _origins),
    "library_db": ("HARBOR_LIBRARY_DB", _text),
    "library_scan": ("HARBOR_LIBRARY_SCAN", _bool),
    "accounts": ("HARBOR_ACCOUNTS", _bool),
    "accounts_db": ("HARBOR_ACCOUNTS_DB", _text),
    "session_days": ("HARBOR_SESSION_DAYS", _int(1, 365)),
    "secure_cookie": ("HARBOR_SECURE_COOKIE", _bool),
    "history_db": ("HARBOR_HISTORY_DB", _text),
    "history_days": ("HARBOR_HISTORY_DAYS", _int(0, 3650)),
    "audit_days": ("HARBOR_AUDIT_DAYS", _int(0, 3650)),
    "host": ("HARBOR_HOST", _host),
    "allowed_hosts": ("HARBOR_ALLOWED_HOSTS", _host_names),
    "tls_cert": ("HARBOR_TLS_CERT", _text),
    "tls_key": ("HARBOR_TLS_KEY", _text),
    "trust_proxy": ("HARBOR_TRUST_PROXY", _bool),
    "local_port": ("HARBOR_LOCAL_PORT", _int(0, 65535)),
    "download_rate": ("HARBOR_DOWNLOAD_RATE", _rate),
    "download_hours": ("HARBOR_DOWNLOAD_HOURS", _hours),
    "download_fair": ("HARBOR_DOWNLOAD_FAIR", _bool),
    "watch_dirs": ("HARBOR_WATCH_DIRS", _text),
    "watch_delete_original": ("HARBOR_WATCH_DELETE_ORIGINAL", _bool),
    "watch_poll": ("HARBOR_WATCH_POLL", _int(5, 3600)),
}


def watch_list(s: Settings) -> list[str]:
    return [d.strip() for d in s.watch_dirs.split(";") if d.strip()]


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def check_reach(s: Settings) -> list[str]:
    """Why these settings may not start harbor (empty when they may). Anything that lets another
    device reach harbor needs HTTPS (its own or a proxy's), the Secure cookie, accounts, and the
    host names it is reached by. Plan decision 13 (docs/plan-features.md)."""
    problems = []
    if bool(s.tls_cert) != bool(s.tls_key):
        problems.append("set both HARBOR_TLS_CERT and HARBOR_TLS_KEY, or neither")
    for name, path in (("HARBOR_TLS_CERT", s.tls_cert), ("HARBOR_TLS_KEY", s.tls_key)):
        if path and not Path(path).expanduser().is_file():
            problems.append(f"{name}: no such file: {path}")
    key = Path(s.tls_key).expanduser() if s.tls_key else None
    if key and key.is_file() and key.stat().st_mode & 0o077:
        problems.append(f"HARBOR_TLS_KEY {key} can be read by other users: chmod 600 {key}")
    local = s.host in LOOPBACK_HOSTS
    if s.trust_proxy and not local:
        problems.append("HARBOR_TRUST_PROXY needs HARBOR_HOST=127.0.0.1: the proxy runs on this machine")
    exposed = not local or s.trust_proxy
    if exposed:
        if not local and not s.tls_cert:
            problems.append(
                f"HARBOR_HOST={s.host} lets other devices connect: HTTPS is required"
                " (HARBOR_TLS_CERT and HARBOR_TLS_KEY, or a reverse proxy with HARBOR_TRUST_PROXY)"
            )
        if not s.accounts:
            problems.append("harbor can be reached from other devices: HARBOR_ACCOUNTS must be true")
        if not s.allowed_hosts.strip():
            problems.append(
                "set HARBOR_ALLOWED_HOSTS to the name(s) or address(es) devices use to reach harbor"
            )
    if s.local_port and not s.tls_cert:
        problems.append("HARBOR_LOCAL_PORT is only for when harbor serves HTTPS itself (HARBOR_TLS_CERT)")
    if s.local_port and s.local_port == s.port:
        problems.append("HARBOR_LOCAL_PORT must differ from HARBOR_PORT")
    if (exposed or s.tls_cert) and not s.secure_cookie:
        problems.append("harbor is served over HTTPS: set HARBOR_SECURE_COOKIE=true")
    return problems


def host_list(s: Settings) -> list[str]:
    return [h.strip() for h in s.allowed_hosts.split(",") if h.strip()]


def load_settings(
    flags: dict | None = None, env_file: str | None = None, environ: dict[str, str] | None = None
) -> Settings:
    """Build settings from defaults, the .env file, the environment and flags (None = not given)."""
    environ = dict(os.environ if environ is None else environ)
    path = find_env_file(env_file, environ)
    file_values = parse_env(path.read_text(encoding="utf-8"), str(path)) if path else {}
    unknown = sorted(k for k in file_values if k.startswith("HARBOR_") and k not in _known_vars())
    if unknown:
        raise ConfigError(f"{path}: unknown setting(s): {', '.join(unknown)}")

    s = Settings(source=str(path) if path else None)
    for name, (var, conv) in VARS.items():
        for layer, label in ((file_values, str(path)), (environ, "environment")):
            if var in layer and layer[var] != "":
                setattr(s, name, conv(layer[var], f"{var} ({label})"))
    for name, value in (flags or {}).items():
        if value is not None:
            setattr(s, name, value)
    return s


def _known_vars() -> set[str]:
    return {var for var, _ in VARS.values()} | {"HARBOR_ENV_FILE"}
