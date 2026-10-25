from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ─── Results ───────────────────────────────────────────────────────────


class TeamMember(BaseModel):
    registration_id: UUID
    full_name: str


class TeamResult(BaseModel):
    id: int
    name: str
    position: Optional[int] = None
    members: list[TeamMember] = []


class TrackResult(BaseModel):
    id: Optional[int] = None
    name: str
    teams: list[TeamResult] = []


class Winner(BaseModel):
    registration_id: UUID
    ticket_id: str
    full_name: str
    email: Optional[str] = None
    position: Optional[int] = None
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    track_id: Optional[int] = None
    track_name: Optional[str] = None


class Results(BaseModel):
    event_id: int
    has_competition: bool
    tracks: list[TrackResult]
    winners: list[Winner]


class PlaceTeam(BaseModel):
    #: 1, 2 or 3; null takes the team's place away.
    position: Optional[int] = Field(None, ge=1, le=3)


# ─── Refunds ───────────────────────────────────────────────────────────


class RefundOut(BaseModel):
    id: UUID
    amount: float
    reason: str
    status: Literal["pending", "succeeded", "failed"]
    source: Literal["club", "cancellation"]
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    by: Optional[str] = None
    attempts: int = 0


class PaymentOut(BaseModel):
    id: UUID
    registration_id: Optional[UUID] = None
    ticket_id: Optional[str] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    amount: float
    refunded: float
    refundable: float
    method: Optional[str] = None
    status: str
    paid_at: Optional[datetime] = None
    gateway: Literal["platform", "club"]
    refunds: list[RefundOut] = []


class PaymentTotals(BaseModel):
    payments: int
    captured: float
    refunded: float


class EventPayments(BaseModel):
    items: list[PaymentOut]
    totals: PaymentTotals


class RefundRequest(BaseModel):
    #: Rupees. Left out: everything still left on the payment.
    amount: Optional[Decimal] = Field(None, gt=0, max_digits=10, decimal_places=2)
    reason: str = Field(..., min_length=3, max_length=300)
    #: The dialog's own id: sending it again answers with the first refund.
    client_ref: Optional[UUID] = None


class RefundResult(BaseModel):
    refund: RefundOut
    payment: Optional[PaymentOut] = None


# ─── Cancelling ────────────────────────────────────────────────────────


class CancelRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    #: What the confirm step showed: a mismatch is 409 CANCEL_PREVIEW_STALE.
    expected_registrations: Optional[int] = Field(None, ge=0)
    expected_refund_total: Optional[Decimal] = Field(None, ge=0, max_digits=12, decimal_places=2)


class RefundCounts(BaseModel):
    total: int
    succeeded: int
    pending: int
    failed: int
    refunded_amount: float


class RefundFailure(BaseModel):
    id: UUID
    registration_id: Optional[UUID] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    amount: float
    error: Optional[str] = None
    attempts: int
    gateway: Literal["platform", "club"]


class CancelNotice(BaseModel):
    email_message_id: Optional[UUID] = None
    push_sent: Optional[int] = None
    whatsapp_sent: int = 0
    whatsapp_skipped: int = 0


class CancelState(BaseModel):
    status: str
    cancelled: bool
    reason: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    can_cancel: bool
    blocked_reason: Optional[str] = None
    registrations: int
    paid_online: int
    refund_total: float
    paid_at_door: int
    paid_at_door_total: float
    refunds: RefundCounts
    failures: list[RefundFailure]
    notice: CancelNotice


# ─── A forgotten password ──────────────────────────────────────────────


class ForgotRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=254)


class ForgotResponse(BaseModel):
    sent: bool = True


class ResetRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=254)
    code: str = Field(..., min_length=1, max_length=20)
    new_password: str = Field(..., min_length=1, max_length=128)
    totp_code: Optional[str] = Field(None, max_length=64)


class ResetResponse(BaseModel):
    reset: bool = True
