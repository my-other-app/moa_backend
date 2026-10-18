"""Is the API alive, is it ready, and is anything running its jobs.

- GET /healthz: liveness. The process answers; nothing else is asked (no
  database), so a slow database never gets a healthy process restarted.
- GET /readyz: readiness. The database answers, its schema is at the code's
  migration head (code deployed ahead of its migration is not ready), and
  Redis answers when APP_REDIS_URL is set. 503 when any of those fails. It
  also says who is running the job queue -- the runners heard from in the
  last minute (app.core.jobs.alive) -- as a warning rather than a failure:
  with APP_JOBS_WORKER set and no worker alive, jobs pile up, but every
  request is still served.
- GET /api/v1/admin/jobs: the queue per job name (due, scheduled, running,
  abandoned, dead, the oldest wait) and the runners; POST
  /api/v1/admin/jobs/{id}/retry puts a dead job back in the queue.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.core import jobs, redis
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ModerateAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

public_router = APIRouter(tags=["health"])
admin_router = APIRouter(prefix="/admin/jobs", tags=["admin-jobs"])

#: How long readiness waits on the database.
DB_TIMEOUT = 3.0

_heads: Optional[set[str]] = None


def migration_heads() -> set[str]:
    """The migration heads in the code this process runs, read once."""
    global _heads
    if _heads is None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        _heads = set(ScriptDirectory.from_config(config).get_heads())
    return _heads


async def check_database() -> dict:
    """The database answers, and its revision is one of the code's heads."""
    from app.db.core import engine

    async def ask():
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
            has_table = await conn.scalar(sa.text("SELECT to_regclass('alembic_version') IS NOT NULL"))
            versions = set((await conn.execute(sa.text("SELECT version_num FROM alembic_version"))).scalars().all()) if has_table else set()
            alive = await jobs.alive(conn)
            return versions, alive

    try:
        versions, alive = await asyncio.wait_for(ask(), timeout=DB_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return {"database": {"ok": False, "error": exc.__class__.__name__}}
    heads = migration_heads()
    return {
        "database": {"ok": True},
        "migrations": {"ok": bool(versions) and versions <= heads, "database": sorted(versions), "code": sorted(heads)},
        "_alive": alive,
    }


def jobs_status(alive: list[dict]) -> dict:
    kinds = sorted({w["kind"] for w in alive})
    workers = [w for w in alive if w["kind"] == "worker"]
    out = {"mode": "worker" if settings.JOBS_WORKER else "in-process", "runners": len(alive), "kinds": kinds}
    if settings.JOBS_WORKER and not workers:
        out["warning"] = "APP_JOBS_WORKER is set but no worker has been heard from in the last minute: jobs are waiting"
    if settings.SWEEPS_IN_WORKER and not any(w["sweeps"] for w in workers):
        out["sweeps_warning"] = "APP_SWEEPS_IN_WORKER is set but no worker running the sweeps has been heard from"
    return out


@public_router.get("/healthz", summary="Liveness: the process answers", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True}


@public_router.get("/readyz", summary="Readiness: database, migrations, Redis", include_in_schema=False)
async def readyz():
    checks = await check_database()
    alive = checks.pop("_alive", [])
    if redis.configured():
        checks["redis"] = {"ok": bool(await redis.ping())}
    checks["jobs"] = jobs_status(alive)
    ok = all(c.get("ok", True) for name, c in checks.items() if name != "jobs")
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)


@admin_router.get("", summary="The job queue per name, and the runners")
async def queue(session: SessionDep, user: AdminAuth) -> dict:
    out = await jobs.stats(session)
    out["mode"] = "worker" if settings.JOBS_WORKER else "in-process"
    return out


@admin_router.post("/{job_id}/retry", summary="Put a dead job back in the queue")
async def retry(session: SessionDep, user: ModerateAuth, job_id: int) -> dict:
    row = await session.get(jobs.Jobs, job_id)
    if row is None:
        raise CustomHTTPException(404, message="No such job.")
    if not row.dead:
        raise CustomHTTPException(409, message="Only a dead job can be retried; this one is still in the queue or finished.")
    if row.dedupe_key and await session.scalar(
        sa.select(jobs.Jobs.id).where(jobs.Jobs.dedupe_key == row.dedupe_key, jobs.Jobs.finished_at.is_(None), jobs.Jobs.dead == False, jobs.Jobs.id != row.id)  # noqa: E712
    ):
        raise CustomHTTPException(409, message="The same job is already waiting in the queue.")
    row.dead = False
    row.attempts = 0
    row.last_error = None
    row.run_at = sa.func.statement_timestamp()
    row.updated_at = sa.func.statement_timestamp()
    await session.flush()
    jobs._after_commit(session, row.id)
    await session.commit()
    return {"id": row.id, "name": row.name, "requeued": True}
