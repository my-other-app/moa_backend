"""The daily backup: a dump of the database, copied off-site.

    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/backup.py

Run by the host's cron once a day (docs/OPERATIONS.md, "Backups"). It

1. dumps the database into BACKUP_DIR (/backups in the container,
   /var/backups/myotherapp on the host) the way the admin endpoint does
   (app/api/backups/service.py: pg_dump's custom format, written under a
   temporary name and moved into place);
2. uploads it to S3 when APP_BACKUP_S3_BUCKET is set: backups/daily/, or
   backups/weekly/ on a Sunday or when the last weekly one is over a week
   old (app/api/backups/offsite.py); encrypted, its sha256 in the metadata;
3. when APP_BACKUP_KEEP_LOCAL is above 0 and the upload worked (or off-site
   is off), deletes the oldest local dumps beyond that many.

It prints one line per step and never a credential. Exit status 0 when the
dump was taken and, if off-site is on, uploaded; 1 otherwise -- and then an
admin alert (backup_failed) is raised too, when the database can take it.
"""

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from app.api.backups import offsite, service  # noqa: E402
from app.config import settings  # noqa: E402


def _say(line: str) -> None:
    print(offsite.scrub(line), flush=True)


def prune(keep: int) -> list[str]:
    """Delete all but the newest `keep` dumps in BACKUP_DIR; the names gone."""
    if keep <= 0:
        return []
    gone = []
    for backup in service.list_backups()[keep:]:
        try:
            (service.backup_dir() / backup.name).unlink()
            gone.append(backup.name)
        except OSError as error:
            _say(f"could not delete {backup.name}: {error}")
    return gone


async def alert(title: str, body: str) -> None:
    """An admin alert (mail, Discord, the dashboard's bell), best effort: the
    cron log has it either way."""
    try:
        from app.api.admin_alerts import service as alerts
        from app.core import after_commit
        from app.db.core import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await alerts.notify(session, "backup_failed", title=title, body=offsite.scrub(body))
            await session.commit()
        await after_commit.drain(timeout=60)
    except Exception as error:  # noqa: BLE001 -- the database may be what failed
        _say(f"could not raise the admin alert: {type(error).__name__}")


async def run(*, s3=None, keep: int | None = None, notify: bool = True) -> int:
    keep = settings.BACKUP_KEEP_LOCAL if keep is None else keep
    try:
        backup = await service.create_backup()
    except Exception as error:  # noqa: BLE001 -- reported, then exit 1
        reason = getattr(error, "message", None) or f"{type(error).__name__}: {error}"
        _say(f"backup FAILED: the dump: {reason}")
        if notify:
            await alert("The daily database backup failed", f"The dump could not be taken: {reason}")
        return 1
    _say(f"dump: {backup.name} ({backup.size_bytes} bytes) in {service.backup_dir()}")

    copy = await service.copy_offsite(backup, scheduled=True, s3=s3)
    if copy is None:
        _say("off-site copy: off (APP_BACKUP_S3_BUCKET is not set) -- this dump is on the database's own disk only")
    elif copy.error:
        _say(f"backup FAILED: the off-site copy: {copy.error}")
        if notify:
            await alert(
                "The daily database backup was not copied off-site",
                f"{backup.name} was dumped but the upload failed: {copy.error}. Local dumps were not pruned.",
            )
        return 1
    else:
        _say(f"off-site copy: s3://{settings.BACKUP_S3_BUCKET}/{copy.key} ({copy.tier}) sha256 {copy.sha256}")

    gone = prune(keep)
    if gone:
        _say(f"pruned {len(gone)} older local dump(s), kept the newest {keep}")
    _say("BACKUP OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump the database and copy the dump off-site.")
    parser.add_argument("--keep-local", type=int, default=None, help="local dumps to keep (default APP_BACKUP_KEEP_LOCAL)")
    args = parser.parse_args()
    return asyncio.run(run(keep=args.keep_local))


if __name__ == "__main__":
    sys.exit(main())
