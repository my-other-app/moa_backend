"""The desk's sweep: every ten minutes, one worker at a time (a Postgres
advisory lock on its own connection, as the club mailbox's sweep).

- The digest: from 08:00 India time, once a day (scheduled_runs
  `support.digest` holds the day it went), a job per team member
  (`support.digest_mail`, dedupe key per person and day).
- Follow-ups three days overdue: each marked once (`overdue_flagged_at`) in
  the transaction that writes the owners' mail job.
- Waiting on them: a reminder on day 5 (`reminded_at`, once a wait), closed
  on day 7 with the reason `no_reply`.
- Resolved for 7 days: closed (`resolved`).
- Retention: a closed case's messages and files deleted once it has been
  closed for the desk's `retention_days`; the case keeps its counts and
  times for the reports (`purged_at`).
- Uploads from the help form never sent with a request, after a day: deleted.
- Away members whose `away_until` has passed: back.

Every step is idempotent -- it acts on what the rows say now, and marks
what it did in the same transaction -- so a sweep run twice, or by two
workers at once, does each thing once. Each step on its own: one failing
leaves the rest to run.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text as sa_text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.support import cases, files, hours, mail, store, teams
from app.api.support.models import (
    CLOSED,
    RESOLVED,
    WAITING,
    AdminTeamMembers,
    SupportAttachments,
    SupportCases,
    SupportMessages,
)
from app.core import jobs
from app.db import lock_keys

logger = logging.getLogger(__name__)

SWEEP_LOCK = lock_keys.SUPPORT_SWEEP
SWEEP_INTERVAL = 600
DIGEST = "support.digest"
DIGEST_HOUR = 8
REMIND_AFTER = timedelta(days=5)
CLOSE_WAITING_AFTER = timedelta(days=7)
CLOSE_RESOLVED_AFTER = timedelta(days=7)
OVERDUE_AFTER = timedelta(days=3)
BATCH = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def digest(session, now: Optional[datetime] = None) -> int:
    """The day's digest jobs, once a day from 08:00 IST. Returns how many."""
    from app.api.models import ScheduledRuns
    from app.api.users.models import UserTypes, Users

    now = now or _now()
    local = now.astimezone(ZoneInfo(hours.IST))
    if local.hour < DIGEST_HOUR:
        return 0
    day = local.date().isoformat()
    row = await session.scalar(select(ScheduledRuns).where(ScheduledRuns.name == DIGEST).with_for_update())
    if row is not None and (row.result or {}).get("date") == day:
        await session.commit()
        return 0
    people = list((await session.scalars(
        select(AdminTeamMembers.user_id).join(Users, Users.id == AdminTeamMembers.user_id)
        .where(Users.user_type == UserTypes.admin, Users.is_active.is_(True)).distinct()
    )).all())
    n = await jobs.enqueue_many(session, mail.DIGEST_JOB, [({"user_id": u, "date": day}, f"{mail.DIGEST_JOB}:{u}:{day}") for u in people])
    stmt = pg_insert(ScheduledRuns.__table__).values(name=DIGEST, started_at=now, finished_at=now, result={"date": day, "people": len(people)})
    await session.execute(stmt.on_conflict_do_update(index_elements=["name"], set_={
        "started_at": now, "finished_at": now, "result": {"date": day, "people": len(people)},
    }))
    await session.commit()
    return n


async def overdue(session, now: Optional[datetime] = None) -> int:
    """Follow-ups three days overdue, flagged to the owners once each."""
    from app.api.leads.models import FollowUps

    now = now or _now()
    F = FollowUps
    ids = list((await session.scalars(
        select(F.id).where(F.done.is_(False), F.overdue_flagged_at.is_(None), F.due_at <= now - OVERDUE_AFTER)
        .order_by(F.id).limit(500).with_for_update(skip_locked=True)
    )).all())
    if not ids:
        await session.commit()
        return 0
    await session.execute(update(F).where(F.id.in_(ids)).values(overdue_flagged_at=now).execution_options(synchronize_session=False))
    batch = f"{ids[0]}-{ids[-1]}-{len(ids)}"
    await jobs.enqueue(session, mail.OVERDUE_JOB, {"follow_up_ids": ids, "batch": batch}, dedupe_key=f"{mail.OVERDUE_JOB}:{batch}")
    await session.commit()
    return len(ids)


async def waiting(session, now: Optional[datetime] = None) -> dict:
    """Day 5 waiting: the reminder. Day 7: closed, no reply."""
    now = now or _now()
    C = SupportCases
    reminded = closed = 0
    rows = (await session.scalars(
        select(C).where(C.status == WAITING, C.waiting_since <= now - CLOSE_WAITING_AFTER).order_by(C.id).limit(BATCH)
        .with_for_update(skip_locked=True)
    )).all()
    for case in rows:
        await cases.set_status(session, case, CLOSED, now=now, reason="no_reply")
        await cases.system_line(session, case, "Closed: no answer from them in 7 days.", now)
        await cases.audit(session, None, "case.auto_close", case, f"Case #{case.number} closed: no reply in 7 days")
        closed += 1
    rows = (await session.scalars(
        select(C).where(C.status == WAITING, C.reminded_at.is_(None), C.waiting_since <= now - REMIND_AFTER).order_by(C.id).limit(BATCH)
        .with_for_update(skip_locked=True)
    )).all()
    for case in rows:
        case.reminded_at = now
        if case.requester_email:
            stamp = int(case.waiting_since.timestamp()) if case.waiting_since else 0
            await jobs.enqueue(session, mail.REMINDER_JOB, {"case_id": case.id}, dedupe_key=f"{mail.REMINDER_JOB}:{case.id}:{stamp}")
        await cases.system_line(session, case, "Reminded them: waiting 5 days.", now)
        reminded += 1
    await session.commit()
    return {"reminded": reminded, "closed": closed}


async def resolved(session, now: Optional[datetime] = None) -> int:
    now = now or _now()
    C = SupportCases
    rows = (await session.scalars(
        select(C).where(C.status == RESOLVED, C.resolved_at <= now - CLOSE_RESOLVED_AFTER).order_by(C.id).limit(BATCH)
        .with_for_update(skip_locked=True)
    )).all()
    for case in rows:
        await cases.set_status(session, case, CLOSED, now=now, reason="resolved")
        await cases.audit(session, None, "case.auto_close", case, f"Case #{case.number} closed: resolved 7 days ago")
    await session.commit()
    return len(rows)


async def retention(session, now: Optional[datetime] = None) -> int:
    """Closed cases past the retention period: their messages and files
    deleted, the case kept with its counts. Returns how many cases."""
    now = now or _now()
    desk = await hours.get(session)
    cutoff = now - timedelta(days=int(desk["retention_days"]))
    C = SupportCases
    done = 0
    while True:
        rows = (await session.scalars(
            select(C).where(C.status == CLOSED, C.purged_at.is_(None), C.closed_at <= cutoff).order_by(C.id).limit(BATCH)
            .with_for_update(skip_locked=True)
        )).all()
        if not rows:
            break
        ids = [c.id for c in rows]
        keys = list((await session.scalars(select(SupportAttachments.storage_key).where(SupportAttachments.case_id.in_(ids)))).all())
        await session.execute(delete(SupportAttachments).where(SupportAttachments.case_id.in_(ids)))
        await session.execute(delete(SupportMessages).where(SupportMessages.case_id.in_(ids)))
        for c in rows:
            c.purged_at, c.survey_comment, c.context = now, None, None
        await session.commit()
        await asyncio.to_thread(store.delete, keys)
        done += len(rows)
        if len(rows) < BATCH:
            break
    return done


async def loose_uploads(session, now: Optional[datetime] = None) -> int:
    now = now or _now()
    A = SupportAttachments
    rows = (await session.execute(
        select(A.id, A.storage_key).where(A.case_id.is_(None), A.created_at < now - files.LOOSE_FOR).limit(1000)
    )).all()
    if not rows:
        return 0
    await session.execute(delete(A).where(A.id.in_([r[0] for r in rows])))
    await session.commit()
    await asyncio.to_thread(store.delete, [r[1] for r in rows])
    return len(rows)


async def back(session, now: Optional[datetime] = None) -> int:
    n = await teams.back_from_away(session, now)
    await session.commit()
    return n


STEPS = (("digest", digest), ("overdue", overdue), ("waiting", waiting), ("resolved", resolved), ("retention", retention),
         ("uploads", loose_uploads), ("back", back))


async def sweep(session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    out: dict = {}
    for name, step in STEPS:
        try:
            out[name] = await step(session, now)
        except Exception as exc:  # noqa: BLE001 - one step failing leaves the others
            logger.warning("desk sweep: %s failed (%s)", name, type(exc).__name__, exc_info=True)
            await session.rollback()
            out[name] = "failed"
    return out


async def run_sweep(session, now: Optional[datetime] = None) -> Optional[dict]:
    """The sweep, if no other worker is running it. None when one is."""
    bind = session.bind
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as lock_conn:
            return await _locked(lock_conn, session, now)
    return await _locked(bind, session, now)


async def _locked(conn, session, now) -> Optional[dict]:
    if not await conn.scalar(sa_text(f"SELECT pg_try_advisory_lock({SWEEP_LOCK})")):
        return None
    try:
        return await sweep(session, now)
    finally:
        await conn.scalar(sa_text(f"SELECT pg_advisory_unlock({SWEEP_LOCK})"))


async def sweep_loop(interval: int = SWEEP_INTERVAL) -> None:
    from app.api.deliveries import service as deliveries

    await asyncio.sleep(43)
    while True:
        try:
            async with deliveries.session_scope() as session:
                done = await run_sweep(session)
                if done and any(v for v in done.values() if v not in (0, {"reminded": 0, "closed": 0})):
                    logger.info("desk sweep: %s", done)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("desk sweep failed (%s)", type(exc).__name__)
        await asyncio.sleep(interval)
