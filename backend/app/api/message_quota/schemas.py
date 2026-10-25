from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class ChannelUsageOut(CustomBaseModel):
    used: int = 0
    #: The plan's monthly allowance; null is unlimited, 0 is none included.
    limit: int | None = None
    #: Past the plan's allowance so far (paid from credits, or over).
    extra: int = 0
    #: Sends held back this month: nothing left of the allowance or the credits.
    blocked: int = 0
    #: Always 0: nothing is billed per message since prepaid credits.
    rate_paise: int = 0
    extra_paise: int = 0
    #: Share of the plan's allowance used, in whole percent; null when unlimited.
    percent: int | None = None
    #: unlimited | not_included (a limit of 0, nothing sent, no credits) | ok |
    #: near (80% of the allowance or more) | credits (the allowance used up,
    #: sending on bought credits) | blocked (nothing left: what is not a
    #: ticket, certificate or payment message is held back)
    state: str
    #: Of `used`: from the plan's allowance, from credits, and neither (a
    #: ticket or certificate with nothing left; never charged).
    plan_used: int = 0
    plan_remaining: int | None = None
    credits_used: int = 0
    over: int = 0
    #: Bought (and granted) credits left on the channel.
    credits_remaining: int = 0
    #: What is left of the allowance plus the credits; null when unlimited.
    total_remaining: int | None = None
    #: `used` by kind: ticket, certificate, payment, reminder, announcement,
    #: campaign, journey, survey.
    kinds: dict[str, int] = Field(default_factory=dict)
    #: The warnings sent this month (80, 100).
    warned: list[int] = Field(default_factory=list)


class UpgradeOut(CustomBaseModel):
    plan_key: str
    plan_name: str
    contact_sales: bool = False
    #: Its limits: {"email": n | null, "whatsapp": n | null}.
    limits: dict[str, int | None] = Field(default_factory=dict)


class MessageUsageOut(CustomBaseModel):
    #: YYYY-MM, a calendar month in India.
    period: str
    period_label: str
    period_start: datetime
    period_end: datetime
    plan_key: str | None = None
    plan_name: str | None = None
    #: "prepaid": past the plan's allowance, sends use bought credits.
    billing: str
    #: With nothing left, announcements, campaigns and reminders are held back.
    blocks_past_limit: bool = False
    #: An admin's own limits apply (Enterprise).
    custom: bool = False
    #: free_trial | enterprise_trial, when a trial gives the plan.
    trial: str | None = None
    channels: dict[str, ChannelUsageOut]
    #: Always 0 (kept for older dashboards).
    estimated_extra_paise: int = 0
    #: A plan with room for more, when one would help.
    upgrade: UpgradeOut | None = None


class StatementChannelOut(CustomBaseModel):
    used: int = 0
    limit: int | None = None
    extra: int = 0
    rate_paise: int = 0
    plan_used: int = 0
    credits_used: int = 0
    over: int = 0
    blocked: int = 0


class MessageStatementOut(CustomBaseModel):
    """A closed month."""

    #: YYYYMM.
    id: int
    period: str
    period_label: str
    plan_key: str | None = None
    billing: str
    email: StatementChannelOut
    whatsapp: StatementChannelOut
    #: What a statement of the retired overage billing charged (0 otherwise).
    amount_paise: int = 0
    #: nothing_due, or a retired statement's status: void (cancelled when
    #: overage billing was retired, never charged), charged, waived...
    status: str
    charged_at: datetime | None = None


class MessageHistoryOut(CustomBaseModel):
    #: Closed months, newest first.
    items: list[MessageStatementOut]


class OwnLimitsOut(CustomBaseModel):
    emails_per_month: int | None = None
    whatsapp_per_month: int | None = None
    email_overage_paise: int | None = None
    whatsapp_overage_paise: int | None = None
    note: str | None = None
    updated_at: datetime | None = None


class OwnLimitsIn(CustomBaseModel):
    """An Enterprise club's own limits: null is unlimited. The prices are
    from the retired overage billing: kept, never used."""

    emails_per_month: int | None = Field(None, ge=0, le=100_000_000)
    whatsapp_per_month: int | None = Field(None, ge=0, le=100_000_000)
    email_overage_paise: int | None = Field(None, ge=0, le=100_000)
    whatsapp_overage_paise: int | None = Field(None, ge=0, le=100_000)
    note: str | None = Field(None, max_length=200)


class AdminClubMessagesOut(CustomBaseModel):
    club_id: int
    club_name: str
    usage: MessageUsageOut
    history: list[MessageStatementOut]
    #: The admin's own limits for this club, or null.
    own_limits: OwnLimitsOut | None = None
    #: Whether they apply now: only while the club is on Enterprise.
    own_limits_apply: bool = False


# ─── Prepaid credits ─────────────────────────────────────────────────────

Channel = Literal["email", "whatsapp"]


class PackOut(CustomBaseModel):
    id: int
    channel: str
    quantity: int
    #: GST included, as plan prices are.
    price_paise: int
    sort_order: int = 0


class PublicPacksOut(CustomBaseModel):
    currency: str = "INR"
    prices_include_gst: bool = True
    validity_months: int = 12
    packs: list[PackOut]


class AdminPackOut(PackOut):
    active: bool = True
    updated_at: datetime | None = None
    #: Paid purchases of it.
    sold: int = 0


class AdminPacksOut(CustomBaseModel):
    prices_include_gst: bool = True
    validity_months: int = 12
    packs: list[AdminPackOut]


class PackIn(CustomBaseModel):
    channel: Channel
    quantity: int = Field(..., ge=1, le=10_000_000)
    price_paise: int = Field(..., ge=100, le=100_000_000)
    active: bool = True
    sort_order: int = Field(0, ge=0, le=10_000)


class PackPatch(CustomBaseModel):
    quantity: int | None = Field(None, ge=1, le=10_000_000)
    price_paise: int | None = Field(None, ge=100, le=100_000_000)
    active: bool | None = None
    sort_order: int | None = Field(None, ge=0, le=10_000)


class PlanBalanceOut(CustomBaseModel):
    limit: int | None = None
    used: int = 0
    remaining: int | None = None


class BoughtBalanceOut(CustomBaseModel):
    remaining: int = 0
    next_expiry: datetime | None = None
    #: Under 10% of the last pack bought is left.
    low: bool = False


class LotOut(CustomBaseModel):
    id: int
    channel: str
    #: purchase | grant
    source: str
    quantity: int
    remaining: int
    bought_at: datetime
    expires_at: datetime
    purchase_id: int | None = None
    #: A grant's reason (admins only).
    reason: str | None = None


class ChannelBalanceOut(CustomBaseModel):
    plan: PlanBalanceOut
    bought: BoughtBalanceOut
    #: The plan's remaining allowance plus the bought credits; null when unlimited.
    total_remaining: int | None = None
    #: Lots with credits left, the first to expire first (the club's view).
    lots: list[LotOut] | None = None


class PurchaseOut(CustomBaseModel):
    id: int
    receipt_number: str
    channel: str
    quantity: int
    amount_paise: int
    currency: str = "INR"
    #: created | paid | refunding | refund_failed | refunded
    status: str
    created_at: datetime
    paid_at: datetime | None = None
    razorpay_order_id: str
    razorpay_payment_id: str | None = None
    payment_method: str | None = None
    refunded_paise: int = 0
    refunded_at: datetime | None = None
    #: Its lot's credits left, and when they expire.
    credits_remaining: int | None = None
    expires_at: datetime | None = None


class ClubCreditsOut(CustomBaseModel):
    period: str
    period_label: str
    prices_include_gst: bool = True
    validity_months: int = 12
    #: False when buying is switched off (no Razorpay key).
    can_buy: bool = True
    channels: dict[str, ChannelBalanceOut]
    packs: list[PackOut]
    #: Paid purchases, newest first.
    purchases: list[PurchaseOut]


class OrderIn(CustomBaseModel):
    pack_id: int


class CheckoutPrefillOut(CustomBaseModel):
    email: str | None = None
    contact: str | None = None
    name: str | None = None


class OrderOut(CustomBaseModel):
    purchase_id: int
    razorpay_order_id: str
    amount_paise: int
    currency: str = "INR"
    #: The platform's publishable key, for Razorpay's checkout.
    key_id: str
    name: str
    description: str
    receipt_number: str
    prefill: CheckoutPrefillOut


class VerifyIn(CustomBaseModel):
    razorpay_order_id: str = Field(..., min_length=1, max_length=64)
    razorpay_payment_id: str = Field(..., min_length=1, max_length=64)
    razorpay_signature: str | None = Field(None, max_length=200)


class PartyOut(CustomBaseModel):
    id: int | None = None
    name: str | None = None
    email: str | None = None
    website: str | None = None


class ReceiptOut(CustomBaseModel):
    receipt_number: str
    issued_at: datetime | None = None
    status: str
    seller: PartyOut
    club: PartyOut
    item: str
    channel: str
    quantity: int
    amount_paise: int
    currency: str = "INR"
    prices_include_gst: bool = True
    razorpay_payment_id: str | None = None
    payment_method: str | None = None
    refunded_paise: int = 0
    refunded_at: datetime | None = None
    expires_at: datetime | None = None


class GrantedByOut(CustomBaseModel):
    id: int
    name: str | None = None


class AdminLotOut(LotOut):
    #: active | used | expired | refunded
    status: str
    granted_by: GrantedByOut | None = None


class AdminPurchaseOut(PurchaseOut):
    refund_reason: str | None = None
    refund_error: str | None = None
    razorpay_refund_id: str | None = None


class AdminClubCreditsOut(CustomBaseModel):
    club_id: int
    club_name: str
    channels: dict[str, ChannelBalanceOut]
    lots: list[AdminLotOut]
    purchases: list[AdminPurchaseOut]


class GrantIn(CustomBaseModel):
    channel: Channel
    quantity: int = Field(..., ge=1, le=1_000_000)
    reason: str = Field(..., min_length=3, max_length=300)
    valid_months: int = Field(12, ge=1, le=36)


class RefundIn(CustomBaseModel):
    reason: str = Field(..., min_length=3, max_length=300)
    #: Default: the unused share of what was paid.
    amount_paise: int | None = Field(None, ge=1, le=100_000_000)
