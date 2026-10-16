"""What became of each message, from the providers themselves."""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select, update

from app.api.deliveries.models import Deliveries, DeliveryOutcome
from app.api.emails.models import MessageDeliveries
from app.api.events.models import EventRegistrationsLink
from app.api.hooks.verdicts import Verdict, is_permanent, resend_verdict, ses_verdicts, whatsapp_verdict

SETTLED = (DeliveryOutcome.bounced, DeliveryOutcome.complained)


async def record(session, provider_id: str, channel: str, v: Verdict, at: datetime) -> int:
    """One message's fate, matched by the id the provider gave us when it
    took it. A ticket send and a message send live in different tables; an
    id belongs to exactly one of them. Outcomes only move forward: a late
    "delivered" must not undo a bounce."""
    touched = 0
    for model in (Deliveries, MessageDeliveries):
        values = {"outcome": v.outcome, "outcome_at": at, "reason": v.reason}
        # A message delivery keeps the reason without the provider's code.
        if hasattr(model, "reason_code"):
            values["reason_code"] = v.code
        res = await session.execute(
            update(model).where(model.provider_id == provider_id, or_(model.outcome.is_(None), model.outcome.not_in(SETTLED))).values(**values)
        )
        touched += res.rowcount or 0
    if touched and is_permanent(v):
        # Whoever it was, later sends must leave that address alone until it is corrected.
        reg_id = await session.scalar(select(Deliveries.registration_id).where(Deliveries.provider_id == provider_id)) or await session.scalar(
            select(MessageDeliveries.registration_id).where(MessageDeliveries.provider_id == provider_id)
        )
        if reg_id is not None:
            reg = await session.get(EventRegistrationsLink, reg_id)
            if reg is not None:
                if channel == "email":
                    reg.email_bounced_at = at
                    reg.email_bounce_reason = v.reason[:300]
                else:
                    reg.wa_bounced_at = at
                    reg.wa_bounce_reason = v.reason[:300]
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
    for message_id, _addr, v in ses_verdicts(message):
        matched += await record(session, message_id, "email", v, at)
    return {"matched": matched}


async def resend(session, event: dict) -> dict:
    """One Resend webhook event, matched by the id Resend gave the mail."""
    got = resend_verdict(event) if isinstance(event, dict) else None
    if got is None:
        return {"matched": 0}
    email_id, v = got
    return {"matched": await record(session, email_id, "email", v, _when(event.get("created_at")))}


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
