"""The documents Meilisearch holds, made from the rows, a batch at a time.

An event is in the events index unless it is deleted or a draft (a deleted
or unpublished event's document is removed); a club is in the clubs index
unless it is deleted. Whether the public may see it is kept as filters --
`listed` (the club's, which clubs.listed keeps: verified, not deleted, its
organisation not blocked), `lifecycle`, `ends_at` -- and asked for in every
query, as the Postgres path asks for them in SQL. So a club's organisation
being blocked, or an event ending, never has to rewrite a document on time.

The filters mirror the SQL they stand for (app.api.discover.service):

- interest_ids: every interest the row picked through a live link, as
  `picks(interest=…)` (which does not ask whether the interest is deleted);
- topic_ids: the topics of those of them not deleted, as `picks(topic=…)`;
- is_online / has_fee: false when null, as coalesce(…, false);
- has_point / _geo: has_point is "lat and lng are set" (an event without one
  is never inside a radius); _geo only for an event held in person
  with a point, so an online event sorts with the unplaced, as km is None;
- starts_at, ends_at, created_at: microseconds since 1970, exact (whole
  numbers below 2^53, so a filter compares them as Postgres does).

`h`, a hash of everything else, lets the reconcile compare a document with
its row without reading it whole (sync.reconcile).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from sqlalchemy import text

#: Changes when the documents or the indexes' settings change shape: an
#: index built for another version is built again (sync.ensure_built).
DOC_VERSION = 1

_ENDS = "timezone('UTC', timezone('UTC', e.event_datetime) + e.duration * INTERVAL '1 hour')"

_EVENTS_SQL = f"""
SELECT e.id, e.name, e.location_name, e.event_tag, e.status::text AS lifecycle, e.is_deleted,
       coalesce(e.is_online, false) AS is_online, coalesce(e.has_fee, false) AS has_fee, e.reg_fee,
       e.lat, e.lng, e.popularity, e.club_id, e.category_id,
       (extract(epoch FROM e.event_datetime) * 1000000)::bigint AS starts_at,
       (extract(epoch FROM {_ENDS}) * 1000000)::bigint AS ends_at,
       (extract(epoch FROM e.created_at) * 1000000)::bigint AS created_at,
       c.name AS club_name, c.location_name AS club_location, coalesce(c.listed, false) AS listed,
       k.name AS category
FROM events e
LEFT JOIN clubs c ON c.id = e.club_id
LEFT JOIN event_categories k ON k.id = e.category_id AND NOT k.is_deleted
WHERE e.id = ANY(:ids)
"""

_CLUBS_SQL = """
SELECT c.id, c.name, c.tagline, c.location_name, c.is_deleted, c.listed, c.follower_count,
       c.place_lat, c.place_lng, (extract(epoch FROM c.created_at) * 1000000)::bigint AS created_at
FROM clubs c
WHERE c.id = ANY(:ids)
"""

# Each row's live interest links: the interest (and whether it is deleted), its topic (and whether that is).
_LINKS_SQL = """
SELECT l.{owner} AS owner, l.interest_id, i.name, i.is_deleted, i.category_id, t.name AS topic, t.is_deleted AS topic_deleted
FROM {table} l
JOIN interests i ON i.id = l.interest_id
LEFT JOIN interest_categories t ON t.id = i.category_id
WHERE l.{owner} = ANY(:ids) AND NOT l.is_deleted
ORDER BY l.id
"""


async def _links(session, table: str, owner: str, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {i: dict(interest_ids=[], topic_ids=[], interests=[], topics=[]) for i in ids}
    rows = await session.execute(text(_LINKS_SQL.format(table=table, owner=owner)), {"ids": ids})
    for owner_id, interest_id, name, deleted, topic_id, topic, topic_deleted in rows.all():
        d = out[owner_id]
        if interest_id not in d["interest_ids"]:
            d["interest_ids"].append(interest_id)
        if deleted:
            continue
        if topic_id is not None and topic_id not in d["topic_ids"]:
            d["topic_ids"].append(topic_id)
        if name and name not in d["interests"]:
            d["interests"].append(name)
        if topic and not topic_deleted and topic not in d["topics"]:
            d["topics"].append(topic)
    for d in out.values():
        d["interest_ids"].sort()
        d["topic_ids"].sort()
        d["interests"].sort()
        d["topics"].sort()
    return out


#: Old names of cities, as people still write them, to the name used today.
_CITY_NAMES = {
    "bangalore": "bengaluru", "trivandrum": "thiruvananthapuram", "bombay": "mumbai", "madras": "chennai",
    "calcutta": "kolkata", "cochin": "kochi", "calicut": "kozhikode", "mysore": "mysuru", "gurgaon": "gurugram",
    "poona": "pune", "baroda": "vadodara", "vizag": "visakhapatnam", "trichy": "tiruchirappalli",
    "mangalore": "mangaluru", "hubli": "hubballi", "belgaum": "belagavi", "pondicherry": "puducherry",
    "new delhi": "delhi", "trichur": "thrissur", "quilon": "kollam", "alleppey": "alappuzha",
}
_PIN = re.compile(r"[\d\s-]+$")
#: Words a venue field holds that are no city.
_NOT_PLACES = {"online", "virtual", "zoom", "google meet", "tbd", "tba", "india"}


def city_of(*places: Optional[str]) -> Optional[str]:
    """A best guess at the city a place's words name: the last part after a
    comma, without a PIN code, lowercased, its old name made today's ("Lab
    3, CUSAT, Cochin 682022" is "kochi"). The first place that gives one.
    A filter for later; no list narrows by it yet."""
    for place in places:
        if not place:
            continue
        parts = [p.strip() for p in place.split(",") if p.strip()]
        while parts:
            last = _PIN.sub("", parts[-1]).strip().lower()
            if last and not last.isdigit() and last not in _NOT_PLACES:
                return _CITY_NAMES.get(last, last)
            parts.pop()
    return None


def doc_hash(doc: dict) -> str:
    raw = json.dumps({k: v for k, v in doc.items() if k != "h"}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


async def event_docs(session, ids: list[int]) -> tuple[list[dict], list[int]]:
    """(the documents to add or replace, the ids whose documents must go) for these events."""
    ids = sorted(set(ids))
    if not ids:
        return [], []
    rows = {r.id: r for r in (await session.execute(text(_EVENTS_SQL), {"ids": ids})).all()}
    keep = [i for i in ids if i in rows and not rows[i].is_deleted and rows[i].lifecycle != "draft"]
    links = await _links(session, "event_interests_link", "event_id", keep)
    docs = []
    for i in keep:
        r = rows[i]
        has_point = r.lat is not None and r.lng is not None
        doc = dict(
            id=r.id, name=r.name, location=r.location_name, tags=[r.event_tag] if r.event_tag else [],
            club_name=r.club_name, club_location=r.club_location, category=r.category,
            interests=links[i]["interests"], topics=links[i]["topics"],
            club_id=r.club_id, category_id=r.category_id, topic_ids=links[i]["topic_ids"], interest_ids=links[i]["interest_ids"],
            city=city_of(r.location_name, r.club_location), is_online=bool(r.is_online), has_fee=bool(r.has_fee),
            fee=float(r.reg_fee) if r.has_fee and r.reg_fee is not None else None,
            has_point=has_point, starts_at=int(r.starts_at), ends_at=int(r.ends_at),
            created_at=int(r.created_at) if r.created_at is not None else 0,
            lifecycle=r.lifecycle, listed=bool(r.listed), popularity=float(r.popularity or 0.0),
        )
        if has_point and not r.is_online:
            doc["_geo"] = {"lat": float(r.lat), "lng": float(r.lng)}
        doc["h"] = doc_hash(doc)
        docs.append(doc)
    return docs, [i for i in ids if i not in set(keep)]


async def club_docs(session, ids: list[int]) -> tuple[list[dict], list[int]]:
    """(the documents to add or replace, the ids whose documents must go) for these clubs."""
    ids = sorted(set(ids))
    if not ids:
        return [], []
    rows = {r.id: r for r in (await session.execute(text(_CLUBS_SQL), {"ids": ids})).all()}
    keep = [i for i in ids if i in rows and not rows[i].is_deleted]
    links = await _links(session, "clubs_interests_link", "club_id", keep)
    docs = []
    for i in keep:
        r = rows[i]
        placed = r.place_lat is not None and r.place_lng is not None
        doc = dict(
            id=r.id, name=r.name, tagline=r.tagline, location=r.location_name,
            interests=links[i]["interests"], topics=links[i]["topics"],
            topic_ids=links[i]["topic_ids"], interest_ids=links[i]["interest_ids"],
            city=city_of(r.location_name), listed=bool(r.listed), follower_count=int(r.follower_count or 0),
            name_sort=(r.name or "").lower(), created_at=int(r.created_at) if r.created_at is not None else 0,
            has_place=placed,
        )
        if placed:
            doc["_geo"] = {"lat": float(r.place_lat), "lng": float(r.place_lng)}
        doc["h"] = doc_hash(doc)
        docs.append(doc)
    return docs, [i for i in ids if i not in set(keep)]


DOCS = {"events": event_docs, "clubs": club_docs}
#: Which rows can have a document, for walking every one (a rebuild, the reconcile).
CANDIDATES = {
    "events": "SELECT id FROM events WHERE id > :last AND NOT is_deleted AND status <> 'draft' ORDER BY id LIMIT :n",
    "clubs": "SELECT id FROM clubs WHERE id > :last AND NOT is_deleted ORDER BY id LIMIT :n",
}
