"""The route class every /partner/v1 route uses.

Around each request: the error shape (errors.py) for anything a route or its
dependencies raise, the idempotent replay, and on every answer `X-Request-Id`,
`X-RateLimit-Limit/Remaining/Reset` and `Cache-Control: no-store`. Then the
answer is kept for its Idempotency-Key, the request counted (usage.py), and
the change's origin reset.

`include_partner_routes(router, other)` adds another router's routes with
this class: how the webhook endpoints (agent B's make_router) are mounted,
so they answer in the same shape with the same headers.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.routing import APIRoute

from app.api.partner import errors, idempotency, usage
from app.core import api_origin

logger = logging.getLogger(__name__)


async def _after(request, response) -> None:
    rid = errors.request_id(request)
    response.headers["X-Request-Id"] = rid
    rate = getattr(request.state, "partner_rate", None)
    if rate is not None:
        limit, remaining, reset = rate
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset)
    response.headers.setdefault("Cache-Control", "no-store")
    session = getattr(request.state, "partner_session", None)
    caller = getattr(request.state, "partner_caller", None)
    if session is None or caller is None:
        return
    await idempotency.finish(session, request, response)
    await usage.record(
        session, client_id=caller.client_id, client_type=caller.client_type, club_id=caller.club_id, livemode=caller.livemode,
        status=response.status_code, write=request.method in ("POST", "PUT", "PATCH", "DELETE"),
        attendee_reads=int(getattr(request.state, "partner_attendee_reads", 0) or 0),
    )


class PartnerRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            errors.request_id(request)
            try:
                try:
                    response = await original(request)
                except idempotency.Replay as replay:
                    response = replay.response
                except Exception as exc:  # noqa: BLE001 - every refusal in this API's own shape
                    try:
                        response = errors.render(request, exc)
                    except Exception:  # noqa: BLE001
                        response = errors.internal(request, exc)
                try:
                    await _after(request, response)
                except Exception:  # noqa: BLE001
                    logger.warning("partner API bookkeeping failed", exc_info=True)
                return response
            finally:
                token = getattr(request.state, "partner_origin_token", None)
                if token is not None:
                    try:
                        api_origin.reset_origin(token)
                    except ValueError:
                        pass
                    request.state.partner_origin_token = None

        return handler


def include_partner_routes(router: APIRouter, other: APIRouter, **kwargs) -> None:
    """`other`'s routes on `router`, each as a PartnerRoute."""
    for route in other.routes:
        if not isinstance(route, APIRoute):
            router.routes.append(route)
            continue
        router.add_api_route(
            route.path, route.endpoint, methods=list(route.methods or []), response_model=route.response_model,
            status_code=route.status_code, tags=route.tags or kwargs.get("tags"), dependencies=route.dependencies,
            summary=route.summary, description=route.description, response_description=route.response_description,
            responses=route.responses, deprecated=route.deprecated, operation_id=route.operation_id,
            include_in_schema=route.include_in_schema, response_class=route.response_class, name=route.name,
            openapi_extra=route.openapi_extra, route_class_override=PartnerRoute,
        )
