"""The quest routes (feature-register/quests/CONTRACT.md).

- `router`: the organiser's, /events/{event_id}/quests/... (ClubAuth: the
  host, a co-host, an admin; team members by role through club_access).
- `gate_router`: the door console's, /events/{event_id}/gate/quests/...
  (GateAuth: the club, volunteers, door links).
- `public_router`: the guest's, /events/{event_id}/quest/....
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.api.checkins.gate_auth import GateAuth, load_event
from app.api.deliveries import service as deliveries
from app.api.quests import gate, guest, organiser
from app.api.quests.schemas import (
    CodeIn,
    HandoverIn,
    MilestoneIn,
    MilestonePatch,
    NoteIn,
    OrderIn,
    QuestPatch,
    RewardIn,
    RewardPatch,
    SizeIn,
    StationIn,
    TaskIn,
    TaskPatch,
)
from app.core.auth.dependencies import ClubAuth, GuestAuth, OptionalAuth
from app.core.rate_limit import account_or_address, limiter
from app.core.utils.spreadsheet import FormatQuery
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/quests", tags=["quests"])
gate_router = APIRouter(prefix="/events/{event_id}/gate/quests", tags=["quests"])
public_router = APIRouter(prefix="/events/{event_id}/quest", tags=["quests"])


def _by(request: Request, user) -> str:
    from app.api.club_access import service as club_access

    member = club_access.current_member(request)
    if member is not None:
        return (member.full_name or member.email or "A team member")[:120]
    return (getattr(user, "full_name", None) or getattr(user, "email", None) or "The club")[:120]


async def _event(session, user, event_id: int):
    event = await deliveries.event_for_club(session, user, event_id)
    return event


# ─── The organiser ─────────────────────────────────────────────────────


@router.get("", summary="The quest: settings, tasks, milestones, rewards, stations")
async def get_quest(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await organiser.view(session, await _event(session, user, event_id))


@router.patch("", summary="Switch the quest on or off, and its settings")
async def patch_quest(session: SessionDep, user: ClubAuth, event_id: int, body: QuestPatch) -> dict:
    return await organiser.patch(session, await _event(session, user, event_id), body, user)


@router.get("/templates", summary="Every kind of task, with its details")
async def templates(session: SessionDep, user: ClubAuth, event_id: int) -> list[dict]:
    from app.api.quests.kinds import templates as all_templates

    await _event(session, user, event_id)
    return all_templates()


@router.post("/tasks", summary="A new task", status_code=201)
async def create_task(session: SessionDep, user: ClubAuth, event_id: int, body: TaskIn) -> dict:
    return await organiser.create_task(session, await _event(session, user, event_id), body)


@router.put("/tasks/order", summary="The tasks' order")
async def order_tasks(session: SessionDep, user: ClubAuth, event_id: int, body: OrderIn) -> list[dict]:
    return await organiser.order_tasks(session, await _event(session, user, event_id), body.ids)


@router.patch("/tasks/{task_id}", summary="Change a task")
async def update_task(session: SessionDep, user: ClubAuth, event_id: int, task_id: UUID, body: TaskPatch) -> dict:
    return await organiser.update_task(session, await _event(session, user, event_id), task_id, body)


@router.delete("/tasks/{task_id}", summary="Remove a task (kept for the record when guests have it)")
async def delete_task(session: SessionDep, user: ClubAuth, event_id: int, task_id: UUID) -> dict:
    await organiser.delete_task(session, await _event(session, user, event_id), task_id)
    return {"deleted": True}


@router.post("/milestones", summary="A new milestone", status_code=201)
async def create_milestone(session: SessionDep, user: ClubAuth, event_id: int, body: MilestoneIn) -> dict:
    return await organiser.create_milestone(session, await _event(session, user, event_id), body)


@router.patch("/milestones/{milestone_id}", summary="Change a milestone")
async def update_milestone(session: SessionDep, user: ClubAuth, event_id: int, milestone_id: UUID, body: MilestonePatch) -> dict:
    return await organiser.update_milestone(session, await _event(session, user, event_id), milestone_id, body)


@router.delete("/milestones/{milestone_id}", summary="Remove a milestone and its rewards")
async def delete_milestone(session: SessionDep, user: ClubAuth, event_id: int, milestone_id: UUID) -> dict:
    await organiser.delete_milestone(session, await _event(session, user, event_id), milestone_id)
    return {"deleted": True}


@router.post("/milestones/{milestone_id}/rewards", summary="A swag item as a milestone's reward", status_code=201)
async def add_reward(session: SessionDep, user: ClubAuth, event_id: int, milestone_id: UUID, body: RewardIn) -> dict:
    return await organiser.add_reward(session, await _event(session, user, event_id), milestone_id, body)


@router.patch("/rewards/{reward_id}", summary="Change a reward")
async def update_reward(session: SessionDep, user: ClubAuth, event_id: int, reward_id: UUID, body: RewardPatch) -> dict:
    return await organiser.update_reward(session, await _event(session, user, event_id), reward_id, body)


@router.delete("/rewards/{reward_id}", summary="Remove a reward (its held units go back to stock)")
async def delete_reward(session: SessionDep, user: ClubAuth, event_id: int, reward_id: UUID) -> dict:
    await organiser.delete_reward(session, await _event(session, user, event_id), reward_id)
    return {"deleted": True}


@router.post("/rewards/{reward_id}/reoffer", summary="Hold free units for guests it was sold out for")
async def reoffer(request: Request, session: SessionDep, user: ClubAuth, event_id: int, reward_id: UUID) -> dict:
    return await organiser.reoffer(session, await _event(session, user, event_id), reward_id, user, _by(request, user))


@router.post("/stations", summary="A station or swag desk: a door, its link and its task", status_code=201)
async def create_station(session: SessionDep, user: ClubAuth, event_id: int, body: StationIn) -> dict:
    return await organiser.create_station(session, await _event(session, user, event_id), body)


@router.get("/dashboard", summary="The quest live: people per task and milestone, rewards and stock")
async def dashboard(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await organiser.live(session, await _event(session, user, event_id))


@router.get("/leaderboard", summary="The leaderboard, full names (the big screen)")
async def club_leaderboard(session: SessionDep, user: ClubAuth, event_id: int, limit: int = Query(20, ge=1, le=100)) -> dict:
    event = await _event(session, user, event_id)
    quest = await organiser.ensure(session, event)
    return await guest.leaderboard(session, quest, limit=limit, full_names=True)


@router.get("/guests", summary="Guests and what they earned")
async def guests(
    session: SessionDep, user: ClubAuth, event_id: int, q: Optional[str] = Query(None, max_length=100), milestone_id: Optional[UUID] = Query(None),
    task_id: Optional[UUID] = Query(None), claim_status: Optional[str] = Query(None, pattern="^(reserved|waitlisted|sold_out|collected|released)$"),
    sort: str = Query("points", pattern="^(points|name|recent)$"), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> dict:
    return await organiser.guests(session, await _event(session, user, event_id), q=q, milestone_id=milestone_id, task_id=task_id,
                                  claim_status=claim_status, sort=sort, page=page, page_size=page_size)


@router.get("/guests/{registration_id}", summary="One guest's quest, and what was granted or revoked")
async def guest_detail(session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID) -> dict:
    return await organiser.guest(session, await _event(session, user, event_id), registration_id)


@router.post("/guests/{registration_id}/tasks/{task_id}/grant", summary="Mark a task done for a guest (audited)")
async def grant(request: Request, session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID, task_id: UUID, body: NoteIn) -> dict:
    return await organiser.grant(session, await _event(session, user, event_id), registration_id, task_id, user, _by(request, user), body.note)


@router.post("/guests/{registration_id}/tasks/{task_id}/revoke", summary="Take a task back from a guest (audited)")
async def revoke(request: Request, session: SessionDep, user: ClubAuth, event_id: int, registration_id: UUID, task_id: UUID, body: NoteIn) -> dict:
    return await organiser.revoke(session, await _event(session, user, event_id), registration_id, task_id, user, _by(request, user), body.note)


@router.get("/audit", summary="Every grant, revoke, release, re-offer and handover")
async def audit(session: SessionDep, user: ClubAuth, event_id: int, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> dict:
    return await organiser.audit(session, await _event(session, user, event_id), page=page, page_size=page_size)


@router.post("/recompute", summary="Work everyone's progress out again", status_code=202)
async def recompute(session: SessionDep, user: ClubAuth, event_id: int) -> dict:
    return await organiser.recompute(session, await _event(session, user, event_id))


@router.get("/export", summary="Who earned what, as CSV or Excel")
async def export(session: SessionDep, user: ClubAuth, event_id: int, fmt: FormatQuery = "csv"):
    from app.core.utils import spreadsheet

    event = await _event(session, user, event_id)
    if fmt == "xlsx":
        return spreadsheet.xlsx_response(await organiser.export_xlsx(session, event), f"quest-{event.id}.xlsx")
    body = await organiser.export_csv(session, event)
    return Response(content="﻿" + body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="quest-{event.id}.csv"'})


# ─── The door console ──────────────────────────────────────────────────


def _may_look(actor) -> None:
    if not (actor.permissions["check_in"] or actor.permissions["merch"]):
        raise CustomHTTPException(403, message="You may not look guests up at this door.")


@gate_router.get("/{registration_id}", summary="A guest's rewards and station visits, at the desk")
async def gate_guest(request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID) -> dict:
    _may_look(actor)
    return await gate.guest_view(session, request.state.gate_event, registration_id)


@gate_router.post("/claims/{claim_id}/handover", summary="Hand a reward over")
async def handover(request: Request, session: SessionDep, actor: GateAuth, event_id: int, claim_id: UUID, body: HandoverIn) -> dict:
    return await gate.handover(session, request.state.gate_event, claim_id, actor, variant_label=body.variant_label, door_label=body.door)


# ─── The guest ─────────────────────────────────────────────────────────


@public_router.get("", summary="The quest card: tasks, progress, milestones, rewards")
@limiter.limit("60/minute", key_func=account_or_address)
async def card(request: Request, session: SessionDep, event_id: int, user: OptionalAuth = None) -> dict:
    event = await load_event(session, event_id)
    return await guest.card(session, event, user)


@public_router.post("/code", summary="Enter a secret code")
@limiter.limit("20/minute", key_func=account_or_address)
async def code(request: Request, session: SessionDep, event_id: int, body: CodeIn, user: GuestAuth) -> dict:
    event = await load_event(session, event_id)
    return await guest.enter_code(session, event, user, body.code)


@public_router.post("/claims/{claim_id}/size", summary="Choose the size of a reward")
async def size(session: SessionDep, event_id: int, claim_id: UUID, body: SizeIn, user: GuestAuth) -> dict:
    event = await load_event(session, event_id)
    return await guest.choose_size(session, event, user, claim_id, body.variant_label)


@public_router.get("/leaderboard", summary="The leaderboard, when the organiser shows it")
async def leaderboard(session: SessionDep, event_id: int, user: OptionalAuth = None, limit: int = Query(20, ge=1, le=100)) -> dict:
    from app.api.quests import engine

    event = await load_event(session, event_id)
    quest = await engine.quest_of(session, event.id)
    if quest is None or not quest.enabled or not quest.leaderboard:
        raise CustomHTTPException(404, message="This event shows no leaderboard.", error_code="QUEST_LEADERBOARD_OFF")
    reg = await guest.registration_for(session, event.id, user)
    return await guest.leaderboard(session, quest, limit=limit, reg=reg)
