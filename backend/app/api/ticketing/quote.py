"""What a registration costs, worked out by the server alone: its ticket
type, the type's price, the discount code's share, and what is left to pay.

`quote` is asked by every registration (registration.service.register_event)
before anything is written, and by the public "check a code" (with no lock
and nothing kept). The registration then carries the answer
(`Quote.apply`): its type, its `actual_amount` (what it owes, the payment's
amount -- payments.handlers reads nothing else), the code and what it took.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from app.api.events.models import EventRegistrationsLink
from app.api.ticketing import codes as codes_mod
from app.api.ticketing import places
from app.api.ticketing import types as types_mod
from app.api.ticketing.models import DiscountCodes, TicketTypes, WaitlistEntries
from app.core import money
from app.response import CustomHTTPException


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Quote:
    ticket_type: TicketTypes
    types: list
    #: The type's price, before any code.
    price: Decimal
    code: Optional[DiscountCodes] = None
    discount: Decimal = field(default_factory=lambda: Decimal("0.00"))
    #: The waiting-list offer this registration claims, if any.
    offer: Optional[WaitlistEntries] = None
    #: Whether the event's own price applies (its one, default type).
    follows: bool = False

    @property
    def amount(self) -> Decimal:
        """What is left to pay."""
        return max(Decimal("0.00"), money.rupees(self.price) - money.rupees(self.discount))

    @property
    def holding(self) -> bool:
        """It owes nothing, so it holds its place as soon as it is made."""
        return self.amount == 0

    @property
    def capacity(self) -> Optional[int]:
        return None if self.follows else self.ticket_type.capacity

    @property
    def label(self) -> Optional[str]:
        """The type's name for the ticket to print, when there is a choice of type."""
        return self.ticket_type.name if len(self.types) > 1 else None

    def apply(self, registration, event) -> None:
        """The registration as this quote prices it. On a paid event, one
        that owes nothing is confirmed now: paid, at nothing."""
        registration.ticket_type_id = self.ticket_type.id
        registration.actual_amount = self.amount
        registration.discount_code_id = self.code.id if self.code is not None else None
        registration.discount_amount = self.discount if self.code is not None else None
        registration.ticket_label = self.label
        if event.has_fee and self.holding:
            registration.is_paid = True
            registration.paid_amount = Decimal("0.00")

    def summary(self) -> dict:
        """What a registration's answer says about its price."""
        return {
            "ticket_type": {"id": self.ticket_type.id, "name": self.ticket_type.name},
            "price": float(self.price),
            "discount": (
                {"code": self.code.code, "amount": float(self.discount)} if self.code is not None else None
            ),
            "amount": float(self.amount),
        }


def required(types: list) -> CustomHTTPException:
    visible = [t.id for t in types if not t.is_hidden]
    return CustomHTTPException(
        422, message="Choose a ticket type.", error_code="TICKET_TYPE_REQUIRED",
        errors={"ticket_type_id": "Choose a ticket type.", "ticket_types": visible},
    )


def pick(types: list, ticket_type_id, *, organiser: bool = False) -> TicketTypes:
    """The type a registration is for: the one it names, else the event's
    only (visible) one. The organiser adding a guest gets the default."""
    if ticket_type_id is not None:
        try:
            wanted = int(ticket_type_id)
        except (TypeError, ValueError):
            wanted = None
        ttype = next((t for t in types if t.id == wanted), None)
        if ttype is None:
            raise CustomHTTPException(
                422, message="That ticket type is not on sale for this event.", error_code="TICKET_TYPE_NOT_FOUND",
                errors={"ticket_type_id": "Not a ticket type of this event."},
            )
        return ttype
    if len(types) == 1:
        return types[0]
    if organiser:
        return next((t for t in types if t.is_default), types[0])
    visible = [t for t in types if not t.is_hidden]
    if len(visible) == 1:
        return visible[0]
    raise required(types)


def check_window(ttype: TicketTypes, now: datetime) -> None:
    if ttype.sales_start is not None and now < ttype.sales_start:
        raise CustomHTTPException(
            409, message=f"“{ttype.name}” tickets aren't on sale yet.", error_code="TICKET_TYPE_NOT_ON_SALE",
            errors={"ticket_type_id": ttype.id, "closed_reason": "not_started", "opens_at": ttype.sales_start.isoformat()},
        )
    if ttype.sales_end is not None and now >= ttype.sales_end:
        raise CustomHTTPException(
            409, message=f"“{ttype.name}” tickets are no longer on sale.", error_code="TICKET_TYPE_NOT_ON_SALE",
            errors={"ticket_type_id": ttype.id, "closed_reason": "ended", "closed_at": ttype.sales_end.isoformat()},
        )


async def check_per_person(session, ttype: TicketTypes, *, email=None, phone=None, user_id=None, exclude=None) -> None:
    if not ttype.per_person_limit:
        return
    R = EventRegistrationsLink
    who = codes_mod._identity(R, email=email, phone=phone, user_id=user_id)
    if who is None:
        return
    stmt = select(func.count()).select_from(R).where(
        R.ticket_type_id == ttype.id, R.is_deleted == False, who,  # noqa: E712
    ).execution_options(include_deleted=True)
    if exclude is not None:
        stmt = stmt.where(R.id != exclude)
    if int(await session.scalar(stmt) or 0) >= ttype.per_person_limit:
        raise CustomHTTPException(
            409, message=f"You already hold as many “{ttype.name}” tickets as one person may.",
            error_code="TICKET_TYPE_LIMIT", errors={"ticket_type_id": ttype.id, "per_person_limit": ttype.per_person_limit},
        )


async def quote(
    session, event, *, ticket_type_id=None, discount_code: Optional[str] = None, email: Optional[str] = None,
    phone: Optional[str] = None, user_id: Optional[int] = None, organiser: bool = False,
    offer: Optional[WaitlistEntries] = None, lock: bool = True, early_capacity: bool = True,
    now: Optional[datetime] = None, exclude=None, window: bool = True,
) -> Quote:
    """The price of a registration about to be made, and every check that can
    refuse it before anything is written: the type (TICKET_TYPE_REQUIRED,
    TICKET_TYPE_NOT_FOUND), its sales window (TICKET_TYPE_NOT_ON_SALE), how
    many one person may hold (TICKET_TYPE_LIMIT), the code (DISCOUNT_CODE_*),
    and an early look at what is left (TICKET_TYPE_SOLD_OUT, EVENT_FULL).
    The organiser adding a guest is held to nothing but the places.
    `exclude`: a registration already made, being priced again. `window`
    False leaves the type's sales window alone: a club confirming a request
    made while it was on sale (app/api/approvals)."""
    now = now or _now()
    types = await types_mod.ensure(session, event)
    if offer is not None:
        ttype = next((t for t in types if t.id == offer.ticket_type_id), None)
        if ttype is None:
            raise CustomHTTPException(409, message="That ticket type no longer exists.", error_code="TICKET_TYPE_NOT_FOUND")
    else:
        ttype = pick(types, ticket_type_id, organiser=organiser)
    q = Quote(
        ticket_type=ttype, types=types, price=types_mod.price_of(event, ttype, types), offer=offer,
        follows=types_mod.follows(types),
    )
    if not organiser and offer is None:
        if window:
            check_window(ttype, now)
        await check_per_person(session, ttype, email=email, phone=phone, user_id=user_id, exclude=exclude)
    if discount_code and not organiser:
        q.code, q.discount = await codes_mod.resolve(
            session, event, discount_code, ttype, price=q.price, email=email, phone=phone, user_id=user_id,
            lock=lock, now=now, exclude=exclude,
        )
    if early_capacity:
        await places.check_early(session, event, q)
    return q
