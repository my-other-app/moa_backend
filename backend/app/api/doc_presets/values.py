"""From our rows to the designer's tokens: one mapping per kind (D5).

Each design names its data with the designer's tokens ({{attendee_name}},
{{institute_name}}, {{badge_id}}, ...). What we fill each with is decided
here and nowhere else. A value we do not have is the empty string, never an
invention: the template's optional block around it is then removed
(app.core.doc_designs.fill).

A club can bind a design's optional tokens (`preset.bindings`). The bindings
are applied once, by A's fill, as they are stored:

    {"source": "auto"}                   what this file says (the default)
    {"source": "text", "value": "..."}   the same text on every document
    {"source": "field", "value": "<slug>"}  values["field.<slug>"]: a registration answer
    {"source": "hidden"}                 nothing: the block is removed

so every answered question of the registration is in the values too, as
"field.<slug>" (by the slug of the question's key and of its label).

The QR is A's to draw: `Mapped.qr_text` goes in as values["qr"]. The other
pictures (the club's logo, a signature, the guest's photo) are named as
references (`Mapped.images`) and become `data:` URIs when the document is
drawn (jobs.materialise): never a URL, so a design never fetches anything (D1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.layout.tokens import field_slug

EVENT_TIMEZONE = "Asia/Kolkata"
PLACINGS = {1: "First", 2: "Second", 3: "Third"}
ROLE_LABELS = {"attendee": "Attendee", "speaker": "Speaker", "staff": "Staff"}

# Every token the designer's 35 templates use, by kind (the packages'
# READMEs and manifests). The mapping below gives each one a value, so no
# template is ever filled with a token missing.
TICKET_TOKENS = (
    "app_name", "event_name", "tagline", "date", "doors_time", "start_time", "venue",
    "gate", "row", "seat", "ticket_type", "ticket_id", "attendee_name",
)
CERTIFICATE_TOKENS = (
    "recipient_name", "institute_name", "certificate_id", "verify_url", "course_title", "event_title",
    "event_date", "issue_date", "issue_year", "start_date", "end_date", "completion_date", "issue_city",
    "grade", "rank_or_grade", "signatory_name", "signatory_designation", "second_signatory_name",
    "second_signatory_designation", "duration_hours", "expiry_date",
)
BADGE_TOKENS = (
    "first_name", "last_name", "full_name", "initials", "job_title", "organization", "role", "access_level",
    "badge_id", "event_name", "event_date", "event_year", "event_venue", "event_tagline", "org_name",
    "org_founded_year", "city", "chapter", "membership_tier", "member_id", "member_since_year", "joined_date",
    "valid_thru", "valid_year", "zone", "hall", "day_label", "doors_open_time", "session_hall", "session_time",
    "checkin_time", "kiosk_id", "booth_no", "table_no", "track", "team_name", "bib_number", "category", "wave",
    "start_time",
)
#: Certificate tokens we have no data for: the club binds them (a fixed
#: text) or they are left out.
FROM_THE_CLUB = (
    "signatory_name", "signatory_designation", "second_signatory_name", "second_signatory_designation",
    "duration_hours", "expiry_date",
)


@dataclass
class Subject:
    """The person a document is for, as the mappings read them: a real
    registration (`subject_of`) or the editor's sample guest (`sample_subject`)."""

    full_name: str
    role: str = "attendee"
    ticket_code: str = ""
    admission_no: Optional[str] = None
    ticket_label: Optional[str] = None
    certificate_no: str = ""
    issued_at: Optional[datetime] = None
    checked_in_at: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    position: Optional[int] = None
    organisation: Optional[str] = None
    #: Answers to the registration form: field.<slug> (of its key and of its label) -> text.
    answers: dict = field(default_factory=dict)
    #: The answer to the form's photo question, as stored (photos.py reads it).
    photo_answer: Optional[str] = None
    #: The bucket key of the person's own profile photo, if they have one.
    profile_photo_key: Optional[str] = None
    team: str = ""


@dataclass
class Mapped:
    """One document's data: the text for every token, and what each picture is."""

    values: dict
    #: token -> ("key", bucket key) | ("photo", Subject) | None
    images: dict
    #: What the QR encodes (values["qr"]): the ticket's code, a certificate's verify page.
    qr_text: str


# ─── Formatting ─────────────────────────────────────────────────────────────


def _local(d: datetime) -> datetime:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(ZoneInfo(EVENT_TIMEZONE))


def fmt_date(d: Optional[datetime]) -> str:
    """14 Feb 2027, in the event's zone."""
    if d is None:
        return ""
    d = _local(d)
    return f"{d.day} {d.strftime('%b')} {d.year}"


def fmt_time(d: Optional[datetime]) -> str:
    """18:30, in the event's zone."""
    return _local(d).strftime("%H:%M") if d is not None else ""


def fmt_month(d: Optional[datetime]) -> str:
    """Mar 2026."""
    return f"{_local(d).strftime('%b')} {_local(d).year}" if d is not None else ""


def _year(d: Optional[datetime]) -> str:
    return str(_local(d).year) if d is not None else ""


def initials_of(name: str) -> str:
    parts = [p for p in (name or "").replace("-", " ").split() if p[:1].isalpha()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _split(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split()
    return (parts[0] if parts else ""), (" ".join(parts[1:]) if len(parts) > 1 else "")


def _venue(event) -> str:
    if getattr(event, "is_online", False) and not getattr(event, "location_name", None):
        return "Online"
    return getattr(event, "location_name", None) or ("Online" if getattr(event, "is_online", False) else "")


def _city(event) -> str:
    from app.api.search.docs import city_of

    club = getattr(event, "club", None)
    found = city_of(getattr(event, "location_name", None), getattr(club, "location_name", None) if club is not None else None)
    return found.title() if found else ""


def _ends(event) -> datetime:
    start = event.event_datetime
    return start + timedelta(hours=event.duration) if getattr(event, "duration", None) else start


def _club_name(event) -> str:
    club = getattr(event, "club", None)
    return (club.name if club is not None else "") or ""


def _role(subject: Subject) -> str:
    return str(subject.role or "attendee")


def ticket_type_of(subject: Subject) -> str:
    """The ticket's label when it has one; a speaker's or staff's role over it,
    as the layered tickets print it (documents.layout_values)."""
    role = _role(subject)
    if role == "attendee" and subject.ticket_label:
        return subject.ticket_label
    return ROLE_LABELS.get(role, role.title())


# ─── The people ─────────────────────────────────────────────────────────────


def _loaded(obj, attr: str) -> bool:
    from app.api.events.documents import _loaded as loaded

    return loaded(obj, attr)


def _answers(registration, event) -> dict:
    """field.<slug> -> the answer as text, by the question's key and its label."""
    details = registration.additional_details if isinstance(getattr(registration, "additional_details", None), dict) else {}
    labels = {}
    for f in getattr(event, "additional_details", None) or []:
        if isinstance(f, dict) and f.get("key") and f.get("label"):
            labels[f["key"]] = f["label"]
    out = {}
    for key, value in details.items():
        text = "" if value is None else (", ".join(str(v) for v in value) if isinstance(value, list) else str(value))
        out[f"field.{field_slug(str(key))}"] = text
        if labels.get(key):
            out.setdefault(f"field.{field_slug(labels[key])}", text)
    return out


def photo_question_key(event) -> Optional[str]:
    """The key of the form's first photo question (an `image` field), if any."""
    for f in getattr(event, "additional_details", None) or []:
        kind = f.get("field_type") if isinstance(f, dict) else None
        kind = getattr(kind, "value", kind)
        if kind == "image" and f.get("key"):
            return f["key"]
    return None


def subject_of(registration, event=None) -> Subject:
    """A registration as the mappings read it. The person's organisation and
    profile photo come from their app profile when it was loaded with them
    (idcards loads it; without it they are simply not on the card)."""
    event = event or registration.event
    raw_role = getattr(registration, "role", None)
    role = getattr(raw_role, "value", raw_role) or "attendee"
    details = registration.additional_details if isinstance(getattr(registration, "additional_details", None), dict) else {}
    organisation, photo_key = None, None
    user = registration.user if _loaded(registration, "user") else None
    profile = user.profile if user is not None and _loaded(user, "profile") else None
    if profile is not None and not getattr(profile, "is_deleted", False):
        if _loaded(profile, "org") and profile.org is not None:
            organisation = profile.org.name
        pic = getattr(profile, "profile_pic", None)
        variations = getattr(pic, "variations", None)
        if isinstance(variations, dict):
            photo_key = variations.get("large") or variations.get("original")
        elif isinstance(pic, str):
            photo_key = pic
    question = photo_question_key(event)
    from app.api.events.certificates import number_of

    return Subject(
        full_name=registration.full_name or "",
        role=str(role),
        ticket_code=registration.ticket_id or "",
        admission_no=getattr(registration, "admission_no", None),
        ticket_label=getattr(registration, "ticket_label", None),
        certificate_no=number_of(registration),
        issued_at=getattr(registration, "certificate_issued_at", None),
        checked_in_at=getattr(registration, "attended_on", None),
        registered_at=getattr(registration, "created_at", None),
        position=getattr(registration, "position", None),
        organisation=organisation,
        answers=_answers(registration, event),
        photo_answer=str(details.get(question)) if question and details.get(question) else None,
        profile_photo_key=photo_key,
        team=str(details.get("team_name") or details.get("team") or ""),
    )


def sample_subject(event, kind: str, role: str = "attendee") -> Subject:
    """The editor's made-up guest, on the event's real details."""
    answers = {}
    for f in getattr(event, "additional_details", None) or []:
        if isinstance(f, dict) and (f.get("key") or f.get("label")):
            shown = f"‹{f.get('label') or f.get('key')}›"
            for name in (f.get("key"), f.get("label")):
                if name:
                    answers.setdefault(f"field.{field_slug(str(name))}", shown)
    return Subject(
        full_name="Aisha Rahman",
        role=role,
        ticket_code="MOA-SAMPLE-0041",
        admission_no=None,
        ticket_label=None,
        certificate_no="CERT-7K3QF-X9PWM",
        issued_at=datetime.now(timezone.utc),
        checked_in_at=None,
        registered_at=datetime.now(timezone.utc),
        position=1 if kind == "certificate" else None,
        organisation="Sahyadri Systems",
        answers=answers,
        team="Team Nova",
    )


# ─── The mappings ───────────────────────────────────────────────────────────


def ticket_values(event, subject: Subject) -> Mapped:
    start = event.event_datetime
    values = {
        "app_name": _club_name(event),
        "event_name": event.name or "",
        # Our events have no subtitle; a club binds one if it wants one.
        "tagline": "",
        "date": fmt_date(start),
        # Doors are hidden unless the club sets a time (a binding).
        "doors_time": "",
        "start_time": fmt_time(start),
        "venue": _venue(event),
        # No seating: general admission hides gate, row and seat.
        "gate": "",
        "row": "",
        "seat": "",
        "ticket_type": ticket_type_of(subject),
        "ticket_id": subject.ticket_code,
        "attendee_name": subject.full_name,
        **subject.answers,
    }
    return Mapped(values=values, images={}, qr_text=subject.ticket_code)


def certificate_values(event, subject: Subject, *, logo_key: Optional[str], signatures: tuple = (None, None)) -> Mapped:
    from app.core.public_urls import site_url

    start, end = event.event_datetime, _ends(event)
    issued = subject.issued_at or datetime.now(timezone.utc)
    placing = PLACINGS.get(subject.position or 0, "")
    verify = site_url(f"/verify/{subject.certificate_no}")
    values = {
        "recipient_name": subject.full_name,
        "institute_name": _club_name(event),
        "certificate_id": subject.certificate_no,
        "verify_url": verify,
        "course_title": event.name or "",
        "event_title": event.name or "",
        "event_date": fmt_date(start),
        "issue_date": fmt_date(issued),
        "issue_year": _year(issued),
        "start_date": fmt_date(start),
        "end_date": fmt_date(end),
        "completion_date": fmt_date(end),
        "issue_city": _city(event),
        "grade": placing,
        "rank_or_grade": placing,
    }
    for token in FROM_THE_CLUB:
        values[token] = ""
    values.update(subject.answers)
    images = {
        "logo_src": ("key", logo_key) if logo_key else None,
        "signature_src": ("key", signatures[0]) if signatures[0] else None,
        "second_signature_src": ("key", signatures[1]) if signatures[1] else None,
    }
    return Mapped(values=values, images=images, qr_text=verify)


def _bib(subject: Subject) -> str:
    if subject.admission_no:
        return str(subject.admission_no)
    code = "".join(ch for ch in (subject.ticket_code or "") if ch.isalnum())
    return code[-4:] if code else "-"


def badge_values(event, subject: Subject, *, door: Optional[str] = None, printed_at: Optional[datetime] = None) -> Mapped:
    """`door` names the kiosk or door printing it; `printed_at` is now when a
    card is printed at a door (a batch printed ahead has neither)."""
    start = event.event_datetime
    first, last = _split(subject.full_name)
    badge_id = subject.admission_no or subject.ticket_code
    access = subject.ticket_label or ""
    checked = subject.checked_in_at or printed_at
    values = {
        "first_name": first,
        "last_name": last,
        "full_name": subject.full_name,
        "initials": initials_of(subject.full_name),
        "job_title": "",
        "organization": subject.organisation or "",
        "role": _role(subject).upper(),
        "access_level": access,
        "badge_id": badge_id,
        "event_name": event.name or "",
        "event_date": fmt_date(start),
        "event_year": _year(start),
        "event_venue": _venue(event),
        "event_tagline": "",
        "org_name": _club_name(event),
        "org_founded_year": "",
        "city": _city(event),
        "chapter": "",
        # The club-card designs, read as an event's card: the badge's id, the
        # ticket, the day it is valid, when they signed up.
        "membership_tier": access.upper(),
        "member_id": badge_id,
        "member_since_year": _year(subject.registered_at),
        "joined_date": fmt_month(subject.registered_at),
        "valid_thru": fmt_date(start),
        "valid_year": _year(start),
        "zone": "",
        "hall": "",
        "day_label": "",
        "doors_open_time": "",
        "session_hall": "",
        "session_time": "",
        "checkin_time": fmt_time(checked),
        "kiosk_id": door or "",
        "booth_no": "",
        "table_no": "",
        "track": "",
        "team_name": subject.team or "",
        # The race bib's big number: the admission number when the event
        # numbers its guests, else the ticket code's last four characters
        # (MOA-ZEN5Y-K6VRN -> K6VR... -> "6VRN"), never empty: the design
        # requires it, and an empty one dropped the card to the classic look.
        "bib_number": _bib(subject),
        "category": "",
        "wave": "",
        "start_time": fmt_time(start),
        **subject.answers,
    }
    return Mapped(values=values, images={"photo_url": ("photo", subject)}, qr_text=subject.ticket_code)


# ─── For the editor ─────────────────────────────────────────────────────────


def tokens_for(kind: str) -> tuple:
    return {"ticket": TICKET_TOKENS, "certificate": CERTIFICATE_TOKENS, "badge": BADGE_TOKENS}[kind]


def bindable(kind: str) -> list[dict]:
    """What `auto` means for each token of a kind (which of a design's tokens
    may be bound is the design's own `bindable`)."""
    meaning = {
        "tagline": "Hidden: events have no subtitle",
        "doors_time": "Hidden unless you set it",
        "gate": "Hidden: no seating", "row": "Hidden: no seating", "seat": "Hidden: no seating",
        "job_title": "Hidden unless you bind it", "organization": "The guest's organisation, from their app profile",
        "access_level": "The ticket's name", "badge_id": "The admission number, else the ticket code",
        "checkin_time": "When they checked in, or the time it prints at a door", "kiosk_id": "The door or kiosk printing it",
        "grade": "The competition placing, if any", "rank_or_grade": "The competition placing, if any",
        "signatory_name": "Hidden unless you set it", "signatory_designation": "Hidden unless you set it",
        "second_signatory_name": "Hidden unless you set it", "second_signatory_designation": "Hidden unless you set it",
        "duration_hours": "Hidden unless you set it", "expiry_date": "Hidden unless you set it",
        "bib_number": "The admission number, else the last four characters of the ticket code",
    }
    return [{"token": t, "auto": meaning.get(t)} for t in tokens_for(kind)]
