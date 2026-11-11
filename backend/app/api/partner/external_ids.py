"""Each client's own ids for our objects: at most 128 characters, unique per
(client, resource). Clients keep a mapping both ways without duplicates:
`GET ...?external_id=` finds the object, and a POST naming an external_id
already used answers 409 ALREADY_EXISTS with the existing object's id.

A POST reserves its external_id before the object is made (`reserve`), so
two requests racing with one id make one object; `bind` gives the
reservation the new object's id, `release` frees it when nothing was made.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.partner.common import aware, now
from app.api.partner.errors import PartnerError
from app.api.partner.models import PartnerExternalIds

RESOURCES = ("event", "registration", "checkin", "club")
MAX = 128
PENDING = "pending"
#: A reservation with no object this long after is taken to have failed.
STALE = timedelta(minutes=2)


def check(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > MAX or any(ord(c) < 32 for c in text):
        raise PartnerError("VALIDATION", f"external_id is 1 to {MAX} printable characters.", details={"fields": {"external_id": f"1 to {MAX} characters."}})
    return text


async def find(session, client_id: str, resource: str, external_id: str) -> Optional[str]:
    """The object id this client's external_id names (never a reservation still pending)."""
    object_id = await session.scalar(select(PartnerExternalIds.object_id).where(
        PartnerExternalIds.client_id == client_id, PartnerExternalIds.resource == resource,
        PartnerExternalIds.external_id == external_id,
    ))
    return None if object_id in (None, PENDING) else object_id


async def of_objects(session, client_id: str, resource: str, object_ids) -> dict:
    ids = [str(i) for i in object_ids if i is not None]
    if not ids:
        return {}
    rows = await session.execute(select(PartnerExternalIds.object_id, PartnerExternalIds.external_id).where(
        PartnerExternalIds.client_id == client_id, PartnerExternalIds.resource == resource, PartnerExternalIds.object_id.in_(ids),
    ))
    return dict(rows.all())


async def of_object(session, client_id: str, resource: str, object_id) -> Optional[str]:
    return (await of_objects(session, client_id, resource, [object_id])).get(str(object_id))


async def _taken(session, client_id: str, resource: str, external_id: str) -> PartnerError:
    row = await session.scalar(select(PartnerExternalIds).where(
        PartnerExternalIds.client_id == client_id, PartnerExternalIds.resource == resource,
        PartnerExternalIds.external_id == external_id,
    ))
    if row is not None and row.object_id == PENDING:
        return PartnerError("IDEMPOTENCY_IN_PROGRESS", f"A {resource} with this external_id is being created right now.")
    return PartnerError(
        "ALREADY_EXISTS", f"A {resource} with this external_id already exists.",
        details={"id": row.object_id if row is not None else None, "external_id": external_id},
    )


async def reserve(session, caller, resource: str, external_id: Optional[str]) -> Optional[int]:
    """Hold `external_id` for an object about to be made, or ALREADY_EXISTS. Commits."""
    if external_id is None:
        return None
    table = PartnerExternalIds.__table__
    # A reservation whose request died is let go first.
    await session.execute(delete(PartnerExternalIds).where(
        PartnerExternalIds.client_id == caller.client_id, PartnerExternalIds.resource == resource,
        PartnerExternalIds.external_id == external_id, PartnerExternalIds.object_id == PENDING,
        PartnerExternalIds.created_at < now() - STALE,
    ))
    row_id = (await session.execute(
        pg_insert(table).values(
            client_id=caller.client_id, resource=resource, external_id=external_id, object_id=PENDING, club_id=caller.club_id, created_at=now(),
        ).on_conflict_do_nothing(index_elements=[table.c.client_id, table.c.resource, table.c.external_id]).returning(table.c.id)
    )).scalar_one_or_none()
    if row_id is None:
        err = await _taken(session, caller.client_id, resource, external_id)
        await session.rollback()
        raise err
    await session.commit()
    return row_id


async def bind(session, reservation: Optional[int], object_id) -> None:
    if reservation is None:
        return
    await session.execute(update(PartnerExternalIds).where(PartnerExternalIds.id == reservation).values(object_id=str(object_id)))
    await session.commit()


async def release(session, reservation: Optional[int]) -> None:
    if reservation is None:
        return
    try:
        await session.rollback()
        await session.execute(delete(PartnerExternalIds).where(PartnerExternalIds.id == reservation, PartnerExternalIds.object_id == PENDING))
        await session.commit()
    except Exception:  # noqa: BLE001 - a stale reservation lets itself go
        await session.rollback()


async def set_for(session, caller, resource: str, object_id, external_id: Optional[str]) -> None:
    """A PATCH giving an object (another) external_id; None clears this client's. Not committed."""
    table = PartnerExternalIds.__table__
    if external_id is None:
        await session.execute(delete(PartnerExternalIds).where(
            PartnerExternalIds.client_id == caller.client_id, PartnerExternalIds.resource == resource,
            PartnerExternalIds.object_id == str(object_id),
        ))
        return
    existing = await session.scalar(select(PartnerExternalIds).where(
        PartnerExternalIds.client_id == caller.client_id, PartnerExternalIds.resource == resource,
        PartnerExternalIds.external_id == external_id,
    ))
    if existing is not None:
        if existing.object_id == str(object_id):
            return
        if existing.object_id != PENDING or aware(existing.created_at) > now() - STALE:
            raise await _taken(session, caller.client_id, resource, external_id)
        await session.delete(existing)
        await session.flush()
    await session.execute(delete(PartnerExternalIds).where(
        PartnerExternalIds.client_id == caller.client_id, PartnerExternalIds.resource == resource,
        PartnerExternalIds.object_id == str(object_id),
    ))
    await session.execute(pg_insert(table).values(
        client_id=caller.client_id, resource=resource, external_id=external_id, object_id=str(object_id), club_id=caller.club_id, created_at=now(),
    ))
