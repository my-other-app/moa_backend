"""What door staff see of the guests: every row and field, the contacts masked.

Door staff (a member with the Door role, a volunteer at the gate, a phone on
a door's gate link) let people in. For that they need a name, the ticket
type, whether they paid and are in, the admission number, and enough of an
address or number to tell two people of one name apart. They do not need
the guest list's addresses and numbers, nor its ticket ids: a ticket id is
the entry credential itself, printed as a QR. Door staff are often outside
volunteers, and the lists and search they read gave all three, a page of a
hundred at a time.

So the answers on the guest-data routes below (`MASKED`, decided by the
shape of the path, as policy's `_TAKEAWAY` is) are masked for a door viewer
on their way out (`DoorMasks`). Every row and every field stays, with its
type, because the console and the live app parse these rows; only values
change:

- `email`:     `a•••@gmail.com` (the first character, then the domain);
- `phone`:     `•••• 4321` (the last four digits);
- `ticket_id`: `••••4F2K` (the last four characters);
- `address` (the deliveries log's email or number): as an email or a phone;
- `offer_url` (a waiting-list offer's claim link): null.

A value that is empty or null stays so; anything not text is left alone.
Scans and admissions (POST) answer as before: the door holds the ticket.
The routes that carry such fields and are left as they are -- the caller's
own account, the event's public page -- are listed in tests/test_club_access.py
with why, and that test fails for a door-readable route in neither list.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DOT = "•"
HIDDEN = DOT * 4

#: The guest data door staff read, masked for them. GET only.
MASKED_SHAPES = (
    # The guest list, one guest, the checked-in list, the winners.
    r"^/api/v1/events/registration/\d+/(?:list|attendees|winners|info/[^/]+)$",
    # Registration requests and the waiting list.
    r"^/api/v1/events/registration/\d+/requests$",
    r"^/api/v1/events/\d+/ticketing/waitlist$",
    # The gate's search, and a guest's paid orders at the gate.
    r"^/api/v1/events/\d+/gate/(?:search|store/[^/]+)$",
    # Lists of guests by what was done for them.
    r"^/api/v1/events/\d+/(?:check-ins|certificates|deliveries|results|competition)$",
    r"^/api/v1/events/\d+/(?:store/orders|store/summary|merch/collections)$",
    r"^/api/v1/events/\d+/messages/[^/]+$",
)
MASKED = re.compile("|".join(f"(?:{p})" for p in MASKED_SHAPES))


def mask_email(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    local, at, domain = value.partition("@")
    if not at or not local:
        return DOT * 3
    return f"{local[0]}{DOT * 3}@{domain}"


def mask_phone(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    digits = re.sub(r"\D", "", value)
    return f"{HIDDEN} {digits[-4:]}" if len(digits) >= 4 else HIDDEN


def mask_ticket(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    return f"{HIDDEN}{value[-4:]}" if len(value) > 4 else HIDDEN


def mask_contact(value: Any) -> Any:
    """An email or a phone number, whichever it is."""
    if isinstance(value, str) and "@" in value:
        return mask_email(value)
    return mask_phone(value)


def _gone(_value: Any) -> None:
    return None


#: Each masked key, and how.
BY_KEY = {
    "email": mask_email,
    "phone": mask_phone,
    "ticket_id": mask_ticket,
    "address": mask_contact,
    "offer_url": _gone,
}


def masked(data: Any) -> Any:
    """`data` (JSON: dicts, lists, values) with every masked key masked, at any depth."""
    if isinstance(data, dict):
        return {k: (BY_KEY[k](v) if k in BY_KEY and not isinstance(v, (dict, list)) else masked(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [masked(v) for v in data]
    return data


def viewing(state: Optional[dict]) -> bool:
    """Whether this request is a door viewer's: a member with the Door role,
    or the gate's own actor when it is not the club (`door_view`, set by
    checkins.gate_auth for a gate link or a volunteer)."""
    if not state:
        return False
    if state.get("door_view"):
        return True
    member = state.get("club_member")
    return getattr(member, "role", None) == "door"


def viewer(request) -> bool:
    """`viewing`, for a route: whether its answer goes out masked."""
    return viewing(request.scope.get("state"))


class DoorMasks:
    """Masks a door viewer's answer on a `MASKED` route, on its way out.

    Plain ASGI, inside the ETag middleware (so the tag is of what is sent).
    Only a GET to a masked path is looked at; only a 200 JSON answer to a
    door viewer is held back, parsed, masked and sent again with its new
    length. Everything else passes straight through."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "GET" or not MASKED.search(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        # The route's request state, made here so this middleware reads the
        # same dict the auth dependencies write into (Request.state).
        state = scope.setdefault("state", {})
        start: Optional[Message] = None
        holding = False
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal start, holding
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers", [])}
                kind = headers.get(b"content-type", b"").split(b";")[0].strip()
                holding = message.get("status") == 200 and kind == b"application/json" and viewing(state)
                if not holding:
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body" or not holding:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            try:
                body = json.dumps(masked(json.loads(body)), ensure_ascii=False, separators=(",", ":")).encode()
            except ValueError:  # not JSON after all: nothing to mask in it
                pass
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, capture)
