"""Missing answers' routes (feature-register/missing-answers-contract.md, docs/MISSING_ANSWERS.md).

The club's (ClubAuth; co-hosts as the host; the roles in club_access.policy):
the impact of a form about to be saved, the summary, moving stray answers
(owners and admins), and asks. The person's own (their personal link, or
their own sign-in): reading and answering. The door's: answers filled at
check-in.

One router, in this order: a literal path (`summary`, `asks`, `move`) is
declared before the person's `{ref}`, which would otherwise take it.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.checkins.gate_auth import GateAuth
from app.api.deliveries import service as deliveries
from app.api.missing_answers import asks as asks_mod
from app.api.missing_answers import service
from app.api.missing_answers.schemas import (
    AnswersIn,
    AnswersView,
    AskIn,
    AskOut,
    AskPreviewIn,
    AskPreviewOut,
    AsksOut,
    DoorAnswersOut,
    ImpactIn,
    ImpactOut,
    MoveIn,
    MoveOut,
    SummaryOut,
)
from app.core.auth.dependencies import ClubAuth, OptionalUserAuth
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}", tags=["missing answers"])

NO_STORE = "no-store"


def _ref_key(request: Request) -> str:
    """A personal link belongs to one person: its limit is theirs, not their network's."""
    params = getattr(request, "path_params", None) or {}
    return f"answers:{params.get('event_id', '')}:{str(params.get('ref', '')).lower()}"


def _actor(request: Request, user):
    from app.api.club_ops.access import actor_of

    return actor_of(request, user)


# ─── The club's ──────────────────────────────────────────────────────────


@router.post("/form/impact", summary="What saving these questions would do to the people signed up")
async def form_impact(session: SessionDep, user: ClubAuth, event_id: int, body: ImpactIn) -> ImpactOut:
    """Nothing is saved. 422 with the rule's words when the form breaks one."""
    event = await deliveries.event_for_club(session, user, event_id)
    return ImpactOut.model_validate(await service.impact(session, event, body.additional_details))


@router.get("/answers/summary", summary="Who is missing which answers")
async def answers_summary(session: SessionDep, user: ClubAuth, event_id: int) -> SummaryOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return SummaryOut.model_validate(await service.summary(session, event))


@router.get("/answers/asks", summary="The event's asks, newest first")
async def list_asks(session: SessionDep, user: ClubAuth, event_id: int) -> AsksOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return AsksOut.model_validate(await asks_mod.list_asks(session, event))


@router.post("/answers/move", summary="Move answers kept under an old question to a current one")
async def move_answers(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: MoveIn) -> MoveOut:
    """Owners and admins (club_access.policy). Copied where the current
    question has no answer, the old key taken off; `skipped`: both answered."""
    event = await deliveries.event_for_club(session, user, event_id)
    return MoveOut.model_validate(await service.move(session, event, body.from_, body.to, actor=user, request=request))


@router.post("/answers/asks/preview", summary="How many an ask would reach, on each channel")
async def preview_ask(session: SessionDep, user: ClubAuth, event_id: int, body: AskPreviewIn) -> AskPreviewOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return AskPreviewOut.model_validate(await asks_mod.preview(session, event, body))


@router.post("/answers/asks", summary="Ask the people missing these answers for them", status_code=201)
@limiter.limit("30/hour", key_func=account_or_address)
async def create_ask(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: AskIn) -> AskOut:
    """409 NOTHING_TO_ASK when nobody is askable; 409 EVENT_ENDED."""
    event = await deliveries.event_for_club(session, user, event_id)
    return AskOut.model_validate(await asks_mod.create(session, event, body, actor=_actor(request, user)))


@router.post("/answers/asks/{ask_id}/remind", summary="Remind the people still missing an ask's answers")
@limiter.limit("30/hour", key_func=account_or_address)
async def remind_ask(request: Request, session: SessionDep, user: ClubAuth, event_id: int, ask_id: UUID) -> AskOut:
    """At most two reminders, 12 hours apart: 409 REMIND_LIMIT, REMIND_TOO_SOON (`retry_after`)."""
    event = await deliveries.event_for_club(session, user, event_id)
    return AskOut.model_validate(await asks_mod.remind(session, event, ask_id))


@router.post("/answers/asks/{ask_id}/close", summary="Close an ask")
async def close_ask(session: SessionDep, user: ClubAuth, event_id: int, ask_id: UUID) -> AskOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return AskOut.model_validate(await asks_mod.close(session, event, ask_id))


# ─── The door's ──────────────────────────────────────────────────────────


@router.post("/gate/answers/{registration_id}", summary="Answers filled in at check-in")
async def gate_answers(request: Request, session: SessionDep, actor: GateAuth, event_id: int, registration_id: UUID, body: AnswersIn) -> DoorAnswersOut:
    """Types checked, nothing required (the door is relaxed)."""
    if not actor.permissions["check_in"]:
        raise CustomHTTPException(403, message="You may not check guests in at this door.")
    return DoorAnswersOut.model_validate(await service.door_answers(session, request.state.gate_event, registration_id, body.answers))


# ─── The person's (their link is the credential, or their own sign-in) ───


@router.get("/answers/{ref}", summary="What a club is asking this person for")
@limiter.limit("60/minute", key_func=_ref_key)
async def public_answers(
    request: Request, response: Response, session: SessionDep, event_id: int, ref: str, user: OptionalUserAuth,
    k: str = Query("", max_length=128),
) -> AnswersView:
    """404 for a bad ref or `k`. Opening it marks their asks opened."""
    response.headers["Cache-Control"] = NO_STORE
    return AnswersView.model_validate(await service.public_get(session, event_id, ref, k, user))


@router.post("/answers/{ref}", summary="The person's answers")
@limiter.limit("20/minute", key_func=_ref_key)
async def submit_answers(
    request: Request, response: Response, session: SessionDep, event_id: int, ref: str, body: AnswersIn, user: OptionalUserAuth,
    k: str = Query("", max_length=128),
) -> AnswersView:
    """422 INVALID_ANSWERS naming each question; 409 CLOSED after the deadline."""
    response.headers["Cache-Control"] = NO_STORE
    return AnswersView.model_validate(await service.public_post(session, event_id, ref, k, body.answers, user))
