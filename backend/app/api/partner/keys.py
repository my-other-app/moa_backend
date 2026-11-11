"""A club's own API keys.

`moa_live_` or `moa_test_` and 40 random characters; kept only as a sha256
digest, so the key itself exists in one answer (create, rotate) and nowhere
else. Each has its scopes, an optional expiry and an optional IP allowlist;
its last use is recorded (at most once a minute). Rotating gives the same
key -- its id, its client_id, its scopes -- a new secret and ends the old
one at once; revoking ends it for good.

Made by the club's owner or an admin (club_access.policy keeps the rest of
the team out of /clubs/integrations), only while the club is allocated, and
at most the allocation's `max_keys` live at once.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select

from app.api.partner import allocation
from app.api.partner import scopes as catalogue
from app.api.partner.models import PartnerApiKeys
from app.response import CustomHTTPException

LIVE = "moa_live_"
TEST = "moa_test_"
SECRET_LENGTH = 40
_ALPHABET = string.ascii_letters + string.digits
MAX_ALLOWLIST = 20
#: How often a key's "last used" is written.
TOUCH_EVERY = timedelta(minutes=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def digest(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def new_secret(livemode: bool) -> str:
    return (LIVE if livemode else TEST) + "".join(secrets.choice(_ALPHABET) for _ in range(SECRET_LENGTH))


def looks_like_key(raw: Optional[str]) -> bool:
    return bool(raw) and (raw.startswith(LIVE) or raw.startswith(TEST))


def display_prefix(raw: str) -> str:
    head = LIVE if raw.startswith(LIVE) else TEST
    return raw[: len(head) + 8]


def _refuse(status: int, message: str, code: str, **errors) -> CustomHTTPException:
    return CustomHTTPException(status, message=message, error_code=code, errors=errors or None)


def check_allowlist(values) -> list[str]:
    """Addresses or CIDR ranges, each checked and written canonically."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_ALLOWLIST:
        raise _refuse(422, f"Give at most {MAX_ALLOWLIST} addresses or ranges.", "VALIDATION", ip_allowlist="Too many.")
    out = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise _refuse(422, f"{text} is not an IP address or range.", "VALIDATION", ip_allowlist=f"{text} is not an IP address or range.")
        canon = str(net.network_address) if net.num_addresses == 1 else str(net)
        if canon not in out:
            out.append(canon)
    return out


def ip_allowed(allowlist, address: Optional[str]) -> bool:
    if not allowlist:
        return True
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def check_scopes(asked, *, attendees_ack: bool) -> list[str]:
    unknown = catalogue.unknown(asked)
    if unknown:
        raise _refuse(422, f"Unknown scopes: {', '.join(unknown)}.", "VALIDATION", scopes=f"Unknown: {', '.join(unknown)}")
    wanted = catalogue.parse(asked)
    if not wanted:
        raise _refuse(422, "Pick at least one scope.", "VALIDATION", scopes="Pick at least one scope.")
    if set(wanted) & catalogue.PARTNER_ONLY:
        raise _refuse(422, "A club's own key cannot create clubs.", "VALIDATION", scopes="clubs:create is for partner apps.")
    if set(wanted) & catalogue.NEEDS_DISCLOSURE and not attendees_ack:
        raise _refuse(
            422, "To share attendees' details, confirm that your registration form tells people their details are shared.",
            "DISCLOSURE_REQUIRED", attendees_disclosure_ack="Required with attendees:read.",
        )
    return wanted


async def live_count(session, club_id: int) -> int:
    return int(await session.scalar(
        select(func.count()).select_from(PartnerApiKeys).where(PartnerApiKeys.club_id == club_id, PartnerApiKeys.revoked_at.is_(None))
    ) or 0)


async def create(
    session, club_id: int, *, name: str, livemode: bool, scopes, expires_at: Optional[datetime] = None,
    ip_allowlist=None, attendees_ack: bool = False, user_id: Optional[int] = None, member_id: Optional[int] = None,
    by_name: Optional[str] = None,
) -> tuple[PartnerApiKeys, str]:
    """A new key and its secret (shown once). Flushed, not committed."""
    alloc = await allocation.of(session, club_id)
    if not alloc.allocated:
        raise _refuse(403, "API access isn't turned on for this club. Ask the MyOtherApp team.", "NOT_ALLOCATED")
    if await live_count(session, club_id) >= alloc.max_keys:
        raise _refuse(409, f"This club can have {alloc.max_keys} keys at once. Revoke one first.", "KEY_LIMIT", max_keys=alloc.max_keys)
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise _refuse(422, "Give the key a name of at most 100 characters.", "VALIDATION", name="Required, at most 100 characters.")
    wanted = check_scopes(scopes, attendees_ack=attendees_ack)
    if expires_at is not None:
        expires_at = _aware(expires_at)
        if expires_at <= _now():
            raise _refuse(422, "The expiry must be in the future.", "VALIDATION", expires_at="Must be in the future.")
    raw = new_secret(livemode)
    row = PartnerApiKeys(
        club_id=club_id, client_id=f"key_{secrets.token_hex(12)}", name=name, prefix=display_prefix(raw), key_hash=digest(raw),
        livemode=bool(livemode), scopes=catalogue.join(wanted), expires_at=expires_at, ip_allowlist=check_allowlist(ip_allowlist),
        created_by_user_id=user_id, created_by_member_id=member_id, created_by_name=(by_name or "")[:120] or None,
        attendees_ack_at=_now() if "attendees:read" in wanted else None,
    )
    session.add(row)
    await session.flush()
    return row, raw


async def club_keys(session, club_id: int, *, include_revoked: bool = False) -> list[PartnerApiKeys]:
    q = select(PartnerApiKeys).where(PartnerApiKeys.club_id == club_id)
    if not include_revoked:
        q = q.where(PartnerApiKeys.revoked_at.is_(None))
    return list((await session.scalars(q.order_by(PartnerApiKeys.created_at.desc()))).all())


async def key_of(session, club_id: Optional[int], key_id) -> PartnerApiKeys:
    try:
        pk = UUID(str(key_id))
    except (TypeError, ValueError):
        pk = None
    row = await session.get(PartnerApiKeys, pk) if pk else None
    if row is None or (club_id is not None and row.club_id != club_id):
        raise _refuse(404, "No such key.", "KEY_NOT_FOUND")
    return row


async def revoke(session, row: PartnerApiKeys, *, by_user_id: Optional[int] = None) -> PartnerApiKeys:
    if row.revoked_at is None:
        row.revoked_at = _now()
        row.revoked_by_user_id = by_user_id
    await session.flush()
    return row


async def rotate(session, row: PartnerApiKeys) -> tuple[PartnerApiKeys, str]:
    """The same key, a new secret; the old one stops working now."""
    if row.revoked_at is not None:
        raise _refuse(409, "This key was revoked. Create a new one.", "KEY_REVOKED")
    raw = new_secret(row.livemode)
    row.key_hash = digest(raw)
    row.prefix = display_prefix(raw)
    row.rotated_at = _now()
    await session.flush()
    return row, raw


def expired(row: PartnerApiKeys) -> bool:
    return row.expires_at is not None and _aware(row.expires_at) <= _now()


async def find(session, raw: str) -> Optional[PartnerApiKeys]:
    """The key a secret names, live or not (the caller says why it is refused)."""
    if not looks_like_key(raw):
        return None
    row = await session.scalar(select(PartnerApiKeys).where(PartnerApiKeys.key_hash == digest(raw)))
    if row is None or not hmac.compare_digest(row.key_hash, digest(raw)):
        return None
    return row


async def touch(session, row: PartnerApiKeys, address: Optional[str]) -> None:
    now = _now()
    seen = _aware(row.last_used_at)
    if seen is None or now - seen > TOUCH_EVERY or row.last_used_ip != (address or None):
        row.last_used_at = now
        row.last_used_ip = (address or "")[:64] or None
        await session.commit()


def out(row: PartnerApiKeys, *, secret: Optional[str] = None) -> dict:
    data = {
        "id": str(row.id),
        "client_id": row.client_id,
        "name": row.name,
        "prefix": row.prefix,
        "mode": "live" if row.livemode else "test",
        "livemode": bool(row.livemode),
        "scopes": catalogue.parse(row.scopes),
        "expires_at": _iso(row.expires_at),
        "ip_allowlist": list(row.ip_allowlist or []),
        "last_used_at": _iso(row.last_used_at),
        "last_used_ip": row.last_used_ip,
        "created_at": _iso(row.created_at),
        "created_by": row.created_by_name,
        "rotated_at": _iso(row.rotated_at),
        "revoked_at": _iso(row.revoked_at),
        "expired": expired(row),
        "attendees_disclosure_ack_at": _iso(row.attendees_ack_at),
    }
    if secret is not None:
        # Shown once: only in the answer that made it.
        data["secret"] = secret
    return data


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _aware(dt).isoformat() if dt is not None else None
