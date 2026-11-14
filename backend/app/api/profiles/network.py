"""The Network: the people a person saved, how and where they met.

Saving is one-way; a pair is mutual when both saved each other. `mutual` is
kept on both rows in the one transaction that saves or removes either,
under a transaction lock on the pair (lock_keys.NETWORK_PAIR): A and B
saving each other at the same moment take turns, and the second one finds
the first's row committed and marks both. Removing a save turns the other
side's `mutual` false the same way.

An encounter is where and when a save happened. It names the event only
when both people are checked in to it and `met_at` falls in its networking
window -- People Like You's rule (app/api/events/networking.py): from the
start to an hour after the end -- and only for a save by code, or a
`discovery` save (People Like You) of someone who shows there; a save by
handle never says whether someone was checked in. One encounter a pair per
event (a partial unique index; a second scan there is the same meeting);
without an event, one a pair per India day. A QR save's `met_at` (an
offline scan) is kept when it is at most 72 hours old; every other save is
made now.

Lists leave out anyone deleted, suspended, hidden by an admin, or blocked
either way; their rows stay, and show again if that changes.
"""
from __future__ import annotations

import zlib
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.models import EventRegistrationsLink, Events
from app.api.profiles import access, limits, rules, service
from app.api.profiles.models import NetworkEncounters, NetworkPeople
from app.api.users.models import UserProfiles, Users
from app.db import lock_keys
from app.response import CustomHTTPException

PAIR_LOCK = lock_keys.NETWORK_PAIR
IST = timezone(timedelta(hours=5, minutes=30))
#: An offline scan older than this is recorded as made now.
OFFLINE_WINDOW = timedelta(hours=72)
#: People Like You's window: from the start to an hour after the end.
AFTER_END = timedelta(hours=1)
LIST_ENCOUNTERS = 3
PAGE_SIZE_MAX = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _not_found() -> CustomHTTPException:
    return CustomHTTPException(404, message="That person isn't in your network.", error_code="NOT_FOUND")


def pair_key(a: int, b: int) -> int:
    """The pair's half of the lock key: a signed 32-bit hash of the two ids,
    whichever way round. A clash makes two pairs take turns, nothing more."""
    lo, hi = sorted((int(a), int(b)))
    v = zlib.crc32(f"{lo}:{hi}".encode())
    return v - (1 << 32) if v >= (1 << 31) else v


async def lock_pair(session, a: int, b: int) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:k, :p)"), {"k": PAIR_LOCK, "p": pair_key(a, b)})


def clamp_met_at(met_at: Optional[datetime], now: Optional[datetime] = None) -> datetime:
    """An offline scan's time when it is at most 72 hours old, else now."""
    now = now or _now()
    if met_at is None:
        return now
    if met_at.tzinfo is None:
        met_at = met_at.replace(tzinfo=timezone.utc)
    if met_at > now or met_at < now - OFFLINE_WINDOW:
        return now
    return met_at


def window_of(event: Events) -> Optional[tuple[datetime, datetime]]:
    if event.event_datetime is None or not event.duration:
        return None
    start = event.event_datetime
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start, start + timedelta(hours=float(event.duration)) + AFTER_END


async def event_for(session, event_id: Optional[int], a: int, b: int, met_at: datetime) -> Optional[Events]:
    """The event an encounter is at: only when both are checked in to it and
    `met_at` is in its networking window; otherwise none."""
    if event_id is None:
        return None
    event = await session.scalar(select(Events).where(Events.id == event_id, Events.is_deleted == False))  # noqa: E712
    if event is None:
        return None
    window = window_of(event)
    if window is None or not (window[0] <= met_at <= window[1]):
        return None
    R = EventRegistrationsLink
    checked = await session.scalar(
        select(func.count(func.distinct(R.user_id))).where(
            R.event_id == event.id, R.user_id.in_((a, b)), R.is_attended == True, R.is_deleted == False,  # noqa: E712
        )
    )
    return event if checked == 2 else None


async def _add_encounter(session, np_id: int, event: Optional[Events], method: str, met_at: datetime, created: bool) -> bool:
    E = NetworkEncounters
    if event is not None:
        got = await session.scalar(
            pg_insert(E).values(network_person_id=np_id, event_id=event.id, method=method, met_at=met_at)
            .on_conflict_do_nothing(index_elements=["network_person_id", "event_id"], index_where=E.event_id.isnot(None))
            .returning(E.id)
        )
        return got is not None
    if not created:
        day = met_at.astimezone(IST).date()
        start = datetime.combine(day, time(0), IST)
        same_day = await session.scalar(select(exists().where(
            E.network_person_id == np_id, E.event_id.is_(None), E.met_at >= start, E.met_at < start + timedelta(days=1),
        )))
        if same_day:
            return False
    session.add(E(network_person_id=np_id, event_id=None, method=method, met_at=met_at))
    await session.flush()
    return True


async def _set_mutual(session, a: int, b: int, value: bool) -> None:
    NP = NetworkPeople
    await session.execute(
        update(NP).where(or_(and_(NP.owner_id == a, NP.person_id == b), and_(NP.owner_id == b, NP.person_id == a)))
        .values(mutual=value).execution_options(synchronize_session=False)
    )


async def save(session, viewer: Users, body) -> dict:
    """POST /network: save a person by their code or handle."""
    from app.api.profiles import notices, recap

    limits.save(viewer.id)
    if body.code:
        target, _ = await service.resolve_code(session, body.code, viewer.id)
    else:
        target = await access.visible_person(session, viewer.id, handle=rules.normal_handle(body.handle))
        if body.method == "qr" and not access.settings_of(target.profile)["scanning_enabled"]:
            raise CustomHTTPException(403, message="This person isn't taking saves by QR code.", error_code="PROFILE_SCANNING_OFF")
    if target.id == viewer.id:
        raise CustomHTTPException(422, message="That's you.", error_code="CANNOT_SAVE_SELF")

    # An offline scan's own time is kept for a QR save only: a save by
    # handle is made now (no claiming a meeting after the fact).
    met_at = clamp_met_at(body.met_at if body.method == "qr" else None)
    # The event is named only on a save by code (they were in front of each
    # other), or from People Like You for someone who shows there: a save by
    # handle must not tell whether a person who hid was checked in.
    event = None
    if body.code or (body.method == "discovery" and target.networking_visible):
        event = await event_for(session, body.event_id, viewer.id, target.id, met_at)

    NP = NetworkPeople
    await lock_pair(session, viewer.id, target.id)
    np_id = await session.scalar(
        pg_insert(NP).values(owner_id=viewer.id, person_id=target.id, tags=[], mutual=False)
        .on_conflict_do_nothing(constraint="uq_network_people_owner_person")
        .returning(NP.id)
    )
    created = np_id is not None
    if not created:
        np_id = await session.scalar(select(NP.id).where(NP.owner_id == viewer.id, NP.person_id == target.id))
    reverse = await session.scalar(select(NP.id).where(NP.owner_id == target.id, NP.person_id == viewer.id))
    if reverse is not None:
        await _set_mutual(session, viewer.id, target.id, True)
    new_encounter = await _add_encounter(session, np_id, event, body.method, met_at, created)
    if new_encounter and not created:
        await session.execute(update(NP).where(NP.id == np_id).values(updated_at=_now()).execution_options(synchronize_session=False))
    if created and access.settings_of(target.profile)["notify_saves"]:
        await notices.note_save(session, person_id=target.id, saver_id=viewer.id, event_id=event.id if event is not None else None)
    if new_encounter and event is not None:
        await recap.schedule(session, event)
    await session.commit()

    item = (await people_out(session, viewer.id, [np_id], all_encounters=False))[0]
    rel = await access.relation(session, viewer.id, target.id)
    return {"person": item, "created": created, "new_encounter": new_encounter, "profile": await service.public_profile(session, target, rel)}


async def remove(session, viewer: Users, np_id: int) -> None:
    """DELETE /network/{id}: silent to the other person, whose `mutual` turns false."""
    NP = NetworkPeople
    person_id = await session.scalar(select(NP.person_id).where(NP.id == np_id, NP.owner_id == viewer.id))
    if person_id is None:
        raise _not_found()
    await lock_pair(session, viewer.id, person_id)
    gone = await session.scalar(select(NP.id).where(NP.id == np_id, NP.owner_id == viewer.id).with_for_update())
    if gone is not None:
        await session.execute(NP.__table__.delete().where(NP.id == np_id))
        await _set_mutual(session, viewer.id, person_id, False)
    await session.commit()


# ─── Reading ────────────────────────────────────────────────────────────


def event_brief(event: Optional[Events]) -> Optional[dict]:
    if event is None:
        return None
    return {"id": event.id, "slug": event.slug, "name": event.name, "starts_at": event.event_datetime}


async def _events(session, ids) -> dict[int, Events]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {e.id: e for e in (await session.scalars(select(Events).where(Events.id.in_(ids), Events.is_deleted == False))).all()}  # noqa: E712


async def people_out(session, viewer_id: int, np_ids: list[int], *, all_encounters: bool) -> list[dict]:
    """NetworkPerson for each of the viewer's rows, in the order given."""
    NP, E = NetworkPeople, NetworkEncounters
    if not np_ids:
        return []
    rows = {r.id: r for r in (await session.scalars(
        select(NP).where(NP.id.in_(np_ids), NP.owner_id == viewer_id).execution_options(populate_existing=True)
    )).all()}
    span = {
        i: (first, last) for i, first, last in (await session.execute(
            select(E.network_person_id, func.min(E.met_at), func.max(E.met_at)).where(E.network_person_id.in_(rows)).group_by(E.network_person_id)
        )).all()
    }
    q = select(E).where(E.network_person_id.in_(rows))
    if not all_encounters:
        ranked = select(E.id, func.row_number().over(partition_by=E.network_person_id, order_by=(E.met_at.desc(), E.id.desc())).label("n")).where(
            E.network_person_id.in_(rows)
        ).subquery()
        q = q.where(E.id.in_(select(ranked.c.id).where(ranked.c.n <= LIST_ENCOUNTERS)))
    encounters: dict[int, list] = {}
    for e in (await session.scalars(q.order_by(E.met_at.desc(), E.id.desc()))).all():
        encounters.setdefault(e.network_person_id, []).append(e)
    events = await _events(session, {e.event_id for es in encounters.values() for e in es})
    persons = await service.people(session, [r.person_id for r in rows.values()])
    rels = await access.relations(session, viewer_id, [r.person_id for r in rows.values()])
    out = []
    for i in np_ids:
        r = rows.get(i)
        user = persons.get(r.person_id) if r is not None else None
        if r is None or user is None:
            continue
        first, last = span.get(r.id, (None, None))
        out.append({
            "id": r.id, "person": service.brief(user, rels[r.person_id]), "mutual": bool(r.mutual),
            # My row's other side: they saved me too. The same as mutual, here.
            "saved_me": rels[r.person_id].saved_me, "note": r.note, "tags": list(r.tags or []),
            "first_met_at": first, "last_met_at": last,
            "encounters": [
                {"event": event_brief(events.get(e.event_id)), "method": e.method, "met_at": e.met_at}
                for e in encounters.get(r.id, [])
            ],
        })
    return out


def _mine(viewer_id: int):
    NP = NetworkPeople
    return (NP.owner_id == viewer_id, *access.shown_person_where(NP.person_id, viewer_id))


async def list_people(session, viewer_id: int, *, q=None, event_id=None, tag=None, mutual=None, sort="recent", order=None, page=1, page_size=20) -> dict:
    NP, E = NetworkPeople, NetworkEncounters
    page_size = max(1, min(int(page_size), PAGE_SIZE_MAX))
    page = max(1, int(page))
    name = func.lower(func.coalesce(UserProfiles.full_name, Users.full_name))
    last_met = select(func.max(E.met_at)).where(E.network_person_id == NP.id).correlate(NP).scalar_subquery()
    base = (
        select(NP.id)
        .join(Users, Users.id == NP.person_id)
        .outerjoin(UserProfiles, and_(UserProfiles.user_id == Users.id, UserProfiles.is_deleted == False))  # noqa: E712
        .where(*_mine(viewer_id))
    )
    if q and q.strip():
        pattern = rules.like_pattern(q.strip()[:100])
        base = base.where(or_(
            Users.full_name.ilike(pattern, escape="\\"), Users.username.ilike(pattern, escape="\\"),
            UserProfiles.full_name.ilike(pattern, escape="\\"), UserProfiles.headline.ilike(pattern, escape="\\"),
            NP.note.ilike(pattern, escape="\\"),
        ))
    if event_id is not None:
        base = base.where(exists().where(E.network_person_id == NP.id, E.event_id == event_id))
    if tag:
        base = base.where(NP.tags.contains([tag]))
    if mutual is not None:
        base = base.where(NP.mutual == mutual)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    descending = (order or ("asc" if sort == "name" else "desc")) == "desc"
    key = {"name": name, "met": last_met}.get(sort, NP.created_at)
    key = key.desc().nulls_last() if descending else key.asc().nulls_last()
    tie = NP.id.desc() if descending else NP.id.asc()
    ids = list((await session.scalars(base.order_by(key, tie).offset((page - 1) * page_size).limit(page_size))).all())
    items = await people_out(session, viewer_id, ids, all_encounters=False)

    # The filters' choices, over the whole network.
    per_event = (await session.execute(
        select(E.event_id, func.count(func.distinct(NP.id)))
        .join(NP, NP.id == E.network_person_id)
        .where(E.event_id.isnot(None), *_mine(viewer_id))
        .group_by(E.event_id)
    )).all()
    events = await _events(session, [e for e, _ in per_event])
    event_counts = sorted(
        ({"event": event_brief(events[e]), "count": int(n)} for e, n in per_event if e in events),
        key=lambda x: (x["event"]["starts_at"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True,
    )
    tag_col = func.jsonb_array_elements_text(NP.tags).table_valued("value").alias("t")
    tag_rows = (await session.execute(
        select(tag_col.c.value, func.count()).select_from(NP).join(tag_col, true()).where(*_mine(viewer_id))
        .group_by(tag_col.c.value).order_by(func.count().desc(), tag_col.c.value).limit(100)
    )).all()
    return {"items": items, "total": total, "events": event_counts, "tags": [{"tag": t, "count": int(n)} for t, n in tag_rows]}


async def get_person(session, viewer_id: int, np_id: int) -> dict:
    NP = NetworkPeople
    found = await session.scalar(select(NP.id).where(NP.id == np_id, *_mine(viewer_id)))
    if found is None:
        raise _not_found()
    return (await people_out(session, viewer_id, [np_id], all_encounters=True))[0]


async def edit_person(session, viewer_id: int, np_id: int, body) -> dict:
    NP = NetworkPeople
    row = await session.scalar(select(NP).where(NP.id == np_id, *_mine(viewer_id)))
    if row is None:
        raise _not_found()
    sent = body.model_fields_set
    if "note" in sent:
        row.note = rules.paragraph(body.note, 500)
    if "tags" in sent:
        row.tags = list(body.tags or [])
    row.updated_at = _now()
    await session.commit()
    return (await people_out(session, viewer_id, [np_id], all_encounters=True))[0]


async def saved_me(session, viewer_id: int, *, event_id=None, page=1, page_size=20) -> dict:
    """People who saved me that I haven't saved, newest first."""
    NP, E = NetworkPeople, NetworkEncounters
    page_size = max(1, min(int(page_size), PAGE_SIZE_MAX))
    page = max(1, int(page))
    mine = NP.__table__.alias("mine")
    base = select(NP.id).where(
        NP.person_id == viewer_id,
        *access.shown_person_where(NP.owner_id, viewer_id),
        ~exists().where(mine.c.owner_id == viewer_id, mine.c.person_id == NP.owner_id),
    )
    if event_id is not None:
        base = base.where(exists().where(E.network_person_id == NP.id, E.event_id == event_id))
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    ids = list((await session.scalars(base.order_by(NP.created_at.desc(), NP.id.desc()).offset((page - 1) * page_size).limit(page_size))).all())
    if not ids:
        return {"items": [], "total": total}
    rows = {r.id: r for r in (await session.scalars(select(NP).where(NP.id.in_(ids)))).all()}
    # Each saver's event: the one asked for, else their latest at an event.
    latest: dict[int, int] = {}
    eq = select(E.network_person_id, E.event_id).where(E.network_person_id.in_(ids), E.event_id.isnot(None))
    if event_id is not None:
        eq = eq.where(E.event_id == event_id)
    for np_id, ev in (await session.execute(eq.order_by(E.met_at.asc()))).all():
        latest[np_id] = ev
    events = await _events(session, latest.values())
    savers = await service.people(session, [r.owner_id for r in rows.values()])
    rels = await access.relations(session, viewer_id, [r.owner_id for r in rows.values()])
    items = []
    for i in ids:
        r = rows[i]
        user = savers.get(r.owner_id)
        if user is None:
            continue
        items.append({
            "person": service.brief(user, rels[r.owner_id]), "saved_at": r.created_at,
            "event": event_brief(events.get(latest.get(i))),
        })
    return {"items": items, "total": total}


async def everyone(session, viewer_id: int) -> list[dict]:
    """Every person in the viewer's network for the export: their row, the
    person, what this viewer may see of them (links, address, number)."""
    from app.api.profiles.models import ProfileLinks

    NP = NetworkPeople
    ids = list((await session.scalars(select(NP.id).where(*_mine(viewer_id)).order_by(NP.created_at.desc(), NP.id.desc()))).all())
    out: list[dict] = []
    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        items = await people_out(session, viewer_id, chunk, all_encounters=True)
        persons = await service.people(session, [i["person"]["user_id"] for i in items])
        rels = await access.relations(session, viewer_id, persons)
        links: dict[int, list] = {}
        for link in (await session.scalars(
            select(ProfileLinks).where(ProfileLinks.user_id.in_(persons)).order_by(ProfileLinks.user_id, ProfileLinks.position, ProfileLinks.id)
        )).all():
            links.setdefault(link.user_id, []).append(link)
        for item in items:
            uid = item["person"]["user_id"]
            user, rel = persons[uid], rels[uid]
            settings = access.settings_of(user.profile)
            open_ = not access.minimal(settings, rel)
            out.append({
                **item,
                "profile_url": service.profile_url(user.username),
                "links": [l.url for l in links.get(uid, [])] if open_ and rel.sees(settings["visibility"]["links"]) else [],
                "email": user.email if open_ and rel.shares(settings["share_email"]) else None,
                "phone": user.phone if open_ and rel.shares(settings["share_phone"]) else None,
            })
    return out
