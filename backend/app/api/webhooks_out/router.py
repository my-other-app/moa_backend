"""Webhook endpoints' routes in the dashboards (docs/WEBHOOKS.md).

- /clubs/integrations/webhooks/...: the club dashboard (Settings ->
  Integrations), for the club's owner and its admins only -- an endpoint
  receives the club's data, and its secret is shown once to whoever makes it.
  Editors and door staff are refused here, whatever club_access.policy lets
  through. Answers are the same objects the partner API gives.
- /admin/integrations/webhooks/...: the admin portal: the platform's webhook
  health (failing endpoints, volumes), any club's endpoints and their log,
  and turning one off (or on again).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, case, func, or_, select

from app.api.club_access import service as club_access
from app.api.social.access import club_of
from app.api.webhooks_out import delivery, objects, service
from app.api.webhooks_out import sweep as _sweep  # noqa: F401 - the retention loop (doth.start_loops)
from app.api.webhooks_out import types as wtypes
from app.api.webhooks_out.models import BY_ADMIN, FAILING, WebhookDeliveries, WebhookEndpoints, WebhookEvents
from app.api.webhooks_out.schemas import AdminEndpointPatch, EndpointIn, EndpointPatch, RotateIn
from app.api.webhooks_out.service import Owner, WebhookError
from app.core.auth.dependencies import AdminAuth, ClubAuth
from app.core.auth.permissions import ModerateAuth
from app.core.rate_limit import account_or_address, limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

club_router = APIRouter(prefix="/clubs/integrations/webhooks", tags=["webhooks"])
admin_router = APIRouter(prefix="/admin/integrations/webhooks", tags=["webhooks"])

ROLE_MESSAGE = "Only the club's owner and its admins can see and change webhooks."


def _fail(exc: WebhookError) -> CustomHTTPException:
    return CustomHTTPException(exc.status, message=exc.message, error_code=exc.code, errors=exc.details)


async def _owner(session, request: Request, user) -> Owner:
    if club_access.role_of(request) not in ("owner", "admin"):
        raise CustomHTTPException(403, message=ROLE_MESSAGE, error_code="ROLE_FORBIDDEN")
    club = await club_of(session, user)
    member = club_access.current_member(request)
    name = getattr(member, "name", None) or getattr(member, "email", None) or getattr(user, "full_name", None) or club.name
    return Owner(club_id=club.id, name=name, user_id=getattr(user, "id", None))


# ─── The club dashboard ──────────────────────────────────────────────────


@club_router.get("/status", summary="Whether webhooks can be used now, and the event types to choose from")
async def club_status(request: Request, session: SessionDep, user: ClubAuth) -> dict:
    owner = await _owner(session, request, user)
    return await service.status(session, owner.club_id)


@club_router.get("/event-types", summary="Every event type, with what it means")
async def event_types(request: Request, session: SessionDep, user: ClubAuth) -> list[dict]:
    await _owner(session, request, user)
    return wtypes.listing()


@club_router.get("", summary="The club's webhook endpoints (a connected app's too, read-only)")
async def list_endpoints(request: Request, session: SessionDep, user: ClubAuth) -> dict:
    owner = await _owner(session, request, user)
    return {"data": [service.endpoint_out(e) for e in await service.listing(session, owner)], "next_cursor": None}


@club_router.post("", status_code=201, summary="Add an endpoint: its signing secret is in this answer only")
@limiter.limit("20/minute", key_func=account_or_address)
async def create_endpoint(request: Request, session: SessionDep, user: ClubAuth, body: EndpointIn) -> dict:
    owner = await _owner(session, request, user)
    try:
        endpoint, secret = await service.create(session, owner, body.model_dump())
    except WebhookError as exc:
        raise _fail(exc)
    await session.commit()
    return service.endpoint_out(endpoint, secret=secret)


@club_router.get("/deliveries/{delivery_id}", summary="One delivery, with the body as it is sent")
async def get_delivery(request: Request, session: SessionDep, user: ClubAuth, delivery_id: str) -> dict:
    owner = await _owner(session, request, user)
    try:
        return await service.delivery_detail(session, owner, delivery_id)
    except WebhookError as exc:
        raise _fail(exc)


@club_router.post("/deliveries/{delivery_id}/retry", summary="Send a delivery again, now (any delivery: a replay)")
@limiter.limit("30/minute", key_func=account_or_address)
async def retry_delivery(request: Request, session: SessionDep, user: ClubAuth, delivery_id: str) -> dict:
    owner = await _owner(session, request, user)
    try:
        return await service.retry(session, owner, delivery_id)
    except WebhookError as exc:
        raise _fail(exc)


@club_router.get("/{endpoint_id}", summary="One endpoint")
async def get_endpoint(request: Request, session: SessionDep, user: ClubAuth, endpoint_id: str) -> dict:
    owner = await _owner(session, request, user)
    try:
        return service.endpoint_out(await service.get(session, owner, endpoint_id))
    except WebhookError as exc:
        raise _fail(exc)


@club_router.patch("/{endpoint_id}", summary="Change an endpoint, or turn it off and on")
async def update_endpoint(request: Request, session: SessionDep, user: ClubAuth, endpoint_id: str, body: EndpointPatch) -> dict:
    owner = await _owner(session, request, user)
    try:
        endpoint = await service.update(session, owner, endpoint_id, body.model_dump(exclude_unset=True))
    except WebhookError as exc:
        raise _fail(exc)
    await session.commit()
    return service.endpoint_out(endpoint)


@club_router.delete("/{endpoint_id}", status_code=204, summary="Delete an endpoint and its delivery log")
async def delete_endpoint(request: Request, session: SessionDep, user: ClubAuth, endpoint_id: str) -> None:
    owner = await _owner(session, request, user)
    try:
        await service.delete(session, owner, endpoint_id)
    except WebhookError as exc:
        raise _fail(exc)
    await session.commit()


@club_router.post("/{endpoint_id}/rotate-secret", summary="A new signing secret, in this answer only; the old one signs too for a while")
@limiter.limit("10/minute", key_func=account_or_address)
async def rotate_secret(request: Request, session: SessionDep, user: ClubAuth, endpoint_id: str, body: Optional[RotateIn] = None) -> dict:
    owner = await _owner(session, request, user)
    try:
        endpoint, secret = await service.rotate(session, owner, endpoint_id, body.keep_previous_hours if body else None)
    except WebhookError as exc:
        raise _fail(exc)
    await session.commit()
    return service.endpoint_out(endpoint, secret=secret)


@club_router.post("/{endpoint_id}/test", summary="Send a webhook.test to the endpoint now, and say how it answered")
@limiter.limit("10/minute", key_func=account_or_address)
async def test_endpoint(request: Request, session: SessionDep, user: ClubAuth, endpoint_id: str) -> dict:
    owner = await _owner(session, request, user)
    try:
        return await service.test(session, owner, endpoint_id)
    except WebhookError as exc:
        raise _fail(exc)


@club_router.get("/{endpoint_id}/deliveries", summary="The endpoint's delivery log, newest first (30 days)")
async def list_deliveries(
    request: Request, session: SessionDep, user: ClubAuth, endpoint_id: str,
    limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None,
    status: Optional[str] = None, event_type: Optional[str] = None,
) -> dict:
    owner = await _owner(session, request, user)
    try:
        return await service.deliveries(session, owner, endpoint_id, limit=limit, cursor=cursor, status_filter=status, event_type=event_type)
    except WebhookError as exc:
        raise _fail(exc)


# ─── The admin portal ────────────────────────────────────────────────────


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@admin_router.get("/health", summary="Webhook health: failing endpoints, volumes and success rates")
async def health(session: SessionDep, admin: AdminAuth, days: int = Query(30, ge=1, le=30)) -> dict:
    from app.api.clubs.models import Clubs

    endpoints = (await session.execute(select(
        func.count(WebhookEndpoints.id),
        func.count(WebhookEndpoints.id).filter(WebhookEndpoints.enabled == True),  # noqa: E712
        func.count(WebhookEndpoints.id).filter(WebhookEndpoints.disabled_reason == FAILING),
        func.count(WebhookEndpoints.id).filter(WebhookEndpoints.failing_since.isnot(None), WebhookEndpoints.enabled == True),  # noqa: E712
    ))).one()
    day = func.date_trunc("day", func.timezone("Asia/Kolkata", WebhookDeliveries.created_at))
    per_day = (await session.execute(
        select(
            day.label("day"), func.count(WebhookDeliveries.id),
            func.count(WebhookDeliveries.id).filter(WebhookDeliveries.status == "succeeded"),
            func.count(WebhookDeliveries.id).filter(WebhookDeliveries.status == "failed"),
            func.count(WebhookDeliveries.id).filter(WebhookDeliveries.status.in_(("pending", "retrying"))),
        ).where(WebhookDeliveries.created_at >= _since(days)).group_by(day).order_by(day)
    )).all()
    top = (await session.execute(
        select(WebhookDeliveries.club_id, Clubs.name, func.count(WebhookDeliveries.id).label("n"),
               func.count(WebhookDeliveries.id).filter(WebhookDeliveries.status == "failed"))
        .join(Clubs, Clubs.id == WebhookDeliveries.club_id)
        .where(WebhookDeliveries.created_at >= _since(7))
        .group_by(WebhookDeliveries.club_id, Clubs.name).order_by(func.count(WebhookDeliveries.id).desc()).limit(10)
    )).all()
    failing = await _failing(session)
    return {
        "endpoints": {"total": endpoints[0], "enabled": endpoints[1], "turned_off_failing": endpoints[2], "failing_now": endpoints[3]},
        "per_day": [{"day": d.date().isoformat(), "deliveries": n, "succeeded": ok, "failed": bad, "waiting": waiting}
                    for d, n, ok, bad, waiting in per_day],
        "top_clubs_7d": [{"club_id": c, "club_name": name, "deliveries": n, "failed": bad} for c, name, n, bad in top],
        "failing_endpoints": failing,
        "retry_schedule_seconds": list(delivery.RETRY_SCHEDULE),
    }


async def _failing(session, limit: int = 50) -> list[dict]:
    from app.api.clubs.models import Clubs

    rows = (await session.execute(
        select(WebhookEndpoints, Clubs.name)
        .join(Clubs, Clubs.id == WebhookEndpoints.club_id)
        .where(or_(and_(WebhookEndpoints.failing_since.isnot(None), WebhookEndpoints.enabled == True),  # noqa: E712
                   WebhookEndpoints.disabled_reason == FAILING))
        .order_by(case((WebhookEndpoints.enabled == True, 0), else_=1), WebhookEndpoints.failing_since.asc().nulls_last())  # noqa: E712
        .limit(limit)
    )).all()
    out = []
    for endpoint, club_name in rows:
        last = await session.scalar(
            select(WebhookDeliveries.error).where(WebhookDeliveries.endpoint_id == endpoint.id, WebhookDeliveries.error.isnot(None))
            .order_by(WebhookDeliveries.updated_at.desc()).limit(1)
        )
        out.append({**service.endpoint_out(endpoint), "club_id": endpoint.club_id, "club_name": club_name, "last_error": last})
    return out


@admin_router.get("/endpoints", summary="Every club's endpoints, or one club's")
async def admin_endpoints(
    session: SessionDep, admin: AdminAuth, club_id: Optional[int] = None,
    state: Optional[str] = Query(None, pattern="^(enabled|disabled|failing)$"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict:
    stmt = select(WebhookEndpoints)
    if club_id is not None:
        stmt = stmt.where(WebhookEndpoints.club_id == club_id)
    if state == "enabled":
        stmt = stmt.where(WebhookEndpoints.enabled == True)  # noqa: E712
    elif state == "disabled":
        stmt = stmt.where(WebhookEndpoints.enabled == False)  # noqa: E712
    elif state == "failing":
        stmt = stmt.where(WebhookEndpoints.failing_since.isnot(None))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(await session.scalars(stmt.order_by(WebhookEndpoints.created_at.desc()).limit(limit).offset(offset)))
    return {"data": [{**service.endpoint_out(e), "club_id": e.club_id} for e in rows], "total": int(total or 0)}


async def _admin_endpoint(session, endpoint_id: str) -> WebhookEndpoints:
    from uuid import UUID

    try:
        endpoint = await session.get(WebhookEndpoints, UUID(endpoint_id))
    except ValueError:
        endpoint = None
    if endpoint is None:
        raise CustomHTTPException(404, message="No such webhook endpoint.", error_code="NOT_FOUND")
    return endpoint


@admin_router.get("/endpoints/{endpoint_id}/deliveries", summary="An endpoint's delivery log")
async def admin_deliveries(
    session: SessionDep, admin: AdminAuth, endpoint_id: str,
    limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None, status: Optional[str] = None,
) -> dict:
    endpoint = await _admin_endpoint(session, endpoint_id)
    owner = Owner(club_id=endpoint.club_id)
    try:
        return await service.deliveries(session, owner, endpoint.id, limit=limit, cursor=cursor, status_filter=status)
    except WebhookError as exc:
        raise _fail(exc)


@admin_router.patch("/endpoints/{endpoint_id}", summary="Turn an endpoint off (or on again), whoever owns it")
async def admin_set_enabled(session: SessionDep, admin: ModerateAuth, endpoint_id: str, body: AdminEndpointPatch) -> dict:
    endpoint = await _admin_endpoint(session, endpoint_id)
    if body.enabled and not endpoint.enabled:
        delivery.enable(endpoint)
    elif not body.enabled and endpoint.enabled:
        await delivery.disable(session, endpoint, BY_ADMIN)
    endpoint.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {**service.endpoint_out(endpoint), "club_id": endpoint.club_id}


@admin_router.get("/events/{event_id}", summary="One change as it was sent (the object, origin, previous)")
async def admin_event(session: SessionDep, admin: AdminAuth, event_id: str) -> dict:
    event = await session.get(WebhookEvents, event_id)
    if event is None:
        raise CustomHTTPException(404, message="No such webhook event.", error_code="NOT_FOUND")
    return {
        "id": event.id, "type": event.type, "club_id": event.club_id, "livemode": event.livemode,
        "origin": {"type": event.origin_type, "client_id": event.origin_client_id},
        "object_type": event.object_type, "object_id": event.object_id, "previous": event.previous,
        "data": event.data, "created_at": objects.when(event.created_at), "fanned_out_at": objects.when(event.fanned_out_at),
    }
