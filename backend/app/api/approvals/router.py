"""Registration requests' routes (docs/APPROVALS.md): the club's queue and
its decisions, and the guest's own request by its link."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app.api.approvals import notify as _notify  # noqa: F401 - registers the job that tells people
from app.api.approvals import service
from app.api.approvals.models import STATUSES
from app.api.approvals.schemas import (
    ApproveIn,
    ApproveOut,
    DeclineIn,
    DeclineOut,
    ReopenIn,
    ReopenOut,
    RequestPublic,
    RequestsMine,
    RequestsPage,
)
from app.api.deliveries import service as deliveries
from app.api.events.access import is_admin_user
from app.core.auth.dependencies import ClubAuth, GuestAuth
from app.core.auth.permissions import EXPORT_DATA, ensure_admin_can
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(tags=["registration requests"])
public_router = APIRouter(tags=["registration requests"])

NO_STORE = "no-store"


def _actor(request: Request, user):
    """Who is deciding (confirm, decline, back to review): the club, never a
    platform admin. ClubAuth admits admins, so an admin reads the queue and
    exports it as they read an event's attendees, but the decisions are the
    club's. An admin acting as the club (impersonating) arrives as the club's
    own account and decides as it."""
    from app.api.club_ops.access import actor_of

    if is_admin_user(user):
        raise CustomHTTPException(
            403,
            message="Only the club can confirm, decline or put back a registration under review",
            error_code="INSUFFICIENT_PERMISSIONS",
        )
    return actor_of(request, user)


# ─── The club's (owner, admin, editor decide; door staff read: club_access.policy) ──
# Platform admins read the queue and its export for any event, as they read
# its attendees (app/api/events/access.py), and decide nothing (_actor).


@router.get("/events/registration/{event_id}/requests", summary="The event's registration requests, a page at a time")
async def list_requests(
    session: SessionDep, user: ClubAuth, event_id: int,
    status: str = Query("pending", pattern="^(" + "|".join((*STATUSES, "all")) + ")$"),
    q: str | None = Query(None, max_length=100, description="Name, email or phone contains"),
    after: str | None = Query(None, max_length=200, description="The `next` of the page before"),
    limit: int = Query(50, ge=1, le=200),
) -> RequestsPage:
    """Pending: the queue, oldest first. Any other status: newest first.
    Keyset pages (`next`), the counts by status, and the places left."""
    event = await deliveries.event_for_club(session, user, event_id)
    return await service.club_list(session, event, status=None if status == "all" else status, q=q, after=after, limit=limit)


@router.get("/events/registration/{event_id}/requests/export", summary="Every registration request of the event, as CSV")
async def export_requests(session: SessionDep, user: ClubAuth, event_id: int) -> Response:
    """Every request, whatever its status: the person, the ticket type and
    code, the answers, when asked and decided and by whom, the decline note,
    and the payment's state. Whoever may read the queue may export it; an
    admin also needs EXPORT_DATA, as for the attendees' export."""
    ensure_admin_can(user, EXPORT_DATA)
    event = await deliveries.event_for_club(session, user, event_id)
    body = await service.export_csv(session, event)
    name = f"{event.slug or event.id}-requests.csv"
    return Response(
        content=body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": NO_STORE},
    )


@router.post("/events/registration/{event_id}/requests/approve", summary="Confirm registration requests")
async def approve_requests(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: ApproveIn) -> ApproveOut:
    """In queue order, never past the event's or a ticket type's places: the
    rest are skipped with a reason. A free ticket is registered now and its
    ticket emailed; a paid one gets a pay link, its place held for the
    waiting list's offer hours."""
    event = await deliveries.event_for_club(session, user, event_id)
    return await service.approve(
        session, event, ids=body.ids, all_pending=body.all_pending, limit=body.limit, actor=_actor(request, user),
    )


@router.post("/events/registration/{event_id}/requests/decline", summary="Decline registration requests")
async def decline_requests(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: DeclineIn) -> DeclineOut:
    """`ids`, or `all_pending` (the oldest `limit`, at most 500). Told by
    email, with the note, when `notify` (the default)."""
    event = await deliveries.event_for_club(session, user, event_id)
    return await service.decline(
        session, event, body.ids, all_pending=body.all_pending, limit=body.limit, notify=body.notify, note=body.note,
        actor=_actor(request, user),
    )


@router.post("/events/registration/{event_id}/requests/reopen", summary="Put declined or lapsed requests back in the queue")
async def reopen_requests(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: ReopenIn) -> ReopenOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return await service.reopen(session, event, body.ids, actor=_actor(request, user))


# ─── The guest's (their link is the credential) ─────────────────────────


@public_router.get("/registration-requests/mine", summary="The signed-in person's registration requests")
async def my_requests(session: SessionDep, user: GuestAuth, response: Response) -> RequestsMine:
    """Guests included (the session the form handed out). Open ones first."""
    response.headers["Cache-Control"] = NO_STORE
    return {"items": await service.mine(session, user)}


@public_router.get("/registration-requests/{token}", summary="A registration request, by its link")
@limiter.limit("30/minute")
async def get_request(request: Request, response: Response, session: SessionDep, token: str) -> RequestPublic:
    response.headers["Cache-Control"] = NO_STORE
    return await service.view(session, await service.by_token(session, token))


@public_router.post("/registration-requests/{token}/withdraw", summary="Withdraw a registration request")
@limiter.limit("10/minute")
async def withdraw_request(request: Request, response: Response, session: SessionDep, token: str) -> RequestPublic:
    """A pending request, or a confirmed paid one not paid yet (its held place
    goes back). 409 REQUEST_NOT_WITHDRAWABLE for anything else, with the
    request as it stands in `errors.request`."""
    response.headers["Cache-Control"] = NO_STORE
    req = await service.withdraw(session, token)
    return await service.view(session, req)
