"""The bot's settings: one file, /srv/moa/env/ops-bot.env (root, mode 600).

Only this file is ever read for settings -- never the stack's .env or any
other env file. The token is kept out of repr() and out of every log line.
"""

from __future__ import annotations

import os
import re
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = "/srv/moa/env/ops-bot.env"
#: The owner's age recipient (public). The private key never touches the box.
DEFAULT_AGE_RECIPIENT = "age17jsckeq68nf0yrz7sgtrrk7y6930uykd3t4je4t9tmnk8jqrwagqmmwu9v"

TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,64}$")
CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
AGE_RECIPIENT_RE = re.compile(r"^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$")

#: Exit status for "not configured / misconfigured": systemd does not
#: restart on it (RestartPreventExitStatus=78 in the unit).
EX_CONFIG = 78


class ConfigError(Exception):
    def __init__(self, message: str, *, unset: bool = False):
        super().__init__(message)
        #: True when the bot is simply not set up yet (no file, no token):
        #: deliver-backup then skips quietly instead of failing the backup.
        self.unset = unset


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    chat_id: int
    age_recipient: str = DEFAULT_AGE_RECIPIENT
    host_label: str = ""
    backup_dir: Path = Path("/var/backups/myotherapp")
    container_backup_dir: str = "/backups"
    work_dir: Path = Path("/var/lib/moa-ops-bot")
    compose_project: str = "moa"
    dc: str = "/srv/moa/dc"
    backup_unit: str = "myotherapp-backup.service"
    disk_paths: tuple[str, ...] = ("/", "/srv/moa-data", "/var/backups/myotherapp")
    watch_interval: int = 60
    error_min_lines: int = 3
    error_ignore_services: frozenset[str] = frozenset()
    mem_low_pct: int = 10

    def __repr__(self) -> str:  # the token never appears, even in a traceback's locals
        return f"Config(chat_id={self.chat_id}, host={self.host_label!r})"

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.token,)


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines; # comments, blank lines, `export ` and one pair of
    surrounding quotes allowed. Nothing is expanded or executed."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _int(values: dict[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = values.get(key, "")
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a whole number") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return number


def load(path: str | os.PathLike[str] | None = None, *, check_mode: bool = True) -> Config:
    """The settings, or ConfigError saying what is wrong (never the token)."""
    path = Path(path or os.environ.get("OPS_BOT_CONFIG") or DEFAULT_PATH)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ConfigError(f"{path} does not exist (install.sh creates it from the template)", unset=True) from None
    if check_mode:
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(f"{path} must be readable by its owner only: chmod 600 {path}")
        if os.geteuid() == 0 and info.st_uid != 0:
            raise ConfigError(f"{path} must be owned by root: chown root:root {path}")
    values = parse_env(path.read_text(encoding="utf-8"))

    token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat = values.get("TELEGRAM_CHAT_ID", "")
    if not token:
        raise ConfigError(f"TELEGRAM_BOT_TOKEN is empty in {path}", unset=True)
    if not TOKEN_RE.match(token):
        raise ConfigError("TELEGRAM_BOT_TOKEN does not look like a bot token (digits:letters, from @BotFather)")
    if not chat:
        raise ConfigError(f"TELEGRAM_CHAT_ID is empty in {path}")
    if not CHAT_ID_RE.match(chat):
        raise ConfigError("TELEGRAM_CHAT_ID must be a number (the owner's chat id)")
    recipient = values.get("OPS_AGE_RECIPIENT") or DEFAULT_AGE_RECIPIENT
    if not AGE_RECIPIENT_RE.match(recipient):
        raise ConfigError("OPS_AGE_RECIPIENT is not an age public key (age1...)")

    def path_value(key: str, default: Path) -> Path:
        raw = values.get(key, "")
        return Path(raw) if raw else default

    disks = tuple(p.strip() for p in values.get("OPS_DISK_PATHS", "").split(",") if p.strip())
    project = values.get("OPS_COMPOSE_PROJECT", "moa")
    if project and not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ConfigError("OPS_COMPOSE_PROJECT must be a compose project name")
    defaults = Config(token="", chat_id=0)
    return Config(
        token=token,
        chat_id=int(chat),
        age_recipient=recipient,
        host_label=(values.get("OPS_HOST_LABEL") or socket.gethostname())[:40],
        backup_dir=path_value("OPS_BACKUP_DIR", defaults.backup_dir),
        container_backup_dir=values.get("OPS_CONTAINER_BACKUP_DIR") or defaults.container_backup_dir,
        work_dir=path_value("OPS_WORK_DIR", defaults.work_dir),
        compose_project=project,
        dc=values.get("OPS_DC") or defaults.dc,
        backup_unit=values.get("OPS_BACKUP_UNIT") or defaults.backup_unit,
        disk_paths=disks or defaults.disk_paths,
        watch_interval=_int(values, "OPS_WATCH_INTERVAL", 60, 15, 3600),
        error_min_lines=_int(values, "OPS_ERROR_MIN_LINES", 3, 1, 10000),
        error_ignore_services=frozenset(s.strip() for s in values.get("OPS_ERROR_IGNORE_SERVICES", "").split(",") if s.strip()),
        mem_low_pct=_int(values, "OPS_MEM_LOW_PCT", 10, 1, 90),
    )


def is_configured(path: str | os.PathLike[str] | None = None) -> bool:
    try:
        load(path)
    except ConfigError:
        return False
    return True
