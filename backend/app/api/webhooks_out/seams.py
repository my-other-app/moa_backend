"""What webhooks ask of the partner API (app/api/partner): its real functions
are put in once at start-up with `install(...)` (webhooks_out.wiring, called
from doth); tests swap some with `configure(...)` and `reset()` back.

- `club_allocated(session, club_id) -> bool`: the platform admin gave the
  club API access (Admin -> Integrations). Endpoints can be added only then,
  and a change is queued only for a club that has it.
- `partner_scopes(session, club_id, partner_app_id, client_id, *, livemode) -> set[str]`:
  the scopes the partner's connection to this club, in that mode, has now;
  empty when it is not connected (or the app is suspended). A partner's endpoint gets an object only
  with its read scope (events:read for event.*, registrations:read for
  registration.*, checkin.* and payment.*, certificates:read, club:read),
  and people's names, emails, phones and answers only with attendees:read.
- `partner_contact(session, partner_app_id, client_id) -> (name, email) | None`:
  whom to tell when the partner's endpoint is turned off.
- `external_id(session, client_id, object_type, object_id) -> str | None`:
  the id the client gave the object (external IDs are per client), put in
  `data.object.external_id` for the endpoint that client owns.

Until installed, each answers "no" (fail closed): no club is allocated, a
partner's endpoints get nothing, a partner's disabled endpoint is told to the
platform admins instead, and `external_id` stays null.
"""
from __future__ import annotations

from typing import Optional


async def _no_club(session, club_id: int) -> bool:
    return False


async def _no_scopes(session, club_id: int, partner_app_id, client_id, *, livemode: bool = True) -> frozenset:
    return frozenset()


async def _no_contact(session, partner_app_id, client_id) -> Optional[tuple[str, str]]:
    return None


async def _no_external_id(session, client_id, object_type, object_id) -> Optional[str]:
    return None


_DEFAULTS = {
    "club_allocated": _no_club, "partner_scopes": _no_scopes,
    "partner_contact": _no_contact, "external_id": _no_external_id,
}
#: What install() put in (the partner API's real functions); the defaults until then.
_base = dict(_DEFAULTS)
_wired = dict(_DEFAULTS)


def _checked(functions: dict) -> dict:
    for name in functions:
        if name not in _DEFAULTS:
            raise TypeError(f"webhooks_out.seams has no {name}")
    return {k: v for k, v in functions.items() if v is not None}


def install(**functions) -> None:
    """The real functions, once at start-up (webhooks_out.wiring.install, from doth)."""
    _base.update(_checked(functions))
    _wired.clear()
    _wired.update(_base)


def configure(**functions) -> None:
    """Swap some for now (the tests); `reset()` puts the installed ones back."""
    _wired.update(_checked(functions))


def reset() -> None:
    _wired.clear()
    _wired.update(_base)


def installed() -> list[str]:
    """The seams install() gave a real function (the rest still answer no)."""
    return [name for name, fn in _base.items() if fn is not _DEFAULTS[name]]


async def club_allocated(session, club_id: int) -> bool:
    return bool(await _wired["club_allocated"](session, club_id))


async def partner_scopes(session, club_id: int, partner_app_id, client_id, *, livemode: bool = True) -> frozenset:
    return frozenset(await _wired["partner_scopes"](session, club_id, partner_app_id, client_id, livemode=livemode) or ())


async def partner_contact(session, partner_app_id, client_id) -> Optional[tuple[str, str]]:
    return await _wired["partner_contact"](session, partner_app_id, client_id)


async def external_id(session, client_id, object_type, object_id) -> Optional[str]:
    return await _wired["external_id"](session, client_id, object_type, object_id)
