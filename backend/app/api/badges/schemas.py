from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional, List


class BadgeDefinitionResponse(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    emoji: str
    badge_type: str
    trigger_metric: str
    threshold: float

    class Config:
        from_attributes = True


class AwardedBadgeResponse(BaseModel):
    id: int
    badge: BadgeDefinitionResponse
    awarded_at: datetime

    class Config:
        from_attributes = True


class UserBadgesResponse(BaseModel):
    user_id: int
    badges: list[AwardedBadgeResponse] = Field(default_factory=list)
    total: int = 0


class ClubBadgesResponse(BaseModel):
    club_id: int
    badges: list[AwardedBadgeResponse] = Field(default_factory=list)
    total: int = 0


class BadgeDefinitionCreate(BaseModel):
    slug: str
    name: str
    description: str
    emoji: str
    badge_type: str
    trigger_metric: str
    threshold: float


class BadgeDefinitionUpdate(BaseModel):
    slug: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    emoji: Optional[str] = None
    trigger_metric: Optional[str] = None
    threshold: Optional[float] = None


class BadgeAdminResponse(BadgeDefinitionResponse):
    claimed_count: int = 0


class BadgeRecipientResponse(BaseModel):
    id: int
    entity_id: int  # user_id or club_id
    entity_name: str
    entity_email: Optional[str] = None
    awarded_at: datetime
    # The recipient's picture: a club's logo, or a person's photo, else the
    # avatar they picked (reports.service.avatar). An image's links by size
    # ({"thumbnail", "medium", ...}), or null.
    avatar: Optional[dict] = None

    class Config:
        from_attributes = True


class BadgeRecipientsListResponse(BaseModel):
    badge: BadgeDefinitionResponse
    recipients: List[BadgeRecipientResponse] = Field(default_factory=list)
    total: int = 0


class ManualAwardRequest(BaseModel):
    entity_id: int  # user_id or club_id


class BadgeStatsResponse(BaseModel):
    badge: BadgeDefinitionResponse
    total_awarded: int = 0
    recent_awards: List[BadgeRecipientResponse] = Field(default_factory=list)
    awards_last_30_days: int = 0
    awards_last_7_days: int = 0
