"""Database snapshots, taken on demand.

This is a convenience, not a backup regime. A snapshot someone remembers to
click is not a schedule, and one written to the same host as the database
survives neither a disk failure nor the instance going away. It exists because
the alternative today is nothing at all, and because an admin who can press a
button before a risky change will press it.

The dump is a full copy of the database -- every user's details, every payment
record, every password hash. Creating and downloading one are therefore the
most privileged actions the API offers, gated behind their own capability and
written to the audit log every time.
"""

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from app.api.backups import offsite
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

# Custom format: compressed, and restorable selectively with pg_restore.
SUFFIX = ".dump"
# Anchored, and no path separators or dots beyond the suffix -- this value
# reaches the filesystem, so "../../etc/passwd" must not survive it.
SAFE_NAME = re.compile(r"^backup-\d{8}-\d{6}(-[a-z0-9]{1,16})?\.dump$")


@dataclass
class Backup:
    name: str
    size_bytes: int
    created_at: datetime


def backup_dir() -> Path:
    return Path(settings.BACKUP_DIR)


def _resolve(name: str) -> Path:
    """
    Turn a client-supplied name into a path inside the backup directory.

    Two checks, not one: the pattern rejects anything that is not a name this
    service generated, and the resolved path is then confirmed to be inside the
    directory. The second catches symlinks, which the first cannot see.
    """
    if not SAFE_NAME.match(name):
        raise CustomHTTPException(400, message="Not a valid backup name")

    directory = backup_dir().resolve()
    path = (directory / name).resolve()
    if not path.is_relative_to(directory):
        raise CustomHTTPException(400, message="Not a valid backup name")
    if not path.is_file():
        raise CustomHTTPException(404, message="That backup no longer exists")
    return path


def list_backups() -> list[Backup]:
    directory = backup_dir()
    if not directory.is_dir():
        return []
    rows = []
    for path in directory.glob(f"*{SUFFIX}"):
        if not path.is_file() or not SAFE_NAME.match(path.name):
            continue
        stat = path.stat()
        rows.append(
            Backup(
                name=path.name,
                size_bytes=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    # Newest first: the one you just took is the one you want.
    return sorted(rows, key=lambda b: b.created_at, reverse=True)


def _connection_params() -> dict[str, str]:
    """Pull host/port/user/password/dbname out of the configured URL."""
    url = urlparse(settings.DATABASE_URL_SYNC.replace("postgresql+psycopg2", "postgresql"))
    return {
        "host": url.hostname or "localhost",
        "port": str(url.port or 5432),
        "user": unquote(url.username or ""),
        "password": unquote(url.password or ""),
        "dbname": (url.path or "/").lstrip("/"),
    }


async def create_backup() -> Backup:
    if shutil.which("pg_dump") is None:
        raise CustomHTTPException(
            503,
            message=(
                "pg_dump is not installed in this container. Rebuild the backend "
                "image -- the Dockerfile installs postgresql-client, but a "
                "running container keeps the image it started from."
            ),
            error_code="PG_DUMP_MISSING",
        )

    directory = backup_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CustomHTTPException(
            503, message=f"Backup directory is not writable: {error}"
        ) from error

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"backup-{stamp}{SUFFIX}"
    # Written under a temporary name and moved into place, so a crashed or
    # half-written dump never appears in the listing as if it were usable.
    final = directory / name
    partial = directory / f".{name}.partial"

    params = _connection_params()
    env = {**os.environ, "PGPASSWORD": params["password"]}

    process = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        f"--host={params['host']}",
        f"--port={params['port']}",
        f"--username={params['user']}",
        f"--file={partial}",
        params["dbname"],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except asyncio.TimeoutError:
        process.kill()
        partial.unlink(missing_ok=True)
        raise CustomHTTPException(504, message="The dump timed out after 10 minutes")

    if process.returncode != 0:
        partial.unlink(missing_ok=True)
        # Scrubbed: a connection error quotes the server it was talking to.
        detail = offsite.scrub((stderr or b"").decode(errors="replace").strip())
        # The version mismatch is the likely one and is unhelpful unless quoted:
        # pg_dump refuses to dump a server newer than itself.
        raise CustomHTTPException(
            500,
            message=f"pg_dump failed: {detail[:400] or 'no output'}",
            error_code="PG_DUMP_FAILED",
        )

    partial.replace(final)
    stat = final.stat()
    return Backup(
        name=final.name,
        size_bytes=stat.st_size,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def path_for_download(name: str) -> Path:
    return _resolve(name)


def delete_backup(name: str) -> None:
    _resolve(name).unlink()


#: Seconds an off-site upload may take before the caller stops waiting.
UPLOAD_TIMEOUT = 900


@dataclass
class OffsiteResult:
    tier: str
    key: Optional[str] = None
    sha256: Optional[str] = None
    error: Optional[str] = None


async def copy_offsite(backup: Backup, *, scheduled: bool = False, s3=None) -> Optional[OffsiteResult]:
    """Upload `backup` to the off-site bucket (app/api/backups/offsite.py).
    None when off-site copies are off. A failure is returned, not raised: the
    local dump is still good, and the caller says what happened."""
    if not offsite.enabled():
        return None
    directory = backup_dir()
    tier = offsite.tier_for(backup.created_at, directory, scheduled=scheduled)
    try:
        copy = await asyncio.wait_for(
            asyncio.to_thread(offsite.upload, directory / backup.name, tier=tier, s3=s3),
            timeout=UPLOAD_TIMEOUT,
        )
    except Exception as error:  # noqa: BLE001 -- reported to the caller
        message = offsite.scrub(f"{type(error).__name__}: {error}")[:300]
        logger.error("Off-site copy of %s failed: %s", backup.name, message)
        return OffsiteResult(tier=tier, error=message)
    return OffsiteResult(tier=tier, key=copy.key, sha256=copy.sha256)
