from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from app.api.clubs.schemas import ClubPublic
from app.api.users.schemas import UserPublic
from app.api.notifications.models import NotificationStatus
from app.core.response.base_model import CustomBaseModel


class EventBrief(BaseModel):
    """Brief event info for notification context."""
    id: int
    name: str
    
    class Config:
        from_attributes = True


class NotificationSchema(CustomBaseModel):
    """Notification response schema with related entities."""
    id: str
    user_id: int
    title: str
    description: str
    type: str
    status: NotificationStatus
    from_club: Optional[ClubPublic] = None
    from_user: Optional[UserPublic] = None
    event: Optional[EventBrief] = None
    data: Optional[dict] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

