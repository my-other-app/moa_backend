"""Who may do what with a club's mailbox.

- Its address and settings (the handle, the name it sends as, retention):
  the club's own account and its admins.
- The mail (reading, answering, writing, the signature, export): the club's
  own account and the team members the owner gave the Mail permission
  (club_members.mail), whatever their role. Nobody else -- not an editor
  without it, not a platform admin acting as the club, who sees whether the
  mailbox is set up and its counts, never what any mail says.

club_access.policy lets every member's request on /clubs/mailbox/... reach
the route (except the address's and settings' writes, admins only); the
checks here decide. The club's own account (no member) passes them all.
"""
from __future__ import annotations

from fastapi import Request

from app.api.club_access import service as club_access
from app.response import CustomHTTPException

PERMISSION_MESSAGE = "You need the Mail permission to see the club's mail. Ask the club's owner."
MANAGE_MESSAGE = "Only the club's owner and its admins can change the club's address and mailbox settings."
PRIVATE_MESSAGE = "A club's mail is for its own team. Acting as the club, you can see whether its mailbox is set up, not its mail."
OWNER_MESSAGE = "Only the club's own account can delete or export all of its mail."


def _impersonated() -> bool:
    from app.api.subscriptions import gates

    return bool(gates.impersonated_by())


def impersonated() -> bool:
    """An admin acting as the club: the page says why the mail is closed to them."""
    return _impersonated()


def is_owner(request: Request) -> bool:
    return club_access.current_member(request) is None and not _impersonated()


def can_manage(request: Request) -> bool:
    member = club_access.current_member(request)
    return member is None or member.role == "admin"


def can_mail(request: Request) -> bool:
    """Reads and sends the club's mail: the owner, or a member with the
    permission. An admin impersonating the club has no member and is not
    the club's team."""
    if _impersonated():
        return False
    member = club_access.current_member(request)
    return member is None or bool(getattr(member, "mail", False))


def require_manage(request: Request) -> None:
    if not can_manage(request):
        raise CustomHTTPException(status_code=403, message=MANAGE_MESSAGE, error_code="ROLE_FORBIDDEN")


def require_mail(request: Request) -> None:
    if _impersonated():
        raise CustomHTTPException(status_code=403, message=PRIVATE_MESSAGE, error_code="MAILBOX_PRIVATE")
    if not can_mail(request):
        raise CustomHTTPException(status_code=403, message=PERMISSION_MESSAGE, error_code="MAILBOX_PERMISSION")


def require_owner(request: Request) -> None:
    """The whole mailbox's export or deletion: the club's own account alone."""
    if _impersonated():
        raise CustomHTTPException(status_code=403, message=PRIVATE_MESSAGE, error_code="MAILBOX_PRIVATE")
    if not is_owner(request):
        raise CustomHTTPException(status_code=403, message=OWNER_MESSAGE, error_code="OWNER_ONLY")


def require_any(request: Request) -> None:
    """The mailbox's status: those who manage it or use it."""
    if not (can_manage(request) or can_mail(request)):
        raise CustomHTTPException(status_code=403, message=PERMISSION_MESSAGE, error_code="MAILBOX_PERMISSION")


def actor_name(request: Request, user) -> str:
    member = club_access.current_member(request)
    if member is not None:
        return (member.full_name or member.email or "A team member")[:120]
    return (getattr(user, "full_name", None) or "The club")[:120]


def actor_ids(request: Request, user) -> tuple:
    """(user id, member id) of who is acting: the club's own account, or a member."""
    member = club_access.current_member(request)
    if member is not None:
        return None, member.id
    return getattr(user, "id", None), None
