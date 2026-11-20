"""A poster's words for one guest (CONTRACT.md §2, §4).

The v1 tokens a canvas knows (doc_presets.values.canvas_values), the form's
answers as field.<slug>, and over them the poster's own: the name from the
registration, their role, their organisation, a speaker's talk from the
agenda, the event, the moment's phrase and their share link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select

from app.api.doc_presets import values as doc_values

ROLES = ("attendee", "speaker", "volunteer", "organiser")
ROLE_LABELS = {"attendee": "Attendee", "speaker": "Speaker", "volunteer": "Volunteer", "organiser": "Organiser"}
#: A form question that asks where someone works or studies.
_ORG_QUESTION = re.compile(r"organi[sz]ation|company|college|institut|universit|employer|workplace|school|campus|firm", re.I)


@dataclass
class Talk:
    title: str = ""
    time: str = ""
    hall: str = ""


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, (role or "attendee").title())


async def poster_role(session, event, registration) -> str:
    """speaker (their registration's role), volunteer (role staff, or an
    approved volunteer of the event or its club), organiser (on the host's or
    a co-host's team), else attendee."""
    raw = getattr(registration.role, "value", registration.role) or "attendee"
    if raw == "speaker":
        return "speaker"
    if raw == "staff":
        return "volunteer"
    from app.api.events.volunteer.models import Volunteer

    email = (registration.email or "").strip().lower()
    where_who = [Volunteer.user_id == registration.user_id]
    if email:
        where_who.append(func.lower(Volunteer.email) == email)
    found = await session.scalar(
        select(Volunteer.id).where(
            Volunteer.is_deleted == False, Volunteer.is_approved == True,  # noqa: E712
            or_(Volunteer.event_id == event.id, and_(Volunteer.event_id.is_(None), Volunteer.club_id == event.club_id)),
            or_(*where_who),
        ).limit(1)
    )
    if found:
        return "volunteer"
    if email:
        from app.api.club_access.models import ClubMembers
        from app.api.cohosts import access as cohosts

        clubs = [c for c in [event.club_id, *await cohosts.cohost_club_ids(session, event.id)] if c]
        if clubs and await session.scalar(
            select(ClubMembers.id).where(
                ClubMembers.club_id.in_(clubs), ClubMembers.status == "active", func.lower(ClubMembers.email) == email,
            ).limit(1)
        ):
            return "organiser"
    return "attendee"


def organisation_of(event, registration, subject) -> str:
    """Their answer to the form's company/college question (as tickets do),
    else their app profile's organisation, else nothing."""
    details = registration.additional_details if isinstance(getattr(registration, "additional_details", None), dict) else {}
    labels = {f.get("key"): f.get("label") for f in (getattr(event, "additional_details", None) or []) if isinstance(f, dict)}
    for key, value in details.items():
        if value in (None, "") or isinstance(value, (list, dict)):
            continue
        if _ORG_QUESTION.search(str(key)) or _ORG_QUESTION.search(str(labels.get(key) or "")):
            text = " ".join(str(value).split())
            if text:
                return text[:120]
    institution = getattr(registration, "institution", None)
    if institution:
        return str(institution)[:120]
    return subject.organisation or ""


async def ticket_type_of(session, registration) -> str:
    label = getattr(registration, "ticket_label", None)
    if label:
        return str(label)
    type_id = getattr(registration, "ticket_type_id", None)
    if not type_id:
        return ""
    from app.api.ticketing.models import TicketTypes

    return str(await session.scalar(select(TicketTypes.name).where(TicketTypes.id == type_id)) or "")


def _talk_time(starts_at, event) -> str:
    if starts_at is None:
        return ""
    from app.api.guest_posters.moments import ends_at

    multi_day = doc_values.fmt_date(event.event_datetime) != doc_values.fmt_date(ends_at(event))
    local = doc_values._local(starts_at)
    return f"{local.strftime('%a')} {local.strftime('%H:%M')}" if multi_day else local.strftime("%H:%M")


async def talk_of(session, event, registration) -> Talk:
    """A speaker's session from the event's agenda: the agenda session linked
    to their speaker entry on the event (matched by their claimed profile,
    the club's contact address for them, or their name), else one whose
    free-text speaker names them; its title, start and hall. Without a
    session, the talk title on their speaker entry."""
    from app.api.agenda.models import AgendaItems
    from app.api.events.models import EventSpeakers
    from app.api.speakers.models import AgendaItemSpeakers, SpeakerContacts, SpeakerProfiles
    from app.api.speakers.names import agenda_names, name_key

    raw = getattr(registration.role, "value", registration.role)
    if raw != "speaker":
        return Talk()
    speakers = list((await session.scalars(
        select(EventSpeakers).where(EventSpeakers.event_id == event.id, EventSpeakers.is_deleted == False)  # noqa: E712
        .order_by(EventSpeakers.display_order, EventSpeakers.id)
    )).all())
    mine = None
    profiles = {s.speaker_profile_id for s in speakers if s.speaker_profile_id}
    if profiles:
        claimed = set((await session.scalars(
            select(SpeakerProfiles.id).where(SpeakerProfiles.id.in_(profiles), SpeakerProfiles.claimed_by_user_id == registration.user_id)
        )).all())
        email = (registration.email or "").strip().lower()
        if email:
            claimed |= set((await session.scalars(
                select(SpeakerContacts.profile_id).where(
                    SpeakerContacts.profile_id.in_(profiles), SpeakerContacts.club_id == event.club_id,
                    func.lower(SpeakerContacts.email) == email,
                )
            )).all())
        mine = next((s for s in speakers if s.speaker_profile_id in claimed), None)
    key = name_key(registration.full_name)
    if mine is None and key:
        mine = next((s for s in speakers if name_key(s.name) == key), None)
    item = None
    if mine is not None:
        item = await session.scalar(
            select(AgendaItems).join(AgendaItemSpeakers, AgendaItemSpeakers.agenda_item_id == AgendaItems.id)
            .where(AgendaItemSpeakers.event_speaker_id == mine.id, AgendaItems.event_id == event.id)
            .order_by(AgendaItems.starts_at, AgendaItems.sort_order).limit(1)
        )
    if item is None and key:
        rows = (await session.scalars(
            select(AgendaItems).where(AgendaItems.event_id == event.id, AgendaItems.speaker.isnot(None))
            .order_by(AgendaItems.starts_at, AgendaItems.sort_order)
        )).all()
        item = next((r for r in rows if key in agenda_names(r.speaker)), None)
    if item is not None:
        return Talk(title=item.title or "", time=_talk_time(item.starts_at, event), hall=item.location or "")
    return Talk(title=(mine.talk_title or "") if mine is not None else "")


def poster_values(event, registration, subject, *, role: str, organisation: str, ticket_type: str, talk: Talk,
                  moment_phrase: str, share_url: str) -> dict:
    """Every token a poster may print for this guest (CONTRACT.md §2)."""
    values = dict(doc_values.canvas_values("ticket", event, subject))
    first, last = doc_values._split(subject.full_name)
    start = event.event_datetime
    values.update({
        "name": subject.full_name,
        "first_name": first,
        "last_name": last,
        "initials": doc_values.initials_of(subject.full_name),
        "role": role,
        "role_label": role_label(role),
        "ticket_type": ticket_type,
        "organisation": organisation,
        "talk_title": talk.title,
        "talk_time": talk.time,
        "talk_hall": talk.hall,
        "event_name": event.name or "",
        "event_date": doc_values.fmt_date(start),
        "event_time": doc_values.fmt_time(start),
        "venue": doc_values._venue(event),
        "city": doc_values._city(event),
        "club_name": doc_values._club_name(event),
        "moment_phrase": moment_phrase,
        "share_url": share_url,
    })
    # A poster is shared in public: no email address or phone number is ever
    # among what it is built from, whatever its design names (CONTRACT.md §2).
    from app.core.layout_v2 import poster

    return poster.without_contacts(values, poster.contact_field_slugs(getattr(event, "additional_details", None)))


SAMPLE_SHARE_URL = "https://myotherapp.in/l/samplecd"


def sample_values(event, *, role: str = "attendee", moment_phrase: str = "I'm attending") -> dict:
    """The board's made-up guest, on the event's real details."""
    subject = doc_values.sample_subject(event, "badge", "speaker" if role == "speaker" else "attendee")
    talk = Talk("Building for Bharat", "11:30", "Hall B") if role == "speaker" else Talk()
    return poster_values(event, _SampleRegistration(), subject, role=role, organisation=subject.organisation or "",
                         ticket_type="General admission", talk=talk, moment_phrase=moment_phrase, share_url=SAMPLE_SHARE_URL)


class _SampleRegistration:
    additional_details: dict = {}
    institution = None
