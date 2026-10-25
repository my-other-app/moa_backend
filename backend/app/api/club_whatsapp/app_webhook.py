"""What Meta sends MyOtherApp's own app about clubs connected with Meta.

A number connected with Embedded Signup has its `messages` sent to the
club's own webhook address (embedded_signup.py sets the account's
`override_callback_uri`). The rest -- template webhooks
(message_template_status_update, template_category_update, ...) and
account webhooks (account_update, account_review_update, account_alerts),
and others overrides don't cover -- always go to the app's default address
(Meta's webhook override page), which is the platform's own WhatsApp
webhook (`/hooks/whatsapp`). This routes each `entry` whose id is a
connected club's WhatsApp Business Account to that club's account, through
the club webhook's own handling (webhook.handle), as if it had come there.

Signed with the platform app's secret (APP_META_APP_SECRET): a body that
isn't is not read for anything here. The platform's own number's statuses
go on as before (hooks.service.whatsapp): this never touches them.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.api.club_whatsapp import live, webhook
from app.api.club_whatsapp.models import LIVE, MODE_EMBEDDED, ClubWhatsAppAccounts
from app.config import settings

logger = logging.getLogger(__name__)


def secret() -> str:
    return (settings.META_APP_SECRET or "").strip()


def signed(raw: bytes, signature: Optional[str]) -> bool:
    """Whether the platform app (Embedded Signup's) signed this body."""
    key = secret()
    return bool(key) and webhook.signature_ok(raw, signature, key)


async def dispatch(session, payload: dict) -> dict:
    """Each entry for a club connected with Meta, to that club's account, in
    a transaction of its own. Never raises: Meta hears nothing of our
    trouble here (the sweep reads templates and quality again anyway).
    {"accounts": handled, "failed": n, "ignored": entries for nobody here}."""
    by_waba: dict[str, list] = {}
    entries = payload.get("entry") if isinstance(payload, dict) else None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("id"):
            by_waba.setdefault(str(entry["id"]), []).append(entry)
    out = {"accounts": 0, "failed": 0, "ignored": 0}
    if not by_waba:
        return out
    A = ClubWhatsAppAccounts
    rows = (await session.scalars(
        select(A).where(A.waba_id.in_(list(by_waba)), A.mode == MODE_EMBEDDED, A.status.in_(LIVE)).order_by(A.id)
    )).all()
    found = {a.waba_id for a in rows}
    out["ignored"] = sum(len(entries) for waba, entries in by_waba.items() if waba not in found)
    for account in rows:
        account_id, club_id = account.id, account.club_id
        try:
            result = await webhook.handle(session, account, {"entry": by_waba[account.waba_id]})
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - never the body in the log
            logger.warning("whatsapp app hook: club account %s failed (%s)", account_id, type(exc).__name__)
            await session.rollback()
            out["failed"] += 1
            continue
        out["accounts"] += 1
        out["failed"] += int(result.get("failed") or 0)
        for conversation_id, kind in (result.get("conversations") or {}).items():
            await live.changed(club_id, conversation_id, kind)
    return out
