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


# Every member: their own membership, the invite pages (public anyway), and
# the AI assistants connected to the club -- each member sees and disconnects
# their own; owners and admins see and disconnect all (mcp.service decides).
_SELF = _rx(
    r"^/api/v1/clubs/access/me(?:/|$)",
    r"^/api/v1/clubs/access/invite/",
    r"^/api/v1/clubs/connected-apps(?:/|$)",
)
# The bell (app/api/club_alerts): what MyOtherApp told the club. Everyone
# at the club reads it, and marking a notice read is no change to the club.
_BELL = _rx(r"^/api/v1/clubs/alerts(?:/|$)")
# The access list itself: an admin may read it; only the owner changes it.
_ACCESS_LIST = _rx(r"^/api/v1/clubs/access/?$")
_ACCESS = _rx(r"^/api/v1/clubs/access(?:/|$)")
# The club account's own credentials and devices, and the account itself.
_OWNER_ACCOUNT = _rx(
    r"^/api/v1/auth/(?:change-password|2fa|sessions|security|admin)",
    r"^/api/v1/user(?:/|$)",
)
_OWNER_WRITES = _rx(r"^/api/v1/clubs/subscription")
# Where the club's money goes, and the keys to its Razorpay account: changed
# by the owner alone (OWNER_ONLY). Read like the rest of the money: admins
# only, and never with the webhook secret (payment_gateways.router).
_GATEWAY = _rx(r"^/api/v1/clubs/payments/gateway(?:/|$)")
# The club's own domain for its website: DNS records, a paid feature, and the
# address every visitor sees. Admins, read and write; nobody else, not even to look.
_SITE_DOMAIN = _rx(r"^/api/v1/clubs/site/domain(?:/|$)")
# The club's own AI key: its provider account, which it pays for. The owner
# and admins, read and write; nobody else. (AI assist itself falls to the
# defaults: drafting and writing are changes, for editors and admins; the
# status and an event's feedback summary are reads, door staff included.)
_AI_KEY = _rx(r"^/api/v1/clubs/ai/key(?:/|$)")
# The club's own WhatsApp number (app/api/club_whatsapp). Its connection --
# the keys to the club's Meta account, routing, retention -- is changed by the
# owner and admins. Everything else there (reading the connection's status,
# the inbox, the templates) reaches the route whatever the role: what decides
# is the WhatsApp permission the owner gives a member (club_members.whatsapp),
# which the routes check (club_whatsapp.access), since a rule here sees only
# the role.
_WHATSAPP_ACCOUNT = _rx(r"^/api/v1/clubs/whatsapp/account(?:/|$)")
_WHATSAPP = _rx(r"^/api/v1/clubs/whatsapp(?:/|$)")
# The club's mailbox (app/api/club_mailbox). Its address and settings (the
# handle, retention, the name it sends as) are changed by the owner and
# admins. Everything else there (the mail itself) reaches the route whatever
# the role: the Mail permission the owner gives a member (club_members.mail)
# decides, which the routes check (club_mailbox.access).
_MAILBOX_SETUP = _rx(r"^/api/v1/clubs/mailbox/(?:address|settings)(?:/|$)")
_MAILBOX = _rx(r"^/api/v1/clubs/mailbox(?:/|$)")

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
# The club's store catalogue is the events' setup too (what they offer comes
# from it), so it is the same, said here so no club-wide rule ever takes it:
# editors and admins make, change, photograph and delete items; door staff read.
_CLUB_STORE = _rx(r"^/api/v1/clubs/store(?:/|$)")

# The only changes door staff make: letting people in and handing things over.
_DOOR_WRITES = _rx(
    r"^/api/v1/events/\d+/gate/",
    r"^/api/v1/events/registration/\d+/attendance/",
    # A handout item's id is a uuid (merch_items.id), never a number.
    r"^/api/v1/events/\d+/merch/[0-9A-Fa-f-]{1,36}/handout(?:/undo)?$",
    r"^/api/v1/events/\d+/store/orders/[^/]+/handover(?:/undo)?$",
    r"^/api/v1/events/volunteer/(?:checkin|validate)/",
    r"^/api/v1/events/\d+/tickets/\d+/send$",
    r"^/api/v1/notifications/read",
)


def allows(role: str, method: str, path: str) -> bool:
    method = method.upper()
    if _SELF.search(path) or _BELL.search(path):
        return True
    if _OWNER_ACCOUNT.search(path):
        return False
    if _GATEWAY.search(path):
        return role == "admin" and method == "GET"
    if _WHATSAPP_ACCOUNT.search(path):
        return role == "admin" or method not in WRITES
    if _WHATSAPP.search(path):
        return True
    if _MAILBOX_SETUP.search(path):
        return role == "admin" or method not in WRITES
    if _MAILBOX.search(path):
        return True
    if _ACCESS.search(path):
        return role == "admin" and method == "GET" and bool(_ACCESS_LIST.search(path))
    if method in WRITES and _OWNER_WRITES.search(path):
        return False
    if _SITE_DOMAIN.search(path) or _AI_KEY.search(path):
        return role == "admin"
    if role == "admin":
        return True
    if _CLUB_STORE.search(path):
        return role == "editor" or (role == "door" and method not in WRITES)
    if _MONEY.search(path):
        return False
    if role == "editor":
        if method in WRITES and (_CLUB_PROFILE_WRITES.search(path) or _DESTRUCTIVE.search(path)):
            return False
        return True
    if role == "door":
        return method not in WRITES or bool(_DOOR_WRITES.search(path))
    return False


def owner_only(method: str, path: str) -> bool:
    """A change no member makes, whatever their role (refused as OWNER_ONLY)."""
    return method.upper() in WRITES and bool(_GATEWAY.search(path))


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
        "club_store": editor,
        "delete_events": admin,
        "door": True,
        "payment_gateway": owner,
        "site_domain": admin,
        "ai_key": admin,
        # The club's own WhatsApp number's connection. The inbox is a
        # permission of its own (`whatsapp_inbox`, from the member's row).
        "whatsapp_account": admin,
        # The club's mailbox's address and settings. Reading and sending mail
        # is a permission of its own (`mail_inbox`, from the member's row).
        "mailbox_manage": admin,
        # Every connected AI assistant, not only one's own (app/api/mcp).
        "connected_apps": admin,
        # Approving what an assistant asked for: publishing, sending.
        "approve_actions": editor,
    }


# ─── Connected AI assistants (app/api/mcp) ──────────────────────────────
#
# An assistant acts as the person who connected it, and only as far as that
# person's role goes *now*: each tool stands in for the dashboard request
# that does the same thing, and a role may use the tool exactly when `allows`
# lets it make that request. So the rules above stay the one place roles are
# written down, and a tool can never do what its person could not do in the
# dashboard. An id in a path is any id: no rule depends on which event.
MCP_TOOLS = {
    "list_events": ("GET", "/api/v1/clubs/events/list"),
    "get_event": ("GET", "/api/v1/events/0/summary"),
    "event_stats": ("GET", "/api/v1/events/0/summary"),
    "list_registrations": ("GET", "/api/v1/events/registration/0/list"),
    "finance_summary": ("GET", "/api/v1/clubs/finance"),
    "list_store_items": ("GET", "/api/v1/clubs/store"),
    "list_feedback": ("GET", "/api/v1/events/ratings/0"),
    "list_team": ("GET", "/api/v1/clubs/access"),
    "create_event_draft": ("POST", "/api/v1/events/create"),
    "update_event_draft": ("PUT", "/api/v1/events/update/0"),
    "create_store_item": ("POST", "/api/v1/clubs/store"),
    "offer_store_items": ("PUT", "/api/v1/events/0/store/offers"),
    "draft_announcement": ("POST", "/api/v1/clubs/notifications/events/0/send"),
    "request_publish_event": ("PATCH", "/api/v1/events/0/closeout/settings"),
    "request_send_announcement": ("POST", "/api/v1/clubs/notifications/events/0/send"),
}

#: Each scope's dashboard request: a scope is offered at consent only to a
#: role that could use it. attendees:read is decided by `allows_contacts`.
MCP_SCOPES = {
    "events:read": ("GET", "/api/v1/clubs/events/list"),
    "events:write": ("POST", "/api/v1/events/create"),
    "finance:read": ("GET", "/api/v1/clubs/finance"),
    "store:read": ("GET", "/api/v1/clubs/store"),
    "store:write": ("POST", "/api/v1/clubs/store"),
    "comms:write": ("POST", "/api/v1/clubs/notifications/events/0/send"),
    "team:read": ("GET", "/api/v1/clubs/access"),
}

#: An event's money, as the event's own finance page reads it.
EVENT_MONEY = ("GET", "/api/v1/events/0/finance")
#: Approving a pending action is making the request it stands for.
MCP_ACTIONS = {
    "publish_event": ("PATCH", "/api/v1/events/0/closeout/settings"),
    "send_announcement": ("POST", "/api/v1/clubs/notifications/events/0/send"),
}


def allows_tool(role: str, tool: str) -> bool:
    """Whether this role may use this MCP tool now. The owner may use every one."""
    if role == "owner":
        return True
    route = MCP_TOOLS.get(tool)
    return route is not None and allows(role, *route)


def allows_contacts(role: str) -> bool:
    """Attendees' email addresses and phone numbers, handed to an assistant
    (and so to whoever runs it). Door staff see names at the gate; they do
    not export a list of contacts to a third party."""
    return role in ("owner", "admin", "editor")


def allows_scope(role: str, scope: str) -> bool:
    if scope == "attendees:read":
        return allows_contacts(role)
    if role == "owner":
        return scope in MCP_SCOPES
    route = MCP_SCOPES.get(scope)
    return route is not None and allows(role, *route)


def allows_event_money(role: str) -> bool:
    return role == "owner" or allows(role, *EVENT_MONEY)


def allows_action(role: str, kind: str) -> bool:
    """Whether this role may approve a pending action of this kind."""
    if role == "owner":
        return True
    route = MCP_ACTIONS.get(kind)
    return route is not None and allows(role, *route)
