from datetime import datetime

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class ChannelUsageOut(CustomBaseModel):
    used: int = 0
    #: The month's limit; null is unlimited, 0 is none included.
    limit: int | None = None
    #: Past the limit so far.
    extra: int = 0
    #: Sends held back this month because the plan cannot pay for more (Free).
    blocked: int = 0
    #: What one extra costs, in paise.
    rate_paise: int = 0
    #: What the extra comes to so far, in paise (0 on Free: nothing is billed).
    extra_paise: int = 0
    #: Share of the limit used, in whole percent; null when unlimited.
    percent: int | None = None
    #: unlimited | not_included (a limit of 0, nothing sent) | ok | near (80%
    #: or more) | over (past it, still sending) | blocked (past it on a plan
    #: that holds sends back)
    state: str
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
    #: razorpay: extra goes on the next invoice. manual: on a statement the
    #: team invoices. none: no payment method (Free, the free trial).
    billing: str
    #: Past the limit, announcements, campaigns and reminders are held back.
    blocks_past_limit: bool = False
    #: An admin's own limits apply (Enterprise).
    custom: bool = False
    #: free_trial | enterprise_trial, when a trial gives the plan.
    trial: str | None = None
    channels: dict[str, ChannelUsageOut]
    #: Both channels' extra so far, in paise.
    estimated_extra_paise: int = 0
    #: A plan with room for more, when one would help.
    upgrade: UpgradeOut | None = None


class StatementChannelOut(CustomBaseModel):
    used: int = 0
    limit: int | None = None
    extra: int = 0
    rate_paise: int = 0


class MessageStatementOut(CustomBaseModel):
    id: int
    period: str
    period_label: str
    plan_key: str | None = None
    billing: str
    email: StatementChannelOut
    whatsapp: StatementChannelOut
    amount_paise: int = 0
    #: nothing_due | pending | charging | charged | failed | unconfirmed |
    #: gave_up | manual | not_billable | waived
    status: str
    charged_at: datetime | None = None
    razorpay_addon_id: str | None = None


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
    """An Enterprise club's own limits: null is unlimited. The prices: null
    is the plan's."""

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
