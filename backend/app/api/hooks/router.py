"""Provider callbacks. Public, because a provider cannot hold a session: they
are trusted only on their signature, and a call without one is refused
before its body is read for anything. Both answer quickly and never report
our own trouble back: a provider that gets a 500 retries for hours."""
import hashlib
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request, Response

from app.api.hooks import service, sns
from app.config import settings
from app.db.core import SessionDep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks")


@router.post("/ses", summary="What SES says became of a mail", status_code=202)
async def ses_hook(request: Request, session: SessionDep, token: Optional[str] = Query(None)) -> dict:
    """An SNS envelope: the first call subscribes, the rest carry SES's
    bounce, complaint, delivery and delay notices."""
    expected = getattr(settings, "SES_WEBHOOK_TOKEN", "") or ""
    if expected and not hmac.compare_digest(token or "", expected):
        logger.warning("ses hook refused: token did not match")
        return {"ok": False, "reason": "token"}
    msg = sns.parse(await request.body())
    if msg is None:
        return {"ok": False, "reason": "not an SNS message"}
    if not sns.verify(msg):
        logger.warning("ses hook refused: signature did not verify")
        return {"ok": False, "reason": "signature"}
    try:
        if msg["Type"] == "SubscriptionConfirmation":
            return {"ok": sns.confirm(msg), "matched": 0}
        if msg["Type"] != "Notification":
            return {"ok": True, "matched": 0}
        return {"ok": True, **await service.ses(session, msg.get("Message") or "")}
    except Exception:  # noqa: BLE001 - the provider never hears our trouble
        logger.exception("ses hook failed")
        return {"ok": True, "matched": 0}


@router.post("/resend", summary="What Resend says became of a mail", status_code=202)
async def resend_hook(request: Request, session: SessionDep) -> dict:
    """Signed by Resend with the webhook's secret; refused without it."""
    import json

    from app.api.hooks import svix

    raw = await request.body()
    h = request.headers
    if not svix.verify(getattr(settings, "RESEND_WEBHOOK_SECRET", "") or "", h.get("svix-id") or "", h.get("svix-timestamp") or "", h.get("svix-signature") or "", raw):
        logger.warning("resend hook refused: signature did not verify")
        return {"ok": False, "reason": "signature"}
    try:
        return {"ok": True, **await service.resend(session, json.loads(raw.decode("utf-8") or "{}"))}
    except Exception:  # noqa: BLE001 - the provider never hears our trouble
        logger.exception("resend hook failed")
        return {"ok": True, "matched": 0}


@router.get("/whatsapp", summary="Meta subscribes by asking for its own token back")
async def whatsapp_verify(request: Request) -> Response:
    """The platform's own number's verify token, or -- the default address of
    the app clubs connect with Meta through (Embedded Signup) -- that one's."""
    q = request.query_params
    given = q.get("hub.verify_token") or ""
    tokens = [getattr(settings, name, "") or "" for name in ("WHATSAPP_VERIFY_TOKEN", "META_WEBHOOK_VERIFY_TOKEN")]
    if q.get("hub.mode") == "subscribe" and any(t and hmac.compare_digest(given, t) for t in tokens):
        return Response(content=q.get("hub.challenge") or "", media_type="text/plain")
    return Response(content="", media_type="text/plain", status_code=403)


@router.post("/whatsapp", summary="What WhatsApp says became of a message", status_code=202)
async def whatsapp_hook(request: Request, session: SessionDep) -> dict:
    """Meta signs the raw body with the app secret: `sha256=<hex>`. Signed
    with the platform's own number's app secret: its statuses, as ever.
    Signed with the app clubs connect with Meta through (APP_META_APP_SECRET,
    the same app or another): what Meta sends that app about those clubs --
    templates, account updates, which no override redirects -- goes to each
    club's account by its WhatsApp Business Account id
    (club_whatsapp/app_webhook.py)."""
    from app.api.club_whatsapp import app_webhook

    secret = getattr(settings, "WHATSAPP_APP_SECRET", "") or ""
    raw = await request.body()
    signature = request.headers.get("x-hub-signature-256") or ""
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest() if secret else ""
    platform = bool(secret) and hmac.compare_digest(signature, expected)
    clubs = app_webhook.signed(raw, signature)
    if not platform and not clubs:
        return {"ok": False, "reason": "signature"}
    try:
        import json

        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        # As before: a provider never hears our trouble.
        logger.exception("whatsapp hook failed")
        return {"ok": True, "matched": 0}
    out: dict = {"ok": True}
    if clubs:
        out["clubs"] = await app_webhook.dispatch(session, payload if isinstance(payload, dict) else {})
    if not platform:
        return out
    try:
        return {**out, **await service.whatsapp(session, payload)}
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp hook failed")
        return {**out, "matched": 0}
