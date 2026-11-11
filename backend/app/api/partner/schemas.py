"""The Integrations API's request bodies and answers, as documented in
/partner/v1/openapi.json. Bodies refuse fields they do not know (a typo is
a VALIDATION error, never silently ignored). Times are ISO 8601 with their
offset; money is whole paise with `currency: "INR"`; ids are strings.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, EmailStr, Field, create_model


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


EXTERNAL_ID = Field(None, max_length=128, description="Your own id for this object: unique per credential and resource.")

# ─── Shared pieces ───────────────────────────────────────────────────────


class Money(_Body):
    amount: int = Field(..., ge=0, le=10_000_000_00, description="Whole paise: 49900 is ₹499.")
    currency: Literal["INR"] = "INR"


class Links(_Body):
    website: Optional[str] = Field(None, max_length=500)
    instagram: Optional[str] = Field(None, max_length=500)
    linkedin: Optional[str] = Field(None, max_length=500)
    youtube: Optional[str] = Field(None, max_length=500)


class Venue(_Body):
    name: str = Field(..., min_length=1, max_length=200)
    address: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=100)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)


class Online(_Body):
    url: str = Field(..., min_length=8, max_length=500)


class RegistrationSettings(_Body):
    opens_at: Optional[str] = Field(None, description="When registration opens; default now.")
    closes_at: Optional[str] = Field(None, description="When it closes; never after the event ends.")
    requires_approval: Optional[bool] = Field(None, description="The club confirms each registration.")


# ─── Club ────────────────────────────────────────────────────────────────


class ClubPatch(_Body):
    name: Optional[str] = Field(None, min_length=3, max_length=100)
    tagline: Optional[str] = Field(None, max_length=120)
    about: Optional[str] = Field(None, max_length=5000)
    links: Optional[Links] = None
    logo_url: Optional[str] = Field(None, max_length=1000, description="An https image we fetch and keep.")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"tagline": "Robots, every Friday", "links": {"website": "https://robotics.example.edu"}}]})


class ClubOwner(_Body):
    email: EmailStr
    name: Optional[str] = Field(None, max_length=100)


class ClubCreate(_Body):
    name: str = Field(..., min_length=3, max_length=100)
    owner: ClubOwner
    tagline: Optional[str] = Field(None, max_length=120)
    about: Optional[str] = Field(None, max_length=5000)
    links: Optional[Links] = None
    logo_url: Optional[str] = Field(None, max_length=1000)
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = Field(None, max_length=20)
    location: Optional[str] = Field(None, max_length=200)
    external_id: Optional[str] = EXTERNAL_ID

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "name": "Robotics Club, NIT Calicut", "owner": {"email": "secretary@robotics.example.edu", "name": "Asha K"},
        "tagline": "Robots, every Friday", "external_id": "club-4411",
    }]})


# ─── Events ──────────────────────────────────────────────────────────────


class EventWrite(_Body):
    name: Optional[str] = Field(None, min_length=3, max_length=100)
    description: Optional[str] = Field(None, max_length=20000)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    timezone: Optional[Literal["Asia/Kolkata"]] = Field(None, description="Every time carries its own offset; the event's zone is Asia/Kolkata.")
    venue: Optional[Venue] = None
    online: Optional[Online] = None
    poster_url: Optional[str] = Field(None, max_length=1000, description="An https image we fetch and keep (4:5, 1080x1350 looks best).")
    capacity: Optional[int] = Field(None, ge=1, le=1_000_000)
    registration: Optional[RegistrationSettings] = None
    price: Optional[Money] = Field(None, description="null or omitted: free.")
    external_payments_allowed: Optional[bool] = Field(
        None, json_schema_extra={"readOnly": True},
        description="Read-only here: only the club turns this on, in the dashboard. Sent, it is 422 FIELD_READ_ONLY.",
    )
    category: Optional[str] = Field(None, max_length=50, description="An event type's name; default the first.")
    external_id: Optional[str] = EXTERNAL_ID


class EventCreate(EventWrite):
    name: str = Field(..., min_length=3, max_length=100)
    starts_at: str
    ends_at: str

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "name": "Bot Build Night", "description": "Bring a laptop.", "starts_at": "2026-12-05T18:00:00+05:30",
        "ends_at": "2026-12-05T21:00:00+05:30", "venue": {"name": "Lab 3", "city": "Kozhikode"}, "capacity": 60,
        "registration": {"closes_at": "2026-12-05T12:00:00+05:30"}, "external_id": "evt-2026-031",
    }]})


class EventPatch(EventWrite):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"capacity": 80, "description": "Now in the big lab."}]})


class EventCancel(_Body):
    reason: str = Field(..., min_length=1, max_length=500)


# ─── Registrations ───────────────────────────────────────────────────────


class Person(_Body):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=15)


class ExternalPayment(_Body):
    status: Literal["paid_externally"]
    amount: int = Field(..., ge=0, description="Whole paise.")
    currency: Literal["INR"] = "INR"
    reference: Optional[str] = Field(None, max_length=128, description="Your receipt or transaction id.")


class RegistrationCreate(_Body):
    person: Person
    answers: Optional[dict[str, Any]] = Field(None, description="The event's questions by key.")
    ticket_type_id: Optional[Union[int, str]] = None
    payment: Optional[ExternalPayment] = None
    external_id: Optional[str] = EXTERNAL_ID
    send_confirmation: bool = Field(True, description="Email the ticket (live mode).")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "person": {"name": "Ravi M", "email": "ravi@example.com", "phone": "9988776655"},
        "answers": {"college": "NIT Calicut"}, "external_id": "reg-88121",
    }]})


class RegistrationPatch(_Body):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    answers: Optional[dict[str, Any]] = None
    external_id: Optional[str] = EXTERNAL_ID


class CheckinCreate(_Body):
    ticket_code: Optional[str] = Field(None, max_length=60)
    registration_id: Optional[str] = Field(None, max_length=64)
    external_id: Optional[str] = Field(None, max_length=128, description="Your external_id of the registration.")
    scanned_at: Optional[str] = None
    gate: Optional[str] = Field(None, max_length=40)
    device: Optional[str] = Field(None, max_length=100)

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"ticket_code": "MOA-7KQ2-9XWD", "gate": "Main gate"}]})


class CertificateIssue(_Body):
    registration_ids: Optional[list[str]] = Field(None, max_length=1000)
    all_checked_in: Optional[bool] = None
    template_id: Optional[int] = None
    send: bool = Field(True, description="Email each certificate (live mode).")

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"all_checked_in": True}]})


# ─── Answers (documentation) ─────────────────────────────────────────────


class ErrorDetail(_Out):
    code: str
    message: str
    details: Optional[dict] = None
    request_id: str


class ErrorBody(_Out):
    error: ErrorDetail

    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{"error": {
        "code": "EVENT_FULL", "message": "This event is full.", "request_id": "req_6f1c0a...",
    }}]})


class Ref(_Out):
    id: str
    name: Optional[str] = None
    slug: Optional[str] = None


class RateLimitOut(_Out):
    read_per_min: int
    write_per_min: int


class MeOut(_Out):
    type: Literal["api_key", "partner_app"]
    club: Optional[Ref] = None
    partner: Optional[Ref] = None
    key: Optional[dict] = Field(None, description="A club key's id, name and prefix.")
    client_id: str = Field(..., description="What webhooks name as a change's origin.")
    scopes: list[str]
    livemode: bool
    rate_limit: RateLimitOut

    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
        "type": "api_key", "club": {"id": "42", "name": "Robotics Club", "slug": "robotics-club"}, "partner": None,
        "key": {"id": "3f0c...", "name": "Our website", "prefix": "moa_live_Ab12Cd34"}, "client_id": "key_9a8b7c6d5e4f3a2b1c0d9e8f",
        "scopes": ["events:read", "events:write"], "livemode": True, "rate_limit": {"read_per_min": 600, "write_per_min": 120},
    }]})


class ClubOut(_Out):
    id: str
    object: Literal["club"] = "club"
    name: str
    slug: str
    status: Literal["active", "pending_approval"]
    tagline: Optional[str] = None
    about: Optional[str] = None
    links: dict
    logo_url: Optional[str] = None
    public_url: Optional[str] = None
    external_id: Optional[str] = None
    livemode: bool = True
    updated_at: Optional[str] = None


class TicketTypeOut(_Out):
    id: str
    name: str
    price: dict
    capacity: Optional[int] = None
    sales_start: Optional[str] = None
    sales_end: Optional[str] = None
    hidden: bool = False


class EventOut(_Out):
    id: str
    object: Literal["event"] = "event"
    club_id: str
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    status: Literal["draft", "published", "cancelled", "ended"]
    starts_at: str
    ends_at: str
    timezone: str = "Asia/Kolkata"
    venue: Optional[dict] = None
    online: Optional[dict] = None
    poster_url: Optional[str] = None
    capacity: Optional[int] = None
    registration: dict
    price: Optional[dict] = None
    ticket_types: list[TicketTypeOut] = []
    external_payments_allowed: bool = False
    registrations_count: int = 0
    external_id: Optional[str] = None
    public_url: Optional[str] = None
    livemode: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
        "id": "1207", "object": "event", "club_id": "42", "name": "Bot Build Night", "slug": "bot-build-night-x1",
        "description": "Bring a laptop.", "status": "published", "starts_at": "2026-12-05T18:00:00+05:30",
        "ends_at": "2026-12-05T21:00:00+05:30", "timezone": "Asia/Kolkata",
        "venue": {"name": "Lab 3, Kozhikode", "address": None, "city": None, "lat": None, "lng": None}, "online": None,
        "poster_url": None, "capacity": 60,
        "registration": {"opens_at": "2026-11-12T10:00:00+05:30", "closes_at": "2026-12-05T12:00:00+05:30", "requires_approval": False},
        "price": None, "ticket_types": [{"id": "88", "name": "General", "price": {"amount": 0, "currency": "INR"}, "capacity": 60, "hidden": False}],
        "external_payments_allowed": False, "registrations_count": 12, "external_id": "evt-2026-031",
        "public_url": "https://myotherapp.in/events/bot-build-night-x1", "livemode": True,
        "created_at": "2026-11-12T10:00:00+05:30", "updated_at": "2026-11-12T10:05:00+05:30",
    }]})


class TicketOut(_Out):
    code: str
    qr_url: Optional[str] = None
    status: Literal["valid", "awaiting_payment", "revoked", "cancelled"]


class RegistrationOut(_Out):
    id: str
    object: Literal["registration"] = "registration"
    event_id: str
    status: Literal["confirmed", "awaiting_payment", "pending_approval", "declined", "withdrawn", "cancelled"]
    person: Optional[dict] = Field(None, description="Only with attendees:read.")
    answers: Optional[dict] = Field(None, description="Only with attendees:read.")
    ticket: Optional[TicketOut] = None
    ticket_type: Optional[dict] = None
    approval: Optional[dict] = None
    payment: dict
    checked_in_at: Optional[str] = None
    request_id: Optional[str] = None
    external_id: Optional[str] = None
    livemode: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
        "id": "6b0f4a8e-3a51-4f0e-9a51-0b1c2d3e4f50", "object": "registration", "event_id": "1207", "status": "confirmed",
        "person": {"name": "Ravi M", "email": "ravi@example.com", "phone": "9988776655"}, "answers": {"college": "NIT Calicut"},
        "ticket": {"code": "MOA-7KQ2-9XWD", "qr_url": "https://api.myotherapp.in/partner/v1/tickets/MOA-7KQ2-9XWD/qr.png?sig=...", "status": "valid"},
        "ticket_type": {"id": "88", "name": "General"}, "approval": None,
        "payment": {"status": "not_required", "amount": 0, "currency": "INR"}, "checked_in_at": None,
        "external_id": "reg-88121", "livemode": True, "created_at": "2026-11-20T09:12:00+05:30", "updated_at": "2026-11-20T09:12:00+05:30",
    }]})


class CheckinOut(_Out):
    id: str
    object: Literal["checkin"] = "checkin"
    registration_id: str
    event_id: str
    checked_in_at: str
    scanned_at: Optional[str] = Field(None, description="The device's own time, when it sent one.")
    gate: Optional[str] = None
    via: Optional[str] = None
    duplicate: bool = False
    deleted: Optional[bool] = Field(None, description="On the answer to DELETE: true.")
    livemode: bool = True
    updated_at: Optional[str] = None


class CertificateOut(_Out):
    number: str
    object: Literal["certificate"] = "certificate"
    registration_id: str
    event_id: str
    status: Literal["issued", "revoked"]
    issued_at: Optional[str] = None
    revoked_at: Optional[str] = None
    name: Optional[str] = Field(None, description="Only with attendees:read.")
    verify_url: Optional[str] = None
    pdf_url: Optional[str] = None
    livemode: bool = True


class CertificateJobOut(_Out):
    id: str
    object: Literal["certificate_job"] = "certificate_job"
    event_id: str
    status: Literal["queued", "running", "completed", "failed"]
    requested: dict
    result: Optional[dict] = None
    error: Optional[str] = None
    deliveries: Optional[list] = Field(None, description="Live, with send: how the emails are going, by channel.")
    livemode: bool = True
    created_at: Optional[str] = None
    finished_at: Optional[str] = None


class CertificateVerification(_Out):
    number: str
    object: Literal["certificate_verification"] = "certificate_verification"
    valid: bool
    reason: str
    registration_id: Optional[str] = None
    event_id: Optional[str] = None
    event_name: Optional[str] = None
    name: Optional[str] = Field(None, description="Only with attendees:read.")
    issued_at: Optional[str] = None
    revoked_at: Optional[str] = None
    verify_url: Optional[str] = None
    pdf_url: Optional[str] = None
    livemode: bool = True


def list_of(model: type[BaseModel]) -> type[BaseModel]:
    """A page of `model`: {data: [...], next_cursor}."""
    return create_model(
        f"{model.__name__.removesuffix('Out')}List", __base__=_Out,
        data=(list[model], ...), next_cursor=(Optional[str], Field(None, description="Pass as `cursor` for the next page; null on the last.")),
    )


EventList = list_of(EventOut)
RegistrationList = list_of(RegistrationOut)
CheckinList = list_of(CheckinOut)
CertificateList = list_of(CertificateOut)
ClubList = list_of(ClubOut)
