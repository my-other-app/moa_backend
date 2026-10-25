"""The job queue's failed and stuck jobs, one by one (app/core/jobs.py).

GET /admin/jobs (app/api/health.py) is the queue per name and the runners;
POST /admin/jobs/{id}/retry puts a dead job back. These add the jobs
themselves, and a way to drop one:

- dead: failed on every try and given up on (the `job_dead` alert);
- failing: failed at least once and waiting to be tried again;
- stuck: claimed by a runner that stopped renewing its lease, or due for
  longer than STUCK_AFTER with nothing taking it (no runner running).

Cancelling settles a job without running it: a dead one is marked
finished, and one that is waiting or stuck is finished too, so no runner
takes it. One a runner holds right now (its lease is live) cannot be
cancelled; it is either done in a moment or it becomes stuck. Every
cancel is in the admin audit log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request
from sqlalchemy import func, or_, select

from app.api.audit import service as audit
from app.core import jobs
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ModerateAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/jobs", tags=["admin-jobs"])

STUCK_AFTER = timedelta(minutes=10)
STATES = ("dead", "failing", "stuck")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at):
    return at.isoformat() if at else None


def _where(state: str, now: datetime):
    J = jobs.Jobs
    unfinished = [J.finished_at.is_(None), J.dead == False]  # noqa: E712
    if state == "dead":
        return [J.dead == True]  # noqa: E712
    if state == "failing":
        return [*unfinished, J.last_error.isnot(None), or_(J.locked_until.is_(None), J.locked_until > now)]
    if state == "stuck":
        return [*unfinished, or_(
            sa.and_(J.locked_until.isnot(None), J.locked_until <= now),
            sa.and_(J.locked_until.is_(None), J.run_at <= now - STUCK_AFTER),
        )]
    raise CustomHTTPException(422, message=f"state is one of {', '.join(STATES)}")


def _state_of(row, now: datetime) -> str:
    if row.dead:
        return "dead"
    if row.finished_at is not None:
        return "finished"
    if row.locked_until is not None:
        return "running" if row.locked_until > now else "stuck"
    if row.run_at <= now - STUCK_AFTER:
        return "stuck"
    return "failing" if row.last_error else "waiting"


def _out(row, now: datetime) -> dict:
    return {
        "id": row.id, "name": row.name, "state": _state_of(row, now), "attempts": row.attempts,
        "max_attempts": row.max_attempts, "error": (row.last_error or None) and row.last_error[:2000],
        "run_at": _iso(row.run_at), "last_run_at": _iso(row.updated_at), "created_at": _iso(row.created_at),
        "locked_by": row.locked_by, "locked_until": _iso(row.locked_until), "priority": row.priority,
        "payload_keys": sorted((row.payload or {}).keys())[:20],
    }


@router.get("/items", summary="Failed and stuck jobs, one by one (admin)")
async def list_jobs(
    session: SessionDep,
    user: AdminAuth,
    state: str = Query("dead", description="dead, failing or stuck"),
    name: Optional[str] = Query(None, max_length=120),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    now = _now()
    where = _where(state, now)
    if name:
        where.append(jobs.Jobs.name == name)
    total = int(await session.scalar(select(func.count()).select_from(jobs.Jobs).where(*where)) or 0)
    rows = (await session.scalars(
        select(jobs.Jobs).where(*where).order_by(jobs.Jobs.updated_at.desc(), jobs.Jobs.id.desc()).offset(offset).limit(limit)
    )).all()
    counts = {s: int(await session.scalar(select(func.count()).select_from(jobs.Jobs).where(*_where(s, now))) or 0) for s in STATES}
    return {"items": [_out(r, now) for r in rows], "total": total, "counts": counts, "state": state,
            "stuck_after_minutes": int(STUCK_AFTER.total_seconds() // 60), "offset": offset, "limit": limit}


@router.post("/{job_id}/cancel", summary="Settle a dead, failing or stuck job without running it (moderate)")
async def cancel_job(job_id: int, session: SessionDep, user: ModerateAuth, request: Request) -> dict:
    now = _now()
    row = await session.scalar(select(jobs.Jobs).where(jobs.Jobs.id == job_id).with_for_update())
    if row is None:
        raise CustomHTTPException(404, message="No such job.", error_code="JOB_NOT_FOUND")
    if row.finished_at is not None and not row.dead:
        raise CustomHTTPException(409, message="That job has finished already.", error_code="JOB_FINISHED")
    if not row.dead and row.locked_until is not None and row.locked_until > now:
        raise CustomHTTPException(409, message="A runner is working on that job right now; try again in a minute.", error_code="JOB_RUNNING")
    was = _state_of(row, now)
    who = user.full_name or user.email
    row.dead = False
    row.finished_at = sa.func.statement_timestamp()
    row.locked_until = None
    row.locked_by = None
    row.last_error = (f"Cancelled by {who}. " + (row.last_error or ""))[:4000]
    row.updated_at = sa.func.statement_timestamp()
    await audit.record(session, actor=user, action="job.cancel", entity_type="job", entity_id=row.id,
                       summary=f"Cancelled job #{row.id} ({row.name}, {was})"[:255], details={"name": row.name, "was": was},
                       request=request)
    await session.commit()
    return {"id": job_id, "cancelled": True, "was": was}
