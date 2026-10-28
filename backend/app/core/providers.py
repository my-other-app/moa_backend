"""Which channels have a provider behind them.

A channel with no provider sends nothing and says so: a send on it is
recorded as skipped with the reason, never as sent. WhatsApp sends once
the server holds its access token and phone number id (app/core/whatsapp.py),
and only to the guests who asked for it at registration.
"""
from dataclasses import asdict, dataclass

from app.config import settings


@dataclass(frozen=True)
class ChannelProvider:
    channel: str
    name: str
    configured: bool
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


def email_provider() -> ChannelProvider:
    if (getattr(settings, "MAIL_PROVIDER", "") or "ses").lower() == "ses_smtp":
        # SES through its SMTP interface: SMTP credentials instead of API keys,
        # the same SES behind them and the same hook for its notices.
        ok = all((getattr(settings, name, "") or "").strip() for name in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SES_DEFAULT_SENDER"))
        return ChannelProvider(
            "email", "Amazon SES (SMTP)", ok,
            "Sent through SES's SMTP interface; delivery, bounce and complaint notices arrive through the SES hook." if ok
            else "No email provider is configured — nothing is sent on this channel.",
        )
    ok = bool(getattr(settings, "SES_ACCESS_KEY", "") and getattr(settings, "SES_SECRET_KEY", "") and getattr(settings, "SES_DEFAULT_SENDER", ""))
    return ChannelProvider("email", "Amazon SES", ok, "Delivery, bounce and complaint notices arrive through the SES hook." if ok else "No email provider is configured — nothing is sent on this channel.")


def whatsapp_provider() -> ChannelProvider:
    """Configured when it can send: the token and the number to send from.
    The note says too whether the statuses can come back, which takes the
    app secret (the hook refuses every unsigned call without it)."""
    from app.core import whatsapp

    ok = whatsapp.configured()
    if not ok:
        note = "No WhatsApp provider is configured — nothing is sent on this channel."
    elif (getattr(settings, "WHATSAPP_APP_SECRET", "") or "").strip():
        note = "Tickets, certificates and reminders go to the guests who asked for WhatsApp; statuses arrive through the WhatsApp hook."
    else:
        note = "Tickets, certificates and reminders go to the guests who asked for WhatsApp, but no app secret is set, so no statuses come back."
    return ChannelProvider("whatsapp", "WhatsApp Cloud API", ok, note)


def providers() -> list:
    return [email_provider().as_dict(), whatsapp_provider().as_dict()]


NO_WHATSAPP = "no WhatsApp provider is configured — nothing was sent"
BOUNCED = "that address bounced — correct it to send again"
NO_WHATSAPP_OPT_IN = "they did not ask for WhatsApp at registration"
WA_BOUNCED = "WhatsApp could not reach that number — correct it to send again"
DEMO_ACCOUNT = "a demo account — nothing is sent to it"
