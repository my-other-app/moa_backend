"""Where the change being made came from, for webhooks' `origin` (no echo to the same client).

Set by the partner API's auth dependency for the length of a request; read by
app.api.webhooks_out.emit. Everything else is "dashboard", "app", "website" or
"system", set by the caller of emit when it knows better.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Origin:
    type: str  # dashboard | app | website | api_key | partner_app | system
    client_id: Optional[str] = None


_current: ContextVar[Optional[Origin]] = ContextVar("api_origin", default=None)


def current_origin() -> Optional[Origin]:
    return _current.get()


def set_origin(origin: Optional[Origin]):
    """Returns the token to pass to reset_origin."""
    return _current.set(origin)


def reset_origin(token) -> None:
    _current.reset(token)
