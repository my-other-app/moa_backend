"""What each role may do, decided from the request alone: method and path.

Checked in the auth dependency for every request a member's session makes, so
a route needs no change to be covered, and a route added later is covered too:
admins and editors are allowed unless a rule here says otherwise, door staff
are refused any change unless a rule here allows it. The club's own account
(the owner) never reaches this — it has no member id.

Keep `can()` in step with the rules: the dashboard hides what a role cannot
open from it, and the server refusing is what actually holds.
"""
import re

ROLE_INFO = {
    "owner": ("Owner", "The club's own account. Everything, including who can sign in."),
    "admin": ("Admin", "Everything except changing who can sign in."),
    "editor": ("Editor", "Events, attendees, messages, designs and the website. No money and no club profile."),
    "door": ("Door", "Check-ins, the gate and hand-overs on the day. Sees events and attendees; changes nothing else."),
}

WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


# Every member: their own membership, the invite pages (public anyway).
_SELF = _rx(r"^/api/v1/clubs/access/me(?:/|$)", r"^/api/v1/clubs/access/invite/")
# The access list itself: an admin may read it; only the owner changes it.
_ACCESS_LIST = _rx(r"^/api/v1/clubs/access/?$")
_ACCESS = _rx(r"^/api/v1/clubs/access(?:/|$)")
# The club account's own credentials and devices, and the account itself.
_OWNER_ACCOUNT = _rx(
    r"^/api/v1/auth/(?:change-password|2fa|sessions|security|admin)",
    r"^/api/v1/user(?:/|$)",
)
_OWNER_WRITES = _rx(r"^/api/v1/clubs/subscription")

# Money: figures, statements, the ledger, refunds.
_MONEY = _rx(
    r"^/api/v1/clubs/finance",
    r"^/api/v1/events/\d+/finance",
    r"^/api/v1/payments/logs",
)
# The club's public face and setup.
_CLUB_PROFILE_WRITES = _rx(
    r"^/api/v1/clubs/(?:update|cover|logo|socials|create)(?:/|$)",
    r"^/api/v1/clubs/(?:partners|team)(?:/|$)",
)
# Things that cannot be taken back.
_DESTRUCTIVE = _rx(
    r"^/api/v1/events/delete/\d+$",
    r"^/api/v1/events/\d+/closeout/retention/run$",
)
# An event's own setup that is not money or profile (its agenda, its organisers)
# falls to the defaults: door staff may read it, editors and admins change it.

# The only changes door staff make: letting people in and handing things over.
_DOOR_WRITES = _rx(
    r"^/api/v1/events/\d+/gate/",
    r"^/api/v1/events/registration/\d+/attendance/",
    r"^/api/v1/events/\d+/merch/\d+/handout(?:/undo)?$",
    r"^/api/v1/events/\d+/store/orders/[^/]+/handover(?:/undo)?$",
    r"^/api/v1/events/volunteer/(?:checkin|validate)/",
    r"^/api/v1/events/\d+/tickets/\d+/send$",
    r"^/api/v1/notifications/read",
)


def allows(role: str, method: str, path: str) -> bool:
    method = method.upper()
    if _SELF.search(path):
        return True
    if _OWNER_ACCOUNT.search(path):
        return False
    if _ACCESS.search(path):
        return role == "admin" and method == "GET" and bool(_ACCESS_LIST.search(path))
    if method in WRITES and _OWNER_WRITES.search(path):
        return False
    if role == "admin":
        return True
    if _MONEY.search(path):
        return False
    if role == "editor":
        if method in WRITES and (_CLUB_PROFILE_WRITES.search(path) or _DESTRUCTIVE.search(path)):
            return False
        return True
    if role == "door":
        return method not in WRITES or bool(_DOOR_WRITES.search(path))
    return False


def can(role: str) -> dict:
    """What the dashboard should offer this role. Mirrors `allows`."""
    owner = role == "owner"
    admin = owner or role == "admin"
    editor = admin or role == "editor"
    return {
        "manage_access": owner,
        "see_access": admin,
        "money": admin,
        "club_profile": admin,
        "edit_events": editor,
        "delete_events": admin,
        "door": True,
    }
