"""What a co-host may not do on an event it co-hosts, decided from the request
alone: its method and path (docs/COHOSTS.md §Permissions).

A co-host's team may do on the event everything the host's team may -- each
member as far as their own role goes (club_access.policy) -- except the
host's own decisions, listed here. A route added later under an event is
allowed to a co-host unless a rule here takes it, and the route table in
tests/test_cohost_routes.py fails until the new route is classified.
"""
import re

WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


#: Changes only the host makes.
_HOST_ONLY_WRITES = _rx(
    # Money out: a refund, and retrying one.
    r"^/api/v1/events/\d+/payments/[^/]+/refund$",
    r"^/api/v1/events/\d+/refunds(?:/|$)",
    # Cancelling the event refunds everyone; and its retry.
    r"^/api/v1/events/\d+/cancel(?:/|$)",
    # Deleting it.
    r"^/api/v1/events/delete/\d+$",
    # A copy is a new event of the host club.
    r"^/api/v1/events/\d+/duplicate$",
    # Scrubbing every attendee's data now, which cannot be taken back.
    r"^/api/v1/events/\d+/closeout/retention/run$",
    # Who else co-hosts: inviting, cancelling an invite, removing a co-host.
    r"^/api/v1/events/\d+/cohosts/?$",
    r"^/api/v1/events/\d+/cohosts/\d+$",
)
#: Reads only the host makes: looking for clubs to invite.
_HOST_ONLY_READS = _rx(r"^/api/v1/events/\d+/cohosts/search$")

HOST_ONLY_MESSAGE = "Only {host}, the host, can do this."


def host_only(method: str, path: str) -> bool:
    """True when a co-host is refused this request (403 COHOST_HOST_ONLY)."""
    method = method.upper()
    if method in WRITES:
        return bool(_HOST_ONLY_WRITES.search(path))
    return bool(_HOST_ONLY_READS.search(path))
