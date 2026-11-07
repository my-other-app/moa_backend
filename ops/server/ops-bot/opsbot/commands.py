"""The bot's commands: parsing, limits and the answers.

/status            every container, plus the host on one line
/disk              host disks, Docker's images and volumes, the backups
/logs <svc> [n]    the last n lines of a service (default 30, max 50), redacted
/backup            start a backup now (the file arrives when it is done)
/help
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import fmt, reports
from .config import Config
from .redact import redact
from .system import SERVICE_RE, CommandError, Host

LOG_LINES_DEFAULT = 30
LOG_LINES_MAX = 50
BACKUP_COOLDOWN = 300.0
MAX_COMMAND_CHARS = 200

HELP = """<b>MyOtherApp ops bot</b>
/status – containers: state, uptime, restarts, CPU, memory; host load, memory, disk
/disk – disks, Docker images and volumes, backups folder
/logs &lt;service&gt; [n] – last n lines (default 30, max 50), redacted
/backup – take a backup now; the encrypted file arrives here
/help – this

Alerts come here by themselves: containers, disk, memory, SSH logins, error bursts. Backups arrive every night at 03:00 IST."""


@dataclass(frozen=True)
class Parsed:
    name: str
    args: tuple[str, ...]


def parse(text: str | None) -> Parsed | None:
    """'/logs@MoaOpsBot backend 20' -> Parsed('logs', ('backend', '20')).
    None for anything that is not a command."""
    if not text:
        return None
    text = text.strip()[:MAX_COMMAND_CHARS]
    if not text.startswith("/"):
        return None
    words = text.split()
    name = words[0][1:].split("@", 1)[0].lower()
    if not name or not name.replace("_", "").isalnum():
        return None
    return Parsed(name, tuple(words[1:]))


def parse_log_args(args: tuple[str, ...]) -> tuple[str, int, str | None]:
    """(service, lines, a note) or ValueError with what to send back."""
    if not args:
        raise ValueError("Which service? /logs &lt;service&gt; [lines], e.g. /logs backend 30")
    service = args[0].lower()
    if not SERVICE_RE.match(service):
        raise ValueError("That is not a service name.")
    lines, note = LOG_LINES_DEFAULT, None
    if len(args) > 1:
        try:
            lines = int(args[1])
        except ValueError:
            raise ValueError("The number of lines must be a whole number, e.g. /logs backend 30") from None
        if lines < 1:
            lines = 1
        if lines > LOG_LINES_MAX:
            lines, note = LOG_LINES_MAX, f"(at most {LOG_LINES_MAX} lines)"
    if len(args) > 2:
        raise ValueError("/logs takes a service and a number of lines, nothing else.")
    return service, lines, note


class Commands:
    def __init__(self, cfg: Config, host: Host, *, clock=time.monotonic):
        self.cfg = cfg
        self.host = host
        self.clock = clock
        self.last_backup: float | None = None

    def handle(self, text: str | None) -> str | None:
        parsed = parse(text)
        if parsed is None:
            return "Send /help for what I can do." if text else None
        handler = getattr(self, f"cmd_{parsed.name}", None)
        if handler is None:
            return f"Unknown command /{fmt.esc(parsed.name)}. Send /help."
        try:
            return handler(parsed.args)
        except CommandError as error:
            return f"Could not do that: {fmt.esc(redact(str(error), 300))}"

    def cmd_start(self, args):
        return HELP

    def cmd_help(self, args):
        return HELP

    def cmd_status(self, args):
        return reports.status(self.host, label=self.cfg.host_label, disk_paths=self.cfg.disk_paths)

    def cmd_disk(self, args):
        return reports.disk(self.host, label=self.cfg.host_label, disk_paths=self.cfg.disk_paths, backup_dir=self.cfg.backup_dir)

    def cmd_logs(self, args):
        try:
            service, lines, note = parse_log_args(args)
        except ValueError as error:
            return str(error)
        containers = self.host.containers()
        match = [c for c in containers if c.service == service] or [c for c in containers if c.name == service]
        if not match:
            names = ", ".join(sorted({c.label for c in containers})) or "none"
            return f"No service {fmt.esc(service)}. Services: {fmt.esc(names)}"
        text = reports.logs(self.host, match[0], lines)
        return f"{text}\n{note}" if note else text

    def cmd_backup(self, args):
        unit = self.cfg.backup_unit
        state = self.host.unit_state(unit)
        if state in ("activating", "active", "reloading", "deactivating"):
            return "A backup is already running; its file (or an alert) will arrive here."
        now = self.clock()
        if self.last_backup is not None and now - self.last_backup < BACKUP_COOLDOWN:
            return f"A backup was started {fmt.duration(now - self.last_backup)} ago; try again in a few minutes."
        self.host.start_unit(unit)
        self.last_backup = now
        return "Backup started. The encrypted file (or an alert, if it fails) arrives here when it is done."
