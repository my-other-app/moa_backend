from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional


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
