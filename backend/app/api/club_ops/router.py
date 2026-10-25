"""The club tools' routes.

On an event (ClubAuth: the club that owns it, or an admin):
- results: GET /events/{id}/results, PUT /events/{id}/results/teams/{team}.
  Changing them is event work: editors and admins (club_access.policy's
  defaults); door staff read.
- money: GET /events/{id}/payments, POST .../payments/{payment}/refund,
  POST /events/{id}/refunds/{refund}/retry, and cancelling --
  GET/POST /events/{id}/cancel, POST /events/{id}/cancel/retry. Owners and
  admins only (club_access.policy._MONEY); a MyOtherApp admin, signed in or
  acting as the club, needs the money capability to give money back
  (access.require_money).

Signed out, on the clubs dashboard's sign-in page:
- POST /auth/club/password/forgot, POST /auth/club/password/reset.
"""
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request

from app.api.club_ops import cancel as cancel_service
from app.api.club_ops import password as password_service
from app.api.club_ops import refunds as refund_service
from app.api.club_ops import results as results_service
from app.api.club_ops.access import actor_of, require_money
from app.api.club_ops.schemas import (
    CancelRequest,
    CancelState,
    EventPayments,
    ForgotRequest,
    ForgotResponse,
    PlaceTeam,
    RefundRequest,
    RefundResult,
    ResetRequest,
    ResetResponse,
    Results,
)
from app.api.deliveries.service import event_for_club
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep

router = APIRouter(tags=["club tools"])
auth_router = APIRouter(prefix="/auth/club/password", tags=["auth"])


# ─── Results ───────────────────────────────────────────────────────────


@router.get("/events/{event_id}/results", summary="The event's 1st, 2nd and 3rd: guests, and teams in each competition track")
async def get_results(event_id: int, session: SessionDep, user: ClubAuth) -> Results:
    event = await event_for_club(session, user, event_id)
    return Results(**await results_service.results(session, event))


@router.put("/events/{event_id}/results/teams/{team_id}", summary="Place a competition team 1st, 2nd or 3rd in its track (null: no place)")
async def place_team(event_id: int, team_id: int, body: PlaceTeam, session: SessionDep, user: ClubAuth) -> Results:
    event = await event_for_club(session, user, event_id)
    return Results(**await results_service.place_team(session, event, team_id, body.position, actor_id=user.id))


# ─── Payments and refunds ──────────────────────────────────────────────


@router.get("/events/{event_id}/payments", summary="Every payment for the event, with what was refunded")
async def event_payments(event_id: int, session: SessionDep, user: ClubAuth) -> EventPayments:
    event = await event_for_club(session, user, event_id)
    return EventPayments(**await refund_service.event_payments(session, event))


@router.post("/events/{event_id}/payments/{payment_id}/refund", summary="Refund a guest, all or part of one payment, with a reason")
async def refund_payment(event_id: int, payment_id: UUID, body: RefundRequest, request: Request, session: SessionDep, user: ClubAuth) -> RefundResult:
    """Through the Razorpay account the payment was taken on: MyOtherApp's
    or the club's own. Answered once Razorpay has: `refund.status` is
    "succeeded", or "failed" with Razorpay's reason (tried again with
    /refunds/{id}/retry). The same `client_ref` again answers with the first."""
    event = await event_for_club(session, user, event_id)
    actor = actor_of(request, user)
    await require_money(session, actor)
    refund = await refund_service.request_refund(
        session, event, payment_id, amount=body.amount, reason=body.reason, actor=actor, client_ref=body.client_ref, request=request,
    )
    return RefundResult(refund=refund_service.refund_out(refund), payment=await refund_service.payment_view(session, event.id, payment_id))


@router.post("/events/{event_id}/refunds/{refund_id}/retry", summary="Ask Razorpay again for a refund it refused")
async def retry_refund(event_id: int, refund_id: UUID, request: Request, session: SessionDep, user: ClubAuth) -> RefundResult:
    event = await event_for_club(session, user, event_id)
    actor = actor_of(request, user)
    await require_money(session, actor)
    refund = await refund_service.retry(session, event, refund_id, actor=actor, request=request)
    return RefundResult(refund=refund_service.refund_out(refund), payment=await refund_service.payment_view(session, event.id, refund.payment_log_id))


# ─── Cancelling ────────────────────────────────────────────────────────


@router.get("/events/{event_id}/cancel", summary="What cancelling would do, or what it did")
async def cancel_state(event_id: int, session: SessionDep, user: ClubAuth) -> CancelState:
    event = await event_for_club(session, user, event_id)
    return CancelState(**await cancel_service.state(session, event))


@router.post("/events/{event_id}/cancel", summary="Cancel the event: tell every guest, refund every online payment")
async def cancel_event(event_id: int, body: CancelRequest, request: Request, session: SessionDep, user: ClubAuth) -> CancelState:
    """Once: an event already cancelled answers with what was done. Send
    what the confirm step showed (`expected_*`): when registrations or
    payments changed since, 409 CANCEL_PREVIEW_STALE with the new figures."""
    event = await event_for_club(session, user, event_id)
    actor = actor_of(request, user)
    await require_money(session, actor)
    return CancelState(**await cancel_service.cancel(
        session, event.id, reason=body.reason, actor=actor, expected_registrations=body.expected_registrations,
        expected_refund_total=body.expected_refund_total, request=request,
    ))


@router.post("/events/{event_id}/cancel/retry", summary="Try every refund of the cancellation that failed again")
async def retry_cancel_refunds(event_id: int, request: Request, session: SessionDep, user: ClubAuth) -> CancelState:
    event = await event_for_club(session, user, event_id)
    actor = actor_of(request, user)
    await require_money(session, actor)
    return CancelState(**await cancel_service.retry_failed(session, event, actor=actor, request=request))


# ─── A forgotten password ──────────────────────────────────────────────


def _caller(request: Request) -> str:
    from app.api.security.service import client_ip

    return client_ip(request) or "unknown"


@auth_router.post("/forgot", summary="Send a code to reset a club login's password (the club's account or a team member's)")
@limiter.limit("5/minute;20/hour", key_func=_caller)
async def forgot(request: Request, body: ForgotRequest, background: BackgroundTasks) -> ForgotResponse:
    """Always 200 `{"sent": true}`, at once, whatever the address."""
    background.add_task(password_service.request_reset, body.email)
    return ForgotResponse()


@auth_router.post("/reset", summary="Set a new password with the emailed code")
@limiter.limit("10/minute", key_func=_caller)
async def reset(request: Request, body: ResetRequest, session: SessionDep) -> ResetResponse:
    """Then sign in as usual. Every session of that login has ended."""
    await password_service.reset(
        session, email=body.email, code=body.code, new_password=body.new_password, totp_code=body.totp_code,
    )
    return ResetResponse()
