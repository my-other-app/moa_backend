from datetime import datetime
from pydantic import BaseModel

from app.api.clubs.schemas import ClubPublic
from app.api.users.schemas import UserPublic
from app.api.notifications.models import NotificationStatus
from app.core.response.base_model import CustomBaseModel


class NotificationSchema(CustomBaseModel):
    id: str
    user_id: int
    title: str
    description: str
    status: NotificationStatus
    from_club: ClubPublic | None = None
    from_user: UserPublic | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
