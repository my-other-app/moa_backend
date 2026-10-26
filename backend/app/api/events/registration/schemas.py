from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from pydantic import EmailStr, Field

from app.core.response.base_model import CustomBaseModel
from app.api.auth.schemas import Token
from app.api.users.schemas import UserPublic
from app.api.events.volunteer.schemas import ListVolunteersResponse
from app.api.deliveries.schemas import DeliveryBrief
from app.api.templates_audience import RegistrationRole


class EventRegistrationResponse(CustomBaseModel):
    event_name: str = Field(...)
    event_id: int = Field(...)
    event_registration_id: UUID = Field(...)
    pay_amount: float = Field(...)
    event_datetime: datetime = Field(...)
    ticket_id: str | None = Field(None)
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    additional_details: dict[str, Any] | None = Field(None)
    auth_token: Token | None = Field(None)
    store_order_id: str | None = Field(None)
    store_total: float = Field(0)
    #: Whether the ticket and the day-before reminder go by WhatsApp too.
    whatsapp_opt_in: bool = Field(False)
    #: Whether "Get updates from <club> on WhatsApp" was kept (club_whatsapp/consent.py).
    club_updates_whatsapp: bool = Field(False)
    #: The ticket type it is for, {id, name} (docs/TICKETING.md).
    ticket_type: dict[str, Any] | None = Field(None)
    #: The type's price before any code, in rupees.
    price: float | None = Field(None)
    #: The code used and what it took off, {code, amount}; null for none.
    discount: dict[str, Any] | None = Field(None)
    #: Paid, or (on a paid event) confirmed with nothing to pay.
    is_paid: bool = Field(False)


from app.api.store.schemas import StorePick  # noqa: E402


class EventRegistrationRequest(CustomBaseModel):
    full_name: str = Field(..., max_length=100, min_length=3)
    email: EmailStr = Field(...)
    phone: str | None = Field(None)
    #: Answers to the event's own questions: text, a number, or for a
    #: checkbox the options ticked (one, or a list). Bounded and checked
    #: against the questions by answers.py.
    additional_details: dict[str, str | int | float | bool | list[str] | None] | None = Field(None)
    organization: str | None = Field(None, max_length=200)
    #: The tracking link's code the page carried, so the sign-up is credited to it.
    ref: str | None = Field(None, max_length=16)
    #: What they picked from the event's shelf; paid with the ticket.
    store: list[StorePick] | None = Field(None, max_length=20)
    #: "Send my ticket and event reminders on WhatsApp", ticked: the consent
    #: WhatsApp's policy asks for. Unticked unless sent; it needs `phone`.
    #: Left out altogether (the app), a registration keeps what it had.
    whatsapp_opt_in: bool = Field(False)
    #: "Get updates from <club> on WhatsApp" (the event's club named, as
    #: Meta's opt-in rule asks), ticked: the club's own WhatsApp number may
    #: send them its promotions (app/api/club_whatsapp/consent.py). Needs
    #: `phone`, as whatsapp_opt_in. False or left out records nothing and
    #: takes nothing back.
    club_updates_whatsapp: bool = Field(False)
    #: The ticket type (docs/TICKETING.md). Left out: the event's only one;
    #: an event selling several answers 422 TICKET_TYPE_REQUIRED.
    ticket_type_id: int | None = Field(None)
    #: A discount code typed for it; checked and applied by the server.
    discount_code: str | None = Field(None, max_length=60)
    #: The client knows registration requests (docs/APPROVALS.md): on an
    #: event whose club confirms each registration, the answer is a request.
    #: Without it such an event answers 409 APPROVAL_REQUIRED (an old app).
    accepts_approval: bool = Field(False)


class EventAttendanceUpdate(CustomBaseModel):
    is_attended: bool


class EventRegistrationDetailResponse(CustomBaseModel):
    id: UUID
    user: UserPublic
    ticket_id: str
    is_paid: bool
    actual_amount: float
    paid_amount: float
    payment_receipt: str | None
    created_at: datetime
    updated_at: datetime
    is_won: bool
    position: int | None
    additional_details: dict | None
    # Picks which ticket and certificate variant this person is issued.
    role: RegistrationRole = Field(RegistrationRole.attendee)
    # The guest in full, as the attendee page shows one: what is on their
    # record and everything that happened to them.
    full_name: str | None = Field(None)
    email: str | None = Field(None)
    phone: str | None = Field(None)
    is_attended: bool = Field(False)
    attended_on: datetime | None = Field(None)
    ticket_revoked_at: datetime | None = Field(None)
    certificate_issued_at: datetime | None = Field(None)
    certificate_revoked_at: datetime | None = Field(None)
    ticket_deliveries: list[DeliveryBrief] = Field(default_factory=list)
    email_bounced_at: datetime | None = Field(None)
    email_bounce_reason: str | None = Field(None)
    admission_no: str | None = Field(None)
    certificate_deliveries: list[DeliveryBrief] = Field(default_factory=list)
    # Whether they asked for WhatsApp, and whether WhatsApp could reach them.
    whatsapp_opt_in: bool = Field(False)
    wa_bounced_at: datetime | None = Field(None)
    wa_bounce_reason: str | None = Field(None)
    # The ticket type it was bought as, and what a discount code took off it.
    ticket_type_id: int | None = Field(None)
    ticket_label: str | None = Field(None)
    discount_amount: float | None = Field(None)


class RegistrationUpdateRequest(CustomBaseModel):
    """A correction by the organiser: name, contact, role, answers."""

    full_name: str | None = Field(None, max_length=100, min_length=1)
    email: EmailStr | None = Field(None)
    phone: str | None = Field(None, max_length=15)
    role: RegistrationRole | None = Field(None)
    additional_details: dict[str, Any] | None = Field(None)


class AddAttendeeRequest(CustomBaseModel):
    """Added by hand at the desk or from a message -- not through the public form."""

    full_name: str = Field(..., max_length=100, min_length=1)
    email: EmailStr = Field(...)
    phone: str | None = Field(None, max_length=15)
    role: RegistrationRole = Field(RegistrationRole.attendee)
    additional_details: dict[str, Any] | None = Field(None)


class ImportLogSummary(CustomBaseModel):
    id: UUID
    task_name: str
    status: str
    created_at: datetime
    imported: int
    failed: int
    # The rows that could not be used, with why -- the permanent record.
    issues: list[str] = Field(default_factory=list)


class EventRegistrationPublicMin(CustomBaseModel):
    id: UUID = Field(...)
    ticket_id: str = Field(...)
    event_id: int = Field(...)
    is_paid: bool = Field(...)
    full_name: str = Field(...)
    email: str = Field(...)
    phone: str | None = Field(None)
    actual_amount: float = Field(...)
    paid_amount: float = Field(...)
    is_attended: bool = Field(...)
    is_won: bool = Field(...)
    position: int | None = Field(None)
    role: RegistrationRole = Field(RegistrationRole.attendee)
    user: UserPublic | None = Field(None)
    volunteer: ListVolunteersResponse | None = Field(None)
    institution: str | None = Field(None)
    name: str | None = Field(None)
    created_at: datetime = Field(...)
    ticket_revoked_at: datetime | None = Field(None)
    ticket_deliveries: list[DeliveryBrief] = Field(default_factory=list)
    email_bounced_at: datetime | None = Field(None)
    email_bounce_reason: str | None = Field(None)
    admission_no: str | None = Field(None)
    # The ticket type it was bought as (docs/TICKETING.md).
    ticket_type_id: int | None = Field(None)
    ticket_type_name: str | None = Field(None)


class SetWinnerRequest(CustomBaseModel):
    """`position` of None clears the placing; 1 is first."""

    position: int | None = Field(None, ge=1)


class RegistrationTimeDistribution(CustomBaseModel):
    morning: int = 0  # 6 AM - 12 PM
    afternoon: int = 0  # 12 PM - 5 PM
    evening: int = 0  # 5 PM - 9 PM
    night: int = 0  # 9 PM - 6 AM


class EventAnalyticsResponse(CustomBaseModel):
    total_registrations: int
    total_revenue: float
    attendance_rate: float
    conversion_rate: float
    
    # Breakdowns
    payment_status: dict[str, int]  # {"paid": 10, "unpaid": 5}
    attendance_status: dict[str, int]  # {"attended": 8, "absent": 7}
    top_institutions: list[dict[str, Any]]  # [{"name": "IIT", "value": 10}, ...]
    total_institutions: int = 0  # distinct institutions, not capped at the top 5
    registration_time: RegistrationTimeDistribution
    attendance_over_time: list[dict[str, Any]]  # [{"time": "10:00 AM", "count": 5}, ...]


class SetRoleRequest(CustomBaseModel):
    """Which role a registration is attending as."""

    role: Optional[RegistrationRole] = Field(
        default=None,
        description="attendee, speaker or staff. Null resets them to attendee.",
    )
