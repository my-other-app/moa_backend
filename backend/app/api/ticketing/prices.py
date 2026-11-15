"""Who may set what a ticket costs, and who may hide one.

What a ticket costs is money, as discount codes are (club_access.policy):
the owner and admins. An editor still runs the event and its ticket types --
names, words, places, sales windows, order -- but a write that would change
what anyone pays, or hide a type (a hidden free type is a free ticket for
whoever has its link), is refused to them (403 ROLE_FORBIDDEN). Door staff
change nothing here anyway.

Decided from the body against what the event has now, because the event
form saves its whole list of types on every save: a list that changes no
price and hides nothing goes through, so an editor's edit of the event's
description still saves. With one type, its price is the event's own fee,
which follows the event (types.follows); the fee itself is checked the same
way when the event is saved (`fee_changes`).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

from app.api.ticketing import types as types_mod
from app.core import money
from app.response import CustomHTTPException

MESSAGE = "Your role can't change what tickets cost or hide a ticket type. Ask the club's owner or an admin."


def _price(value) -> Decimal:
    return money.rupees(value or 0)


def may_set(request) -> bool:
    """Whether the caller may set prices and hide types: anyone but an editor
    or door staff (a club's own account and a platform admin may)."""
    from app.api.club_access import policy
    from app.api.club_access import service as club_access

    return policy.allows_ticket_prices(club_access.role_of(request))


def refuse() -> CustomHTTPException:
    return CustomHTTPException(403, message=MESSAGE, error_code="ROLE_FORBIDDEN")


def _paid(types: Iterable) -> bool:
    return any(_price(t.price) > 0 for t in types)


def new_type_changes(event, types: list, data: dict) -> bool:
    """A type made: hidden, priced, or a free one on an event that charges."""
    return bool(data.get("is_hidden")) or _price(data.get("price")) > 0 or _paid(types) or types_mod.event_price(event) > 0


def change_changes(ttype, data: dict) -> bool:
    """A type changed (the fields sent): its price or whether it is hidden."""
    if "price" in data and data["price"] is not None and _price(data["price"]) != _price(ttype.price):
        return True
    return "is_hidden" in data and data["is_hidden"] is not None and bool(data["is_hidden"]) != bool(ttype.is_hidden)


def removal_changes(ttype) -> bool:
    """A type taken away: a priced one leaves the event charging less or nothing."""
    return _price(ttype.price) > 0


def list_changes(event, types: list, items: list[dict]) -> bool:
    """The event form's list (types.save_all) against the types it has now."""
    by_id = {t.id: t for t in types}
    fee = types_mod.event_price(event)
    single = len(items) == 1
    for item in items:
        current = by_id.get(item.get("id"))
        if bool(item.get("is_hidden")) != (bool(current.is_hidden) if current is not None else False):
            return True
        price = _price(item.get("price"))
        if single and price == fee:
            continue  # one type at the event's own fee: the event form's single kind of ticket
        if current is None:
            if price > 0 or _paid(types):
                return True
        elif price != _price(current.price):
            return True
    if not single:
        kept = {i.get("id") for i in items}
        if any(removal_changes(t) for t in types if t.id not in kept):
            return True
    return False


def fee_changes(event, types: list, has_fee: Optional[bool], reg_fee) -> bool:
    """The event's own fee changed on an event whose one type follows it."""
    if len(types) > 1:
        return False  # with several types the types decide the fee (types.sync_event_fee)
    after = _price(reg_fee) if has_fee and reg_fee else Decimal("0.00")
    return after != types_mod.event_price(event)
