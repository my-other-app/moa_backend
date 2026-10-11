from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DeliveryBrief(BaseModel):
    """One channel's state for one guest, as the attendee list shows it."""

    channel: str
    status: str
    error: Optional[str] = None
    outcome: Optional[str] = None
    reason: Optional[str] = None
    attempts: int = 0
    sent_at: Optional[datetime] = None


class SendTicketsRequest(BaseModel):
    # unsent: everyone not yet reached; failed: retry what failed or was
    # skipped; all: everyone again, delivered or not.
    only: Literal["unsent", "failed", "all"] = "unsent"
    registration_ids: Optional[list[UUID]] = Field(default=None, max_length=5000)


class SendResponse(BaseModel):
    queued: int
    already_sent: int
    # Guests whose ticket is revoked are not sent anything.
    revoked: int
    channels: list[str]


class RevokeRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)


class TicketState(BaseModel):
    registration_id: UUID
    ticket_id: str
    revoked_at: Optional[datetime]


class ChannelSummary(BaseModel):
    channel: str
    sent: int
    failed: int
    skipped: int
    pending: int
    # Registrations with no row on this channel at all.
    unsent: int


class DeliverySummary(BaseModel):
    kind: str
    registrations: int
    revoked: int
    channels: list[ChannelSummary]
    # Which channels have a provider behind them; one without sends nothing and says so.
    providers: list[dict] = []


class DeliveryLogRow(BaseModel):
    id: UUID
    registration_id: UUID
    ticket_id: str
    name: str
    address: Optional[str]
    kind: str
    # A message's subject; nothing for a ticket or certificate.
    what: Optional[str] = None
    channel: str
    status: str
    outcome: Optional[str]
    reason: Optional[str]
    attempts: int
    sent_at: Optional[datetime]
    outcome_at: Optional[datetime]


class DeliveryTally(BaseModel):
    sent: int
    delivered: int
    awaiting: int
    bounced: int
    complained: int
    failed: int
    skipped: int
    pending: int


class DeliveryLog(BaseModel):
    items: list[DeliveryLogRow]
    total: int
    page: int
    page_size: int
    tally: DeliveryTally
