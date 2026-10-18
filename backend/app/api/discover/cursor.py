"""Keyset cursors: where the last page ended, opaque to the client.

`?offset=` makes the database walk and throw away every row before the page
(offset=1,000,000 read a million index entries). A cursor names the last row
of the page instead, by the values the list is ordered on, and the next page
starts after it: a range of the same index, however deep. A cursor belongs
to one list and one order (`kind`); another list's is refused (422).
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from app.response import CustomHTTPException


def _out(v: Any) -> Any:
    if isinstance(v, datetime):
        return {"t": v.isoformat()}
    if isinstance(v, uuid.UUID):
        return {"u": str(v)}
    return v


def _in(v: Any, kind: str) -> Any:
    if v is None:
        return None
    if kind == "dt":
        return datetime.fromisoformat(v["t"])
    if kind == "uuid":
        return uuid.UUID(v["u"])
    if kind == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("not an int")
        return v
    if kind == "float":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("not a number")
        return float(v)
    if kind == "str":
        if not isinstance(v, str):
            raise ValueError("not text")
        return v
    raise ValueError(kind)


def encode(kind: str, values: Sequence[Any]) -> str:
    raw = json.dumps([kind, *[_out(v) for v in values]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: Optional[str], kind: str, types: Sequence[str]) -> Optional[list]:
    """The values a cursor of `kind` carries, typed as `types`; None for no cursor."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, list) or payload[0] != kind or len(payload) != len(types) + 1:
            raise ValueError("another list's cursor")
        return [_in(v, t) for v, t in zip(payload[1:], types)]
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        raise CustomHTTPException(422, message="Invalid request", errors={"after": "This cursor is not one this list gave out, or not for this order."})


def after(keys: Sequence[tuple[Any, bool]], values: Sequence[Any]):
    """SQL for "comes after the row with these values" in the order of `keys`
    ((expression, descending)): the first key past its value, or equal and
    the rest after. The first key's bound (>= or <=) is written out on its
    own as well, so an index on the order starts its range there instead of
    reading from the top. NULL-free keys only."""
    from sqlalchemy import and_, or_

    def past(i: int):
        expr, desc = keys[i]
        strict = expr < values[i] if desc else expr > values[i]
        if i == len(keys) - 1:
            return strict
        return or_(strict, and_(expr == values[i], past(i + 1)))

    first, desc = keys[0]
    bound = first <= values[0] if desc else first >= values[0]
    return and_(bound, past(0))
