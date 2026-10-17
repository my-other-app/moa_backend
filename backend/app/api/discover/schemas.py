"""What the public discover site reads. The website is built against these
exact names: add fields, never rename them."""
from typing import Literal, Optional

from pydantic import BaseModel

from app.api.agenda.schemas import AgendaItemOut
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
    slug: str
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


class ClubEvents(BaseModel):
    """A club's events, a page at a time (past: newest first; upcoming: soonest first)."""
    items: list[EventCard]
    total: int
    offset: int
    limit: int


class CalendarMonth(BaseModel):
    """How many public events touch each day of a month ("YYYY-MM-DD", days
    in India); days with none are left out."""
    month: str
    days: dict[str, int]


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


class EventContact(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class PageEvent(BaseModel):
    """The event itself. Never the online joining link: that goes to those registered."""
    id: int
    slug: str
    name: str
    about: Optional[str] = None
    poster: Optional[str] = None
    poster_thumb: Optional[str] = None
    starts_at: str
    ends_at: str
    duration_hours: Optional[float] = None
    timezone: str
    is_online: bool
    location_name: Optional[str] = None
    location_link: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    has_fee: bool
    fee: Optional[float] = None
    has_prize: bool
    prize_amount: Optional[float] = None
    capacity: Optional[int] = None
    going: int
    seats_left: Optional[int] = None
    reg_opens_at: Optional[str] = None
    reg_closes_at: Optional[str] = None
    status: Literal["upcoming", "closing_soon", "full", "live", "ended"]
    registration_open: bool
    category: Optional[CategoryRef] = None
    interests: list[str]
    guidelines: Optional[str] = None
    contact: EventContact
    url: str


class PageClub(BaseModel):
    id: int
    name: str
    slug: str
    logo: Optional[str] = None
    tagline: Optional[str] = None
    followers: int
    location_name: Optional[str] = None


class Speaker(BaseModel):
    id: int
    name: str
    designation: Optional[str] = None
    photo: Optional[str] = None
    bio: Optional[str] = None
    link: Optional[str] = None


class PageAgenda(BaseModel):
    timezone: str
    items: list[AgendaItemOut]


class EventPage(BaseModel):
    event: PageEvent
    club: PageClub
    speakers: list[Speaker]
    agenda: PageAgenda
    team: list[TeamMember]
    # The event's own organisers when it names them, else the club's team.
    team_source: Literal["event", "club", "none"]
    more_from_club: list[EventCard]
    similar: list[EventCard]


class SitemapEntry(BaseModel):
    slug: str
    updated_at: str


class Sitemap(BaseModel):
    events: list[SitemapEntry]
    clubs: list[SitemapEntry]


class BackfillCounts(BaseModel):
    checked: int
    resolved: int


class BackfillResult(BaseModel):
    events: BackfillCounts
    clubs: BackfillCounts


class GeocodeCounts(BaseModel):
    # Rows whose words the geocoder has answered for (from its cache or just now).
    checked: int
    # Of those, the rows that got a point.
    geocoded: int
    not_found: int


class GeocodeBackfillResult(BaseModel):
    """What one run of the venue geocoder's backfill did."""
    # False when GEOCODER_ENABLED is off: then nothing was done.
    enabled: bool
    events: GeocodeCounts
    clubs: GeocodeCounts
    # Requests sent to the geocoder in this run (one a second).
    requests: int
    # Rows left without an answer: the request budget ran out, the geocoder
    # paused or did not answer. Run it again for them.
    remaining: int


class CounterFixes(BaseModel):
    """How many events' going counts and clubs' follower counts had to be recounted."""
    events: int
    clubs: int
