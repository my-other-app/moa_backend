"""What the public discover site reads. The website is built against these
exact names: add fields, never rename them."""
from typing import Literal, Optional

from pydantic import BaseModel

from app.api.threads.schemas import ThreadOut


class Where(BaseModel):
    city: Optional[str] = None
    region: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    source: Literal["browser", "ip", "none"] = "none"


class CategoryRef(BaseModel):
    id: int
    name: str


class EventClub(BaseModel):
    id: int
    name: str
    slug: str
    logo: Optional[str] = None


class EventCard(BaseModel):
    id: int
    name: str
    poster: Optional[str] = None
    starts_at: str
    ends_at: str
    location_name: Optional[str] = None
    is_online: bool
    lat: Optional[float] = None
    lng: Optional[float] = None
    distance_km: Optional[float] = None
    has_fee: bool
    fee: Optional[float] = None
    category: Optional[CategoryRef] = None
    club: Optional[EventClub] = None
    going: int
    capacity: Optional[int] = None
    status: Literal["upcoming", "closing_soon", "full", "live", "ended"]
    url: str


class ClubCard(BaseModel):
    id: int
    name: str
    slug: str
    logo: Optional[str] = None
    cover: Optional[str] = None
    tagline: Optional[str] = None
    location_name: Optional[str] = None
    followers: int
    upcoming_events: int
    interests: list[str]
    distance_km: Optional[float] = None
    website_url: Optional[str] = None


class CategoryCount(BaseModel):
    id: int
    name: str
    icon: Optional[str] = None
    upcoming: int


class SearchCategory(BaseModel):
    id: int
    name: str
    upcoming: int


class InterestCount(BaseModel):
    id: int
    name: str
    clubs: int


class Stats(BaseModel):
    upcoming_events: int
    clubs: int
    cities: int


class Home(BaseModel):
    where: Where
    near: list[EventCard]
    this_week: list[EventCard]
    online: list[EventCard]
    popular: list[EventCard]
    categories: list[CategoryCount]
    clubs: list[ClubCard]
    threads: list[ThreadOut]
    stats: Stats


class EventSearch(BaseModel):
    items: list[EventCard]
    total: int
    offset: int
    limit: int
    where: Where
    categories: list[SearchCategory]


class ClubSearch(BaseModel):
    items: list[ClubCard]
    total: int
    offset: int
    limit: int
    where: Where
    interests: list[InterestCount]


class Socials(BaseModel):
    instagram: Optional[str] = None
    linkedin: Optional[str] = None
    youtube: Optional[str] = None
    website: Optional[str] = None


class ClubDetail(ClubCard):
    about: Optional[str] = None
    contact_email: Optional[str] = None
    socials: Socials
    events_held: int


class TeamLinks(BaseModel):
    instagram: Optional[str] = None
    linkedin: Optional[str] = None
    x: Optional[str] = None
    website: Optional[str] = None


class TeamMember(BaseModel):
    name: str
    role: Optional[str] = None
    photo: Optional[str] = None
    links: TeamLinks


class Partner(BaseModel):
    name: str
    role: Optional[str] = None
    logo: Optional[str] = None


class ClubPage(BaseModel):
    club: ClubDetail
    upcoming: list[EventCard]
    past: list[EventCard]
    threads: list[ThreadOut]
    team: list[TeamMember]
    partners: list[Partner]


class BackfillCounts(BaseModel):
    checked: int
    resolved: int


class BackfillResult(BaseModel):
    events: BackfillCounts
    clubs: BackfillCounts
