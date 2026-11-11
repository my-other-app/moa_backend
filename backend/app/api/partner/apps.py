"""Partner apps: platforms acting for many clubs, registered by a platform
admin only (there is no self-registration), and the clubs connected to each.

The admin decides which scopes an app may ever ask for (`allowed_scopes`,
`clubs:create` among them only by choice). The app's client secret is shown
once, at registration or rotation; only its digest is kept. Suspending an
app refuses every token it holds until it is resumed; disconnecting a club
(the club's owner or admin, or the admin) ends that connection and every
token it has at once.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select, update

from app.api.partner import scopes as catalogue
from app.api.partner.models import RATE_TIERS, PartnerApps, PartnerConnections, PartnerTokens
from app.response import CustomHTTPException

CLIENT_PREFIX = "moa_app_"
SECRET_PREFIX = "moa_pcs_"
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _refuse(status: int, message: str, code: str = "VALIDATION", **errors) -> CustomHTTPException:
    return CustomHTTPException(status, message=message, error_code=code, errors=errors or None)


def new_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def check_redirect_uri(uri) -> str:
    """https, or plain http back to this machine while a partner builds; never a fragment."""
    if not isinstance(uri, str) or not uri.strip() or len(uri) > 2000:
        raise _refuse(422, "Each redirect URI must be a URL.", redirect_uris="Each must be a URL.")
    parts = urlsplit(uri.strip())
    scheme = parts.scheme.lower()
    if parts.fragment:
        raise _refuse(422, "A redirect URI cannot have a fragment.", redirect_uris="No fragments.")
    if scheme == "https" and parts.hostname:
        return uri.strip()
    if scheme == "http" and (parts.hostname or "").lower() in _LOOPBACK:
        return uri.strip()
    raise _refuse(422, f"{uri} is not allowed: use https (or http://localhost while building).", redirect_uris="https only.")


def _https(value, field: str) -> Optional[str]:
    value = (value or "").strip()[:500] or None
    if value is not None and urlsplit(value).scheme.lower() != "https":
        raise _refuse(422, f"{field} must be an https URL.", **{field: "https only."})
    return value


def check_allowed_scopes(value) -> list[str]:
    unknown = catalogue.unknown(value)
    if unknown:
        raise _refuse(422, f"Unknown scopes: {', '.join(unknown)}.", allowed_scopes=f"Unknown: {', '.join(unknown)}")
    wanted = catalogue.parse(value)
    if not wanted:
        raise _refuse(422, "Allow the app at least one scope.", allowed_scopes="At least one.")
    return wanted


def _apply(app: PartnerApps, fields: dict) -> None:
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name or len(name) > 120:
            raise _refuse(422, "Give the app a name of at most 120 characters.", name="Required.")
        app.name = name
    if "description" in fields:
        app.description = (fields["description"] or "").strip()[:500] or None
    if "website" in fields:
        app.website = _https(fields["website"], "website")
    if "logo_url" in fields:
        app.logo_url = _https(fields["logo_url"], "logo_url")
    if "contact_email" in fields:
        app.contact_email = (fields["contact_email"] or "").strip()[:200] or None
    if "redirect_uris" in fields:
        uris = fields["redirect_uris"] or []
        if not isinstance(uris, list) or not uris or len(uris) > 10:
            raise _refuse(422, "Give between one and ten redirect URIs.", redirect_uris="One to ten.")
        app.redirect_uris = list(dict.fromkeys(check_redirect_uri(u) for u in uris))
    if "allowed_scopes" in fields:
        app.allowed_scopes = catalogue.join(check_allowed_scopes(fields["allowed_scopes"]))
    if "rate_tier" in fields and fields["rate_tier"] is not None:
        if fields["rate_tier"] not in RATE_TIERS:
            raise _refuse(422, f"rate_tier is one of {', '.join(RATE_TIERS)}.", rate_tier="standard or high.")
        app.rate_tier = fields["rate_tier"]


async def register(session, fields: dict, *, by_user_id: Optional[int]) -> tuple[PartnerApps, str]:
    secret = new_secret()
    app = PartnerApps(client_id=CLIENT_PREFIX + secrets.token_urlsafe(18), client_secret_hash="", status="active", created_by_id=by_user_id)
    for required in ("name", "redirect_uris", "allowed_scopes"):
        fields.setdefault(required, None)
    _apply(app, fields)
    from app.api.partner.keys import digest

    app.client_secret_hash = digest(secret)
    session.add(app)
    await session.flush()
    return app, secret


async def edit(session, app: PartnerApps, fields: dict) -> PartnerApps:
    _apply(app, fields)
    await session.flush()
    return app


async def rotate_secret(session, app: PartnerApps) -> str:
    from app.api.partner.keys import digest

    secret = new_secret()
    app.client_secret_hash = digest(secret)
    app.secret_rotated_at = _now()
    await session.flush()
    return secret


async def set_suspended(session, app: PartnerApps, suspended: bool, reason: Optional[str] = None) -> PartnerApps:
    if suspended:
        app.status = "suspended"
        app.suspended_at = _now()
        app.suspended_reason = (reason or "").strip()[:300] or None
    else:
        app.status = "active"
        app.suspended_at = None
        app.suspended_reason = None
    await session.flush()
    return app


async def app_of(session, app_id) -> PartnerApps:
    try:
        pk = UUID(str(app_id))
    except (TypeError, ValueError):
        pk = None
    app = await session.get(PartnerApps, pk) if pk else None
    if app is None:
        raise _refuse(404, "No such partner app.", "APP_NOT_FOUND")
    return app


async def by_client_id(session, client_id: Optional[str]) -> Optional[PartnerApps]:
    if not client_id or len(client_id) > 64:
        return None
    return await session.scalar(select(PartnerApps).where(PartnerApps.client_id == client_id))


async def end_connection(session, conn: PartnerConnections, *, reason: str, by_user_id: Optional[int] = None) -> None:
    """Revoke the connection and every token it holds. Not committed."""
    now = _now()
    if conn.revoked_at is None:
        conn.revoked_at = now
        conn.revoked_reason = reason[:40]
        conn.revoked_by_user_id = by_user_id
    await session.execute(
        update(PartnerTokens).where(PartnerTokens.connection_id == conn.id, PartnerTokens.revoked_at.is_(None)).values(revoked_at=now)
    )
    await session.flush()


async def live_connection(session, app_id, club_id: int, livemode: bool) -> Optional[PartnerConnections]:
    return await session.scalar(select(PartnerConnections).where(
        PartnerConnections.app_id == app_id, PartnerConnections.club_id == club_id,
        PartnerConnections.livemode == bool(livemode), PartnerConnections.revoked_at.is_(None),
    ))


async def club_connections(session, club_id: int) -> list[tuple[PartnerConnections, PartnerApps]]:
    rows = await session.execute(
        select(PartnerConnections, PartnerApps).join(PartnerApps, PartnerApps.id == PartnerConnections.app_id)
        .where(PartnerConnections.club_id == club_id, PartnerConnections.revoked_at.is_(None))
        .order_by(PartnerConnections.created_at.desc())
    )
    return list(rows.all())


async def app_connections(session, app_id) -> list[PartnerConnections]:
    rows = await session.scalars(
        select(PartnerConnections).where(PartnerConnections.app_id == app_id, PartnerConnections.revoked_at.is_(None))
        .order_by(PartnerConnections.created_at.desc())
    )
    return list(rows.all())


async def connection_counts(session, app_ids: list) -> dict:
    if not app_ids:
        return {}
    rows = await session.execute(
        select(PartnerConnections.app_id, func.count()).where(
            PartnerConnections.app_id.in_(app_ids), PartnerConnections.revoked_at.is_(None),
        ).group_by(PartnerConnections.app_id)
    )
    return dict(rows.all())


async def disconnect(session, club_id: int, connection_id, *, by_user_id: Optional[int]) -> tuple[PartnerConnections, PartnerApps]:
    try:
        pk = UUID(str(connection_id))
    except (TypeError, ValueError):
        pk = None
    conn = await session.get(PartnerConnections, pk) if pk else None
    if conn is None or conn.club_id != club_id or conn.revoked_at is not None:
        raise _refuse(404, "No such connected app.", "APP_NOT_FOUND")
    app = await session.get(PartnerApps, conn.app_id)
    await end_connection(session, conn, reason="disconnected", by_user_id=by_user_id)
    return conn, app


def app_out(app: PartnerApps, *, secret: Optional[str] = None, connections: Optional[int] = None) -> dict:
    data = {
        "id": str(app.id),
        "client_id": app.client_id,
        "name": app.name,
        "description": app.description,
        "website": app.website,
        "logo_url": app.logo_url,
        "contact_email": app.contact_email,
        "redirect_uris": list(app.redirect_uris or []),
        "allowed_scopes": catalogue.parse(app.allowed_scopes),
        "rate_tier": app.rate_tier,
        "status": app.status,
        "suspended_at": _iso(app.suspended_at),
        "suspended_reason": app.suspended_reason,
        "secret_rotated_at": _iso(app.secret_rotated_at),
        "created_at": _iso(app.created_at),
    }
    if connections is not None:
        data["connected_clubs"] = connections
    if secret is not None:
        data["client_secret"] = secret
    return data


def connection_out(conn: PartnerConnections, app: Optional[PartnerApps] = None) -> dict:
    data = {
        "id": str(conn.id),
        "club_id": conn.club_id,
        "mode": "live" if conn.livemode else "test",
        "livemode": bool(conn.livemode),
        "scopes": [{"key": s, "label": catalogue.SCOPES[s][0]} for s in catalogue.parse(conn.scopes)],
        "via": conn.via,
        "connected_by": conn.connected_by_name,
        "connected_at": _iso(conn.created_at),
        "last_used_at": _iso(conn.last_used_at),
        "attendees_disclosure_ack_at": _iso(conn.attendees_ack_at),
    }
    if app is not None:
        data["app"] = {"id": str(app.id), "name": app.name, "logo_url": app.logo_url, "website": app.website, "status": app.status}
    return data
