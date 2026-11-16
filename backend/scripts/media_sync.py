"""The media bucket, copied off the box.

    docker compose exec -T backend python scripts/media_sync.py [--dry-run] [--limit N]

Every object in the media bucket (settings.S3_BUCKET: Garage on the
production box) that the off-site backup bucket (APP_BACKUP_S3_*) does not
have yet under APP_MEDIA_SYNC_PREFIX (default media/) is copied there. Run
every night by ops/backup's myotherapp-media-sync timer once it is turned on
(ops/server/README.md, "Off-box copies"), or by hand.

Off unless APP_MEDIA_SYNC=true and APP_BACKUP_S3_BUCKET is set: it then says
so and exits 0, so the timer can be on before the settings are.

- Copy only: nothing is ever deleted from the copy, so a file removed on the
  box (by mistake, or a wipe) is still there. The backup bucket's own rules
  (versioning, lifecycle) decide how long copies are kept.
- An object is copied when its key is missing there or its size differs.
  Uploads are stored under content-versioned keys, so the same key and size
  is the same file.
- Each object is streamed, never held whole in memory, and stored with the
  backup bucket's server-side encryption (APP_BACKUP_S3_SSE), its content
  type and the source's ETag (metadata source-etag).
- It prints counts, never a key or a credential. Exit status 1 when the
  listing or any copy failed (the unit's OnFailure= then alerts), else 0.
"""

import argparse
import sys

sys.path.insert(0, ".")

from app.api.backups import offsite  # noqa: E402
from app.config import settings  # noqa: E402

#: At most this many failures are described one by one; the rest are counted.
SHOWN_FAILURES = 5


def _say(line: str) -> None:
    print(offsite.scrub(line), flush=True)


def prefix() -> str:
    raw = (settings.MEDIA_SYNC_PREFIX or "media/").strip().lstrip("/")
    return raw if raw.endswith("/") else raw + "/"


def why_off() -> str:
    """Why the sync would not run, or "" when it would."""
    if not settings.MEDIA_SYNC:
        return "APP_MEDIA_SYNC is not true"
    if not offsite.enabled():
        return "APP_BACKUP_S3_BUCKET is not set"
    if not (settings.S3_BUCKET or "").strip():
        return "APP_S3_BUCKET is not set"
    backups = offsite._prefix()
    if prefix().startswith(backups) or backups.startswith(prefix()):
        return f"APP_MEDIA_SYNC_PREFIX ({prefix()}) overlaps the backups' prefix ({backups}): give it a folder of its own"
    return ""


def listing(s3, bucket: str, under: str = "") -> dict[str, int]:
    """key (without `under`) -> size, for every object under `under`."""
    found: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=under):
        for item in page.get("Contents", []) or []:
            key = item["Key"]
            if key.endswith("/"):
                continue
            found[key[len(under) :]] = int(item["Size"])
    return found


def to_copy(source: dict[str, int], target: dict[str, int]) -> list[str]:
    return sorted(key for key, size in source.items() if target.get(key) != size)


def copy_one(src, dst, key: str) -> None:
    obj = src.get_object(Bucket=settings.S3_BUCKET.strip(), Key=key)
    body = obj["Body"]
    try:
        extra = {
            **offsite.encryption(),
            "ContentType": obj.get("ContentType") or "application/octet-stream",
            "Metadata": {"source-etag": str(obj.get("ETag") or "").strip('"')},
        }
        dst.upload_fileobj(
            body, settings.BACKUP_S3_BUCKET.strip(), prefix() + key, ExtraArgs=extra
        )
    finally:
        body.close()


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def _error(error: BaseException) -> str:
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return f"{type(error).__name__}" + (f" ({code})" if code else "")


def run(*, dry_run: bool = False, limit: int | None = None, src=None, dst=None) -> int:
    off = why_off()
    if off:
        _say(f"media sync: off ({off})")
        return 0
    from app.core.storage.clients import media_client

    src = src or media_client()
    dst = dst or offsite.client()
    where = f"s3://{settings.BACKUP_S3_BUCKET.strip()}/{prefix()}"
    try:
        source = listing(src, settings.S3_BUCKET.strip())
        target = listing(dst, settings.BACKUP_S3_BUCKET.strip(), prefix())
    except Exception as error:  # noqa: BLE001 -- reported, then exit 1
        _say(f"media sync FAILED: could not list the buckets: {_error(error)}")
        return 1
    todo = to_copy(source, target)
    waiting = len(todo)
    if limit is not None:
        todo = todo[: max(limit, 0)]
    size = sum(source[key] for key in todo)
    if dry_run:
        _say(
            f"media sync (dry run): {len(source)} objects in the media bucket, {len(source) - waiting} already at {where}; "
            f"would copy {len(todo)} ({_mb(size)})"
        )
        return 0
    copied = failed = 0
    copied_bytes = 0
    for key in todo:
        try:
            copy_one(src, dst, key)
        except Exception as error:  # noqa: BLE001 -- counted; the rest still go
            failed += 1
            if failed <= SHOWN_FAILURES:
                _say(f"media sync: an object was not copied: {_error(error)}")
            continue
        copied += 1
        copied_bytes += source[key]
    left = waiting - copied
    _say(
        f"media sync: {len(source)} objects in the media bucket; {copied} copied ({_mb(copied_bytes)}) to {where}, "
        f"{failed} failed, {left} still to copy"
    )
    if failed:
        _say("media sync FAILED")
        return 1
    _say("MEDIA SYNC OK")
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy the media bucket to the off-site backup bucket."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be copied; copy nothing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="copy at most this many objects this run",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return run(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
