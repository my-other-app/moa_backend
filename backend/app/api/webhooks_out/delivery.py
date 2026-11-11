"""From a change written down (emit.py) to each endpoint that wants it.

The fan-out (job `webhooks.fanout`, a moment after the commit): the object
is built once, as the change left it, and a delivery is made for each of the
club's endpoints that is on, in the same mode (live or test), subscribed to
the type, not the change's own client (unless it opted in), and -- for a
partner's endpoint -- whose connection to the club still has the scope to
read that kind of object. One delivery per (change, endpoint), whatever runs
twice.

A delivery (job `webhooks.deliver`, one job per try):

- waits (a few seconds, a few times) while an older change to the same
  object for the same endpoint has not gone yet: in order per object, as
  far as that can be kept;
- is claimed by one conditional UPDATE, committed, before anything is sent;
  a try whose process stopped mid-send is taken again once it is clearly
  cut off (at least once);
- is sent with the contract's body and headers, signed (crypto.py), the
  object without contact fields unless the endpoint's owner may have them;
- 2xx is done. Anything else is tried again after 1 m, 5 m, 30 m, 2 h, 6 h,
  12 h and 24 h; after the eighth try it has failed (and can be sent again
  by hand from the log). An endpoint whose deliveries have failed for three
  days in a row, with no success between, is turned off -- what it still had
  to send is failed with it -- and its owner told (alerts.py).

A test (`send_test`) is one try, now, its answer returned; it never counts
towards turning an endpoint off. A retry by hand is one try, now.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.webhooks_out import alerts, crypto, objects, seams, sender
from app.api.webhooks_out import types as wtypes
from app.api.webhooks_out.emit import FANOUT_JOB
from app.api.webhooks_out.models import (
    BY_ADMIN, BY_OWNER, FAILED, FAILING, OPEN, PENDING, RETRYING, SUCCEEDED, WebhookDeliveries, WebhookEndpoints, WebhookEvents,
)
from app.core import jobs

logger = logging.getLogger(__name__)

DELIVER_JOB = "webhooks.deliver"
#: Waits before each retry, after try 1, 2, ... 7: then it has failed.
RETRY_SCHEDULE = (60, 5 * 60, 30 * 60, 2 * 3600, 6 * 3600, 12 * 3600, 24 * 3600)
MAX_ATTEMPTS = len(RETRY_SCHEDULE) + 1
#: Failing this long with no success between and the endpoint is turned off.
DISABLE_AFTER = timedelta(days=3)
#: In order per object: how long, and how often, a delivery waits for an older one.
ORDER_WAIT_SECONDS = 3.0
ORDER_MAX_WAITS = 10
#: A try claimed this long ago with nothing recorded was cut off (its process stopped).
CUT_OFF = timedelta(seconds=sender.TIMEOUT_SECONDS * 6)

#: Each object's read scope: a partner's endpoint gets the type only while
#: its connection to the club has it.
READ_SCOPES = {
    "event": "events:read", "registration": "registrations:read", "checkin": "registrations:read",
    "certificate": "certificates:read", "payment": "registrations:read", "club": "club:read",
}
ATTENDEES_SCOPE = "attendees:read"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_own(endpoint, origin_type: Optional[str], origin_client_id: Optional[str]) -> bool:
    """The change was made by the client the endpoint belongs to."""
    return bool(origin_client_id) and bool(endpoint.owner_client_id) and origin_client_id == endpoint.owner_client_id


def object_name(event: WebhookEvents) -> str:
    data = event.data or {}
    return str(data.get("object") or wtypes.object_of(event.type))


# ─── The fan-out ─────────────────────────────────────────────────────────


async def _partner_scopes(session, endpoint, cache: dict) -> frozenset:
    key = (endpoint.club_id, endpoint.partner_app_id, endpoint.owner_client_id)
    if key not in cache:
        cache[key] = frozenset(await seams.partner_scopes(session, endpoint.club_id, endpoint.partner_app_id, endpoint.owner_client_id))
    return cache[key]


async def fanout(session, event_id: str) -> int:
    """Build the change's object and make its deliveries. Returns how many."""
    event = await session.scalar(
        select(WebhookEvents).where(WebhookEvents.id == event_id, WebhookEvents.fanned_out_at.is_(None)).with_for_update(skip_locked=True)
    )
    if event is None:
        return 0
    if event.data is None:
        data = await objects.load(session, event.object_type, event.object_id)
        if data is None:
            data = {"object": wtypes.object_of(event.type), "id": event.object_id, "deleted": True}
        event.data = json.loads(json.dumps(data, default=str))
        event.livemode = bool(data.get("livemode", True))
    if event.only_endpoint_id is not None:
        endpoints = list(await session.scalars(select(WebhookEndpoints).where(WebhookEndpoints.id == event.only_endpoint_id)))
    else:
        endpoints = list(await session.scalars(
            select(WebhookEndpoints).where(WebhookEndpoints.club_id == event.club_id, WebhookEndpoints.enabled == True)  # noqa: E712
            .order_by(WebhookEndpoints.created_at)
        ))
    scope = READ_SCOPES.get(object_name(event))
    scopes_cache: dict = {}
    made: list = []
    for endpoint in endpoints:
        if event.only_endpoint_id is None:
            if not wtypes.wants(endpoint.events, event.type) or bool(endpoint.livemode) != bool(event.livemode):
                continue
            if not endpoint.include_own_changes and is_own(endpoint, event.origin_type, event.origin_client_id):
                continue
            if endpoint.partner_app_id is not None or endpoint.created_via == "partner_app":
                if scope is None or scope not in await _partner_scopes(session, endpoint, scopes_cache):
                    continue
        delivery_id = await session.scalar(
            pg_insert(WebhookDeliveries).values(
                event_id=event.id, endpoint_id=endpoint.id, club_id=event.club_id, status=PENDING, attempts=0,
                next_attempt_at=_now(), created_at=_now(), updated_at=_now(),
            ).on_conflict_do_nothing(constraint="uq_webhook_deliveries_event_endpoint").returning(WebhookDeliveries.id)
        )
        if delivery_id is not None:
            made.append(delivery_id)
    event.fanned_out_at = _now()
    if event.only_endpoint_id is None:
        for delivery_id in made:
            await _queue(session, delivery_id, 0)
    await session.commit()
    return len(made)


async def _queue(session, delivery_id, attempt: int, *, run_at: Optional[datetime] = None, tag: str = "", manual: bool = False) -> None:
    await jobs.enqueue(
        session, DELIVER_JOB, {"delivery_id": str(delivery_id), "attempt": attempt, "manual": manual},
        run_at=run_at, dedupe_key=f"{DELIVER_JOB}:{delivery_id}:{attempt}{tag}",
    )


# ─── The body ────────────────────────────────────────────────────────────


async def may_see_attendees(session, endpoint) -> bool:
    """A club's own endpoint: the club ticked the DPDP box for it. A
    partner's: its connection to the club has attendees:read."""
    if endpoint.partner_app_id is None and endpoint.created_via != "partner_app":
        return bool(endpoint.share_attendee_details and endpoint.dpdp_confirmed_at is not None)
    scopes = await seams.partner_scopes(session, endpoint.club_id, endpoint.partner_app_id, endpoint.owner_client_id)
    return ATTENDEES_SCOPE in set(scopes or ())


async def body_for(session, event: WebhookEvents, endpoint) -> bytes:
    data = dict(event.data or {})
    previous = dict(event.previous) if event.previous else None
    name = object_name(event)
    if name in wtypes.WITH_CONTACTS and not await may_see_attendees(session, endpoint):
        # What changed is redacted as the object is: an old email is still an email.
        data = objects.redact(name, data)
        previous = objects.redact(name, previous) or None
    if "external_id" in data and endpoint.owner_client_id:
        data["external_id"] = await seams.external_id(session, endpoint.owner_client_id, name, data.get("id"))
    envelope = {
        "id": event.id,
        "type": event.type,
        "created_at": objects.when(event.created_at),
        "livemode": bool(event.livemode),
        "club_id": str(event.club_id),
        "origin": {"type": event.origin_type, **({"client_id": event.origin_client_id} if event.origin_client_id else {})},
        "data": {"object": data, **({"previous": previous} if previous else {})},
    }
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def signing_secrets(endpoint, now: datetime) -> list[str]:
    """The current secret, and during a rotation's overlap the old one."""
    out = [crypto.open_(endpoint.secret_enc)]
    if endpoint.previous_secret_enc and endpoint.previous_secret_expires_at and endpoint.previous_secret_expires_at > now:
        try:
            out.append(crypto.open_(endpoint.previous_secret_enc))
        except crypto.Unavailable:
            pass
    return out


def headers_for(event: WebhookEvents, delivery_id, body: bytes, secrets_: list[str], attempt: int) -> dict:
    timestamp = int(time.time())
    return {
        crypto.SIGNATURE_HEADER: crypto.header(secrets_, timestamp, body),
        crypto.EVENT_ID_HEADER: event.id,
        "MOA-Event-Type": event.type,
        "MOA-Delivery-Id": str(delivery_id),
        "MOA-Delivery-Attempt": str(attempt),
        "Content-Type": "application/json",
    }


# ─── One try ─────────────────────────────────────────────────────────────


async def _older_waiting(session, event: WebhookEvents, endpoint_id) -> bool:
    """An older change to the same object that this endpoint has not had yet:
    still to be built, or built and its delivery here not tried."""
    older = and_(
        WebhookEvents.club_id == event.club_id, WebhookEvents.object_type == event.object_type,
        WebhookEvents.object_id == event.object_id, WebhookEvents.seq < event.seq,
    )
    unbuilt = await session.scalar(
        select(WebhookEvents.id).where(older, WebhookEvents.fanned_out_at.is_(None), WebhookEvents.only_endpoint_id.is_(None)).limit(1)
    )
    if unbuilt is not None:
        return True
    untried = await session.scalar(
        select(WebhookDeliveries.id).join(WebhookEvents, WebhookEvents.id == WebhookDeliveries.event_id)
        .where(older, WebhookDeliveries.endpoint_id == endpoint_id, WebhookDeliveries.status == PENDING,
               WebhookDeliveries.attempts == 0).limit(1)
    )
    return untried is not None


async def _claim(session, delivery_id, attempt: int):
    """Take try `attempt + 1`: one conditional UPDATE, committed before
    anything is sent. A try claimed long ago with nothing recorded (its
    process stopped mid-send) is taken again as the same try."""
    now = _now()
    taken = await session.scalar(
        update(WebhookDeliveries)
        .where(
            WebhookDeliveries.id == delivery_id, WebhookDeliveries.status.in_(OPEN),
            or_(
                WebhookDeliveries.attempts == attempt,
                and_(WebhookDeliveries.attempts == attempt + 1, WebhookDeliveries.last_attempt_at <= now - CUT_OFF),
            ),
        )
        .values(attempts=attempt + 1, last_attempt_at=now, next_attempt_at=None, updated_at=now)
        .returning(WebhookDeliveries.id)
    )
    await session.commit()
    if taken is None:
        return None
    return await session.get(WebhookDeliveries, delivery_id, populate_existing=True)


async def attempt(session, delivery_id, attempt_no: int, *, manual: bool = False, test: bool = False) -> Optional[sender.Result]:
    """One try of a delivery. None when there was nothing to try (sent
    already, a stale job, an endpoint turned off, or a wait for order)."""
    delivery = await session.get(WebhookDeliveries, delivery_id, populate_existing=True)
    if delivery is None or delivery.status not in OPEN:
        return None
    endpoint = await session.get(WebhookEndpoints, delivery.endpoint_id, populate_existing=True)
    event = await session.get(WebhookEvents, delivery.event_id)
    if endpoint is None or event is None:
        return None
    if not endpoint.enabled and not test:
        delivery.status, delivery.error, delivery.next_attempt_at = FAILED, "Not sent: the endpoint is turned off.", None
        await session.commit()
        return None
    if attempt_no == 0 and not manual and not test and delivery.waits < ORDER_MAX_WAITS and await _older_waiting(session, event, endpoint.id):
        delivery.waits = (delivery.waits or 0) + 1
        run_at = _now() + timedelta(seconds=ORDER_WAIT_SECONDS)
        delivery.next_attempt_at = run_at
        await _queue(session, delivery.id, 0, run_at=run_at, tag=f":w{delivery.waits}")
        await session.commit()
        return None
    claimed = await _claim(session, delivery.id, attempt_no)
    if claimed is None:
        return None
    delivery = claimed
    try:
        body = await body_for(session, event, endpoint)
        secrets_ = signing_secrets(endpoint, _now())
    except crypto.Unavailable:
        result = sender.Result(ok=False, error="Not sent: the server cannot sign it (no usable APP_WEBHOOK_KEYS_SECRET).")
        await _record(session, delivery.id, result, manual=manual, test=test, platform=True)
        return result
    headers = headers_for(event, delivery.id, body, secrets_, delivery.attempts)
    # Nothing held open while the endpoint answers.
    await session.commit()
    result = await sender.post(endpoint.url, body, headers)
    await _record(session, delivery.id, result, manual=manual, test=test)
    return result


async def _record(session, delivery_id, result: sender.Result, *, manual: bool = False, test: bool = False, platform: bool = False) -> None:
    now = _now()
    delivery = await session.get(WebhookDeliveries, delivery_id, populate_existing=True)
    endpoint = await session.scalar(
        select(WebhookEndpoints).where(WebhookEndpoints.id == delivery.endpoint_id).with_for_update().execution_options(populate_existing=True)
    )
    delivery.response_status = result.status
    delivery.response_body = (result.body or None) and result.body[: sender.RESPONSE_BODY_MAX]
    delivery.error = (result.error or None) and result.error[:300]
    delivery.duration_ms = result.duration_ms
    delivery.updated_at = now
    if result.ok:
        delivery.status, delivery.succeeded_at, delivery.next_attempt_at = SUCCEEDED, now, None
        if not test and endpoint is not None:
            endpoint.last_success_at, endpoint.failing_since, endpoint.consecutive_failures = now, None, 0
        await session.commit()
        return
    counts = not test and not platform and endpoint is not None
    if counts:
        endpoint.last_failure_at = now
        endpoint.failing_since = endpoint.failing_since or now
        endpoint.consecutive_failures = (endpoint.consecutive_failures or 0) + 1
    if test or manual or delivery.attempts >= MAX_ATTEMPTS:
        delivery.status, delivery.next_attempt_at = FAILED, None
    else:
        delivery.status = RETRYING
        delivery.next_attempt_at = now + timedelta(seconds=RETRY_SCHEDULE[delivery.attempts - 1])
        await _queue(session, delivery.id, delivery.attempts, run_at=delivery.next_attempt_at)
    if counts and endpoint.enabled and endpoint.failing_since <= now - DISABLE_AFTER:
        await disable(session, endpoint, FAILING, now=now)
    await session.commit()


# ─── Turning an endpoint off ─────────────────────────────────────────────


async def disable(session, endpoint, reason: str, *, now: Optional[datetime] = None, tell: bool = True) -> None:
    """Off, with what it still had to send failed with it. For `failing`, its
    owner is told (alerts.py). In the caller's transaction."""
    now = now or _now()
    endpoint.enabled = False
    endpoint.disabled_reason = reason
    endpoint.disabled_at = now
    await session.execute(
        update(WebhookDeliveries)
        .where(WebhookDeliveries.endpoint_id == endpoint.id, WebhookDeliveries.status.in_(OPEN))
        .values(status=FAILED, next_attempt_at=None, updated_at=now,
                error="Not sent: the endpoint was turned off." if reason != FAILING else "Not sent: the endpoint was turned off after failing for three days.")
    )
    if reason == FAILING and tell:
        await alerts.endpoint_disabled(session, endpoint)


def enable(endpoint) -> None:
    endpoint.enabled = True
    endpoint.disabled_reason = None
    endpoint.disabled_at = None
    endpoint.failing_since = None
    endpoint.consecutive_failures = 0


# ─── By hand ─────────────────────────────────────────────────────────────


async def retry(session, delivery) -> Optional[sender.Result]:
    """Send a delivery again, now: one try, whatever it was (replay)."""
    now = _now()
    delivery.status = RETRYING if delivery.attempts else PENDING
    delivery.next_attempt_at = now
    await session.commit()
    return await attempt(session, delivery.id, delivery.attempts, manual=True)


async def send_test(session, endpoint) -> tuple[WebhookDeliveries, sender.Result]:
    """A `webhook.test` change to this endpoint alone, sent now, logged with
    its deliveries; its answer returned."""
    from app.api.webhooks_out.emit import _write

    data = {
        "object": "test", "id": str(endpoint.id),
        "message": "A test from MyOtherApp. If you can read this, your endpoint works.",
        "livemode": bool(endpoint.livemode),
    }
    event_id = await _write(session, endpoint.club_id, wtypes.TEST_TYPE, data, None, livemode=endpoint.livemode,
                            only_endpoint_id=endpoint.id, settle=0)
    await session.commit()
    await fanout(session, event_id)
    delivery = await session.scalar(select(WebhookDeliveries).where(WebhookDeliveries.event_id == event_id))
    result = await attempt(session, delivery.id, 0, test=True)
    delivery = await session.get(WebhookDeliveries, delivery.id, populate_existing=True)
    return delivery, result or sender.Result(ok=False, error="Not sent.")


# ─── The jobs ────────────────────────────────────────────────────────────


@jobs.handler(FANOUT_JOB, max_attempts=6, timeout=60.0, priority=jobs.NORMAL)
async def _fanout_job(payload: dict) -> None:
    async with jobs.scope() as session:
        await fanout(session, str(payload["event_id"]))


@jobs.handler(DELIVER_JOB, max_attempts=3, timeout=sender.TIMEOUT_SECONDS + 30.0, priority=jobs.NORMAL)
async def _deliver_job(payload: dict) -> None:
    async with jobs.scope() as session:
        await attempt(session, payload["delivery_id"], int(payload.get("attempt") or 0), manual=bool(payload.get("manual")))


__all__ = ["BY_ADMIN", "BY_OWNER", "DELIVER_JOB", "FANOUT_JOB", "attempt", "disable", "enable", "fanout", "retry", "send_test"]
