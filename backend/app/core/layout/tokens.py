"""What a ticket can say, and the small template language that says it.

A text layer holds a template such as
    {{event_time}}{% if event_end_time %} – {{event_end_time}}{% endif %}
Tokens are a fixed catalogue, not free-form: a typo is caught when the design
is saved, not discovered on five hundred printed tickets. The names other
ticketing tools use ({{name}}, {{ticket_code}}, {{event_title}}) are accepted
as aliases, so a Figma template made for one of them keeps working.

The language is deliberately tiny -- tokens, three filters, and if/else. No
expressions, no loops, nothing that can run.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.layout.admission import admission_number, certificate_number

TICKET_TOKENS: list[dict] = [
    {"key": "attendee_name", "group": "Guest", "label": "Guest name", "sample": "Ananya Iyer",
     "aliases": ["name", "guest_name", "full_name", "delegate_name", "participant_name"]},
    {"key": "first_name", "group": "Guest", "label": "First name", "sample": "Ananya"},
    {"key": "last_name", "group": "Guest", "label": "Last name", "sample": "Iyer"},
    {"key": "organization", "group": "Guest", "label": "Their organisation", "sample": "Sahyadri Systems",
     "aliases": ["company", "organisation", "institution", "college"]},
    {"key": "email", "group": "Guest", "label": "Email", "sample": "ananya@sahyadrisystems.in"},
    {"key": "seat", "group": "Guest", "label": "Seat label", "sample": "A-014", "aliases": ["seat_label", "seat_number"]},
    # The code under the QR: what the gate and the kiosk ask a guest to type
    # when the QR will not scan. The ticket's own id -- the gate does not take
    # an admission number (it answers INVALID and asks staff to check the
    # guest in by hand), so a ticket must never print one there.
    {"key": "ticket_code", "group": "Ticket", "label": "Ticket code", "sample": "MOA-7K3QF-X9PWM",
     "aliases": ["ticket_id", "ticket_number", "invite_code"],
     "note": "Typed at the gate or the kiosk when a QR will not scan."},
    {"key": "admission_no", "group": "Ticket", "label": "Admission number", "sample": "MOA-0041-KF26",
     "aliases": ["admission_number"],
     "note": "The guest's number in this event's sequence, for a smaller line of its own. Not accepted at the gate: the ticket code is."},
    {"key": "ticket_type", "group": "Ticket", "label": "Ticket type", "sample": "VIP",
     "aliases": ["ticket_name", "ticket_type_name", "pass_type", "role"]},
    {"key": "event_name", "group": "Event", "label": "Event name", "sample": "Builders Day", "aliases": ["event_title", "event"]},
    {"key": "event_date", "group": "Event", "label": "Date", "sample": "16 Sept 2026", "aliases": ["date", "event_start_date"]},
    {"key": "event_day", "group": "Event", "label": "Weekday", "sample": "Wednesday"},
    {"key": "event_time", "group": "Event", "label": "Start time", "sample": "09:00", "aliases": ["event_start_time", "start_time", "time"]},
    {"key": "event_end_time", "group": "Event", "label": "End time", "sample": "18:00", "aliases": ["end_time"]},
    {"key": "event_end_date", "group": "Event", "label": "End date", "sample": "16 Sept 2026",
     "note": "Empty when the event ends the day it starts."},
    {"key": "venue", "group": "Event", "label": "Venue", "sample": "KTPO Convention Centre, Bengaluru",
     "aliases": ["event_venue", "location", "event_location"]},
    {"key": "event_code", "group": "Event", "label": "Event code", "sample": "GBD26"},
    {"key": "organiser", "group": "Event", "label": "Organiser", "sample": "Google Developer Relations", "aliases": ["organizer", "host"]},
    {"key": "org_name", "group": "Club", "label": "Club name", "sample": "Yukthi", "aliases": ["club_name", "client_name", "org"]},
    {"key": "team_name", "group": "Guest", "label": "Team", "sample": "Team Nova", "aliases": ["team"]},
    {"key": "certificate_no", "group": "Certificate", "label": "Certificate number", "sample": "CERT-0041-KF26",
     "aliases": ["certificate_number", "certificate_id"],
     "note": "Unique per guest; the verification code the QR carries points at it."},
    {"key": "issued_date", "group": "Certificate", "label": "Issued on", "sample": "18 Sept 2026",
     "aliases": ["issue_date", "certificate_date"]},
    {"key": "checked_in_date", "group": "Certificate", "label": "Attended on", "sample": "16 Sept 2026",
     "note": "The day they checked in; the event date when they never did."},
    {"key": "placing", "group": "Certificate", "label": "Placing", "sample": "First", "aliases": ["position", "rank"]},
]

BY_NAME: dict[str, str] = {}
for _t in TICKET_TOKENS:
    BY_NAME[_t["key"]] = _t["key"]
    for _a in _t.get("aliases", []):
        BY_NAME[_a] = _t["key"]
TOKEN_BY_KEY = {t["key"]: t for t in TICKET_TOKENS}


def field_slug(label: str) -> str:
    """A guest-list column as a token: "T-Shirt Size" → t_shirt_size."""
    s = unicodedata.normalize("NFKD", label.lower())
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:40]


def field_token(label: str) -> str:
    return f"field.{field_slug(label)}"


FIELD_TOKEN = re.compile(r"^field\.[a-z0-9_]{1,40}$")


def canonical_token(name: str, extra: Optional[set] = None) -> Optional[str]:
    """The canonical key a written name stands for, or None when it is not a token."""
    n = name.strip().lower()
    if extra and n in extra:
        return n
    if FIELD_TOKEN.match(n):
        return n
    return BY_NAME.get(n)


TEMPLATE_FILTERS = ("upper", "lower", "title")


@dataclass
class ParsedTemplate:
    nodes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    tokens: list = field(default_factory=list)


def _distance(a: str, b: str) -> int:
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    return dp[len(a)][len(b)]


def _suggest(name: str) -> str:
    best = None
    for candidate in BY_NAME:
        d = _distance(name.lower(), candidate)
        if d <= 3 and (best is None or d < best[1]):
            best = (candidate, d)
    return f" Did you mean {{{{{best[0]}}}}}?" if best else ""


TAG = re.compile(r"\{\{([\s\S]*?)\}\}|\{%([\s\S]*?)%\}")
COND = re.compile(r"^if\s+(not\s+)?(?:\{\{\s*)?([A-Za-z0-9_.]+)(?:\s*\}\})?$", re.I)


def parse_ticket_template(src: str, extra: Optional[set] = None) -> ParsedTemplate:
    """Parse a layer's text. Errors are for the person designing the ticket,
    and the template still parses as far as it can so the canvas can show
    something while they fix it."""
    out = ParsedTemplate()
    tokens: set = set()
    stack: list = []  # open ifs: (node, in_else)

    def target() -> list:
        if not stack:
            return out.nodes
        node, in_else = stack[-1]
        return node["else"] if in_else else node["then"]

    def resolve(raw: str) -> Optional[str]:
        key = canonical_token(raw, extra)
        if not key:
            out.errors.append(f"{{{{{raw.strip()}}}}} is not a token.{_suggest(raw.strip())}")
        else:
            tokens.add(key)
        return key

    last = 0
    for m in TAG.finditer(src):
        if m.start() > last:
            target().append({"t": "text", "v": src[last:m.start()]})
        last = m.end()
        if m.group(1) is not None:
            parts = [s.strip() for s in m.group(1).split("|")]
            name, rest = parts[0], parts[1:]
            if not name:
                out.errors.append("An empty {{ }} needs a token name inside it.")
                continue
            filters = []
            for f in rest:
                if f.lower() in TEMPLATE_FILTERS:
                    filters.append(f.lower())
                else:
                    out.errors.append(f'"{f}" is not a filter. Use {", ".join(TEMPLATE_FILTERS)}.')
            key = resolve(name)
            if key:
                target().append({"t": "var", "key": key, "filters": filters})
            continue
        tag = (m.group(2) or "").strip()
        cond = COND.match(tag)
        if cond:
            key = resolve(cond.group(2)) or ""
            node = {"t": "if", "key": key, "negate": bool(cond.group(1)), "then": [], "else": []}
            target().append(node)
            stack.append((node, False))
        elif re.match(r"^else$", tag, re.I):
            if not stack:
                out.errors.append("{% else %} has no {% if %} before it.")
            elif stack[-1][1]:
                out.errors.append("An {% if %} can have only one {% else %}.")
            else:
                stack[-1] = (stack[-1][0], True)
        elif re.match(r"^endif$", tag, re.I):
            if not stack:
                out.errors.append("{% endif %} has no {% if %} to close.")
            else:
                stack.pop()
        else:
            out.errors.append(f"{{% {tag} %}} is not understood. Use {{% if token %}}, {{% else %}} and {{% endif %}}.")
    if last < len(src):
        target().append({"t": "text", "v": src[last:]})
    if stack:
        n = len(stack)
        out.errors.append(f"{'An' if n == 1 else n} {{% if %}} {'is' if n == 1 else 'are'} never closed with {{% endif %}}.")
    out.tokens = sorted(tokens)
    return out


def _title_case(s: str) -> str:
    return re.sub(r"(^|[\s\-/'])(\w)", lambda m: m.group(1) + m.group(2).upper(), s.lower())


def _apply_filters(v: str, filters: list) -> str:
    for f in filters:
        v = v.upper() if f == "upper" else v.lower() if f == "lower" else _title_case(v)
    return v


def render_template_nodes(nodes: list, values: dict) -> str:
    out = []
    for n in nodes:
        if n["t"] == "text":
            out.append(n["v"])
        elif n["t"] == "var":
            out.append(_apply_filters(values.get(n["key"], "") or "", n["filters"]))
        else:
            truthy = bool((values.get(n["key"]) or "").strip())
            out.append(render_template_nodes(n["then"] if truthy != n["negate"] else n["else"], values))
    return "".join(out)


def render_ticket_template(src: str, values: dict, extra: Optional[set] = None) -> str:
    return render_template_nodes(parse_ticket_template(src, extra).nodes, values)


PER_GUEST_TOKENS = {t["key"] for t in TICKET_TOKENS if t["group"] in ("Guest", "Certificate")} | {"admission_no", "ticket_code"}


def carries_per_guest_token(src: str) -> bool:
    """Whether text says something only one guest's card can say."""
    return any(k in PER_GUEST_TOKENS or k.startswith("field.") for k in parse_ticket_template(src).tokens)


# ─── Values ────────────────────────────────────────────────────────────────

# en-GB abbreviated months, as Intl writes them; "Sept" is the odd one.
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _in(d: datetime, tz: str) -> datetime:
    if d.tzinfo is None:
        d = d.replace(tzinfo=ZoneInfo("UTC"))
    return d.astimezone(ZoneInfo(tz))


def format_date(d: datetime, tz: str) -> str:
    d = _in(d, tz)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


_MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def format_agenda_day(d: datetime, tz: str) -> str:
    """A day heading as en-GB writes it: Saturday 11 October."""
    d = _in(d, tz)
    return f"{_DAYS[d.weekday()]} {d.day} {_MONTHS_LONG[d.month - 1]}"


def day_key(d: datetime, tz: str) -> str:
    """The calendar day, in the event's zone, that two sessions are compared on."""
    return _in(d, tz).strftime("%Y-%m-%d")


def at_wall_clock(base: datetime, tz: str, hour: int, minute: int = 0) -> datetime:
    """The instant that reads as the given wall-clock time in a zone, on the day of `base`."""
    return _in(base, tz).replace(hour=hour, minute=minute, second=0, microsecond=0)


def format_event_time(d: datetime, tz: str, hour12: bool = False) -> str:
    """A time as the event prints it: 09:00, or 9:00 AM when the event asks for it."""
    d = _in(d, tz)
    if hour12:
        h = d.hour % 12 or 12
        return f"{h}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    return f"{d.hour:02d}:{d.minute:02d}"


@dataclass
class TokenEvent:
    name: str
    code: str
    venue: Optional[str]
    organiser: Optional[str]
    starts_at: datetime
    ends_at: datetime
    timezone: str = "Asia/Kolkata"
    hour12: bool = False
    admission_prefix: Optional[str] = None
    admission_digits: int = 4
    admission_include_code: bool = True


@dataclass
class TokenAttendee:
    name: str
    organization: Optional[str] = None
    email: Optional[str] = None
    seat_label: Optional[str] = None
    custom_fields: dict = field(default_factory=dict)
    team: Optional[str] = None


@dataclass
class TokenCertificate:
    number: str
    issued_at: datetime
    checked_in_at: Optional[datetime] = None
    placing: str = ""


def ticket_token_values(
    event: TokenEvent,
    org_name: str,
    attendee: TokenAttendee,
    admission_no: str,
    ticket_type_name: str,
    certificate: Optional[TokenCertificate] = None,
    field_labels: Optional[dict] = None,
    ticket_code: Optional[str] = None,
) -> dict:
    """Every token's value for one ticket, formatted in the venue's time zone.

    `ticket_code` is the ticket's id, the code printed under the QR; without
    one (an older caller) it is the admission number, as it used to be.

    `field_labels` maps a custom field's stored key to its label, so an
    answer stored under "tshirt_size" is reachable as both field.tshirt_size
    and field.t_shirt_size -- the editor offers the label, the import may
    have stored the key.
    """
    tz = event.timezone
    name = attendee.name or attendee.seat_label or ""
    parts = name.strip().split()
    values = {
        "attendee_name": name,
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "organization": attendee.organization or "",
        "email": attendee.email or "",
        "seat": attendee.seat_label or "",
        "admission_no": admission_no,
        "ticket_code": ticket_code or admission_no,
        "ticket_type": ticket_type_name,
        "event_name": event.name,
        "event_date": format_date(event.starts_at, tz),
        "event_day": _DAYS[_in(event.starts_at, tz).weekday()],
        "event_time": format_event_time(event.starts_at, tz, event.hour12),
        "event_end_time": format_event_time(event.ends_at, tz, event.hour12),
        "event_end_date": "" if format_date(event.ends_at, tz) == format_date(event.starts_at, tz) else format_date(event.ends_at, tz),
        "venue": event.venue or "",
        "event_code": event.code,
        "organiser": event.organiser or "",
        "org_name": org_name,
        "team_name": attendee.team or "",
        "certificate_no": certificate.number if certificate else "",
        "issued_date": format_date(certificate.issued_at, tz) if certificate else "",
        "checked_in_date": format_date(certificate.checked_in_at or event.starts_at, tz) if certificate else "",
        "placing": certificate.placing if certificate else "",
    }
    for key, v in (attendee.custom_fields or {}).items():
        values[field_token(str(key))] = "" if v is None else str(v)
        label = (field_labels or {}).get(key)
        if label:
            values[field_token(label)] = "" if v is None else str(v)
    return values


def sample_token_values(event: TokenEvent, org_name: str, kind: str = "ticket",
                        fields: Optional[list] = None) -> dict:
    """Sample values for the editor: the event's own details, a made-up guest."""
    fmt = {"prefix": event.admission_prefix, "digits": event.admission_digits, "includeCode": event.admission_include_code}
    values = ticket_token_values(
        event, org_name,
        TokenAttendee(name="Ananya Iyer", organization="Sahyadri Systems", email="ananya@sahyadrisystems.in", team="Team Nova"),
        admission_number(fmt, event.code, 41, None),
        "General",
        certificate=TokenCertificate(
            number=certificate_number({"digits": event.admission_digits, "includeCode": event.admission_include_code}, event.code, 41),
            issued_at=datetime.now(ZoneInfo(event.timezone)),
            checked_in_at=event.starts_at,
            placing="First" if kind == "certificate" else "",
        ),
        ticket_code=TOKEN_BY_KEY["ticket_code"]["sample"],
    )
    for f in fields or []:
        values.setdefault(field_token(f), f"‹{f}›")
    return values
