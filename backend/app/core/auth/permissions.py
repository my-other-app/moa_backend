"""What each admin role may do.

`user_type` answers "is this an admin"; this answers "which kind". Keeping the
two separate means roles did not need a second auth system bolted on, and an
endpoint opts in by declaring the capability it needs rather than listing
roles -- so adding a role later does not mean auditing every route.
"""

from typing import Annotated

from fastapi import Depends, status

from app.api.users.models import AdminRoles
from app.core.auth.dependencies import AdminAuth
from app.response import CustomHTTPException

# Capabilities, not roles, at the call site.
VIEW = "view"
MODERATE = "moderate"
MANAGE_MONEY = "manage_money"
DESTROY = "destroy"
IMPERSONATE = "impersonate"
# Editing accounts and granting admin roles. Owner-only: anyone who can
# hand out `owner` can grant themselves anything, so this is the capability
# that gates every other one.
MANAGE_ADMINS = "manage_admins"
# The catalogue: interests, categories, avatars, badge definitions, and the
# club and organisation records themselves. Separate from MODERATE because
# approving one club is a decision about one club, while renaming an interest
# changes what every profile in the product displays. Separate from DESTROY so
# the refusal can say "cannot manage content" rather than claiming a create
# was a deletion.
MANAGE_CONTENT = "manage_content"
# Setting another user's password, which is account takeover by another name.
RESET_CREDENTIALS = "reset_credentials"
# Taking or downloading a database snapshot. Its own capability because it is
# the broadest action the API has: one response carries every user's details,
# every payment and every password hash.
EXPORT_DATA = "export_data"
# A push to every device on the platform. Owner-only: it reaches every person
# who ever installed the app, at once.
BROADCAST = "broadcast"

ROLE_CAPABILITIES: dict[AdminRoles, set[str]] = {
    AdminRoles.viewer: {VIEW},
    AdminRoles.support: {VIEW, MODERATE},
    AdminRoles.owner: {
        VIEW,
        MODERATE,
        MANAGE_MONEY,
        DESTROY,
        IMPERSONATE,
        MANAGE_ADMINS,
        MANAGE_CONTENT,
        RESET_CREDENTIALS,
        EXPORT_DATA,
        BROADCAST,
    },
}


def role_of(user) -> AdminRoles:
    """
    Defaults to owner for anything unset.

    Existing admins predate this column, and the alternative -- defaulting to
    viewer -- would have locked the platform out of itself on deploy.
    """
    value = getattr(user, "admin_role", None)
    if value is None:
        return AdminRoles.owner
    return value if isinstance(value, AdminRoles) else AdminRoles(value)


def can(user, capability: str) -> bool:
    return capability in ROLE_CAPABILITIES.get(role_of(user), set())


def ensure_can(user, capability: str) -> None:
    """403 INSUFFICIENT_ADMIN_ROLE unless this admin's role allows `capability`."""
    if not can(user, capability):
        raise CustomHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            message=f"Your admin role cannot {capability.replace('_', ' ')}",
            error_code="INSUFFICIENT_ADMIN_ROLE",
        )


# Reading a club's records is VIEW; changing them is MANAGE_CONTENT, the
# capability that editing the club record itself takes (PUT /clubs/admin/{id});
# deleting is DESTROY, as deleting a club is.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def capability_for_method(method: str) -> str:
    """What an admin needs to use a club's own route with this HTTP method."""
    method = (method or "GET").upper()
    if method in _SAFE_METHODS:
        return VIEW
    if method == "DELETE":
        return DESTROY
    return MANAGE_CONTENT


def ensure_admin_can(user, capability: str) -> None:
    """
    For a route a club uses on its own records and an admin may use on any
    club's: the admin must hold `capability`. A club passes untouched -- its
    access is the ownership check the route already makes.

    The club-only auth dependency (ClubAuth) already holds an admin to the
    capability its HTTP method implies (`capability_for_method`); a route
    calls this only to ask for more, as a download of a whole guest list
    asks for EXPORT_DATA.
    """
    from app.api.events.access import is_admin_user

    if user is not None and is_admin_user(user):
        ensure_can(user, capability)


def requires(capability: str):
    """
    Dependency factory: `admin: Annotated[Users, Depends(requires(DESTROY))]`.

    Layered on AdminAuth rather than replacing it, so the existing
    authentication, suspension and token-version checks all still run first.
    """

    async def checker(user: AdminAuth):
        ensure_can(user, capability)
        return user

    return checker


DestroyAuth = Annotated[object, Depends(requires(DESTROY))]
MoneyAuth = Annotated[object, Depends(requires(MANAGE_MONEY))]
ModerateAuth = Annotated[object, Depends(requires(MODERATE))]
ImpersonateAuth = Annotated[object, Depends(requires(IMPERSONATE))]
ManageAdminsAuth = Annotated[object, Depends(requires(MANAGE_ADMINS))]
ContentAuth = Annotated[object, Depends(requires(MANAGE_CONTENT))]
ResetCredentialsAuth = Annotated[object, Depends(requires(RESET_CREDENTIALS))]
ExportDataAuth = Annotated[object, Depends(requires(EXPORT_DATA))]
BroadcastAuth = Annotated[object, Depends(requires(BROADCAST))]
