"""Managing endpoints: one set of rules for the club dashboard
(/api/v1/clubs/integrations/webhooks, router.py) and the partner API
(/partner/v1/webhook-endpoints, partner.py).

Who sees what:

- the club's owner and admins in the dashboard: every endpoint of the club,
  a partner's too (read-only: the partner manages it; the club can
  disconnect the partner);
- a club API key with webhooks:manage: the club's own endpoints;
- a partner app with webhooks:manage: its own endpoints for that club.

Errors are `WebhookError(status, code, message, details)`; each surface
answers them in its own error shape.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from app.api.webhooks_out import crypto, delivery, objects, seams, sender
from app.api.webhooks_out import types as wtypes
from app.api.webhooks_out.models import (
    BY_OWNER, DELIVERY_STATUSES, VIA_API_KEY, VIA_DASHBOARD, VIA_PARTNER, WebhookDeliveries, WebhookEndpoints, WebhookEvents,
)
from app.core import net

#: Endpoints a club may have of its own, and a partner for one club.
MAX_ENDPOINTS = 10
MAX_PARTNER_ENDPOINTS = 5
#: After a rotation the old secret keeps signing this long unless told otherwise.
DEFAULT_OVERLAP_HOURS = 24
MAX_OVERLAP_HOURS = 72


class WebhookError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


@dataclass
class Owner:
    """Who is managing endpoints, and for which club."""

    club_id: int
    #: dashboard | api_key | partner_app
    via: str = VIA_DASHBOARD
    client_id: Optional[str] = None
    partner_app_id: Optional[UUID] = None
    #: An API caller's own mode: its endpoints are in it, and only it.
    livemode: Optional[bool] = None
    scopes: frozenset = field(default_factory=frozenset)
    name: Optional[str] = None
    user_id: Optional[int] = None

    @property
    def is_partner(self) -> bool:
        return self.via == VIA_PARTNER

    @property
    def may_share_attendees(self) -> bool:
        """Ticking the DPDP box: the dashboard, or a key that reads attendee details itself."""
        return self.via == VIA_DASHBOARD or "attendees:read" in self.scopes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _partner_owned(endpoint) -> bool:
    return endpoint.partner_app_id is not None or endpoint.created_via == VIA_PARTNER


# ─── What is shown ───────────────────────────────────────────────────────


async def last_answers(session, endpoint_ids) -> dict:
    """Each endpoint's last try: (its answer's status, when), one query."""
    ids = [e for e in endpoint_ids or []]
    if not ids:
        return {}
    rows = (await session.execute(
        select(WebhookDeliveries.endpoint_id, WebhookDeliveries.response_status, WebhookDeliveries.last_attempt_at)
        .where(WebhookDeliveries.endpoint_id.in_(ids), WebhookDeliveries.last_attempt_at.isnot(None))
        .distinct(WebhookDeliveries.endpoint_id)
        .order_by(WebhookDeliveries.endpoint_id, WebhookDeliveries.last_attempt_at.desc())
    )).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def endpoint_out(endpoint, *, secret: Optional[str] = None, last: Optional[tuple] = None) -> dict:
    if _partner_owned(endpoint):
        owner = {"type": "partner_app", "client_id": endpoint.owner_client_id,
                 "partner_app_id": str(endpoint.partner_app_id) if endpoint.partner_app_id else None}
    else:
        owner = {"type": "club", "client_id": endpoint.owner_client_id, "partner_app_id": None}
    out = {
        "id": str(endpoint.id),
        "url": endpoint.url,
        "description": endpoint.description,
        "events": list(endpoint.events or []),
        "include_own_changes": bool(endpoint.include_own_changes),
        "livemode": bool(endpoint.livemode),
        "enabled": bool(endpoint.enabled),
        "status": "enabled" if endpoint.enabled else "disabled",
        "disabled_reason": endpoint.disabled_reason,
        "disabled_at": objects.when(endpoint.disabled_at),
        "share_attendee_details": bool(endpoint.share_attendee_details) and not _partner_owned(endpoint),
        "dpdp_confirmed_at": objects.when(endpoint.dpdp_confirmed_at),
        "dpdp_confirmed_by": endpoint.dpdp_confirmed_by,
        "owner": owner,
        "created_via": endpoint.created_via,
        "secret_hint": f"{crypto.PREFIX}…{endpoint.secret_hint}" if endpoint.secret_hint else None,
        "secret_rotated_at": objects.when(endpoint.secret_rotated_at),
        "previous_secret_expires_at": objects.when(endpoint.previous_secret_expires_at) if endpoint.previous_secret_enc else None,
        "failing_since": objects.when(endpoint.failing_since),
        "consecutive_failures": int(endpoint.consecutive_failures or 0),
        "last_success_at": objects.when(endpoint.last_success_at),
        "last_failure_at": objects.when(endpoint.last_failure_at),
        "last_response_status": last[0] if last else None,
        "last_delivery_at": objects.when(last[1]) if last else None,
        "created_by_name": endpoint.created_by_name,
        "created_at": objects.when(endpoint.created_at),
        "updated_at": objects.when(endpoint.updated_at),
    }
    if secret is not None:
        out["secret"] = secret
    return out


def delivery_out(row: WebhookDeliveries, event: Optional[WebhookEvents]) -> dict:
    return {
        "id": str(row.id),
        "endpoint_id": str(row.endpoint_id),
        "event_id": row.event_id,
        "event_type": event.type if event is not None else None,
        "object_id": event.object_id if event is not None else None,
        "status": row.status,
        "attempts": int(row.attempts or 0),
        "next_attempt_at": objects.when(row.next_attempt_at),
        "last_attempt_at": objects.when(row.last_attempt_at),
        "succeeded_at": objects.when(row.succeeded_at),
        "response_status": row.response_status,
        "response_body": row.response_body,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "created_at": objects.when(row.created_at),
    }


# ─── Checks ──────────────────────────────────────────────────────────────


async def require_ready(session, club_id: int) -> None:
    if not crypto.available():
        raise WebhookError(503, "WEBHOOKS_UNAVAILABLE", crypto.UNAVAILABLE_MESSAGE)
    if not await seams.club_allocated(session, club_id):
        raise WebhookError(403, "NOT_ALLOCATED", "API access (and with it webhooks) is not turned on for this club. Ask the MyOtherApp team.")


async def status(session, club_id: int) -> dict:
    available = crypto.available()
    allocated = await seams.club_allocated(session, club_id)
    reason = None if available and allocated else ("unavailable" if not available else "not_allocated")
    return {
        "available": available and allocated, "reason": reason,
        "message": None if reason is None else (crypto.UNAVAILABLE_MESSAGE if reason == "unavailable" else
                                                "API access is not turned on for this club. Ask the MyOtherApp team."),
        "max_endpoints": MAX_ENDPOINTS, "retry_schedule_seconds": list(delivery.RETRY_SCHEDULE),
        "event_types": wtypes.listing(),
    }


async def check_url(url: str) -> str:
    """The address, when the server may send to it: https, and every address
    its host resolves to public (asked now, and again at every send)."""
    try:
        parts = net.check_https_url(url)
        await sender.RESOLVE(parts.hostname, parts.port or 443)
    except net.UnsafeURL as exc:
        raise WebhookError(422, "VALIDATION", str(exc), {"field": "url"})
    return url.strip()


def check_events(events) -> list[str]:
    try:
        return wtypes.valid(events if events is not None else [wtypes.ALL])
    except ValueError as exc:
        raise WebhookError(422, "VALIDATION", str(exc), {"field": "events"})


def _dpdp_statement(url: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or url
    return f"Our registration form tells people their details are shared with {host}."


def _check_sharing(owner: Owner, url: str, share: Optional[bool], confirmed: bool) -> bool:
    if not share:
        return False
    if owner.is_partner:
        raise WebhookError(422, "VALIDATION", "A partner app's endpoints get attendee details by its connection's attendees:read scope, "
                                              "not by this setting.", {"field": "share_attendee_details"})
    if not owner.may_share_attendees:
        raise WebhookError(403, "FORBIDDEN_SCOPE", "Sending people's details to an endpoint needs the attendees:read scope.",
                           {"scope": "attendees:read"})
    if not confirmed:
        raise WebhookError(422, "VALIDATION", "Confirm the statement to send people's names, emails, phones and answers: "
                                              + _dpdp_statement(url), {"field": "dpdp_confirmed", "statement": _dpdp_statement(url)})
    return True


def _visible(owner: Owner):
    """The endpoints this owner may see."""
    conds = [WebhookEndpoints.club_id == owner.club_id]
    if owner.is_partner:
        conds.append(WebhookEndpoints.owner_client_id == owner.client_id)
        conds.append(or_(WebhookEndpoints.partner_app_id.isnot(None), WebhookEndpoints.created_via == VIA_PARTNER))
    elif owner.via == VIA_API_KEY:
        conds.append(and_(WebhookEndpoints.partner_app_id.is_(None), WebhookEndpoints.created_via != VIA_PARTNER))
    return and_(*conds)


async def get(session, owner: Owner, endpoint_id, *, for_change: bool = False) -> WebhookEndpoints:
    try:
        uid = UUID(str(endpoint_id))
    except ValueError:
        raise WebhookError(404, "NOT_FOUND", "No such webhook endpoint.")
    stmt = select(WebhookEndpoints).where(WebhookEndpoints.id == uid, _visible(owner))
    if for_change:
        stmt = stmt.with_for_update()
    endpoint = await session.scalar(stmt)
    if endpoint is None:
        raise WebhookError(404, "NOT_FOUND", "No such webhook endpoint.")
    if for_change and not owner.is_partner and _partner_owned(endpoint):
        raise WebhookError(403, "PARTNER_MANAGED", "This endpoint belongs to a connected app, which manages it. "
                                                   "Disconnect the app to stop it.")
    return endpoint


# ─── Changes ─────────────────────────────────────────────────────────────


async def listing(session, owner: Owner) -> list[WebhookEndpoints]:
    return list(await session.scalars(select(WebhookEndpoints).where(_visible(owner)).order_by(WebhookEndpoints.created_at)))


async def listing_out(session, owner: Owner) -> list[dict]:
    rows = await listing(session, owner)
    last = await last_answers(session, [e.id for e in rows])
    return [endpoint_out(e, last=last.get(e.id)) for e in rows]


async def one_out(session, owner: Owner, endpoint_id) -> dict:
    endpoint = await get(session, owner, endpoint_id)
    return endpoint_out(endpoint, last=(await last_answers(session, [endpoint.id])).get(endpoint.id))


async def create(session, owner: Owner, data: dict) -> tuple[WebhookEndpoints, str]:
    await require_ready(session, owner.club_id)
    url = await check_url(data.get("url") or "")
    events = check_events(data.get("events"))
    livemode = data.get("livemode")
    if owner.livemode is not None:
        if livemode is not None and bool(livemode) != owner.livemode:
            raise WebhookError(422, "VALIDATION", "An endpoint added with this key is in the key's own mode.", {"field": "livemode"})
        livemode = owner.livemode
    share = _check_sharing(owner, url, data.get("share_attendee_details"), bool(data.get("dpdp_confirmed")))
    count = await session.scalar(select(func.count(WebhookEndpoints.id)).where(_visible(owner)) if owner.is_partner else
                                 select(func.count(WebhookEndpoints.id)).where(
                                     WebhookEndpoints.club_id == owner.club_id, WebhookEndpoints.partner_app_id.is_(None),
                                     WebhookEndpoints.created_via != VIA_PARTNER))
    most = MAX_PARTNER_ENDPOINTS if owner.is_partner else MAX_ENDPOINTS
    if (count or 0) >= most:
        raise WebhookError(409, "LIMIT_REACHED", f"At most {most} webhook endpoints. Delete one you no longer use.")
    secret = crypto.new_secret()
    now = _now()
    client_id = owner.client_id if owner.via != VIA_DASHBOARD else ((data.get("client_id") or "").strip() or None)
    endpoint = WebhookEndpoints(
        club_id=owner.club_id, partner_app_id=owner.partner_app_id if owner.is_partner else None,
        owner_client_id=client_id, created_via=owner.via, url=url,
        description=(data.get("description") or "").strip()[:200] or None, events=events,
        include_own_changes=bool(data.get("include_own_changes")), livemode=True if livemode is None else bool(livemode),
        enabled=True, share_attendee_details=share, dpdp_confirmed_at=now if share else None,
        dpdp_confirmed_by=(owner.name or owner.via)[:120] if share else None,
        secret_enc=crypto.seal(secret), secret_hint=crypto.hint(secret),
        created_by_user_id=owner.user_id, created_by_name=(owner.name or None) and owner.name[:120],
        created_at=now, updated_at=now,
    )
    session.add(endpoint)
    await session.flush()
    return endpoint, secret


async def update(session, owner: Owner, endpoint_id, data: dict) -> WebhookEndpoints:
    endpoint = await get(session, owner, endpoint_id, for_change=True)
    if "url" in data and data["url"] is not None and data["url"].strip() != endpoint.url:
        endpoint.url = await check_url(data["url"])
        # The details were confirmed for the old address's system.
        if endpoint.share_attendee_details and not data.get("dpdp_confirmed"):
            endpoint.share_attendee_details, endpoint.dpdp_confirmed_at, endpoint.dpdp_confirmed_by = False, None, None
    if data.get("events") is not None:
        endpoint.events = check_events(data["events"])
    if data.get("description") is not None:
        endpoint.description = data["description"].strip()[:200] or None
    if data.get("include_own_changes") is not None:
        endpoint.include_own_changes = bool(data["include_own_changes"])
    if owner.via == VIA_DASHBOARD and "client_id" in data:
        endpoint.owner_client_id = (data.get("client_id") or "").strip() or None
    if data.get("share_attendee_details") is not None:
        share = _check_sharing(owner, endpoint.url, data["share_attendee_details"], bool(data.get("dpdp_confirmed")))
        endpoint.share_attendee_details = share
        endpoint.dpdp_confirmed_at = _now() if share else None
        endpoint.dpdp_confirmed_by = (owner.name or owner.via)[:120] if share else None
    if data.get("enabled") is not None and bool(data["enabled"]) != bool(endpoint.enabled):
        if data["enabled"]:
            await require_ready(session, owner.club_id)
            delivery.enable(endpoint)
        else:
            await delivery.disable(session, endpoint, BY_OWNER)
    endpoint.updated_at = _now()
    await session.flush()
    return endpoint


async def delete(session, owner: Owner, endpoint_id) -> None:
    endpoint = await get(session, owner, endpoint_id, for_change=True)
    await session.delete(endpoint)
    await session.flush()


async def rotate(session, owner: Owner, endpoint_id, keep_previous_hours: Optional[int] = None) -> tuple[WebhookEndpoints, str]:
    if not crypto.available():
        raise WebhookError(503, "WEBHOOKS_UNAVAILABLE", crypto.UNAVAILABLE_MESSAGE)
    endpoint = await get(session, owner, endpoint_id, for_change=True)
    hours = DEFAULT_OVERLAP_HOURS if keep_previous_hours is None else int(keep_previous_hours)
    if not 0 <= hours <= MAX_OVERLAP_HOURS:
        raise WebhookError(422, "VALIDATION", f"Keep the old secret 0 to {MAX_OVERLAP_HOURS} hours.", {"field": "keep_previous_hours"})
    secret = crypto.new_secret()
    now = _now()
    if hours:
        endpoint.previous_secret_enc = endpoint.secret_enc
        endpoint.previous_secret_expires_at = now + timedelta(hours=hours)
    else:
        endpoint.previous_secret_enc, endpoint.previous_secret_expires_at = None, None
    endpoint.secret_enc, endpoint.secret_hint, endpoint.secret_rotated_at = crypto.seal(secret), crypto.hint(secret), now
    endpoint.updated_at = now
    await session.flush()
    return endpoint, secret


async def test(session, owner: Owner, endpoint_id) -> dict:
    if not crypto.available():
        raise WebhookError(503, "WEBHOOKS_UNAVAILABLE", crypto.UNAVAILABLE_MESSAGE)
    endpoint = await get(session, owner, endpoint_id)
    row, result = await delivery.send_test(session, endpoint)
    event = await session.get(WebhookEvents, row.event_id)
    return {"ok": bool(result.ok), "delivery": delivery_out(row, event)}


# ─── The delivery log ────────────────────────────────────────────────────


def _cursor(row) -> str:
    raw = json.dumps([row.created_at.isoformat(), str(row.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _uncursor(cursor: Optional[str]):
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        when, rid = json.loads(raw)
        return datetime.fromisoformat(when), UUID(rid)
    except (ValueError, TypeError):
        raise WebhookError(422, "VALIDATION", "That cursor is not one this list gave.", {"field": "cursor"})


async def deliveries(session, owner: Owner, endpoint_id, *, limit: int = 25, cursor: Optional[str] = None,
                     status_filter: Optional[str] = None, event_type: Optional[str] = None) -> dict:
    endpoint = await get(session, owner, endpoint_id)
    limit = max(1, min(100, int(limit or 25)))
    stmt = (
        select(WebhookDeliveries, WebhookEvents)
        .join(WebhookEvents, WebhookEvents.id == WebhookDeliveries.event_id)
        .where(WebhookDeliveries.endpoint_id == endpoint.id)
    )
    if status_filter:
        if status_filter not in DELIVERY_STATUSES:
            raise WebhookError(422, "VALIDATION", f"status is one of {', '.join(DELIVERY_STATUSES)}.", {"field": "status"})
        stmt = stmt.where(WebhookDeliveries.status == status_filter)
    if event_type:
        stmt = stmt.where(WebhookEvents.type == event_type)
    after = _uncursor(cursor)
    if after is not None:
        stmt = stmt.where(or_(WebhookDeliveries.created_at < after[0],
                              and_(WebhookDeliveries.created_at == after[0], WebhookDeliveries.id < after[1])))
    rows = (await session.execute(stmt.order_by(WebhookDeliveries.created_at.desc(), WebhookDeliveries.id.desc()).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "data": [delivery_out(d, e) for d, e in rows],
        "next_cursor": _cursor(rows[-1][0]) if more and rows else None,
    }


async def _delivery(session, owner: Owner, delivery_id) -> tuple[WebhookDeliveries, WebhookEndpoints, WebhookEvents]:
    try:
        uid = UUID(str(delivery_id))
    except ValueError:
        raise WebhookError(404, "NOT_FOUND", "No such delivery.")
    row = await session.scalar(
        select(WebhookDeliveries).join(WebhookEndpoints, WebhookEndpoints.id == WebhookDeliveries.endpoint_id)
        .where(WebhookDeliveries.id == uid, _visible(owner))
    )
    if row is None:
        raise WebhookError(404, "NOT_FOUND", "No such delivery.")
    return row, await session.get(WebhookEndpoints, row.endpoint_id), await session.get(WebhookEvents, row.event_id)


async def delivery_detail(session, owner: Owner, delivery_id) -> dict:
    """One delivery, with the body as it is sent to its endpoint."""
    row, endpoint, event = await _delivery(session, owner, delivery_id)
    out = delivery_out(row, event)
    out["request_body"] = json.loads(await delivery.body_for(session, event, endpoint)) if event.data is not None else None
    return out


async def retry(session, owner: Owner, delivery_id) -> dict:
    row, endpoint, event = await _delivery(session, owner, delivery_id)
    if not owner.is_partner and _partner_owned(endpoint):
        raise WebhookError(403, "PARTNER_MANAGED", "This endpoint belongs to a connected app, which manages it.")
    if not endpoint.enabled:
        raise WebhookError(409, "ENDPOINT_DISABLED", "Turn the endpoint back on before sending to it again.")
    if not crypto.available():
        raise WebhookError(503, "WEBHOOKS_UNAVAILABLE", crypto.UNAVAILABLE_MESSAGE)
    if event.data is None:
        await delivery.fanout(session, event.id)
    result = await delivery.retry(session, row)
    row = await session.get(WebhookDeliveries, row.id, populate_existing=True)
    return {"ok": bool(result and result.ok), "delivery": delivery_out(row, event)}
