"""The off-site copy of each database dump: S3, encrypted, on a schedule of
its own.

A dump in /var/backups/myotherapp sits on the same disk as the database it
copies. It survives a bad migration or a deleted table, not the disk, the
instance or the account going. So every dump is also uploaded, when
BACKUP_S3_BUCKET is set, to

    s3://{BACKUP_S3_BUCKET}/{BACKUP_S3_PREFIX}daily/backup-YYYYMMDD-HHMMSS.dump
    s3://{BACKUP_S3_BUCKET}/{BACKUP_S3_PREFIX}weekly/backup-YYYYMMDD-HHMMSS.dump

with server-side encryption and the dump's sha256 in its metadata (the
restore drill checks the download against it).

Retention is the bucket's job, not this code's: an S3 lifecycle rule expires
daily/ after 14 days and weekly/ after 56 (docs/OPERATIONS.md has the rule).
That way the credentials here never need to delete anything, and a leaked
key cannot empty the backups. A dump is weekly when the scheduled run makes
it on a Sunday (UTC), or when the last weekly one is more than seven days
old -- so one missed Sunday does not leave a week without one. The date of
the last weekly upload is kept in BACKUP_DIR/.last-weekly, since a key that
cannot list the bucket cannot ask it.

Nothing here prints or logs a credential: errors are reported by type and
message, and boto3's messages do not carry the keys.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

DAILY = "daily"
WEEKLY = "weekly"
#: Monday is 0: a Sunday dump is the week's.
WEEKLY_DAY = 6
MARKER = ".last-weekly"


@dataclass
class OffsiteCopy:
    bucket: str
    key: str
    tier: str
    size_bytes: int
    sha256: str


@dataclass
class RemoteDump:
    key: str
    size_bytes: int
    modified: datetime
    sha256: Optional[str]

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def enabled() -> bool:
    return bool((settings.BACKUP_S3_BUCKET or "").strip())


def _prefix() -> str:
    prefix = (settings.BACKUP_S3_PREFIX or "backups/").strip().lstrip("/")
    return prefix if prefix.endswith("/") else prefix + "/"


def key_for(name: str, tier: str) -> str:
    return f"{_prefix()}{tier}/{name}"


def client():
    """An S3 client with the backup credentials when they are set, else the
    media bucket's. Made per call: a dump is a rare event, and a long-lived
    client would hold the keys for no reason."""
    import boto3

    access = settings.BACKUP_S3_ACCESS_KEY or settings.S3_ACCESS_KEY
    secret = settings.BACKUP_S3_SECRET_KEY or settings.S3_SECRET_KEY
    return boto3.client(
        "s3",
        region_name=settings.BACKUP_S3_REGION or None,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
    )


def encryption() -> dict:
    """The ExtraArgs that encrypt an object at rest."""
    mode = (settings.BACKUP_S3_SSE or "AES256").strip()
    if mode == "aws:kms":
        extra = {"ServerSideEncryption": "aws:kms"}
        if settings.BACKUP_S3_KMS_KEY_ID:
            extra["SSEKMSKeyId"] = settings.BACKUP_S3_KMS_KEY_ID
        return extra
    if mode != "AES256":
        raise ValueError(f"APP_BACKUP_S3_SSE must be AES256 or aws:kms, not {mode!r}")
    return {"ServerSideEncryption": "AES256"}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_weekly(directory: Path) -> Optional[date]:
    try:
        return date.fromisoformat((directory / MARKER).read_text().strip())
    except (OSError, ValueError):
        return None


def tier_for(when: datetime, directory: Path, *, scheduled: bool) -> str:
    """weekly for a scheduled dump on a Sunday, or when the last weekly one is
    over a week old (or there is none); daily otherwise. A dump an admin
    takes by hand is always daily: it is a checkpoint, not the schedule."""
    if not scheduled:
        return DAILY
    if when.weekday() == WEEKLY_DAY:
        return WEEKLY
    last = _last_weekly(directory)
    return WEEKLY if last is None or when.date() - last > timedelta(days=7) else DAILY


def upload(path: Path, *, tier: str = DAILY, s3=None) -> OffsiteCopy:
    """Upload one dump (a managed multipart upload for a big one) and return
    where it went. Raises on any failure: the caller decides what that means."""
    if not enabled():
        raise RuntimeError("Off-site backups are off: APP_BACKUP_S3_BUCKET is not set")
    path = Path(path)
    s3 = s3 or client()
    bucket = settings.BACKUP_S3_BUCKET.strip()
    key = key_for(path.name, tier)
    digest = sha256_of(path)
    s3.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={
            **encryption(),
            "ContentType": "application/octet-stream",
            "Metadata": {"sha256": digest, "tier": tier},
        },
    )
    if tier == WEEKLY:
        try:
            (path.parent / MARKER).write_text(datetime.now(timezone.utc).date().isoformat())
        except OSError:
            logger.warning("Could not note the weekly backup in %s", path.parent / MARKER)
    return OffsiteCopy(bucket=bucket, key=key, tier=tier, size_bytes=path.stat().st_size, sha256=digest)


def remote_dumps(s3=None) -> list[RemoteDump]:
    """Every dump under the prefix, newest first (needs s3:ListBucket on it)."""
    s3 = s3 or client()
    bucket = settings.BACKUP_S3_BUCKET.strip()
    out = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=_prefix()):
        for item in page.get("Contents", []) or []:
            if item["Key"].endswith(".dump"):
                out.append(RemoteDump(item["Key"], int(item["Size"]), item["LastModified"], None))
    return sorted(out, key=lambda d: d.modified, reverse=True)


_URL_CREDENTIALS = re.compile(r"(\w+://)([^/@\s:]*):([^/@\s]+)@")


def scrub(text: str) -> str:
    """`text` with every credential this process knows replaced by ***: the
    database password, the S3 and backup keys, and the user:password of any
    URL. For anything printed or returned about a backup -- a pg_dump error
    quoting its connection, an exception from boto3 -- as a second line of
    defence after not putting them there."""
    from urllib.parse import unquote, urlsplit

    out = _URL_CREDENTIALS.sub(r"\1\2:***@", str(text))
    secrets = [
        settings.S3_ACCESS_KEY, settings.S3_SECRET_KEY,
        settings.BACKUP_S3_ACCESS_KEY, settings.BACKUP_S3_SECRET_KEY,
    ]
    for url in (settings.DATABASE_URL, settings.DATABASE_URL_SYNC):
        try:
            password = urlsplit(url).password
        except ValueError:
            password = None
        if password:
            secrets += [password, unquote(password)]
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(secret, "***")
    return out


def download(dump: RemoteDump, destination: Path, s3=None) -> RemoteDump:
    """Fetch `dump` to `destination`, with its recorded sha256 filled in."""
    s3 = s3 or client()
    bucket = settings.BACKUP_S3_BUCKET.strip()
    head = s3.head_object(Bucket=bucket, Key=dump.key)
    s3.download_file(bucket, dump.key, str(destination))
    dump.sha256 = (head.get("Metadata") or {}).get("sha256")
    return dump
