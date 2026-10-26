"""The shapes of registration requests' routes (docs/APPROVALS.md)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import Field, model_validator

from app.api.auth.schemas import Token
from app.core.response.base_model import CustomBaseModel


class ApprovalBrief(CustomBaseModel):
    id: UUID
    status: str
    submitted_at: datetime
    #: The status link's secret. Null when the form was sent again by someone
    #: who could not show the request is theirs: the link goes to the
    #: request's own address by email instead.
    token: Optional[str] = None
    status_url: Optional[str] = None


class RegistrationApprovalResponse(CustomBaseModel):
    """POST /events/registration/{event}/register on an event that confirms
    each registration: a request, not a registration."""

    approval: ApprovalBrief
    event_registration_id: None = None
    event_id: int
    event_name: str
    #: A session for the guest the request made (the public form), as a registration hands one out.
    auth_token: Optional[Token] = None


class RequestEventClub(CustomBaseModel):
    name: str
    slug: str


class RequestEvent(CustomBaseModel):
    id: int
    slug: Optional[str] = None
    name: Optional[str] = None
    event_datetime: Optional[datetime] = None
    poster: Optional[str] = None
    club: Optional[RequestEventClub] = None


class RequestPay(CustomBaseModel):
    amount: float
    pay_by: datetime
    claim_token: str
    claim_url: Optional[str] = None


class RequestPublic(CustomBaseModel):
    id: UUID
    status: str
    event: RequestEvent
    ticket_type: Optional[dict[str, Any]] = None
    full_name: str
    submitted_at: datetime
    decided_at: Optional[datetime] = None
    decline_note: Optional[str] = None
    ticket_id: Optional[str] = None
    pay: Optional[RequestPay] = None
    token: Optional[str] = None
    status_url: Optional[str] = None


class RequestsMine(CustomBaseModel):
    items: list[RequestPublic]


class RequestClubItem(CustomBaseModel):
    id: UUID
    status: str
    full_name: str
    email: str
    phone: Optional[str] = None
    ticket_type: Optional[dict[str, Any]] = None
    answers: dict[str, Any] = Field(default_factory=dict)
    submitted_at: datetime
    decided_at: Optional[datetime] = None
    decided_by: Optional[dict[str, Any]] = None
    decline_note: Optional[str] = None
    registration_id: Optional[UUID] = None
    pay_by: Optional[datetime] = None
    #: A confirmed paid ticket's price, as confirmed (its code taken off).
    amount: Optional[float] = None
    discount_code: Optional[str] = None
    whatsapp_opt_in: bool = False


class RequestCounts(CustomBaseModel):
    pending: int = 0
    approved: int = 0
    declined: int = 0
    withdrawn: int = 0
    lapsed: int = 0
    closed: int = 0


class RequestsPage(CustomBaseModel):
    items: list[RequestClubItem]
    next: Optional[str] = None
    counts: RequestCounts
    #: Null: no cap.
    places_left: Optional[int] = None


class ApproveIn(CustomBaseModel):
    """`ids` (at most 500), or `all_pending` with a `limit` (oldest first)."""

    ids: Optional[list[UUID]] = Field(None, max_length=500)
    all_pending: bool = False
    limit: int = Field(500, ge=1, le=500)

    @model_validator(mode="after")
    def _one_of(self):
        if not self.all_pending and not self.ids:
            raise ValueError("Send the requests' ids, or all_pending.")
        return self


class ApprovedRow(CustomBaseModel):
    id: UUID
    registration_id: Optional[UUID] = None
    pay_by: Optional[datetime] = None


class SkippedRow(CustomBaseModel):
    id: UUID
    reason: str


class ApproveOut(CustomBaseModel):
    approved: list[ApprovedRow]
    skipped: list[SkippedRow]
    places_left: Optional[int] = None


class DeclineIn(CustomBaseModel):
    """`ids` (at most 500), or `all_pending` with a `limit` (oldest first)."""

    ids: Optional[list[UUID]] = Field(None, max_length=500)
    all_pending: bool = False
    limit: int = Field(500, ge=1, le=500)
    notify: bool = True
    note: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def _one_of(self):
        if not self.all_pending and not self.ids:
            raise ValueError("Send the requests' ids, or all_pending.")
        return self


class DeclineOut(CustomBaseModel):
    declined: list[UUID]
    skipped: list[SkippedRow]


class ReopenIn(CustomBaseModel):
    ids: list[UUID] = Field(..., min_length=1, max_length=500)


class ReopenOut(CustomBaseModel):
    reopened: list[UUID]
    skipped: list[SkippedRow]
