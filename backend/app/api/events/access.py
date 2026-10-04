"""Who may act on an event.

The ownership rule was written out seven times across two service modules as
`if event.club.user_id != user_id: raise 403`. That is correct for a club, but
it is also why no admin could read an event they did not own: `ClubAuth`
already admits admins by role, so the request got through the router and then
failed the comparison, because an admin's user id is never a club's user id.

Centralising it means the rule is stated once and the admin exemption cannot
drift between call sites.
"""

from app.api.users.models import UserTypes
from app.response import CustomHTTPException


def is_admin_user(user) -> bool:
    """
    True for an admin account.

    `user_type` is a SQLAlchemy Enum column, so it is normally a UserTypes
    member -- but `use_enum_values` is set on some response models, which can
    hand back the raw string. Reading `.value` when it exists covers both.
    """
    value = getattr(user.user_type, "value", user.user_type)
    return value == UserTypes.admin.value


def ensure_event_access(
    event,
    user_id: int,
    as_admin: bool = False,
    message: str = "Not authorized to view this event",
) -> None:
    """
    Raise unless the caller may act on this event.

    `as_admin` defaults to False so that a caller which forgets to pass it
    fails closed -- the safe direction for an authorisation check.
    """
    if as_admin:
        return
    if event.club is None or event.club.user_id != user_id:
        raise CustomHTTPException(403, message=message)
