"""Meta's Embedded Signup: "Connect with Meta" (MyOtherApp as a Tech Provider).

Off until the owner has Meta's approval and turns on
APP_WHATSAPP_EMBEDDED_SIGNUP with the platform app's id and secret, the
login configuration's id and the app webhook's verify token
(docs/OPERATIONS.md section 21, "Turning on one-click connect"). Pasting a
club's own app's keys (service.connect) works either way.

The dashboard opens Meta's flow (FB.login with the configuration, a `code`
response, Embedded Signup v5) and posts what comes back at once -- the
code lasts 30 seconds -- with the session's ids (`phone_number_id`,
`messaging_account_ids`, `is_on_biz_app`). Then, in order:

1. the code is exchanged for the business's token (GET /oauth/access_token,
   with the platform app's id and secret, server to server);
2. Meta's debug_token: valid, with whatsapp_business_management and
   whatsapp_business_messaging, MyOtherApp's own app; its expiry is kept
   (null: never). The account comes from the token's granular scopes when
   the session didn't say, and the number from the account when it has one;
3. the number is read and checked, as a pasted one is, and the connection
   kept (mode `embedded`: the token sealed as a pasted one is; the app
   secret and app id are the platform's, never stored per club) -- kept
   before the next step, because Meta checks the club's webhook address
   against its verify token when it is set;
4. the platform app is subscribed to the club's account with the club's
   own webhook address and verify token (`override_callback_uri`), so its
   messages reach the club's webhook as a pasted connection's do;
5. the number is registered with a 6-digit PIN made here and kept sealed --
   not a WhatsApp Business app number (coexistence), which is not
   registered by us. Meta allows 10 registrations of a number in 72 hours
   (133016): the connection then waits, failed, and "Check again" tries
   again;
6. its quality and templates are read, as for a pasted one.

What Meta sends the app's default address (template, account_update,
account_review_update and account_alerts webhooks ignore the override)
is routed to the club's account by app_webhook.py.

Nothing here logs or audits a token, a code or a PIN.
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.api.club_whatsapp import crypto, graph
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import ACTIVE, FAILED, MODE_EMBEDDED, ClubWhatsAppAccounts, ClubWhatsAppTemplates
from app.config import settings
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: Embedded Signup's version the dashboard asks for (v2 and v3 were retired on 15 October 2026).
VERSION = "v5"
#: What an exchangeable code looks like: Meta's url-safe characters, never anything else.
CODE = re.compile(r"^[A-Za-z0-9_\-#.]{10,2048}$")
#: Meta's answer to /register when the number had 10 tries in 72 hours.
REGISTER_LIMIT = "133016"
#: Our code for a number not yet registered (Meta unreachable meanwhile).
REGISTER_PENDING = "REGISTER"
#: The events the dashboard reports when the flow didn't finish.
ABANDONED = ("CANCEL", "ERROR")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setting(name: str) -> str:
    return (getattr(settings, name, "") or "").strip()


def enabled() -> bool:
    """On, with everything the flow needs."""
    return bool(settings.WHATSAPP_EMBEDDED_SIGNUP and _setting("META_APP_ID") and _setting("META_APP_SECRET")
                and _setting("META_ES_CONFIG_ID") and crypto.available())


def public_config() -> Optional[dict]:
    """What the dashboard needs to open Meta's flow (public values), or None while off."""
    if not enabled():
        return None
    return {"app_id": _setting("META_APP_ID"), "config_id": _setting("META_ES_CONFIG_ID"), "graph_version": graph.version(),
            "version": VERSION}


def require_enabled() -> None:
    if not enabled():
        raise CustomHTTPException(404, message="Connecting with Meta isn't available yet. Paste your app's keys instead.",
                                  error_code="EMBEDDED_SIGNUP_OFF")


def _refuse(status: int, message: str, code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(status_code=status, message=message, error_code=code, errors=errors)


# ─── Meta's answers ──────────────────────────────────────────────────────


async def exchange(code: str) -> str:
    """The business's token for the flow's code (GET /oauth/access_token,
    server to server, with the platform app's id and secret). GraphError
    when Meta says no or can't be reached."""
    params = {"client_id": _setting("META_APP_ID"), "client_secret": _setting("META_APP_SECRET"), "code": code}
    try:
        async with graph._client() as client:
            r = await client.get(graph.url("oauth/access_token"), params=params)
    except httpx.HTTPError as exc:
        raise graph.GraphError(f"Meta could not be reached ({exc.__class__.__name__})", unreachable=True) from None
    if r.status_code >= 300:
        raise graph._error(r)
    try:
        body = r.json()
    except ValueError:
        body = None
    token = str((body or {}).get("access_token") or "") if isinstance(body, dict) else (r.text or "").strip()
    if not token:
        raise graph.GraphError("Meta answered the code without a token", code="NO_TOKEN")
    return token


def exchange_refusal(exc: graph.GraphError) -> CustomHTTPException:
    """The code's exchange refused, in words: expired (or already used) --
    Meta's code lasts 30 seconds -- or anything else Meta said."""
    if exc.unreachable:
        return _refuse(502, "Couldn't reach Meta to finish connecting. Click Connect with Meta again.", "META_UNREACHABLE")
    words = (exc.message or "").lower()
    if exc.code == "100" and (exc.subcode in ("36007", "36009") or "expired" in words or "been used" in words):
        return _refuse(422, "Meta's sign-in expired before MyOtherApp could finish it (it lasts 30 seconds). Click Connect with Meta again.",
                       "EMBEDDED_SIGNUP_CODE_EXPIRED", {"meta_code": exc.code, "meta_subcode": exc.subcode})
    return _refuse(422, f"Meta didn't accept the sign-in: {exc.message}"[:300], "EMBEDDED_SIGNUP_REFUSED",
                   {"meta_code": exc.code, "meta_subcode": exc.subcode} if exc.code else None)


def check_token(info: dict) -> Optional[datetime]:
    """What debug_token says of the business's token: valid, both WhatsApp
    permissions, MyOtherApp's own app. When it expires (None: never)."""
    if not info.get("is_valid"):
        raise _refuse(422, "Meta says the sign-in's token isn't valid. Click Connect with Meta again.", "WHATSAPP_TOKEN_INVALID")
    scopes = set(info.get("scopes") or [])
    missing = [s for s in graph.SCOPES if s not in scopes]
    if missing:
        raise _refuse(422, f"The sign-in didn't grant {' and '.join(missing)}. Connect again and allow both WhatsApp permissions.",
                      "WHATSAPP_PERMISSION_MISSING", {"missing": missing})
    app = str(info.get("app_id") or "")
    if app and app != _setting("META_APP_ID"):
        raise _refuse(422, "That token is another app's, not MyOtherApp's.", "WHATSAPP_APP_MISMATCH")
    try:
        expires = int(info.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0
    return datetime.fromtimestamp(expires, tz=timezone.utc) if expires > 0 else None


def waba_from(info: dict) -> Optional[str]:
    """The WhatsApp Business Account the token was granted, from its granular scopes, when there is exactly one."""
    for scope in info.get("granular_scopes") or []:
        if isinstance(scope, dict) and scope.get("scope") == "whatsapp_business_management":
            targets = [str(t) for t in scope.get("target_ids") or [] if t]
            if len(targets) == 1:
                return targets[0]
    return None


# ─── The flow ────────────────────────────────────────────────────────────


@dataclass
class Finish:
    """What the dashboard posts the moment Meta's flow finishes."""

    code: str
    phone_number_id: Optional[str] = None
    waba_id: Optional[str] = None
    business_id: Optional[str] = None
    is_on_biz_app: bool = False
    event: Optional[str] = None


def _clean_id(value: Optional[str], field: str) -> Optional[str]:
    value = (value or "").strip() or None
    if value is not None and not accounts._ID.match(value):
        raise _refuse(422, "Meta's flow sent an id that isn't one. Click Connect with Meta again.", "INVALID_WHATSAPP_KEYS", {"field": field})
    return value


async def connect(session, club, user, finish: Finish, *, member=None, request=None) -> ClubWhatsAppAccounts:
    """The six steps above. 404 EMBEDDED_SIGNUP_OFF; 403 PLAN_FEATURE_LOCKED;
    422 EMBEDDED_SIGNUP_CODE_EXPIRED, EMBEDDED_SIGNUP_REFUSED,
    EMBEDDED_SIGNUP_NO_ACCOUNT, EMBEDDED_SIGNUP_NO_NUMBER and what a pasted
    connection's check says; 409 WHATSAPP_NUMBER_IN_USE; 502 META_UNREACHABLE.
    A number Meta wouldn't register yet is kept, failed, with why."""
    from app.api.admin_alerts import notify
    from app.api.audit import service as audit
    from app.api.club_whatsapp import templates

    require_enabled()
    await accounts.require_plan(session, club.id, actor=user)
    code = (finish.code or "").strip()
    if not CODE.match(code):
        raise _refuse(422, "Meta's flow didn't send a sign-in code. Click Connect with Meta again.", "EMBEDDED_SIGNUP_REFUSED")
    waba, phone = _clean_id(finish.waba_id, "waba_id"), _clean_id(finish.phone_number_id, "phone_number_id")
    business = _clean_id(finish.business_id, "business_id")
    # Nothing held while Meta answers.
    await session.commit()
    # 1. The code first: it lasts 30 seconds.
    try:
        token = await exchange(code)
    except graph.GraphError as exc:
        raise exchange_refusal(exc) from None
    keys = graph.Keys(token=token, app_secret=_setting("META_APP_SECRET"), waba_id=waba or "", phone_number_id=phone or "",
                      app_id=_setting("META_APP_ID"))
    # 2. The token, as Meta sees it.
    try:
        info = await graph.debug_token(keys)
    except graph.GraphError as exc:
        raise accounts.refusal(exc) from None
    expires_at = check_token(info)
    keys.waba_id = waba = waba or waba_from(info) or ""
    if not waba:
        raise _refuse(422, "Meta didn't say which WhatsApp Business Account was shared. Connect again and pick one.",
                      "EMBEDDED_SIGNUP_NO_ACCOUNT")
    # 3. The number, checked as a pasted one is.
    try:
        numbers = await graph.waba_numbers(keys)
        if not phone:
            if len(numbers) != 1:
                raise _refuse(422, "Add a phone number to the WhatsApp Business Account in Meta's flow, then connect again.",
                              "EMBEDDED_SIGNUP_NO_NUMBER")
            phone = numbers[0]
        keys.phone_number_id = phone
        if phone not in numbers:
            raise _refuse(422, "That number isn't in the WhatsApp Business Account Meta shared.", "WHATSAPP_NUMBER_NOT_IN_WABA",
                          {"field": "phone_number_id"})
        number = await graph.phone_number(keys)
    except graph.GraphError as exc:
        raise accounts.refusal(exc) from None
    if phone == _setting("WHATSAPP_PHONE_NUMBER_ID"):
        raise _refuse(422, "That's MyOtherApp's own WhatsApp number. Use your club's.", "INVALID_WHATSAPP_KEYS", {"field": "phone_number_id"})
    taken = await session.scalar(select(ClubWhatsAppAccounts.id).where(
        ClubWhatsAppAccounts.phone_number_id == phone, ClubWhatsAppAccounts.status.in_((ACTIVE, FAILED)),
        ClubWhatsAppAccounts.club_id != club.id,
    ))
    if taken is not None:
        raise _refuse(409, "That number is connected to another club on MyOtherApp.", "WHATSAPP_NUMBER_IN_USE", {"field": "phone_number_id"})

    # Kept now: Meta checks the club's webhook address against its verify token when it is set (step 4).
    await session.execute(select(func.pg_advisory_xact_lock(accounts._CONNECT_LOCK, club.id)))
    now = _now()
    account = await accounts.live_account(session, club.id)
    changed = account is not None
    previous = {"waba_id": account.waba_id, "phone_number_id": account.phone_number_id, "mode": account.mode} if changed else None
    if account is None:
        account = ClubWhatsAppAccounts(club_id=club.id, webhook_token=secrets.token_urlsafe(24), created_by=getattr(user, "id", None),
                                       created_by_member_id=getattr(member, "id", None))
        session.add(account)
    try:
        verify_token = crypto.decrypt(account.verify_token_enc)
    except crypto.Unavailable:
        verify_token = secrets.token_urlsafe(24)
    account.verify_token_enc = crypto.encrypt(verify_token)
    if previous and previous["phone_number_id"] != phone:
        await session.execute(delete(ClubWhatsAppTemplates).where(ClubWhatsAppTemplates.account_id == account.id))
        account.quality_rating = account.messaging_limit_tier = None
        account.register_pin_enc = None
    if not previous or previous["phone_number_id"] != phone or previous["mode"] != MODE_EMBEDDED:
        account.registered_at = None
    account.mode = MODE_EMBEDDED
    account.waba_id, account.phone_number_id, account.business_id = waba, phone, business
    # The token's app is the platform's: its id is settings', its secret never stored per club.
    account.app_id, account.app_secret_enc = _setting("META_APP_ID"), None
    account.access_token_enc, account.token_last4 = crypto.encrypt(token), token[-4:]
    account.token_expires_at, account.on_biz_app = expires_at, bool(finish.is_on_biz_app)
    account.status, account.failed_by, account.restrictions = ACTIVE, None, None
    account.verified_at, account.ended_at = now, None
    account.last_error = account.last_error_code = None
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise _refuse(409, "That number is connected to another club on MyOtherApp.", "WHATSAPP_NUMBER_IN_USE", {"field": "phone_number_id"}) from None
    await session.commit()

    # 4. Meta's webhooks for the account, to the club's own address.
    subscribed = True
    try:
        await graph.call(keys, "POST", f"{waba}/subscribed_apps",
                         json={"override_callback_uri": accounts.webhook_url(account.webhook_token), "verify_token": verify_token})
    except graph.GraphError as exc:
        subscribed = False
        logger.info("embedded signup: club %s's account not subscribed (%s)", club.id, exc.code)
    # 5. The number registered (not a WhatsApp Business app number).
    registered = await _register(session, account, keys)
    # 6. Its quality now; its templates below.
    await accounts.record_quality(session, account, number, "connect")
    if not subscribed and account.last_error is None:
        account.last_error = ("Meta didn't let MyOtherApp subscribe to your WhatsApp account's messages. "
                              "Click Check again, or connect with Meta again.")
    await audit.record(
        session, actor=user, action="club_whatsapp.keys_changed" if changed else "club_whatsapp.connect",
        entity_type="club", entity_id=club.id,
        summary=f"{'Reconnected' if changed else 'Connected'} WhatsApp number {account.display_phone_number or phone} with Meta",
        details={"account_id": account.id, "mode": MODE_EMBEDDED, "waba_id": waba, "phone_number_id": phone, "business_id": business,
                 "display_phone_number": account.display_phone_number, "token_last4": account.token_last4,
                 "token_expires_at": expires_at.isoformat() if expires_at else None, "on_biz_app": account.on_biz_app,
                 "event": (finish.event or "")[:40] or None, "subscribed": subscribed, "registered": registered,
                 "member_id": getattr(member, "id", None), "previous": previous},
        request=request,
    )
    if not changed:
        await notify(
            session, "club_whatsapp_connected", title=f"{club.name} connected its own WhatsApp number with Meta",
            body=f"{account.display_phone_number or phone} ({account.verified_name or 'no verified name yet'}).",
            club=club, data={"account_id": account.id, "mode": MODE_EMBEDDED}, dedupe=False,
        )
    await session.commit()
    try:
        await templates.sync(session, account, keys)
        await session.commit()
    except (graph.GraphError, crypto.Unavailable):
        await session.rollback()
    return account


async def _register(session, account: ClubWhatsAppAccounts, keys: graph.Keys) -> bool:
    """POST /{number}/register with the account's PIN (made and sealed the
    first time). Skipped for a WhatsApp Business app number. True when the
    number is registered (or needs no registering); otherwise the account is
    failed with why -- only registering undoes it (`register_again`)."""
    if account.on_biz_app or account.registered_at is not None:
        return True
    try:
        pin = crypto.decrypt(account.register_pin_enc) if account.register_pin_enc else None
    except crypto.Unavailable:
        pin = None
    if not pin:
        pin = f"{secrets.randbelow(1_000_000):06d}"
        account.register_pin_enc = crypto.encrypt(pin)
        await session.commit()
    try:
        await graph.call(keys, "POST", f"{account.phone_number_id}/register", json={"messaging_product": "whatsapp", "pin": pin})
    except graph.GraphError as exc:
        if exc.code == REGISTER_LIMIT:
            words = ("Meta allows a number 10 registrations in 72 hours, and this one has had them. "
                     "Click Check again in a day or so; until then nothing goes from it.")
        elif exc.unreachable:
            words = "Couldn't reach Meta to register the number. Click Check again in a minute."
        else:
            words = f"Meta wouldn't register the number: {exc.message}"
        # Held as a refused send is (failed_by "send"): a check of the number doesn't undo it, registering does.
        await accounts.mark_failed(session, account, words, exc.code or REGISTER_PENDING, source=accounts.SEND, alert=False)
        return False
    account.registered_at = _now()
    if account.status == FAILED and account.failed_by == accounts.SEND:
        # Unregistered, nothing could have been sent from it: the failure was the registration's.
        account.status, account.failed_by, account.last_error, account.last_error_code = ACTIVE, None, None, None
    return True


async def register_again(session, account: ClubWhatsAppAccounts) -> bool:
    """"Check again" on a number connected with Meta that isn't registered
    yet: registering it again first. True when it is registered now."""
    if account.mode != MODE_EMBEDDED or account.on_biz_app or account.registered_at is not None or account.status not in (ACTIVE, FAILED):
        return True
    try:
        keys = accounts.keys_of(account)
    except crypto.Unavailable:
        return False
    await session.commit()
    done = await _register(session, account, keys)
    await session.commit()
    return done


async def abandoned(session, club, user, *, event: str, current_step: Optional[str] = None, error_message: Optional[str] = None,
                    error_code: Optional[str] = None, session_id: Optional[str] = None, request=None) -> None:
    """Meta's flow ended without finishing: a CANCEL at `current_step`, or an
    ERROR the person reported (Meta's message, code and session id, for its
    support). Kept in the audit log; nothing is connected."""
    from app.api.audit import service as audit

    event = (event or "").upper()
    if event not in ABANDONED:
        raise _refuse(422, "That isn't an event of Meta's flow.", "INVALID_EVENT", {"field": "event"})
    what = current_step or error_code or "unknown"
    await audit.record(
        session, actor=user, action="club_whatsapp.embedded_signup", entity_type="club", entity_id=club.id,
        summary=f"Connect with Meta ended: {event.lower()} ({what})"[:255],
        details={"event": event, "current_step": (current_step or "")[:60] or None, "error_code": (error_code or "")[:40] or None,
                 "error_message": (error_message or "")[:300] or None, "session_id": (session_id or "")[:80] or None},
        request=request,
    )
    await session.commit()
