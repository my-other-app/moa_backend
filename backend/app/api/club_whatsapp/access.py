"""Who may do what with a club's own WhatsApp number.

- The connection (its keys, its webhook, routing and retention): the club's
  own account and its admins -- like the club's own AI key and domain.
- The inbox and the templates (message content): the club's own account,
  and the team members the owner gave the WhatsApp permission
  (club_members.whatsapp), whatever their role. Nobody else -- not an
  editor without it, not a platform admin acting as the club. An admin sees
  whether a number is connected and its quality, never what it said.

club_access.policy lets every member's request on /clubs/whatsapp/... reach
the route (except the connection's writes, admins only), and the checks here
decide; the club's own account (no member) passes them all.
"""
from __future__ import annotations

from fastapi import Request

from app.api.club_access import service as club_access
from app.response import CustomHTTPException

PERMISSION_MESSAGE = "You need the WhatsApp permission to see the club's WhatsApp messages. Ask the club's owner."
MANAGE_MESSAGE = "Only the club's owner and its admins can change the WhatsApp connection."
PRIVATE_MESSAGE = "A club's WhatsApp messages are for its own team. Acting as the club, you can see the connection and its quality, not the messages."


def is_owner(request: Request) -> bool:
    return club_access.current_member(request) is None


def can_manage(request: Request) -> bool:
    member = club_access.current_member(request)
    return member is None or member.role == "admin"


def can_inbox(request: Request) -> bool:
    """Reads and answers the inbox: the owner, or a member with the permission.
    An admin impersonating the club has no member and is not the club's team."""
    from app.api.subscriptions import gates

    if gates.impersonated_by():
        return False
    member = club_access.current_member(request)
    return member is None or bool(getattr(member, "whatsapp", False))


def require_manage(request: Request) -> None:
    if not can_manage(request):
        raise CustomHTTPException(status_code=403, message=MANAGE_MESSAGE, error_code="ROLE_FORBIDDEN")


def require_inbox(request: Request) -> None:
    from app.api.subscriptions import gates

    if gates.impersonated_by():
        raise CustomHTTPException(status_code=403, message=PRIVATE_MESSAGE, error_code="WHATSAPP_PRIVATE")
    if not can_inbox(request):
        raise CustomHTTPException(status_code=403, message=PERMISSION_MESSAGE, error_code="WHATSAPP_PERMISSION")


def require_any(request: Request) -> None:
    """The connection's status and quality: those who manage it or use it."""
    if not (can_manage(request) or can_inbox(request)):
        raise CustomHTTPException(status_code=403, message=PERMISSION_MESSAGE, error_code="WHATSAPP_PERMISSION")


def actor_name(request: Request, user) -> str:
    member = club_access.current_member(request)
    if member is not None:
        return (member.full_name or member.email or "A team member")[:120]
    return (getattr(user, "full_name", None) or "The club")[:120]
