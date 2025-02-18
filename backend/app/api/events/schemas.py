import enum
from pydantic import AwareDatetime, BaseModel, Field
from datetime import datetime, timezone

from app.api.clubs.schemas import ClubPublic, ClubPublicMin
from app.api.users.schemas import UserPublic


class FieldTypes(enum.Enum):
    text = "text"
    number = "number"
    email = "email"
    url = "url"
    tel = "tel"
    date = "date"
    time = "time"
    datetime = "datetime"
    checkbox = "checkbox"
    radio = "radio"
    select = "select"
    textarea = "textarea"
    file = "file"
    image = "image"


class EventAdditionalDetail(BaseModel):
    key: str
    label: str
    field_type: FieldTypes
    required: bool = True
    options: list[str] | None = None


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


class EventPublic(EventBase):
    id: int = Field(...)
    category: EventCategoryPublic = Field(...)
    club: ClubPublic = Field(...)
    additional_details: list[EventAdditionalDetail] | None = Field(None)


class Event(EventBase):
    id: int = Field(...)
    category_id: int = Field(...)
    club_id: int | None = Field(...)
    additional_details: list[EventAdditionalDetail] | None = Field(None)


class EventCreate(EventBase):
    category_id: int = Field(...)
    club_id: int | None = Field(...)
    additional_details: list[EventAdditionalDetail] | None = Field(None)


class EventEdit(EventCreate):
    id: int = Field(...)
    pass


class EventRegistration(BaseModel):
    user: UserPublic
    event: EventPublic
    additional_details: dict | None = Field(None)
