"""What the public discover site reads. The website is built against these
exact names: add fields, never rename them."""
from typing import Literal, Optional

from pydantic import BaseModel

from app.api.agenda.schemas import AgendaItemOut
from app.api.cohosts.schemas import CohostRefUrl
from app.api.event_partners.schemas import PagePartner
from app.api.interests.schemas import TopicRef
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
    #: "full": nothing left as the ticket picker counts it (holds count).
    status: Literal["upcoming", "closing_soon", "full", "live", "ended"]
    #: The club confirms each place (docs/APPROVALS.md), and its "High
    #: demand" label is on: a chip, never a number.
    requires_approval: bool = False
    high_demand: bool = False
    url: str
    # The description as one line of plain text, at most 200 characters (cut
    # at a word, with an ellipsis): enough for a feed without the event page.
    summary: Optional[str] = None
    # When the event was made (no publish time is kept; sort=newest orders
    # by this) and last changed, ISO 8601.
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # The distinct topics of the event's interests, in topic order.
    topics: list[TopicRef] = []
    #: The clubs co-hosting it, in the order they accepted; `club` is the
    #: host (app/api/cohosts, docs/COHOSTS.md).
    cohosts: list[CohostRefUrl] = []


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
    # The distinct topics of all the club's interests, in topic order.
    topics: list[TopicRef] = []


class CategoryCount(BaseModel):
    id: int
    name: str
    icon: Optional[str] = None
    upcoming: int


class SearchCategory(BaseModel):
    id: int
    name: str
    upcoming: int


class SearchTopic(BaseModel):
    """A topic beside the search, with its upcoming events (as /interests/topics counts them)."""
    id: int
    slug: str
    name: str
    color: Optional[str] = None
    upcoming: int


class InterestCount(BaseModel):
    id: int
    name: str
    clubs: int


class Stats(BaseModel):
    upcoming_events: int
    clubs: int
    # Places events were held, a venue within 25 km of another counted as the
    # same city (stats.py); events with no point on the map are not counted.
    cities: int


class Home(BaseModel):
    where: Where
    near: list[EventCard]
    this_week: list[EventCard]
    online: list[EventCard]
    popular: list[EventCard]
    categories: list[CategoryCount]
    # With a place: the clubs within 50 km, nearest first; with none that
    # near, or no place, the most followed, with no distance. A club in
    # here with a distance is near.
    clubs: list[ClubCard]
    # With a place: the nearest clubs further than 50 km, nearest first, each
    # with its distance; empty without a place.
    clubs_more: list[ClubCard] = []
    threads: list[ThreadOut]
    stats: Stats


class EventSearch(BaseModel):
    items: list[EventCard]
    # Exact up to 1,000; past that 1,000 with total_capped ("1,000+"), and
    # the pages go on: load more until a page comes back short.
    total: int
    total_capped: bool = False
    offset: int
    limit: int
    where: Where
    categories: list[SearchCategory]
    # Every topic, in topic order, whether anything is on in it or not.
    topics: list[SearchTopic] = []
    # Pass as `after` for the next page (offset stops at 1,000); null on the last page.
    next_cursor: Optional[str] = None


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
    # As EventSearch's.
    total: int
    total_capped: bool = False
    offset: int
    limit: int
    where: Where
    interests: list[InterestCount]
    next_cursor: Optional[str] = None


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
    # Whether to offer "Get updates from <club> on WhatsApp": the club can
    # broadcast from its own number (club_whatsapp.consent.offered).
    whatsapp_updates: bool = False


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
    #: What is left as the ticket picker counts it (holds count; a paid
    #: event's cap counts paid tickets). Null: no cap.
    seats_left: Optional[int] = None
    reg_opens_at: Optional[str] = None
    reg_closes_at: Optional[str] = None
    #: "cancelled": the club called it off (app/api/club_ops); `cancellation` says why and when.
    status: Literal["upcoming", "closing_soon", "full", "live", "ended", "cancelled"]
    registration_open: bool
    #: The club confirms each registration: the form makes a request (docs/APPROVALS.md).
    requires_approval: bool = False
    #: The club's "High demand" label is on: a chip, never a number.
    high_demand: bool = False
    cancellation: Optional[dict] = None
    category: Optional[CategoryRef] = None
    interests: list[str]
    topics: list[TopicRef] = []
    guidelines: Optional[str] = None
    contact: EventContact
    url: str
    summary: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PageClub(BaseModel):
    id: int
    name: str
    slug: str
    logo: Optional[str] = None
    tagline: Optional[str] = None
    followers: int
    location_name: Optional[str] = None
    # As ClubDetail's: the registration form offers the club's updates on WhatsApp.
    whatsapp_updates: bool = False


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
    # The event's own sponsors and partners, each in its order (docs/EVENT_PARTNERS.md).
    sponsors: list[PagePartner] = []
    partners: list[PagePartner] = []
    #: The clubs co-hosting it, beside `club` (the host), in the order they
    #: accepted (app/api/cohosts, docs/COHOSTS.md).
    cohosts: list[CohostRefUrl] = []
    more_from_club: list[EventCard]
    similar: list[EventCard]


class SitemapEntry(BaseModel):
    slug: str
    updated_at: str


class Sitemap(BaseModel):
    events: list[SitemapEntry]
    clubs: list[SitemapEntry]


class SitemapPageRef(BaseModel):
    """One page of the paged sitemap: GET /discover/sitemap/{kind}/{page}."""
    kind: Literal["events", "clubs"]
    page: int
    entries: int
    # The newest change on the page, for the sitemap index's <lastmod>.
    updated_at: Optional[str] = None


class SitemapIndex(BaseModel):
    """Every page of the sitemap, events then clubs, each at most page_size entries."""
    page_size: int
    pages: list[SitemapPageRef]


class SitemapPage(BaseModel):
    kind: Literal["events", "clubs"]
    page: int
    pages: int
    items: list[SitemapEntry]


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


class ClubPlaceFixes(BaseModel):
    """How many clubs' stored places disagreed with their rows and were put right."""
    clubs: int


class CounterFixes(BaseModel):
    """How many counters had to be recounted: events' going counts, clubs'
    follower counts, posts' likes, comments and views, poll options' votes,
    live questions' votes."""
    events: int
    clubs: int
    posts: int = 0
    poll_options: int = 0
    questions: int = 0
