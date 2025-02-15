from pydantic import AwareDatetime, BaseModel, Field
from datetime import datetime, timezone

from app.api.clubs.schemas import ClubPublic, ClubPublicMin
from app.api.orgs.schema import OrganizationPublicMin
from app.api.users.schemas import UserPublic


class EventCategoryBase(BaseModel):
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class EventCategoryPublic(EventCategoryBase):
    id: int = Field(...)


class EventCategoryCreate(EventCategoryBase):
    pass


class EventBaseMin(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    poster: str | None = Field(None)
    event_datetime: AwareDatetime = Field(...)
    has_fee: bool = Field(False)
    reg_fee: float | None = Field(None)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    has_prize: bool = Field(False)
    prize_amount: float | None = Field(None)
    is_online: bool = Field(False)
    reg_startdate: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reg_enddate: datetime | None = Field(None)


class EventBase(EventBaseMin):
    images: list[str] = Field([])
    about: str | None = Field(None)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    url: str | None = Field(None)


class EventPublicMin(EventBaseMin):
    id: int = Field(...)
    category: EventCategoryPublic = Field(...)
    club: ClubPublicMin = Field(...)
    org: OrganizationPublicMin | None = Field(None)


class EventPublic(EventBase):
    id: int = Field(...)
    category: EventCategoryPublic = Field(...)
    club: ClubPublic = Field(...)
    created_by: UserPublic = Field(...)
    org: OrganizationPublicMin | None = Field(None)


class EventCreate(EventBase):
    org_id: int | None = Field(None)
    category_id: int = Field(...)
    club_id: int | None = Field(...)


class EventEdit(EventCreate):
    id: int = Field(...)
    pass
