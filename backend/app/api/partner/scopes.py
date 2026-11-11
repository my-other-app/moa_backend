"""The Integrations API's scopes, least privilege, in the order the
dashboard lists them: (what it lets a system do, in plain words; the longer line).

Attendee contact data is a scope of its own (`attendees:read`): without it
every person's name, email, phone and answers are left out of what the API
returns. Granting it asks the club to tick that its registration form tells
people their details are shared (India's DPDP Act 2023: the club is the data
fiduciary). `clubs:create` is a partner app's alone, and only when a
platform admin allowed it.
"""
SCOPES: dict[str, tuple[str, str]] = {
    "club:read": ("Read your club's profile", "Name, tagline, about, links and logo."),
    "club:write": ("Edit your club's profile", "Name, tagline, about, links and logo. Never ownership, the team or the plan."),
    "events:read": ("Read your events", "Every event, drafts included, with dates, place, capacity, price and ticket types."),
    "events:write": ("Create and edit events", "New events are drafts: nobody sees them until they are published."),
    "events:publish": ("Publish and cancel events", "Put a draft live, or call an event off (never one with online payments: that is done in the dashboard)."),
    "registrations:read": ("Read registrations", "Who is registered by id and status, their tickets and check-ins. Names and contacts need the next scope."),
    "registrations:write": ("Register people", "Add, correct and cancel registrations. Paid events need a payment taken outside MyOtherApp, if you allow that."),
    "attendees:read": ("See attendees' details", "Names, email addresses, phone numbers and form answers of the people who registered."),
    "checkins:write": ("Check people in", "Mark people as checked in at the door, and undo it."),
    "certificates:read": ("Read certificates", "Certificates issued for your events and whether they are valid."),
    "certificates:write": ("Issue certificates", "Issue certificates to people who registered or were checked in."),
    "webhooks:manage": ("Get told of changes", "Add the addresses MyOtherApp tells about changes, as they happen."),
    "clubs:create": ("Create clubs", "Set up new clubs on MyOtherApp, each waiting for the MyOtherApp team's approval."),
}

ALL = tuple(SCOPES)
#: What a club's own key may hold: everything but making clubs.
CLUB_SCOPES = tuple(s for s in ALL if s != "clubs:create")
#: What needs the DPDP tick from the club.
NEEDS_DISCLOSURE = frozenset({"attendees:read"})
PARTNER_ONLY = frozenset({"clubs:create"})


def parse(value) -> list[str]:
    """The known scopes in a space-separated string or a list, each once, in catalogue order."""
    if value is None:
        return []
    asked = set(value.split()) if isinstance(value, str) else {str(v) for v in value}
    return [s for s in ALL if s in asked]


def unknown(value) -> list[str]:
    if value is None:
        return []
    asked = set(value.split()) if isinstance(value, str) else {str(v) for v in value}
    return sorted(asked - set(ALL))


def join(scopes) -> str:
    wanted = set(scopes or ())
    return " ".join(s for s in ALL if s in wanted)


def catalogue(keys=ALL) -> list[dict]:
    """For a dashboard: each scope with its plain words."""
    return [
        {"key": k, "label": SCOPES[k][0], "description": SCOPES[k][1], "needs_disclosure": k in NEEDS_DISCLOSURE}
        for k in ALL if k in set(keys)
    ]
