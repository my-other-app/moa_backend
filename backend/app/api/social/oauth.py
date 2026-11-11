"""Connecting an account: the platform's own sign-in, never a password.

1. An owner or admin presses Connect: a state row is written (who, which
   club, where to go back to, X's PKCE verifier sealed) and the platform's
   sign-in opens with a signed, one-time state: `<id>.<nonce>.<sig>`, the
   signature an HMAC of the three under SECRET_KEY; only the nonce's hash is
   kept.
2. The platform sends the browser back to
   /api/v1/social/oauth/{platform}/callback with a code. The state must be
   ours (signature), for this platform, unexpired (15 minutes) and unused --
   it is spent by one conditional UPDATE, so a replay is refused.
3. The code is exchanged server to server; the tokens are sealed. One Page
   or account: connected. Several: they wait, sealed, on the state row
   (30 minutes) for the club to pick one (GET/POST /accounts/pending/{id}).
4. The browser goes back to the dashboard: `?social=connected|choose|error`
   with `platform`, and `reason` or `pending`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from sqlalchemy import update

from app.api.social import accounts as accounts_mod
from app.api.social import alerts, crypto, platforms
from app.api.social import http as h
from app.api.social.access import Who
from app.api.social.adapters import for_platform, x as x_adapter
from app.api.social.adapters.base import Candidate
from app.api.social.models import SocialOAuthStates
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

STATE_TTL = timedelta(minutes=15)
CHOICE_TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def redirect_uri(platform: str) -> str:
    """What each platform's app must list as its redirect URL."""
    return f"{settings.PUBLIC_API_URL.rstrip('/')}/api/v1/social/oauth/{platform}/callback"


def _sig(state_id: str, nonce: str, platform: str) -> str:
    mac = hmac.new(settings.SECRET_KEY.encode(), f"social-oauth:{platform}:{state_id}:{nonce}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:43]


def make_state(state_id: uuid.UUID, nonce: str, platform: str) -> str:
    return f"{state_id.hex}.{nonce}.{_sig(state_id.hex, nonce, platform)}"


def read_state(raw: Optional[str], platform: str) -> Optional[tuple[uuid.UUID, str]]:
    """(state id, nonce) of a state we signed for this platform, else None."""
    parts = (raw or "").split(".")
    if len(parts) != 3 or not all(parts):
        return None
    sid, nonce, sig = parts
    if not hmac.compare_digest(sig, _sig(sid, nonce, platform)):
        return None
    try:
        return uuid.UUID(hex=sid), nonce
    except ValueError:
        return None


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


def safe_return(value: Optional[str]) -> str:
    """Where the browser may be sent back to: the club dashboard (and, off
    production, the origins the API already trusts)."""
    from app.core.config_checks import is_production

    default = settings.PUBLIC_WEB_URL.rstrip("/") + alerts.SETTINGS_PATH
    if not value:
        return default
    parts = urlsplit(value)
    allowed = {_origin(settings.PUBLIC_WEB_URL)}
    if not is_production(settings):
        allowed |= {o.lower().rstrip("/") for o in settings.cors_origins}
    if parts.scheme not in ("https", "http") or not parts.netloc or _origin(value) not in allowed or len(value) > 500:
        raise CustomHTTPException(422, message="Come back to the club dashboard.", error_code="RETURN_TO_NOT_ALLOWED",
                                  errors={"return_to": "Not the club dashboard."})
    return value


def with_query(url: str, **params) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in params]
    q += [(k, str(v)) for k, v in params.items() if v is not None]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


async def start(session, who: Who, platform: str, return_to: Optional[str], request=None) -> str:
    """The platform's sign-in address for this club, with a fresh one-time state."""
    back = safe_return(return_to)
    reason = platforms.unavailable_reason(platform)
    if reason:
        raise CustomHTTPException(409, message=f"Connecting {platforms.get(platform).label} isn't available yet.",
                                  error_code="SOCIAL_PLATFORM_UNAVAILABLE", errors={"platform": platform, "reason": reason})
    await platforms.require_plan(session, who.club.id, platform, actor=who.user)
    state_id, nonce = uuid.uuid4(), secrets.token_urlsafe(24)
    verifier = x_adapter.new_verifier() if platform == "x" else None
    session.add(SocialOAuthStates(
        id=state_id, club_id=who.club.id, platform=platform, nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
        user_id=getattr(who.user, "id", None), member_id=who.member_id, actor_name=who.name[:120], return_to=back,
        verifier_enc=crypto.seal(verifier), created_at=_now(), expires_at=_now() + STATE_TTL,
    ))
    await session.commit()
    return for_platform(platform).authorize_url(
        state=make_state(state_id, nonce, platform), redirect_uri=redirect_uri(platform),
        challenge=x_adapter.challenge(verifier) if verifier else None,
    )


def _fail(back: str, platform: str, reason: str) -> str:
    return with_query(back, social="error", platform=platform, reason=reason)


async def callback(session, platform: str, params: dict) -> str:
    """Where to send the browser after the platform's sign-in."""
    default = settings.PUBLIC_WEB_URL.rstrip("/") + alerts.SETTINGS_PATH
    parsed = read_state(params.get("state"), platform)
    if parsed is None:
        return _fail(default, platform, "invalid_state")
    state_id, nonce = parsed
    now = _now()
    # Spent here, once: a second arrival with the same state finds it used.
    spent = await session.scalar(
        update(SocialOAuthStates)
        .where(SocialOAuthStates.id == state_id, SocialOAuthStates.platform == platform, SocialOAuthStates.used_at.is_(None),
               SocialOAuthStates.nonce_hash == hashlib.sha256(nonce.encode()).hexdigest())
        .values(used_at=now).returning(SocialOAuthStates.id)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if spent is None:
        return _fail(default, platform, "invalid_state")
    state = await session.get(SocialOAuthStates, state_id, populate_existing=True)
    back = state.return_to or default
    if state.expires_at < now:
        return _fail(back, platform, "expired_state")
    if params.get("error") or not params.get("code"):
        return _fail(back, platform, "denied" if params.get("error") in ("access_denied", "user_cancelled_login", "user_cancelled_authorize") or params.get("error") else "no_code")
    reason = platforms.unavailable_reason(platform)
    if reason:
        return _fail(back, platform, reason)
    if not await platforms.plan_allows(session, state.club_id, platform):
        return _fail(back, platform, "plan")
    try:
        verifier = crypto.open_or_none(state.verifier_enc)
        signed = await for_platform(platform).sign_in(code=params["code"], redirect_uri=redirect_uri(platform), verifier=verifier)
    except h.PlatformError as exc:
        logger.info("social: %s sign-in for club %s refused (%s)", platform, state.club_id, exc.code)
        return _fail(back, platform, "exchange_failed")
    state.verifier_enc = None
    if signed.empty_reason or not signed.candidates:
        await session.commit()
        return _fail(back, platform, signed.empty_reason or "no_accounts")
    if len(signed.candidates) > 1:
        state.choices = [c.choice() for c in signed.candidates]
        state.pending_enc = crypto.seal(json.dumps({
            "connector_id": signed.connector_id, "scopes": signed.scopes,
            "candidates": [_cand_dict(c) for c in signed.candidates],
        }))
        await session.commit()
        return with_query(back, social="choose", platform=platform, pending=str(state.id))
    account = await _keep(session, state, signed.candidates[0], signed.connector_id, signed.scopes)
    return with_query(back, social="connected", platform=platform, account=str(account.id))


def _cand_dict(c: Candidate) -> dict:
    return {"external_id": c.external_id, "name": c.name, "handle": c.handle, "picture": c.picture, "token": c.token,
            "refresh_token": c.refresh_token, "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "refresh_expires_at": c.refresh_expires_at.isoformat() if c.refresh_expires_at else None}


def _cand(d: dict) -> Candidate:
    def when(v):
        return datetime.fromisoformat(v) if v else None

    return Candidate(external_id=d["external_id"], name=d.get("name"), handle=d.get("handle"), picture=d.get("picture"),
                     token=d["token"], refresh_token=d.get("refresh_token"), expires_at=when(d.get("expires_at")),
                     refresh_expires_at=when(d.get("refresh_expires_at")))


async def _keep(session, state: SocialOAuthStates, cand: Candidate, connector_id, scopes):
    by = {"user_id": state.user_id, "member_id": state.member_id, "name": state.actor_name}
    account, new = await accounts_mod.save(session, club_id=state.club_id, platform=state.platform, cand=cand,
                                           connector_id=connector_id, scopes=scopes, by=by)
    from app.api.audit import service as audit
    from app.api.users.models import Users

    actor = await session.get(Users, state.user_id) if state.user_id else None
    await audit.record(
        session, actor=actor, action="social.connect", entity_type="club", entity_id=state.club_id,
        summary=f"{'Connected' if new else 'Reconnected'} {alerts.LABELS.get(state.platform, state.platform)}: {account.name or account.handle or account.external_id}"[:255],
        details={"account_id": str(account.id), "platform": state.platform, "external_id": account.external_id,
                 "member_id": state.member_id, "by": state.actor_name, "scopes": account.scopes},
    )
    await session.commit()
    return account


async def _pending(session, who: Who, state_id) -> SocialOAuthStates:
    state = await session.get(SocialOAuthStates, state_id)
    if (state is None or state.club_id != who.club.id or not state.choices or state.chosen_at is not None
            or not state.pending_enc or state.created_at + CHOICE_TTL < _now()):
        raise CustomHTTPException(404, message="That sign-in has expired. Press Connect again.", error_code="SOCIAL_PENDING_EXPIRED")
    return state


async def choices(session, who: Who, state_id) -> list[dict]:
    return list((await _pending(session, who, state_id)).choices)


async def choose(session, who: Who, state_id, external_id: str):
    state = await _pending(session, who, state_id)
    try:
        pending = json.loads(crypto.open_(state.pending_enc))
    except (crypto.Unavailable, ValueError):
        raise CustomHTTPException(404, message="That sign-in has expired. Press Connect again.", error_code="SOCIAL_PENDING_EXPIRED")
    picked = next((c for c in pending.get("candidates", []) if c.get("external_id") == str(external_id)), None)
    if picked is None:
        raise CustomHTTPException(422, message="Pick one of the accounts listed.", error_code="SOCIAL_CHOICE_INVALID")
    done = await session.scalar(
        update(SocialOAuthStates).where(SocialOAuthStates.id == state.id, SocialOAuthStates.chosen_at.is_(None))
        .values(chosen_at=_now(), pending_enc=None).returning(SocialOAuthStates.id).execution_options(synchronize_session=False)
    )
    if done is None:
        raise CustomHTTPException(409, message="That account was picked already.", error_code="SOCIAL_PENDING_EXPIRED")
    return await _keep(session, state, _cand(picked), pending.get("connector_id"), pending.get("scopes"))
