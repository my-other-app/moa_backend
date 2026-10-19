"""The scopes an assistant can ask for, in the order the consent page lists them.

Each is (what the consent page says it can do, the longer line under it).
Which roles may grant which scope is club_access.policy's (`allows_scope`);
which tool needs which scope is in tools.py.
"""
SCOPES: dict[str, tuple[str, str]] = {
    "events:read": (
        "Read your events",
        "Events and drafts, their registrations and check-ins, form answers and feedback.",
    ),
    "events:write": (
        "Draft events",
        "Create and edit draft events, and ask you to publish one. Nothing goes live until you approve it.",
    ),
    "attendees:read": (
        "See attendees' contact details",
        "The email addresses and phone numbers of the people who registered.",
    ),
    "finance:read": (
        "Read your finances",
        "Ticket and store income, refunds, expenses and what is owed, by event and by month.",
    ),
    "store:read": (
        "Read your store",
        "The merchandise catalogue, its stock and which events offer each item.",
    ),
    "store:write": (
        "Add to your store",
        "Add items (kept off sale until you put them on sale) and offer items on draft events.",
    ),
    "comms:write": (
        "Draft announcements",
        "Write announcements to an event's people, and ask you to send one. Nothing is sent until you approve it.",
    ),
    "team:read": (
        "See your team",
        "Who can sign in to the club's dashboard, and their roles.",
    ),
}

ALL = tuple(SCOPES)


def parse(value: str | None) -> list[str]:
    """The known scopes in a space-separated string, each once, in catalogue order."""
    asked = set((value or "").split())
    return [s for s in ALL if s in asked]


def unknown(value: str | None) -> list[str]:
    return sorted(set((value or "").split()) - set(ALL))


def join(scopes) -> str:
    return " ".join(s for s in ALL if s in set(scopes))
