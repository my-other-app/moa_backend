"""The templates on MyOtherApp's own WhatsApp number, as Meta has them.

Read from Meta's Graph API for the platform's WhatsApp Business Account
(WHATSAPP_BUSINESS_ACCOUNT_ID, or the account the access token was granted:
Meta's debug_token), with the same calls a club's own number uses
(app.api.club_whatsapp.graph). Kept for CACHE_SECONDS in this process, so
the page does not ask Meta on every visit; `refresh=true` asks again (at
most once a minute). Each template's status -- approved, pending, rejected
with Meta's reason, paused, disabled -- beside the ones the platform sends
(EXPECTED), so a missing or rejected one is plain to see. Reading only:
nothing here creates or deletes a template.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Query

from app.api.club_whatsapp import graph
from app.config import settings
from app.core import whatsapp
from app.core.auth.dependencies import AdminAuth
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/whatsapp", tags=["admin-whatsapp"])

CACHE_SECONDS = 600.0
REFRESH_EVERY = 60.0
#: The templates the platform sends (app/core/whatsapp.py), and what each is for.
EXPECTED = {
    "ticket_confirmed": "The ticket, after registering or paying",
    "event_reminder": "The day-before reminder",
    "certificate_ready": "A certificate, once issued",
    "payment_pending": "A payment left unfinished",
    "event_update": "An organiser's update to the guests (optional)",
}

_cache: dict = {"at": 0.0, "data": None, "asked_at": 0.0}


def reset_for_tests() -> None:
    _cache.update(at=0.0, data=None, asked_at=0.0)


def _keys(waba_id: str = "") -> graph.Keys:
    return graph.Keys(
        token=(settings.WHATSAPP_ACCESS_TOKEN or "").strip(),
        waba_id=waba_id,
        phone_number_id=(settings.WHATSAPP_PHONE_NUMBER_ID or "").strip(),
    )


async def _waba_id() -> Optional[str]:
    configured = (getattr(settings, "WHATSAPP_BUSINESS_ACCOUNT_ID", "") or "").strip()
    if configured:
        return configured
    # The accounts the token may manage, as Meta says.
    info = await graph.debug_token(_keys())
    for scope in info.get("granular_scopes") or []:
        if isinstance(scope, dict) and scope.get("scope") == "whatsapp_business_management":
            ids = [str(t) for t in scope.get("target_ids") or [] if t]
            if ids:
                return ids[0]
    return None


def _component_text(t: dict) -> Optional[str]:
    for c in t.get("components") or []:
        if isinstance(c, dict) and (c.get("type") or "").upper() == "BODY":
            return (c.get("text") or "")[:1000] or None
    return None


def _template_out(t: dict) -> dict:
    return {
        "id": t.get("id"),
        "name": t.get("name"),
        "language": t.get("language"),
        "status": (t.get("status") or "").lower() or None,
        "category": (t.get("category") or "").lower() or None,
        "rejected_reason": (t.get("rejected_reason") or None) if (t.get("rejected_reason") or "").upper() not in ("", "NONE") else None,
        "quality": ((t.get("quality_score") or {}).get("score") or None) if isinstance(t.get("quality_score"), dict) else None,
        "body": _component_text(t),
        "expected": t.get("name") in EXPECTED,
    }


async def _read() -> dict:
    waba = await _waba_id()
    if not waba:
        return {"configured": True, "waba_id": None, "templates": [], "error": "The WhatsApp Business Account id is not known: "
                "set APP_WHATSAPP_BUSINESS_ACCOUNT_ID in the server's .env."}
    rows = await graph.list_templates(_keys(waba))
    templates = sorted((_template_out(t) for t in rows), key=lambda t: (not t["expected"], t["name"] or "", t["language"] or ""))
    have = {t["name"] for t in templates}
    missing = [{"name": n, "purpose": p} for n, p in EXPECTED.items() if n not in have]
    return {"configured": True, "waba_id": waba, "templates": templates, "missing": missing, "error": None}


@router.get("/templates", summary="The templates on MyOtherApp's WhatsApp number and Meta's verdict on each (admin)")
async def platform_templates(user: AdminAuth, refresh: bool = Query(False)) -> dict:
    expected = [{"name": n, "purpose": p} for n, p in EXPECTED.items()]
    if not whatsapp.configured():
        return {"configured": False, "waba_id": None, "templates": [], "missing": expected, "expected": expected,
                "checked_at": None, "cached": False, "error": None}
    now = time.monotonic()
    fresh = _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS
    if fresh and not (refresh and now - _cache["asked_at"] >= REFRESH_EVERY):
        return {**_cache["data"], "expected": expected, "cached": True}
    _cache["asked_at"] = now
    try:
        data = await _read()
    except graph.GraphError as exc:
        logger.warning("Meta would not list the platform's templates: %s", exc.message)
        if _cache["data"] is not None:
            return {**_cache["data"], "expected": expected, "cached": True, "error": f"Meta did not answer just now: {exc.message}"}
        raise CustomHTTPException(502, message=f"Meta did not answer: {exc.message}"[:300], error_code="META_UNREACHABLE")
    from datetime import datetime, timezone

    data["checked_at"] = datetime.now(timezone.utc).isoformat()
    data.setdefault("missing", [])
    _cache.update(at=now, data=data)
    return {**data, "expected": expected, "cached": False}
