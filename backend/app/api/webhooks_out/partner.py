"""The webhook endpoints on the partner API: /partner/v1/webhook-endpoints...
(feature-register/integrations-api-contract.md, Resources).

    router = make_router(require_scope)            # at the partner router's
    partner_router.include_router(router)          # `# WEBHOOKS_ROUTER_MOUNT`

`require_scope(name)` is the partner API's: a FastAPI dependency giving the
caller -- `PartnerCaller(club_id, client_id, client_type, scopes, livemode)`,
`client_type` "api_key" or "partner_app" (a `partner_app_id` attribute is
used when it has one). Every route here needs `webhooks:manage`.

- A club API key manages the club's own endpoints (those added in the
  dashboard too), in the key's own mode; an endpoint it adds belongs to it
  (its changes are not sent back to that endpoint unless
  `include_own_changes`). Sending people's details to an endpoint needs the
  key to have `attendees:read` and the DPDP statement confirmed.
- A partner app manages only its own endpoints for the club it is acting
  for; whether they get people's details is its connection's attendees:read.

Refusals are CustomHTTPExceptions with the contract's code as `error_code`
and the details as `errors`: what the partner API's PartnerRoute
(app/api/partner/routing.py, `include_partner_routes`) answers in its error
shape (`{error: {code, message, details?, request_id}}`). `error(status, code,
message, details)` makes another exception instead.
"""
from __future__ import annotations

from typing import Callable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.api.webhooks_out import service
from app.api.webhooks_out import types as wtypes
from app.api.webhooks_out.models import VIA_API_KEY, VIA_PARTNER
from app.api.webhooks_out.schemas import EndpointIn, EndpointPatch, RotateIn
from app.api.webhooks_out.service import Owner, WebhookError
from app.db.core import SessionDep
from app.response import CustomHTTPException

SCOPE = "webhooks:manage"


def _default_error(status: int, code: str, message: str, details: Optional[dict] = None) -> Exception:
    return CustomHTTPException(status, message=message, error_code=code, errors=details)


def owner_of(caller) -> Owner:
    """The caller as an endpoints' owner. An app's own token acts for no club
    (WebhookError FORBIDDEN_SCOPE): endpoints are per club."""
    if getattr(caller, "club_id", None) is None:
        raise WebhookError(403, "FORBIDDEN_SCOPE", "Webhook endpoints are a club's: use the token a club's connection gave you "
                                                   "(or the MOA-Club-Id header with the app's own token).")
    via = VIA_PARTNER if getattr(caller, "client_type", None) == "partner_app" else VIA_API_KEY
    partner_app_id = getattr(caller, "partner_app_id", None)
    if partner_app_id is None and via == VIA_PARTNER:
        partner_app_id = getattr(getattr(caller, "app", None), "id", None)
    if partner_app_id is None and via == VIA_PARTNER:
        try:
            partner_app_id = UUID(str(caller.client_id))
        except (TypeError, ValueError):
            partner_app_id = None
    livemode = getattr(caller, "livemode", None)
    return Owner(
        club_id=int(caller.club_id), via=via, client_id=str(caller.client_id) if caller.client_id is not None else None,
        partner_app_id=partner_app_id, livemode=None if livemode is None else bool(livemode),
        scopes=frozenset(getattr(caller, "scopes", None) or ()), name=getattr(caller, "name", None) or str(caller.client_id or via),
    )


def make_router(require_scope: Callable[[str], Callable], *, error: Optional[Callable[..., Exception]] = None) -> APIRouter:
    fail = error or _default_error
    router = APIRouter(tags=["webhook endpoints"])
    Caller = Depends(require_scope(SCOPE))

    def _raise(exc: WebhookError):
        raise fail(exc.status, exc.code, exc.message, exc.details)

    @router.get("/webhook-event-types", summary="Every event type a webhook can carry")
    async def event_types(caller=Caller) -> dict:
        return {"data": wtypes.listing(), "next_cursor": None}

    @router.get("/webhook-endpoints", summary="Your webhook endpoints")
    async def list_endpoints(session: SessionDep, caller=Caller) -> dict:
        try:
            owner = owner_of(caller)
        except WebhookError as exc:
            _raise(exc)
        return {"data": await service.listing_out(session, owner), "next_cursor": None}

    @router.post("/webhook-endpoints", status_code=201, summary="Add an endpoint; its secret (whsec_...) is in this answer only")
    async def create_endpoint(body: EndpointIn, session: SessionDep, caller=Caller) -> dict:
        try:
            owner = owner_of(caller)
            endpoint, secret = await service.create(session, owner, body.model_dump(exclude={"client_id"}))
        except WebhookError as exc:
            _raise(exc)
        await session.commit()
        return service.endpoint_out(endpoint, secret=secret)

    @router.get("/webhook-endpoints/{endpoint_id}", summary="One endpoint")
    async def get_endpoint(endpoint_id: str, session: SessionDep, caller=Caller) -> dict:
        try:
            return await service.one_out(session, owner_of(caller), endpoint_id)
        except WebhookError as exc:
            _raise(exc)

    @router.patch("/webhook-endpoints/{endpoint_id}", summary="Change an endpoint, or turn it off and on")
    async def update_endpoint(endpoint_id: str, body: EndpointPatch, session: SessionDep, caller=Caller) -> dict:
        try:
            endpoint = await service.update(session, owner_of(caller), endpoint_id, body.model_dump(exclude_unset=True, exclude={"client_id"}))
        except WebhookError as exc:
            _raise(exc)
        await session.commit()
        return service.endpoint_out(endpoint)

    @router.delete("/webhook-endpoints/{endpoint_id}", status_code=204, summary="Delete an endpoint and its delivery log")
    async def delete_endpoint(endpoint_id: str, session: SessionDep, caller=Caller) -> Response:
        try:
            await service.delete(session, owner_of(caller), endpoint_id)
        except WebhookError as exc:
            _raise(exc)
        await session.commit()
        return Response(status_code=204)

    @router.post("/webhook-endpoints/{endpoint_id}/rotate-secret", summary="A new secret, in this answer only; the old one signs too for a while")
    async def rotate_secret(endpoint_id: str, session: SessionDep, body: Optional[RotateIn] = None, caller=Caller) -> dict:
        try:
            endpoint, secret = await service.rotate(session, owner_of(caller), endpoint_id, body.keep_previous_hours if body else None)
        except WebhookError as exc:
            _raise(exc)
        await session.commit()
        return service.endpoint_out(endpoint, secret=secret)

    @router.post("/webhook-endpoints/{endpoint_id}/test", summary="Send a webhook.test to the endpoint now")
    async def test_endpoint(endpoint_id: str, session: SessionDep, caller=Caller) -> dict:
        try:
            return await service.test(session, owner_of(caller), endpoint_id)
        except WebhookError as exc:
            _raise(exc)

    @router.get("/webhook-endpoints/{endpoint_id}/deliveries", summary="The endpoint's deliveries, newest first (30 days)")
    async def list_deliveries(
        endpoint_id: str, session: SessionDep, caller=Caller,
        limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None,
        status: Optional[str] = None, type: Optional[str] = None,
    ) -> dict:
        try:
            return await service.deliveries(session, owner_of(caller), endpoint_id, limit=limit, cursor=cursor,
                                            status_filter=status, event_type=type)
        except WebhookError as exc:
            _raise(exc)

    @router.get("/webhook-deliveries/{delivery_id}", summary="One delivery, with the body as it is sent")
    async def get_delivery(delivery_id: str, session: SessionDep, caller=Caller) -> dict:
        try:
            return await service.delivery_detail(session, owner_of(caller), delivery_id)
        except WebhookError as exc:
            _raise(exc)

    @router.post("/webhook-deliveries/{delivery_id}/retry", summary="Send a delivery again, now")
    async def retry_delivery(delivery_id: str, session: SessionDep, caller=Caller) -> dict:
        try:
            return await service.retry(session, owner_of(caller), delivery_id)
        except WebhookError as exc:
            _raise(exc)

    return router
