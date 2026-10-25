from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import EmailStr, Field

from app.core.response.base_model import CustomBaseModel

#: The most a ticket may cost, in rupees: a typo of a few zeros is refused
#: rather than charged.
MAX_PRICE = 1_000_000


# ─── Ticket types ────────────────────────────────────────────────────────


class TicketTypeIn(CustomBaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    #: Rupees; 0 is free.
    price: float = Field(0, ge=0, le=MAX_PRICE)
    #: Null: no cap of its own.
    capacity: Optional[int] = Field(None, ge=0, le=1_000_000)
    sales_start: Optional[datetime] = Field(None)
    sales_end: Optional[datetime] = Field(None)
    is_hidden: bool = Field(False)
    per_person_limit: Optional[int] = Field(None, ge=1, le=100)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)


class TicketTypePatch(CustomBaseModel):
    """Only the fields sent are changed; a null clears what may be cleared."""

    name: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    price: Optional[float] = Field(None, ge=0, le=MAX_PRICE)
    capacity: Optional[int] = Field(None, ge=0, le=1_000_000)
    sales_start: Optional[datetime] = Field(None)
    sales_end: Optional[datetime] = Field(None)
    is_hidden: Optional[bool] = Field(None)
    per_person_limit: Optional[int] = Field(None, ge=1, le=100)
    sort_order: Optional[int] = Field(None, ge=0, le=1000)


class TicketTypeItem(TicketTypeIn):
    """One type in the editor's list: with its `id` it is changed, without it is made."""

    id: Optional[int] = Field(None)


class TicketTypesSave(CustomBaseModel):
    """The editor's list, saved at once (see types.save_all)."""

    types: list[TicketTypeItem] = Field(..., min_length=1, max_length=50)
    #: The event's total across its types (null: none). Left out: unchanged.
    event_capacity: Optional[int] = Field(None, ge=0, le=1_000_000)


class TicketTypesOrder(CustomBaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=50)


class TicketTypeOut(CustomBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    price: float
    capacity: Optional[int] = None
    sales_start: Optional[datetime] = None
    sales_end: Optional[datetime] = None
    is_hidden: bool = False
    per_person_limit: Optional[int] = None
    sort_order: int = 0
    is_default: bool = False
    #: Holding a place now: paid, or confirmed with nothing to pay.
    sold: int = 0
    #: Left for someone new (null: no cap).
    available: Optional[int] = None
    waiting: int = 0
    offered: int = 0
    #: Someone holds a ticket of it: it can be hidden, not removed.
    has_registrations: bool = False


class TicketTypesOut(CustomBaseModel):
    types: list[TicketTypeOut]
    #: One type, the default: its price and capacity are the event's own.
    follows_event: bool
    #: The event's total across its types (null: none).
    event_capacity: Optional[int] = None
    event_available: Optional[int] = None
    #: The plan's cap on types per event (null: no cap).
    max_types: Optional[int] = None
    currency: str = "INR"


class TypeSales(CustomBaseModel):
    id: int
    name: str
    price: float
    capacity: Optional[int] = None
    is_hidden: bool = False
    sold: int = 0
    paid: int = 0
    revenue: float = 0.0
    pending: int = 0
    discount_total: float = 0.0
    available: Optional[int] = None
    waiting: int = 0
    offered: int = 0


class CodeSales(CustomBaseModel):
    id: int
    code: str
    uses: int
    discount_total: float


class SalesOut(CustomBaseModel):
    types: list[TypeSales]
    totals: dict[str, float]
    codes: list[CodeSales]
    event_capacity: Optional[int] = None
    event_available: Optional[int] = None
    waitlist_enabled: bool = False
    currency: str = "INR"


# ─── The event page's tickets (public) ──────────────────────────────────


class PublicTicketType(CustomBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    price: float
    currency: str = "INR"
    is_free: bool
    capacity: Optional[int] = None
    #: Left for someone new now; null for no limit.
    available: Optional[int] = None
    #: on_sale, sold_out, not_started, ended, closed (the event's registration).
    status: str
    sales_start: Optional[datetime] = None
    sales_end: Optional[datetime] = None
    per_person_limit: Optional[int] = None
    is_hidden: bool = False
    #: Sold out, and its waiting list may be joined.
    waitlist: bool = False


class WaitlistBrief(CustomBaseModel):
    enabled: bool = False
    offer_hours: int = 24


class WaitlistEntryPublic(CustomBaseModel):
    id: UUID
    ticket_type_id: int
    ticket_type_name: Optional[str] = None
    #: waiting, offered, claimed, expired, removed, left.
    status: str
    #: Their place in line (1 is next), while waiting.
    position: Optional[int] = None
    offer_expires_at: Optional[datetime] = None
    joined_at: Optional[datetime] = None


class PublicTicketing(CustomBaseModel):
    types: list[PublicTicketType]
    #: A registration must name `ticket_type_id`.
    requires_choice: bool = False
    #: The event's places left for someone new (null: no total).
    available: Optional[int] = None
    sold_out: bool = False
    waitlist: WaitlistBrief = Field(default_factory=WaitlistBrief)
    #: The signed-in viewer's own places in line.
    viewer_waitlist: list[WaitlistEntryPublic] = Field(default_factory=list)


# ─── Discount codes ─────────────────────────────────────────────────────


class DiscountCodeIn(CustomBaseModel):
    code: str = Field(..., min_length=3, max_length=40)
    kind: Literal["percent", "amount"]
    #: Percent (up to 100) or rupees off.
    value: float = Field(..., gt=0, le=MAX_PRICE)
    #: One event's; null for every event of the club.
    event_id: Optional[int] = Field(None)
    #: Only these ticket types of the event (an event's code only).
    ticket_type_ids: Optional[list[int]] = Field(None, max_length=50)
    starts_at: Optional[datetime] = Field(None)
    ends_at: Optional[datetime] = Field(None)
    max_uses: Optional[int] = Field(None, ge=1, le=1_000_000)
    per_person_limit: Optional[int] = Field(None, ge=1, le=1000)
    is_active: bool = Field(True)


class DiscountCodePatch(CustomBaseModel):
    kind: Optional[Literal["percent", "amount"]] = Field(None)
    value: Optional[float] = Field(None, gt=0, le=MAX_PRICE)
    ticket_type_ids: Optional[list[int]] = Field(None, max_length=50)
    starts_at: Optional[datetime] = Field(None)
    ends_at: Optional[datetime] = Field(None)
    max_uses: Optional[int] = Field(None, ge=1, le=1_000_000)
    per_person_limit: Optional[int] = Field(None, ge=1, le=1000)
    is_active: Optional[bool] = Field(None)


class DiscountCodeOut(CustomBaseModel):
    id: int
    code: str
    kind: str
    value: float
    event_id: Optional[int] = None
    event_name: Optional[str] = None
    ticket_type_ids: Optional[list[int]] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    max_uses: Optional[int] = None
    per_person_limit: Optional[int] = None
    is_active: bool = True
    #: Paid or confirmed registrations it was used on.
    uses: int = 0
    #: What it took off them, in all.
    discount_total: float = 0.0
    #: active, scheduled, expired, used_up, inactive.
    status: str = "active"
    created_at: Optional[datetime] = None


class CheckCodeIn(CustomBaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    ticket_type_id: Optional[int] = Field(None)
    #: Who is asking, for a code held to a number of uses per person.
    email: Optional[EmailStr] = Field(None)
    phone: Optional[str] = Field(None, max_length=20)


class CheckCodeOut(CustomBaseModel):
    """Only what the code does to the price."""

    code: str
    ticket_type_id: int
    price: float
    discount: float
    amount: float
    #: Nothing left to pay: no payment step.
    free: bool


# ─── Waiting lists ──────────────────────────────────────────────────────


class WaitlistJoinIn(CustomBaseModel):
    ticket_type_id: Optional[int] = Field(None)
    full_name: str = Field(..., min_length=1, max_length=100)
    email: Optional[EmailStr] = Field(None)
    phone: Optional[str] = Field(None, max_length=20)
    #: "Tell me on WhatsApp too": needs `phone`.
    whatsapp_opt_in: bool = Field(False)


class WaitlistJoinOut(WaitlistEntryPublic):
    #: False when they were in line already (joining again is the same place).
    created: bool = True


class WaitlistSettingsIn(CustomBaseModel):
    enabled: Optional[bool] = Field(None)
    #: How long a claim link holds the place: 1 to 168 hours.
    offer_hours: Optional[int] = Field(None, ge=1, le=168)


class WaitlistSettingsOut(CustomBaseModel):
    enabled: bool = False
    offer_hours: int = 24
    #: The club's plan has the waiting list.
    allowed: bool = True


class WaitlistEntryClub(CustomBaseModel):
    id: UUID
    ticket_type_id: int
    ticket_type_name: Optional[str] = None
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    user_id: Optional[int] = None
    whatsapp_opt_in: bool = False
    status: str
    position: Optional[int] = None
    offered_at: Optional[datetime] = None
    offer_expires_at: Optional[datetime] = None
    offers_made: int = 0
    registration_id: Optional[UUID] = None
    created_at: datetime
    closed_at: Optional[datetime] = None
    #: The claim link of the standing offer, for the club to pass on.
    offer_url: Optional[str] = None


class WaitlistClubOut(CustomBaseModel):
    settings: WaitlistSettingsOut
    counts: dict[str, int]
    entries: list[WaitlistEntryClub]
    total: int


class OfferEvent(CustomBaseModel):
    id: int
    slug: str
    name: str
    event_datetime: datetime
    location_name: Optional[str] = None
    poster: Optional[dict] = None


class OfferOut(CustomBaseModel):
    """What the claim page shows."""

    id: UUID
    status: str
    event: OfferEvent
    ticket_type: dict[str, Any]
    full_name: str
    #: Masked: the page confirms whose place it is without showing it whole.
    email: Optional[str] = None
    phone: Optional[str] = None
    expires_at: Optional[datetime] = None
    #: The offer has no email address: the claim must send one.
    needs_email: bool = False
    #: The ticket costs money: the claim is followed by a payment.
    needs_payment: bool = False
    #: A claim was started and is waiting for its payment.
    registration_id: Optional[UUID] = None


class ClaimIn(CustomBaseModel):
    full_name: Optional[str] = Field(None, min_length=3, max_length=100)
    #: Only when the offer has none.
    email: Optional[EmailStr] = Field(None)
    phone: Optional[str] = Field(None, max_length=15)
    additional_details: Optional[dict[str, str | int | float | bool | list[str] | None]] = Field(None)
    organization: Optional[str] = Field(None, max_length=200)
    discount_code: Optional[str] = Field(None, max_length=60)
    whatsapp_opt_in: bool = Field(False)
