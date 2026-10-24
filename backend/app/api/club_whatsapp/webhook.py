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
  Meta's reason), paused, disabled, flagged or reinstated.
- `template_category_update`: a template's category changed (a ticket
  template moved to MARKETING is no longer one our sends use).
- `phone_number_quality_update`, `business_capability_update`: the
  business portfolio's messaging limit changed (`max_daily_conversations_per_business`);
  it is recorded, and the quality read again at the sweep's next pass.
- `account_update`: Meta banning, restricting (routing paused while it
  stops sending), warning, reinstating or offboarding the account: kept on
  the connection and told to the club and the admins.

Meta delivers again (for 7 days) whatever is not answered 200, and may
deliver anything twice. Each message, status and change is handled in a
SAVEPOINT of its own: one that fails is undone alone, the rest are kept,
and the delivery is answered 503 so Meta sends it again -- when what was
kept the first time is not kept twice (a message by its wamid, a status
only ever moving forward).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from app.api.club_whatsapp import alerts, inbox, templates
from app.api.club_whatsapp import service as accounts
from app.api.club_whatsapp.models import ClubWhatsAppAccounts

logger = logging.getLogger(__name__)

def signature_ok(body: bytes, signature: Optional[str], app_secret: str) -> bool:
    """Compared as bytes: a header with characters beyond ASCII is simply
    not the signature (hmac.compare_digest raises on such a str)."""
    if not signature or not app_secret:
        return False
    expected = b"sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest().encode("ascii")
    return hmac.compare_digest(expected, signature.strip().encode("utf-8", "replace"))


def _errors_said(account: ClubWhatsAppAccounts, where: str, errors) -> None:
    """Meta's own errors in a delivery (`value.errors`: about the app or the
    account): logged by code and title, which are Meta's words, never what
    a message said."""
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, dict):
            logger.warning("club whatsapp account %s: Meta reported an error in %s (%s: %s)", account.id, where,
                           error.get("code"), str(error.get("title") or error.get("message") or "")[:200])


async def handle(session, account: ClubWhatsAppAccounts, payload: dict) -> dict:
    """One delivery from Meta. What changed, for the inbox's live updates:
    {"messages": n, "statuses": n, "failed": n, "conversations": {id: kind}};
    `failed` items were undone and the caller has Meta deliver again."""
    now = datetime.now(timezone.utc)
    account.last_webhook_at = now
    touched: dict[int, str] = {}
    counts = {"messages": 0, "statuses": 0, "failed": 0}
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
                continue
            try:
                async with session.begin_nested():
                    if field == "message_template_status_update":
                        await templates.status_update(session, account, value)
                    elif field == "template_category_update":
                        await templates.category_update(session, account, value)
                    elif field == "phone_number_quality_update":
                        await _quality(session, account, value)
                    elif field == "business_capability_update":
                        await _capability(session, account, value)
                    elif field == "account_update":
                        await _account(session, account, value)
            except Exception as exc:  # noqa: BLE001 - undone alone; Meta delivers it again
                counts["failed"] += 1
                logger.warning("club whatsapp account %s: a %s change could not be kept (%s)", account.id, field, type(exc).__name__)
    return {**counts, "conversations": touched}


async def _messages(session, account, value: dict, touched: dict, counts: dict) -> None:
    number = str(((value.get("metadata") or {}).get("phone_number_id")) or "")
    if number and number != account.phone_number_id:
        return
    _errors_said(account, "messages", value.get("errors"))
    # Who each message is from, by number or -- for someone whose number Meta doesn't share -- by BSUID.
    contacts: dict = {}
    for c in value.get("contacts") or []:
        if not isinstance(c, dict):
            continue
        profile = c.get("profile") if isinstance(c.get("profile"), dict) else {}
        seen = {"name": profile.get("name") or None, "username": profile.get("username") or None,
                "wa_id": c.get("wa_id") or None, "user_id": c.get("user_id") or None}
        for key in (seen["wa_id"], seen["user_id"]):
            if key:
                contacts[str(key)] = seen
    for m in value.get("messages") or []:
        if not isinstance(m, dict):
            continue
        contact = contacts.get(str(m.get("from") or "")) or contacts.get(str(m.get("from_user_id") or ""))
        try:
            async with session.begin_nested():
                conv_id = await inbox.record_inbound(session, account, account.phone_number_id, m, contact)
        except Exception as exc:  # noqa: BLE001 - never the message in the log
            counts["failed"] += 1
            logger.warning("club whatsapp account %s: a message could not be kept (%s)", account.id, type(exc).__name__)
            continue
        if conv_id is not None:
            counts["messages"] += 1
            touched[conv_id] = "message"
    for st in value.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        try:
            async with session.begin_nested():
                conv_id = await inbox.record_status(session, account, st)
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            logger.warning("club whatsapp account %s: a status could not be kept (%s)", account.id, type(exc).__name__)
            continue
        counts["statuses"] += 1
        if conv_id is not None:
            touched.setdefault(conv_id, "status")


async def _quality(session, account, value: dict) -> None:
    """The business portfolio's messaging limit (`max_daily_conversations_per_business`;
    the older `current_limit` when that is all there is -- and never a
    THROUGHPUT_UPGRADE's, which is about speed, not a limit)."""
    limit = value.get("max_daily_conversations_per_business")
    if limit is None and str(value.get("event") or "").upper() != "THROUGHPUT_UPGRADE":
        limit = value.get("current_limit") or value.get("new_limit")
    if accounts.tier_of(limit):
        await accounts.record_quality(session, account, {"max_daily_conversations_per_business": limit}, "webhook")
    # The rating itself is read from Meta at the sweep's next pass.
    account.quality_checked_at = None


async def _capability(session, account, value: dict) -> None:
    """`business_capability_update`: the portfolio's messaging limit, as a
    count (250, 2000 ... -1 for unlimited) or a TIER_*."""
    limit = value.get("max_daily_conversations_per_business")
    if limit is None:
        limit = value.get("max_daily_conversation_per_phone")
    if accounts.tier_of(limit):
        await accounts.record_quality(session, account, {"max_daily_conversations_per_business": limit}, "webhook")


#: account_update events that end the number's use until the club acts.
ENDING = {"ACCOUNT_DELETED", "PARTNER_REMOVED", "PARTNER_APP_UNINSTALLED", "ACCOUNT_OFFBOARDED"}
#: And those that say it is back.
BACK = {"ACCOUNT_RECONNECTED"}


async def _account(session, account, value: dict) -> None:
    """Meta's `account_update` about the club's account: banned (DISABLE),
    to be banned, reinstated (REINSTATE: the error cleared and the number
    checked again), restricted (each restriction kept, and routing paused
    while one stops it sending), a policy violation, or the account gone or
    offboarded (a device change or re-registration). The club and the
    admins are told; other events (pricing tiers, partner news) are not
    about sending."""
    event = str(value.get("event") or "").upper()
    ban = value.get("ban_info") if isinstance(value.get("ban_info"), dict) else {}
    state = str(ban.get("waba_ban_state") or "").upper()
    if state == "REINSTATE" or event in BACK:
        await _reinstated(session, account, event or state)
        return
    if event == "ACCOUNT_RESTRICTION":
        await _restricted(session, account, value)
        return
    if state == "DISABLE" or (event == "DISABLED_UPDATE" and state != "SCHEDULE_FOR_DISABLE") or event in ENDING:
        what = {
            "ACCOUNT_OFFBOARDED": "was taken off the WhatsApp Business Platform (a device change or the number registered again)",
            "ACCOUNT_DELETED": "was deleted", "PARTNER_REMOVED": "was disconnected from the app",
            "PARTNER_APP_UNINSTALLED": "removed the app's access",
        }.get(event, "was disabled for a policy violation")
        await accounts.mark_failed(session, account, f"Meta says your WhatsApp Business Account {what}.", event or state,
                                   source=accounts.WEBHOOK)
        return
    if state == "SCHEDULE_FOR_DISABLE" or event == "ACCOUNT_VIOLATION":
        violation = (value.get("violation_info") or {}).get("violation_type") if isinstance(value.get("violation_info"), dict) else None
        reason = ("Meta has scheduled your WhatsApp Business Account to be disabled." if state == "SCHEDULE_FOR_DISABLE"
                  else f"Meta says your WhatsApp Business Account broke its policies ({violation or 'a violation'}).")
        account.last_error, account.last_error_code = reason[:300], (state or event)[:40]
        await _tell(session, account, "violation", reason, code=state or event)


async def _restricted(session, account, value: dict) -> None:
    restrictions = []
    for r in value.get("restriction_info") or []:
        if not isinstance(r, dict) or not r.get("restriction_type"):
            continue
        ends = None
        try:
            ends = datetime.fromtimestamp(int(r["expiration"]), tz=timezone.utc).isoformat() if r.get("expiration") else None
        except (TypeError, ValueError, OverflowError, OSError):
            ends = None
        restrictions.append({"type": str(r["restriction_type"])[:80], "expires_at": ends,
                             "remediation": (r.get("remediation") or None) and str(r["remediation"])[:300]})
    if not restrictions:
        restrictions = [{"type": "ACCOUNT_RESTRICTION", "expires_at": None, "remediation": None}]
    account.restrictions = restrictions
    kinds = ", ".join(r["type"] for r in restrictions)
    reason = f"Meta restricted your WhatsApp Business Account ({kinds})."
    account.last_error, account.last_error_code = reason[:300], "ACCOUNT_RESTRICTION"
    await _tell(session, account, "restricted", reason, code="ACCOUNT_RESTRICTION",
                paused=accounts.sending_restricted(account))


async def _reinstated(session, account, event: str) -> None:
    """Back: the restrictions and the error Meta's events set are cleared,
    a number they had failed is active again, and Meta is asked how it
    stands now (best effort; the sweep asks too)."""
    account.restrictions = None
    if account.status == accounts.FAILED and account.failed_by == accounts.WEBHOOK:
        account.status, account.failed_by = accounts.ACTIVE, None
    if account.status == accounts.ACTIVE:
        account.last_error = account.last_error_code = None
    account.quality_checked_at = None
    if await accounts.plan_allows(session, account.club_id):
        await accounts.recheck(session, account, source="webhook")


async def _tell(session, account, kind: str, reason: str, *, code: str, paused: bool = False) -> None:
    await alerts.tell_owner(
        session, account.club_id, kind,
        subject="Meta restricted your club's WhatsApp account" if kind == "restricted" else "Meta warned your club's WhatsApp account",
        paragraphs=[
            reason,
            "Open WhatsApp Manager to see why and what to do."
            + (" Until it's lifted, your tickets, certificates and reminders go from MyOtherApp's number." if paused else ""),
        ],
    )
    await alerts.tell_admins(
        session, "club_whatsapp_failed", title=f"Meta restricted a club's own WhatsApp account ({code})", body=reason,
        club_id=account.club_id, data={"account_id": account.id, "code": code},
    )
