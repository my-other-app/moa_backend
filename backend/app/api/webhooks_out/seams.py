"""What webhooks ask of the partner API (app/api/partner), whose tables live
on its own branch: wired at the merge with `configure(...)`, called from the
partner API's module import.

- `club_allocated(session, club_id) -> bool`: the platform admin gave the
  club API access (Admin -> Integrations). Endpoints can be added only then,
  and a change is queued only for a club that has it.
- `partner_scopes(session, club_id, partner_app_id, client_id) -> set[str]`:
  the scopes the partner's connection to this club has now; empty when it is
  not connected (or suspended). A partner's endpoint gets an object only
  with its read scope (events:read for event.*, registrations:read for
  registration.*, checkin.* and payment.*, certificates:read, club:read),
  and people's names, emails, phones and answers only with attendees:read.
- `partner_contact(session, partner_app_id, client_id) -> (name, email) | None`:
  whom to tell when the partner's endpoint is turned off.
- `external_id(session, client_id, object_type, object_id) -> str | None`:
  the id the client gave the object (external IDs are per client), put in
  `data.object.external_id` for the endpoint that client owns.

Until wired, each answers "no" (fail closed): no club is allocated, a
partner's endpoints get nothing, a partner's disabled endpoint is told to the
platform admins instead, and `external_id` stays null.
"""
from __future__ import annotations

from typing import Optional


async def _no_club(session, club_id: int) -> bool:
    return False


async def _no_scopes(session, club_id: int, partner_app_id, client_id) -> frozenset:
    return frozenset()


async def _no_contact(session, partner_app_id, client_id) -> Optional[tuple[str, str]]:
    return None


async def _no_external_id(session, client_id, object_type, object_id) -> Optional[str]:
    return None


_DEFAULTS = {
    "club_allocated": _no_club, "partner_scopes": _no_scopes,
    "partner_contact": _no_contact, "external_id": _no_external_id,
}
_wired = dict(_DEFAULTS)


def configure(**functions) -> None:
    """`configure(club_allocated=..., partner_scopes=..., partner_contact=..., external_id=...)`:
    each an async function as above; those not given stay as they are."""
    for name, fn in functions.items():
        if name not in _DEFAULTS:
            raise TypeError(f"webhooks_out.seams has no {name}")
        if fn is not None:
            _wired[name] = fn


def reset() -> None:
    """Back to unwired (the tests)."""
    _wired.clear()
    _wired.update(_DEFAULTS)


async def club_allocated(session, club_id: int) -> bool:
    return bool(await _wired["club_allocated"](session, club_id))


async def partner_scopes(session, club_id: int, partner_app_id, client_id) -> frozenset:
    return frozenset(await _wired["partner_scopes"](session, club_id, partner_app_id, client_id) or ())


async def partner_contact(session, partner_app_id, client_id) -> Optional[tuple[str, str]]:
    return await _wired["partner_contact"](session, partner_app_id, client_id)


async def external_id(session, client_id, object_type, object_id) -> Optional[str]:
    return await _wired["external_id"](session, client_id, object_type, object_id)
