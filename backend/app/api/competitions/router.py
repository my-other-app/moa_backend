"""The competition: the console's side under ClubAuth, a team's side for a
registered guest, and the leaderboard for anyone once it is public."""
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.competitions import service
from app.api.competitions.schemas import (
    AdvanceIn,
    CompetitionState,
    JudgeIn,
    JudgePatch,
    MemberIn,
    MentorIn,
    MentorPatch,
    MyState,
    MyTeamIn,
    MyTeamPatch,
    PublicBoard,
    ScoreIn,
    SeekingIn,
    SettingsPatch,
    SlotPatch,
    SlotsIn,
    SubmissionIn,
    TeamIn,
    TeamPatch,
    TrackIn,
    TrackPatch,
)
from app.api.deliveries import service as deliveries
from app.api.events.models import Events
from app.core.auth.dependencies import ClubAuth, UserAuth
from app.api.subscriptions import gates
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/competition")
public_router = APIRouter()


async def _state(session, event: Events) -> CompetitionState:
    return CompetitionState(**await service.state(session, await service.get_or_create(session, event), event))


async def _public_event(session, event_id: int) -> Events:
    event = await session.scalar(select(Events).options(joinedload(Events.club)).where(Events.id == event_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


# ─── The organiser ───────────────────────────────────────────────────────


@router.get("", summary="The whole competition: settings, tracks, teams, submissions, judges, scores, rounds, the board, office hours")
async def get_state(session: SessionDep, user: ClubAuth, event_id: int) -> CompetitionState:
    return await _state(session, await deliveries.event_for_club(session, user, event_id))


@router.patch("", summary="Turn it on or off; team size, lock, the close, what to submit, the rubric, the board, rounds, slot length")
async def patch_settings(session: SessionDep, user: ClubAuth, event_id: int, body: SettingsPatch) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    if body.enabled and not c.enabled:
        # Turning the competition on is the plan's; turning it off never is.
        await gates.require_feature(session, event.club_id, "teams", actor=user, event=event)
    await service.patch_settings(session, c, body.model_dump(exclude_unset=True))
    return await _state(session, event)


@router.post("/tracks", summary="A new track", status_code=201)
async def add_track(session: SessionDep, user: ClubAuth, event_id: int, body: TrackIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.add_track(session, await service.get_or_create(session, event), body.model_dump())
    return await _state(session, event)


@router.patch("/tracks/{track_id}", summary="Change a track")
async def patch_track(session: SessionDep, user: ClubAuth, event_id: int, track_id: int, body: TrackPatch) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.patch_track(session, await service.track_for(session, c, track_id), body.model_dump(exclude_unset=True))
    return await _state(session, event)


@router.delete("/tracks/{track_id}", summary="Remove a track; its teams keep going without one")
async def delete_track(session: SessionDep, user: ClubAuth, event_id: int, track_id: int) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.delete_track(session, await service.track_for(session, c, track_id))
    return await _state(session, event)


@router.post("/teams", summary="A team made by the organiser, with whoever is named", status_code=201)
async def create_team(session: SessionDep, user: ClubAuth, event_id: int, body: TeamIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "teams", actor=user, event=event)
    c = await service.get_or_create(session, event)
    await service.create_team(session, c, event, body.name, body.track_id, body.registration_ids)
    return await _state(session, event)


@router.patch("/teams/{team_id}", summary="Rename a team, move it to a track, or set its stage by hand")
async def patch_team(session: SessionDep, user: ClubAuth, event_id: int, team_id: int, body: TeamPatch) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    if body.stage is not None:
        # Moving a team through the rounds by hand is rounds, like advancing them.
        await gates.require_feature(session, event.club_id, "rounds", actor=user, event=event)
    c = await service.get_or_create(session, event)
    await service.patch_team(session, c, await service.team_for(session, c, team_id), body.model_dump(exclude_unset=True))
    return await _state(session, event)


@router.delete("/teams/{team_id}", summary="Dissolve a team")
async def delete_team(session: SessionDep, user: ClubAuth, event_id: int, team_id: int) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.delete_team(session, await service.team_for(session, c, team_id))
    return await _state(session, event)


@router.post("/teams/{team_id}/members", summary="Put a registered guest on a team", status_code=201)
async def add_member(session: SessionDep, user: ClubAuth, event_id: int, team_id: int, body: MemberIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.add_member(session, c, event, await service.team_for(session, c, team_id), body.registration_id)
    return await _state(session, event)


@router.delete("/teams/{team_id}/members/{registration_id}", summary="Take someone off a team")
async def remove_member(session: SessionDep, user: ClubAuth, event_id: int, team_id: int, registration_id: UUID) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.remove_member(session, await service.team_for(session, c, team_id), registration_id)
    return await _state(session, event)


@router.post("/submissions/{action}", summary="close · reopen")
async def submissions_action(session: SessionDep, user: ClubAuth, event_id: int, action: str) -> CompetitionState:
    if action not in ("close", "reopen"):
        raise CustomHTTPException(404, message="No such action")
    event = await deliveries.event_for_club(session, user, event_id)
    await service.close_submissions(session, await service.get_or_create(session, event), action == "close")
    return await _state(session, event)


@router.post("/judges", summary="A judge", status_code=201)
async def add_judge(session: SessionDep, user: ClubAuth, event_id: int, body: JudgeIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "judging", actor=user, event=event)
    await service.add_judge(session, await service.get_or_create(session, event), body.model_dump())
    return await _state(session, event)


@router.patch("/judges/{judge_id}", summary="Change a judge")
async def patch_judge(session: SessionDep, user: ClubAuth, event_id: int, judge_id: int, body: JudgePatch) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.patch_judge(session, await service.judge_for(session, c, judge_id), body.model_dump(exclude_unset=True))
    return await _state(session, event)


@router.delete("/judges/{judge_id}", summary="Remove a judge and their scores")
async def delete_judge(session: SessionDep, user: ClubAuth, event_id: int, judge_id: int) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.delete_judge(session, await service.judge_for(session, c, judge_id))
    return await _state(session, event)


@router.put("/judges/{judge_id}/scores/{team_id}", summary="A judge's scores for a team, on the rubric")
async def put_score(session: SessionDep, user: ClubAuth, event_id: int, judge_id: int, team_id: int, body: ScoreIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.put_score(session, c, await service.judge_for(session, c, judge_id), await service.team_for(session, c, team_id), body.scores, body.note)
    return await _state(session, event)


@router.post("/rounds/advance", summary="Bring the shortlist, or the finalists, up to a size: the best-scored move")
async def advance(session: SessionDep, user: ClubAuth, event_id: int, body: AdvanceIn) -> CompetitionState:
    """Idempotent: `size` is how many the round should hold, so a second call
    moves nobody. A tie at the cut is a 409 TIE_AT_CUT naming the teams."""
    event = await deliveries.event_for_club(session, user, event_id)
    await gates.require_feature(session, event.club_id, "rounds", actor=user, event=event)
    await service.advance(session, await service.get_or_create(session, event), body.to, body.size)
    return await _state(session, event)


@router.post("/mentors", summary="A mentor for office hours", status_code=201)
async def add_mentor(session: SessionDep, user: ClubAuth, event_id: int, body: MentorIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    await service.add_mentor(session, await service.get_or_create(session, event), body.model_dump())
    return await _state(session, event)


@router.patch("/mentors/{mentor_id}", summary="Change a mentor")
async def patch_mentor(session: SessionDep, user: ClubAuth, event_id: int, mentor_id: int, body: MentorPatch) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.patch_mentor(session, await service.mentor_for(session, c, mentor_id), body.model_dump(exclude_unset=True))
    return await _state(session, event)


@router.delete("/mentors/{mentor_id}", summary="Remove a mentor and their slots")
async def delete_mentor(session: SessionDep, user: ClubAuth, event_id: int, mentor_id: int) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.delete_mentor(session, await service.mentor_for(session, c, mentor_id))
    return await _state(session, event)


@router.post("/mentors/{mentor_id}/slots", summary="Open slots for a mentor across a window, one every slot length", status_code=201)
async def make_slots(session: SessionDep, user: ClubAuth, event_id: int, mentor_id: int, body: SlotsIn) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.make_slots(session, c, await service.mentor_for(session, c, mentor_id), body.starts_at, body.ends_at)
    return await _state(session, event)


@router.patch("/slots/{slot_id}", summary="Book a slot for a team, or free it")
async def set_slot(session: SessionDep, user: ClubAuth, event_id: int, slot_id: int, body: SlotPatch) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.set_slot(session, c, await service.slot_for(session, c, slot_id), body.team_id)
    return await _state(session, event)


@router.delete("/slots/{slot_id}", summary="Remove a slot")
async def delete_slot(session: SessionDep, user: ClubAuth, event_id: int, slot_id: int) -> CompetitionState:
    event = await deliveries.event_for_club(session, user, event_id)
    c = await service.get_or_create(session, event)
    await service.delete_slot(session, await service.slot_for(session, c, slot_id))
    return await _state(session, event)


# ─── A team, from a ticket ───────────────────────────────────────────────


async def _mine(session, event_id: int, user):
    event = await _public_event(session, event_id)
    # A guest's side reads; only the organiser's first look makes one.
    c = await service.find(session, event)
    if c is None or not c.enabled:
        raise CustomHTTPException(404, message="This event has no competition")
    reg = await service.registration_of_user(session, event, user.id)
    return event, c, reg


async def _my(session, c, event, reg) -> MyState:
    return MyState(**await service.my_state(session, c, event, reg))


def _unlocked(c):
    if c.teams_locked:
        raise CustomHTTPException(409, message="Teams are locked; ask the organisers")


@public_router.get("/events/{event_id}/competition/me", summary="A registered guest's side: the tracks, their team or the open ones, what to submit, the office hours")
async def my_state(session: SessionDep, user: UserAuth, event_id: int) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    return await _my(session, c, event, reg)


@public_router.post("/events/{event_id}/competition/me/team", summary="Make a team and be on it", status_code=201)
async def my_create_team(session: SessionDep, user: UserAuth, event_id: int, body: MyTeamIn) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    _unlocked(c)
    if await service.team_of_registration(session, c, reg.id) is not None:
        raise CustomHTTPException(409, message="You are already on a team")
    await service.create_team(session, c, event, body.name, body.track_id, [reg.id], by=reg.id)
    return await _my(session, c, event, reg)


@public_router.post("/events/{event_id}/competition/me/team/join/{team_id}", summary="Join an open team")
async def my_join(session: SessionDep, user: UserAuth, event_id: int, team_id: int) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    _unlocked(c)
    await service.add_member(session, c, event, await service.team_for(session, c, team_id), reg.id)
    return await _my(session, c, event, reg)


@public_router.post("/events/{event_id}/competition/me/team/leave", summary="Leave your team; an empty team is dissolved")
async def my_leave(session: SessionDep, user: UserAuth, event_id: int) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    _unlocked(c)
    team = await service.team_of_registration(session, c, reg.id)
    if team is None:
        raise CustomHTTPException(404, message="You are not on a team")
    await service.remove_member(session, team, reg.id)
    if not team.members:
        await service.delete_team(session, team)
    return await _my(session, c, event, reg)


@public_router.patch("/events/{event_id}/competition/me/team", summary="Rename your team or pick its track")
async def my_patch_team(session: SessionDep, user: UserAuth, event_id: int, body: MyTeamPatch) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    _unlocked(c)
    team = await service.team_of_registration(session, c, reg.id)
    if team is None:
        raise CustomHTTPException(404, message="You are not on a team")
    await service.patch_team(session, c, team, body.model_dump(exclude_unset=True))
    return await _my(session, c, event, reg)


@public_router.put("/events/{event_id}/competition/me/seeking", summary="Say you are looking for a team, and what you bring; or stop")
async def my_seeking(session: SessionDep, user: UserAuth, event_id: int, body: SeekingIn) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    await service.set_seeking(session, c, reg.id, body.on, body.skills, body.note)
    return await _my(session, c, event, reg)


@public_router.put("/events/{event_id}/competition/me/submission", summary="Your team's submission, link by link, until the close")
async def my_submission(session: SessionDep, user: UserAuth, event_id: int, body: SubmissionIn) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    team = await service.team_of_registration(session, c, reg.id)
    if team is None:
        raise CustomHTTPException(409, message="Join or make a team first")
    await service.put_submission(session, c, team, body.fields)
    return await _my(session, c, event, reg)


@public_router.post("/events/{event_id}/competition/me/slots/{slot_id}", summary="Book a mentor slot for your team")
async def my_book(session: SessionDep, user: UserAuth, event_id: int, slot_id: int) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    team = await service.team_of_registration(session, c, reg.id)
    if team is None:
        raise CustomHTTPException(409, message="Join or make a team first")
    await service.set_slot(session, c, await service.slot_for(session, c, slot_id), team.id, by_team=team)
    return await _my(session, c, event, reg)


@public_router.delete("/events/{event_id}/competition/me/slots/{slot_id}", summary="Free a slot your team booked")
async def my_free(session: SessionDep, user: UserAuth, event_id: int, slot_id: int) -> MyState:
    event, c, reg = await _mine(session, event_id, user)
    team = await service.team_of_registration(session, c, reg.id)
    if team is None:
        raise CustomHTTPException(409, message="Join or make a team first")
    await service.set_slot(session, c, await service.slot_for(session, c, slot_id), None, by_team=team)
    return await _my(session, c, event, reg)


@public_router.get("/events/{event_id}/competition/leaderboard", summary="The leaderboard, once the organisers make it public")
async def leaderboard(session: SessionDep, event_id: int) -> PublicBoard:
    """Reads only: anyone can call it, so it never makes a competition (it
    used to, for every event id anyone asked about)."""
    event = await _public_event(session, event_id)
    c = await service.find(session, event)
    if c is None:
        return PublicBoard(event_id=event.id, enabled=False, public=False, rows=[])
    return PublicBoard(**await service.public_board(session, c, event))
