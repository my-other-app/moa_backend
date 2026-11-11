"""My day, the side menu's badge, and the reports page.

"Today" is India's day (hours.IST). A follow-up is due today when its time
falls inside it, overdue when before it and not done.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select

from app.api.support import hours, teams
from app.api.support.models import ACTIVE, CLOSED, RESOLVED, SPAM_TAG, UNFINISHED, SupportCases


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _follow_up_out(f, lead_name: Optional[str], number: Optional[int], subject: Optional[str]) -> dict:
    about = f"lead {lead_name}" if f.lead_id else f"case #{number}"
    return {
        "id": f.id, "kind": f.kind, "title": f.title, "due_at": f.due_at, "lead_id": f.lead_id, "case_id": f.case_id,
        "lead_name": lead_name, "case_number": number, "case_subject": subject, "about": about, "owner_id": f.owner_id,
    }


async def my_day(session, user_id: int, now: Optional[datetime] = None) -> dict:
    from app.api.leads.models import FollowUps, Leads

    now = now or _now()
    start, end = hours.today_bounds(now)
    rows = (await session.execute(
        select(FollowUps, Leads.name, SupportCases.number, SupportCases.subject)
        .outerjoin(Leads, Leads.id == FollowUps.lead_id).outerjoin(SupportCases, SupportCases.id == FollowUps.case_id)
        .where(FollowUps.owner_id == user_id, FollowUps.done.is_(False), FollowUps.due_at < end)
        .order_by(FollowUps.due_at, FollowUps.id)
    )).all()
    due = [_follow_up_out(f, n, num, subj) for f, n, num, subj in rows if f.due_at >= start]
    overdue = [_follow_up_out(f, n, num, subj) for f, n, num, subj in rows if f.due_at < start]
    from app.api.support import cases as cases_mod

    mine = (await session.scalars(
        select(SupportCases).where(
            SupportCases.assignee_id == user_id, SupportCases.status.in_(UNFINISHED), ~SupportCases.tags.contains([SPAM_TAG]),
        ).order_by(SupportCases.first_reply_due.asc().nullslast(), SupportCases.id).limit(200)
    )).all()
    cases_out = [
        {"id": c.id, "number": c.number, "subject": c.subject, "status": c.status, "priority": c.priority,
         "first_reply_due": c.first_reply_due, "resolution_due": c.resolution_due, "overdue": cases_mod.is_overdue(c, now),
         "updated_at": c.updated_at}
        for c in mine
    ]
    return {
        "follow_ups_due": due, "follow_ups_overdue": overdue, "cases_mine_open": cases_out,
        "counts": {"due": len(due), "overdue": len(overdue), "cases": len(cases_out),
                   "cases_needing_answer": sum(1 for c in mine if c.status in ACTIVE)},
    }


async def badge(session, user_id: int, now: Optional[datetime] = None) -> dict:
    from app.api.leads.models import FollowUps

    now = now or _now()
    start, end = hours.today_bounds(now)
    F = FollowUps
    row = (await session.execute(
        select(
            func.count().filter(and_(F.due_at >= start, F.due_at < end)),
            func.count().filter(F.due_at < start),
        ).where(F.owner_id == user_id, F.done.is_(False), F.due_at < end)
    )).one()
    cases = await session.scalar(
        select(func.count()).select_from(SupportCases).where(
            SupportCases.assignee_id == user_id, SupportCases.status.in_(ACTIVE), ~SupportCases.tags.contains([SPAM_TAG]),
        )
    )
    return {"due": int(row[0]), "overdue": int(row[1]), "cases": int(cases or 0)}


def _range(start: Optional[date], end: Optional[date]) -> tuple[datetime, datetime]:
    tz = ZoneInfo(hours.IST)
    end = end or hours.local_date()
    start = start or (end - timedelta(days=29))
    return (datetime.combine(start, time.min, tz).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), time.min, tz).astimezone(timezone.utc))


def _hours(seconds) -> Optional[float]:
    return None if seconds is None else round(float(seconds) / 3600.0, 2)


async def reports(session, start: Optional[date] = None, end: Optional[date] = None) -> dict:
    from app.api.leads.models import STAGES, TYPES, Leads

    lo, hi = _range(start, end)
    C = SupportCases
    in_range = and_(C.created_at >= lo, C.created_at < hi, ~C.tags.contains([SPAM_TAG]), C.merged_into_id.is_(None))
    first = func.extract("epoch", C.first_replied_at - C.created_at)
    resolution = func.extract("epoch", C.resolved_at - C.created_at)
    row = (await session.execute(
        select(
            func.percentile_cont(0.5).within_group(first).filter(C.first_replied_at.isnot(None)),
            func.percentile_cont(0.5).within_group(resolution).filter(C.resolved_at.isnot(None)),
            func.count(),
            func.count().filter(C.first_replied_at.isnot(None), C.first_replied_at <= C.first_reply_due),
            func.count().filter(C.first_replied_at.isnot(None)),
            func.count().filter(C.resolved_at.isnot(None), C.resolved_at <= C.resolution_due),
            func.count().filter(C.resolved_at.isnot(None)),
        ).where(in_range)
    )).one()
    by_person_rows = (await session.execute(
        select(
            C.assignee_id, func.count().filter(C.status.in_(UNFINISHED)), func.count().filter(C.status.in_((RESOLVED, CLOSED))),
            func.percentile_cont(0.5).within_group(first).filter(C.first_replied_at.isnot(None)),
        ).where(in_range).group_by(C.assignee_id)
    )).all()
    names = await teams.names(session, [r[0] for r in by_person_rows])
    by_topic_rows = (await session.execute(
        select(C.topic, func.count(), func.count().filter(C.status.in_(UNFINISHED))).where(in_range).group_by(C.topic)
    )).all()
    survey = (await session.execute(
        select(func.count().filter(C.survey_solved.is_(True)), func.count().filter(C.survey_solved.is_(False)))
        .where(C.survey_at >= lo, C.survey_at < hi)
    )).one()
    open_now = await session.scalar(select(func.count()).select_from(C).where(C.status.in_(UNFINISHED), ~C.tags.contains([SPAM_TAG])))

    L = Leads
    live = L.merged_into_id.is_(None)
    pipeline: dict = {t: {s: 0 for s in STAGES} for t in TYPES}
    for t, s, n in (await session.execute(select(L.type, L.stage, func.count()).where(live).group_by(L.type, L.stage))).all():
        pipeline.setdefault(t, {})[s] = int(n)
    won = await session.scalar(select(func.count()).select_from(L).where(live, L.won_at >= lo, L.won_at < hi))
    lost = await session.scalar(select(func.count()).select_from(L).where(live, L.lost_at >= lo, L.lost_at < hi))
    by_reason = {k or "unknown": int(n) for k, n in (await session.execute(
        select(L.lost_reason, func.count()).where(live, L.lost_at >= lo, L.lost_at < hi, L.stage == "lost").group_by(L.lost_reason)
    )).all()}
    value_by_stage = {s: 0 for s in STAGES}
    for s, v in (await session.execute(select(L.stage, func.coalesce(func.sum(L.value_inr), 0)).where(live).group_by(L.stage))).all():
        value_by_stage[s] = int(v or 0)
    from app.api.leads.models import FollowUps

    F = FollowUps
    fu = (await session.execute(
        select(
            func.count().filter(F.done.is_(True), F.done_at <= F.due_at),
            func.count().filter(F.done.is_(True)),
            func.count().filter(F.done.is_(False), F.due_at < _now()),
        ).where(F.due_at >= lo, F.due_at < hi)
    )).one()
    open_leads = await session.scalar(select(func.count()).select_from(L).where(live, L.stage.in_(("new", "contacted", "meeting", "proposal", "negotiation"))))
    with_next = await session.scalar(
        select(func.count(func.distinct(F.lead_id))).join(L, L.id == F.lead_id)
        .where(live, L.stage.in_(("new", "contacted", "meeting", "proposal", "negotiation")), F.done.is_(False), F.due_at >= _now())
    )
    return {
        "from": lo, "to": hi,
        "support": {
            "cases": int(row[2] or 0), "open_now": int(open_now or 0),
            "first_reply_median_h": _hours(row[0]), "resolution_median_h": _hours(row[1]),
            "first_reply_on_target": {"on_target": int(row[3] or 0), "of": int(row[4] or 0)},
            "resolution_on_target": {"on_target": int(row[5] or 0), "of": int(row[6] or 0)},
            "by_person": [
                {"user_id": uid, "name": names.get(uid) if uid else "Unassigned", "open": int(o or 0), "resolved": int(r or 0),
                 "first_reply_median_h": _hours(med)}
                for uid, o, r, med in by_person_rows
            ],
            "by_topic": [{"topic": t, "cases": int(n or 0), "open": int(o or 0)} for t, n, o in by_topic_rows],
            "survey": {"yes": int(survey[0] or 0), "no": int(survey[1] or 0)},
        },
        "leads": {
            "pipeline": pipeline,
            "won_lost": {"won": int(won or 0), "lost": int(lost or 0), "by_reason": by_reason},
            "value_by_stage": value_by_stage,
            "follow_ups": {"done_on_time": int(fu[0] or 0), "done": int(fu[1] or 0), "overdue": int(fu[2] or 0)},
            "open_with_next_step": {"with": int(with_next or 0), "of": int(open_leads or 0)},
        },
    }


def report_rows(data: dict) -> list:
    """The report as one sheet: a section per block."""
    from app.core.utils.spreadsheet import Bold

    s, ld = data["support"], data["leads"]
    rows: list = [Bold(["Support", "", "", ""])]
    rows += [
        ["Cases opened", s["cases"], "", ""], ["Open now", s["open_now"], "", ""],
        ["First reply, median hours", s["first_reply_median_h"], "", ""],
        ["Resolution, median hours", s["resolution_median_h"], "", ""],
        ["First reply on target", s["first_reply_on_target"]["on_target"], "of", s["first_reply_on_target"]["of"]],
        ["Resolved on target", s["resolution_on_target"]["on_target"], "of", s["resolution_on_target"]["of"]],
        ["Survey: solved", s["survey"]["yes"], "not solved", s["survey"]["no"]],
        [], Bold(["Person", "Open", "Resolved", "First reply, median hours"]),
    ]
    rows += [[p["name"] or "", p["open"], p["resolved"], p["first_reply_median_h"]] for p in s["by_person"]]
    rows += [[], Bold(["Topic", "Cases", "Open", ""])]
    rows += [[t["topic"] or "", t["cases"], t["open"], ""] for t in s["by_topic"]]
    rows += [[], Bold(["Leads: type", "Stage", "Leads", ""])]
    for t, stages in ld["pipeline"].items():
        rows += [[t, st, n, ""] for st, n in stages.items() if n]
    rows += [[], Bold(["Won", ld["won_lost"]["won"], "Lost", ld["won_lost"]["lost"]])]
    rows += [["Lost because", k, n, ""] for k, n in ld["won_lost"]["by_reason"].items()]
    rows += [[], Bold(["Stage", "Value (INR a year)", "", ""])]
    rows += [[k, v, "", ""] for k, v in ld["value_by_stage"].items()]
    return rows
