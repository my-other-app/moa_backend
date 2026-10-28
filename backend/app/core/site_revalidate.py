"""Telling the public site that one of its pages changed.

myotherapp.in renders event and club pages once and keeps them (Next.js
ISR). When one changes here, the site is told, so it renders that page and
the sitemap again within seconds rather than whenever its copy runs out:

    POST settings.SITE_REVALIDATE_URL
    Authorization: Bearer <settings.SITE_REVALIDATE_SECRET>
    Content-Type: application/json

    {"events": ["<slug>", "<id>", ...], "clubs": ["<slug>", ...], "sitemap": true, "topics": true}

`events` holds each event's slug and its id (as a string: the site has
pages by both); a renamed event's old slug comes too, as does a club's.

What counts is watched at the ORM, so every path that saves through a
session is covered without having to remember to call anything: the
dashboard's edits and the admin's, restores, duplicates, the status clock's
sweep, approval and rejection, logo and cover uploads, scripts.

  * An event created, edited, published, unpublished, cancelled, deleted or
    moved along by the status clock -- unless it was a draft (or deleted)
    both before and after, which no visitor could see. With it goes its
    club, whose page lists it.
  * A club's own row (name, logo, cover, tagline, verification, deletion...)
    -- unless it was unverified (or deleted) both before and after: a club
    rejected before it was ever approved had no page. With it go its most
    recent events, whose pages show the club.
  * The rows those pages show: an event's speakers, agenda, organisers,
    sponsors and partners;
    a club's team, partners, socials and interests.
  * A club's invite to co-host an event answered, left or ended
    (app/api/cohosts): the event's page, and that club's, which lists it.

An admin's change to the topic tree (a topic or an interest made, renamed,
moved, reordered or deleted) is told as "topics": true (`topics_changed`,
called by those endpoints), with whatever else is waiting.

Counters and housekeeping -- page views, going and follower counts,
admission numbers, door, kiosk and retention settings, timestamps -- change
no page and send nothing.

Only after the transaction commits (app.core.after_commit: a rollback sends
nothing). The slugs join a set, and DEBOUNCE_SECONDS after the first of a
burst the whole set goes in one POST, on a worker thread, with a short
timeout, never raising: whatever is lost, the site's own expiry still
catches. Just before sending, this process's discover cache (a minute's
answers for the front page and the sitemap) is emptied, so that what the
site asks for next is the new page rather than the minute-old one.

An empty SITE_REVALIDATE_SECRET switches all of it off.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Iterator, Optional

import httpx
from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.core import after_commit

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 3.0
TIMEOUT_SECONDS = 3.0
#: At most this many entries in each list of one POST, more going in several:
#: the site's handler reads no more than 200 of each.
MAX_PER_POST = 200
#: A club's most recent events, told along with a change to the club.
CLUB_EVENTS = 100

_STAMPS = frozenset({"created_at", "updated_at", "deleted_at"})
#: Columns of an event that change no page.
EVENT_QUIET = _STAMPS | frozenset({
    "page_views", "going_count",
    "admission_seq", "admission_prefix", "admission_digits", "admission_include_code",
    "ticket_template_id", "certificate_template_id",
    "door_mode", "close_after_minutes", "spot_registration_checks_in",
    "capture_contact_at_gate", "require_contact_at_gate",
    "reprint_policy", "auto_print_badges", "preprinted_carries",
    "self_check_in_enabled", "self_check_in_verify", "self_check_in_print", "kiosk_pin",
    "status_clock", "retention_mode", "retention_hours", "report_recipients", "keep_contacts",
    "qa_approve_first",
})
#: Columns of a club that change no page.
CLUB_QUIET = _STAMPS | frozenset({
    "follower_count", "rating", "total_ratings",
    "user_id", "initial_password", "requested_plan", "requested_cycle",
})
#: The collections on a row that its page shows.
SHOWN_RELATIONS = frozenset({"interests"})

_UNKNOWN = object()

_pending_events: set[str] = set()
_pending_clubs: set[str] = set()
_pending_topics = False
_timer: Optional[asyncio.TimerHandle] = None
_sending: set[asyncio.Future] = set()


def enabled() -> bool:
    return bool(settings.SITE_REVALIDATE_SECRET and settings.SITE_REVALIDATE_URL)


@functools.lru_cache(maxsize=1)
def _models():
    from app.api.agenda.models import AgendaItems
    from app.api.clubs.models import ClubInterestsLink, ClubPartners, Clubs, ClubSocials, ClubTeamMembers
    from app.api.event_partners.models import EventPartners
    from app.api.events.models import Events, EventSpeakers
    from app.api.organisers.models import EventTeamMembers

    return Events, Clubs, (EventSpeakers, AgendaItems, EventTeamMembers, EventPartners), (ClubTeamMembers, ClubPartners, ClubSocials, ClubInterestsLink)


# ─── What changed ─────────────────────────────────────────────────────────


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _flush_context) -> None:
    """Every flush of every session: note what a visitor would see differently,
    to be told once the transaction commits. Never raises into the flush."""
    if not enabled():
        return
    try:
        events, clubs = _changes(session)
    except Exception:  # noqa: BLE001
        logger.warning("could not work out which of the site's pages changed", exc_info=True)
        return
    if not events and not clubs:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    after_commit.run_after_commit(session, _queue, loop, frozenset(events), frozenset(clubs))


def _changes(session: Session) -> tuple[set[str], set[str]]:
    """(event slugs and ids, club slugs) whose pages this flush changed. In
    after_flush, `new`, `dirty`, `deleted` and every attribute's history still
    describe the flush, and new rows already have their ids."""
    Events, Clubs, event_rows, club_rows = _models()
    from app.api.cohosts.models import EventCohosts

    events: set[str] = set()
    clubs: set[str] = set()
    event_ids: set[int] = set()  # events reached through a row on their page
    club_ids: set[int] = set()   # clubs reached through one of their events or rows
    with_events: set[int] = set()  # clubs whose own row changed: their events show it

    for how, objects in (("new", session.new), ("dirty", session.dirty), ("deleted", session.deleted)):
        for obj in objects:
            if isinstance(obj, Events):
                if _shown(obj, how, EVENT_QUIET, ("lifecycle", "is_deleted"), _event_public):
                    events.update(_values(obj, "slug"))
                    events.update(str(v) for v in _values(obj, "id"))
                    club_ids.update(_values(obj, "club_id"))
            elif isinstance(obj, Clubs):
                if _shown(obj, how, CLUB_QUIET, ("is_verified", "is_deleted"), _club_public):
                    clubs.update(_values(obj, "slug"))
                    with_events.update(_values(obj, "id"))
            elif isinstance(obj, EventCohosts):
                # A club co-hosting an event, or no longer: the event's page
                # names its hosts, and the co-host's page lists it.
                if _row_changed(obj, how):
                    event_ids.update(_values(obj, "event_id"))
                    club_ids.update(_values(obj, "club_id"))
            elif isinstance(obj, event_rows):
                if _row_changed(obj, how):
                    event_ids.update(_values(obj, "event_id"))
            elif isinstance(obj, club_rows):
                if _row_changed(obj, how):
                    club_ids.update(_values(obj, "club_id"))

    if not (event_ids or club_ids or with_events):
        return events, clubs
    from app.api.closeout.models import EventStatus

    connection = session.connection()
    if event_ids:
        for id_, slug in connection.execute(select(Events.id, Events.slug).where(Events.id.in_(event_ids))):
            events.update((slug, str(id_)))
    if club_ids:
        clubs.update(slug for (slug,) in connection.execute(select(Clubs.slug).where(Clubs.id.in_(club_ids))))
    if with_events:
        rows = connection.execute(
            select(Events.id, Events.slug)
            .where(Events.club_id.in_(with_events), Events.is_deleted == False, Events.lifecycle != EventStatus.draft)  # noqa: E712
            .order_by(Events.event_datetime.desc(), Events.id.desc())
            .limit(CLUB_EVENTS)
        )
        for id_, slug in rows:
            events.update((slug, str(id_)))
    events.discard("")
    clubs.discard("")
    return events, clubs


def _values(obj, key) -> set:
    """Every value `key` held in this flush: the new one and, when it changed,
    the old (a renamed event's old slug is a page to drop too). Only what is
    loaded: nothing here may reach the database."""
    state = sa_inspect(obj)
    history = state.attrs[key].history
    out = {*history.added, *history.deleted, *history.unchanged}
    current = state.dict.get(key)
    if current is not None:
        out.add(current)
    return {v for v in out if isinstance(v, (str, int)) and not isinstance(v, bool)}


def _before(state, key):
    history = state.attrs[key].history
    if history.deleted:
        return history.deleted[0]
    if history.unchanged:
        return history.unchanged[0]
    if history.added:
        return _UNKNOWN  # set without the old value ever being read
    return state.dict.get(key, _UNKNOWN)


def _after(state, key):
    history = state.attrs[key].history
    if history.added:
        return history.added[0]
    if history.unchanged:
        return history.unchanged[0]
    return state.dict.get(key, _UNKNOWN)


def _event_public(lifecycle, is_deleted) -> bool:
    if lifecycle is _UNKNOWN or is_deleted is _UNKNOWN:
        return True  # cannot tell: better told once too often
    return getattr(lifecycle, "value", lifecycle) != "draft" and not is_deleted


def _club_public(is_verified, is_deleted) -> bool:
    if is_verified is _UNKNOWN or is_deleted is _UNKNOWN:
        return True
    return bool(is_verified) and not is_deleted


def _shown(obj, how: str, quiet: frozenset, flags: tuple[str, str], public) -> bool:
    """Whether an event's or club's change is one a visitor could see."""
    if how == "deleted":
        return True
    state = sa_inspect(obj)
    if how == "new":
        return public(*(_after(state, f) for f in flags))
    if not _changed_keys(state, quiet):
        return False
    return public(*(_before(state, f) for f in flags)) or public(*(_after(state, f) for f in flags))


def _changed_keys(state, quiet: frozenset) -> set[str]:
    keys = {a.key for a in state.mapper.column_attrs if a.key not in quiet and state.attrs[a.key].history.has_changes()}
    for rel in state.mapper.relationships:
        if rel.key in SHOWN_RELATIONS and state.attrs[rel.key].history.has_changes():
            keys.add(rel.key)
    return keys


def _row_changed(obj, how: str) -> bool:
    return how != "dirty" or bool(_changed_keys(sa_inspect(obj), _STAMPS))


def topics_changed(session) -> None:
    """The topic tree changed: once `session` commits, the site is told
    "topics": true with the next burst, so its topic pages and filters are
    made again. Never raises."""
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        after_commit.run_after_commit(session, _queue, loop, frozenset(), frozenset(), True)
    except Exception:  # noqa: BLE001
        logger.warning("could not queue the site's topics revalidation", exc_info=True)


# ─── Telling the site ─────────────────────────────────────────────────────


def _queue(loop, events: frozenset, clubs: frozenset, topics: bool = False) -> None:
    """After the commit (on a worker thread): over to the loop, to wait out
    the burst. A script with no loop of its own sends at once."""
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(_add, events, clubs, topics)
        return
    for payload in _payloads(events, clubs, topics):
        _send(payload)


def _add(events: frozenset, clubs: frozenset, topics: bool = False) -> None:
    global _timer, _pending_topics
    _pending_events.update(events)
    _pending_clubs.update(clubs)
    _pending_topics = _pending_topics or topics
    if _timer is None:
        _timer = asyncio.get_running_loop().call_later(DEBOUNCE_SECONDS, _fire)


def _fire() -> None:
    global _timer, _pending_topics
    if _timer is not None:
        _timer.cancel()
        _timer = None
    events, clubs, topics = set(_pending_events), set(_pending_clubs), _pending_topics
    _pending_events.clear()
    _pending_clubs.clear()
    _pending_topics = False
    if not events and not clubs and not topics:
        return
    _forget_discover_answers()
    loop = asyncio.get_running_loop()
    for payload in _payloads(events, clubs, topics):
        sent = loop.run_in_executor(None, _send, payload)
        _sending.add(sent)
        sent.add_done_callback(_sending.discard)


def _forget_discover_answers() -> None:
    """The front page and the sitemap are kept here for a minute
    (app.api.discover.cache); the site is about to ask for them again."""
    try:
        from app.api.discover import cache

        cache.shared.clear()
    except Exception:  # noqa: BLE001
        logger.warning("could not empty the discover cache", exc_info=True)


def _payloads(events, clubs, topics: bool = False) -> Iterator[dict]:
    """The POSTs: events and clubs MAX_PER_POST at a time, "topics" on the first."""
    events, clubs = sorted(events), sorted(clubs)
    for start in range(0, max(len(events), len(clubs), 1), MAX_PER_POST):
        payload = {"events": events[start:start + MAX_PER_POST], "clubs": clubs[start:start + MAX_PER_POST], "sitemap": True}
        if topics and start == 0:
            payload["topics"] = True
        yield payload


def _http() -> httpx.Client:
    """Swapped for a fake in tests."""
    return httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, headers={"User-Agent": "MyOtherApp-API/1.0 (+revalidate)"})


def _send(payload: dict) -> None:
    """One POST to the site. Never raises; never logs the secret."""
    url, secret = settings.SITE_REVALIDATE_URL, settings.SITE_REVALIDATE_SECRET
    if not (url and secret):
        return
    try:
        with _http() as http:
            response = http.post(url, json=payload, headers={"Authorization": f"Bearer {secret}"})
        if response.status_code >= 300:
            logger.warning("the site answered %s to a revalidation of %d events and %d clubs", response.status_code, len(payload["events"]), len(payload["clubs"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("the site could not be told to revalidate: %s", type(exc).__name__)


async def flush(*, after: float = 0.0) -> None:
    """Send what is waiting now, rather than when its few seconds are up, and
    wait for every send to finish. `after`: hold it that many seconds first.

    For tests, and for a script about to exit -- which is not the API's
    process, so cannot empty the API's minute of cached answers, and waits
    that minute out with `after` instead."""
    await after_commit.drain()
    await asyncio.sleep(0)  # the hand-over from the after-commit thread
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    if after and (_pending_events or _pending_clubs or _pending_topics):
        await asyncio.sleep(after)
    _fire()
    if _sending:
        await asyncio.gather(*list(_sending), return_exceptions=True)


def reset_for_tests() -> None:
    global _timer, _pending_topics
    if _timer is not None:
        _timer.cancel()
        _timer = None
    _pending_events.clear()
    _pending_clubs.clear()
    _pending_topics = False
