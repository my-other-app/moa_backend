"""Kept documents nobody needs any more, deleted once a day (APP_DOC_CACHE_DAYS).

Every rendered ticket and certificate is kept under docs/ (doc_cache.py),
and nothing deleted them: a redesign, a renamed guest or a new renderer
version leaves the old file behind for ever, and docs/ only grew.

With APP_DOC_CACHE_DAYS set (0, the default, is off), one process a day:

- pages through `{base}docs/` with ListObjectsV2, 1,000 keys a page;
- deletes, with DeleteObjects, 1,000 keys a call and a pause between calls,
  each kept document last written more than DOC_CACHE_DAYS ago;
- touches only keys of the exact shape the cache writes,
  `docs/{kind}/{registration uuid}/{design version}-{24 hex}.{ext}` for the
  kinds and extensions in doc_cache.KINDS / EXTENSIONS. Anything else under
  docs/ is left alone and counted; nothing outside docs/ is ever listed;
- deletes at most APP_DOC_CACHE_MAX_DELETE_PER_RUN (50,000) a run: the rest
  wait for the next day;
- logs what it did, and raises an admin alert (`doc_cache_unknown`) when more
  than APP_DOC_CACHE_SKIP_ALERT_PERCENT of what it found was not a kept
  document: something else is writing there, and the sweep will not guess.

"Last written", because S3 keeps no read time: a document downloaded every
day is deleted at DOC_CACHE_DAYS like any other, and drawn again (once, see
doc_cache's single-flight) the next time it is asked for. Nothing is lost:
the cache is rebuilt from the database and the design.

A bucket lifecycle rule on the same prefix does the same without this code
(docs/OPERATIONS.md, section 13); do not run both with different day counts.

One process runs it (a session advisory lock, DOC_CACHE_SWEEP), once a day
(its row in scheduled_runs).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.doc_cache import EXTENSIONS, KINDS
from app.config import settings
from app.core.storage import objects
from app.core.storage.bucket import prefix
from app.db import lock_keys
from app.db.locks import singleton

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.DOC_CACHE_SWEEP
JOB = "doc-cache-sweep"
EVERY = timedelta(days=1)
#: After a process starts, and then how often it looks whether a day has passed.
FIRST_LOOK_SECONDS = 600
LOOK_EVERY_SECONDS = 3600
#: Keys a ListObjectsV2 page, keys a DeleteObjects call (S3's and R2's most), and the pause between calls.
PAGE = 1000
BATCH = 1000
PAUSE_SECONDS = 1.0
#: Below this many objects found, no alert: two odd files in ten are not a trend.
ALERT_MIN_OBJECTS = 50
#: Unknown keys named in the log and the alert.
SAMPLES = 10

#: A kept document's key, after the docs/ prefix: exactly what doc_cache.key_for writes.
KEPT = re.compile(
    r"^(?:" + "|".join(map(re.escape, KINDS)) + r")"
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"/\d{1,9}-[0-9a-f]{24}"
    r"\.(?:" + "|".join(map(re.escape, EXTENSIONS)) + r")$"
)


def root() -> str:
    """The prefix every kept document is under: `{APP_S3_BASE_PATH}/docs/`."""
    return prefix("docs")


def kind_of(key: str, base: Optional[str] = None) -> Optional[str]:
    """The document kind when `key` is a kept document's key, else None."""
    base = base if base is not None else root()
    if not key.startswith(base):
        return None
    rest = key[len(base):]
    return rest.split("/", 1)[0] if KEPT.match(rest) else None


def _aware(value) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def pages(client, base: str):
    """Every object under `base`, a ListObjectsV2 page at a time (blocking)."""
    token = None
    while True:
        args = {"Bucket": settings.S3_BUCKET, "Prefix": base, "MaxKeys": PAGE}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        yield page.get("Contents") or []
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return


async def _delete(client, keys: list[str]) -> tuple[int, list]:
    """One DeleteObjects call; (deleted, errors)."""
    response = await asyncio.to_thread(
        client.delete_objects,
        Bucket=settings.S3_BUCKET,
        Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
    )
    errors = response.get("Errors") or []
    return len(keys) - len(errors), errors


async def sweep(*, days: int, max_delete: int, now: Optional[datetime] = None, client=None, pause: Optional[float] = None) -> dict:
    """Delete the kept documents last written more than `days` ago, at most `max_delete`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    base = root()
    client = client or objects.client()
    pause = PAUSE_SECONDS if pause is None else pause
    done = {
        "days": days, "cutoff": cutoff.isoformat(), "prefix": base,
        "listed": 0, "documents": 0, "newer": 0, "deleted": 0, "deleted_bytes": 0,
        "unknown": 0, "unknown_samples": [], "outside": 0, "errors": 0, "error_samples": [], "capped": False,
    }
    batch: list[str] = []
    sizes: dict[str, int] = {}

    async def flush() -> None:
        deleted, errors = await _delete(client, batch)
        failed = {e.get("Key") for e in errors}
        done["deleted"] += deleted
        done["deleted_bytes"] += sum(sizes.pop(k, 0) for k in batch if k not in failed)
        done["errors"] += len(errors)
        done["error_samples"].extend(f"{e.get('Key')}: {e.get('Code')}" for e in errors[: SAMPLES - len(done["error_samples"])])
        batch.clear()
        sizes.clear()

    listing = pages(client, base)
    while not done["capped"]:
        page = await asyncio.to_thread(next, listing, None)
        if page is None:
            break
        for obj in page:
            key = obj.get("Key") or ""
            done["listed"] += 1
            if not key.startswith(base):
                done["outside"] += 1  # never asked for; never touched
                continue
            if kind_of(key, base) is None:
                done["unknown"] += 1
                if len(done["unknown_samples"]) < SAMPLES:
                    done["unknown_samples"].append(key)
                continue
            done["documents"] += 1
            modified = obj.get("LastModified")
            if modified is None or _aware(modified) >= cutoff:
                done["newer"] += 1
                continue
            if done["deleted"] + done["errors"] + len(batch) >= max_delete:
                done["capped"] = True
                break
            batch.append(key)
            sizes[key] = int(obj.get("Size") or 0)
            if len(batch) >= BATCH:
                await flush()
                await asyncio.sleep(pause)
    if batch:
        await flush()
    done["unknown_percent"] = round(100.0 * done["unknown"] / done["listed"], 2) if done["listed"] else 0.0
    return done


def worth_an_alert(done: dict) -> bool:
    return done.get("listed", 0) >= ALERT_MIN_OBJECTS and done.get("unknown_percent", 0) > float(settings.DOC_CACHE_SKIP_ALERT_PERCENT)


async def _record(session, **values) -> None:
    from app.api.models import ScheduledRuns

    stmt = pg_insert(ScheduledRuns.__table__).values(name=JOB, **values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_=values))
    await session.commit()


async def run_if_due(session, now: Optional[datetime] = None, *, client=None, pause: Optional[float] = None) -> Optional[dict]:
    """The sweep, when it is on, no other process is running it, and a day
    has passed since it last started. What it did, or None when it did not run."""
    from app.api.models import ScheduledRuns

    days = int(settings.DOC_CACHE_DAYS or 0)
    if days <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    async with singleton(session, SWEEP_LOCK) as mine:
        if not mine:
            return None
        last = await session.scalar(select(ScheduledRuns.started_at).where(ScheduledRuns.name == JOB))
        if last is not None and now - _aware(last) < EVERY:
            return None
        await _record(session, started_at=now, finished_at=None)
        done = await sweep(days=days, max_delete=max(0, int(settings.DOC_CACHE_MAX_DELETE_PER_RUN)), now=now, client=client, pause=pause)
        await _record(session, finished_at=datetime.now(timezone.utc), result=done)
    logger.info(
        "doc cache sweep: %d deleted (%d bytes) of %d kept documents over %d days old; %d newer, %d not kept documents left alone, %d errors%s",
        done["deleted"], done["deleted_bytes"], done["documents"], days, done["newer"], done["unknown"], done["errors"],
        "; capped, the rest next time" if done["capped"] else "",
    )
    if worth_an_alert(done):
        from app.api.admin_alerts import notify

        logger.warning("doc cache sweep: %s%% of docs/ is not kept documents, e.g. %s", done["unknown_percent"], done["unknown_samples"][:3])
        await notify(
            session, "doc_cache_unknown",
            title=f"{done['unknown']} objects under docs/ are not kept documents ({done['unknown_percent']}%)",
            body="The kept-documents sweep left them alone. Something other than the document cache is writing under docs/: find what, before anything there is deleted by hand.",
            data={"unknown": done["unknown"], "listed": done["listed"], "percent": done["unknown_percent"], "samples": done["unknown_samples"]},
        )
        await session.commit()
    return done


async def sweep_loop() -> None:
    """For the life of the process; the work on one process a day."""
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(FIRST_LOOK_SECONDS)
    while True:
        try:
            if int(settings.DOC_CACHE_DAYS or 0) > 0:
                async with deliveries.session_scope() as session:
                    await run_if_due(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("doc cache sweep failed")
        await asyncio.sleep(LOOK_EVERY_SECONDS)
