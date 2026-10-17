from datetime import datetime

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class GatewayKeysIn(CustomBaseModel):
    """A club's Razorpay keys. The secret is checked with Razorpay, encrypted,
    and never shown, logged or audited again."""

    key_id: str = Field(..., max_length=64, description="rzp_live_…")
    key_secret: str = Field(..., max_length=128)


class GatewayOut(CustomBaseModel):
    #: False when the server has no PAYMENT_KEYS_SECRET: nothing can be connected.
    available: bool
    connected: bool
    #: active | failed | replaced | disconnected | disabled, or null with no account.
    status: str | None = None
    #: keys (pasted key id and secret); oauth is reserved.
    mode: str | None = None
    #: Publishable, shown in full.
    key_id: str | None = None
    #: Paste into Razorpay → Webhooks, with the secret below. Only while connected.
    webhook_url: str | None = None
    webhook_secret: str | None = None
    #: The events to tick in Razorpay.
    webhook_events: list[str] = Field(default_factory=list)
    verified_at: datetime | None = None
    last_webhook_at: datetime | None = None
    last_error: str | None = None
    can_manage: bool = False


class AdminGatewayClub(CustomBaseModel):
    id: int
    name: str | None = None
    slug: str | None = None


class AdminGatewayItem(CustomBaseModel):
    id: int
    club: AdminGatewayClub | None = None
    status: str
    mode: str
    key_id: str
    verified_at: datetime | None = None
    last_webhook_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    ended_at: datetime | None = None


class AdminGatewayList(CustomBaseModel):
    items: list[AdminGatewayItem]
    total: int
