"""Topics as everything else shows them.

A topic is an interest category (InterestCategory); people, clubs and events
pick interests, and their topics follow from those. Here:

- `tree`: every live topic with its live interests, in order (topic
  sort_order, then interest sort_order, then name). /interests/list and the
  admin's editor read it; a topic with no interests is in it too.
- `with_counts`: the tree with what is on in each topic and interest and how
  many clubs pick it, for the explore pages and the discover facet. The same
  for everyone, so worked out once every TTL seconds in this process
  (discover.cache) and dropped at once when an admin changes the tree
  (`forget`). Each worker keeps its own copy, so another worker may answer
  with the old tree for up to TTL after a change.
- The topics of an event or a club for its payload: `topics_of`, from its
  interests loaded with their category. (Discover's cards read theirs for a
  whole page in the statement that reads its going counts: card_extras.)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, inspect as sa_inspect, select

from app.api.discover import cache
from app.api.interests.models import InterestCategory, Interests

#: How long the counts are kept, in seconds; /interests/topics says the same
#: to browsers and the site (Cache-Control).
TTL = 300.0
KEY = ("topics",)


def _enum(value):
    return getattr(value, "value", value)


def _topic_order(c) -> tuple:
    return (c.sort_order or 0, (c.name or "").lower(), c.id)


def topic_ref(c: InterestCategory) -> dict:
    """A topic on an event or club: enough to show and link a chip."""
    return dict(id=c.id, slug=c.slug, name=c.name, color=c.color)


def interest_out(i: Interests) -> dict:
    """An interest inside a payload: as it always was, with its topic and slug."""
    return dict(id=i.id, name=i.name, icon=i.icon, icon_type=_enum(i.icon_type), category_id=i.category_id, slug=i.slug)


def topics_of(interests: Iterable[Interests]) -> list[dict]:
    """The distinct topics of these interests, in topic order, from each
    one's `category` as it was loaded with it (never loaded here: an async
    session cannot load lazily). A deleted interest or topic is left out."""
    seen: dict[int, InterestCategory] = {}
    for i in interests or ():
        if "category" in sa_inspect(i).unloaded:
            continue
        c = i.category
        if c is None or i.is_deleted or c.is_deleted:
            continue
        seen.setdefault(c.id, c)
    return [topic_ref(c) for c in sorted(seen.values(), key=_topic_order)]


def loaded_topics(owner) -> list[dict]:
    """An event's or club's topics from its interests, when they were loaded
    with their categories (`selectinload(X.interests).joinedload(Interests.category)`);
    [] when they were not. Events.topics and Clubs.topics, which the response
    schemas read."""
    if "interests" in sa_inspect(owner).unloaded:
        return []
    return topics_of(owner.interests)


async def tree(session) -> list[dict]:
    """Every live topic and its live interests, in order: /interests/list's shape."""
    cats = (await session.scalars(
        select(InterestCategory)
        .where(InterestCategory.is_deleted == False)  # noqa: E712
        .order_by(InterestCategory.sort_order, func.lower(InterestCategory.name), InterestCategory.id)
    )).all()
    ints = (await session.scalars(
        select(Interests)
        .where(Interests.is_deleted == False)  # noqa: E712
        .order_by(Interests.sort_order, func.lower(Interests.name), Interests.id)
    )).all()
    under: dict[int, list[dict]] = {c.id: [] for c in cats}
    for i in ints:
        if i.category_id in under:
            under[i.category_id].append(dict(
                id=i.id, slug=i.slug, name=i.name, icon=i.icon, icon_type=_enum(i.icon_type),
                category_id=i.category_id, sort_order=i.sort_order,
            ))
    return [
        dict(
            id=c.id, slug=c.slug, name=c.name, icon=c.icon, icon_type=_enum(c.icon_type),
            color=c.color, description=c.description, sort_order=c.sort_order, interests=under[c.id],
        )
        for c in cats
    ]


async def _with_counts(session, now: datetime) -> list[dict]:
    from app.api.discover import service as discover

    topics = await tree(session)
    counts = await discover.topic_counts(session, now)
    return [
        dict(
            id=t["id"], slug=t["slug"], name=t["name"], icon=t["icon"], icon_type=t["icon_type"],
            color=t["color"], description=t["description"],
            upcoming_events=counts["topic_events"].get(t["id"], 0), clubs=counts["topic_clubs"].get(t["id"], 0),
            interests=[
                dict(id=i["id"], slug=i["slug"], name=i["name"], upcoming_events=counts["interest_events"].get(i["id"], 0))
                for i in t["interests"]
            ],
        )
        for t in topics
    ]


async def with_counts(session, now: Optional[datetime] = None) -> list[dict]:
    """Every live topic, in order, with its upcoming events (as discover shows
    them), the clubs that pick it and each interest's upcoming events. Kept
    for TTL seconds; copies, so a caller can change what it is given."""
    now = now or datetime.now(timezone.utc)
    got = await cache.kept(cache.shared, KEY, _with_counts, session, now, ttl=TTL)
    return [dict(t, interests=[dict(i) for i in t["interests"]]) for t in got]


def find(topics: list[dict], ref: str) -> Optional[dict]:
    """The topic a slug, or an id, names."""
    ref = (ref or "").strip()
    for t in topics:
        if t["slug"] == ref or (ref.isdigit() and t["id"] == int(ref)):
            return t
    return None


async def facet(session, now: datetime) -> list[dict]:
    """Discover's topic facet: every topic, in order, with its upcoming events."""
    return [
        dict(id=t["id"], slug=t["slug"], name=t["name"], color=t["color"], upcoming=t["upcoming_events"])
        for t in await with_counts(session, now)
    ]


def forget() -> None:
    """The tree changed: the next read works the counts out afresh (in this process)."""
    cache.shared.forget(KEY)
