from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class AccountKeysIn(CustomBaseModel):
    """The club's own Meta app's keys. The token and the secret are checked
    with Meta, encrypted, and never shown, logged or audited again."""

    waba_id: str = Field(..., max_length=40, description="WhatsApp Business Account ID")
    phone_number_id: str = Field(..., max_length=40, description="The number's ID at Meta (not the number)")
    access_token: str = Field(..., max_length=2048, description="A system user's permanent token")
    app_secret: str = Field(..., max_length=64, description="App settings → Basic → App secret")
    app_id: Optional[str] = Field(None, max_length=40, description="Needed to submit a template with a document header")


class AccountSettingsIn(CustomBaseModel):
    #: Send the club's tickets, certificates and reminders from its own number.
    routing: Optional[bool] = None
    retention_months: Optional[int] = Field(None, ge=1, le=36)


class UpgradeOut(CustomBaseModel):
    feature: Optional[str] = None
    label: Optional[str] = None
    required_plan: Optional[str] = None
    required_plan_name: Optional[str] = None
    upgrade_url: Optional[str] = None


class AccountOut(CustomBaseModel):
    #: False when the server has no WHATSAPP_KEYS_SECRET: nothing can be connected.
    available: bool
    #: False when the club's plan lacks `whatsapp_own`: the page shows `upgrade`.
    plan_allows: bool
    upgrade: Optional[UpgradeOut] = None
    connected: bool
    #: active | failed | disconnected, or null with no number.
    status: Optional[str] = None
    mode: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    app_id: Optional[str] = None
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    token_last4: Optional[str] = None
    #: GREEN | YELLOW | RED | UNKNOWN
    quality_rating: Optional[str] = None
    #: TIER_250, TIER_1K, TIER_10K, TIER_100K, TIER_UNLIMITED ...
    messaging_limit_tier: Optional[str] = None
    quality_checked_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    last_webhook_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_code: Optional[str] = None
    routing: bool = True
    retention_months: int = 12
    #: Paste into the Meta app → WhatsApp → Configuration, with the verify token.
    webhook_url: Optional[str] = None
    verify_token: Optional[str] = None
    #: The webhook fields to subscribe to.
    webhook_fields: list[str] = Field(default_factory=list)
    can_manage: bool = False
    can_inbox: bool = False


class QualityPoint(CustomBaseModel):
    quality_rating: Optional[str] = None
    messaging_limit_tier: Optional[str] = None
    previous_rating: Optional[str] = None
    previous_tier: Optional[str] = None
    source: str
    created_at: datetime


class StatsOut(CustomBaseModel):
    days: int
    conversations: int
    open: int
    unanswered: int
    unread: int
    window_open: int
    received: int
    replies: int
    sent_by_deliveries: int
    median_response_seconds: Optional[int] = None
    average_response_seconds: Optional[int] = None
    oldest_unanswered_at: Optional[datetime] = None


class OverviewOut(CustomBaseModel):
    account: AccountOut
    stats: Optional[StatsOut] = None
    quality_history: list[QualityPoint] = Field(default_factory=list)


class ContactEvent(CustomBaseModel):
    id: int
    name: Optional[str] = None
    event_datetime: Optional[datetime] = None
    registration_id: Optional[UUID] = None


class ContactOut(CustomBaseModel):
    name: Optional[str] = None
    #: Their number in digits; null when Meta gives only their BSUID.
    wa_id: Optional[str] = None
    #: Meta's business-scoped user id ("IN.1349..."), which every webhook carries.
    bsuid: Optional[str] = None
    #: Their WhatsApp username, when they have one.
    username: Optional[str] = None
    user_id: Optional[int] = None
    registration_id: Optional[UUID] = None
    events: list[ContactEvent] = Field(default_factory=list)


class LastMessage(CustomBaseModel):
    direction: Optional[str] = None
    type: Optional[str] = None
    preview: Optional[str] = None
    status: Optional[str] = None
    at: Optional[datetime] = None


class ConversationOut(CustomBaseModel):
    id: int
    #: Their number in digits; null for someone with a username whose
    #: number Meta doesn't share (then `bsuid`, and `username`, say who).
    wa_id: Optional[str] = None
    bsuid: Optional[str] = None
    username: Optional[str] = None
    #: "+<wa_id>", or null with it.
    phone: Optional[str] = None
    profile_name: Optional[str] = None
    contact: ContactOut
    #: open | closed
    status: str
    unread_count: int
    awaiting_reply: bool
    awaiting_since: Optional[datetime] = None
    last_message: Optional[LastMessage] = None
    last_inbound_at: Optional[datetime] = None
    #: Free text may be sent until window_expires_at; after it, a template only.
    window_open: bool
    window_expires_at: Optional[datetime] = None


class ConversationList(CustomBaseModel):
    items: list[ConversationOut]
    total: int
    unread_total: int = 0


class MediaOut(CustomBaseModel):
    #: The dashboard's authenticated proxy: never Meta's own address.
    url: str
    mime: Optional[str] = None
    filename: Optional[str] = None


class TemplateUsed(CustomBaseModel):
    name: str
    language: Optional[str] = None
    params: Optional[dict] = None


class MessageError(CustomBaseModel):
    code: Optional[str] = None
    title: Optional[str] = None


class MessageOut(CustomBaseModel):
    id: int
    #: in | out
    direction: str
    type: str
    body: Optional[str] = None
    media: Optional[MediaOut] = None
    location: Optional[dict] = None
    template: Optional[TemplateUsed] = None
    context_wamid: Optional[str] = None
    wamid: Optional[str] = None
    #: received | pending | sent | delivered | read | failed
    status: str
    status_at: Optional[datetime] = None
    error: Optional[MessageError] = None
    #: inbox | delivery, for what was sent
    via: Optional[str] = None
    sent_by: Optional[str] = None
    created_at: datetime
    wa_timestamp: Optional[datetime] = None


class MessageList(CustomBaseModel):
    conversation: ConversationOut
    items: list[MessageOut]
    #: Older messages exist before the first item.
    has_more: bool = False


class SendIn(CustomBaseModel):
    """`text` inside the 24-hour window; `template` (an approved one) any time."""

    type: str = Field(..., pattern="^(text|template)$")
    text: Optional[str] = Field(None, max_length=4096)
    template: Optional[str] = Field(None, max_length=512)
    language: Optional[str] = Field(None, max_length=16)
    #: {"header": [...], "body": [...], "buttons": {"0": "..."}}
    params: dict[str, Any] = Field(default_factory=dict)


class ConversationPatch(CustomBaseModel):
    status: str = Field(..., pattern="^(open|closed)$")


class StartIn(CustomBaseModel):
    registration_id: UUID


class TemplateHeaderIn(CustomBaseModel):
    text: Optional[str] = Field(None, max_length=200)
    examples: list[str] = Field(default_factory=list)


class TemplateBodyIn(CustomBaseModel):
    text: str = Field(..., max_length=2000)
    examples: list[str] = Field(default_factory=list)


class TemplateButtonIn(CustomBaseModel):
    #: QUICK_REPLY | URL | PHONE_NUMBER
    type: str
    text: str = Field(..., max_length=60)
    url: Optional[str] = Field(None, max_length=2000)
    example: Optional[str] = Field(None, max_length=2000)
    phone_number: Optional[str] = Field(None, max_length=30)


class TemplateIn(CustomBaseModel):
    name: str = Field(..., max_length=512)
    language: str = Field(..., max_length=16)
    #: UTILITY | MARKETING
    category: str
    header: Optional[TemplateHeaderIn] = None
    body: TemplateBodyIn
    footer: Optional[str] = Field(None, max_length=120)
    buttons: list[TemplateButtonIn] = Field(default_factory=list)


class TemplateButtonOut(CustomBaseModel):
    index: int
    type: str
    text: Optional[str] = None
    url: Optional[str] = None
    phone_number: Optional[str] = None
    variables: int = 0


class TemplateHeaderOut(CustomBaseModel):
    format: Optional[str] = None
    text: Optional[str] = None
    variables: int = 0


class TemplateOut(CustomBaseModel):
    id: Optional[str] = None
    name: str
    language: str
    category: Optional[str] = None
    #: APPROVED | PENDING | REJECTED | PAUSED | DISABLED | ...
    status: Optional[str] = None
    rejected_reason: Optional[str] = None
    quality: Optional[str] = None
    header: Optional[TemplateHeaderOut] = None
    body: str = ""
    body_variables: int = 0
    footer: Optional[str] = None
    buttons: list[TemplateButtonOut] = Field(default_factory=list)


class TemplateList(CustomBaseModel):
    items: list[TemplateOut]
    #: True when Meta couldn't be asked and this is what was last read.
    stale: bool = False
    #: MyOtherApp's ticket, reminder and certificate templates the account lacks.
    standard_missing: list[str] = Field(default_factory=list)


class StandardInstallOut(CustomBaseModel):
    #: name -> submitted | exists | why not
    results: dict[str, str]


class AdminWhatsAppOut(CustomBaseModel):
    club_id: int
    connected: bool
    status: Optional[str] = None
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    quality_rating: Optional[str] = None
    messaging_limit_tier: Optional[str] = None
    quality_checked_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    last_webhook_at: Optional[datetime] = None
    last_error: Optional[str] = None
    routing: Optional[bool] = None
    connected_at: Optional[datetime] = None
    conversations: int = 0
    plan_allows: bool = False
