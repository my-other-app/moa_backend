import enum
import json
from typing import Any, Optional
from uuid import UUID
from fastapi import File, Form, UploadFile
from pydantic import AwareDatetime, BaseModel, EmailStr, Field, ValidationError
from datetime import datetime, timezone
from fastapi.exceptions import RequestValidationError

from app.api.clubs.schemas import ClubPublic, ClubPublicMin
from app.api.users.schemas import UserPublic
from app.api.interests.schemas import InterestPublic
from app.response import CustomHTTPException
from app.core.response.base_model import CustomBaseModel
from app.api.auth.schemas import Token


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


class EventAdditionalDetail(CustomBaseModel):
    key: str
    label: str
    field_type: FieldTypes
    required: bool = True
    options: list[str] | None = None


class EventCategoryBase(CustomBaseModel):
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)


class EventCategoryPublic(EventCategoryBase):
    id: int = Field(...)


class EventCategoryCreate(EventCategoryBase):
    pass


class EventBaseMin(CustomBaseModel):
    name: str = Field(min_length=3, max_length=100)
    poster: dict | None = Field(None)
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
    max_participants: int | None = Field(None)
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
    rating: float = Field(...)
    total_rating: int = Field(...)
    additional_details: list[EventAdditionalDetail] | None = Field(None)
    interests: list[InterestPublic] | None = Field(None)


class Event(EventBase):
    id: int = Field(...)
    category_id: int = Field(...)
    club_id: int | None = Field(...)
    additional_details: list[EventAdditionalDetail] | None = Field(None)
    event_guidelines: str | None = Field(None)


class EventCreate:
    def __init__(
        self,
        name: str = Form(..., min_length=3, max_length=100),
        poster: Optional[UploadFile] = File(None),
        event_datetime: datetime = Form(...),  # ISO format string
        has_fee: bool = Form(False),
        reg_fee: Optional[float] = Form(None),
        duration: float = Form(...),
        location_name: Optional[str] = Form(None),
        has_prize: bool = Form(False),
        prize_amount: Optional[float] = Form(None),
        is_online: bool = Form(False),
        reg_startdate: str = Form(
            default_factory=lambda: datetime.now(timezone.utc).isoformat()
        ),  # ISO string
        reg_enddate: Optional[str] = Form(None),  # ISO format string
        # images: Optional[str] = Form("[]"),  # JSON string
        about: Optional[str] = Form(None),
        contact_phone: Optional[str] = Form(None),
        contact_email: Optional[str] = Form(None),
        url: Optional[str] = Form(None),
        category_id: int = Form(...),
        club_id: Optional[int] = Form(None),
        interest_ids: Optional[str] = Form(""),  # JSON string
        max_participants: Optional[int] = Form(None),
        additional_details: Optional[str] = Form("[]"),  # JSON string
        event_guidelines: Optional[str] = Form(None),
    ):
        self.name = name
        self.poster = poster
        self.event_datetime = event_datetime
        self.has_fee = has_fee
        self.reg_fee = reg_fee
        self.duration = duration
        self.location_name = location_name
        self.has_prize = has_prize
        self.prize_amount = prize_amount
        self.is_online = is_online
        self.reg_startdate = datetime.fromisoformat(reg_startdate)
        self.reg_enddate = datetime.fromisoformat(reg_enddate) if reg_enddate else None
        # self.images = json.loads(images)  # Convert JSON string to list
        self.about = about
        self.contact_phone = contact_phone
        self.contact_email = contact_email
        self.url = url
        self.category_id = category_id
        self.club_id = club_id
        try:
            self.interest_ids = (
                [int(x) for x in interest_ids.split(",")] if interest_ids else None
            )
        except Exception:
            raise CustomHTTPException(
                status_code=400,
                message="Invalid interest_ids format, expected comma seperated integers",
            )
        self.max_participants = max_participants
        self.event_guidelines = event_guidelines
        try:
            self.additional_details = [
                EventAdditionalDetail(**detail)
                for detail in json.loads(additional_details)
            ]
        except json.JSONDecodeError:
            raise CustomHTTPException(
                status_code=400,
                message="Invalid JSON format for additional_details",
            )
        except ValidationError as e:
            raise RequestValidationError(e.errors())


class EventEdit(EventCreate):

    def __init__(
        self,
        name: str = Form(..., min_length=3, max_length=100),
        poster: Optional[UploadFile] = File(None),
        event_datetime: datetime = Form(...),  # ISO format string
        has_fee: bool = Form(False),
        reg_fee: Optional[float] = Form(None),
        duration: float = Form(...),
        location_name: Optional[str] = Form(None),
        has_prize: bool = Form(False),
        prize_amount: Optional[float] = Form(None),
        is_online: bool = Form(False),
        reg_startdate: str = Form(
            default_factory=lambda: datetime.now(timezone.utc).isoformat()
        ),  # ISO string
        reg_enddate: Optional[str] = Form(None),  # ISO format string
        # images: Optional[str] = Form("[]"),  # JSON string
        about: Optional[str] = Form(None),
        contact_phone: Optional[str] = Form(None),
        contact_email: Optional[str] = Form(None),
        url: Optional[str] = Form(None),
        category_id: int = Form(...),
        club_id: Optional[int] = Form(None),
        interest_ids: Optional[str] = Form("[]"),  # JSON string
        additional_details: Optional[str] = Form("[]"),  # JSON string
        event_guidelines: Optional[str] = Form(None),
        max_participants: Optional[int] = Form(None),
    ):
        super().__init__(
            name=name,
            poster=poster,
            event_datetime=event_datetime,
            has_fee=has_fee,
            reg_fee=reg_fee,
            duration=duration,
            location_name=location_name,
            has_prize=has_prize,
            prize_amount=prize_amount,
            is_online=is_online,
            reg_startdate=reg_startdate,
            reg_enddate=reg_enddate,
            # images,
            about=about,
            contact_phone=contact_phone,
            contact_email=contact_email,
            url=url,
            category_id=category_id,
            club_id=club_id,
            interest_ids=interest_ids,
            additional_details=additional_details,
            max_participants=max_participants,
            event_guidelines=event_guidelines,
        )


class EventRatingCreate(CustomBaseModel):
    rating: float = Field(..., ge=0, le=5)
    review: str | None = Field(None)


class EventRating(EventRatingCreate):
    id: str
    event_id: int
    user_id: int
    created_at: datetime


# RESPONSE MODELs


class EventCategoryResponse(CustomBaseModel):
    id: int
    name: str
    icon: str | None = None
    icon_type: str | None = None


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


class EventCreateUpdateResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    has_fee: bool = Field(...)
    reg_fee: float | None = Field(None)
    has_prize: bool = Field(True)
    prize_amount: float | None = Field(None)
    is_online: bool = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)
    club: EventClubDetail = Field(...)
    category: EventCategoryDetail = Field(...)
    interests: list[EventInterestDetail] | None = Field(None)
    additional_details: list[EventAdditionalDetail] | None = Field(None)
    event_guidelines: str | None = Field(None)
    max_participants: int | None = Field(None)


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


class EventListResponseSelf(CustomBaseModel):
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
    event_guidelines: str | None = Field(None)
    max_participants: int | None = Field(None)


# REQUEST MODELS


class TicketDetailsResponse(CustomBaseModel):
    ticket_id: str
    event: EventListResponse
    is_paid: bool
    actual_amount: float | None
    paid_amount: float | None
    payment_receipt: str | None
    is_attended: bool
    attended_on: datetime | None
    user: UserPublic
    created_at: datetime
