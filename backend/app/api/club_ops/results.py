"""An event's results: who came 1st, 2nd and 3rd.

A guest's placing is their registration's `position` (with `is_won`): what
the certificate's "placing" prints and what the badge rules count (events
won, runner-up, podium). Without a competition the club places guests one by
one (PUT /events/registration/{event}/winners/{registration}).

With a competition switched on, places are a team's within its track: 1st,
2nd and 3rd in each track, each kept in competition_placings and given to
every member of the team -- their registrations carry it, so each member's
certificate prints it and each member's badges count it. Taking a place
away, or moving it, changes the members' placings with it.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.api.club_ops.models import CompetitionPlacings
from app.api.competitions.models import CompetitionTeamMembers, CompetitionTeams, Competitions, CompetitionTracks
from app.api.events.models import EventRegistrationsLink as Reg
from app.api.events.models import Events
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

PLACES = (1, 2, 3)
ALL_TEAMS = "All teams"
NO_TRACK = "No track"


async def _competition(session, event_id: int) -> Optional[Competitions]:
    return await session.scalar(select(Competitions).where(Competitions.event_id == event_id))


async def results(session, event: Events) -> dict:
    comp = await _competition(session, event.id)
    has_competition = bool(comp is not None and comp.enabled)
    tracks_out: list[dict] = []
    team_of: dict = {}  # registration id -> (team, track)
    if has_competition:
        tracks = list((await session.scalars(
            select(CompetitionTracks).where(CompetitionTracks.competition_id == comp.id).order_by(CompetitionTracks.sort_order, CompetitionTracks.id)
        )).all())
        teams = list((await session.scalars(
            select(CompetitionTeams).where(CompetitionTeams.competition_id == comp.id).order_by(CompetitionTeams.name, CompetitionTeams.id)
        )).all())
        placed = {p.team_id: p.position for p in (await session.scalars(
            select(CompetitionPlacings).where(CompetitionPlacings.competition_id == comp.id)
        )).all()}
        members: dict = {}
        for team_id, reg_id, name in (await session.execute(
            select(CompetitionTeamMembers.team_id, Reg.id, Reg.full_name)
            .join(Reg, Reg.id == CompetitionTeamMembers.registration_id)
            .where(CompetitionTeamMembers.competition_id == comp.id, Reg.is_deleted == False)  # noqa: E712
            .order_by(Reg.full_name)
        )).all():
            members.setdefault(team_id, []).append({"registration_id": reg_id, "full_name": name or ""})
        names = {t.id: t.name for t in tracks}

        def team_row(t: CompetitionTeams) -> dict:
            return {"id": t.id, "name": t.name, "position": placed.get(t.id), "members": members.get(t.id, [])}

        for t in teams:
            for m in members.get(t.id, []):
                team_of[m["registration_id"]] = (t, names.get(t.track_id) if t.track_id in names else None)
        if tracks:
            for tr in tracks:
                tracks_out.append({"id": tr.id, "name": tr.name, "teams": [team_row(t) for t in teams if t.track_id == tr.id]})
            loose = [team_row(t) for t in teams if t.track_id not in names]
            if loose:
                tracks_out.append({"id": None, "name": NO_TRACK, "teams": loose})
        else:
            tracks_out.append({"id": None, "name": ALL_TEAMS, "teams": [team_row(t) for t in teams]})

    winners = []
    for r in (await session.scalars(
        select(Reg).where(Reg.event_id == event.id, Reg.is_deleted == False, Reg.is_won == True)  # noqa: E712
        .order_by(Reg.position.asc().nulls_last(), Reg.full_name.asc())
    )).all():
        team, track_name = team_of.get(r.id, (None, None))
        winners.append({
            "registration_id": r.id, "ticket_id": r.ticket_id or "", "full_name": r.full_name or "", "email": r.email,
            "position": r.position, "team_id": team.id if team is not None else None,
            "team_name": team.name if team is not None else None,
            "track_id": team.track_id if team is not None else None, "track_name": track_name,
        })
    return {"event_id": event.id, "has_competition": has_competition, "tracks": tracks_out, "winners": winners}


async def place_team(session, event: Events, team_id: int, position: Optional[int], *, actor_id: Optional[int]) -> dict:
    """A team's place in its track (None takes it away), and its members'."""
    if position is not None and position not in PLACES:
        raise CustomHTTPException(422, message="Pick 1st, 2nd or 3rd.", error_code="INVALID_PLACE")
    comp = await _competition(session, event.id)
    team = await session.scalar(
        select(CompetitionTeams).where(CompetitionTeams.id == team_id, CompetitionTeams.competition_id == comp.id)
    ) if comp is not None else None
    if team is None:
        raise CustomHTTPException(404, message="Team not found", error_code="TEAM_NOT_FOUND")
    row = await session.scalar(select(CompetitionPlacings).where(CompetitionPlacings.team_id == team.id).with_for_update())
    if position is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(CompetitionPlacings(competition_id=comp.id, team_id=team.id, track_id=team.track_id, position=position, set_by=actor_id))
    else:
        row.position, row.track_id, row.set_by = position, team.track_id, actor_id
    regs = list((await session.scalars(
        select(Reg).join(CompetitionTeamMembers, CompetitionTeamMembers.registration_id == Reg.id)
        .where(CompetitionTeamMembers.team_id == team.id, Reg.event_id == event.id, Reg.is_deleted == False)  # noqa: E712
        .with_for_update(of=Reg)
    )).all())
    for r in regs:
        r.position = position
        r.is_won = position is not None
    await session.commit()
    # The placing badges are earned once a position exists; a badge check
    # that fails must not fail the placing (as for one guest's placing).
    if position is not None:
        from app.api.badges.service import check_and_award_user_badges

        for user_id in sorted({r.user_id for r in regs if r.user_id}):
            try:
                await check_and_award_user_badges(session, user_id=user_id)
            except Exception:  # noqa: BLE001
                logger.exception("badge check after a team placing failed for user %s", user_id)
                await session.rollback()
    return await results(session, event)
