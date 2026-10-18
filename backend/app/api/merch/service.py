"""Merchandise: items and their stock, what a guest is owed, the handover, the log."""
import io
import os
from typing import Optional
from uuid import UUID, uuid4

from PIL import Image
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.checkins import doors as doors_mod
from app.api.checkins.gate_auth import GateActor
from app.api.checkins.models import Doors
from app.api.checkins.service import registration_of
from app.api.events.documents import EVENT_TIMEZONE, ROLE_LABELS
from app.api.events.models import EventRegistrationsLink, Events
from app.api.merch import rules
from app.api.merch.models import MerchCollections, MerchItems, MerchVariants
from app.api.users.models import Users
from app.config import settings
from app.core.storage import objects
from app.response import CustomHTTPException

# ─── Items ────────────────────────────────────────────────────────────────


def _total(m: MerchItems) -> int:
    return sum(v.stock for v in m.variants) if m.variant_field else (m.stock or 0)


def item_out(m: MerchItems) -> dict:
    return {
        "id": m.id, "event_id": m.event_id, "name": m.name, "photo_key": m.photo_key,
        "photo_url": objects.presigned_url(m.photo_key) if m.photo_key else None,
        "stock": m.stock, "collected": m.collected_count, "total": _total(m),
        "eligibility_field": m.eligibility_field, "eligibility_value": m.eligibility_value, "variant_field": m.variant_field,
        "variants": [{"id": v.id, "label": v.label, "stock": v.stock, "collected": v.collected_count} for v in m.variants],
        "sponsor": m.sponsor, "handout_rule": m.handout_rule, "manual_handed_out": m.manual_handed_out or 0,
        "handed_out": m.collected_count + (m.manual_handed_out or 0),
        "created_at": m.created_at,
    }


def _query():
    return select(MerchItems).options(selectinload(MerchItems.variants))


async def _item(session, event: Events, item_id: UUID) -> MerchItems:
    # Fresh from the database every time: a variant just added or removed shows in the list returned.
    m = await session.scalar(_query().execution_options(populate_existing=True).where(MerchItems.id == item_id, MerchItems.event_id == event.id))
    if m is None:
        raise CustomHTTPException(404, message="That item could not be found.")
    return m


async def rows_for(session, event_id: int) -> list:
    return list((await session.scalars(_query().where(MerchItems.event_id == event_id).order_by(MerchItems.created_at, MerchItems.id))).all())


async def list_items(session, event_id: int) -> list:
    return [item_out(m) for m in await rows_for(session, event_id)]


async def has_merch(session, event_id: int) -> bool:
    return bool(await session.scalar(select(func.count()).select_from(MerchItems).where(MerchItems.event_id == event_id)))


def fields_for(event: Events) -> list:
    """What an item may key eligibility or sizes on: the role, and each guest field."""
    out = [{"name": "Role", "key": "role", "kind": "role", "values": list(ROLE_LABELS.values())}]
    for f in event.additional_details or []:
        if isinstance(f, dict) and f.get("key") and f.get("label"):
            options = [o for o in (f.get("options") or []) if isinstance(o, str)]
            out.append({"name": f["label"], "key": f["key"], "kind": "choice" if options else "text", "values": options})
    return out


async def create_item(session, event: Events, body) -> dict:
    m = MerchItems(
        event_id=event.id, name=body.name, photo_key=body.photo_key,
        stock=None if body.variant_field else (body.stock or 0),
        eligibility_field=body.eligibility_field, eligibility_value=body.eligibility_value, variant_field=body.variant_field,
        sponsor=(body.sponsor or None), handout_rule=(body.handout_rule or None),
    )
    if body.variant_field:
        m.variants = [MerchVariants(label=v.label, stock=v.stock) for v in body.variants]
    session.add(m)
    await session.commit()
    return item_out(await _item(session, event, m.id))


async def update_item(session, event: Events, item_id: UUID, body) -> dict:
    """Renaming a variant field re-matches it against the guest list at the next lookup; nobody silently loses a claim."""
    m = await _item(session, event, item_id)
    data = body.model_dump(exclude_unset=True)
    nxt = {
        "stock": data.get("stock", m.stock), "variant_field": data.get("variant_field", m.variant_field),
        "eligibility_field": data.get("eligibility_field", m.eligibility_field), "eligibility_value": data.get("eligibility_value", m.eligibility_value),
    }
    if nxt["variant_field"] and nxt["stock"] is not None:
        raise CustomHTTPException(422, message="An item has its own stock or variants, never both.", error_code="MERCH_STOCK_XOR_VARIANTS")
    if not nxt["variant_field"] and nxt["stock"] is None:
        raise CustomHTTPException(422, message="Without variants, the item needs its own stock.", error_code="MERCH_STOCK_XOR_VARIANTS")
    if bool(nxt["eligibility_field"]) != bool(nxt["eligibility_value"]):
        raise CustomHTTPException(422, message="Set both the field and the value, or neither.", error_code="MERCH_ELIGIBILITY_INCOMPLETE")
    if nxt["stock"] is not None and nxt["stock"] < m.collected_count:
        raise CustomHTTPException(422, message=f"{m.collected_count} have already been handed out, so stock cannot go below that.")
    for k in ("name", "photo_key", "sponsor", "handout_rule"):
        if k in data:
            setattr(m, k, data[k])
    for k, v in nxt.items():
        setattr(m, k, v)
    await session.commit()
    return item_out(await _item(session, event, m.id))


async def delete_item(session, event: Events, item_id: UUID, with_collections: bool = False) -> None:
    """An item nobody has collected simply goes. One that has been handed
    out goes only when asked for explicitly, and takes its collections with
    it: the guests keep what they were given; the record of it is removed."""
    m = await _item(session, event, item_id)
    if m.collected_count > 0 and not with_collections:
        raise CustomHTTPException(409, message=f"{m.collected_count} have already been handed out. Delete it with its collections, or set its stock to what was given instead.", error_code="ITEM_HAS_COLLECTIONS")
    await session.execute(delete(MerchCollections).where(MerchCollections.item_id == m.id))
    await session.delete(m)
    await session.commit()


async def add_variant(session, event: Events, item_id: UUID, body) -> dict:
    m = await _item(session, event, item_id)
    if not m.variant_field:
        raise CustomHTTPException(422, message="This item has its own stock. Turn on sizes before adding variants.", error_code="MERCH_STOCK_XOR_VARIANTS")
    if any(rules.norm(v.label) == rules.norm(body.label) for v in m.variants):
        raise CustomHTTPException(409, message="A variant with that label exists.")
    session.add(MerchVariants(item_id=m.id, label=body.label, stock=body.stock))
    await session.commit()
    return item_out(await _item(session, event, m.id))


def _variant(m: MerchItems, variant_id: UUID) -> MerchVariants:
    v = next((v for v in m.variants if v.id == variant_id), None)
    if v is None:
        raise CustomHTTPException(404, message="That variant could not be found.")
    return v


async def update_variant(session, event: Events, item_id: UUID, variant_id: UUID, body) -> dict:
    m = await _item(session, event, item_id)
    v = _variant(m, variant_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("label") and any(o.id != v.id and rules.norm(o.label) == rules.norm(data["label"]) for o in m.variants):
        raise CustomHTTPException(409, message="A variant with that label exists.")
    if data.get("stock") is not None and data["stock"] < v.collected_count:
        raise CustomHTTPException(422, message=f"{v.collected_count} have already been handed out in {v.label}, so stock cannot go below that.")
    for k, val in data.items():
        if val is not None:
            setattr(v, k, val.strip() if isinstance(val, str) else val)
    await session.commit()
    return item_out(await _item(session, event, m.id))


async def delete_variant(session, event: Events, item_id: UUID, variant_id: UUID) -> dict:
    m = await _item(session, event, item_id)
    v = _variant(m, variant_id)
    if v.collected_count > 0:
        raise CustomHTTPException(409, message=f"{v.collected_count} have already been handed out in {v.label}. Set its stock to what was given instead.")
    await session.delete(v)
    await session.commit()
    return item_out(await _item(session, event, m.id))


MAX_PHOTO_BYTES = 5 * 1024 * 1024
_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def store_photo(event_id: int, raw: bytes) -> dict:
    """A product photo, stored under the event; answered with the key an item saves and a URL to show it now."""
    if len(raw) > MAX_PHOTO_BYTES:
        raise CustomHTTPException(413, message="That image is over 5 MB. Export it smaller and try again.")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    fmt = (image.format or "").upper()
    if fmt not in _EXT:
        raise CustomHTTPException(422, message="Use PNG, JPEG or WebP.")
    if image.width > 1200:
        image = image.resize((1200, round(image.height * 1200 / image.width)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if fmt == "JPEG":
            image.convert("RGB").save(out, "JPEG", quality=88)
        else:
            image.save(out, "PNG")
            fmt = "PNG"
        raw = out.getvalue()
    ext = _EXT[fmt]
    key = os.path.join(settings.S3_BASE_PATH, "events", str(event_id), "merch") + f"/{uuid4().hex}.{ext}"
    objects.put_bytes(key, raw, "image/jpeg" if ext == "jpg" else f"image/{ext}")
    return {"key": key, "url": objects.presigned_url(key)}


# ─── At the gate ──────────────────────────────────────────────────────────


def _attendee(reg: EventRegistrationsLink) -> dict:
    role = getattr(reg.role, "value", reg.role) or "attendee"
    return {"id": reg.id, "name": reg.full_name or "", "ticket_id": reg.ticket_id, "role": str(role), "role_label": rules.role_label(reg)}


async def _collected(session, registration_id) -> set:
    return set((await session.scalars(select(MerchCollections.item_id).where(MerchCollections.registration_id == registration_id))).all())


async def status_for(session, event: Events, registration_id) -> dict:
    """What this guest is owed, item by item, from their own record."""
    reg = await registration_of(session, event, registration_id)
    collected = await _collected(session, reg.id)
    rows = []
    for m in await rows_for(session, event.id):
        r = rules.resolve(m, reg, event, m.id in collected)
        can_pick = r.status in (rules.NO_VARIANT_VALUE, rules.NO_MATCHING_VARIANT)
        rows.append({
            "item_id": m.id, "name": m.name, "photo_url": objects.presigned_url(m.photo_key) if m.photo_key else None,
            "status": r.status, "variant_label": r.variant_label, "remaining": r.remaining,
            "on_file": rules.field_value(reg, event, m.variant_field) if m.variant_field else None,
            # Offered only when their record cannot answer: the volunteer completes it, then collects.
            "choices": [{"label": v.label, "remaining": max(0, v.stock - v.collected_count)} for v in m.variants] if can_pick else [],
        })
    return {"attendee": _attendee(reg), "items": rows}


async def _record_door_choice(session, event: Events, m: MerchItems, reg: EventRegistrationsLink, label: str) -> None:
    """A size chosen at the door goes onto the guest's record before anything
    is handed over, and only when the record has no usable size of its own,
    so the rule "resolved from their own record" holds."""
    variant = next((v for v in m.variants if rules.norm(v.label) == rules.norm(label)), None)
    if variant is None:
        raise CustomHTTPException(422, message="That is not one of the sizes set up for this item.")
    current = rules.field_value(reg, event, m.variant_field)
    if current and any(rules.norm(v.label) == rules.norm(current) for v in m.variants):
        return
    key = rules.field_key(event, m.variant_field)
    details = dict(reg.additional_details) if isinstance(reg.additional_details, dict) else {}
    details = {k: v for k, v in details.items() if rules.norm(k) != rules.norm(key)}
    details[key] = variant.label
    reg.additional_details = details
    await session.commit()


def _result(status: str, collected: bool = False, variant_label: Optional[str] = None, remaining: Optional[int] = None) -> dict:
    return {"status": status, "collected": collected, "variant_label": variant_label, "remaining": remaining}


async def collect(session, event: Events, item_id: UUID, registration_id, actor: GateActor, *, variant_label: Optional[str] = None, door_label: Optional[str] = None) -> dict:
    """Eligibility and variant are re-derived from the guest's own record. The
    conditional stock decrement and the collection row share one
    transaction, so a blocked duplicate never silently consumes stock and
    two people racing for the last unit cannot both take it."""
    m = await _item(session, event, item_id)
    reg = await registration_of(session, event, registration_id)
    already = m.id in await _collected(session, reg.id)
    if variant_label and not already and m.variant_field:
        await _record_door_choice(session, event, m, reg, variant_label)
    r = rules.resolve(m, reg, event, already)
    if r.status != rules.COLLECTABLE:
        return _result(r.status, False, r.variant_label, r.remaining)
    door = await doors_mod.resolve(session, event.id, door_label) if door_label else None
    try:
        if r.variant_id is not None:
            row = (await session.execute(
                update(MerchVariants).where(MerchVariants.id == r.variant_id, MerchVariants.collected_count < MerchVariants.stock)
                .values(collected_count=MerchVariants.collected_count + 1).returning(MerchVariants.stock, MerchVariants.collected_count)
            )).first()
            if row is None:
                await session.rollback()
                return _result(rules.OUT_OF_STOCK, False, r.variant_label, 0)
            await session.execute(update(MerchItems).where(MerchItems.id == m.id).values(collected_count=MerchItems.collected_count + 1))
        else:
            # Pieces given out by hand come off the same stock.
            row = (await session.execute(
                update(MerchItems).where(MerchItems.id == m.id, MerchItems.collected_count + MerchItems.manual_handed_out < MerchItems.stock)
                .values(collected_count=MerchItems.collected_count + 1)
                .returning(MerchItems.stock, MerchItems.collected_count + MerchItems.manual_handed_out)
            )).first()
            if row is None:
                await session.rollback()
                return _result(rules.OUT_OF_STOCK, False, None, 0)
        left = row[0] - row[1]
        session.add(MerchCollections(
            registration_id=reg.id, item_id=m.id, variant_id=r.variant_id,
            door_label=door.name if door is not None else (door_label or None), door_id=door.id if door is not None else None,
            collected_by=actor.user_id,
        ))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _result(rules.ALREADY_COLLECTED)
    return _result(rules.COLLECTABLE, True, r.variant_label, left)


# ─── The log and the counts ───────────────────────────────────────────────


def _door_of(door: Optional[Doors], label: Optional[str]) -> Optional[dict]:
    if door is not None:
        return {"id": door.id, "name": door.name, "kind": door.kind.value}
    if label:
        return {"id": None, "name": label, "kind": "desk"}
    return None


async def log(session, event: Events, *, q: Optional[str] = None, item_id=None, variant_id=None, door_id=None, by: Optional[int] = None, order: str = "newest", page: int = 1, page_size: int = 50) -> dict:
    """Every handover, row by row; the facets always describe the whole event
    so a chip never vanishes because a filter is on."""
    base = (
        select(MerchCollections, EventRegistrationsLink, MerchItems, MerchVariants, Doors, Users)
        .select_from(MerchCollections)
        .join(EventRegistrationsLink, EventRegistrationsLink.id == MerchCollections.registration_id)
        .join(MerchItems, MerchItems.id == MerchCollections.item_id)
        .outerjoin(MerchVariants, MerchVariants.id == MerchCollections.variant_id)
        .outerjoin(Doors, Doors.id == MerchCollections.door_id)
        .outerjoin(Users, Users.id == MerchCollections.collected_by)
        .where(MerchItems.event_id == event.id)
    )
    if q:
        pat = f"%{q.strip()}%"
        base = base.where(or_(EventRegistrationsLink.full_name.ilike(pat), EventRegistrationsLink.email.ilike(pat), EventRegistrationsLink.ticket_id.ilike(pat)))
    if item_id is not None:
        base = base.where(MerchCollections.item_id == item_id)
    if variant_id is not None:
        base = base.where(MerchCollections.variant_id == variant_id)
    if door_id is not None:
        base = base.where(MerchCollections.door_id == door_id)
    if by is not None:
        base = base.where(MerchCollections.collected_by == by)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    direction = MerchCollections.collected_at.asc() if order == "oldest" else MerchCollections.collected_at.desc()
    rows = (await session.execute(base.order_by(direction, MerchCollections.id).limit(page_size).offset((page - 1) * page_size))).all()
    items = []
    for c, reg, m, v, door, user in rows:
        role = getattr(reg.role, "value", reg.role) or "attendee"
        items.append({
            "id": c.id, "registration_id": reg.id, "name": reg.full_name or "", "ticket_id": reg.ticket_id, "role": str(role),
            "item": {"id": m.id, "name": m.name}, "variant": {"id": v.id, "label": v.label} if v is not None else None,
            "door": _door_of(door, c.door_label), "collected_at": c.collected_at,
            "by": {"kind": "PERSON" if user else "NOBODY", "id": user.id if user else None, "name": (user.full_name or user.email) if user else None},
        })
    by_item = (await session.execute(select(MerchItems.id, MerchItems.name, func.count()).select_from(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id).group_by(MerchItems.id, MerchItems.name).order_by(func.count().desc()))).all()
    by_variant = (await session.execute(select(MerchVariants.id, MerchVariants.item_id, MerchVariants.label, func.count()).select_from(MerchCollections).join(MerchVariants, MerchVariants.id == MerchCollections.variant_id).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id).group_by(MerchVariants.id, MerchVariants.item_id, MerchVariants.label))).all()
    by_door = (await session.execute(select(MerchCollections.door_id, MerchCollections.door_label, Doors.name, Doors.kind, func.count()).select_from(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).outerjoin(Doors, Doors.id == MerchCollections.door_id).where(MerchItems.event_id == event.id).group_by(MerchCollections.door_id, MerchCollections.door_label, Doors.name, Doors.kind).order_by(func.count().desc()))).all()
    staff = (await session.execute(select(Users.id, Users.full_name, Users.email, func.count()).select_from(MerchCollections).join(Users, Users.id == MerchCollections.collected_by).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id).group_by(Users.id, Users.full_name, Users.email).order_by(func.count().desc()))).all()
    bounds = (await session.execute(select(func.min(MerchCollections.collected_at), func.max(MerchCollections.collected_at), func.count()).select_from(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id))).one()
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "facets": {
            "total": bounds[2] or 0,
            "items": [{"id": i, "name": n, "count": c} for i, n, c in by_item],
            "variants": [{"id": vid, "item_id": iid, "label": label, "count": c} for vid, iid, label, c in by_variant],
            "doors": [{"id": did, "name": name or label, "kind": kind.value if kind else "desk", "count": c} for did, label, name, kind, c in by_door if did or label],
            "staff": [{"id": uid, "name": name or email, "count": c} for uid, name, email, c in staff],
            "without_door": sum(c for did, label, _n, _k, c in by_door if not did and not label),
            "first_at": bounds[0], "last_at": bounds[1],
        },
    }


async def analytics(session, event: Events) -> dict:
    """The same handovers, counted. Stock sits beside collected so a glance
    says what is left; the hourly series is in the event's own zone."""
    items = await rows_for(session, event.id)
    on_event = select(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id)
    collectors = await session.scalar(select(func.count(func.distinct(MerchCollections.registration_id))).select_from(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id)) or 0
    handovers = await session.scalar(select(func.count()).select_from(on_event.subquery())) or 0
    by_door = (await session.execute(select(MerchCollections.door_id, MerchCollections.door_label, Doors.name, func.count()).select_from(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).outerjoin(Doors, Doors.id == MerchCollections.door_id).where(MerchItems.event_id == event.id).group_by(MerchCollections.door_id, MerchCollections.door_label, Doors.name).order_by(func.count().desc()))).all()
    hour = func.date_trunc("hour", func.timezone(EVENT_TIMEZONE, MerchCollections.collected_at)).label("hour")
    hours = (await session.execute(select(hour, func.count()).select_from(MerchCollections).join(MerchItems, MerchItems.id == MerchCollections.item_id).where(MerchItems.event_id == event.id).group_by(hour).order_by(hour))).all()
    return {
        "collectors": collectors, "handovers": handovers,
        "by_item": [{"id": m.id, "name": m.name, "collected": m.collected_count, "stock": m.stock, "variants": [{"id": v.id, "label": v.label, "collected": v.collected_count, "stock": v.stock} for v in m.variants]} for m in items],
        # A handover from before the door was recorded says so, rather than being folded into whichever door is first.
        "by_door": [{"id": did, "name": name or label or "Door not recorded", "count": c} for did, label, name, c in by_door],
        "by_hour": [{"hour": h.isoformat(), "count": c} for h, c in hours],
    }


async def hand_out_by_hand(session, event: Events, item_id: UUID, delta: int) -> dict:
    """A piece given out without a scan — counted, and taken back with Undo.

    One conditional UPDATE, bounded by the stock: two staff tapping at once
    used to read the same count and both write it plus one, losing a piece
    or giving out one more than there was."""
    m = await _item(session, event, item_id)
    total = _total(m)
    t = MerchItems.__table__
    if delta > 0:
        stmt = update(t).where(t.c.id == m.id, t.c.event_id == event.id)
        if total:
            stmt = stmt.where(t.c.collected_count + t.c.manual_handed_out + delta <= total)
        stmt = stmt.values(manual_handed_out=t.c.manual_handed_out + delta).returning(t.c.id)
        if (await session.execute(stmt)).first() is None:
            raise CustomHTTPException(422, message="Nothing left to hand out")
    else:
        await session.execute(
            update(t).where(t.c.id == m.id, t.c.event_id == event.id)
            .values(manual_handed_out=func.greatest(t.c.manual_handed_out + delta, 0))
        )
    await session.commit()
    return item_out(await _item(session, event, item_id))
