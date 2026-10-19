import enum
import json
from typing import Any, Optional
from urllib.parse import urlsplit
from uuid import UUID
from fastapi import File, Form, UploadFile
from pydantic import AwareDatetime, BaseModel, EmailStr, Field, ValidationError, field_validator
from datetime import datetime, timezone
from fastapi.exceptions import RequestValidationError

from app.api.clubs.schemas import ClubPublic, ClubPublicMin
from app.api.users.schemas import UserPublic
from app.api.interests.schemas import InterestPublic, TopicRef
from app.response import CustomHTTPException
from app.core.response.base_model import CustomBaseModel
from app.api.auth.schemas import Token
from app.api.discover.geo import pin_from_form


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


SPEAKER_BIO_MAX = 600
SPEAKER_LINK_MAX = 500


class EventSpeakerCreate(CustomBaseModel):
    """Schema for creating/updating event speakers."""
    name: str = Field(..., min_length=1, max_length=100)
    designation: str | None = Field(None, max_length=200)
    photo_url: str | None = Field(None)  # URL or base64 image data
    photo_index: int | None = Field(None) # Index in the uploaded files list
    id: int | None = Field(None) # ID for updating existing speakers
    # Left out, an existing speaker keeps theirs: older clients do not send these.
    bio: str | None = Field(None)
    link: str | None = Field(None)

    @field_validator("bio", mode="before")
    @classmethod
    def _bio(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("A speaker's bio must be text.")
        v = v.strip()
        if len(v) > SPEAKER_BIO_MAX:
            raise ValueError(f"A speaker's bio can be at most {SPEAKER_BIO_MAX} characters.")
        return v or None

    @field_validator("link", mode="before")
    @classmethod
    def _link(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("A speaker's link must be a web address.")
        v = v.strip()
        if not v:
            return None
        parts = urlsplit(v)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc or any(c.isspace() for c in v):
            raise ValueError("A speaker's link must be a web address starting with http:// or https://.")
        if len(v) > SPEAKER_LINK_MAX:
            raise ValueError(f"A speaker's link can be at most {SPEAKER_LINK_MAX} characters.")
        return v


class EventSpeakerPublic(CustomBaseModel):
    """Schema for event speaker response."""
    id: int
    name: str
    designation: str | None = None
    photo: dict | None = None
    bio: str | None = None
    link: str | None = None
    display_order: int = 0


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
    merch_enabled: bool = Field(False)
    swag_enabled: bool = Field(False)
    is_online: bool | None = Field(False)
    reg_startdate: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reg_enddate: datetime | None = Field(None)
    page_views: int = Field(0)
    status: str = Field(...)


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
    club: ClubPublicMin | None = Field(None)
    interests: list[InterestPublic] | None = Field(None)


class EventPublic(EventBase):
    id: int = Field(...)
    category: EventCategoryPublic = Field(...)
    club: ClubPublic | None = Field(None)
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


#: The pin's form fields: range-checked here, paired by pin_from_form.
def _lat_form():
    return Form(None, ge=-90, le=90, description="The venue's latitude, from the dashboard's map; sent with lng")


def _lng_form():
    return Form(None, ge=-180, le=180, description="The venue's longitude, from the dashboard's map; sent with lat")


def parse_store_item_ids(raw: Optional[str]) -> Optional[list[str]]:
    """The form's `store_item_ids`: a JSON array of item ids, or None when
    the form did not send it (a form posts "" as omitted). Each id is checked
    against the club's store when it is used (store.service.check_offers)."""
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > 200:
        raise CustomHTTPException(
            status_code=422,
            message="store_item_ids must be a JSON array of item ids",
            errors={"store_item_ids": "a JSON array of up to 200 item ids"},
        )
    return value


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
        # A map link for the venue; a point is read from it for "near you".
        location_link: Optional[str] = Form(None, max_length=2000),
        # A pin from the dashboard's map: the point itself, above any link's.
        lat: Optional[float] = _lat_form(),
        lng: Optional[float] = _lng_form(),
        has_prize: bool = Form(False),
        prize_amount: Optional[float] = Form(None),
        is_online: bool = Form(False),
        merch_enabled: Optional[bool] = Form(None),
        swag_enabled: Optional[bool] = Form(None),
        # The club's store items the event offers, in order: a JSON array of
        # item ids ("[]" offers none). Omitted leaves the offers as they are.
        store_item_ids: Optional[str] = Form(None),
        reg_startdate: str = Form(
            default_factory=lambda: datetime.now(timezone.utc).isoformat()
        ),  # ISO string
        reg_enddate: Optional[str] = Form(None),  # ISO format string
        # images: Optional[str] = Form("[]"),  # JSON string
        about: Optional[str] = Form(None),
        contact_name: Optional[str] = Form(None),
        contact_phone: Optional[str] = Form(None),
        contact_email: Optional[str] = Form(None),
        url: Optional[str] = Form(None),
        category_id: int = Form(...),
        club_id: Optional[int] = Form(None),
        interest_ids: Optional[str] = Form(""),  # JSON string
        max_participants: Optional[int] = Form(None),
        additional_details: Optional[str] = Form("[]"),  # JSON string
        event_guidelines: Optional[str] = Form(None),
        event_tag: Optional[str] = Form(None),  # Event tag like "Free", "Paid", etc.
        speakers: Optional[str] = Form("[]"),  # JSON array of speakers
        ticket_template_id: Optional[int] = Form(None),
        certificate_template_id: Optional[int] = Form(None),
        # Where the event starts on its status clock: a draft stays off the
        # app and refuses check-ins until it is published.
        status: Optional[str] = Form(None, pattern="^(draft|published)$"),
    ):
        self.name = name
        self.poster = poster
        self.status = status
        self.event_datetime = event_datetime
        self.has_fee = has_fee
        self.reg_fee = reg_fee
        self.duration = duration
        self.location_name = location_name
        self.location_link = location_link
        self.pin = pin_from_form(lat, lng)
        self.has_prize = has_prize
        self.prize_amount = prize_amount
        self.is_online = is_online
        self.merch_enabled = merch_enabled
        self.swag_enabled = swag_enabled
        self.store_item_ids = parse_store_item_ids(store_item_ids)
        self.reg_startdate = datetime.fromisoformat(reg_startdate)
        self.reg_enddate = datetime.fromisoformat(reg_enddate) if reg_enddate else None
        # self.images = json.loads(images)  # Convert JSON string to list
        self.about = about
        self.contact_name = contact_name
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
        self.event_tag = event_tag
        self.ticket_template_id = ticket_template_id
        self.certificate_template_id = certificate_template_id
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
        
        # Parse speakers JSON
        try:
            speakers_data = json.loads(speakers) if speakers else []
            self.speakers = [
                EventSpeakerCreate(**speaker) if isinstance(speaker, dict) else speaker
                for speaker in speakers_data
            ]
        except json.JSONDecodeError:
            raise CustomHTTPException(
                status_code=400,
                message="Invalid JSON format for speakers",
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
        # A map link for the venue; a point is read from it for "near you". Omitted keeps the
        # link (a form posts "" as omitted), so removing it takes clear_location_link.
        location_link: Optional[str] = Form(None, max_length=2000),
        clear_location_link: bool = Form(False),
        # A pin from the dashboard's map. It stays through later saves and
        # link changes until new values replace it, or clear_point drops it
        # back to the point the link or the venue's words give.
        lat: Optional[float] = _lat_form(),
        lng: Optional[float] = _lng_form(),
        clear_point: bool = Form(False),
        has_prize: bool = Form(False),
        prize_amount: Optional[float] = Form(None),
        is_online: bool = Form(False),
        merch_enabled: Optional[bool] = Form(None),
        swag_enabled: Optional[bool] = Form(None),
        # The club's store items the event offers, in order: a JSON array of
        # item ids ("[]" offers none). Omitted leaves the offers as they are.
        store_item_ids: Optional[str] = Form(None),
        reg_startdate: str = Form(
            default_factory=lambda: datetime.now(timezone.utc).isoformat()
        ),  # ISO string
        reg_enddate: Optional[str] = Form(None),  # ISO format string
        # images: Optional[str] = Form("[]"),  # JSON string
        about: Optional[str] = Form(None),
        contact_name: Optional[str] = Form(None),
        contact_phone: Optional[str] = Form(None),
        contact_email: Optional[str] = Form(None),
        url: Optional[str] = Form(None),
        category_id: int = Form(...),
        club_id: Optional[int] = Form(None),
        interest_ids: Optional[str] = Form("[]"),  # JSON string
        additional_details: Optional[str] = Form("[]"),  # JSON string
        event_guidelines: Optional[str] = Form(None),
        max_participants: Optional[int] = Form(None),
        event_tag: Optional[str] = Form(None),
        speakers: Optional[str] = Form("[]"),
        ticket_template_id: Optional[int] = Form(None),
        certificate_template_id: Optional[int] = Form(None),
    ):
        super().__init__(
            name=name,
            poster=poster,
            event_datetime=event_datetime,
            has_fee=has_fee,
            reg_fee=reg_fee,
            duration=duration,
            location_name=location_name,
            location_link=location_link,
            lat=lat,
            lng=lng,
            has_prize=has_prize,
            prize_amount=prize_amount,
            is_online=is_online,
            merch_enabled=merch_enabled,
            swag_enabled=swag_enabled,
            store_item_ids=store_item_ids,
            reg_startdate=reg_startdate,
            reg_enddate=reg_enddate,
            # images,
            about=about,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            url=url,
            category_id=category_id,
            club_id=club_id,
            interest_ids=interest_ids,
            additional_details=additional_details,
            max_participants=max_participants,
            event_guidelines=event_guidelines,
            event_tag=event_tag,
            speakers=speakers,
            ticket_template_id=ticket_template_id,
            certificate_template_id=certificate_template_id,
        )
        self.clear_location_link = clear_location_link
        self.clear_point = clear_point


class EventRatingCreate(CustomBaseModel):
    rating: float = Field(..., ge=0, le=5)
    review: str | None = Field(None)


class EventRating(EventRatingCreate):
    id: UUID
    event_id: int
    user_id: int
    created_at: datetime
    
    
class EventRatingDetailResponse(CustomBaseModel):
    id: UUID
    rating: float
    review: str | None
    user: UserPublic
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
    # Its topic (GET /interests/list has the tree), and its address.
    category_id: int | None = Field(None)
    slug: str | None = Field(None)

    @field_validator("icon_type", mode="before")
    @classmethod
    def _extract_icon_type_value(cls, value):
        return value.value if hasattr(value, "value") else value


class EventCategoryDetail(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    icon: str | None = Field(None)
    icon_type: str | None = Field(None)

    @field_validator("icon_type", mode="before")
    @classmethod
    def _extract_icon_type_value(cls, value):
        return value.value if hasattr(value, "value") else value


class EventCreateUpdateResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    # The venue's point as it stands after the save, and where it came from:
    # 'manual' (a pin), 'link' (the map link) or 'geocoded' (the venue's
    # words). A point still to be found after the save is null here.
    lat: float | None = Field(None)
    lng: float | None = Field(None)
    geo_source: str | None = Field(None)
    has_fee: bool = Field(...)
    reg_fee: float | None = Field(None)
    has_prize: bool = Field(True)
    prize_amount: float | None = Field(None)
    is_online: bool | None = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)
    club: EventClubDetail | None = Field(None)
    category: EventCategoryDetail = Field(...)
    interests: list[EventInterestDetail] | None = Field(None)
    # The distinct topics of its interests, in topic order.
    topics: list[TopicRef] = Field(default_factory=list)
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
    is_online: bool | None = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    club: EventClubDetail | None = Field(None)
    category: EventCategoryDetail = Field(...)
    page_views: int = Field(0)
    # User-specific fields (only populated when authenticated and registered)
    is_attended: bool | None = Field(None)
    # A paid event's registration still waiting for its payment is not a ticket yet.
    is_paid: bool | None = Field(None)
    ticket_id: str | None = Field(None)
    ticket_url: str | None = Field(None)
    # The organiser revoked this ticket: the door refuses it. Null when the
    # caller has no registration for the event.
    ticket_revoked: bool | None = Field(None)
    certificate_url: str | None = Field(None)



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
    is_online: bool | None = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    category: EventCategoryDetail = Field(...)
    page_views: int = Field(0)
    registration_count: int = Field(0)
    attended_count: int = Field(0)
    #: Where the event is on its status clock: draft, published, live, closed, archived.
    status: str = Field("published")
    #: What the organiser checks the row for.
    checked_in: int = Field(0)
    max_participants: Optional[int] = Field(None)
    collected: float = Field(0)
    outstanding: float = Field(0)
    door_open: bool = Field(False)
    registration_open: bool = Field(False)
    ends_at: Optional[datetime] = Field(None)


class EventAdminListResponse(CustomBaseModel):
    """Admin event listing — club is optional since Events.club_id is nullable."""

    id: int = Field(...)
    name: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float | None = Field(None)
    location_name: str | None = Field(None)
    has_fee: bool = Field(...)
    reg_fee: float | None = Field(None)
    has_prize: bool = Field(False)
    prize_amount: float | None = Field(None)
    is_online: bool | None = Field(False)
    reg_startdate: datetime | None = Field(None)
    reg_enddate: datetime | None = Field(None)
    page_views: int = Field(0)
    is_deleted: bool = Field(False)
    category: EventCategoryDetail | None = Field(None)
    club: EventClubDetail | None = Field(None)
    interests: list[EventInterestDetail] | None = Field(None)
    topics: list[TopicRef] = Field(default_factory=list)

    #: How many people registered, and how many were marked present.
    #:
    #: Defaulted rather than required: an event that has finished with
    #: `attended_count` at zero and registrations above it is the one an admin
    #: is looking for -- certificates and winners are both blocked until the
    #: club marks attendance -- and a missing count must not be mistaken for
    #: a marked zero. The list fills both for every row it returns.
    registration_count: int = Field(0)
    attended_count: int = Field(0)


class EventFileResponse(CustomBaseModel):
    """Response model for event files/downloads."""
    name: str
    url: str


class EventDetailResponse(CustomBaseModel):
    id: int = Field(...)
    name: str = Field(...)
    slug: str = Field(...)
    poster: dict | None = Field(None)
    event_datetime: datetime = Field(...)
    duration: float = Field(...)
    location_name: str | None = Field(None)
    location_link: str | None = Field(None)
    # The venue's pin for the dashboard's map, and where it came from:
    # 'manual', 'link' or 'geocoded' (null when there is no point yet).
    lat: float | None = Field(None)
    lng: float | None = Field(None)
    geo_source: str | None = Field(None)
    has_fee: bool = Field(...)
    reg_fee: float | None = Field(None)
    has_prize: bool = Field(True)
    merch_enabled: bool = Field(False)
    swag_enabled: bool = Field(False)
    prize_amount: float | None = Field(None)
    is_online: bool | None = Field(False)
    reg_startdate: datetime = Field(...)
    reg_enddate: datetime | None = Field(None)
    club: EventClubDetail | None = Field(None)
    category: EventCategoryDetail = Field(...)
    interests: list[EventInterestDetail] | None = Field(None)
    # The distinct topics of its interests, in topic order.
    topics: list[TopicRef] = Field(default_factory=list)
    additional_details: list[EventAdditionalDetail] | None = Field(None)
    rating: float = Field(...)
    total_rating: int = Field(...)
    about: str | None = Field(None)
    contact_name: str | None = Field(None)
    contact_phone: str | None = Field(None)
    contact_email: str | None = Field(None)
    url: str | None = Field(None)
    event_guidelines: str | None = Field(None)
    max_participants: int | None = Field(None)
    page_views: int = Field(0)
    #: The status clock, so the console can say "draft" or "closed" without a second call.
    status: str = Field("published")
    status_clock: bool = Field(True)
    retention_mode: str = Field("keep")
    created_at: datetime | None = Field(None)
    #: Whether the club's plan leaves this event as it was when enforcement
    #: began (subscriptions.gates.grandfathered, its date window included):
    #: no event-scoped lock applies to it. False whenever nothing is enforced
    #: (the switch is off, or there is no Free plan).
    plan_grandfathered: bool = Field(False)
    #: Whose Razorpay account takes this event's ticket and merch payments:
    #: "club" when the club has connected its own and it is working,
    #: "platform" otherwise (app/api/payment_gateways).
    payments_to: str = Field("platform")
    # User-specific fields (only populated when authenticated)
    is_registered: bool = Field(False)
    #: Null when the viewer is not registered. True for a free event, or a
    #: paid one that is paid; false while a paid event's ticket waits for
    #: payment -- the door refuses that ticket (FEE_UNPAID).
    is_paid: bool | None = Field(None)
    #: The viewer's own registration, for POST /payments/orders when it is
    #: not paid yet. Null when not registered.
    registration_id: str | None = Field(None)
    is_attended: bool = Field(False)
    ticket_id: str | None = Field(None)
    ticket_url: str | None = Field(None)
    certificate_url: str | None = Field(None)
    #: The viewer's own link to the event's feedback survey, the one its
    #: email sends (`/feedback/{event}/{registration}?k=...`, the app's survey
    #: screen): set once they were checked in and the survey is open (sent,
    #: or its time after the end has come). Null otherwise, and when signed out.
    feedback_url: str | None = Field(None)
    user_rating: float | None = Field(None)
    user_position: int | None = Field(None)
    position_label: str | None = Field(None)
    downloads: list[EventFileResponse] | None = Field(None)
    speakers: list[EventSpeakerPublic] | None = Field(None)


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


# ─── Networking Discovery ─────────────────────────────────────────────

class NetworkingInterestItem(BaseModel):
    id: int
    name: str
    icon: str | None = None
    icon_type: str | None = None


class NetworkingAttendeeResponse(BaseModel):
    user_id: int
    name: str
    profile_pic_url: str | None = None
    organization: str | None = None
    shared_interests: list[NetworkingInterestItem] = Field(default_factory=list)
    match_count: int = 0


class NetworkingAttendeesResponse(BaseModel):
    event_id: int
    event_name: str
    window_closes_at: datetime | None = None
    attendees: list[NetworkingAttendeeResponse] = Field(default_factory=list)
    total: int = 0
