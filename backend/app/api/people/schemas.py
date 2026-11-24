"""The guest profile's answer (contract §1)."""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel

Tone = Literal["default", "success", "warning", "danger", "info"]


class Account(BaseModel):
    kind: Literal["app", "guest", "none"]
    user_id: Optional[int] = None


class Person(BaseModel):
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    institution: Optional[str] = None
    role: str = "attendee"
    photo_url: Optional[str] = None
    account: Account


class Status(BaseModel):
    stage: Literal["under_review", "waitlisted", "confirmed", "declined", "cancelled", "removed"]
    checked_in: bool
    card_printed: bool
    certificate: Literal["none", "issued", "revoked"]
    paid: Optional[bool] = None
    ticket_revoked: bool
    email_bounced: bool = False
    # Signed in to the MyOtherApp app on a phone: an app notification can reach them.
    on_app: bool = False


class Answer(BaseModel):
    key: str
    label: str
    value: Any = None
    missing: bool


class Ticket(BaseModel):
    code: str
    type_name: Optional[str] = None
    admission_no: Optional[str] = None
    pdf_url: Optional[str] = None
    issued_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class Payment(BaseModel):
    amount: float
    currency: str = "INR"
    method: Optional[str] = None
    status: str
    paid_at: Optional[datetime] = None
    receipt: Optional[str] = None


class CheckIn(BaseModel):
    at: datetime
    door: Optional[str] = None
    method: str
    #: The machine word behind `method`: gate, offline_sync, desk, kiosk, walkup, phone, app, api, marked.
    via: str
    by: Optional[str] = None


class Print(BaseModel):
    # The print-station request (Retry / Cancel act on it); null for a card printed another way.
    id: Optional[str] = None
    at: datetime
    status: Literal["printed", "queued", "printing", "failed", "cancelled"]
    source: str
    door: Optional[str] = None
    printer: Optional[str] = None
    by: Optional[str] = None
    error: Optional[str] = None


class MerchLine(BaseModel):
    item: str
    variant: Optional[str] = None
    at: datetime
    by: Optional[str] = None


class Delivery(BaseModel):
    what: str
    channel: str
    status: str
    outcome: Optional[str] = None
    at: Optional[datetime] = None
    reason: Optional[str] = None


class TimelineRow(BaseModel):
    at: datetime
    kind: str
    title: str
    detail: Optional[str] = None
    by: Optional[str] = None
    tone: Tone = "default"


class Actions(BaseModel):
    can_print: bool
    can_send_to_station: bool
    printers_online: int
    can_resend_ticket: bool
    can_check_in: bool
    can_undo_check_in: bool
    can_edit: bool
    can_confirm: bool
    can_decline: bool
    can_ask_missing: bool
    can_remove: bool


class PersonProfile(BaseModel):
    kind: Literal["registration", "request"]
    registration_id: Optional[str] = None
    request_id: Optional[str] = None
    person: Person
    status: Status
    answers: list[Answer]
    ticket: Optional[Ticket] = None
    payment: Optional[Payment] = None
    check_in: Optional[CheckIn] = None
    prints: list[Print]
    merch: list[MerchLine]
    deliveries: list[Delivery]
    timeline: list[TimelineRow]
    timeline_truncated: bool
    actions: Actions
