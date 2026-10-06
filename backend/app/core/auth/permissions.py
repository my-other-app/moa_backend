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


def requires(capability: str):
    """
    Dependency factory: `admin: Annotated[Users, Depends(requires(DESTROY))]`.

    Layered on AdminAuth rather than replacing it, so the existing
    authentication, suspension and token-version checks all still run first.
    """

    async def checker(user: AdminAuth):
        if not can(user, capability):
            raise CustomHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                message=f"Your admin role cannot {capability.replace('_', ' ')}",
                error_code="INSUFFICIENT_ADMIN_ROLE",
            )
        return user

    return checker


DestroyAuth = Annotated[object, Depends(requires(DESTROY))]
MoneyAuth = Annotated[object, Depends(requires(MANAGE_MONEY))]
ModerateAuth = Annotated[object, Depends(requires(MODERATE))]
ImpersonateAuth = Annotated[object, Depends(requires(IMPERSONATE))]
ManageAdminsAuth = Annotated[object, Depends(requires(MANAGE_ADMINS))]
ContentAuth = Annotated[object, Depends(requires(MANAGE_CONTENT))]
ResetCredentialsAuth = Annotated[object, Depends(requires(RESET_CREDENTIALS))]
