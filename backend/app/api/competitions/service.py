"""The competition, as the organiser runs it and as a team lives it."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.competitions.models import (
    DEFAULT_REQUIREMENTS,
    DEFAULT_RUBRIC,
    CompetitionJudges,
    CompetitionMentors,
    Competitions,
    CompetitionScores,
    CompetitionSeekers,
    CompetitionSlots,
    CompetitionSubmissions,
    CompetitionTeamMembers,
    CompetitionTeams,
    CompetitionTracks,
    TeamStage,
)
from app.api.events.models import EventRegistrationsLink as Reg, Events
from app.response import CustomHTTPException


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── The competition itself ──────────────────────────────────────────────


async def get_or_create(session: AsyncSession, event: Events) -> Competitions:
    c = await session.scalar(select(Competitions).where(Competitions.event_id == event.id))
    if c is None:
        c = Competitions(event_id=event.id, requirements=list(DEFAULT_REQUIREMENTS), rubric=list(DEFAULT_RUBRIC))
        session.add(c)
        await session.commit()
        await session.refresh(c)
    return c


def submissions_open(c: Competitions, now: datetime | None = None) -> bool:
    now = now or _now()
    if c.submissions_closed_at is not None:
        return False
    return c.submissions_close_at is None or c.submissions_close_at > now


def _settings(c: Competitions) -> dict:
    return dict(
        enabled=c.enabled, team_min=c.team_min, team_max=c.team_max, teams_locked=c.teams_locked,
        submissions_close_at=c.submissions_close_at, submissions_closed_at=c.submissions_closed_at, submissions_open=submissions_open(c),
        requirements=c.requirements or [], rubric=c.rubric or [], leaderboard_public=c.leaderboard_public,
        shortlist_size=c.shortlist_size, final_size=c.final_size, slot_minutes=c.slot_minutes,
    )


async def patch_settings(session: AsyncSession, c: Competitions, data: dict) -> Competitions:
    """Checked whole before anything is set, so a refused change leaves nothing behind."""
    if data.get("requirements") is not None:
        keys = [r["key"] for r in data["requirements"]]
        if len(keys) != len(set(keys)):
            raise CustomHTTPException(422, message="Each requirement needs its own key")
    if data.get("rubric") is not None:
        keys = [r["key"] for r in data["rubric"]]
        if len(keys) != len(set(keys)):
            raise CustomHTTPException(422, message="Each criterion needs its own key")
    team_min = data.get("team_min") if data.get("team_min") is not None else c.team_min
    team_max = data.get("team_max") if data.get("team_max") is not None else c.team_max
    if team_min > team_max:
        raise CustomHTTPException(422, message="The smallest team cannot be bigger than the largest")
    for k, v in data.items():
        if v is not None or k == "submissions_close_at":
            setattr(c, k, v)
    await session.commit()
    await session.refresh(c)
    return c


# ─── Registrations ───────────────────────────────────────────────────────


async def registration_of_user(session: AsyncSession, event: Events, user_id: int) -> Reg:
    r = await session.scalar(select(Reg).where(Reg.event_id == event.id, Reg.user_id == user_id, Reg.is_deleted == False))  # noqa: E712
    if r is None:
        raise CustomHTTPException(403, message="Register for the event first")
    return r


async def _registrations(session: AsyncSession, event_id: int, ids: set) -> dict:
    if not ids:
        return {}
    rows = await session.scalars(select(Reg).where(Reg.event_id == event_id, Reg.id.in_(list(ids))))
    return {r.id: r for r in rows.all()}


# ─── Tracks ──────────────────────────────────────────────────────────────


async def add_track(session: AsyncSession, c: Competitions, data: dict) -> CompetitionTracks:
    last = await session.scalar(select(func.max(CompetitionTracks.sort_order)).where(CompetitionTracks.competition_id == c.id))
    t = CompetitionTracks(competition_id=c.id, sort_order=(last if last is not None else -1) + 1, **data)
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


async def track_for(session: AsyncSession, c: Competitions, track_id: int) -> CompetitionTracks:
    t = await session.scalar(select(CompetitionTracks).where(CompetitionTracks.id == track_id, CompetitionTracks.competition_id == c.id))
    if t is None:
        raise CustomHTTPException(404, message="Track not found")
    return t


async def patch_track(session: AsyncSession, t: CompetitionTracks, data: dict) -> CompetitionTracks:
    for k, v in data.items():
        if v is not None or k in ("description", "sponsor", "prize"):
            setattr(t, k, v)
    await session.commit()
    await session.refresh(t)
    return t


async def delete_track(session: AsyncSession, t: CompetitionTracks) -> None:
    await session.delete(t)
    await session.commit()


# ─── Teams ───────────────────────────────────────────────────────────────


async def teams_of(session: AsyncSession, c: Competitions) -> list[CompetitionTeams]:
    rows = await session.scalars(select(CompetitionTeams).where(CompetitionTeams.competition_id == c.id).order_by(CompetitionTeams.created_at, CompetitionTeams.id))
    return list(rows.unique().all())


async def team_for(session: AsyncSession, c: Competitions, team_id: int) -> CompetitionTeams:
    t = await session.scalar(select(CompetitionTeams).where(CompetitionTeams.id == team_id, CompetitionTeams.competition_id == c.id))
    if t is None:
        raise CustomHTTPException(404, message="Team not found")
    return t


async def team_of_registration(session: AsyncSession, c: Competitions, registration_id: UUID) -> CompetitionTeams | None:
    return await session.scalar(
        select(CompetitionTeams).join(CompetitionTeamMembers, CompetitionTeamMembers.team_id == CompetitionTeams.id)
        .where(CompetitionTeams.competition_id == c.id, CompetitionTeamMembers.registration_id == registration_id)
    )


async def _track_or_none(session: AsyncSession, c: Competitions, track_id: int | None) -> int | None:
    if track_id is None:
        return None
    return (await track_for(session, c, track_id)).id


async def create_team(session: AsyncSession, c: Competitions, event: Events, name: str, track_id: int | None, registration_ids: list[UUID], by: UUID | None = None) -> CompetitionTeams:
    name = name.strip()
    if await session.scalar(select(func.count()).select_from(CompetitionTeams).where(CompetitionTeams.competition_id == c.id, func.lower(CompetitionTeams.name) == name.lower())):
        raise CustomHTTPException(409, message="A team already has that name")
    team = CompetitionTeams(competition_id=c.id, name=name, track_id=await _track_or_none(session, c, track_id), created_by_registration_id=by)
    session.add(team)
    await session.flush()
    for rid in registration_ids:
        await _add_member(session, c, event, team, rid)
    await session.commit()
    await session.refresh(team)
    return team


async def _add_member(session: AsyncSession, c: Competitions, event: Events, team: CompetitionTeams, registration_id: UUID) -> None:
    reg = await session.scalar(select(Reg).where(Reg.id == registration_id, Reg.event_id == event.id, Reg.is_deleted == False))  # noqa: E712
    if reg is None:
        raise CustomHTTPException(404, message="No such registration on this event")
    existing = await team_of_registration(session, c, registration_id)
    if existing is not None:
        raise CustomHTTPException(409, message=f"{reg.full_name} is already on {existing.name}")
    n = await session.scalar(select(func.count()).select_from(CompetitionTeamMembers).where(CompetitionTeamMembers.team_id == team.id))
    if n >= c.team_max:
        raise CustomHTTPException(409, message=f"{team.name} is full")
    session.add(CompetitionTeamMembers(team_id=team.id, registration_id=registration_id))
    await session.flush()
    seeker = await session.scalar(select(CompetitionSeekers).where(CompetitionSeekers.competition_id == c.id, CompetitionSeekers.registration_id == registration_id))
    if seeker is not None:
        await session.delete(seeker)


async def add_member(session: AsyncSession, c: Competitions, event: Events, team: CompetitionTeams, registration_id: UUID) -> CompetitionTeams:
    await _add_member(session, c, event, team, registration_id)
    await session.commit()
    await session.refresh(team)
    return team


async def remove_member(session: AsyncSession, team: CompetitionTeams, registration_id: UUID) -> CompetitionTeams:
    m = await session.scalar(select(CompetitionTeamMembers).where(CompetitionTeamMembers.team_id == team.id, CompetitionTeamMembers.registration_id == registration_id))
    if m is None:
        raise CustomHTTPException(404, message="Not on this team")
    await session.delete(m)
    await session.commit()
    await session.refresh(team)
    return team


async def patch_team(session: AsyncSession, c: Competitions, team: CompetitionTeams, data: dict) -> CompetitionTeams:
    if data.get("name"):
        team.name = data["name"].strip()
    if "track_id" in data:
        team.track_id = await _track_or_none(session, c, data["track_id"])
    if data.get("stage"):
        team.stage = TeamStage(data["stage"]).value
    await session.commit()
    await session.refresh(team)
    return team


async def delete_team(session: AsyncSession, team: CompetitionTeams) -> None:
    await session.delete(team)
    await session.commit()


# ─── Seekers ─────────────────────────────────────────────────────────────


async def set_seeking(session: AsyncSession, c: Competitions, registration_id: UUID, on: bool, skills: list[str], note: str | None) -> CompetitionSeekers | None:
    s = await session.scalar(select(CompetitionSeekers).where(CompetitionSeekers.competition_id == c.id, CompetitionSeekers.registration_id == registration_id))
    if not on:
        if s is not None:
            await session.delete(s)
            await session.commit()
        return None
    if await team_of_registration(session, c, registration_id) is not None:
        raise CustomHTTPException(409, message="You are already on a team")
    if s is None:
        s = CompetitionSeekers(competition_id=c.id, registration_id=registration_id)
        session.add(s)
    s.skills = skills
    s.note = (note or "").strip() or None
    await session.commit()
    await session.refresh(s)
    return s


# ─── Submissions ─────────────────────────────────────────────────────────


def _submission_out(c: Competitions, team: CompetitionTeams) -> dict:
    sub = team.submission
    fields = dict(sub.fields) if sub is not None and sub.fields else {}
    required = [r["key"] for r in (c.requirements or []) if r.get("required")]
    missing = [k for k in required if not fields.get(k)]
    status = "complete" if required and not missing else ("in_progress" if fields else "nothing")
    if not required and fields:
        status = "complete"
    return dict(fields=fields, status=status, missing=missing, completed_at=sub.completed_at if sub else None, updated_at=sub.updated_at if sub else None)


async def put_submission(session: AsyncSession, c: Competitions, team: CompetitionTeams, fields: dict) -> CompetitionTeams:
    if not submissions_open(c):
        raise CustomHTTPException(409, message="Submissions are closed")
    known = {r["key"] for r in (c.requirements or [])}
    unknown = [k for k in fields if k not in known]
    if unknown:
        raise CustomHTTPException(422, message=f"Not something teams submit here: {', '.join(unknown)}")
    sub = team.submission
    if sub is None:
        sub = CompetitionSubmissions(team_id=team.id, fields={})
        session.add(sub)
    sub.fields = fields
    required = [r["key"] for r in (c.requirements or []) if r.get("required")]
    if all(fields.get(k) for k in required) and sub.completed_at is None:
        sub.completed_at = _now()
    await session.commit()
    await session.refresh(team)
    return team


async def close_submissions(session: AsyncSession, c: Competitions, closed: bool) -> Competitions:
    c.submissions_closed_at = _now() if closed else None
    if not closed and c.submissions_close_at is not None and c.submissions_close_at <= _now():
        c.submissions_close_at = None
    await session.commit()
    await session.refresh(c)
    return c


# ─── Judging ─────────────────────────────────────────────────────────────


async def add_judge(session: AsyncSession, c: Competitions, data: dict) -> CompetitionJudges:
    last = await session.scalar(select(func.max(CompetitionJudges.sort_order)).where(CompetitionJudges.competition_id == c.id))
    j = CompetitionJudges(competition_id=c.id, sort_order=(last if last is not None else -1) + 1, **data)
    session.add(j)
    await session.commit()
    await session.refresh(j)
    return j


async def judge_for(session: AsyncSession, c: Competitions, judge_id: int) -> CompetitionJudges:
    j = await session.scalar(select(CompetitionJudges).where(CompetitionJudges.id == judge_id, CompetitionJudges.competition_id == c.id))
    if j is None:
        raise CustomHTTPException(404, message="Judge not found")
    return j


async def patch_judge(session: AsyncSession, j: CompetitionJudges, data: dict) -> CompetitionJudges:
    for k, v in data.items():
        if v is not None or k == "email":
            setattr(j, k, v)
    await session.commit()
    await session.refresh(j)
    return j


async def delete_judge(session: AsyncSession, j: CompetitionJudges) -> None:
    await session.delete(j)
    await session.commit()


def total_of(c: Competitions, scores: dict) -> float | None:
    """A judge's scores on the rubric, weighted, out of a hundred. None until every criterion is scored."""
    rubric = c.rubric or []
    if not rubric or not scores:
        return None
    weight = sum(r["weight"] for r in rubric)
    if weight == 0:
        return None
    if any(r["key"] not in scores for r in rubric):
        return None
    return round(sum(r["weight"] * scores[r["key"]] / 10 for r in rubric) / weight * 100, 1)


async def put_score(session: AsyncSession, c: Competitions, judge: CompetitionJudges, team: CompetitionTeams, scores: dict, note: str | None) -> CompetitionScores:
    keys = {r["key"] for r in (c.rubric or [])}
    unknown = [k for k in scores if k not in keys]
    if unknown:
        raise CustomHTTPException(422, message=f"Not on the rubric: {', '.join(unknown)}")
    s = await session.scalar(select(CompetitionScores).where(CompetitionScores.judge_id == judge.id, CompetitionScores.team_id == team.id))
    if s is None:
        s = CompetitionScores(judge_id=judge.id, team_id=team.id)
        session.add(s)
    s.scores = scores
    s.note = (note or "").strip() or None
    await session.commit()
    await session.refresh(s)
    return s


async def _scores(session: AsyncSession, c: Competitions) -> list[CompetitionScores]:
    rows = await session.scalars(select(CompetitionScores).join(CompetitionJudges, CompetitionJudges.id == CompetitionScores.judge_id).where(CompetitionJudges.competition_id == c.id))
    return list(rows.all())


def _board(c: Competitions, teams: list[CompetitionTeams], scores: list[CompetitionScores], tracks: dict) -> list[dict]:
    """Every team by its mean judge total; the unscored at the bottom, unranked."""
    by_team: dict = {}
    for s in scores:
        t = total_of(c, s.scores)
        if t is not None:
            by_team.setdefault(s.team_id, []).append(t)
    judges_total = len(c.judges)
    rows = []
    for t in teams:
        totals = by_team.get(t.id, [])
        rows.append(dict(rank=None, team_id=t.id, name=t.name, track=tracks.get(t.track_id), stage=t.stage, score=round(sum(totals) / len(totals), 1) if totals else None, judges_scored=len(totals), judges_total=judges_total))
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["name"].lower()))
    rank = 0
    for r in rows:
        if r["score"] is not None:
            rank += 1
            r["rank"] = rank
    return rows


# ─── Rounds ──────────────────────────────────────────────────────────────


async def advance(session: AsyncSession, c: Competitions, to: str, size: int | None) -> None:
    """Shortlist the top N of the entered by score; make finalists of the top M of the shortlist. Only scored teams move."""
    teams = await teams_of(session, c)
    scores = await _scores(session, c)
    tracks = {t.id: t.name for t in c.tracks}
    board = _board(c, teams, scores, tracks)
    pool = {"shortlisted": ("entered", size or c.shortlist_size), "finalist": ("shortlisted", size or c.final_size)}[to]
    from_stage, n = pool
    by_id = {t.id: t for t in teams}
    moved = 0
    for r in board:
        if moved >= n:
            break
        if r["score"] is None:
            break
        t = by_id[r["team_id"]]
        if t.stage == from_stage:
            t.stage = to
            moved += 1
    await session.commit()


# ─── Office hours ────────────────────────────────────────────────────────


async def add_mentor(session: AsyncSession, c: Competitions, data: dict) -> CompetitionMentors:
    last = await session.scalar(select(func.max(CompetitionMentors.sort_order)).where(CompetitionMentors.competition_id == c.id))
    m = CompetitionMentors(competition_id=c.id, sort_order=(last if last is not None else -1) + 1, **data)
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return m


async def mentor_for(session: AsyncSession, c: Competitions, mentor_id: int) -> CompetitionMentors:
    m = await session.scalar(select(CompetitionMentors).where(CompetitionMentors.id == mentor_id, CompetitionMentors.competition_id == c.id))
    if m is None:
        raise CustomHTTPException(404, message="Mentor not found")
    return m


async def patch_mentor(session: AsyncSession, m: CompetitionMentors, data: dict) -> CompetitionMentors:
    for k, v in data.items():
        if v is not None or k == "topic":
            setattr(m, k, v)
    await session.commit()
    await session.refresh(m)
    return m


async def delete_mentor(session: AsyncSession, m: CompetitionMentors) -> None:
    await session.delete(m)
    await session.commit()


async def make_slots(session: AsyncSession, c: Competitions, m: CompetitionMentors, starts_at: datetime, ends_at: datetime) -> CompetitionMentors:
    if ends_at <= starts_at:
        raise CustomHTTPException(422, message="The window ends before it starts")
    if ends_at - starts_at > timedelta(hours=12):
        raise CustomHTTPException(422, message="Make a window of twelve hours at most")
    have = {s.starts_at for s in m.slots}
    at = starts_at
    step = timedelta(minutes=c.slot_minutes)
    while at + step <= ends_at:
        if at not in have:
            session.add(CompetitionSlots(mentor_id=m.id, starts_at=at, minutes=c.slot_minutes))
        at += step
    await session.commit()
    await session.refresh(m)
    return m


async def slot_for(session: AsyncSession, c: Competitions, slot_id: int) -> CompetitionSlots:
    s = await session.scalar(select(CompetitionSlots).join(CompetitionMentors, CompetitionMentors.id == CompetitionSlots.mentor_id).where(CompetitionSlots.id == slot_id, CompetitionMentors.competition_id == c.id))
    if s is None:
        raise CustomHTTPException(404, message="Slot not found")
    return s


async def set_slot(session: AsyncSession, c: Competitions, slot: CompetitionSlots, team_id: int | None, *, by_team: CompetitionTeams | None = None) -> CompetitionSlots:
    """Book a slot for a team or free it. A team books only a free slot and frees only its own, one per mentor."""
    if by_team is not None:
        if team_id is not None:
            if slot.team_id is not None:
                raise CustomHTTPException(409, message="That slot is taken")
            if slot.starts_at <= _now():
                raise CustomHTTPException(409, message="That slot has passed")
            held = await session.scalar(select(func.count()).select_from(CompetitionSlots).where(CompetitionSlots.mentor_id == slot.mentor_id, CompetitionSlots.team_id == by_team.id))
            if held:
                raise CustomHTTPException(409, message="Your team already has a slot with this mentor")
        elif slot.team_id != by_team.id:
            raise CustomHTTPException(403, message="That is not your team's slot")
    if team_id is not None:
        await team_for(session, c, team_id)
    slot.team_id = team_id
    await session.commit()
    await session.refresh(slot)
    return slot


async def delete_slot(session: AsyncSession, slot: CompetitionSlots) -> None:
    await session.delete(slot)
    await session.commit()


# ─── Reading it all ──────────────────────────────────────────────────────


async def _team_rows(session: AsyncSession, c: Competitions, event: Events, teams: list[CompetitionTeams], scores: list[CompetitionScores], tracks: dict, *, with_email: bool) -> list[dict]:
    regs = await _registrations(session, event.id, {m.registration_id for t in teams for m in t.members})
    by_team: dict = {}
    for s in scores:
        t = total_of(c, s.scores)
        if t is not None:
            by_team.setdefault(s.team_id, []).append(t)
    out = []
    for t in teams:
        totals = by_team.get(t.id, [])
        members = [dict(registration_id=m.registration_id, name=regs[m.registration_id].full_name if m.registration_id in regs else "Guest", email=regs[m.registration_id].email if with_email and m.registration_id in regs else None) for m in t.members]
        out.append(dict(
            id=t.id, name=t.name, track_id=t.track_id, track=tracks.get(t.track_id), stage=t.stage, members=members, open_spots=max(0, c.team_max - len(members)),
            submission=_submission_out(c, t), score=round(sum(totals) / len(totals), 1) if totals else None, judges_scored=len(totals), created_at=t.created_at,
        ))
    return out


async def _seeker_rows(session: AsyncSession, c: Competitions, event: Events) -> list[dict]:
    seekers = list((await session.scalars(select(CompetitionSeekers).where(CompetitionSeekers.competition_id == c.id).order_by(CompetitionSeekers.created_at))).all())
    regs = await _registrations(session, event.id, {s.registration_id for s in seekers})
    return [dict(registration_id=s.registration_id, name=regs[s.registration_id].full_name if s.registration_id in regs else "Guest", skills=list(s.skills or []), note=s.note) for s in seekers]


def _mentor_rows(c: Competitions, teams: dict) -> list[dict]:
    return [dict(id=m.id, name=m.name, topic=m.topic, sort_order=m.sort_order, slots=[dict(id=s.id, starts_at=s.starts_at, minutes=s.minutes, team_id=s.team_id, team=teams.get(s.team_id)) for s in m.slots]) for m in c.mentors]


async def state(session: AsyncSession, c: Competitions, event: Events) -> dict:
    await session.refresh(c)
    teams = await teams_of(session, c)
    scores = await _scores(session, c)
    tracks = {t.id: t.name for t in c.tracks}
    team_rows = await _team_rows(session, c, event, teams, scores, tracks, with_email=True)
    board = _board(c, teams, scores, tracks)
    team_names = {t.id: t.name for t in teams}
    counts: dict = {}
    for t in teams:
        counts[t.track_id] = counts.get(t.track_id, 0) + 1
    scored_by_judge: dict = {}
    for s in scores:
        if total_of(c, s.scores) is not None:
            scored_by_judge[s.judge_id] = scored_by_judge.get(s.judge_id, 0) + 1
    return dict(
        event_id=event.id, settings=_settings(c),
        figures=dict(teams=len(teams), people=sum(len(t.members) for t in teams), seekers=await session.scalar(select(func.count()).select_from(CompetitionSeekers).where(CompetitionSeekers.competition_id == c.id)) or 0, submitted=sum(1 for r in team_rows if r["submission"]["status"] == "complete"), judges=len(c.judges), scored_pairs=sum(scored_by_judge.values())),
        tracks=[dict(id=t.id, name=t.name, description=t.description, sponsor=t.sponsor, prize=t.prize, sort_order=t.sort_order, teams=counts.get(t.id, 0)) for t in c.tracks],
        teams=team_rows, seekers=await _seeker_rows(session, c, event),
        judges=[dict(id=j.id, name=j.name, email=j.email, sort_order=j.sort_order, scored=scored_by_judge.get(j.id, 0)) for j in c.judges],
        scores=[dict(judge_id=s.judge_id, team_id=s.team_id, scores=dict(s.scores or {}), note=s.note, total=total_of(c, s.scores)) for s in scores],
        board=board,
        rounds=dict(entered=[r for r in board if r["stage"] == "entered"], shortlisted=[r for r in board if r["stage"] == "shortlisted"], finalists=[r for r in board if r["stage"] == "finalist"]),
        mentors=_mentor_rows(c, team_names), generated_at=_now(),
    )


async def public_board(session: AsyncSession, c: Competitions, event: Events) -> dict:
    if not (c.enabled and c.leaderboard_public):
        return dict(event_id=event.id, public=False, rows=[])
    teams = await teams_of(session, c)
    board = _board(c, teams, await _scores(session, c), {t.id: t.name for t in c.tracks})
    return dict(event_id=event.id, public=True, rows=board)


async def my_state(session: AsyncSession, c: Competitions, event: Events, reg: Reg) -> dict:
    await session.refresh(c)
    teams = await teams_of(session, c)
    scores = await _scores(session, c)
    tracks = {t.id: t.name for t in c.tracks}
    mine = await team_of_registration(session, c, reg.id)
    my_row = (await _team_rows(session, c, event, [mine], scores, tracks, with_email=False))[0] if mine is not None else None
    seeking = next((s for s in await _seeker_rows(session, c, event) if s["registration_id"] == reg.id), None)
    counts: dict = {}
    for t in teams:
        counts[t.track_id] = counts.get(t.track_id, 0) + 1
    team_names = {t.id: t.name for t in teams}
    return dict(
        event_id=event.id, enabled=c.enabled, registration_id=reg.id, name=reg.full_name, team_min=c.team_min, team_max=c.team_max, teams_locked=c.teams_locked,
        tracks=[dict(id=t.id, name=t.name, description=t.description, sponsor=t.sponsor, prize=t.prize, sort_order=t.sort_order, teams=counts.get(t.id, 0)) for t in c.tracks],
        requirements=c.requirements or [], submissions_open=submissions_open(c), submissions_close_at=c.submissions_close_at,
        team=my_row,
        open_teams=[dict(id=t.id, name=t.name, track=tracks.get(t.track_id), members=len(t.members), open_spots=c.team_max - len(t.members)) for t in teams if len(t.members) < c.team_max and (mine is None or t.id != mine.id)],
        seeking=seeking, seekers=[s for s in await _seeker_rows(session, c, event) if s["registration_id"] != reg.id],
        mentors=_mentor_rows(c, team_names),
        board=_board(c, teams, scores, tracks) if c.leaderboard_public else None,
    )
