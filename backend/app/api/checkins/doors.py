"""Doors: the places people are admitted, and the links that open a
console on one."""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.checkins.models import CheckIns, DoorKind, Doors
from app.api.events.models import Events
from app.api.events.volunteer.models import Volunteer
from app.api.users.models import Users
from app.config import settings
from app.response import CustomHTTPException

LINK_DAYS = 30


def normalize(name: str) -> str:
    return " ".join((name or "").split())[:40]


async def by_name(session, event_id: int, name: str) -> Optional[Doors]:
    return await session.scalar(select(Doors).where(Doors.event_id == event_id, func.lower(Doors.name) == name.lower()))


async def resolve(session, event_id: int, label: Optional[str], kind: DoorKind = DoorKind.door) -> Optional[Doors]:
    """The door a label refers to, made if it is new. A label typed as
    "gate a" finds "Gate A"; nothing is refused for its spelling. No label,
    no door.

    Two first scans at a new door at once both make it: the insert yields
    to uq_doors_event_lower_name (ON CONFLICT DO NOTHING) and both read
    back the one door."""
    name = normalize(label or "")
    if not name:
        return None
    door = await by_name(session, event_id, name)
    if door is None:
        count = await session.scalar(select(func.count()).select_from(Doors).where(Doors.event_id == event_id)) or 0
        now = datetime.now(timezone.utc)
        await session.execute(
            pg_insert(Doors.__table__)
            .values(id=uuid.uuid4(), event_id=event_id, name=name, kind=kind, sort_order=count, active=True, link_can_walkup=False, created_at=now, updated_at=now)
            .on_conflict_do_nothing()
        )
        door = await by_name(session, event_id, name)
    return door


async def _save_name(session, door: Doors) -> None:
    """Flush a new or renamed door; a name another door has (in any case)
    is a 409, whichever request got there first."""
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise CustomHTTPException(409, message=f"There is already a door called “{door.name}”.", errors={"fields": {"name": "Pick a different name."}})


async def options(session, event_id: int) -> list[Doors]:
    return list(await session.scalars(select(Doors).where(Doors.event_id == event_id, Doors.active == True).order_by(Doors.sort_order, Doors.created_at)))


async def door_of(session, event: Events, door_id) -> Doors:
    door = await session.get(Doors, door_id)
    if door is None or door.event_id != event.id:
        raise CustomHTTPException(404, message="That door could not be found.")
    return door


def _link_row(door: Doors) -> Optional[dict]:
    if not door.link_hash:
        return None
    return {
        "prefix": door.link_prefix,
        "can_walkup": door.link_can_walkup,
        "expires_at": door.link_expires_at,
        "expired": bool(door.link_expires_at and door.link_expires_at < datetime.now(timezone.utc)),
    }


async def list_doors(session, event: Events) -> list[dict]:
    doors = list(await session.scalars(select(Doors).where(Doors.event_id == event.id).order_by(Doors.sort_order, Doors.created_at)))
    counts = dict((await session.execute(select(CheckIns.door_id, func.count()).where(CheckIns.event_id == event.id, CheckIns.door_id.isnot(None)).group_by(CheckIns.door_id))).all())
    team = (
        await session.execute(
            select(Volunteer.door_id, Volunteer.id, Users.full_name, Volunteer.email)
            .outerjoin(Users, Users.id == Volunteer.user_id)
            .where(Volunteer.door_id.isnot(None), Volunteer.is_deleted == False)
        )
    ).all()
    return [
        {
            "id": d.id,
            "event_id": d.event_id,
            "name": d.name,
            "kind": d.kind.value,
            "sort_order": d.sort_order,
            "active": d.active,
            "volunteers": [{"id": vid, "name": name or email} for did, vid, name, email in team if did == d.id],
            "check_ins": counts.get(d.id, 0),
            "link": _link_row(d),
            "created_at": d.created_at,
        }
        for d in doors
    ]


async def create_door(session, event: Events, *, name: str, kind: DoorKind) -> Doors:
    name = normalize(name)
    if not name:
        raise CustomHTTPException(422, message="Name the door — “Gate A”, “Loading bay”, “Front desk”.", errors={"fields": {"name": "Name the door."}})
    clash = await by_name(session, event.id, name)
    if clash is not None:
        raise CustomHTTPException(409, message=f"There is already a door called “{clash.name}”.", errors={"fields": {"name": "Pick a different name."}})
    count = await session.scalar(select(func.count()).select_from(Doors).where(Doors.event_id == event.id)) or 0
    door = Doors(id=uuid.uuid4(), event_id=event.id, name=name, kind=kind, sort_order=count, active=True)
    session.add(door)
    await _save_name(session, door)
    await session.commit()
    return door


async def update_door(session, event: Events, door_id, **changes) -> Doors:
    door = await door_of(session, event, door_id)
    name = changes.get("name")
    if name is not None:
        name = normalize(name)
        if not name:
            raise CustomHTTPException(422, message="Name the door.", errors={"fields": {"name": "Name the door."}})
        clash = await by_name(session, event.id, name)
        if clash is not None and clash.id != door.id:
            raise CustomHTTPException(409, message=f"There is already a door called “{clash.name}”.", errors={"fields": {"name": "Pick a different name."}})
        door.name = name
    if changes.get("kind") is not None:
        door.kind = DoorKind(changes["kind"])
    if changes.get("active") is False and door.active:
        others = await session.scalar(select(func.count()).select_from(Doors).where(Doors.event_id == event.id, Doors.active == True, Doors.id != door.id)) or 0
        if others == 0:
            raise CustomHTTPException(409, message="An event needs at least one open door. Add another before retiring this one.")
    if changes.get("active") is not None:
        door.active = bool(changes["active"])
    if changes.get("sort_order") is not None:
        door.sort_order = int(changes["sort_order"])
    if changes.get("link_can_walkup") is not None:
        door.link_can_walkup = bool(changes["link_can_walkup"])
    await _save_name(session, door)
    await session.commit()
    return door


async def delete_door(session, event: Events, door_id) -> None:
    door = await door_of(session, event, door_id)
    used = await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.door_id == door.id)) or 0
    if used:
        raise CustomHTTPException(409, message=f"{used} {'guest was' if used == 1 else 'guests were'} admitted at {door.name}. Retire it instead of deleting it, so the record keeps its door.")
    await session.execute(Volunteer.__table__.update().where(Volunteer.door_id == door.id).values(door_id=None))
    await session.delete(door)
    await session.commit()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def link_url(event_id: int, key: str) -> str:
    return f"{settings.PUBLIC_WEB_URL.rstrip('/')}/gate/{event_id}?key={key}"


async def issue_link(session, event: Events, door_id, *, can_walkup: bool = False, days: int = LINK_DAYS) -> dict:
    """A fresh key for this door. The old one stops working the moment a new
    one is made; the key itself is shown once and never stored."""
    door = await door_of(session, event, door_id)
    key = secrets.token_urlsafe(24)
    door.link_hash = _hash(key)
    door.link_prefix = key[:6]
    door.link_can_walkup = can_walkup
    door.link_expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    await session.commit()
    return {"url": link_url(event.id, key), "key": key, "door_id": door.id, "expires_at": door.link_expires_at, "can_walkup": can_walkup}


async def revoke_link(session, event: Events, door_id) -> Doors:
    door = await door_of(session, event, door_id)
    door.link_hash = None
    door.link_prefix = None
    door.link_expires_at = None
    await session.commit()
    return door


async def door_by_key(session, key: str) -> Optional[Doors]:
    if not key:
        return None
    door = await session.scalar(select(Doors).where(Doors.link_hash == _hash(key)))
    if door is None or not door.active:
        return None
    if door.link_expires_at and door.link_expires_at < datetime.now(timezone.utc):
        return None
    return door
