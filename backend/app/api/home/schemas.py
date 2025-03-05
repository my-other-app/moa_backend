from datetime import datetime
from pydantic import BaseModel, Field

from app.api.clubs.schemas import ClubPublicMin, NotesPublic
from app.api.events.schemas import EventAdditionalDetail, EventPublicMin
from app.core.response.base_model import CustomBaseModel


class SearchResults(CustomBaseModel):
    events: list[EventPublicMin]
    clubs: list[ClubPublicMin]
    notes: list[NotesPublic]


# RESPONSE MODEL
class EventClubDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    logo: dict | None = Field(None)


class EventInterestDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class EventCategoryDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class EventListResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    has_fee: bool = Field(...)
    has_prize: bool = Field(True)
    prize_amount: float | None = Field(None)
    is_online: bool = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    club: EventClubDetail = Field(...)
    category: EventCategoryDetail = Field(...)


class EventDetailResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    has_fee: bool = Field(...)
    reg_fee: float | None = Field(None)
    has_prize: bool = Field(True)
    prize_amount: float | None = Field(None)
    is_online: bool = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    club: EventClubDetail = Field(...)
    category: EventCategoryDetail = Field(...)
    interests: list[EventInterestDetail] | None = Field(None)
    additional_details: list[EventAdditionalDetail] | None = Field(None)
    rating: float = Field(...)
    total_rating: int = Field(...)
    about: str | None = Field(None)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    url: str | None = Field(None)
