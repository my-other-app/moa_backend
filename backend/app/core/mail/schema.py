"""Emails a guest receives are designed, not typed.

A named template is a theme plus a column of blocks, each block's text a
ticket-style template ({{tokens}} and {% if %}). A template also carries
how it is sent -- to whom, on which channels, when, with what attached --
so a name is enough to send it. Four stages make one journey per event:
the invite (which is the ticket email), a pre-event note, a reminder as
the doors open, and a thank-you afterwards.

The wire format is camelCase, shared with the editor; this is the Python
half of Muster's email contract.
"""
from typing import Annotated, Literal, Optional, Union

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.core.layout.model import Wire
from app.core.layout.tokens import TOKEN_BY_KEY

Hex = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]

EmailStage = Literal["INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT", "CUSTOM"]
JOURNEY_STAGES: list[str] = ["INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT"]

EMAIL_STAGE_META: dict[str, dict] = {
    "INVITE": {"label": "Invite", "note": "The ticket email — sent with the ticket when tickets go out.", "order": 1},
    "PRE_EVENT": {"label": "Pre-event", "note": "The day before: venue, what to bring.", "order": 2},
    "REMINDER": {"label": "Reminder", "note": "As the doors open, to guests not yet inside.", "order": 3},
    "POST_EVENT": {"label": "Post-event", "note": "After the close: thanks, the certificate, what is next.", "order": 4},
    "CUSTOM": {"label": "Custom", "note": "Any other email, sent by hand or on a timer.", "order": 5},
}

# Web-safe families only: an email client draws with the fonts it has.
EmailFont = Literal["sans", "serif", "mono"]
EMAIL_FONT_STACK = {
    "sans": "Helvetica Neue, Helvetica, Arial, sans-serif",
    "serif": "Georgia, 'Times New Roman', Times, serif",
    "mono": "'Courier New', Courier, monospace",
}

DEFAULT_FOOTER = "{{org_name}}{% if organiser_email %} · {{organiser_email}}{% endif %}"


class EmailTheme(Wire):
    font: EmailFont = "sans"
    # Behind the card, edge to edge.
    background: Hex = "#F3F4F1"
    # The card the blocks sit on.
    surface: Hex = "#FFFFFF"
    text: Hex = "#141614"
    muted: Hex = "#6B716C"
    # Buttons and links. 'brand' follows the club's primary colour.
    accent: str = "brand"
    accent_text: Hex = "#FFFFFF"
    radius: int = Field(default=12, ge=0, le=32)
    # The club's logo at the top of the card, or nothing.
    logo: Literal["org_logo", "none"] = "org_logo"
    # Small print under the card; a template like any block's text.
    footer: str = Field(default=DEFAULT_FOOTER, max_length=1000)

    @field_validator("accent")
    @classmethod
    def _accent(cls, v: str) -> str:
        import re

        if v == "brand" or re.match(r"^#[0-9A-Fa-f]{6}$", v):
            return v
        raise ValueError("The accent is a 6-digit hex colour, or 'brand'.")


class BlockStyle(Wire):
    """What every block can set for itself; null means the theme decides."""

    align: Literal["left", "center", "right"] = "left"
    color: Optional[Hex] = None
    background: Optional[Hex] = None
    # Font size in px.
    size: Optional[int] = Field(default=None, ge=10, le=72)
    weight: Optional[Literal["400", "600", "700"]] = None
    padding_top: int = Field(default=0, ge=0, le=120)
    padding_bottom: int = Field(default=8, ge=0, le=120)
    letter_spacing: float = Field(default=0, ge=-2, le=12)


class _BlockBase(Wire):
    id: str = Field(min_length=1, max_length=40)
    style: BlockStyle = Field(default_factory=BlockStyle)


# Inline markup inside text: **bold**, _italic_, [label](url), and tokens.
Text = Annotated[str, StringConstraints(max_length=4000)]


class HeadingBlock(_BlockBase):
    kind: Literal["heading"] = "heading"
    text: Text
    level: Literal[1, 2] = 1


class ParagraphBlock(_BlockBase):
    kind: Literal["paragraph"] = "paragraph"
    text: Text


class ButtonBlock(_BlockBase):
    kind: Literal["button"] = "button"
    label: str = Field(max_length=120)
    # A link, or a token that becomes one: {{ticket_url}}.
    href: str = Field(max_length=1000)
    variant: Literal["primary", "secondary"] = "primary"


class ImageBlock(_BlockBase):
    kind: Literal["image"] = "image"
    source: Literal["upload", "org_logo", "event_icon", "qr"] = "upload"
    image_key: Optional[str] = Field(default=None, max_length=300)
    alt: str = Field(default="", max_length=200)
    # Percent of the card's width.
    width: int = Field(default=100, ge=10, le=100)
    href: Optional[str] = Field(default=None, max_length=1000)


class DividerBlock(_BlockBase):
    kind: Literal["divider"] = "divider"
    color: Optional[Hex] = None


class SpacerBlock(_BlockBase):
    kind: Literal["spacer"] = "spacer"
    height: int = Field(default=24, ge=4, le=160)


class ListBlock(_BlockBase):
    kind: Literal["list"] = "list"
    items: list[Text] = Field(min_length=1, max_length=30)
    ordered: bool = False


class DetailsRow(Wire):
    label: str = Field(max_length=60)
    value: Text


class DetailsBlock(_BlockBase):
    """Label · value rows -- date, venue, ticket -- set as a tidy table."""

    kind: Literal["details"] = "details"
    rows: list[DetailsRow] = Field(min_length=1, max_length=12)


class TicketCardBlock(_BlockBase):
    """The ticket at a glance: the guest, what they hold, and the QR that
    admits them. Everything but the QR is a template. `rows` left empty is
    the card as it has always been -- event, when, where, admission."""

    kind: Literal["ticket_card"] = "ticket_card"
    show_qr: bool = True
    show_venue: bool = True
    # The small line above the name. Empty hides it.
    eyebrow: Text = "{{ticket_type}}"
    # The line a guest looks for first.
    title: Text = "{{attendee_name}}"
    rows: list[DetailsRow] = Field(default_factory=list, max_length=10)
    note: Text = "Show this code at the door, on your phone or printed."


EmailBlock = Annotated[
    Union[HeadingBlock, ParagraphBlock, ButtonBlock, ImageBlock, DividerBlock, SpacerBlock, ListBlock, DetailsBlock, TicketCardBlock],
    Field(discriminator="kind"),
]

EMAIL_BLOCK_META: dict[str, dict] = {
    "heading": {"label": "Heading", "note": "A title line", "group": "Text"},
    "paragraph": {"label": "Text", "note": "A paragraph; **bold**, _italic_ and [links](…) inside", "group": "Text"},
    "list": {"label": "List", "note": "Bullets or numbers", "group": "Text"},
    "button": {"label": "Button", "note": "A link drawn as a button", "group": "Layout"},
    "divider": {"label": "Divider", "note": "A thin rule", "group": "Layout"},
    "spacer": {"label": "Spacer", "note": "Empty room", "group": "Layout"},
    "details": {"label": "Details", "note": "Label and value rows", "group": "Layout"},
    "image": {"label": "Image", "note": "An upload, the logo or the QR", "group": "Media"},
    "ticket_card": {"label": "Ticket card", "note": "Event, date, venue, QR", "group": "Event"},
}


class EmailDesign(Wire):
    theme: EmailTheme = Field(default_factory=EmailTheme)
    blocks: list[EmailBlock] = Field(default_factory=list, max_length=60)


# --- how it is sent -------------------------------------------------------------


class EmailAttachments(Wire):
    # Each guest's own ticket, not one file shared between them.
    ticket_pdf: bool = False
    # Each guest's own certificate, and only where one has been issued.
    certificate_pdf: bool = False


class EmailAudience(Wire):
    who: Literal["ALL", "CHECKED_IN", "NOT_CHECKED_IN"] = "ALL"
    # Narrows to these roles; empty means everyone.
    roles: list[str] = Field(default_factory=list, max_length=10)


class EmailTrigger(Wire):
    mode: Literal["MANUAL", "ON_TICKETS", "BEFORE_START", "AFTER_END"] = "MANUAL"
    minutes: Optional[int] = Field(default=None, ge=1, le=60 * 24 * 30)

    @model_validator(mode="after")
    def _minutes_when_timed(self):
        if self.mode in ("BEFORE_START", "AFTER_END") and self.minutes is None:
            raise ValueError("A timed email needs a number of minutes.")
        if self.mode in ("MANUAL", "ON_TICKETS"):
            self.minutes = None
        return self


class EmailTemplateFields(Wire):
    name: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=200)
    design: EmailDesign = Field(default_factory=EmailDesign)
    attachments: EmailAttachments = Field(default_factory=EmailAttachments)
    audience: EmailAudience = Field(default_factory=EmailAudience)
    trigger: EmailTrigger = Field(default_factory=EmailTrigger)
    channels: list[Literal["email"]] = Field(default_factory=lambda: ["email"], min_length=1)
    enabled: bool = True

    @field_validator("name", "subject")
    @classmethod
    def _trimmed(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Cannot be empty.")
        return v


# Tokens only an email has, beside every ticket token.
# The code the door types when a QR will not scan: the ticket's own id. The
# admission number no longer checks anyone in, so an email names this one.
# An email-only token while the ticket catalogue still reads {{ticket_code}}
# as an alias of the admission number; once the catalogue has it as a token
# of its own, that one is used and this steps aside.
_TICKET_CODE = {
    "key": "ticket_code", "group": "Ticket", "label": "Ticket code", "sample": "MOA-1K2F9-QX7RT",
    "note": "What the door types when the QR will not scan.",
}

EMAIL_ONLY_TOKENS: list[dict] = [
    {"key": "ticket_url", "group": "Links", "label": "Ticket PDF link", "sample": "https://api.myotherapp.in/api/v1/files/…"},
    {"key": "certificate_url", "group": "Links", "label": "Certificate PDF link", "sample": "https://api.myotherapp.in/api/v1/files/…"},
    {"key": "organiser_email", "group": "Club", "label": "Organiser contact email", "sample": "hello@yukthi.example"},
    {"key": "event_start", "group": "Event", "label": "Start, date and time", "sample": "Wed 16 Sept 2026 · 09:00"},
    {"key": "doors_in", "group": "Event", "label": "Minutes until the doors open", "sample": "30"},
    *([] if "ticket_code" in TOKEN_BY_KEY else [_TICKET_CODE]),
]
EMAIL_TOKEN_KEYS: frozenset = frozenset(t["key"] for t in EMAIL_ONLY_TOKENS)
