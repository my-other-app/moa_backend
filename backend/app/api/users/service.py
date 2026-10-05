from datetime import datetime, timezone
import io
import re
from fastapi import File, UploadFile
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from app.response import CustomHTTPException
from app.api.users.models import (
    UserAvatars,
    UserInterests,
    UserProfiles,
    UserTypes,
    Users,
)
from app.api.users.schemas import UserCreate
from app.core.auth.authentication import get_password_hash
from app.api.clubs.models import ClubUsersLink, Clubs
from app.api.orgs.models import Organizations
from app.core.validations.schema import validate_relations, validate_unique
from app.api.interests.models import Interests
from app.api.events.models import EventInterestsLink, EventRegistrationsLink, Events
from app.api.badges.models import UserBadges


async def generate_username(user: Users, session: AsyncSession, is_guest: bool = False):
    base_username = re.sub(
        r"[^a-zA-Z0-9_-]", "", user.full_name.lower().replace(" ", "_")
    )
    if is_guest:
        base_username = f"guest_{base_username}"

    if not base_username:
        base_username = "user"

    username = base_username
    count = 1

    while (
        await session.execute(select(exists().where(Users.username == username)))
    ).scalar():
        username = f"{base_username}_{count}"
        count += 1
    user.username = username
    return username


async def get_non_club_user_by_email(session: AsyncSession, email: str):
    query = (
        select(Users)
        .where(
            Users.email == email, 
            Users.user_type != UserTypes.club,
            Users.is_deleted == False,
        )
        .order_by(Users.id.desc())
        .limit(1)
    )
    return await session.scalar(query)


async def create_user(
    session: AsyncSession,
    full_name: str,
    email: str,
    phone: str | None = None,
    password: str | None = None,
    provider: str = "email",
    user_type: UserTypes = UserTypes.app_user,
):
    await validate_unique(
        session,
        unique_together=[
            {"email": (Users, email), "user_type": (Users, user_type)},
            {"phone": (Users, phone), "user_type": (Users, user_type)},
        ],
    )
    user_obj = Users(
        full_name=full_name,
        email=email,
        phone=phone,
        password=password,
        user_type=user_type,
        provider=provider,
    )
    if user_type != UserTypes.guest and provider == "email":
        if not password:
            raise CustomHTTPException(400, "Password is required")
        hashed_password = get_password_hash(user_obj.password)
        user_obj.password = hashed_password

    username = await generate_username(
        user_obj, session, is_guest=user_type == UserTypes.guest
    )
    user_obj.username = username

    session.add(user_obj)
    await session.commit()

    query = await session.execute(
        select(Users)
        .where(Users.username == username)
        .options(selectinload(Users.profile))
    )
    return query.scalar()


async def merge_guest_into_user(session: AsyncSession, app_user: Users) -> dict:
    """
    Merge any guest user accounts into the real app_user account.
    
    Transfers all data (registrations, ratings, payments) from guest users
    matching the app_user's email to the app_user, then soft-deletes the guest.
    
    Returns a summary dict with counts of merged records.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # Find guest users with the same email
    guest_users = await session.scalars(
        select(Users).where(
            Users.email == app_user.email,
            Users.user_type == UserTypes.guest,
            Users.is_deleted == False,
            Users.id != app_user.id,
        )
    )
    guest_users = list(guest_users)
    
    if not guest_users:
        return {"merged": False, "guests_merged": 0}
    
    total_registrations = 0
    total_ratings = 0
    total_payments = 0
    guests_merged = 0
    
    from app.api.events.models import EventRatingsLink
    from app.api.payments.models import PaymentOrders
    
    for guest in guest_users:
        guest_id = guest.id
        logger.info(f"Merging guest user {guest_id} into app_user {app_user.id} (email: {app_user.email})")
        
        # 1. Transfer EventRegistrationsLink records
        # First, find events where the app_user is already registered (to avoid unique constraint violations)
        app_user_event_ids = set(
            await session.scalars(
                select(EventRegistrationsLink.event_id).where(
                    EventRegistrationsLink.user_id == app_user.id,
                    EventRegistrationsLink.is_deleted == False,
                )
            )
        )
        
        guest_registrations = list(
            await session.scalars(
                select(EventRegistrationsLink).where(
                    EventRegistrationsLink.user_id == guest_id,
                    EventRegistrationsLink.is_deleted == False,
                )
            )
        )
        
        for reg in guest_registrations:
            if reg.event_id in app_user_event_ids:
                # Conflict: both accounts registered for the same event
                # Keep the app_user's registration, soft-delete the guest's
                reg.is_deleted = True
                logger.info(f"  Soft-deleted duplicate registration {reg.id} for event {reg.event_id}")
            else:
                # Transfer the registration to the app_user
                reg.user_id = app_user.id
                total_registrations += 1
                logger.info(f"  Transferred registration {reg.id} for event {reg.event_id}")
        
        # 2. Transfer EventRatingsLink records
        app_user_rated_event_ids = set(
            await session.scalars(
                select(EventRatingsLink.event_id).where(
                    EventRatingsLink.user_id == app_user.id,
                    EventRatingsLink.is_deleted == False,
                )
            )
        )
        
        guest_ratings = list(
            await session.scalars(
                select(EventRatingsLink).where(
                    EventRatingsLink.user_id == guest_id,
                    EventRatingsLink.is_deleted == False,
                )
            )
        )
        
        for rating in guest_ratings:
            if rating.event_id in app_user_rated_event_ids:
                rating.is_deleted = True
                logger.info(f"  Soft-deleted duplicate rating {rating.id} for event {rating.event_id}")
            else:
                rating.user_id = app_user.id
                total_ratings += 1
                logger.info(f"  Transferred rating {rating.id} for event {rating.event_id}")
        
        # 3. Transfer PaymentOrders records (no unique constraint to worry about)
        guest_payments = list(
            await session.scalars(
                select(PaymentOrders).where(
                    PaymentOrders.user_id == guest_id,
                    PaymentOrders.is_deleted == False,
                )
            )
        )
        
        for payment in guest_payments:
            payment.user_id = app_user.id
            total_payments += 1
            logger.info(f"  Transferred payment order {payment.id}")
        
        # 4. Soft-delete the guest user and any associated profile
        guest.is_deleted = True
        
        guest_profile = await session.scalar(
            select(UserProfiles).where(
                UserProfiles.user_id == guest_id,
                UserProfiles.is_deleted == False,
            )
        )
        if guest_profile:
            guest_profile.is_deleted = True
        
        guests_merged += 1
        logger.info(f"  Soft-deleted guest user {guest_id}")
    
    await session.flush()
    
    summary = {
        "merged": True,
        "guests_merged": guests_merged,
        "registrations_transferred": total_registrations,
        "ratings_transferred": total_ratings,
        "payments_transferred": total_payments,
    }
    logger.info(f"Account merge complete for user {app_user.id}: {summary}")
    return summary


async def following_clubs(
    session: AsyncSession, user_id: int, limit: int = 10, offset: int = 0
):
    """
    Clubs this user follows, in the shape ClubListResponse expects.

    This used to return bare Clubs rows, but ClubListResponse requires
    `followers_count`, `is_following` and `interests`, which a plain row does
    not carry -- so the endpoint raised a ValidationError and returned 500
    every single time it was called. The clubs service already assembles
    exactly that shape and takes `is_following` as a filter, so this delegates
    to it rather than keeping a second, broken projection of the same data.
    """
    user_exists = await session.scalar(
        select(exists().where(Users.id == user_id, Users.is_deleted == False))
    )
    if not user_exists:
        raise CustomHTTPException(status_code=404, message="User not found")

    # Imported here, not at module scope: clubs.service imports from this
    # module, and a top-level import would close the cycle.
    from app.api.clubs import service as clubs_service

    clubs, _total = await clubs_service.get_all_clubs(
        session,
        user_id=user_id,
        is_following=True,
        limit=limit,
        offset=offset,
    )
    # The caller counts separately via count_following_clubs, which the admin
    # route already uses for its own total.
    return clubs


async def create_user_profile(
    session: AsyncSession,
    user_id: int,
    full_name: str,
    whatsapp: str | None = None,
    org_id: str | None = None,
):
    await validate_relations(
        session,
        {
            "user_id": (Users, user_id),
            "org_id": (Organizations, org_id),
        },
    )

    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if profile:
        raise CustomHTTPException(400, "Profile already exists")
    else:
        await validate_unique(
            session,
            unique_together=[{"whatsapp": (UserProfiles, whatsapp)}],
        )
        profile = UserProfiles(
            user_id=user_id, org_id=org_id, whatsapp=whatsapp, full_name=full_name
        )
        session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


async def update_user_profile(
    session: AsyncSession,
    user_id: int,
    full_name: str,
    whatsapp: str | None = None,
    org_id: str | None = None,
):
    await validate_relations(
        session,
        {
            "user_id": (Users, user_id),
            "org_id": (Organizations, org_id),
        },
    )

    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    
    if not profile:
        # Create profile if it doesn't exist (e.g., for email login users)
        await validate_unique(
            session,
            unique_together=[{"whatsapp": (UserProfiles, whatsapp)}],
        )
        profile = UserProfiles(
            user_id=user_id, org_id=org_id, whatsapp=whatsapp, full_name=full_name
        )
        session.add(profile)
    else:
        # Update existing profile
        if profile.whatsapp != whatsapp:
            await validate_unique(
                session,
                unique_together=[{"whatsapp": (UserProfiles, whatsapp)}],
            )
        profile.whatsapp = whatsapp
        profile.org_id = org_id
        profile.full_name = full_name

    await session.commit()
    await session.refresh(profile)
    return profile



async def list_interests(session: AsyncSession, user_id: int):
    query = (
        select(Interests)
        .join(UserInterests, UserInterests.interest_id == Interests.id)
        .where(UserInterests.user_id == user_id)
        .options(joinedload(Interests.category))
    )
    return await session.scalars(query)


async def select_interests(
    session: AsyncSession, user_id: int, interest_ids: list[int]
):
    delete_exisisting = delete(UserInterests).where(UserInterests.user_id == user_id)
    await session.execute(delete_exisisting)
    for id in interest_ids:
        if await session.scalar(select(exists().where(Interests.id == id))):
            link = UserInterests(user_id=user_id, interest_id=id)
            session.add(link)
    await session.commit()
    return None


async def select_avatar(session: AsyncSession, user_id: int, avatar_id: int | None):
    await validate_relations(
        session, {"user_id": (Users, user_id), "avatar_id": (UserAvatars, avatar_id)}
    )
    user_profile = await session.scalar(
        select(UserProfiles)
        .where(UserProfiles.user_id == user_id, UserProfiles.is_deleted == False)
        .options(
            joinedload(UserProfiles.avatar),
        )
    )
    if not user_profile:
        raise CustomHTTPException(400, "user profile does not exists")

    user_profile.avatar_id = avatar_id
    await session.commit()
    await session.refresh(user_profile)
    return user_profile.avatar


async def get_user_profile(session: AsyncSession, user_id: int):
    await validate_relations(session, {"user_id": (Users, user_id)})

    query = (
        select(Users)
        .where(Users.id == user_id)
        .options(
            joinedload(Users.profile).options(
                joinedload(UserProfiles.avatar), joinedload(UserProfiles.org)
            ),
            selectinload(Users.interests),
        )
    )

    return await session.scalar(query)


async def admin_update_user(
    session: AsyncSession,
    user_id: int,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
):
    """
    Correct an account's details.

    Admins could suspend a user but not fix a typo'd email, which is the most
    common support request there is. Only the fields that were sent are
    applied -- unlike the club and event handlers, an omitted field is left
    alone rather than cleared.

    `username` is deliberately not editable: it is referenced in URLs and
    exports, and renaming it silently breaks links that already exist.
    """
    user = await session.scalar(
        select(Users).where(Users.id == user_id, Users.is_deleted == False)
    )
    if not user:
        raise CustomHTTPException(404, "User not found")

    if email and email != user.email:
        # (email, user_type) is unique, so a clash is a 500 from the database
        # unless it is caught here.
        clash = await session.scalar(
            select(Users).where(
                Users.email == email,
                Users.user_type == user.user_type,
                Users.id != user_id,
                Users.is_deleted == False,
            )
        )
        if clash:
            raise CustomHTTPException(400, "Another account already uses that email")
        user.email = email

    if full_name is not None:
        user.full_name = full_name
    if phone is not None:
        user.phone = phone or None

    await session.commit()
    return user


async def admin_delete_user(session: AsyncSession, user_id: int):
    """
    Soft-delete an account.

    Suspension is the reversible option and should be preferred; this exists
    for the cases where a record genuinely has to go. The token version is
    bumped so existing sessions stop immediately rather than running until
    they expire.
    """
    user = await session.scalar(
        select(Users).where(Users.id == user_id, Users.is_deleted == False)
    )
    if not user:
        raise CustomHTTPException(404, "User not found")

    user.soft_delete()
    user.token_version += 1
    await session.commit()
    return user


async def set_admin_role(session: AsyncSession, user_id: int, role):
    """
    Grant or change an admin role.

    Only meaningful for an admin account; setting it on anyone else would be
    a no-op the caller would reasonably expect to have worked.
    """
    from app.api.users.models import AdminRoles, UserTypes

    user = await session.scalar(
        select(Users).where(Users.id == user_id, Users.is_deleted == False)
    )
    if not user:
        raise CustomHTTPException(404, "User not found")

    user_type = getattr(user.user_type, "value", user.user_type)
    if user_type != UserTypes.admin.value:
        raise CustomHTTPException(
            400, "Only an admin account can have an admin role"
        )

    user.admin_role = role if isinstance(role, AdminRoles) else AdminRoles(role)
    await session.commit()
    return user


async def set_user_active(
    session: AsyncSession, user_id: int, is_active: bool, reason: str | None = None
):
    """
    Suspend or reinstate an account.

    Distinct from deletion on purpose: the only lever an admin had was
    soft-delete, which removes the account rather than restricting it and has
    no undo in the product. A suspended user keeps everything and can be let
    back in.

    Suspending also bumps `token_version`, which invalidates every token
    already issued -- without it the user would keep working until their
    current session expired, which is not what "suspended" means.
    """
    user = await session.scalar(
        select(Users).where(Users.id == user_id, Users.is_deleted == False)
    )
    if not user:
        raise CustomHTTPException(404, "User not found")

    user.is_active = is_active
    user.suspended_reason = reason if not is_active else None
    if not is_active:
        user.token_version += 1

    await session.commit()
    return user


async def get_user_admin_detail(session: AsyncSession, user_id: int):
    """
    One user with everything an admin needs to judge the account.

    The counts are aggregates rather than lengths of loaded collections: a
    user with two thousand registrations should not be pulled into memory to
    show the number 2000. Each list has its own paginated endpoint.
    """
    user = await session.scalar(
        select(Users)
        .where(Users.id == user_id, Users.is_deleted == False)
        .options(
            joinedload(Users.profile).options(
                joinedload(UserProfiles.avatar), joinedload(UserProfiles.org)
            ),
            selectinload(Users.interests),
        )
    )
    if not user:
        raise CustomHTTPException(404, "User not found")

    registrations = EventRegistrationsLink
    base = [registrations.user_id == user_id, registrations.is_deleted == False]

    async def count_registrations(*extra):
        return await session.scalar(
            select(func.count(registrations.id)).where(*base, *extra)
        )

    clubs_following = await session.scalar(
        select(func.count(ClubUsersLink.id)).where(
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_following == True,
        )
    )
    events_registered = await count_registrations()
    events_attended = await count_registrations(registrations.is_attended == True)
    events_won = await count_registrations(registrations.is_won == True)
    total_paid = await session.scalar(
        select(func.coalesce(func.sum(registrations.paid_amount), 0)).where(
            *base, registrations.is_paid == True
        )
    )
    badges_earned = await session.scalar(
        select(func.count(UserBadges.id)).where(UserBadges.user_id == user_id)
    )

    return user, {
        "clubs_following": clubs_following or 0,
        "events_registered": events_registered or 0,
        "events_attended": events_attended or 0,
        "events_won": events_won or 0,
        "badges_earned": badges_earned or 0,
        "total_paid": float(total_paid or 0),
    }


async def count_following_clubs(session: AsyncSession, user_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(ClubUsersLink.id)).where(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
            )
        )
    ) or 0


async def count_registered_events(
    session: AsyncSession,
    user_id: int,
    is_attended: bool | None = None,
    is_paid: bool | None = None,
    is_won: bool | None = None,
) -> int:
    """Companion to list_registered_events, so pages report a real total."""
    query = select(func.count(EventRegistrationsLink.id)).where(
        EventRegistrationsLink.user_id == user_id,
        EventRegistrationsLink.is_deleted == False,
    )
    if is_paid is not None:
        query = query.where(EventRegistrationsLink.is_paid == is_paid)
    if is_attended is not None:
        query = query.where(EventRegistrationsLink.is_attended == is_attended)
    if is_won is not None:
        query = query.where(EventRegistrationsLink.is_won == is_won)
    return (await session.scalar(query)) or 0


async def update_profile_picture(
    session: AsyncSession,
    user_id: int,
    profile_picture: UploadFile,
) -> dict:
    """Update user's profile picture."""
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if not profile:
        profile = UserProfiles(user_id=user_id)
        session.add(profile)

    if profile.profile_pic:
        profile.profile_pic.delete()

    content = io.BytesIO(await profile_picture.read())
    profile.profile_pic = {
        "bytes": content,
        "filename": profile_picture.filename,
    }
    await session.commit()
    await session.refresh(profile)
    return profile.profile_pic


async def create_user_avatar(session: AsyncSession, name: str, file: UploadFile):
    avatar = UserAvatars(name=name)
    avatar.image = {"bytes": io.BytesIO(await file.read()), "filename": file.filename}
    session.add(avatar)
    await session.commit()
    await session.refresh(avatar)
    return avatar


async def update_user_avatar(session: AsyncSession, id: int, name: str | None = None, file: UploadFile | None = None):
    avatar = await session.get(UserAvatars, id)
    if not avatar or getattr(avatar, 'is_deleted', False):
        raise CustomHTTPException(404, "Avatar not found")
        
    if name is not None:
        avatar.name = name
        
    if file is not None:
        if avatar.image:
            avatar.image.delete()
        avatar.image = {"bytes": io.BytesIO(await file.read()), "filename": file.filename}
        
    await session.commit()
    await session.refresh(avatar)
    return avatar


async def delete_user_avatar(session: AsyncSession, id: int):
    avatar = await session.get(UserAvatars, id)
    if not avatar or getattr(avatar, 'is_deleted', False):
        raise CustomHTTPException(404, "Avatar not found")
        
    avatar.soft_delete()
    await session.commit()
    return {"message": "Avatar deleted successfully"}


async def list_avatars(session: AsyncSession):
    query = select(UserAvatars)
    return await session.scalars(query)


async def delete_user(session: AsyncSession, user_id: int):
    await validate_relations(session, {"user_id": (Users, user_id)})
    user = await session.scalar(select(Users).where(Users.id == user_id))
    user.soft_delete()
    # delete user registrations
    follows = select(ClubUsersLink).where(ClubUsersLink.user_id == user_id)
    for follow in await session.scalars(follows):
        follow.soft_delete()
    registrations = select(EventRegistrationsLink).where(
        EventRegistrationsLink.user_id == user_id
    )
    for registration in await session.scalars(registrations):
        registration.soft_delete()
    interests = select(UserInterests).where(UserInterests.user_id == user_id)
    for interest in await session.scalars(interests):
        interest.soft_delete()
    profile = await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id)
    )
    if profile:
        profile.soft_delete()
    await session.commit()
    return None


async def list_registered_events(
    session: AsyncSession,
    user_id: int,
    is_attended: bool | None = None,
    is_paid: bool | None = None,
    is_won: bool | None = None,
    limit: int = 10,
    offset: int = 0,
):
    query = (
        select(EventRegistrationsLink)
        .where(
            EventRegistrationsLink.user_id == user_id,
            EventRegistrationsLink.is_deleted == False,
        )
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category)
            )
        )
        .limit(limit)
        .offset(offset)
    )
    if is_paid is not None:
        query = query.where(EventRegistrationsLink.is_paid == is_paid)
    if is_attended is not None:
        query = query.where(EventRegistrationsLink.is_attended == is_attended)
    if is_won is not None:
        query = query.where(EventRegistrationsLink.is_won == is_won)
    return (await session.scalars(query)).all()


async def update_fcm_token(
    session: AsyncSession,
    user_id: int,
    fcm_token: str,
    platform: str,
):
    """Store or update the user's FCM token for push notifications."""
    from app.api.users.models import UserDeviceTokens
    
    # Check if token already exists for this user/platform
    existing = await session.scalar(
        select(UserDeviceTokens).where(
            UserDeviceTokens.user_id == user_id,
            UserDeviceTokens.platform == platform,
        )
    )
    
    if existing:
        existing.fcm_token = fcm_token
        existing.is_deleted = False
    else:
        device_token = UserDeviceTokens(
            user_id=user_id,
            fcm_token=fcm_token,
            platform=platform,
        )
        session.add(device_token)
    
    await session.commit()

