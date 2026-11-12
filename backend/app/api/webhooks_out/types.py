"""The kinds of change a webhook tells about, in the contract's words.

Each type names the object its `data.object` is (objects.py builds it, in the
same shape the partner API's GET returns). The dashboard's events picker
shows the descriptions as they are.
"""
from __future__ import annotations

#: type -> (object it carries, what it means, in plain words).
EVENT_TYPES: dict[str, tuple[str, str]] = {
    "event.created": ("event", "An event was made (as a draft or published straight away)."),
    "event.updated": ("event", "An event's details changed: its name, dates, place, price, places, form or status."),
    "event.published": ("event", "An event was published: the public can see it and register."),
    "event.cancelled": ("event", "An event was cancelled (or deleted before anyone registered)."),
    "registration.created": ("registration", "Someone registered for an event, or was added by the club."),
    "registration.updated": ("registration", "A registration changed: the person's details, answers, ticket type or whether it is paid."),
    "registration.approved": ("registration", "The club confirmed a registration request."),
    "registration.declined": ("registration", "The club declined a registration request."),
    "registration.cancelled": ("registration", "A registration was cancelled: by the person, by the club, or because it was never paid."),
    "checkin.created": ("checkin", "Someone was checked in at the door, a kiosk or by hand."),
    "checkin.deleted": ("checkin", "A check-in was undone."),
    "certificate.issued": ("certificate", "A certificate was issued to someone who took part."),
    "payment.completed": ("payment", "A payment for a ticket went through (amounts and status only)."),
    "payment.refunded": ("payment", "A payment was refunded, in full or in part (amounts and status only)."),
    "club.updated": ("club", "The club's public profile changed: its name, tagline, about, links or logo."),
}

#: The test a club sends itself from the dashboard (or POST .../test): to
#: one endpoint, never part of its subscriptions.
TEST_TYPE = "webhook.test"

ALL = "*"

#: Object types whose contact fields (name, email, phone, answers) need the
#: endpoint's owner to be allowed attendee details.
WITH_CONTACTS = ("registration", "certificate")


def object_of(event_type: str) -> str:
    if event_type == TEST_TYPE:
        return "test"
    return EVENT_TYPES[event_type][0]


def valid(names) -> list[str]:
    """The subscription as given, checked: known types or ["*"], in order,
    without repeats. Raises ValueError naming what is unknown."""
    out: list[str] = []
    for name in names or []:
        name = str(name).strip()
        if name == ALL:
            return [ALL]
        if name not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {name}")
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("Choose at least one event type, or \"*\" for all of them.")
    return out


def wants(subscribed, event_type: str) -> bool:
    if event_type == TEST_TYPE:
        return True
    subscribed = subscribed or []
    return ALL in subscribed or event_type in subscribed


def listing() -> list[dict]:
    return [{"type": t, "object": obj, "description": text} for t, (obj, text) in EVENT_TYPES.items()]
