"""Where the directory meets an event: its speaker rows linked to profiles
when the event is saved, the profile's address on every payload that
carries the event's speakers, agenda sessions pointing at the event's
speakers, and a profile's edits copied to a club's upcoming events.

An event's speaker row keeps its own copy of name, designation, photo, bio
and link: that is what its page shows. A claimed profile is its speaker's,
and one on another club's events too is shared: a club's edit of its event
touches only its own copy then. A profile that is the club's alone (active,
unclaimed, on no other club's event) follows the club's edits
(`follow_club_edits`): the event page's speaker card opens the profile, and
an edit that stopped at the event's copy left that page showing what the
club had just corrected. Admins edit any profile.
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


# ─── A club's edits, to the profile that is the club's alone ─────────────

#: What a club's edit of an event row may change on its profile.
_FOLLOWED = ("name", "designation", "bio", "link", "photo")


def snapshot(row: EventSpeakers) -> dict:
    """An event row as it was before a club's edit (`follow_club_edits`)."""
    return {
        "profile_id": row.speaker_profile_id, "name": row.name, "designation": row.designation,
        "bio": row.bio, "link": row.link, "photo": sp.photo_key(row.photo),
    }


async def _clubs_of_other_rows(session, profile_id: int, row_id: Optional[int]) -> list[int]:
    """The clubs of the profile's other live event rows (one entry a row)."""
    q = (
        select(Events.club_id)
        .join(EventSpeakers, EventSpeakers.event_id == Events.id)
        .where(
            EventSpeakers.speaker_profile_id == profile_id, EventSpeakers.is_deleted == False,  # noqa: E712
            Events.is_deleted == False,  # noqa: E712
        )
    )
    if row_id is not None:
        q = q.where(EventSpeakers.id != row_id)
    return list((await session.scalars(q)).all())


def _the_clubs_alone(profile: Optional[SpeakerProfiles], club_id: int, others: list[int]) -> bool:
    return (
        profile is not None and profile.status == ACTIVE and profile.claimed_by_user_id is None
        and all(c == club_id for c in others)
    )


def _links_after(links: Optional[dict], old: Optional[str], new: Optional[str]) -> dict:
    """A profile's links with the event row's one link changed from `old` to `new`."""
    out = {k: v for k, v in dict(links or {}).items() if not (old and names.link_key(v) == names.link_key(old))}
    if new:
        out[names.link_kind(new)] = new
    return out


async def follow_club_edits(session, *, event_id: int, club_id: Optional[int], edited: list, actor: sp.Actor) -> None:
    """A club's edit of its event's speakers, on each one's profile when that
    profile is the club's alone (active, unclaimed, on no other club's event).

    `edited`: (row, `snapshot(row)` from before the edit), after
    `link_event_speakers`. Only what this edit changed is copied: the name,
    the designation (as the headline), the bio, the photo and the link. A row
    linked or picked in this save is left as linking left it.

    A new name that is not the profile's is another person, unless it is
    plainly the same name ("Meera" to "Meera Nair") or the profile has no
    other talk (a spelling put right): the row joins the club's profile of
    that exact name when there is one, else a new profile made from it.
    """
    if club_id is None or not edited:
        return
    made: list[SpeakerProfiles] = []
    for row, before in edited:
        if row.speaker_profile_id is None or row.speaker_profile_id != before.get("profile_id"):
            continue
        now = {**{f: getattr(row, f) for f in _FOLLOWED if f != "photo"}, "photo": sp.photo_key(row.photo)}
        changed = [f for f in _FOLLOWED if now[f] != before.get(f)]
        if not changed:
            continue
        profile = await session.get(SpeakerProfiles, row.speaker_profile_id)
        others = await _clubs_of_other_rows(session, row.speaker_profile_id, row.id) if profile is not None else []
        if not _the_clubs_alone(profile, club_id, others):
            continue
        was, after = {}, {}
        if "name" in changed and (row.name or "").strip():
            key = names.name_key(row.name)
            if key != profile.name_key:
                match = await sp.club_exact_match(session, club_id, key)
                if match is not None and match.id != profile.id:
                    row.speaker_profile_id = match.id
                    filled = _fill_blanks(match, row)
                    await sp.record(session, match.id, "added_to_event", actor, after={
                        "event_id": event_id, "event_speaker_id": row.id, "renamed_from": before.get("name"),
                        **({"filled": sorted(filled)} if filled else {}),
                    })
                    continue
                if others and not names.names_alike(profile.name_key, key):
                    new = await sp.create_profile(
                        session, name=row.name, headline=row.designation, bio=row.bio, photo=row.photo,
                        links=names.links_from(row.link), club_id=club_id, actor=actor,
                        source={"event_id": event_id, "event_speaker_id": row.id, "renamed_from": before.get("name")},
                    )
                    row.speaker_profile_id = new.id
                    made.append(new)
                    continue
                profile.name_key = key
            if row.name.strip()[:100] != profile.name:
                was["name"], after["name"] = profile.name, row.name.strip()[:100]
                profile.name = after["name"]
        if "designation" in changed:
            headline = (row.designation or "").strip()[:200] or None
            if headline != profile.headline:
                was["headline"], after["headline"] = profile.headline, headline
                profile.headline = headline
        if "bio" in changed:
            bio = (row.bio or "").strip()[:600] or None
            if bio != profile.bio:
                was["bio"], after["bio"] = profile.bio, bio
                profile.bio = bio
        if "link" in changed:
            links = _links_after(profile.links, before.get("link"), row.link)
            if links != dict(profile.links or {}):
                was["links"], after["links"] = dict(profile.links or {}), links
                profile.links = links
        if "photo" in changed and now["photo"] and now["photo"] != sp.photo_key(profile.photo):
            was["photo"], after["photo"] = sp.photo_key(profile.photo), now["photo"]
            profile.photo = now["photo"]
        if after:
            await sp.record(session, profile.id, "edited_from_event", actor, before=was, after={**after, "event_id": event_id})
    await session.flush()
    for profile in made:
        await sp.candidates_for_new(session, profile)


async def resync_club_profiles(session, *, apply: bool = False) -> dict:
    """Profiles that a club's edits made before `follow_club_edits` never
    reached: each active, unclaimed profile on one club's events only, from
    the event row that club changed last, when it changed after the profile
    did (an admin's later edit stands). Copies the designation (as the
    headline), the bio, the photo and the link where the row has one and it
    differs, and the name when it is the same name written otherwise; a
    different name is counted, not copied. A dry run unless `apply`.
    Returns the counts."""
    rows = (await session.execute(
        select(EventSpeakers, Events.club_id, SpeakerProfiles)
        .join(Events, Events.id == EventSpeakers.event_id)
        .join(SpeakerProfiles, SpeakerProfiles.id == EventSpeakers.speaker_profile_id)
        .where(
            EventSpeakers.is_deleted == False, Events.is_deleted == False,  # noqa: E712
            SpeakerProfiles.status == ACTIVE, SpeakerProfiles.claimed_by_user_id.is_(None),
        )
    )).all()
    by_profile: dict[int, list] = {}
    for row, club_id, profile in rows:
        by_profile.setdefault(profile.id, []).append((row, club_id, profile))
    counts = {"profiles_checked": len(by_profile), "profiles_updated": 0, "names_differ": 0,
              "fields": {"name": 0, "headline": 0, "bio": 0, "links": 0, "photo": 0}}
    for entries in by_profile.values():
        if len({club_id for _, club_id, _ in entries}) != 1:
            continue
        profile = entries[0][2]
        row = max((r for r, _, _ in entries), key=lambda r: (r.updated_at, r.id))
        if profile.updated_at is not None and row.updated_at is not None and row.updated_at <= profile.updated_at:
            continue
        want = {}
        name = (row.name or "").strip()[:100]
        if name and name != profile.name:
            if names.name_key(name) == profile.name_key:
                want["name"] = name
            else:
                counts["names_differ"] += 1
        headline = (row.designation or "").strip()[:200] or None
        if headline and headline != profile.headline:
            want["headline"] = headline
        bio = (row.bio or "").strip()[:600] or None
        if bio and bio != profile.bio:
            want["bio"] = bio
        if row.link and names.link_key(row.link) not in names.profile_link_keys(profile.links):
            want["links"] = {**dict(profile.links or {}), names.link_kind(row.link): row.link}
        photo = sp.photo_key(row.photo)
        if photo and photo != sp.photo_key(profile.photo):
            want["photo"] = photo
        if not want:
            continue
        counts["profiles_updated"] += 1
        for field in want:
            counts["fields"][field] += 1
        if apply:
            was = {f: (sp.photo_key(profile.photo) if f == "photo" else getattr(profile, f)) for f in want}
            for field, value in want.items():
                setattr(profile, field, value)
            await sp.record(session, profile.id, "resynced_from_event", sp.SYSTEM, before=was, after={**want, "event_speaker_id": row.id})
    if apply:
        await session.commit()
    else:
        await session.rollback()
    return counts


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
