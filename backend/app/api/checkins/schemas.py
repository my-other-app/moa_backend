"""The door's words: what a scan answers, what a walk-up needs, what the
log and the team page show."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

DoorKindName = Literal["door", "desk", "kiosk"]
DoorModeName = Literal["open", "auto", "closed"]
ViaName = Literal["gate", "kiosk", "walkup", "offline_sync", "desk", "phone", "app"]
ScanOutcome = Literal["CHECKED_IN", "ALREADY_CHECKED_IN", "WRONG_EVENT", "REVOKED", "UNPAID", "INVALID", "EVENT_NOT_OPEN"]

DOOR_KIND_META = {
    "door": {"label": "Door", "note": "Volunteers scan here. Walk-ups too, if switched on."},
    "desk": {"label": "Desk", "note": "Spot registrations are admitted here — no QR to scan."},
    "kiosk": {"label": "Kiosk station", "note": "Guests check themselves in. A paired kiosk stands here."},
}
VIA_LABEL = {
    "gate": "Scanned at a door",
    "kiosk": "Self check-in kiosk",
    "walkup": "Walk-up at the door",
    "offline_sync": "Scanned offline",
    "desk": "By hand at the desk",
    "phone": "From their phone",
    "app": "Volunteer app",
}


# --- doors ----------------------------------------------------------------------


class DoorLink(BaseModel):
    prefix: str
    can_walkup: bool
    expires_at: Optional[datetime]
    expired: bool


class DoorOut(BaseModel):
    id: UUID
    event_id: int
    name: str
    kind: DoorKindName
    sort_order: int
    active: bool
    volunteers: list[dict] = Field(default_factory=list)
    check_ins: int = 0
    link: Optional[DoorLink] = None
    created_at: Optional[datetime] = None


class DoorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    kind: DoorKindName = "door"


class DoorUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=40)
    kind: Optional[DoorKindName] = None
    active: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0)
    link_can_walkup: Optional[bool] = None


class LinkRequest(BaseModel):
    can_walkup: bool = False
    days: int = Field(default=30, ge=1, le=365)


class LinkOut(BaseModel):
    url: str
    key: str
    door_id: UUID
    expires_at: Optional[datetime]
    can_walkup: bool


# --- the team ----------------------------------------------------------------------


class TeamVolunteer(BaseModel):
    id: int
    email: str
    name: Optional[str]
    user_id: Optional[int]
    is_approved: bool
    # Club-wide volunteers help at every event of the club.
    scope: Literal["event", "club"]
    can_check_in: bool
    can_walkup: bool
    can_merch: bool
    door_id: Optional[UUID]
    check_ins: int = 0


class TeamOut(BaseModel):
    owner: dict
    volunteers: list[TeamVolunteer]
    doors: list[DoorOut]
    settings: "GateSettings"


class VolunteerUpdate(BaseModel):
    can_check_in: Optional[bool] = None
    can_walkup: Optional[bool] = None
    can_merch: Optional[bool] = None
    door_id: Optional[UUID] = None
    clear_door: bool = False


class GateSettings(BaseModel):
    door_mode: DoorModeName = "open"
    close_after_minutes: int = Field(default=240, ge=0, le=60 * 24 * 7)
    spot_registration_checks_in: bool = True
    capture_contact_at_gate: bool = False
    require_contact_at_gate: bool = False


class GateSettingsUpdate(BaseModel):
    door_mode: Optional[DoorModeName] = None
    close_after_minutes: Optional[int] = Field(default=None, ge=0, le=60 * 24 * 7)
    spot_registration_checks_in: Optional[bool] = None
    capture_contact_at_gate: Optional[bool] = None
    require_contact_at_gate: Optional[bool] = None


# --- the gate ----------------------------------------------------------------------


class AttendeeSummary(BaseModel):
    id: UUID
    name: str
    ticket_id: str
    role: str
    email: Optional[str] = None
    phone: Optional[str] = None
    has_contact: bool = False
    is_paid: bool = True
    checked_in_at: Optional[datetime] = None


class ScanBody(BaseModel):
    code: str = Field(min_length=4, max_length=128)
    door: Optional[str] = Field(default=None, max_length=40)
    scanned_at: Optional[datetime] = None
    # From the device, so a retried scan admits once.
    client_ref: str = Field(min_length=8, max_length=64)


class ScanResult(BaseModel):
    outcome: ScanOutcome
    attendee: Optional[AttendeeSummary]
    checked_in_at: Optional[datetime]
    checked_in_by: Optional[dict]
    other_event: Optional[dict]
    revoked_at: Optional[datetime]
    capture: dict
    replayed: bool = False


class ScanBatchBody(BaseModel):
    scans: list[ScanBody] = Field(min_length=1, max_length=200)


class ScanBatchResult(BaseModel):
    results: list[dict]


class ByHandBody(BaseModel):
    door: Optional[str] = Field(default=None, max_length=40)


class WalkupBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=15)
    role: Literal["attendee", "speaker", "staff"] = "attendee"
    custom_fields: Optional[dict] = None
    # Somebody of this name is on the list and no contact tells them apart:
    # the volunteer has checked, and this is a different person.
    confirm_new: bool = False
    door: Optional[str] = Field(default=None, max_length=40)
    client_ref: str = Field(min_length=8, max_length=64)


class WalkupResult(BaseModel):
    outcome: Literal["REGISTERED", "DUPLICATE", "EVENT_NOT_OPEN"]
    attendee: Optional[AttendeeSummary]
    duplicate_of: Optional[dict]
    duplicate_by: Optional[Literal["CONTACT", "NAME"]]
    checked_in: bool = False
    ticket_sent: bool = False


class CaptureBody(BaseModel):
    phone: str = Field(min_length=6, max_length=15)


class CaptureResult(BaseModel):
    phone: str
    shared_with: list[str] = Field(default_factory=list)


class GateContext(BaseModel):
    event: dict
    permissions: dict
    counters: dict
    walkup_roles: list[str]
    doors: list[dict]
    expected_door_id: Optional[UUID]
    guest_fields: list[dict]
    me: dict


class GateSearchRow(AttendeeSummary):
    pass


# --- the log ----------------------------------------------------------------------


class CheckInRow(BaseModel):
    id: UUID
    registration_id: UUID
    name: str
    email: Optional[str]
    ticket_id: str
    role: str
    door: dict
    via: ViaName
    checked_in_at: datetime
    recorded_at: datetime
    by: dict


class CheckInLog(BaseModel):
    items: list[CheckInRow]
    total: int
    page: int
    page_size: int
    facets: dict
