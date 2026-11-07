"""deliver-backup: the newest dump, encrypted with age, sent to Telegram.

Runs as myotherapp-backup.service's ExecStartPost=, after ops/backup/run.sh
has taken the dump (so only after a dump that succeeded). The order is
fixed and checked:

  1. the newest backup-*.dump in the backups folder, from this run (fresh),
     and really a pg_dump archive (PGDMP);
  2. `age -r <recipient>` into the bot's own work folder (root only);
  3. the result must be an age file -- Telegram.send_document refuses
     anything else, so a plaintext dump can never be sent;
  4. over Telegram's 50 MB bot limit: a message saying so instead (exit 0:
     the backup itself is fine and is on the box and in S3);
  5. the encrypted copy is deleted; the dump stays where it was, pruned by
     scripts/backup.py as before.

Any failure prints one clear line and exits 1: the unit then fails, and its
OnFailure= (ops/backup/alert.sh) sends the alert -- to Telegram first.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import fmt
from .config import Config
from .redact import redact
from .system import CommandError, Runner
from .telegram import UPLOAD_LIMIT, NotEncrypted, Telegram, TelegramError, is_age_file

log = logging.getLogger("opsbot.backup")

DUMP_RE = re.compile(r"^backup-(\d{8})-(\d{6})(?:-[a-z0-9]{1,16})?\.dump$")
TOC_TABLE_RE = re.compile(r"^\d+; \d+ \d+ TABLE (?!DATA )\S+ \S+", re.M)
#: The dump must be from this run: the ExecStart that just ended.
MAX_DUMP_AGE = 3 * 3600
PGDMP = b"PGDMP"


class DeliveryError(Exception):
    pass


def newest_dump(directory: Path, *, now: float | None = None, max_age: float = MAX_DUMP_AGE) -> Path:
    now = now or time.time()
    dumps = []
    try:
        for path in directory.iterdir():
            if DUMP_RE.match(path.name) and path.is_file() and not path.is_symlink():
                dumps.append((path.stat().st_mtime, path))
    except OSError as error:
        raise DeliveryError(f"cannot read {directory}: {type(error).__name__}") from None
    if not dumps:
        raise DeliveryError(f"no backup-*.dump in {directory}")
    mtime, path = max(dumps)
    if now - mtime > max_age:
        raise DeliveryError(f"the newest dump, {path.name}, is {fmt.duration(now - mtime)} old: this run made none")
    with open(path, "rb") as handle:
        if handle.read(len(PGDMP)) != PGDMP:
            raise DeliveryError(f"{path.name} is not a pg_dump archive")
    return path


def dump_seconds(path: Path) -> float | None:
    """How long the dump took: its name is when it began (UTC), its mtime
    when it was finished."""
    match = DUMP_RE.match(path.name)
    if not match:
        return None
    began = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    seconds = path.stat().st_mtime - began.timestamp()
    return seconds if 0 <= seconds < 86400 else None


def count_tables(cfg: Config, runner: Runner, name: str) -> int | None:
    """Tables in the dump's table of contents (pg_restore -l, in the backend
    container, which has the client); None when that cannot be run."""
    try:
        toc = runner.run([cfg.dc, "exec", "-T", "backend", "pg_restore", "-l", f"{cfg.container_backup_dir}/{name}"], timeout=120)
    except CommandError as error:
        log.warning("could not count the tables: %s", redact(str(error), 200))
        return None
    return len(TOC_TABLE_RE.findall(toc))


def encrypt(runner: Runner, recipient: str, source: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    try:
        runner.run(["age", "--encrypt", "-r", recipient, "-o", str(target), str(source)], timeout=1800)
    except CommandError as error:
        target.unlink(missing_ok=True)
        raise DeliveryError(f"encryption failed: {redact(str(error), 200)}") from None
    if not is_age_file(target):
        target.unlink(missing_ok=True)
        raise DeliveryError("encryption produced no age file")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def caption(cfg: Config, *, dump: Path, encrypted: Path, size: int, seconds: float | None, tables: int | None, digest: str) -> str:
    return "\n".join(
        [
            f"<b>Database backup</b> · {fmt.esc(cfg.host_label)}",
            f"{fmt.stamp()} · <code>{fmt.esc(encrypted.name)}</code>",
            f"{fmt.mb(size)} encrypted (dump {fmt.mb(dump.stat().st_size)}) · took {fmt.duration(seconds)} · "
            + (f"{tables} tables" if tables is not None else "tables: not counted"),
            f"sha256 <code>{digest[:16]}</code>…",
            "Decrypt: <code>age -d -i backup-age.key -o backup.dump FILE</code>",
        ]
    )


def deliver(cfg: Config, telegram: Telegram, runner: Runner, *, now: float | None = None, limit: int = UPLOAD_LIMIT) -> int:
    started = time.monotonic()
    try:
        dump = newest_dump(cfg.backup_dir, now=now)
        outbox = cfg.work_dir / "outbox"
        outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        outbox.chmod(0o700)
        for stale in outbox.glob("*.age"):
            stale.unlink(missing_ok=True)
        encrypted = outbox / f"{dump.name}.age"
        try:
            encrypt(runner, cfg.age_recipient, dump, encrypted)
            size = encrypted.stat().st_size
            seconds = dump_seconds(dump)
            if size > limit:
                telegram.send_message(
                    f"<b>Database backup OK, but not sent</b> · {fmt.esc(cfg.host_label)}\n"
                    f"<code>{fmt.esc(dump.name)}</code> is {fmt.mb(size)} encrypted, over Telegram's 50 MB limit for bots. "
                    f"It is on the server in {fmt.esc(cfg.backup_dir)} (and off-site, if S3 is set up)."
                )
                print(f"deliver-backup: {dump.name} is {size} bytes encrypted, over the limit: told the owner, not sent")
                return 0
            tables = count_tables(cfg, runner, dump.name)
            text = caption(cfg, dump=dump, encrypted=encrypted, size=size, seconds=seconds, tables=tables, digest=sha256(encrypted))
            telegram.send_document(encrypted, text, filename=encrypted.name)
        finally:
            encrypted.unlink(missing_ok=True)
    except (DeliveryError, NotEncrypted) as error:
        print(f"deliver-backup FAILED: {redact(str(error), 300)}")
        return 1
    except TelegramError as error:
        print(f"deliver-backup FAILED: Telegram did not take the file: {redact(str(error), 300)}")
        return 1
    except OSError as error:
        print(f"deliver-backup FAILED: {type(error).__name__}: {redact(str(error), 200)}")
        return 1
    print(f"deliver-backup: {dump.name} sent encrypted ({size} bytes) in {time.monotonic() - started:.0f}s")
    return 0
