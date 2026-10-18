"""The competition, as the organiser runs it and as a team lives it."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from statistics import mean, pstdev

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
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


async def find(session: AsyncSession, event: Events) -> Competitions | None:
    """The event's competition, if anyone has made one. Reads only: the
    public board and a guest's side never make one."""
    return await session.scalar(select(Competitions).where(Competitions.event_id == event.id))


async def get_or_create(session: AsyncSession, event: Events) -> Competitions:
    """The organiser's first look makes it. Two first looks at once make one:
    the insert yields to the unique event_id (ON CONFLICT DO NOTHING)."""
    c = await find(session, event)
    if c is None:
        await session.execute(
            pg_insert(Competitions.__table__)
            .values(event_id=event.id, requirements=list(DEFAULT_REQUIREMENTS), rubric=list(DEFAULT_RUBRIC))
            .on_conflict_do_nothing(index_elements=["event_id"])
        )
        await session.commit()
        c = await find(session, event)
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
        score_method=c.score_method or "mean",
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


async def _flush_name(session: AsyncSession) -> None:
    """Flush a new or renamed team: a name another team of the competition
    has (in any case) is a 409, whichever request got there first
    (uq_competition_teams_name)."""
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise CustomHTTPException(409, message="A team already has that name")


async def create_team(session: AsyncSession, c: Competitions, event: Events, name: str, track_id: int | None, registration_ids: list[UUID], by: UUID | None = None) -> CompetitionTeams:
    name = name.strip()
    if await session.scalar(select(func.count()).select_from(CompetitionTeams).where(CompetitionTeams.competition_id == c.id, func.lower(CompetitionTeams.name) == name.lower())):
        raise CustomHTTPException(409, message="A team already has that name")
    team = CompetitionTeams(competition_id=c.id, name=name, track_id=await _track_or_none(session, c, track_id), created_by_registration_id=by)
    session.add(team)
    await _flush_name(session)
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
    # The team's row, held to the commit: two joins at once cannot both see
    # the last place free.
    await session.execute(select(CompetitionTeams.id).where(CompetitionTeams.id == team.id).with_for_update())
    n = await session.scalar(select(func.count()).select_from(CompetitionTeamMembers).where(CompetitionTeamMembers.team_id == team.id))
    if n >= c.team_max:
        raise CustomHTTPException(409, message=f"{team.name} is full")
    try:
        async with session.begin_nested():
            session.add(CompetitionTeamMembers(competition_id=c.id, team_id=team.id, registration_id=registration_id))
            await session.flush()
    except IntegrityError:
        # One team per person per competition (uq_competition_members_once):
        # they joined another team meanwhile.
        other = await team_of_registration(session, c, registration_id)
        raise CustomHTTPException(409, message=f"{reg.full_name} is already on {other.name if other else 'a team'}")
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
    await _flush_name(session)
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
    members = await session.scalar(select(func.count()).select_from(CompetitionTeamMembers).where(CompetitionTeamMembers.team_id == team.id)) or 0
    if members < (c.team_min or 1):
        raise CustomHTTPException(409, message=f"A team needs at least {c.team_min} people to submit; {team.name} has {members}.", error_code="TEAM_TOO_SMALL")
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


SCORE_METHODS = ("mean", "zscore")


def team_scores(c: Competitions, scores: list[CompetitionScores]) -> dict:
    """{team id: (score, judges who scored it)}.

    "mean" (the default): the mean of the judges' totals, out of a hundred.
    "zscore": each judge's totals are first put on that judge's own scale
    -- how far above or below their own average, in their own spread -- so a
    hard marker and a generous one count alike; the team's score is the
    mean of those, shown as 50 + 10z (a judge's average team scores 50). A
    judge who scored one team, or gave every team the same, says nothing
    about order and contributes 0."""
    totals: dict = {}
    for s in scores:
        t = total_of(c, s.scores)
        if t is not None:
            totals.setdefault(s.team_id, []).append((s.judge_id, t))
    if (c.score_method or "mean") != "zscore":
        return {team: (round(mean(t for _, t in pairs), 1), len(pairs)) for team, pairs in totals.items()}
    by_judge: dict = {}
    for pairs in totals.values():
        for judge, t in pairs:
            by_judge.setdefault(judge, []).append(t)
    scale = {j: (mean(ts), pstdev(ts)) for j, ts in by_judge.items()}

    def z(judge, t):
        mu, sd = scale[judge]
        return (t - mu) / sd if sd > 0 else 0.0

    return {team: (round(50 + 10 * mean(z(j, t) for j, t in pairs), 1), len(pairs)) for team, pairs in totals.items()}


def _board(c: Competitions, teams: list[CompetitionTeams], scores: list[CompetitionScores], tracks: dict) -> list[dict]:
    """Every team by its score; the unscored at the bottom, unranked. Teams
    on the same score share a rank (1, 2, 2, 4): the board does not pick
    between them by their names."""
    by_team = team_scores(c, scores)
    judges_total = len(c.judges)
    rows = []
    for t in teams:
        score, scored = by_team.get(t.id, (None, 0))
        rows.append(dict(rank=None, team_id=t.id, name=t.name, track=tracks.get(t.track_id), stage=t.stage, score=score, judges_scored=scored, judges_total=judges_total))
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["name"].lower()))
    previous = None
    for i, r in enumerate(rows, start=1):
        if r["score"] is None:
            continue
        r["rank"] = previous["rank"] if previous is not None and previous["score"] == r["score"] else i
        previous = r
    return rows


# ─── Rounds ──────────────────────────────────────────────────────────────


#: The stages at or past each round, which count towards its size.
_REACHED = {"shortlisted": ("shortlisted", "finalist"), "finalist": ("finalist",)}
_FROM = {"shortlisted": "entered", "finalist": "shortlisted"}


async def advance(session: AsyncSession, c: Competitions, to: str, size: int | None) -> list[str]:
    """Bring the round up to `size` teams (the shortlist, or the finalists):
    the best-scored of the stage before move until it has that many. Run it
    twice and the second moves nobody; teams moved by hand count. Only
    scored teams move.

    A tie at the cut -- two teams on the same score for the last place --
    is not settled here: nothing moves, and the 409 names the teams, so the
    organiser moves one by hand (or asks for a size the tie does not split)
    and runs it again. Returns the names moved."""
    # One advance at a time: the competition's row, held to the commit.
    await session.execute(select(Competitions.id).where(Competitions.id == c.id).with_for_update())
    teams = await teams_of(session, c)
    scores = await _scores(session, c)
    board = _board(c, teams, scores, {t.id: t.name for t in c.tracks})
    target = size or (c.shortlist_size if to == "shortlisted" else c.final_size)
    by_id = {t.id: t for t in teams}
    have = sum(1 for t in teams if t.stage in _REACHED[to])
    places = target - have
    candidates = [r for r in board if r["score"] is not None and by_id[r["team_id"]].stage == _FROM[to]]
    if places <= 0 or not candidates:
        return []
    chosen = candidates[:places]
    if len(candidates) > places and candidates[places]["score"] == chosen[-1]["score"]:
        cut = chosen[-1]["score"]
        tied = [r for r in candidates if r["score"] == cut]
        clear = sum(1 for r in candidates if r["score"] > cut)
        names = ", ".join(r["name"] for r in tied)
        left = places - clear
        raise CustomHTTPException(
            409,
            message=f"{names} tie on {cut} for the last {'place' if left == 1 else f'{left} places'}. Move one by hand, or advance {have + clear} or {have + clear + len(tied)}.",
            error_code="TIE_AT_CUT",
            errors={"tied": [dict(team_id=r["team_id"], name=r["name"], score=r["score"]) for r in tied], "places_left": left, "score": cut},
        )
    for r in chosen:
        by_id[r["team_id"]].stage = to
    await session.commit()
    return [r["name"] for r in chosen]


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
    """Book a slot for a team or free it. A team books only a free slot and
    frees only its own; a team holds one slot per mentor.

    Each is one conditional UPDATE, so two teams after the last slot cannot
    both get it: a team's booking is `WHERE team_id IS NULL`, its freeing
    `WHERE team_id = <its own>`, and uq_competition_slots_team refuses a
    second slot with one mentor whoever books it."""
    t = CompetitionSlots.__table__
    stmt = update(t).where(t.c.id == slot.id).values(team_id=team_id).returning(t.c.id)
    if by_team is not None:
        if team_id is not None:
            if slot.starts_at <= _now():
                raise CustomHTTPException(409, message="That slot has passed")
            held = await session.scalar(select(func.count()).select_from(CompetitionSlots).where(CompetitionSlots.mentor_id == slot.mentor_id, CompetitionSlots.team_id == by_team.id))
            if held:
                raise CustomHTTPException(409, message="Your team already has a slot with this mentor")
            stmt = stmt.where(t.c.team_id.is_(None))
        else:
            stmt = stmt.where(t.c.team_id == by_team.id)
    if team_id is not None:
        await team_for(session, c, team_id)
    try:
        async with session.begin_nested():
            done = (await session.execute(stmt)).first()
    except IntegrityError:
        raise CustomHTTPException(409, message="That team already has a slot with this mentor")
    if done is None:
        if by_team is not None and team_id is None:
            raise CustomHTTPException(403, message="That is not your team's slot")
        raise CustomHTTPException(409, message="That slot is taken")
    await session.commit()
    await session.refresh(slot)
    return slot


async def delete_slot(session: AsyncSession, slot: CompetitionSlots) -> None:
    await session.delete(slot)
    await session.commit()


# ─── Reading it all ──────────────────────────────────────────────────────


async def _team_rows(session: AsyncSession, c: Competitions, event: Events, teams: list[CompetitionTeams], scores: list[CompetitionScores], tracks: dict, *, with_email: bool) -> list[dict]:
    regs = await _registrations(session, event.id, {m.registration_id for t in teams for m in t.members})
    by_team = team_scores(c, scores)
    out = []
    for t in teams:
        score, scored = by_team.get(t.id, (None, 0))
        members = [dict(registration_id=m.registration_id, name=regs[m.registration_id].full_name if m.registration_id in regs else "Guest", email=regs[m.registration_id].email if with_email and m.registration_id in regs else None) for m in t.members]
        out.append(dict(
            id=t.id, name=t.name, track_id=t.track_id, track=tracks.get(t.track_id), stage=t.stage, members=members, open_spots=max(0, c.team_max - len(members)),
            submission=_submission_out(c, t), score=score, judges_scored=scored, created_at=t.created_at,
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
        return dict(event_id=event.id, enabled=c.enabled, public=False, rows=[])
    teams = await teams_of(session, c)
    board = _board(c, teams, await _scores(session, c), {t.id: t.name for t in c.tracks})
    return dict(event_id=event.id, enabled=True, public=True, rows=board)


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
