"""Security: who is signed in, from where, and what happened at the door.

Devices are read from the user agent, places from the address through a
cached lookup, and the lockout from the attempts themselves: five wrong
passwords from one address at one account in fifteen minutes shut that
door for fifteen minutes.
"""
from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime, timedelta, timezone

import logging

import httpx
from sqlalchemy import func, or_, select

from app.api.auth import sessions as session_store
from app.api.security.models import IpPlaces, SignInAttempts
from app.config import settings
from app.core.email import email as email_mod

logger = logging.getLogger(__name__)

MAX_FAILED = 5
LOCK_WINDOW = timedelta(minutes=15)
FAILED_RESULTS = ("wrong_password", "totp_invalid")
NEW_DEVICE_MEMORY = timedelta(days=90)
PLACE_TTL = timedelta(days=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _pct(cur: int, prev: int) -> float | None:
    return None if prev == 0 else float(round((cur - prev) / prev * 100))


# ─── Request facts ──────────────────────────────────────────────────────


def client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45] or None
    return request.client.host[:45] if request.client else None


def user_agent_of(request) -> str | None:
    return ((request.headers.get("user-agent") if request is not None else None) or "")[:255] or None


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("CriOS", "Chrome"), ("FxiOS", "Firefox"), ("Safari/", "Safari"))


def parse_agent(ua: str | None) -> tuple[str, str]:
    """("Chrome on Windows", "computer") from a user agent, without a library."""
    if not ua:
        return "Unknown device", "unknown"
    s = ua
    if "iPhone" in s:
        os_name, kind = "iPhone", "phone"
    elif "iPad" in s:
        os_name, kind = "iPad", "tablet"
    elif "Android" in s:
        os_name, kind = "Android", "tablet" if "Mobile" not in s else "phone"
    elif "Windows" in s:
        os_name, kind = "Windows", "computer"
    elif "Mac OS X" in s or "Macintosh" in s:
        os_name, kind = "macOS", "computer"
    elif "CrOS" in s:
        os_name, kind = "ChromeOS", "computer"
    elif "Linux" in s:
        os_name, kind = "Linux", "computer"
    else:
        os_name, kind = "", "unknown"
    browser = ""
    for needle, name in _BROWSERS:
        if needle in s:
            browser = name
            break
    if browser == "Safari" and "Chrome/" in s:
        browser = "Chrome"
    if not browser and re.search(r"(bot|curl|python|httpx|okhttp|dart)", s, re.I):
        browser = "App or script"
    if browser and os_name:
        return f"{browser} on {os_name}", kind
    if browser:
        return browser, kind
    if os_name:
        return f"Unknown browser on {os_name}", kind
    return "Unknown device", "unknown"


# ─── Places ─────────────────────────────────────────────────────────────


def _is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified)


async def lookup_place(ip: str) -> str | None:
    """One network call; swapped for a fake in tests. Any failure is simply no place."""
    if not settings.GEOIP_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.5) as http:
            r = await http.get(settings.GEOIP_URL.format(ip=ip))
        if r.status_code != 200:
            return None
        d = r.json()
        city = d.get("city") or ""
        region = d.get("region") or d.get("regionName") or ""
        country = d.get("country_name") or d.get("country") or ""
        parts = [p for p in (city, region or country) if p]
        return ", ".join(parts)[:120] or None
    except Exception:
        return None


async def place_for(session, ip: str | None, *, resolve: bool = True) -> str | None:
    """The cached place for an address, looking it up once if asked to."""
    if not ip or not _is_public(ip):
        return None
    row = await session.get(IpPlaces, ip)
    if row is not None and (row.place or _now() - row.looked_up_at.astimezone(timezone.utc) < PLACE_TTL):
        return row.place
    if not resolve:
        return row.place if row else None
    place = await lookup_place(ip)
    if row is None:
        session.add(IpPlaces(ip_address=ip, place=place, looked_up_at=_now()))
    else:
        row.place, row.looked_up_at = place, _now()
    await session.flush()
    return place


# ─── The door ───────────────────────────────────────────────────────────


async def record(session, *, request, identifier: str | None, user, method: str, result: str, session_id=None) -> SignInAttempts:
    ip = client_ip(request)
    row = SignInAttempts(
        user_id=user.id if user is not None else None,
        identifier=(identifier or "").strip().lower()[:120] or None,
        ip_address=ip,
        user_agent=user_agent_of(request),
        method=method,
        result=result,
        session_id=session_id,
        place=await place_for(session, ip),
    )
    session.add(row)
    await session.flush()
    return row


async def lockout_remaining(session, identifier: str | None, ip: str | None) -> int:
    """Seconds this address must wait at this account, or 0."""
    ident = (identifier or "").strip().lower()[:120]
    if not ident or not ip:
        return 0
    since = _now() - LOCK_WINDOW
    rows = list(
        (await session.scalars(
            select(SignInAttempts.created_at)
            .where(SignInAttempts.identifier == ident, SignInAttempts.ip_address == ip, SignInAttempts.result.in_(FAILED_RESULTS), SignInAttempts.created_at >= since)
            .order_by(SignInAttempts.created_at.desc())
            .limit(MAX_FAILED)
        )).all()
    )
    if len(rows) < MAX_FAILED:
        return 0
    latest = rows[0] if rows[0].tzinfo else rows[0].replace(tzinfo=timezone.utc)
    return max(1, int((latest + LOCK_WINDOW - _now()).total_seconds()))


async def is_new_device(session, user_id: int, request) -> bool:
    """No successful sign-in from this user agent and address in the last 90 days."""
    ua, ip = user_agent_of(request), client_ip(request)
    seen = await session.scalar(
        select(func.count()).select_from(SignInAttempts).where(
            SignInAttempts.user_id == user_id, SignInAttempts.result == "signed_in", SignInAttempts.user_agent == ua, SignInAttempts.ip_address == ip,
            SignInAttempts.created_at >= _now() - NEW_DEVICE_MEMORY,
        )
    )
    return (seen or 0) == 0


def send_new_device_email(to: str, *, account: str, device: str, place: str | None, ip: str | None, when: datetime) -> None:
    where = place or "an unknown place"
    stamp = when.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p IST")
    link = f"{settings.PUBLIC_WEB_URL}/security"
    text = (
        f"A new device signed in to {account}.\n\n{device}\n{where}" + (f" · {ip}" if ip else "") + f"\n{stamp}\n\n"
        f"If this was you, there is nothing to do. If not, sign out other sessions and change the password: {link}\n"
    )
    html = (
        f"<p>A new device signed in to <strong>{account}</strong>.</p>"
        f"<p><strong>{device}</strong><br>{where}" + (f" &middot; <code>{ip}</code>" if ip else "") + f"<br>{stamp}</p>"
        f"<p>If this was you, there is nothing to do. If not, <a href=\"{link}\">sign out other sessions and change the password</a>.</p>"
    )
    try:
        email_mod.send_email(recipients=[to], subject=f"New sign-in to {account}", text=text, html=html)
    except Exception:
        # A mail that could not go out must never turn into a failed sign-in.
        logger.warning("new-device email to %s could not be sent", to, exc_info=True)


# ─── The page ───────────────────────────────────────────────────────────


async def account_name(session, user) -> str:
    """The club's name when the account is a club; otherwise the person's handle."""
    from app.api.clubs.models import Clubs

    name = await session.scalar(select(Clubs.name).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    return name or user.username or user.email


async def overview(session, user, current_sid: str | None) -> dict:
    now = _now()
    d30, d60 = now - timedelta(days=30), now - timedelta(days=60)
    mine = or_(SignInAttempts.user_id == user.id, SignInAttempts.identifier.in_([v for v in ((user.username or "").lower(), (user.email or "").lower()) if v]))

    sessions_out = []
    for s in await session_store.list_for_user(session, user.id):
        device, kind = parse_agent(s.user_agent)
        sessions_out.append(dict(
            id=str(s.id), device=device, kind=kind, place=await place_for(session, s.ip_address, resolve=False), ip_address=s.ip_address,
            created_at=_iso(s.created_at), last_seen_at=_iso(s.last_seen_at), current=str(s.id) == str(current_sid),
        ))
    sessions_out.sort(key=lambda s: (not s["current"], s["last_seen_at"] or ""), reverse=False)
    sessions_out = sorted(sessions_out, key=lambda s: (0 if s["current"] else 1, -(datetime.fromisoformat(s["last_seen_at"]).timestamp() if s["last_seen_at"] else 0)))

    async def window(frm, to):
        rows = list((await session.execute(
            select(SignInAttempts.result, SignInAttempts.place).where(mine, SignInAttempts.created_at >= frm, SignInAttempts.created_at < to)
        )).all())
        signed = sum(1 for r, _ in rows if r == "signed_in")
        failed = sum(1 for r, _ in rows if r in FAILED_RESULTS or r == "blocked")
        places = {p for _, p in rows if p}
        return signed, failed, places, rows

    signed, failed, places, rows = await window(d30, now)
    p_signed, p_failed, p_places, _ = await window(d60, d30)
    by_place: dict[str, dict] = {}
    for result, place in rows:
        key = place or "Unknown"
        t = by_place.setdefault(key, dict(place=key, total=0, failed=0))
        t["total"] += 1
        if result in FAILED_RESULTS or result == "blocked":
            t["failed"] += 1
    last_blocked = await session.scalar(select(SignInAttempts.place).where(mine, SignInAttempts.result == "blocked").order_by(SignInAttempts.created_at.desc()).limit(1))

    history = []
    for a in (await session.scalars(select(SignInAttempts).where(mine).order_by(SignInAttempts.created_at.desc()).limit(60))).all():
        device, _ = parse_agent(a.user_agent)
        history.append(dict(id=str(a.id), at=_iso(a.created_at), place=a.place, device=device, ip_address=a.ip_address, method=a.method, result=a.result))

    return dict(
        account=await account_name(session, user),
        counts=dict(
            active_sessions=len(sessions_out), sessions_30d=signed, sessions_delta=_pct(signed, p_signed),
            places_30d=len(places), places_delta=_pct(len(places), len(p_places)),
            failed_30d=failed, failed_delta=_pct(failed, p_failed), last_blocked_place=last_blocked,
        ),
        sessions=sessions_out,
        by_place=sorted(by_place.values(), key=lambda t: (-t["total"], t["place"])),
        history=history,
        two_factor=dict(enabled=bool(getattr(user, "totp_enabled", False)), recovery_codes_remaining=len(getattr(user, "totp_recovery_codes", None) or [])),
        notify_new_device=bool(getattr(user, "notify_new_device", True)),
        lockout=dict(max_failed=MAX_FAILED, window_minutes=int(LOCK_WINDOW.total_seconds() // 60)),
        generated_at=_iso(now),
    )


async def sign_out_others(session, user, current_sid: str | None) -> int:
    n = 0
    for s in await session_store.list_for_user(session, user.id):
        if str(s.id) == str(current_sid):
            continue
        s.revoked = True
        n += 1
    await session.commit()
    return n


def sid_of(request) -> str | None:
    """The session id of whatever credential authenticated this request."""
    from app.core.auth.jwt import decode_jwt_token

    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get("access_token")
    if not token:
        return None
    try:
        return decode_jwt_token(token).get("sid")
    except Exception:
        return None


def sid_in(token: str) -> uuid.UUID | None:
    from app.core.auth.jwt import decode_jwt_token

    try:
        sid = decode_jwt_token(token).get("sid")
        return uuid.UUID(str(sid)) if sid else None
    except Exception:
        return None
