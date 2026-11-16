"""Links to a hidden ticket type: the key that lets a link list and sell it.

A hidden type (comp, speaker, staff, an early bird taken off sale) is not on
the event page. The organiser copies its link from the dashboard, and the
link carries a key the server made for that event and that type:
`/events/{slug}?ticket_type={id}&ticket_key={key}`. The event page lists the
type, and a registration, a request, a code check or a place in line takes
it, only with that key (HIDDEN_TYPES_REQUIRE_TOKEN; off, a link that names
the type is enough, as before).

The key is an HMAC of the event and the type under a key of its own, made
from each emailed-link key (settings.link_signing_keys) for this one use, so
no other signed link is ever a ticket key. The first key signs and every one
verifies, so a rotation keeps the links working while the old key is listed.
It never expires: to stop a link, stop the type's sales or make a new type.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional
from urllib.parse import urlencode

from app.config import settings

#: Hex characters of a key (96 bits).
KEY_LENGTH = 24
#: The longest key a request may send (anything longer is not a key).
MAX_KEY = 64
_PURPOSE = b"moa:hidden-ticket-type-link"


def _subkey(link_key: str) -> bytes:
    return hmac.new(link_key.encode(), _PURPOSE, hashlib.sha256).digest()


def _digest(link_key: str, event_id: int, type_id: int) -> str:
    message = f"ticket-type:{int(event_id)}:{int(type_id)}".encode()
    return hmac.new(_subkey(link_key), message, hashlib.sha256).hexdigest()[:KEY_LENGTH]


def key_for(event_id: int, type_id: int) -> str:
    """The key a link to this type carries."""
    return _digest(settings.link_signing_keys[0], event_id, type_id)


def key_ok(event_id: int, type_id: int, key: Optional[str]) -> bool:
    """Whether `key` is this type's under any of the keys (each one tried)."""
    if not key or not isinstance(key, str) or len(key) > MAX_KEY or not key.isascii():
        return False
    key = key.strip().lower()
    ok = False
    for link_key in settings.link_signing_keys:
        ok |= hmac.compare_digest(_digest(link_key, event_id, type_id), key)
    return ok


def required() -> bool:
    return bool(settings.HIDDEN_TYPES_REQUIRE_TOKEN)


def may_sell(ttype, key: Optional[str]) -> bool:
    """Whether a request from the public may list or take this type: a shown
    type always; a hidden one with its key (or with the setting off)."""
    if not ttype.is_hidden or not required():
        return True
    return key_ok(ttype.event_id, ttype.id, key)


def link_for(event, ttype) -> Optional[str]:
    """The event page's address for this type: with its key when it is
    hidden. None for an event with no page address yet."""
    from app.core import public_urls

    if not getattr(event, "slug", None):
        return None
    query = {"ticket_type": ttype.id}
    if ttype.is_hidden:
        query["ticket_key"] = key_for(event.id, ttype.id)
    return f"{public_urls.event_url(event.slug)}?{urlencode(query)}"
