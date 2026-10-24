"""What Meta sends a club's own webhook, at
`/api/v1/hooks/whatsapp/club/{token}` (the address we gave the club).

The token says whose it is; the signature -- `X-Hub-Signature-256`, the raw
body's HMAC-SHA256 under the club's app secret -- says it is Meta. A call
without a good one is refused before its body is read for anything.

- `messages`: what people sent the number (kept, the conversation opened,
  linked and counted unread), and what became of what the number sent
  (sent, delivered, read, failed: the inbox's ticks, and a ticket sent from
  the club's number hears it too). Only the club's own number's: an app may
  serve several, and the others are not this club's.
- `message_template_status_update`: a template approved, rejected (with
  Meta's reason), paused or disabled.
- `phone_number_quality_update`: the messaging limit changed, or the number
  was flagged; the limit is recorded and the quality read again at the
  sweep's next pass.
- `account_update`: Meta restricting or banning the account, kept as the
  connection's last error and told to the club.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional

from app.api.club_whatsapp import alerts, inbox, templates
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import ClubWhatsAppAccounts

#: Meta's account_update events that mean the number may not send.
RESTRICTING = {"DISABLED_UPDATE", "ACCOUNT_VIOLATION", "ACCOUNT_RESTRICTION", "ACCOUNT_DELETED", "PARTNER_REMOVED"}


def signature_ok(body: bytes, signature: Optional[str], app_secret: str) -> bool:
    if not signature or not app_secret:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


async def handle(session, account: ClubWhatsAppAccounts, payload: dict) -> dict:
    """One delivery from Meta. What changed, for the inbox's live updates:
    {"messages": n, "statuses": n, "conversations": {id: kind}}."""
    now = datetime.now(timezone.utc)
    account.last_webhook_at = now
    touched: dict[int, str] = {}
    counts = {"messages": 0, "statuses": 0}
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") and str(entry.get("id")) != account.waba_id:
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field = change.get("field")
            value = change.get("value") if isinstance(change.get("value"), dict) else {}
            if field == "messages":
                await _messages(session, account, value, touched, counts)
            elif field == "message_template_status_update":
                await templates.status_update(session, account, value)
            elif field == "phone_number_quality_update":
                await _quality(session, account, value)
            elif field == "account_update":
                await _account(session, account, value)
    return {**counts, "conversations": touched}


async def _messages(session, account, value: dict, touched: dict, counts: dict) -> None:
    number = str(((value.get("metadata") or {}).get("phone_number_id")) or "")
    if number and number != account.phone_number_id:
        return
    names = {}
    for c in value.get("contacts") or []:
        if isinstance(c, dict) and c.get("wa_id"):
            names[str(c["wa_id"])] = ((c.get("profile") or {}).get("name") or None)
    for m in value.get("messages") or []:
        if not isinstance(m, dict):
            continue
        conv = await inbox.record_inbound(session, account, account.phone_number_id, m, names.get(str(m.get("from") or "")))
        if conv is not None:
            counts["messages"] += 1
            touched[conv.id] = "message"
    for st in value.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        conv_id = await inbox.record_status(session, account, st)
        counts["statuses"] += 1
        if conv_id is not None:
            touched.setdefault(conv_id, "status")


async def _quality(session, account, value: dict) -> None:
    limit = value.get("current_limit") or value.get("new_limit")
    seen = {"messaging_limit_tier": str(limit).upper()} if limit else {}
    if seen:
        await accounts.record_quality(session, account, seen, "webhook")
    # The rating itself is read from Meta at the sweep's next pass.
    account.quality_checked_at = None


async def _account(session, account, value: dict) -> None:
    event = str(value.get("event") or "").upper()
    if event not in RESTRICTING and not value.get("ban_info") and not value.get("violation_info"):
        return
    reason = f"Meta restricted the WhatsApp account ({event or 'account update'})."
    account.last_error = reason
    account.last_error_code = event[:16] or None
    await alerts.tell_owner(
        session, account.club_id, "account",
        subject="Meta restricted your club's WhatsApp account",
        paragraphs=[
            reason,
            "Open WhatsApp Manager to see why and what to do. Until it's lifted, sends from your number may fail; "
            "your tickets then go from MyOtherApp's number.",
        ],
    )
