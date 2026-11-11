"""Small things every part of the Integrations API shares: times, money,
ETags and cursors."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.responses import JSONResponse, Response

from app.api.partner.errors import PartnerError

IST = timezone(timedelta(hours=5, minutes=30))
TIMEZONE = "Asia/Kolkata"
CURRENCY = "INR"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """ISO 8601 with India's offset: what every time in the API reads as."""
    if dt is None:
        return None
    return aware(dt).astimezone(IST).isoformat()


def parse_time(value, field: str) -> Optional[datetime]:
    """An ISO 8601 time with its offset (a time without one is refused: whose midnight would it be?)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise PartnerError("VALIDATION", f"{field} is not an ISO 8601 time.", details={"fields": {field: "Not an ISO 8601 time."}})
    if dt.tzinfo is None:
        raise PartnerError("VALIDATION", f"{field} needs its offset, like 2026-12-01T18:00:00+05:30.", details={"fields": {field: "Give the offset."}})
    return dt.astimezone(timezone.utc)


def money(paise: Optional[int]) -> Optional[dict]:
    if paise is None:
        return None
    return {"amount": int(paise), "currency": CURRENCY}


def paise_of(rupees) -> int:
    from app.core import money as money_mod

    return money_mod.to_paise(rupees or 0)


# ─── ETags ───────────────────────────────────────────────────────────────


#: Links signed afresh on every read (a ticket's QR link carries the second it
#: was signed; a picture's link is presigned when the CDN is off): left out of
#: the ETag, or it would change by itself every second and every If-Match a
#: client sends a moment after reading would be refused. What they point at
#: changing moves the object's updated_at, which is in it.
_SIGNED_LINKS = frozenset({"qr_url", "poster_url", "logo_url", "pdf_url"})


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _SIGNED_LINKS}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def etag_of(obj: dict) -> str:
    """A strong ETag over the object as this caller reads it, its signed links left out."""
    raw = json.dumps(_stable(obj), sort_keys=True, separators=(",", ":"), default=str).encode()
    return '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'


def _tags(header: Optional[str]) -> list[str]:
    return [t.strip() for t in (header or "").split(",") if t.strip()]


def check_if_match(request, current: dict) -> None:
    """412 PRECONDITION_FAILED when If-Match names another version of the object."""
    header = request.headers.get("if-match")
    if not header:
        return
    tags = _tags(header)
    if "*" in tags:
        return
    mine = etag_of(current)
    if not any(t.removeprefix("W/") == mine for t in tags):
        raise PartnerError(
            "PRECONDITION_FAILED", "The object changed since you read it. Read it again and retry.",
            details={"etag": mine},
        )


def object_response(request, obj: dict, status: int = 200, headers: Optional[dict] = None) -> Response:
    """The object, with its ETag; a 304 for a GET whose If-None-Match has it."""
    tag = etag_of(obj)
    if request.method == "GET" and any(t.removeprefix("W/") == tag or t == "*" for t in _tags(request.headers.get("if-none-match"))):
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse(obj, status_code=status, headers={"ETag": tag, **(headers or {})})


def list_response(items: list, next_cursor: Optional[str]) -> JSONResponse:
    return JSONResponse({"data": items, "next_cursor": next_cursor})


# ─── Cursors ─────────────────────────────────────────────────────────────


def encode_cursor(*values) -> str:
    raw = json.dumps([v.isoformat() if isinstance(v, datetime) else v for v in values], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: Optional[str], kinds: tuple) -> Optional[list]:
    """The values a cursor carries, as the kinds given ("dt", "int", "str"), or None for no cursor."""
    if not cursor:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(values, list) or len(values) != len(kinds):
            raise ValueError
        out = []
        for kind, value in zip(kinds, values):
            if kind == "dt":
                out.append(datetime.fromisoformat(value))
            elif kind == "int":
                out.append(int(value))
            else:
                out.append(str(value))
        return out
    except (ValueError, TypeError):
        raise PartnerError("VALIDATION", "That cursor is not one this list gave.", details={"fields": {"cursor": "Not valid."}})


def check_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise PartnerError("VALIDATION", f"limit is between 1 and {MAX_LIMIT}.", details={"fields": {"limit": f"1 to {MAX_LIMIT}."}})
    return limit
