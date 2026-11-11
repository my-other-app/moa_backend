"""`emit`: a domain change, written down to be sent once it has committed.

Called by the change itself, wherever it happens (signals.py names each),
inside the caller's transaction:

    await emit(session, event.club_id, "event.published", Ref("event", event.id))

Nothing is sent from here. When the club has an endpoint that wants this
type (and the platform admin gave it API access), one `webhook_events` row
and one job are added to the caller's transaction -- the durable after-commit
of app/core/after_commit.py: a rollback (or a SAVEPOINT rolled back) takes
both away, so a change that did not happen is never told; a commit cannot
lose them. The job (delivery.fanout) runs a moment after the commit
(WEBHOOKS_SETTLE_SECONDS), builds the object as the change left it, and
queues it for each endpoint that wants it.

`object_builder` is a `Ref` (the object is loaded when it is sent: by then
every step of the save is in) or a dict (the object now: for one that will
be gone by then, a check-in undone).

`origin` is the change's source: the caller's when it knows better, else the
request's (origin.py, app/core/api_origin.py), else `system`.

Never raises: a change must never fail because of a webhook. Each write is
in a SAVEPOINT of its own, and whatever goes wrong is logged.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from sqlalchemy import event, select

from app.api.webhooks_out import types as wtypes
from app.core.api_origin import Origin

logger = logging.getLogger(__name__)

FANOUT_JOB = "webhooks.fanout"


@dataclass(frozen=True)
class Ref:
    """The object to send, by kind (objects.LOADERS) and id: loaded when sent."""

    kind: str
    id: Any


def new_event_id() -> str:
    return "evt_" + secrets.token_hex(12)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_CACHE = "webhooks_out:wanted"


def _forget(sync, *args) -> None:
    sync.info.pop(_CACHE, None)


def _cache(session) -> dict:
    """club id -> (its endpoints' subscriptions, allocated), for the length of
    the transaction: an import of thousands of rows asks once per club."""
    sync = getattr(session, "sync_session", session)
    cache = sync.info.get(_CACHE)
    if cache is None:
        cache = sync.info[_CACHE] = {}
        if not sync.info.get(_CACHE + ":hooked"):
            event.listen(sync, "after_commit", _forget)
            event.listen(sync, "after_rollback", _forget)
            sync.info[_CACHE + ":hooked"] = True
    return cache


async def wanted(session, club_id: Optional[int], event_type: str) -> bool:
    """The club has an endpoint on that wants this type (any mode; the
    fan-out matches the mode), it has API access, and webhooks can be signed."""
    from app.api.webhooks_out import crypto, seams
    from app.api.webhooks_out.models import WebhookEndpoints

    if club_id is None or not crypto.available():
        return False
    cache = _cache(session)
    if club_id not in cache:
        subscriptions = list(await session.scalars(
            select(WebhookEndpoints.events).where(WebhookEndpoints.club_id == club_id, WebhookEndpoints.enabled == True)  # noqa: E712
        ))
        cache[club_id] = (subscriptions, None)
    subscriptions, allocated = cache[club_id]
    if not any(wtypes.wants(s, event_type) for s in subscriptions):
        return False
    if allocated is None:
        allocated = await seams.club_allocated(session, club_id)
        cache[club_id] = (subscriptions, allocated)
    return bool(allocated)


def _is_session(value) -> bool:
    return hasattr(value, "execute") and hasattr(value, "flush") and hasattr(value, "begin_nested")


async def emit(*args, **kwargs) -> Optional[str]:
    """Write a change to be sent once it has committed. Two forms:

    - `await emit(session, club_id, type, object_builder, origin=None, *, previous=None, livemode=None)`:
      in the caller's transaction, before its commit (every domain change);
    - `await emit(club_id, type, object, origin=None, ...)`: the contract's
      form, for a caller whose change has committed already and has no
      transaction to share (the partner API's sandbox, whose test objects
      are not domain rows): written in a session of its own, committed now.

    Returns the change's id (evt_...), or None when nobody wants it (or it
    could not be written). Never raises."""
    if args and _is_session(args[0]):
        return await emit_in(*args, **kwargs)
    return await emit_committed(*args, **kwargs)


async def emit_committed(club_id: Optional[int], event_type: str, object_builder: Union[Ref, dict],
                         origin: Optional[Origin] = None, **kwargs) -> Optional[str]:
    from app.core import jobs

    try:
        async with jobs.scope() as session:
            written = await emit_in(session, club_id, event_type, object_builder, origin, **kwargs)
            if written:
                await session.commit()
            return written
    except Exception:  # noqa: BLE001 - see the module's doc
        logger.exception("webhooks: %s for club %s could not be written", event_type, club_id)
        return None


async def emit_in(
    session,
    club_id: Optional[int],
    event_type: str,
    object_builder: Union[Ref, dict],
    origin: Optional[Origin] = None,
    *,
    previous: Optional[dict] = None,
    livemode: Optional[bool] = None,
) -> Optional[str]:
    """Write the change in `session`'s transaction, to be sent after its commit."""
    try:
        if event_type not in wtypes.EVENT_TYPES:
            raise ValueError(f"unknown webhook type {event_type}")
        if not await wanted(session, club_id, event_type):
            return None
        return await _write(session, club_id, event_type, object_builder, origin, previous=previous, livemode=livemode)
    except Exception:  # noqa: BLE001 - see the module's doc
        logger.exception("webhooks: %s for club %s could not be written", event_type, club_id)
        return None


async def _write(session, club_id, event_type, object_builder, origin, *, previous=None, livemode=None,
                 only_endpoint_id=None, settle: Optional[float] = None) -> str:
    from app.api.webhooks_out import delivery  # noqa: F401 - registers the jobs' handlers
    from app.api.webhooks_out import origin as origins
    from app.api.webhooks_out.models import WebhookEvents
    from app.config import settings
    from app.core import jobs

    source = origins.resolve(origin)
    if isinstance(object_builder, Ref):
        kind, object_id, data = object_builder.kind, str(object_builder.id), None
    else:
        data = dict(object_builder)
        kind, object_id = str(data.get("object") or wtypes.object_of(event_type)), str(data.get("id") or data.get("number") or "")
        if livemode is None:
            livemode = data.get("livemode")
    event_id = new_event_id()
    delay = settings.WEBHOOKS_SETTLE_SECONDS if settle is None else settle
    async with session.begin_nested():
        session.add(WebhookEvents(
            id=event_id, club_id=club_id, type=event_type, object_type=kind[:20], object_id=object_id[:64],
            livemode=True if livemode is None else bool(livemode),
            origin_type=source.type, origin_client_id=(source.client_id or None),
            previous=previous or None, data=data, only_endpoint_id=only_endpoint_id, created_at=_now(),
        ))
        await session.flush()
        await jobs.enqueue(
            session, FANOUT_JOB, {"event_id": event_id},
            run_at=_now() + timedelta(seconds=max(0.0, float(delay or 0))) if delay else None,
            dedupe_key=f"{FANOUT_JOB}:{event_id}",
        )
    return event_id


# ─── Updates: what changed ───────────────────────────────────────────────


async def before(session, club_id: Optional[int], kind: str, object_id, event_type: str) -> Optional[dict]:
    """The object as it is, to tell later what changed -- or None when no
    endpoint of the club wants `event_type` (and nothing more is read)."""
    from app.api.webhooks_out import objects

    try:
        if not await wanted(session, club_id, event_type):
            return None
        return await objects.compare_form(session, kind, object_id)
    except Exception:  # noqa: BLE001
        logger.exception("webhooks: could not read %s %s before a change", kind, object_id)
        return None


async def changed(session, club_id: Optional[int], event_type: str, ref: Ref, was: Optional[dict],
                  origin: Optional[Origin] = None) -> Optional[str]:
    """Emit `event_type` with `previous` (the fields that changed, as they
    were) when anything did; nothing when `was` is None (nobody wanted it)
    or nothing changed."""
    from app.api.webhooks_out import objects

    if was is None:
        return None
    try:
        await session.flush()
        now = await objects.compare_form(session, ref.kind, ref.id)
        previous = objects.diff(was, now)
        if not previous:
            return None
    except Exception:  # noqa: BLE001
        logger.exception("webhooks: could not compare %s %s after a change", ref.kind, ref.id)
        return None
    return await emit(session, club_id, event_type, ref, origin, previous=previous)
