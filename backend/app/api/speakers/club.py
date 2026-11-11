"""A club's speakers (`/api/v1/clubs/speakers`): the picker's suggestions,
the club's list of everyone who has spoken at its events (and as a
spreadsheet), one speaker with the club's private contact, and copying a
profile onto the club's upcoming events.

Access is editing the club's events: the owner, admins and editors; door
staff never (club_access.policy `_SPEAKERS`). Every plan.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.events.models import Events, EventSpeakers
from app.api.speakers import links, notify  # noqa: F401 - notify registers the push and email jobs
from app.api.speakers import service as sp
from app.api.speakers.models import ACTIVE, MERGED, SpeakerContacts, SpeakerProfiles
from app.core.auth.dependencies import ClubAuth
from app.core.utils import spreadsheet
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/speakers", tags=["speakers"])

SUGGEST_MAX = 20
LIST_PAGE_MAX = 200
EXPORT_MAX = 20000
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")
_PHONE = re.compile(r"^\+?[\d\s().-]{7,20}$")


def club_of(user):
    club = getattr(user, "club", None)
    if club is None:
        raise CustomHTTPException(403, message="A club's speakers are for the club's own dashboard.", error_code="CLUB_ONLY")
    return club


def _last_used_at(club_id: int):
    return (
        select(func.max(Events.event_datetime))
        .select_from(EventSpeakers).join(Events, Events.id == EventSpeakers.event_id)
        .where(
            EventSpeakers.speaker_profile_id == SpeakerProfiles.id, EventSpeakers.is_deleted == False,  # noqa: E712
            Events.club_id == club_id, Events.is_deleted == False,  # noqa: E712
        )
        .correlate(SpeakerProfiles).scalar_subquery()
    )


def contact_out(c: Optional[SpeakerContacts]) -> Optional[dict]:
    if c is None:
        return None
    return {"email": c.email, "phone": c.phone, "notes": c.notes, "consent_at": sp.iso(c.consent_at), "consent_by": c.consent_by_name}


async def _contacts(session, club_id: int, profile_ids: list[int]) -> dict[int, SpeakerContacts]:
    if not profile_ids:
        return {}
    rows = (await session.scalars(
        select(SpeakerContacts).where(SpeakerContacts.club_id == club_id, SpeakerContacts.profile_id.in_(profile_ids))
    )).all()
    return {c.profile_id: c for c in rows}


# ─── The picker ──────────────────────────────────────────────────────────


async def suggestions(session, club_id: int, q: Optional[str], limit: int) -> list[dict]:
    """The club's own past speakers first (by name when `q`, else the most
    recent), then claimed public profiles whose name matches `q`."""
    own = sp.own_ids(club_id).subquery()
    used = _last_used_at(club_id)
    cond, key = sp.name_match(SpeakerProfiles.name_key, q) if (q or "").strip() else (None, "")
    first = select(SpeakerProfiles).where(SpeakerProfiles.id.in_(select(own.c.id)), SpeakerProfiles.status == ACTIVE)
    if cond is not None:
        first = first.where(cond).order_by(func.similarity(SpeakerProfiles.name_key, key).desc(), used.desc().nulls_last(), SpeakerProfiles.id.desc())
    else:
        first = first.order_by(used.desc().nulls_last(), SpeakerProfiles.id.desc())
    rows = list((await session.scalars(first.limit(limit))).all())
    own_set = {p.id for p in rows}
    if cond is not None and len(rows) < limit:
        rows += list((await session.scalars(
            select(SpeakerProfiles)
            .where(
                SpeakerProfiles.status == ACTIVE, SpeakerProfiles.claimed_by_user_id.isnot(None),
                SpeakerProfiles.id.notin_(select(own.c.id)), cond,
            )
            .order_by(func.similarity(SpeakerProfiles.name_key, key).desc(), SpeakerProfiles.id.desc())
            .limit(limit - len(rows))
        )).all())
    ids = [p.id for p in rows]
    talk_rows = await sp.talks(session, ids, club_or_public=club_id)
    catalogue = await sp.topic_catalogue(session, [t for p in rows for t in (p.topics or [])])
    return [
        {**sp.summary(p, catalogue), "talks_count": len(talk_rows.get(p.id, [])), "last_talk": sp.last_talk(talk_rows.get(p.id, [])),
         "own": p.id in own_set}
        for p in rows
    ]


async def match_draft(session, club_id: int, speakers: list, per_name: int = 3) -> None:
    """An AI draft's speakers (app/api/ai): each name's matches among the
    club's past speakers, and the best one's id when its name is the same
    name (names.name_key) -- a near spelling is offered, never picked."""
    from app.api.ai.schemas import SpeakerMatch
    from app.api.speakers import names

    for person in speakers or []:
        found = await suggestions(session, club_id, person.name, per_name)
        person.matches = [
            SpeakerMatch(speaker_profile_id=m["profile_id"], name=m["name"], headline=m["headline"], photo=m["photo"],
                         talks_count=m["talks_count"], last_talk=m["last_talk"])
            for m in found
        ]
        key = names.name_key(person.name)
        same = [m for m in found if names.name_key(m["name"]) == key]
        person.speaker_profile_id = same[0]["profile_id"] if same else None


# ─── The club's list ─────────────────────────────────────────────────────


async def club_list(session, club_id: int, *, q: Optional[str], page: int, page_size: int, every: bool = False) -> dict:
    own = sp.own_ids(club_id).subquery()
    used = _last_used_at(club_id)
    where = [SpeakerProfiles.id.in_(select(own.c.id)), SpeakerProfiles.status != MERGED]
    if (q or "").strip():
        cond, _key = sp.name_match(SpeakerProfiles.name_key, q)
        where.append(cond)
    total = int(await session.scalar(select(func.count()).select_from(SpeakerProfiles).where(*where)) or 0)
    query = select(SpeakerProfiles).where(*where).order_by(used.desc().nulls_last(), SpeakerProfiles.name, SpeakerProfiles.id)
    if not every:
        query = query.offset((page - 1) * page_size).limit(page_size)
    else:
        query = query.limit(EXPORT_MAX)
    rows = list((await session.scalars(query)).all())
    ids = [p.id for p in rows]
    talk_rows = await sp.talks(session, ids, club_id=club_id)
    contacts = await _contacts(session, club_id, ids)
    catalogue = await sp.topic_catalogue(session, [t for p in rows for t in (p.topics or [])])
    items = [
        {**sp.summary(p, catalogue), "talks_count": len(talk_rows.get(p.id, [])), "last_talk": sp.last_talk(talk_rows.get(p.id, [])),
         "contact": contact_out(contacts.get(p.id))}
        for p in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


EXPORT_HEADERS = (
    "Name", "Headline", "City", "Topics", "Talks at the club", "Last talk", "Last talk date",
    "Email", "Phone", "Agreed to be contacted", "Notes", "Website", "LinkedIn", "X", "GitHub", "Profile claimed", "Status",
)


def export_rows(items: list[dict]):
    for i in items:
        links_ = i.get("links") or {}
        last = i.get("last_talk") or {}
        c = i.get("contact") or {}
        yield (
            i["name"], i.get("headline"), i.get("city"), ", ".join(t["name"] for t in i.get("topics") or []),
            i.get("talks_count", 0), last.get("event_name"), (last.get("date") or "")[:10] or None,
            c.get("email"), c.get("phone"), (c.get("consent_at") or "")[:10] or None, c.get("notes"),
            links_.get("website"), links_.get("linkedin"), links_.get("x"), links_.get("github"),
            bool(i.get("claimed")), i.get("status"),
        )


# ─── One speaker ─────────────────────────────────────────────────────────


async def sessions_for(session, event_speaker_ids: list[int]) -> dict[int, list[dict]]:
    """The agenda sessions each event row is a speaker of."""
    from app.api.agenda.models import AgendaItems
    from app.api.speakers.models import AgendaItemSpeakers

    if not event_speaker_ids:
        return {}
    rows = (await session.execute(
        select(AgendaItemSpeakers.event_speaker_id, AgendaItems.id, AgendaItems.title, AgendaItems.starts_at, AgendaItems.ends_at)
        .join(AgendaItems, AgendaItems.id == AgendaItemSpeakers.agenda_item_id)
        .where(AgendaItemSpeakers.event_speaker_id.in_(event_speaker_ids))
        .order_by(AgendaItems.starts_at)
    )).all()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r.event_speaker_id, []).append({"id": str(r.id), "title": r.title, "starts_at": sp.iso(r.starts_at), "ends_at": sp.iso(r.ends_at)})
    return out


def talk_out(t: dict, sessions: dict) -> dict:
    return {
        "event_id": t["event_id"], "event_name": t["event_name"], "slug": t["slug"], "date": sp.iso(t["date"]),
        "talk_title": t["talk_title"], "status": t["status"], "club": t["club"],
        "sessions": sessions.get(t["event_speaker_id"], []),
    }


async def club_profile(session, club_id: int, profile: SpeakerProfiles) -> dict:
    rows = (await sp.talks(session, [profile.id], club_id=club_id)).get(profile.id, [])
    sessions = await sessions_for(session, [t["event_speaker_id"] for t in rows])
    contact = (await _contacts(session, club_id, [profile.id])).get(profile.id)
    catalogue = await sp.topic_catalogue(session, profile.topics or [])
    return {
        **sp.summary(profile, catalogue), "own": await sp.is_own(session, club_id, profile.id),
        "talks": [talk_out(t, sessions) for t in rows], "talks_count": len(rows), "last_talk": sp.last_talk(rows),
        "contact": contact_out(contact),
    }


class ContactIn(BaseModel):
    """Fields left out keep what is there; an empty one clears it. A new
    email or phone needs `consent: true` (the speaker agreed)."""

    email: Optional[str] = Field(None, max_length=254)
    phone: Optional[str] = Field(None, max_length=20)
    notes: Optional[str] = Field(None, max_length=1000)
    consent: bool = False

    @field_validator("email", "phone", "notes", mode="before")
    @classmethod
    def _blank(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if v is not None and not _EMAIL.match(v):
            raise ValueError("Enter an email address like name@example.com.")
        return v.lower() if v else v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        if v is not None and not _PHONE.match(v):
            raise ValueError("Enter a phone number with its digits (a + and spaces are fine).")
        return v


async def put_contact(session, club_id: int, profile: SpeakerProfiles, body: ContactIn, actor: sp.Actor) -> dict:
    if profile.removed_at is not None:
        raise CustomHTTPException(409, message="This speaker asked to be removed, so their contact details are not kept.", error_code="SPEAKER_REMOVED")
    row = (await _contacts(session, club_id, [profile.id])).get(profile.id)
    sent = body.model_fields_set
    email = body.email if "email" in sent else (row.email if row else None)
    phone = body.phone if "phone" in sent else (row.phone if row else None)
    notes = body.notes if "notes" in sent else (row.notes if row else None)
    new_reach = (email and email != (row.email if row else None)) or (phone and phone != (row.phone if row else None))
    if new_reach and not body.consent:
        raise CustomHTTPException(
            422, message="Tick that the speaker agreed to their email or phone being kept.",
            error_code="CONSENT_REQUIRED", errors={"consent": "Required with an email or phone."},
        )
    if not (email or phone or notes):
        if row is not None:
            await session.delete(row)
            await sp.record(session, profile.id, "contact_removed", actor, after={"club_id": club_id})
            await session.commit()
        return {"contact": None}
    if row is None:
        row = SpeakerContacts(profile_id=profile.id, club_id=club_id)
        session.add(row)
    row.email, row.phone, row.notes = email, phone, notes
    if new_reach:
        row.consent_at, row.consent_by, row.consent_by_name = sp.now(), actor.user_id, actor.name
    elif not (email or phone):
        row.consent_at = row.consent_by = row.consent_by_name = None
    # Which fields changed, never their values: the history is the admins', the contact is the club's.
    await sp.record(session, profile.id, "contact_updated", actor, after={"club_id": club_id, "fields": sorted(sent - {"consent"})})
    await session.commit()
    return {"contact": contact_out(row)}


# ─── Routes ──────────────────────────────────────────────────────────────


@router.get("", summary="Speakers for the picker: the club's past speakers first, then claimed profiles by name")
async def suggest(
    session: SessionDep, user: ClubAuth,
    q: Optional[str] = Query(None, max_length=100), limit: int = Query(10, ge=1, le=SUGGEST_MAX),
) -> list[dict]:
    club = club_of(user)
    return await suggestions(session, club.id, q, limit)


@router.get("/list", summary="Everyone who has spoken at the club's events, with the club's contact details (or as a spreadsheet)")
async def list_speakers(
    session: SessionDep, user: ClubAuth,
    q: Optional[str] = Query(None, max_length=100), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=LIST_PAGE_MAX),
    fmt: Literal["json", "xlsx"] = Query("json", alias="format", description="json (the default) or xlsx"),
):
    club = club_of(user)
    if fmt == "xlsx":
        data = await club_list(session, club.id, q=q, page=1, page_size=EXPORT_MAX, every=True)
        path = await spreadsheet.write_xlsx(export_rows(data["items"]), title="Speakers", headers=EXPORT_HEADERS)
        return spreadsheet.xlsx_response(path, "speakers.xlsx")
    return await club_list(session, club.id, q=q, page=page, page_size=page_size)


@router.get("/{profile_id}", summary="One speaker: the profile, their talks at the club and the club's contact")
async def get_speaker(session: SessionDep, user: ClubAuth, profile_id: int) -> dict:
    club = club_of(user)
    profile = await sp.visible_to_club(session, club.id, profile_id)
    return await club_profile(session, club.id, profile)


@router.put("/{profile_id}/contact", summary="The club's own contact for a speaker (an email or phone needs their agreement)")
async def set_contact(request: Request, session: SessionDep, user: ClubAuth, profile_id: int, body: ContactIn) -> dict:
    club = club_of(user)
    profile = await sp.visible_to_club(session, club.id, profile_id)
    return await put_contact(session, club.id, profile, body, sp.club_actor(user, club.id, request))


@router.post("/{profile_id}/apply-to-upcoming", summary="Copy the profile's photo, headline, bio and link onto the club's upcoming events")
async def apply_upcoming(request: Request, session: SessionDep, user: ClubAuth, profile_id: int) -> dict:
    club = club_of(user)
    profile = await sp.visible_to_club(session, club.id, profile_id)
    return await links.apply_to_upcoming(session, club_id=club.id, profile=profile, actor=sp.club_actor(user, club.id, request))
