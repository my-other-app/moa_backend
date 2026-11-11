"""What the leads' routes take (feature-register/support-leads-contract.md)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

LeadType = Literal["club", "sponsor", "venue", "vendor", "other"]
Stage = Literal["new", "contacted", "meeting", "proposal", "negotiation", "won", "lost"]
Source = Literal["for_clubs", "referral", "event", "outreach", "inbound_email", "import", "signup", "speaker", "other"]
FollowUpKind = Literal["call", "meeting", "email", "whatsapp", "visit", "other"]
Channel = Literal["email", "phone", "whatsapp", "meeting", "other"]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ContactIn(_In):
    name: Optional[str] = Field(None, max_length=120)
    role: Optional[str] = Field(None, max_length=120)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    whatsapp: Optional[str] = Field(None, max_length=30)
    preferred_channel: Optional[Channel] = None
    is_primary: Optional[bool] = None


class FollowUpBody(_In):
    kind: FollowUpKind = "call"
    title: str = Field(..., min_length=1, max_length=200)
    due_at: datetime
    owner_id: Optional[int] = None


class LeadIn(_In):
    name: str = Field(..., min_length=1, max_length=200)
    type: LeadType
    owner_id: Optional[int] = None
    city: Optional[str] = Field(None, max_length=120)
    website: Optional[str] = Field(None, max_length=300)
    source: Optional[Source] = None
    value_inr: Optional[int] = Field(None, ge=0, le=10_000_000_000)
    expected_close: Optional[date] = None
    tags: Optional[list[str]] = Field(None, max_length=20)
    contacts: Optional[list[ContactIn]] = Field(None, max_length=50)
    club_id: Optional[int] = None
    follow_up: Optional[FollowUpBody] = None
    force: bool = False


class LeadPatch(_In):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    type: Optional[LeadType] = None
    stage: Optional[Stage] = None
    owner_id: Optional[int] = None
    city: Optional[str] = Field(None, max_length=120)
    website: Optional[str] = Field(None, max_length=300)
    value_inr: Optional[int] = Field(None, ge=0, le=10_000_000_000)
    expected_close: Optional[date] = None
    tags: Optional[list[str]] = Field(None, max_length=20)
    club_id: Optional[int] = None
    lost_reason: Optional[str] = Field(None, max_length=60)
    lost_note: Optional[str] = Field(None, max_length=500)
    retry_on: Optional[date] = None
    renewal_on: Optional[date] = None


class MergeIn(_In):
    into_id: int


class ActivityIn(_In):
    kind: Literal["call", "meeting", "email", "whatsapp", "note"]
    body: str = Field(..., min_length=1, max_length=20000)
    occurred_at: Optional[datetime] = None
    contact_id: Optional[int] = None


class FollowUpIn(_In):
    lead_id: Optional[int] = None
    case_id: Optional[int] = None
    kind: FollowUpKind = "call"
    title: str = Field(..., min_length=1, max_length=200)
    due_at: datetime
    owner_id: Optional[int] = None


class FollowUpPatch(_In):
    done: Optional[bool] = None
    due_at: Optional[datetime] = None
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    kind: Optional[FollowUpKind] = None
    owner_id: Optional[int] = None


class TalkToUsIn(_In):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=30)
    organisation: str = Field(..., min_length=1, max_length=200)
    city: Optional[str] = Field(None, max_length=120)
    type: LeadType = "club"
    message: Optional[str] = Field(None, max_length=5000)
    page_url: Optional[str] = Field(None, max_length=1000)
