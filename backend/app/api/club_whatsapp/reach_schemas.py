from datetime import datetime
from typing import Any, Optional

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class KindsOut(CustomBaseModel):
    tickets: bool = True
    certificates: bool = True
    reminders: bool = True


class EmbeddedSignupConfig(CustomBaseModel):
    """What the dashboard opens Meta's flow with (public values)."""

    app_id: str
    config_id: str
    graph_version: str
    #: Embedded Signup's version: v5.
    version: str


class MessagingLimitOut(CustomBaseModel):
    tier: Optional[str] = None
    #: Unique people a day; null for no limit.
    per_day: Optional[int] = None


class ReachOut(CustomBaseModel):
    #: The master switch (PATCH /clubs/whatsapp/account {"routing"}).
    routing: bool = True
    #: Under it, each kind on its own (PATCH /clubs/whatsapp/account/routing).
    kinds: KindsOut
    #: "Connect with Meta": shown only when this is set (APP_WHATSAPP_EMBEDDED_SIGNUP on).
    embedded_signup: Optional[EmbeddedSignupConfig] = None
    #: keys | embedded, with a number.
    mode: Optional[str] = None
    #: A number connected with Meta: when its token expires (null: never), when it was registered, and
    #: whether it is a WhatsApp Business app number (never registered by MyOtherApp).
    token_expires_at: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    on_biz_app: bool = False
    #: Meta's marketing_messages_onboarding_status: ONBOARDED sends promotions through its Marketing Messages API.
    mm_status: Optional[str] = None
    messaging_limit: MessagingLimitOut
    #: The version of "Get updates from <club> on WhatsApp" a consent is kept with.
    consent_text_version: str
    consent_text: str


class KindsIn(CustomBaseModel):
    tickets: Optional[bool] = None
    certificates: Optional[bool] = None
    reminders: Optional[bool] = None


class EmbeddedSignupIn(CustomBaseModel):
    """What Meta's flow gave the dashboard, posted at once (the code lasts 30 seconds)."""

    code: str = Field(..., max_length=2048)
    phone_number_id: Optional[str] = Field(None, max_length=40)
    #: v5's session info; the first is the WhatsApp Business Account (Messaging account).
    messaging_account_ids: list[str] = Field(default_factory=list, max_length=20)
    #: v4's, in place of messaging_account_ids.
    waba_id: Optional[str] = Field(None, max_length=40)
    business_id: Optional[str] = Field(None, max_length=40)
    is_on_biz_app: bool = False
    #: FINISH, FINISH_ONLY_WABA, FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING ...
    event: Optional[str] = Field(None, max_length=60)


class EmbeddedSignupEventIn(CustomBaseModel):
    """Meta's flow ended without finishing."""

    #: CANCEL | ERROR
    event: str = Field(..., max_length=20)
    current_step: Optional[str] = Field(None, max_length=60)
    error_message: Optional[str] = Field(None, max_length=500)
    error_code: Optional[str] = Field(None, max_length=40)
    session_id: Optional[str] = Field(None, max_length=80)


class OkOut(CustomBaseModel):
    ok: bool = True


class SpendCategory(CustomBaseModel):
    category: str
    volume: int = 0
    cost: float = 0.0


class SendsOut(CustomBaseModel):
    deliveries: int = 0
    broadcasts: int = 0
    inbox: int = 0
    total: int = 0


class FallbackOut(CustomBaseModel):
    code: Optional[str] = None
    reason: Optional[str] = None
    count: int = 0


class SpendOut(CustomBaseModel):
    #: YYYY-MM, India's calendar.
    month: str
    #: The WhatsApp Business Account's currency, as Meta reports it.
    currency: Optional[str] = None
    #: When Meta's figure was read (the sweep, at most daily); null: not yet this month.
    fetched_at: Optional[datetime] = None
    checked_at: Optional[datetime] = None
    categories: list[SpendCategory] = Field(default_factory=list)
    volume: int = 0
    cost: float = 0.0
    #: Meta calls its figure approximate.
    approximate: bool = True
    note: str
    since: datetime
    from_your_number: SendsOut
    fallbacks: list[FallbackOut] = Field(default_factory=list)
    fallback_total: int = 0


class VariableIn(CustomBaseModel):
    #: name | event_name | event_date | text
    source: str = Field(..., max_length=16)
    value: Optional[str] = Field(None, max_length=1000)


class VariablesIn(CustomBaseModel):
    header: list[VariableIn] = Field(default_factory=list, max_length=10)
    body: list[VariableIn] = Field(default_factory=list, max_length=60)
    buttons: dict[str, VariableIn] = Field(default_factory=dict)


class BroadcastIn(CustomBaseModel):
    """An approved template (its category decides the kind), what fills its
    values, the event (an update's audience; a promotion may name it too),
    and when: now, or `scheduled_at`."""

    template: str = Field(..., max_length=512)
    language: Optional[str] = Field(None, max_length=16)
    #: Optional: checked against the template's category when given.
    kind: Optional[str] = Field(None, pattern="^(event_update|promotion)$")
    event_id: Optional[int] = None
    variables: VariablesIn = Field(default_factory=VariablesIn)
    scheduled_at: Optional[datetime] = None


class LimitOut(CustomBaseModel):
    tier: Optional[str] = None
    per_day: Optional[int] = None
    used_today: int = 0
    #: How many go today; the rest wait for the next days.
    today: int = 0
    days: int = 1


class EstimateOut(CustomBaseModel):
    recipients: int
    category: str
    currency: str = "INR"
    #: Meta's list rate for India, per delivered message, before GST.
    rate: float
    subtotal: float
    gst_rate: float
    gst: float
    total: float
    estimate: bool = True
    label: str
    messaging_limit: LimitOut


class ExcludedOut(CustomBaseModel):
    opted_out: int = 0
    no_consent: int = 0
    no_number: int = 0


class PreviewOut(CustomBaseModel):
    kind: str
    category: str
    template: str
    language: str
    event_id: Optional[int] = None
    recipients: int
    excluded: ExcludedOut
    estimate: EstimateOut
    #: The words the first person would get.
    sample: str = ""


class SkippedOut(CustomBaseModel):
    opted_out: int = 0
    no_consent: int = 0
    no_number: int = 0
    #: Held back to a later day by the messaging limit (sent since, or waiting).
    limit_spill: int = 0
    cancelled: int = 0
    not_sent: int = 0


class FailedReason(CustomBaseModel):
    code: Optional[str] = None
    title: Optional[str] = None
    count: int = 0


class ResultsOut(CustomBaseModel):
    recipients: int = 0
    queued: int = 0
    #: Queued for a later moment: the next day under the messaging limit, a retry, Meta's 24 hours after 131049.
    waiting: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    failed: int = 0
    failed_by_reason: list[FailedReason] = Field(default_factory=list)
    skipped: SkippedOut = Field(default_factory=SkippedOut)


class BroadcastEvent(CustomBaseModel):
    id: int
    name: str


class BroadcastOut(CustomBaseModel):
    id: int
    #: event_update | promotion
    kind: str
    template: str
    language: str
    category: str
    event: Optional[BroadcastEvent] = None
    variables: dict[str, Any] = Field(default_factory=dict)
    #: scheduled | sending | done | cancelled | failed
    status: str
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    #: messages | marketing_messages
    endpoint: Optional[str] = None
    estimate: Optional[EstimateOut] = None
    last_error: Optional[str] = None
    results: ResultsOut


class BroadcastList(CustomBaseModel):
    items: list[BroadcastOut]
    total: int


class AdminFallbackClub(CustomBaseModel):
    club_id: int
    club_name: Optional[str] = None
    fallbacks: int
    top_code: Optional[str] = None
    top_reason: Optional[str] = None


class AdminFallbacksOut(CustomBaseModel):
    since: datetime
    items: list[AdminFallbackClub]
