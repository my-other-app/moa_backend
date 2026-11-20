"""Each automatic kind's rule as one query (CONTRACT §3.1).

`count(session, ctx, task, regs)` answers {registration_id: count} for the
guests in `regs` (a list of registrations of the event), counting only what
the task's window allows. One grouped query per task, never one per guest:
the same function serves one guest's evaluation and the full recompute.
Every function only reads, so running one twice changes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import Boolean, distinct, func, literal_column, or_, select, union_all

from app.api.quests import kinds as K


@dataclass
class Ctx:
    """What every rule needs: the event, its quest, and now."""

    event: object
    quest: object
    now: datetime


@dataclass
class Guest:
    id: object
    user_id: Optional[int]


def window(ctx: Ctx, task) -> tuple[Optional[datetime], Optional[datetime]]:
    """The task's window where it has one, else the quest's."""
    return (task.starts_at or ctx.quest.starts_at, task.ends_at or ctx.quest.ends_at)


def _between(column, start: Optional[datetime], end: Optional[datetime]) -> list:
    out = []
    if start is not None:
        out.append(column >= start)
    if end is not None:
        out.append(column <= end)
    return out


def open_now(ctx: Ctx, task) -> bool:
    start, end = window(ctx, task)
    return (start is None or ctx.now >= start) and (end is None or ctx.now <= end)


def _by_user(rows: Iterable, regs: list[Guest]) -> dict:
    users = {g.user_id: g.id for g in regs if g.user_id is not None}
    return {users[u]: int(n) for u, n in rows if u in users and n}


async def _checked_in(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.checkins.models import CheckIns

    start, end = window(ctx, task)
    before = (task.config or {}).get("before")
    if before:
        try:
            cut = datetime.fromisoformat(str(before).replace("Z", "+00:00"))
            cut = cut if cut.tzinfo else cut.replace(tzinfo=timezone.utc)
            end = min(end, cut) if end is not None else cut
        except ValueError:
            pass
    rows = await session.execute(
        select(CheckIns.registration_id).where(
            CheckIns.event_id == ctx.event.id, CheckIns.registration_id.in_([g.id for g in regs]),
            *_between(CheckIns.checked_in_at, start, end),
        )
    )
    return {r: 1 for (r,) in rows.all()}


async def _network(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    """Distinct checked-in guests of the event met by QR here, at most
    `per_minute` counted in any one minute (each at their first meeting)."""
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.profiles.models import NetworkEncounters as E
    from app.api.profiles.models import NetworkPeople as NP

    cfg = task.config or {}
    direction = cfg.get("direction", "either")
    cap = max(1, int(cfg.get("per_minute") or 2))
    uids = [g.user_id for g in regs if g.user_id is not None]
    if not uids:
        return {}
    start, end = window(ctx, task)
    checked = select(R.user_id).where(R.event_id == ctx.event.id, R.is_attended == True, R.is_deleted == False)  # noqa: E712
    met = [E.event_id == ctx.event.id, E.method == "qr", *_between(E.met_at, start, end)]
    parts = []
    if direction in ("saved", "either"):
        parts.append(
            select(NP.owner_id.label("me"), NP.person_id.label("other"), E.met_at.label("at"))
            .join(E, E.network_person_id == NP.id).where(NP.owner_id.in_(uids), NP.person_id.in_(checked), *met)
        )
    if direction in ("saved_by", "either"):
        parts.append(
            select(NP.person_id.label("me"), NP.owner_id.label("other"), E.met_at.label("at"))
            .join(E, E.network_person_id == NP.id).where(NP.person_id.in_(uids), NP.owner_id.in_(checked), *met)
        )
    both = (union_all(*parts) if len(parts) > 1 else parts[0]).subquery()
    first = select(both.c.me, both.c.other, func.min(both.c.at).label("at")).where(both.c.me != both.c.other).group_by(both.c.me, both.c.other).subquery()
    ranked = select(
        first.c.me,
        func.row_number().over(partition_by=(first.c.me, func.date_trunc("minute", first.c.at)), order_by=(first.c.at, first.c.other)).label("n"),
    ).subquery()
    rows = await session.execute(select(ranked.c.me, func.count()).where(ranked.c.n <= cap).group_by(ranked.c.me))
    return _by_user(rows.all(), regs)


async def _quiz(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.live.models import LiveQuizAnswers, LiveQuizPlayers, LiveQuizzes, QuizStatus

    quiz = await session.scalar(select(LiveQuizzes).where(LiveQuizzes.event_id == ctx.event.id))
    if quiz is None:
        return {}
    P = LiveQuizPlayers
    ids = [g.id for g in regs]
    start, end = window(ctx, task)
    # Only a player who answered inside the window counts (any answer, when there is no window).
    answered = select(LiveQuizAnswers.player_id).where(*_between(LiveQuizAnswers.answered_at, start, end))
    if task.kind == K.QUIZ_PLAYED:
        rows = await session.execute(select(P.registration_id).where(P.quiz_id == quiz.id, P.registration_id.in_(ids), P.id.in_(answered)).distinct())
        return {r: 1 for (r,) in rows.all()}
    best = (
        select(P.registration_id, func.max(P.score).label("score"))
        .where(P.quiz_id == quiz.id, P.registration_id.isnot(None), P.id.in_(answered))
        .group_by(P.registration_id).subquery()
    )
    if task.kind == K.QUIZ_SCORE:
        need = int((task.config or {}).get("min_score") or 1)
        rows = await session.execute(select(best.c.registration_id).where(best.c.registration_id.in_(ids), best.c.score >= need))
        return {r: 1 for (r,) in rows.all()}
    # QUIZ_TOP: only once the quiz is over; rank = 1 + players with a higher score.
    if quiz.status is not QuizStatus.ended:
        return {}
    top = int((task.config or {}).get("top") or 1)
    higher = select(func.count()).select_from(P).where(P.quiz_id == quiz.id, P.score > best.c.score).correlate(best).scalar_subquery()
    rows = await session.execute(select(best.c.registration_id).where(best.c.registration_id.in_(ids), best.c.score > 0, higher + 1 <= top))
    return {r: 1 for (r,) in rows.all()}


def _keys(regs: list[Guest]) -> dict:
    from app.api.live.room import account_key

    return {account_key(g.user_id): g.id for g in regs if g.user_id is not None}


async def _polls(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.live.models import LivePollResponses as PR
    from app.api.live.models import LivePolls

    keys = _keys(regs)
    if not keys:
        return {}
    start, end = window(ctx, task)
    rows = await session.execute(
        select(PR.voter_key, func.count(distinct(PR.poll_id))).join(LivePolls, LivePolls.id == PR.poll_id)
        .where(LivePolls.event_id == ctx.event.id, PR.voter_key.in_(list(keys)), *_between(PR.created_at, start, end))
        .group_by(PR.voter_key)
    )
    return {keys[k]: int(n) for k, n in rows.all() if n}


async def _questions(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.live.models import LiveQuestions as Q
    from app.api.live.models import QuestionStatus

    keys = _keys(regs)
    ids = {g.id for g in regs}
    start, end = window(ctx, task)
    rows = await session.execute(
        select(Q.id, Q.registration_id, Q.author_key).where(
            Q.event_id == ctx.event.id, Q.status != QuestionStatus.rejected,
            or_(Q.registration_id.in_(list(ids)), Q.author_key.in_(list(keys) or [""])), *_between(Q.created_at, start, end),
        )
    )
    seen: dict = {}
    for qid, reg_id, key in rows.all():
        who = reg_id if reg_id in ids else keys.get(key)
        if who is not None:
            seen.setdefault(who, set()).add(qid)
    return {r: len(q) for r, q in seen.items()}


async def _feedback(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.live.models import FeedbackResponses, FeedbackSurveys

    start, end = window(ctx, task)
    rows = await session.execute(
        select(FeedbackResponses.registration_id).join(FeedbackSurveys, FeedbackSurveys.id == FeedbackResponses.survey_id)
        .where(FeedbackSurveys.event_id == ctx.event.id, FeedbackResponses.registration_id.in_([g.id for g in regs]),
               *_between(FeedbackResponses.submitted_at, start, end))
    )
    return {r: 1 for (r,) in rows.all()}


async def _poster_made(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.guest_posters.models import GuestPosters as G

    start, end = window(ctx, task)
    rows = await session.execute(
        select(G.registration_id).where(G.event_id == ctx.event.id, G.registration_id.in_([g.id for g in regs]), G.made_at.isnot(None), *_between(G.made_at, start, end))
    )
    return {r: 1 for (r,) in rows.all()}


async def _poster_link(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.guest_posters.models import GuestPosters as G
    from app.api.links.models import TrackingClicks as C

    start, end = window(ctx, task)
    links = dict((await session.execute(
        select(G.tracking_link_id, G.registration_id).where(G.event_id == ctx.event.id, G.registration_id.in_([g.id for g in regs]), G.tracking_link_id.isnot(None))
    )).all())
    if not links:
        return {}
    if (task.config or {}).get("metric") == "registrations":
        rows = await session.execute(
            select(R.tracking_link_id, R.id).where(R.event_id == ctx.event.id, R.is_deleted == False, R.tracking_link_id.in_(list(links)), *_between(R.created_at, start, end))  # noqa: E712
        )
        out: dict = {}
        for link, reg_id in rows.all():
            owner = links[link]
            if reg_id != owner:
                out[owner] = out.get(owner, 0) + 1
        return out
    rows = await session.execute(
        select(C.link_id, func.count(distinct(C.visitor))).where(C.link_id.in_(list(links)), C.visitor.isnot(None), *_between(C.created_at, start, end)).group_by(C.link_id)
    )
    return {links[link]: int(n) for link, n in rows.all() if n}


async def _follow(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.clubs.models import ClubUsersLink as F

    if not ctx.event.club_id or not open_now(ctx, task):
        return {}
    uids = [g.user_id for g in regs if g.user_id is not None]
    rows = await session.execute(
        select(F.user_id, func.count()).where(F.club_id == ctx.event.club_id, F.user_id.in_(uids), F.is_following == True, F.is_deleted == False).group_by(F.user_id)  # noqa: E712
    )
    return _by_user(rows.all(), regs)


async def _profile(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.profiles.models import ProfileLinks, ProfileProjects
    from app.api.users.models import UserProfiles as UP

    if not open_now(ctx, task):
        return {}
    uids = [g.user_id for g in regs if g.user_id is not None]
    if not uids:
        return {}
    # The photo column's type can't be part of a cached statement: asked in SQL text (profiles.stats).
    has_photo = literal_column("user_profiles.profile_pic IS NOT NULL", Boolean)
    rows = await session.execute(
        select(UP.user_id, func.count()).where(
            UP.user_id.in_(uids), UP.is_deleted == False, UP.headline.isnot(None), UP.headline != "", has_photo,  # noqa: E712
            select(ProfileLinks.id).where(ProfileLinks.user_id == UP.user_id).exists(),
            select(ProfileProjects.id).where(ProfileProjects.user_id == UP.user_id).exists(),
        ).group_by(UP.user_id)
    )
    return _by_user(rows.all(), regs)


async def _station(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    from app.api.quests.models import QuestStationVisits as V

    if task.door_id is None:
        return {}
    start, end = window(ctx, task)
    rows = await session.execute(
        select(V.registration_id).where(V.door_id == task.door_id, V.registration_id.in_([g.id for g in regs]), *_between(V.visited_at, start, end))
    )
    return {r: 1 for (r,) in rows.all()}


RULES = {
    K.CHECKED_IN: _checked_in,
    K.NETWORK: _network,
    K.QUIZ_PLAYED: _quiz,
    K.QUIZ_SCORE: _quiz,
    K.QUIZ_TOP: _quiz,
    K.POLLS: _polls,
    K.QA_QUESTION: _questions,
    K.FEEDBACK: _feedback,
    K.POSTER_MADE: _poster_made,
    K.POSTER_LINK: _poster_link,
    K.FOLLOW_CLUB: _follow,
    K.PROFILE_COMPLETE: _profile,
    K.STATION: _station,
}


async def count(session, ctx: Ctx, task, regs: list[Guest]) -> dict:
    rule = RULES.get(task.kind)
    if rule is None or not regs:
        return {}
    return await rule(session, ctx, task, regs)


__all__ = ["Ctx", "Guest", "count", "window", "open_now", "RULES"]
