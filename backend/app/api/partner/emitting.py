"""Webhooks for the changes the domain services do not make themselves.

Every change made through the domain services (an event created, a
registration made, a check-in) is emitted by them (app/api/webhooks_out,
feat/api-webhooks). Two kinds of change made here are not theirs:

- test mode's: sandbox objects never reach the domain, so their changes are
  emitted from sandbox.py, each object carrying `livemode: false`;
- a registration marked paid outside MyOtherApp (live.py), after the
  registration itself was made: `registration.updated`.

Both go through webhooks_out.emit(club_id, type, object, origin) once it is
there; until the two branches are merged (or with it missing) this does
nothing. Never raises: a webhook must never cost the change it describes.
"""
from __future__ import annotations

import inspect
import logging

logger = logging.getLogger(__name__)


async def emit(caller, type_: str, obj: dict) -> None:
    try:
        from app.api.webhooks_out import emit as _emit  # feat/api-webhooks
    except ImportError:
        return
    try:
        result = _emit(caller.club_id, type_, obj, caller.origin())
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        logger.warning("webhook %s could not be emitted", type_, exc_info=True)
