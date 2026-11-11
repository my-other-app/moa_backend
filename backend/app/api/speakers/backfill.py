"""The backfill (`speakers.backfill`): every event speaker row joins a
profile, once, after the deploy. It changes nothing any page shows -- each
event keeps its own copy of the speaker -- and it can be run again: a row
linked already is left alone, a pair in the merge queue is not added twice,
an agenda session with speakers is not looked at.

The same person, linked to one profile (in event order, oldest first):

1. the same link (website, LinkedIn...) under a name that is plainly theirs
   ("Meera" and "Meera Nair"); the same link under another name goes to the
   merge queue instead (`same_link_other_name`);
2. the same name (speakers.names.name_key) at the same club;
3. the same name and the same photo (a photo key is one upload).

Anything else is a new profile. Afterwards, profiles with the same name at
different clubs go to the merge queue (`same_name_other_club`) for an admin
to decide. Agenda sessions' free-text speaker is matched by name to that
event's speakers (the whole text, else each name in "A, B & C"); a name that
matches nobody stays text only. Profiles without topics take their events'.

Run it once after the deploy (docs/SPEAKERS.md): POST
/api/v1/admin/speakers/backfill, or `python -m scripts.speakers_backfill
--apply` on the server.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import func, select, update

from app.api.agenda.models import AgendaItems
from app.api.events.models import Events, EventInterestsLink, EventSpeakers
from app.api.speakers import names
from app.api.speakers import service as sp
from app.api.speakers.models import MERGED, AgendaItemSpeakers, SpeakerProfiles
from app.core import jobs

logger = logging.getLogger(__name__)

JOB = "speakers.backfill"
BATCH = 200
#: Profiles with one name compared pairwise for the queue, at most.
GROUP_MAX = 25


def _raw(column):
    """A column's stored text, not what its type makes of it (an image's key,
    not its links). Written out: a cast or type_coerce carries the image
    type into the statement's cache key, and its dict of sizes cannot be hashed."""
    return sa.literal_column(f"{column.table.name}.{column.name}", sa.Text())


async def _index(session):
    """What is linked already: by (club, name), by photo, by link."""
    by_club_name: dict = {}
    by_photo: dict = {}
    by_link: dict = defaultdict(list)
    rows = (await session.execute(
        select(EventSpeakers.speaker_profile_id, EventSpeakers.name, EventSpeakers.link, _raw(EventSpeakers.__table__.c.photo), Events.club_id)
        .join(Events, Events.id == EventSpeakers.event_id)
        .join(SpeakerProfiles, SpeakerProfiles.id == EventSpeakers.speaker_profile_id)
        .where(EventSpeakers.speaker_profile_id.isnot(None), SpeakerProfiles.status != MERGED)
        .execution_options(include_deleted=True)
    )).all()
    for pid, name, link, photo, club_id in rows:
        key = names.name_key(name)
        by_club_name.setdefault((club_id, key), pid)
        if photo:
            by_photo.setdefault(photo, (pid, key))
        lk = names.link_key(link)
        if lk and (pid, key) not in by_link[lk]:
            by_link[lk].append((pid, key))
    for pid, name_key, links, photo, club_id in (await session.execute(
        select(SpeakerProfiles.id, SpeakerProfiles.name_key, SpeakerProfiles.links, _raw(SpeakerProfiles.__table__.c.photo), SpeakerProfiles.created_by_club_id)
        .where(SpeakerProfiles.status != MERGED)
    )).all():
        if club_id is not None:
            by_club_name.setdefault((club_id, name_key), pid)
        if photo:
            by_photo.setdefault(photo, (pid, name_key))
        for lk in names.profile_link_keys(links):
            if (pid, name_key) not in by_link[lk]:
                by_link[lk].append((pid, name_key))
    return by_club_name, by_photo, by_link


async def link_rows(session, counts: dict, *, commit: bool) -> None:
    by_club_name, by_photo, by_link = await _index(session)
    last_id = 0
    while True:
        rows = (await session.execute(
            select(
                EventSpeakers.id, EventSpeakers.name, EventSpeakers.designation, EventSpeakers.bio, EventSpeakers.link,
                _raw(EventSpeakers.__table__.c.photo).label("photo"), EventSpeakers.event_id, Events.club_id,
            )
            .join(Events, Events.id == EventSpeakers.event_id)
            .where(
                EventSpeakers.speaker_profile_id.is_(None), EventSpeakers.is_deleted == False,  # noqa: E712
                Events.is_deleted == False, EventSpeakers.id > last_id,  # noqa: E712
            )
            .order_by(EventSpeakers.id).limit(BATCH)
        )).all()
        if not rows:
            return
        for r in rows:
            last_id = r.id
            key = names.name_key(r.name)
            lk = names.link_key(r.link)
            pid: Optional[int] = None
            how = None
            for cand_pid, cand_key in by_link.get(lk, []) if lk else []:
                if names.names_alike(key, cand_key):
                    pid, how = cand_pid, "by_link"
                    break
            if pid is None and (r.club_id, key) in by_club_name:
                pid, how = by_club_name[(r.club_id, key)], "by_club_name"
            if pid is None and r.photo and r.photo in by_photo and by_photo[r.photo][1] == key:
                pid, how = by_photo[r.photo][0], "by_photo"
            if pid is None:
                profile = await sp.create_profile(
                    session, name=r.name, headline=r.designation, bio=r.bio, photo=r.photo or None, links=names.links_from(r.link),
                    club_id=r.club_id, actor=sp.SYSTEM, source={"event_id": r.event_id, "event_speaker_id": r.id, "via": "backfill"},
                )
                pid, how = profile.id, "profiles_made"
                key = profile.name_key
            counts[how] += 1
            await session.execute(
                update(EventSpeakers).where(EventSpeakers.id == r.id).values(speaker_profile_id=pid)
                .execution_options(synchronize_session=False)
            )
            by_club_name.setdefault((r.club_id, key), pid)
            if r.photo:
                by_photo.setdefault(r.photo, (pid, key))
            if lk:
                for cand_pid, cand_key in by_link[lk]:
                    if cand_pid != pid and not names.names_alike(key, cand_key):
                        counts["candidates"] += int(await sp.add_candidate(session, pid, cand_pid, sp.SAME_LINK_OTHER_NAME, 0.6))
                if (pid, key) not in by_link[lk]:
                    by_link[lk].append((pid, key))
        if commit:
            await session.commit()
        else:
            await session.flush()


async def same_name_candidates(session, counts: dict) -> None:
    """Profiles sharing a name at different clubs: the queue's pairs."""
    keys = (await session.scalars(
        select(SpeakerProfiles.name_key).where(SpeakerProfiles.status != MERGED)
        .group_by(SpeakerProfiles.name_key).having(func.count() > 1)
    )).all()
    for key in keys:
        group = (await session.execute(
            select(SpeakerProfiles.id, SpeakerProfiles.created_by_club_id)
            .where(SpeakerProfiles.name_key == key, SpeakerProfiles.status != MERGED).order_by(SpeakerProfiles.id).limit(GROUP_MAX)
        )).all()
        for i, (a, club_a) in enumerate(group):
            for b, club_b in group[i + 1:]:
                if club_a is not None and club_a == club_b:
                    continue
                counts["candidates"] += int(await sp.add_candidate(session, a, b, sp.SAME_NAME_OTHER_CLUB, 0.8))


async def link_agenda(session, counts: dict) -> None:
    """Free-text agenda speakers, to that event's speaker rows by name."""
    linked = select(AgendaItemSpeakers.agenda_item_id).distinct()
    items = (await session.execute(
        select(AgendaItems.id, AgendaItems.event_id, AgendaItems.speaker)
        .where(AgendaItems.speaker.isnot(None), AgendaItems.speaker != "", AgendaItems.id.notin_(linked))
    )).all()
    by_event: dict = defaultdict(list)
    for item in items:
        by_event[item.event_id].append(item)
    for event_id, its in by_event.items():
        speakers = (await session.execute(
            select(EventSpeakers.id, EventSpeakers.name).where(EventSpeakers.event_id == event_id, EventSpeakers.is_deleted == False)  # noqa: E712
            .order_by(EventSpeakers.display_order, EventSpeakers.id)
        )).all()
        keyed: dict = {}
        for sid, name in speakers:
            keyed.setdefault(names.name_key(name), sid)
        if not keyed:
            continue
        for item in its:
            whole = names.name_key(item.speaker)
            matched = [keyed[whole]] if whole in keyed else [keyed[k] for k in names.agenda_names(item.speaker) if k in keyed]
            seen: list[int] = []
            for sid in matched:
                if sid not in seen:
                    seen.append(sid)
            for n, sid in enumerate(seen):
                session.add(AgendaItemSpeakers(agenda_item_id=item.id, event_speaker_id=sid, position=n))
            if seen:
                counts["agenda_sessions_linked"] += 1
            else:
                counts["agenda_sessions_unmatched"] += 1
    await session.flush()


async def seed_topics(session, counts: dict) -> None:
    """A profile with no topics takes its events' (the topics of their interests)."""
    from app.api.interests.models import Interests

    rows = (await session.execute(
        select(SpeakerProfiles.id, Interests.category_id)
        .join(EventSpeakers, EventSpeakers.speaker_profile_id == SpeakerProfiles.id)
        .join(EventInterestsLink, EventInterestsLink.event_id == EventSpeakers.event_id)
        .join(Interests, Interests.id == EventInterestsLink.interest_id)
        .where(
            func.cardinality(SpeakerProfiles.topics) == 0,
            SpeakerProfiles.status != MERGED, EventSpeakers.is_deleted == False, EventInterestsLink.is_deleted == False,  # noqa: E712
        )
        .distinct().order_by(SpeakerProfiles.id, Interests.category_id)
    )).all()
    topics: dict = defaultdict(list)
    for pid, cat in rows:
        if cat is not None and cat not in topics[pid]:
            topics[pid].append(cat)
    for pid, cats in topics.items():
        await session.execute(update(SpeakerProfiles).where(SpeakerProfiles.id == pid).values(topics=cats[:10]))
        counts["topics_seeded"] += 1


KEYS = (
    "by_link", "by_club_name", "by_photo", "profiles_made", "candidates",
    "agenda_sessions_linked", "agenda_sessions_unmatched", "topics_seeded",
)


async def run(session, *, apply: bool = True) -> dict:
    """The whole backfill on this session: committed when `apply`, else
    done and rolled back (a dry run: the counts are what --apply would do now)."""
    counts = {k: 0 for k in KEYS}
    try:
        await link_rows(session, counts, commit=apply)
        await same_name_candidates(session, counts)
        await link_agenda(session, counts)
        await seed_topics(session, counts)
        counts["unlinked_left"] = int(await session.scalar(
            select(func.count()).select_from(EventSpeakers).join(Events, Events.id == EventSpeakers.event_id)
            .where(EventSpeakers.speaker_profile_id.is_(None), EventSpeakers.is_deleted == False, Events.is_deleted == False)  # noqa: E712
        ) or 0)
        counts["linked"] = counts["by_link"] + counts["by_club_name"] + counts["by_photo"] + counts["profiles_made"]
        if apply:
            await session.commit()
        else:
            await session.rollback()
    except Exception:
        await session.rollback()
        raise
    return {"applied": apply, **counts}


@jobs.handler(JOB, max_attempts=3, timeout=1800.0, concurrency=1, priority=jobs.LOW)
async def _job(p: dict) -> None:
    async with jobs.scope() as s:
        done = await run(s, apply=True)
        logger.info("speakers backfill: %s", done)
        if p.get("by"):
            from app.api.audit import service as audit
            from app.api.users.models import Users

            admin = await s.get(Users, int(p["by"]))
            if admin is not None:
                await audit.record(s, actor=admin, action="speaker.backfill_done", entity_type="speaker_profile",
                                   summary=f"Speakers backfill: {done['linked']} rows linked, {done['profiles_made']} profiles made"[:255], details=done)
                await s.commit()
