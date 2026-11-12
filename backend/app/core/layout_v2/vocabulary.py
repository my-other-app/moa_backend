"""What a v2 text layer can say (layout-v2 contract, V5).

The language is v1's (app.core.layout.tokens): {{token}}, the filters
upper / lower / title, and {% if token %}…{% else %}…{% endif %}. The
catalogue is v1's tokens plus every token the 35 designer designs print,
with the meanings app/api/doc_presets/values.py gives them (D5). A design
token is read as itself, never as a v1 alias of the same spelling: on a
badge {{role}} is ATTENDEE/SPEAKER/STAFF (the design's), not the ticket's
type (v1's alias).

`tokens(kind)` lists the additions for a kind; `known_tokens(kind)` is
everything a layout of that kind may name.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Mapping, Optional

from app.core.layout.tokens import (
    BY_NAME,
    FIELD_TOKEN,
    TICKET_TOKENS as V1_TOKENS,
    parse_ticket_template,
    render_template_nodes,
)

# key, group, label, sample, kinds ("t" ticket, "c" certificate, "b" badge)
_DESIGN_TOKENS: tuple[tuple[str, str, str, str, str], ...] = (
    # Tickets
    ("app_name", "Event", "Organiser (the club's name)", "EVENTLY", "t"),
    ("tagline", "Event", "Tagline (hidden unless set)", "An evening of live music", "t"),
    ("date", "Event", "Date", "14 Feb 2027", "t"),
    ("doors_time", "Event", "Doors open (hidden unless set)", "18:30", "t"),
    ("start_time", "Event", "Start time", "19:30", "tb"),
    ("gate", "Seating", "Gate (hidden without seating)", "B2", "t"),
    ("row", "Seating", "Row (hidden without seating)", "F", "t"),
    ("seat", "Seating", "Seat (hidden without seating)", "12", "t"),
    ("ticket_type", "Ticket", "Ticket type", "VIP", "t"),
    ("ticket_id", "Ticket", "Ticket code", "TK-48213", "t"),
    ("attendee_name", "Guest", "Guest name", "Asha Nair", "t"),
    ("venue", "Event", "Venue (or Online)", "Tagore Theatre, Trivandrum", "t"),
    ("event_name", "Event", "Event name", "Sunset Sessions", "tb"),
    # Certificates
    ("recipient_name", "Guest", "Recipient's name", "Ananya R. Menon", "c"),
    ("institute_name", "Club", "Issuer (the club's name)", "Sample Institute of Technology", "c"),
    ("certificate_id", "Certificate", "Certificate number", "CERT-2026-000123", "c"),
    ("verify_url", "Certificate", "Verify address (what the QR opens)", "https://example.com/verify/CERT-2026-000123", "c"),
    ("course_title", "Event", "Programme (the event's name)", "Full Stack Web Development", "c"),
    ("event_title", "Event", "Event name", "Robotics Weekend Workshop", "c"),
    ("event_date", "Event", "Event date", "8 November 2026", "cb"),
    ("issue_date", "Certificate", "Issued on", "12 November 2026", "c"),
    ("issue_year", "Certificate", "Year of issue", "2026", "c"),
    ("start_date", "Event", "Start date", "1 June 2026", "c"),
    ("end_date", "Event", "End date", "30 October 2026", "c"),
    ("completion_date", "Event", "Completed on (the event's end)", "30 October 2026", "c"),
    ("issue_city", "Event", "City of issue (the event's city)", "Trivandrum", "c"),
    ("grade", "Certificate", "Grade (the placing, if any)", "A+", "c"),
    ("rank_or_grade", "Certificate", "Rank or grade (the placing, if any)", "First Rank", "c"),
    ("signatory_name", "Signatories", "Signatory (hidden unless set)", "Dr. Sample Name", "c"),
    ("signatory_designation", "Signatories", "Signatory's title (hidden unless set)", "Director", "c"),
    ("second_signatory_name", "Signatories", "Second signatory (hidden unless set)", "Sample Name", "c"),
    ("second_signatory_designation", "Signatories", "Second signatory's title (hidden unless set)", "Course Lead", "c"),
    ("duration_hours", "Certificate", "Duration in hours (hidden unless set)", "120", "c"),
    ("expiry_date", "Certificate", "Valid until (hidden unless set)", "12 November 2029", "c"),
    # Badges
    ("first_name", "Guest", "First name", "Meera", "b"),
    ("last_name", "Guest", "Last name", "Pillai", "b"),
    ("full_name", "Guest", "Full name", "Meera Pillai", "b"),
    ("initials", "Guest", "Initials", "MP", "b"),
    ("job_title", "Guest", "Job title (hidden unless bound)", "Product Designer", "b"),
    ("organization", "Guest", "Their organisation", "Acme Labs", "b"),
    ("role", "Badge", "Role: ATTENDEE, SPEAKER or STAFF", "ATTENDEE", "b"),
    ("access_level", "Badge", "Access (the ticket's name)", "All-access", "b"),
    ("badge_id", "Badge", "Badge number (admission number, else ticket code)", "A-0417", "b"),
    ("event_year", "Event", "Event year", "2026", "b"),
    ("event_venue", "Event", "Venue (or Online)", "Trivandrum", "b"),
    ("event_tagline", "Event", "Tagline (hidden unless set)", "48h build", "b"),
    ("org_name", "Club", "Club name", "Makers Club", "b"),
    ("org_founded_year", "Club", "Club founded (hidden unless set)", "2019", "b"),
    ("city", "Event", "City", "Kochi", "b"),
    ("chapter", "Club", "Chapter (hidden unless set)", "Design Circle", "b"),
    ("membership_tier", "Badge", "Tier (the ticket's name, upper case)", "GOLD MEMBER", "b"),
    ("member_id", "Badge", "Member number (the badge number)", "CL-2291", "b"),
    ("member_since_year", "Guest", "Registered in (year)", "2023", "b"),
    ("joined_date", "Guest", "Registered (month)", "Mar 2023", "b"),
    ("valid_thru", "Badge", "Valid on (the event date)", "12 / 2027", "b"),
    ("valid_year", "Badge", "Valid year", "2027", "b"),
    ("zone", "Badge", "Zone (hidden unless set)", "B2", "b"),
    ("hall", "Badge", "Hall (hidden unless set)", "HALL B", "b"),
    ("day_label", "Badge", "Day (hidden unless set)", "DAY 1", "b"),
    ("doors_open_time", "Event", "Doors open (hidden unless set)", "09:00", "b"),
    ("session_hall", "Badge", "Session hall (hidden unless set)", "Hall B", "b"),
    ("session_time", "Badge", "Session time (hidden unless set)", "11:30", "b"),
    ("checkin_time", "Badge", "Checked in at", "09:12", "b"),
    ("kiosk_id", "Badge", "Door or kiosk printing it", "03", "b"),
    ("booth_no", "Badge", "Booth (hidden unless set)", "C-14", "b"),
    ("table_no", "Badge", "Table (hidden unless set)", "27", "b"),
    ("track", "Badge", "Track (hidden unless set)", "FinTech", "b"),
    ("team_name", "Guest", "Team", "Null Pointers", "b"),
    ("bib_number", "Badge", "Bib number", "2047", "b"),
    ("category", "Badge", "Category (hidden unless set)", "10K", "b"),
    ("wave", "Badge", "Wave (hidden unless set)", "B", "b"),
)
_KIND = {"t": "ticket", "c": "certificate", "b": "badge"}
KINDS = ("ticket", "certificate", "badge", "agenda")


@lru_cache(maxsize=None)
def design_tokens(kind: str) -> frozenset[str]:
    """The designs' token names for a kind."""
    return frozenset(k for k, _g, _l, _s, kinds in _DESIGN_TOKENS if kind in {_KIND[c] for c in kinds})


def all_design_tokens() -> frozenset[str]:
    return frozenset(t[0] for t in _DESIGN_TOKENS)


def tokens(kind: str) -> list[dict]:
    """The catalogue additions for a kind: the designs' tokens, each with what
    it means and a sample. (The v1 catalogue, app.core.layout.tokens, stays.)"""
    out = []
    for key, group, label, sample, kinds in _DESIGN_TOKENS:
        ks = [_KIND[c] for c in kinds]
        if kind in ks:
            out.append({"key": key, "group": group, "label": label, "sample": sample, "kinds": ks,
                        "v1": key in BY_NAME})
    return out


def known_tokens(kind: str) -> frozenset[str]:
    """Every token name a layout of this kind may use: v1's keys and aliases,
    and the designs' tokens of the kind (field.<slug> is always allowed)."""
    return frozenset(BY_NAME) | design_tokens(kind)


def samples(kind: str) -> dict[str, str]:
    """A sample value for every token of a kind (the designers' samples over
    v1's), for the editor and the tests."""
    out = {t["key"]: t["sample"] for t in V1_TOKENS}
    out.update({k: s for k, _g, _l, s, kinds in _DESIGN_TOKENS if kind in {_KIND[c] for c in kinds}})
    return out


@lru_cache(maxsize=4096)
def _parsed(src: str, kind: str):
    return parse_ticket_template(src, set(design_tokens(kind)))


def parse(src: str, kind: str = "ticket"):
    """A text template parsed (v1's ParsedTemplate: nodes, errors, tokens)."""
    return _parsed(src, kind)


def fill(src: str, values: Mapping[str, str], kind: str = "ticket") -> str:
    """A text template filled. Values are plain text (escaping is the builder's)."""
    if "{" not in src:
        return src
    return render_template_nodes(_parsed(src, kind).nodes, _str_values(values))


def _str_values(values: Mapping[str, object]) -> dict:
    return {k: ("" if v is None else str(v)) for k, v in values.items()}


def present(name: str, values: Mapping[str, object], kind: str = "ticket") -> bool:
    """Whether a token has a value (after trimming), by any of its names."""
    key = canonical(name, kind) or name
    v = values.get(key)
    if v is None and key != name:
        v = values.get(name)
    return bool(str(v or "").strip())


def canonical(name: str, kind: str = "ticket") -> Optional[str]:
    n = name.strip().lower()
    if n in design_tokens(kind):
        return n
    if FIELD_TOKEN.match(n):
        return n
    return BY_NAME.get(n)


def show(show_if: Optional[str], values: Mapping[str, object], kind: str = "ticket") -> bool:
    """A layer's showIf: "a" (a has a value), "a b" (all), "a|b" (any)."""
    if not show_if:
        return True
    if "|" in show_if:
        return any(present(t, values, kind) for t in show_if.split("|") if t.strip())
    return all(present(t, values, kind) for t in show_if.split())


def show_if_names(show_if: Optional[str]) -> list[str]:
    if not show_if:
        return []
    return [t for t in show_if.replace("|", " ").split() if t]
