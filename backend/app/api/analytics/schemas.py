from typing import Optional
from uuid import UUID

from pydantic import BaseModel


# --- the shape of the door -------------------------------------------------


class ArrivalBucket(BaseModel):
    starts_at: str
    admits: int
    cumulative: int


class DoorFigures(BaseModel):
    door_id: Optional[UUID]
    name: str
    kind: str
    admits: int
    share_pct: float
    last_admit_at: Optional[str]
    peak_admits: int


class TypeFigures(BaseModel):
    role: str
    label: str
    color: str
    registered: int
    checked_in: int


class DeliveryFigures(BaseModel):
    channel: str
    sent: int
    failed: int
    skipped: int
    pending: int


class ViaFigures(BaseModel):
    via: str
    label: str
    admits: int


class CardFigures(BaseModel):
    printed: int
    failed: int
    holders: int


class Timing(BaseModel):
    first_admit_at: Optional[str]
    last_admit_at: Optional[str]
    peak_starts_at: Optional[str]
    peak_admits: int
    half_house_at: Optional[str]
    half_house_minutes: Optional[int]


class ContactFigures(BaseModel):
    phone: int
    email: int
    bounced: int


class EventFigures(BaseModel):
    total_admitted: int
    arrivals: list[ArrivalBucket]
    by_door: list[DoorFigures]
    by_type: list[TypeFigures]
    deliveries: list[DeliveryFigures]
    via: list[ViaFigures]
    cards: CardFigures
    timing: Timing
    contacts: ContactFigures


# --- the live summary ------------------------------------------------------


class RecentCheckIn(BaseModel):
    id: UUID
    name: str
    door: str
    by: Optional[str]
    via: str
    checked_in_at: str


class SourceFigures(BaseModel):
    source: str
    label: str
    registered: int
    checked_in: int


class MerchFigures(BaseModel):
    item_id: UUID
    name: str
    collected: int
    total: int


class TicketFigures(BaseModel):
    issued: int
    missing: int
    pending: int
    older_design: int
    revoked: int
    design_version: int


class RetentionFigures(BaseModel):
    mode: str
    state: Optional[str]
    run_at: Optional[str]


class EventSummary(BaseModel):
    event_id: int
    status: str
    status_clock: bool
    door_open: bool
    starts_at: str
    ends_at: str
    timezone: str
    capacity: Optional[int]
    registered: int
    checked_in: int
    recent: list[RecentCheckIn]
    by_source: list[SourceFigures]
    merch: list[MerchFigures]
    tickets: TicketFigures
    #: Registration requests waiting for the club (docs/APPROVALS.md).
    requests_pending: int = 0
    retention: RetentionFigures
    analytics: EventFigures
    from_report: bool
    generated_at: str


# --- the dashboard ---------------------------------------------------------


class EventCounts(BaseModel):
    total: int
    draft: int
    published: int
    live: int
    closed: int
    archived: int


class DashboardStats(BaseModel):
    events: EventCounts
    on_file: int
    retention_due: int
    next_retention_at: Optional[str]
    doors_open: int


class AttentionItem(BaseModel):
    kind: str
    tone: str
    title: str
    body: str
    event_id: Optional[int]


class LiveEvent(BaseModel):
    event_id: int
    name: str
    club: Optional[str]
    registered: int
    checked_in: int
    gates: list[str]
    volunteers: int
    door_open: bool
    starts_at: str


class ClubDashboard(BaseModel):
    stats: DashboardStats
    attention: list[AttentionItem]
    live: list[LiveEvent]
    generated_at: str


class ClubLifetime(BaseModel):
    events_run: int
    registered: int
    guests_admitted: int
    average_turnout_pct: Optional[int]
    reports_delivered: int


class PlatformDoors(ClubDashboard):
    clubs_live: int
