"""Discount codes: made by a club for one event or all of its events, typed
by a guest at registration, applied by the server alone.

A code takes a percentage off, or an amount (never below nothing: a code
worth more than the ticket makes it free, and a free ticket needs no
payment). It may be held to some of an event's ticket types, to a window of
time, to a number of uses in all, and to a number per person -- a person
being an account, an email address or a phone number.

A use is counted on a registration that is paid, or confirmed with nothing to
pay, and stops counting when it is refunded or cancelled: the count is read
from the registrations themselves (`uses`), so nothing has to remember to
give one back. Checking the total, a registration made in the last
RESERVE_FOR that is still waiting for its payment counts too, so two people
at checkout cannot both take the last use; one that never pays stops
counting once that time is up. Checked holding the code's row, so two
registrations at once see each other.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import and_, func, or_, select

from app.api.events.models import EventRegistrationsLink, Events
from app.api.ticketing.models import AMOUNT, PERCENT, DiscountCodes
from app.core import money
from app.response import CustomHTTPException

FEATURE = "coupons"
#: How long a registration waiting for its payment keeps its use of a code.
RESERVE_FOR = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalise(text: Optional[str]) -> str:
    """As the code is kept: no spaces, upper case."""
    return "".join((text or "").split()).upper()[:40]


def discount_on(code: DiscountCodes, price: Decimal) -> Decimal:
    """What `code` takes off `price`, to the paisa, never more than the price."""
    price = money.rupees(price)
    if code.kind == PERCENT:
        off = (price * money.rupees(code.value) / Decimal(100)).quantize(money.PAISA, rounding=ROUND_HALF_UP)
    else:
        off = money.rupees(code.value)
    return min(price, max(Decimal("0.00"), off))


def _error(status: int, code: str, message: str) -> CustomHTTPException:
    # Keyed by the field, as the website reads a registration's field errors.
    return CustomHTTPException(status, message=message, error_code=code, errors={"discount_code": message})


def invalid() -> CustomHTTPException:
    return _error(422, "DISCOUNT_CODE_INVALID", "This code isn't valid for this event.")


async def find(session, event, text: str) -> Optional[DiscountCodes]:
    """The club's live code with this text that this event takes."""
    code = normalise(text)
    if not code or event.club_id is None:
        return None
    return await session.scalar(
        select(DiscountCodes).where(
            DiscountCodes.club_id == event.club_id,
            DiscountCodes.code == code,
            DiscountCodes.is_deleted == False,  # noqa: E712
            or_(DiscountCodes.event_id == event.id, DiscountCodes.event_id.is_(None)),
        )
    )


def _identity(R, *, email=None, phone=None, user_id=None):
    from app.api.users.service import same_email

    who = []
    if user_id is not None:
        who.append(R.user_id == user_id)
    if email:
        who.append(same_email(R.email, email))
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    if len(digits) == 10:
        who.append(func.right(func.regexp_replace(R.phone, r"\D", "", "g"), 10) == digits)
    return or_(*who) if who else None


def holding(R):
    """A registration that holds what it bought: live, and paid or owing nothing."""
    return and_(R.is_deleted == False, or_(R.is_paid == True, R.actual_amount == 0))  # noqa: E712


async def uses(
    session, code_id: int, *, reserving_since: Optional[datetime] = None, exclude=None,
    email=None, phone=None, user_id=None, person: bool = False,
) -> int:
    """The code's uses: registrations that hold what they bought with it, and
    with `reserving_since`, those still waiting for a payment made since then.
    With `person`, only this person's."""
    R = EventRegistrationsLink
    counted = holding(R)
    if reserving_since is not None:
        from app.api.payments.models import OrderStatus, PaymentOrders

        # Waiting for its payment: made lately, and not one whose money went back.
        refunded = select(PaymentOrders.id).where(
            PaymentOrders.receipt == R.payment_receipt, PaymentOrders.status == OrderStatus.refunded,
        ).exists()
        counted = or_(counted, and_(R.is_deleted == False, R.created_at > reserving_since, ~refunded))  # noqa: E712
    stmt = select(func.count()).select_from(R).where(R.discount_code_id == code_id, counted)
    if exclude is not None:
        stmt = stmt.where(R.id != exclude)
    if person:
        who = _identity(R, email=email, phone=phone, user_id=user_id)
        if who is None:
            return 0
        stmt = stmt.where(who)
    return int(await session.scalar(stmt.execution_options(include_deleted=True)) or 0)


async def usage(session, code_ids) -> dict[int, dict]:
    """Per code: uses (paid or confirmed) and what they took off in all."""
    ids = sorted({int(i) for i in code_ids})
    if not ids:
        return {}
    R = EventRegistrationsLink
    rows = (await session.execute(
        select(R.discount_code_id, func.count(), func.coalesce(func.sum(R.discount_amount), 0))
        .where(R.discount_code_id.in_(ids), holding(R))
        .group_by(R.discount_code_id)
        .execution_options(include_deleted=True)
    )).all()
    out = {i: {"uses": 0, "discount_total": 0.0} for i in ids}
    for code_id, n, total in rows:
        out[code_id] = {"uses": int(n), "discount_total": float(money.rupees(total))}
    return out


async def resolve(
    session, event, text: Optional[str], ttype, *, price: Decimal, email=None, phone=None, user_id=None,
    lock: bool = True, now: Optional[datetime] = None, exclude=None,
) -> tuple[Optional[DiscountCodes], Decimal]:
    """The code `text` names, checked for this registration, and what it
    takes off `price`. (None, 0) for no code. Raises with the reason, keyed
    by `discount_code`. With `lock` the code's row is held to the end of the
    transaction while its uses are counted: the registration that takes the
    last use is the only one."""
    if not (text or "").strip():
        return None, Decimal("0.00")
    now = now or _now()
    code = await find(session, event, text)
    if code is None or not code.is_active:
        raise invalid()
    from app.api.subscriptions import gates

    if not await gates.allows(session, event.club_id, FEATURE, event=event):
        raise invalid()
    if code.starts_at is not None and now < code.starts_at:
        raise _error(409, "DISCOUNT_CODE_NOT_STARTED", "This code can't be used yet.")
    if code.ends_at is not None and now >= code.ends_at:
        raise _error(409, "DISCOUNT_CODE_EXPIRED", "This code has expired.")
    if code.ticket_type_ids and ttype is not None and ttype.id not in code.ticket_type_ids:
        raise _error(409, "DISCOUNT_CODE_NOT_FOR_TYPE", f"This code can't be used on “{ttype.name}” tickets.")
    if lock and (code.max_uses or code.per_person_limit):
        await session.execute(select(DiscountCodes.id).where(DiscountCodes.id == code.id).with_for_update())
    if code.max_uses:
        if await uses(session, code.id, reserving_since=now - RESERVE_FOR, exclude=exclude) >= code.max_uses:
            raise _error(409, "DISCOUNT_CODE_USED_UP", "This code has been used up.")
    if code.per_person_limit:
        mine = await uses(
            session, code.id, reserving_since=now - RESERVE_FOR, exclude=exclude,
            email=email, phone=phone, user_id=user_id, person=True,
        )
        if mine >= code.per_person_limit:
            raise _error(409, "DISCOUNT_CODE_LIMIT", "You've already used this code as many times as it allows.")
    return code, discount_on(code, price)


# ─── The club's codes ───────────────────────────────────────────────────


async def for_club(session, club_id: int, *, event_id: Optional[int] = None, scope: str = "all") -> list[DiscountCodes]:
    """The club's live codes: all of them; with `event_id`, those that event
    takes (its own and the club-wide ones); `scope="club"` the club-wide only."""
    q = select(DiscountCodes).where(DiscountCodes.club_id == club_id, DiscountCodes.is_deleted == False)  # noqa: E712
    if event_id is not None:
        q = q.where(or_(DiscountCodes.event_id == event_id, DiscountCodes.event_id.is_(None)))
    elif scope == "club":
        q = q.where(DiscountCodes.event_id.is_(None))
    return list((await session.scalars(q.order_by(DiscountCodes.created_at.desc(), DiscountCodes.id.desc()))).all())


async def get(session, club_id: int, code_id: int) -> DiscountCodes:
    row = await session.scalar(
        select(DiscountCodes).where(
            DiscountCodes.id == code_id, DiscountCodes.club_id == club_id, DiscountCodes.is_deleted == False,  # noqa: E712
        )
    )
    if row is None:
        raise CustomHTTPException(404, message="Discount code not found", error_code="DISCOUNT_CODE_NOT_FOUND")
    return row


async def _codes_at(session, club_id: int, event_id: Optional[int], *, excluding=None) -> int:
    """Codes usable at one event, as the plan's `coupons_per_event` counts
    them: the event's own and the club-wide ones (a club-wide code is
    counted against the club-wide ones alone)."""
    q = select(func.count()).select_from(DiscountCodes).where(
        DiscountCodes.club_id == club_id, DiscountCodes.is_deleted == False,  # noqa: E712
        DiscountCodes.is_active == True,  # noqa: E712
        or_(DiscountCodes.event_id.is_(None), DiscountCodes.event_id == event_id) if event_id is not None else DiscountCodes.event_id.is_(None),
    )
    if excluding is not None:
        q = q.where(DiscountCodes.id != excluding)
    return int(await session.scalar(q) or 0)


def _check_values(data: dict, *, types_of_event: Optional[set] = None, event_id: Optional[int] = None) -> None:
    kind = data.get("kind")
    value = data.get("value")
    errors = {}
    if kind not in (PERCENT, AMOUNT):
        errors["kind"] = "percent or amount"
    if value is None or money.rupees(value) <= 0:
        errors["value"] = "More than nothing."
    elif kind == PERCENT and money.rupees(value) > 100:
        errors["value"] = "At most 100%."
    if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
        errors["ends_at"] = "Must be after it starts."
    ids = data.get("ticket_type_ids")
    if ids:
        if event_id is None:
            errors["ticket_type_ids"] = "Only a code for one event can be held to its ticket types."
        elif types_of_event is not None and not set(ids) <= types_of_event:
            errors["ticket_type_ids"] = "Ticket types of this event only."
    if errors:
        raise CustomHTTPException(422, message="Check the code's details.", error_code="INVALID_DISCOUNT_CODE", errors=errors)


async def _event_of_club(session, club_id: int, event_id: int) -> Events:
    event = await session.scalar(
        select(Events).where(Events.id == event_id, Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
    )
    if event is None:
        raise CustomHTTPException(404, message="Event not found")
    return event


async def create(session, club, data: dict, *, actor=None) -> DiscountCodes:
    from app.api.subscriptions import gates
    from app.api.ticketing import types as types_mod

    event = await _event_of_club(session, club.id, data["event_id"]) if data.get("event_id") else None
    await gates.require_feature(session, club.id, FEATURE, actor=actor, event=event)
    type_ids = {t.id for t in await types_mod.live(session, event.id)} if event is not None else None
    _check_values(data, types_of_event=type_ids, event_id=event.id if event else None)
    code = normalise(data.get("code"))
    if len(code) < 3:
        raise CustomHTTPException(
            422, message="A code is at least 3 letters or digits.", error_code="INVALID_DISCOUNT_CODE",
            errors={"code": "At least 3 letters or digits, no spaces."},
        )
    await gates.require_within(
        session, club.id, "coupons_per_event", used=await _codes_at(session, club.id, event.id if event else None),
        actor=actor, feature=FEATURE, event=event,
    )
    taken = await session.scalar(
        select(DiscountCodes.id).where(
            DiscountCodes.club_id == club.id, DiscountCodes.code == code, DiscountCodes.is_deleted == False,  # noqa: E712
        )
    )
    if taken is not None:
        raise CustomHTTPException(
            409, message="You already have a code with that name.", error_code="DISCOUNT_CODE_TAKEN",
            errors={"code": "Already one of your codes."},
        )
    row = DiscountCodes(
        club_id=club.id, event_id=event.id if event else None, code=code, kind=data["kind"],
        value=money.rupees(data["value"]), ticket_type_ids=sorted(set(data.get("ticket_type_ids") or [])) or None,
        starts_at=data.get("starts_at"), ends_at=data.get("ends_at"), max_uses=data.get("max_uses"),
        per_person_limit=data.get("per_person_limit"), is_active=bool(data.get("is_active", True)),
        created_by_id=getattr(actor, "id", None),
    )
    session.add(row)
    await session.commit()
    return row


EDITABLE = ("kind", "value", "ticket_type_ids", "starts_at", "ends_at", "max_uses", "per_person_limit", "is_active")


async def update_code(session, club, row: DiscountCodes, data: dict, *, actor=None) -> DiscountCodes:
    from app.api.subscriptions import gates
    from app.api.ticketing import types as types_mod

    merged = {key: getattr(row, key) for key in EDITABLE}
    merged.update({k: v for k, v in data.items() if k in EDITABLE})
    type_ids = {t.id for t in await types_mod.live(session, row.event_id)} if row.event_id else None
    _check_values(merged, types_of_event=type_ids, event_id=row.event_id)
    if data.get("is_active") and not row.is_active:
        event = await session.get(Events, row.event_id) if row.event_id else None
        await gates.require_feature(session, club.id, FEATURE, actor=actor, event=event)
        await gates.require_within(
            session, club.id, "coupons_per_event", used=await _codes_at(session, club.id, row.event_id, excluding=row.id),
            actor=actor, feature=FEATURE, event=event,
        )
    for key in EDITABLE:
        if key not in data:
            continue
        value = data[key]
        if key == "value":
            value = money.rupees(value)
        elif key == "ticket_type_ids":
            value = sorted(set(value or [])) or None
        elif key == "is_active":
            value = bool(value)
        setattr(row, key, value)
    await session.commit()
    return row


async def delete_code(session, row: DiscountCodes) -> None:
    """Gone from the list and from use; the tickets bought with it keep their price."""
    row.soft_delete()
    row.is_active = False
    await session.commit()
