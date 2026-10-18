"""Time the work each web worker does on its own, every minute and at boot,
at this database's size: the status clock (closeout.advance_clock), the
counter check (counters.drift: every six hours, on one process, in slices of
ids since write_path; before that at each worker's first sweep, one scan) and
the timed-email sweep (emails.sweep: what it reads every minute -- by
email_templates.due_at since write_path, every timed email before).

    BENCH_DB=moa_s1 python -m scripts.bench.sweeps --templates 2000,10000,40000 --out results/scale/s1_sweeps.json

The seeded world has no email templates. The first ticket an event sends
copies the club's four journey emails onto the event (emails.invite_email →
ensure_event_templates); two of them are timed (24 hours and 30 minutes
before the start) and enabled. --templates gives them to that many events
with registrations, inside a transaction that is rolled back, and times the
sweep's query and its loop over the rows (without sending anything).
Everything here is rolled back.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid

from scripts.bench import _env  # noqa: F401  (settings before app)

from sqlalchemy import event, insert, select, text


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--templates", default="2000,10000", help="events given journey templates, one measurement each")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--out")
    args = p.parse_args()

    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.orm import joinedload

    from app.api.closeout import service as closeout
    from app.api.discover import counters
    from app.api.emails import service as emails
    from app.api.emails.models import EmailStage, EmailTemplates
    from app.api.events.models import Events
    from app.core.mail.schema import JOURNEY_STAGES
    from app.core.mail.starters import email_starter
    from app.db.core import SESSION_KWARGS, engine
    from app.db.listeners import add_loader_criteria

    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
    seen = {"n": 0, "t0": 0.0, "longest": 0.0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(*_a):
        seen["n"] += 1
        seen["t0"] = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _took(*_a):
        seen["longest"] = max(seen["longest"], time.perf_counter() - seen["t0"])

    out: dict = dict(db=_env.DB, clock=[], drift=[], email_sweep=[])

    async def in_tx(work):
        async with engine.connect() as conn:
            tx = await conn.begin()
            session = async_sessionmaker(bind=conn, **SESSION_KWARGS)()
            add_loader_criteria(session)
            try:
                return await work(session)
            finally:
                await session.close()
                await tx.rollback()

    # ── the status clock ──
    for i in range(args.runs):
        async def clock(session):
            seen["n"] = 0
            t0 = time.perf_counter()
            moved = await closeout.advance_clock(session)
            return dict(ms=round((time.perf_counter() - t0) * 1000, 1), statements=seen["n"], moved=moved)
        r = await in_tx(clock)
        out["clock"].append(r)
        print("advance_clock", r, flush=True)

    # ── the counter check ──
    for i in range(args.runs):
        async def drift(session):
            seen["n"], seen["longest"] = 0, 0.0
            t0 = time.perf_counter()
            found = await counters.drift(session)
            return dict(ms=round((time.perf_counter() - t0) * 1000, 1), statements=seen["n"],
                        longest_statement_ms=round(seen["longest"] * 1000, 1), events=len(found["events"]), clubs=len(found["clubs"]))
        r = await in_tx(drift)
        out["drift"].append(r)
        print("counters.drift", r, flush=True)

    # ── the timed-email sweep ──
    starters = {}
    for s in JOURNEY_STAGES:
        tpl = emails._new(1, 1, EmailStage(s), email_starter(s))
        starters[s] = dict(stage=EmailStage(s), name=tpl.name, subject=tpl.subject, design=tpl.design, attachments=tpl.attachments,
                           audience=tpl.audience, trigger=tpl.trigger, channels=tpl.channels, enabled=tpl.enabled, version=1)
    for n_events in [int(x) for x in args.templates.split(",") if x]:
        async def sweep(session):
            ids = list((await session.execute(
                select(Events.id, Events.club_id).where(Events.going_count > 0, Events.is_deleted == False).order_by(Events.id.desc()).limit(n_events)  # noqa: E712
            )).all())
            rows = [dict(id=uuid.uuid4(), club_id=club_id, event_id=eid, **starters[s]) for eid, club_id in ids for s in JOURNEY_STAGES]
            t0 = time.perf_counter()
            for i in range(0, len(rows), 5000):
                await session.execute(insert(EmailTemplates.__table__), rows[i:i + 5000])
            inserted_ms = (time.perf_counter() - t0) * 1000
            size = (await session.execute(text("SELECT pg_total_relation_size('email_templates')"))).scalar()
            # What emails.sweep reads every minute, the same statement: by
            # due instant where the code has one (emails.due_query), else
            # every timed email.
            from datetime import datetime, timezone

            if hasattr(emails, "due_query"):
                stmt = emails.due_query(datetime.now(timezone.utc))
                await session.execute(text("ANALYZE email_templates"))
            else:
                stmt = (
                    select(EmailTemplates, Events)
                    .join(Events, Events.id == EmailTemplates.event_id)
                    .options(joinedload(Events.club))
                    .where(
                        EmailTemplates.enabled == True,  # noqa: E712
                        EmailTemplates.event_id.isnot(None),
                        Events.is_deleted == False,  # noqa: E712
                        EmailTemplates.trigger["mode"].as_string().in_(["BEFORE_START", "AFTER_END"]),
                    )
                )
            runs = []
            for _ in range(2):
                session.expunge_all()
                t1 = time.perf_counter()
                got = (await session.execute(stmt)).unique().all()
                t2 = time.perf_counter()
                due = 0
                now = datetime.now(timezone.utc)
                for tpl, ev in got:
                    d = emails.due_at(ev, emails.fields_of(tpl).trigger)
                    if d is not None and now - emails.SWEEP_GRACE <= d <= now:
                        due += 1
                t3 = time.perf_counter()
                runs.append(dict(rows=len(got), query_ms=round((t2 - t1) * 1000, 1), loop_ms=round((t3 - t2) * 1000, 1), due=due))
            return dict(events_with_templates=len(ids), templates=len(rows), insert_ms=round(inserted_ms), email_templates_bytes=size, runs=runs)
        r = await in_tx(sweep)
        out["email_sweep"].append(r)
        print("email sweep", json.dumps(r), flush=True)

    await engine.dispose()
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
