"""What became of each message, from the providers themselves."""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from app.api.deliveries.models import Deliveries, DeliveryAttempts, DeliveryOutcome
from app.api.emails.models import MessageDeliveries
from app.api.hooks.verdicts import Verdict, is_permanent, resend_verdict, ses_verdicts, whatsapp_verdict
from app.core.mail import suppression

SETTLED = (DeliveryOutcome.bounced, DeliveryOutcome.complained)


def _unsettled(outcome: Optional[DeliveryOutcome]) -> bool:
    return outcome is None or outcome not in SETTLED


async def record(session, provider_id: str, channel: str, v: Verdict, at: datetime, address: Optional[str] = None) -> int:
    """One message's fate, matched by the id the provider gave us when it
    took it -- an index lookup on each table (it was a sequential scan of
    both, two to four times a notification).

    The id is looked for on the sends themselves (their latest attempt) and
    among every attempt ever made (delivery_attempts), so a late bounce of a
    mail since sent again still lands. A verdict about an earlier attempt
    marks the send only when that attempt went to the address the guest has
    now: the bounce of a mistyped address must not mark the corrected one.
    Outcomes only move forward: a late "delivered" must not undo a bounce.

    A hard bounce or a complaint also puts the address on the platform's
    suppression list -- whether or not a send matched (account mail has no
    row) -- so no event or club mails it again. Returns the sends updated."""
    attempt = await session.scalar(select(DeliveryAttempts).where(DeliveryAttempts.provider_id == provider_id))
    address = suppression.normal(address) or (attempt.address if attempt is not None else "") or ""
    if attempt is not None and _unsettled(attempt.outcome):
        attempt.outcome, attempt.outcome_at, attempt.reason = v.outcome, at, (v.reason or "")[:400]

    touched = 0
    marked = None
    for model, via_attempt in (
        (Deliveries, getattr(attempt, "delivery_id", None)),
        (MessageDeliveries, getattr(attempt, "message_delivery_id", None)),
    ):
        match = model.provider_id == provider_id
        if via_attempt is not None:
            match = or_(match, model.id == via_attempt)
        for row in await session.scalars(select(model).options(joinedload(model.registration)).where(match)):
            reg = row.registration
            current = row.provider_id == provider_id
            same_address = channel != "email" or not address or reg is None or suppression.normal(reg.email) == address
            if not (current or same_address):
                continue
            if _unsettled(row.outcome):
                row.outcome, row.outcome_at, row.reason = v.outcome, at, (v.reason or "")[:400]
                # A message delivery keeps the reason without the provider's code.
                if hasattr(model, "reason_code"):
                    row.reason_code = v.code
                touched += 1
                if same_address and reg is not None:
                    marked = reg
    if is_permanent(v):
        if marked is not None:
            # Whoever it was, later sends must leave that address alone until it is corrected.
            if channel == "email":
                marked.email_bounced_at = at
                marked.email_bounce_reason = v.reason[:300]
            else:
                marked.wa_bounced_at = at
                marked.wa_bounce_reason = v.reason[:300]
        if channel == "email":
            dead_address = address or (suppression.normal(marked.email) if marked is not None else "")
            reason = suppression.COMPLAINT if v.outcome is DeliveryOutcome.complained else suppression.BOUNCE
            await suppression.add(session, dead_address, reason, v.reason)
    await session.commit()
    return touched


def _when(value: Optional[str]) -> datetime:
    if value:
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def ses(session, envelope_message: str) -> dict:
    """The SES notification carried inside the SNS envelope."""
    try:
        message = json.loads(envelope_message)
    except Exception:
        return {"matched": 0}
    if not isinstance(message, dict):
        return {"matched": 0}
    matched = 0
    mail = message.get("mail") or {}
    at = _when((message.get("bounce") or message.get("complaint") or message.get("delivery") or {}).get("timestamp") or mail.get("timestamp"))
    for message_id, addr, v in ses_verdicts(message):
        matched += await record(session, message_id, "email", v, at, address=addr)
    return {"matched": matched}


async def resend(session, event: dict) -> dict:
    """One Resend webhook event, matched by the id Resend gave the mail."""
    got = resend_verdict(event) if isinstance(event, dict) else None
    if got is None:
        return {"matched": 0}
    email_id, v = got
    to = (event.get("data") or {}).get("to")
    address = to[0] if isinstance(to, list) and len(to) == 1 else (to if isinstance(to, str) else None)
    return {"matched": await record(session, email_id, "email", v, _when(event.get("created_at")), address=address)}


async def whatsapp(session, payload: dict) -> dict:
    """Meta batches statuses: entry[].changes[].value.statuses[]."""
    matched = 0
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for st in (change.get("value") or {}).get("statuses") or []:
                v = whatsapp_verdict(st.get("status") or "", (st.get("errors") or [None])[0]) if st.get("status") else None
                if not st.get("id") or v is None:
                    continue
                ts = st.get("timestamp")
                at = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts and str(ts).isdigit() else datetime.now(timezone.utc)
                matched += await record(session, st["id"], "whatsapp", v, at)
    return {"matched": matched}
