from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.deliveries.schemas import ChannelSummary, DeliveryBrief

CertificateAudience = Literal["checked_in", "everyone", "roles"]

AUDIENCE_LABEL = {
    "checked_in": ("Everyone who checked in", "The usual rule: a certificate says they were there."),
    "everyone": ("Everyone registered", "Including no-shows -- for an online event, or a course."),
    "roles": ("Certain roles", "Speakers, staff, or attendees only."),
}


class IssueRequest(BaseModel):
    audience: CertificateAudience = "checked_in"
    roles: list[str] = Field(default_factory=list, max_length=10)


class IssuePreview(BaseModel):
    eligible: int
    already_issued: int
    to_issue: int
    # Issued with a design older than the current one; re-issuing refreshes them.
    outdated: int


class IssueResponse(BaseModel):
    issued: int
    refreshed: int
    already_issued: int


class CertificateSummary(BaseModel):
    issued: int
    revoked: int
    attended: int
    registrations: int
    outdated: int
    design_version: int
    channels: list[ChannelSummary]


class CertificateRow(BaseModel):
    registration_id: UUID
    ticket_id: str
    number: str
    name: str
    email: Optional[str]
    role: str
    status: Literal["issued", "revoked"]
    issued_at: Optional[datetime]
    revoked_at: Optional[datetime]
    design_version: int
    deliveries: list[DeliveryBrief] = Field(default_factory=list)


class CertificatePage(BaseModel):
    items: list[CertificateRow]
    total: int
    page: int
    page_size: int


class CertificateState(BaseModel):
    registration_id: UUID
    number: str
    revoked_at: Optional[datetime]


class Verification(BaseModel):
    """Public. Says only what a verifier needs: genuine or not, valid or
    revoked, whose, for what, from whom, when. Never an address, a phone,
    a ticket id or any id of a row (tests/test_certificate_verify.py holds
    the keys to exactly these). Every answer has every key."""

    valid: bool
    reason: Literal["ok", "revoked", "not_found"]
    number: Optional[str] = None
    attendee_name: Optional[str] = None
    event_name: Optional[str] = None
    org_name: Optional[str] = None
    # The issuing club's page on the site, /clubs/{org_slug}: only while the
    # club is public.
    org_slug: Optional[str] = None
    issued_at: Optional[datetime] = None


class FileCheck(BaseModel):
    """Public. The SHA-256 of each copy of a valid certificate handed out,
    for a browser to compare a file with without sending it anywhere.
    Not available: nothing to compare with (or no such certificate)."""

    available: bool
    sha256: list[str] = Field(default_factory=list)
