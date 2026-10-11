"""Which channels have a provider behind them.

A channel with no provider sends nothing and says so: a send on it is
recorded as skipped with the reason, never as sent. The day a WhatsApp
provider is wired in, this is the one place that changes shape.
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
    ok = bool(getattr(settings, "SES_ACCESS_KEY", "") and getattr(settings, "SES_SECRET_KEY", "") and getattr(settings, "SES_DEFAULT_SENDER", ""))
    return ChannelProvider("email", "Amazon SES", ok, "Delivery, bounce and complaint notices arrive through the SES hook." if ok else "No email provider is configured — nothing is sent on this channel.")


def whatsapp_provider() -> ChannelProvider:
    ok = bool(getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") and getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", ""))
    return ChannelProvider("whatsapp", "WhatsApp Cloud API", ok, "Statuses arrive through the WhatsApp hook." if ok else "No WhatsApp provider is configured — nothing is sent on this channel.")


def providers() -> list:
    return [email_provider().as_dict(), whatsapp_provider().as_dict()]


NO_WHATSAPP = "no WhatsApp provider is configured — nothing was sent"
BOUNCED = "that address bounced — correct it to send again"
