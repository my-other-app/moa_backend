"""What a provider says, in words somebody at a desk can act on. The code is
kept beside the sentence so a support thread can quote it, and anything
unrecognised keeps the provider's own wording rather than being flattened
into "failed"."""
from dataclasses import dataclass
from typing import Optional

from app.api.deliveries.models import DeliveryOutcome


@dataclass(frozen=True)
class Verdict:
    outcome: DeliveryOutcome
    reason: str
    code: Optional[str] = None


def is_permanent(v: Verdict) -> bool:
    """Whether this verdict should stop later sends to the same address."""
    return v.outcome in (DeliveryOutcome.bounced, DeliveryOutcome.complained)


def ses_verdicts(message: dict) -> list:
    """SES's notification (classic) or event (configuration-set) shape: one
    verdict per recipient it names, keyed by the message id SES gave us when
    it took the mail. A hard bounce is permanent; everything else may still
    arrive."""
    kind = (message.get("notificationType") or message.get("eventType") or "").lower()
    mail = message.get("mail") or {}
    message_id = mail.get("messageId")
    if not message_id:
        return []
    out = []
    if kind == "bounce":
        b = message.get("bounce") or {}
        permanent = (b.get("bounceType") or "").lower() == "permanent"
        sub = b.get("bounceSubType")
        for r in b.get("bouncedRecipients") or [{}]:
            diag = (r.get("diagnosticCode") or "").strip()
            reason = diag[:300] if diag else ("The address does not exist" if permanent else "The mailbox would not take it just now")
            out.append((message_id, r.get("emailAddress"), Verdict(DeliveryOutcome.bounced if permanent else DeliveryOutcome.undelivered, reason, sub)))
    elif kind == "complaint":
        c = message.get("complaint") or {}
        for r in c.get("complainedRecipients") or [{}]:
            out.append((message_id, r.get("emailAddress"), Verdict(DeliveryOutcome.complained, "Marked as spam by the recipient", c.get("complaintFeedbackType"))))
    elif kind == "delivery":
        d = message.get("delivery") or {}
        for addr in d.get("recipients") or [None]:
            out.append((message_id, addr, Verdict(DeliveryOutcome.delivered, "Delivered to the mailbox", None)))
    elif kind == "deliverydelay":
        d = message.get("deliveryDelay") or {}
        for r in d.get("delayedRecipients") or [{}]:
            out.append((message_id, r.get("emailAddress"), Verdict(DeliveryOutcome.undelivered, "Held up on the way — the mail server is retrying", d.get("delayType"))))
    elif kind == "reject":
        r = message.get("reject") or {}
        for addr in (mail.get("destination") or [None]):
            out.append((message_id, addr, Verdict(DeliveryOutcome.undelivered, f"Refused by SES before sending: {r.get('reason') or 'no detail'}", "Reject")))
    # Send, Open, Click, RenderingFailure and Subscription tell us nothing a desk acts on.
    return out


# Meta's failure codes, the ones an event actually meets. Permanent ones mark
# the number so later sends skip it; the rest are worth trying again.
WHATSAPP_REASONS = {
    "131026": ("This number cannot receive WhatsApp messages", True),
    "131047": ("Outside the 24-hour window — only an approved template can reach them now", False),
    "131049": ("Held back by WhatsApp to protect the recipient from too much marketing", False),
    "131051": ("That kind of message is not supported", False),
    "131053": ("The attachment could not be uploaded", False),
    "132000": ("The template needs a different number of values than were sent", False),
    "132001": ("No approved template by that name in this language", False),
    "132005": ("The text is too long for the template", False),
    "132007": ("The template was refused for its content", False),
    "132012": ("A value does not fit the template’s format", False),
    "132015": ("The template is paused for poor quality", False),
    "132016": ("The template has been disabled", False),
    "133010": ("The sending number is not registered", False),
    "470": ("Outside the 24-hour window — only an approved template can reach them now", False),
    "368": ("The recipient’s account is restricted", True),
}


def whatsapp_verdict(status: str, error: Optional[dict] = None) -> Optional[Verdict]:
    if status == "delivered":
        return Verdict(DeliveryOutcome.delivered, "Delivered to their phone")
    if status == "read":
        return Verdict(DeliveryOutcome.read, "Read")
    if status == "failed":
        error = error or {}
        code = None if error.get("code") is None else str(error.get("code"))
        known = WHATSAPP_REASONS.get(code) if code else None
        reason = known[0] if known else (error.get("title") or error.get("message") or "WhatsApp could not deliver it")
        return Verdict(DeliveryOutcome.bounced if (known and known[1]) else DeliveryOutcome.undelivered, reason, code)
    # `sent` tells us nothing we did not already know.
    return None


def resend_verdict(event: dict) -> Optional[tuple]:
    """Resend's webhook event: `{"type": "email.bounced", "created_at", "data": {"email_id", "to", "bounce": {...}}}`.
    Returns `(email_id, verdict)`, or None for an event a desk does not act on."""
    kind = (event.get("type") or "").lower()
    data = event.get("data") or {}
    email_id = data.get("email_id")
    if not email_id:
        return None
    if kind == "email.delivered":
        return email_id, Verdict(DeliveryOutcome.delivered, "Delivered to the mailbox", None)
    if kind == "email.opened":
        return email_id, Verdict(DeliveryOutcome.read, "Opened", None)
    if kind == "email.bounced":
        b = data.get("bounce") or {}
        permanent = (b.get("type") or "").lower() in ("permanent", "hard", "")
        msg = (b.get("message") or "").strip()
        reason = msg[:300] if msg else ("The address does not exist" if permanent else "The mailbox would not take it just now")
        return email_id, Verdict(DeliveryOutcome.bounced if permanent else DeliveryOutcome.undelivered, reason, b.get("subType") or b.get("type"))
    if kind == "email.complained":
        return email_id, Verdict(DeliveryOutcome.complained, "Marked as spam by the recipient", None)
    if kind == "email.delivery_delayed":
        return email_id, Verdict(DeliveryOutcome.undelivered, "Held up on the way — the mail server is retrying", "delayed")
    if kind == "email.failed":
        reason = ((data.get("failed") or {}).get("reason") or "Resend could not send it").strip()
        return email_id, Verdict(DeliveryOutcome.undelivered, f"Refused before sending: {reason}"[:300], "failed")
    # email.sent, email.clicked and email.scheduled tell us nothing a desk acts on.
    return None
