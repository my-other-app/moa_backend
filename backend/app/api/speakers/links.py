"""Where the directory meets an event: its speaker rows linked to profiles
when the event is saved, the profile's address on every payload that
carries the event's speakers, agenda sessions pointing at the event's
speakers, and a profile's edits copied to a club's upcoming events.

An event's speaker row keeps its own copy of name, designation, photo, bio
and link: that is what its page shows, and a club's edit there touches only
that copy. The profile is edited by admins and, once claimed, the speaker.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import delete, select

from app.api.closeout.models import EventStatus
from app.api.events.models import Events, EventSpeakers, ends_at_sql
from app.api.speakers import names
from app.api.speakers import service as sp
from app.api.speakers.models import ACTIVE, AgendaItemSpeakers, SpeakerProfiles
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

TALK_TITLE_MAX = 200


def clean_title(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A talk title must be text.")
    value = value.strip()
    if len(value) > TALK_TITLE_MAX:
        raise ValueError(f"A talk title can be at most {TALK_TITLE_MAX} characters.")
    return value or None


def _fill_blanks(profile: SpeakerProfiles, row: EventSpeakers) -> dict:
    """An unclaimed profile's empty fields, from an event row of its club:
    nothing it holds is changed. Returns what was filled."""
    if profile.claimed_by_user_id is not None:
        return {}
    filled = {}
    if not profile.headline and row.designation:
        profile.headline = row.designation[:200]
        filled["headline"] = profile.headline
    if not profile.bio and row.bio:
        profile.bio = row.bio[:600]
        filled["bio"] = True
    if not profile.photo and sp.photo_key(row.photo):
        profile.photo = sp.photo_key(row.photo)
        filled["photo"] = True
    if not profile.links and row.link:
        profile.links = names.links_from(row.link)
        filled["links"] = True
    return filled


async def check_picks(session, club_id: Optional[int], speakers: list, *, existing: Optional[dict] = None) -> None:
    """422 for a `speaker_profile_id` this club may not put on an event,
    before the form changes anything. A row keeping the profile it has is
    never refused (a profile hidden since stays on its events)."""
    existing = existing or {}
    for sent in speakers or []:
        wanted = getattr(sent, "speaker_profile_id", None)
        if wanted is None or "speaker_profile_id" not in getattr(sent, "model_fields_set", set()):
            continue
        if getattr(sent, "id", None) is not None and existing.get(sent.id) == int(wanted):
            continue
        if club_id is None:
            raise CustomHTTPException(422, message="That speaker can't be added from the directory.", error_code="SPEAKER_NOT_AVAILABLE")
        await sp.pickable(session, club_id, int(wanted))


async def link_event_speakers(session, *, event_id: int, club_id: Optional[int], pairs: list, actor: sp.Actor) -> list[SpeakerProfiles]:
    """Each saved speaker row of an event, linked to its profile.

    `pairs`: (row, what the form sent for it). A `speaker_profile_id` sent
    picks that profile (the club's own, or a claimed one; 422 otherwise),
    and an empty photo takes the profile's. Without one, a row already
    linked keeps its profile; one that is not is linked to the club's own
    profile of exactly that name, or a new profile made from it. A
    `talk_title` sent is the row's. Flushes (photos are stored, rows have
    ids). Returns the profiles made."""
    if not pairs:
        return []
    await session.flush()
    made: list[SpeakerProfiles] = []
    for row, sent in pairs:
        fields = getattr(sent, "model_fields_set", set())
        if "talk_title" in fields:
            row.talk_title = getattr(sent, "talk_title", None)
        wanted = getattr(sent, "speaker_profile_id", None) if "speaker_profile_id" in fields else None
        if wanted is not None and int(wanted) != row.speaker_profile_id:
            if club_id is None:
                raise CustomHTTPException(422, message="That speaker can't be added from the directory.", error_code="SPEAKER_NOT_AVAILABLE")
            profile = await sp.pickable(session, club_id, int(wanted))
            row.speaker_profile_id = profile.id
            if not sp.photo_key(row.photo) and sp.photo_key(profile.photo):
                row.photo = sp.photo_key(profile.photo)
            await sp.record(session, profile.id, "added_to_event", actor, after={"event_id": event_id, "event_speaker_id": row.id})
            continue
        if row.speaker_profile_id is not None:
            continue
        key = names.name_key(row.name)
        profile = await sp.club_exact_match(session, club_id, key) if club_id is not None else None
        if profile is not None:
            row.speaker_profile_id = profile.id
            filled = _fill_blanks(profile, row)
            await sp.record(session, profile.id, "added_to_event", actor, after={"event_id": event_id, "event_speaker_id": row.id, **({"filled": sorted(filled)} if filled else {})})
            continue
        profile = await sp.create_profile(
            session, name=row.name, headline=row.designation, bio=row.bio, photo=row.photo, links=names.links_from(row.link),
            club_id=club_id, actor=actor, source={"event_id": event_id, "event_speaker_id": row.id},
        )
        row.speaker_profile_id = profile.id
        made.append(profile)
    await session.flush()
    for profile in made:
        await sp.candidates_for_new(session, profile)
    return made


# ─── Payloads ────────────────────────────────────────────────────────────


async def speakers_out(session, rows: Iterable[EventSpeakers], *, photo=None) -> list[dict]:
    """An event's speakers as its payloads send them, each with its profile's
    id, talk title and public address (`profile_slug`: null when the profile
    is hidden). `photo`: how a row's photo is shown (the stored links by default)."""
    rows = [r for r in rows if not getattr(r, "is_deleted", False)]
    slugs = await sp.slugs_for(session, [r.speaker_profile_id for r in rows])
    out = []
    for r in sorted(rows, key=lambda r: (r.display_order or 0, r.id or 0)):
        out.append({
            "id": r.id, "name": r.name, "designation": r.designation,
            "photo": photo(r.photo) if photo is not None else r.photo, "bio": r.bio, "link": r.link,
            "display_order": r.display_order, "speaker_profile_id": r.speaker_profile_id,
            "talk_title": r.talk_title, "profile_slug": slugs.get(r.speaker_profile_id),
        })
    return out


# ─── Agenda sessions ─────────────────────────────────────────────────────


async def agenda_links(session, item_ids: Iterable) -> dict:
    """Each agenda session's speakers, in order: {event_speaker_id, name, photo_thumbnail, profile_slug}."""
    ids = list({i for i in item_ids if i is not None})
    if not ids:
        return {}
    rows = (await session.execute(
        select(AgendaItemSpeakers.agenda_item_id, AgendaItemSpeakers.position, EventSpeakers)
        .join(EventSpeakers, EventSpeakers.id == AgendaItemSpeakers.event_speaker_id)
        .where(AgendaItemSpeakers.agenda_item_id.in_(ids), EventSpeakers.is_deleted == False)  # noqa: E712
        .order_by(AgendaItemSpeakers.agenda_item_id, AgendaItemSpeakers.position, EventSpeakers.id)
    )).all()
    slugs = await sp.slugs_for(session, [r[2].speaker_profile_id for r in rows])
    out: dict = {}
    for item_id, _position, s in rows:
        photo = s.photo if isinstance(s.photo, dict) else None
        out.setdefault(item_id, []).append({
            "event_speaker_id": s.id, "name": s.name,
            "photo_thumbnail": (photo or {}).get("thumbnail") or (photo or {}).get("original"),
            "profile_slug": slugs.get(s.speaker_profile_id),
        })
    return out


async def set_agenda_speakers(session, event_id: int, item_id, speaker_ids: list) -> list[str]:
    """A session's speakers, replaced: rows of this event only (422 for any
    other). Returns their names, in order."""
    wanted: list[int] = []
    for i in speaker_ids or []:
        if int(i) not in wanted:
            wanted.append(int(i))
    rows = {}
    if wanted:
        rows = {s.id: s for s in (await session.scalars(
            select(EventSpeakers).where(EventSpeakers.id.in_(wanted), EventSpeakers.event_id == event_id, EventSpeakers.is_deleted == False)  # noqa: E712
        )).all()}
        missing = [i for i in wanted if i not in rows]
        if missing:
            raise CustomHTTPException(
                422, message="Pick the session's speakers from this event's speakers.",
                error_code="NOT_THIS_EVENTS_SPEAKER", errors={"event_speaker_ids": missing},
            )
    await session.execute(delete(AgendaItemSpeakers).where(AgendaItemSpeakers.agenda_item_id == item_id))
    for n, i in enumerate(wanted):
        session.add(AgendaItemSpeakers(agenda_item_id=item_id, event_speaker_id=i, position=n))
    return [rows[i].name for i in wanted]


def joined_names(names_: list[str], limit: int = 120) -> Optional[str]:
    text = ", ".join(n for n in names_ if n)
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip(", ") + "…"


# ─── A profile's edits, to a club's upcoming events ──────────────────────

_NOT_UPCOMING = (EventStatus.cancelled, EventStatus.closed, EventStatus.archived)


async def apply_to_upcoming(session, *, club_id: int, profile: SpeakerProfiles, actor: sp.Actor) -> dict:
    """The profile's shown fields (name, headline, photo, bio, first link)
    copied onto this club's event rows of it, on events not over yet (never
    a past one, never another club's). Returns the events changed."""
    if profile.status != ACTIVE:
        raise CustomHTTPException(409, message="This speaker's profile is not active.", error_code="SPEAKER_NOT_ACTIVE")
    now = sp.now()
    rows = (await session.execute(
        select(EventSpeakers, Events.id)
        .join(Events, Events.id == EventSpeakers.event_id)
        .where(
            EventSpeakers.speaker_profile_id == profile.id, EventSpeakers.is_deleted == False,  # noqa: E712
            Events.club_id == club_id, Events.is_deleted == False, ends_at_sql() > now,  # noqa: E712
            Events.lifecycle.notin_(_NOT_UPCOMING),
        )
    )).all()
    link = names.first_link(profile.links)
    key = sp.photo_key(profile.photo)
    events: list[int] = []
    for row, event_id in rows:
        row.name = profile.name[:100]
        row.designation = profile.headline
        row.bio = profile.bio
        row.link = link
        if key:
            row.photo = key
        if event_id not in events:
            events.append(event_id)
    await sp.record(session, profile.id, "applied_to_upcoming", actor, after={"club_id": club_id, "event_ids": events})
    await session.commit()
    from app.api.events.service import _republish_site

    for event_id in events:
        try:
            await _republish_site(session, event_id)
        except Exception:  # noqa: BLE001 - the copy is saved; the site catches up on its next publish
            logger.exception("republishing the site after a speaker update failed for event %s", event_id)
    return {"updated": len(rows), "event_ids": events}
