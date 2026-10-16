"""What the emails screens send and receive. camelCase on the wire, like
the design contract these carry."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.layout.model import Wire
from app.core.mail.schema import EmailAttachments, EmailAudience, EmailDesign, EmailStage, EmailTemplateFields

MessageKindName = Literal["BROADCAST", "TEMPLATE", "TEST"]


class EmailTemplateOut(EmailTemplateFields):
    id: UUID
    club_id: int
    event_id: Optional[int]
    stage: EmailStage
    version: int
    source_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class EmailTemplateCreate(EmailTemplateFields):
    stage: EmailStage = "CUSTOM"


class MessageOut(Wire):
    id: UUID
    kind: MessageKindName
    template_id: Optional[UUID]
    scheduled: bool
    subject: Optional[str]
    body: str = ""
    audience: str
    roles: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    status: Literal["SENDING", "SENT"]
    recipients: int
    sent_count: int
    failed_count: int
    skipped_count: int
    created_at: datetime
    created_by_name: Optional[str] = None


class JourneyStageOut(Wire):
    stage: EmailStage
    label: str
    description: str
    template: EmailTemplateOut
    # When the timer will send it, if it has one and has not yet.
    due_at: Optional[datetime] = None
    last_message: Optional[MessageOut] = None


class TemplatesOut(Wire):
    journey: list[JourneyStageOut]
    custom: list[EmailTemplateOut]


class EmailContext(Wire):
    tokens: list[dict]
    sample_values: dict
    brand_primary: str
    logo_url: Optional[str]
    event_icon_url: Optional[str]
    stages: dict
    blocks: dict


class PreviewRequest(Wire):
    subject: str = Field(max_length=200)
    design: EmailDesign
    registration_id: Optional[UUID] = None


class PreviewResponse(Wire):
    subject: str
    html: str
    text: str
    # Template problems the editor should show, if any.
    problems: list[str] = Field(default_factory=list)


class TestSendRequest(PreviewRequest):
    to: EmailStr
    attachments: EmailAttachments = Field(default_factory=EmailAttachments)


class TestSendResponse(Wire):
    sent: bool
    to: str
    subject: str


class AudienceRequest(EmailAudience):
    pass


class AudienceCount(Wire):
    recipients: int
    without_email: int


class MessageCreate(Wire):
    """A one-off email, typed now. `body` is paragraphs with the usual
    tokens and markup; a full `design` may be sent instead."""

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)
    design: Optional[EmailDesign] = None
    attachments: EmailAttachments = Field(default_factory=EmailAttachments)
    audience: EmailAudience = Field(default_factory=EmailAudience)
    # From the composer, so a double submit sends once.
    client_ref: Optional[UUID] = None


class MessageDeliveryOut(Wire):
    registration_id: UUID
    name: str
    email: Optional[str]
    ticket_id: str
    channel: str
    status: str
    error: Optional[str] = None
    sent_at: Optional[datetime] = None


class MessagePage(Wire):
    items: list[MessageOut]
    total: int
    page: int
    page_size: int


class MessageDetail(Wire):
    message: MessageOut
    deliveries: list[MessageDeliveryOut]
    total: int
    page: int
    page_size: int


class ImageUpload(Wire):
    key: str
    url: str
    width: int
    height: int


class LibraryTemplateOut(EmailTemplateOut):
    label: str
    usage: dict


class LibraryOverview(BaseModel):
    """The Emails page: the library with its use and results, the month, what goes out next."""

    figures: dict
    previous: dict
    delta: Optional[dict] = None
    templates: list[LibraryTemplateOut]
    queue: list[dict]
    recent: list[dict]
    attention: dict
    all_time: dict
    generated_at: datetime
