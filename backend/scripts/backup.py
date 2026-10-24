"""The daily backup: a dump of the database, copied off-site.

    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/backup.py
        --keep-local N          local dumps to keep (default APP_BACKUP_KEEP_LOCAL)
        --no-alert              on failure, raise no admin alert: the caller will
        --report-failure TITLE  only raise the `backup_failed` alert, its body
                                read from stdin, and exit

Run every day at 02:30 IST by the systemd timer in ops/backup
(docs/OPERATIONS.md, section 19), as `--no-alert`: when a run fails for any
reason -- this script's, or a container that is not running -- the unit's
OnFailure handler (ops/backup/alert.sh) raises the one alert, through
`--report-failure`. It

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
dump was taken and, if off-site is on, uploaded; 1 otherwise -- and then,
unless --no-alert, an admin alert (backup_failed) is raised too.
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


async def alert(title: str, body: str) -> bool:
    """An admin alert (mail, Discord, the dashboard's bell), best effort: the
    log has it either way. When the database cannot take the alert's row --
    it may be what failed -- the mail and the post go without it. True when
    anyone was told."""
    from app.api.admin_alerts import service as alerts

    body = offsite.scrub(body)
    try:
        from app.core import after_commit
        from app.db.core import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            row = await alerts.notify(session, "backup_failed", title=title, body=body)
            await session.commit()
        await after_commit.drain(timeout=60)
        if row is not None:
            return True
    except Exception as error:  # noqa: BLE001 -- the database may be what failed
        _say(f"could not record the admin alert: {type(error).__name__}; mailing and posting it without the database")
    alert_row = {"kind": "backup_failed", "title": title, "body": body}
    try:
        mailed = await asyncio.to_thread(alerts.send_email, alert_row)
        posted = await asyncio.to_thread(alerts.post_discord, alert_row)
    except Exception as error:  # noqa: BLE001
        _say(f"could not raise the admin alert: {type(error).__name__}")
        return False
    if not (mailed or posted):
        _say("could not raise the admin alert: no mail went (APP_ADMIN_ALERT_EMAILS) and no Discord post (APP_ADMIN_ALERT_DISCORD_WEBHOOK)")
    return bool(mailed or posted)


async def report_failure(title: str, body: str) -> int:
    """--report-failure: the systemd unit's one alert (ops/backup/alert.sh).
    0 when it reached someone, 1 when not (alert.sh then posts to Discord itself)."""
    told = await alert(offsite.scrub(title)[:200], body[-3000:])
    _say("alert raised" if told else "alert NOT raised")
    return 0 if told else 1


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


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dump the database and copy the dump off-site.")
    parser.add_argument("--keep-local", type=int, default=None, help="local dumps to keep (default APP_BACKUP_KEEP_LOCAL)")
    parser.add_argument("--no-alert", action="store_true", help="raise no admin alert on failure (the systemd unit's OnFailure does)")
    parser.add_argument("--report-failure", metavar="TITLE", help="only raise the backup_failed alert; its body is read from stdin")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.report_failure:
        return asyncio.run(report_failure(args.report_failure, sys.stdin.read()))
    return asyncio.run(run(keep=args.keep_local, notify=not args.no_alert))


if __name__ == "__main__":
    sys.exit(main())
