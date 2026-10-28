import asyncio

import bcrypt
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import func, select
from app.api.users.models import UserTypes, Users
from app.db.core import SessionDep
from app.api.clubs.models import Clubs
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

ALGORITHM = "HS256"


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


#: A bcrypt hash of nothing anyone types, at the same cost as real ones. A
#: sign-in for an address with no account checks the password against it, so
#: "no such account" takes as long as "wrong password" and the time a refusal
#: takes does not say which addresses have accounts.
DUMMY_HASH = "$2b$12$snJQCZ8V0OinZhBeTfmb3OrHhYv7gdBpzrGI6sswwR2XPKWvsFQm2"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Blocking (about a quarter of a second of CPU): request code awaits
    verify_password_async instead. Kept for scripts and tests."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        # Not a bcrypt hash at all: no password matches it.
        return False


def get_password_hash(password: str) -> str:
    """Blocking: request code awaits get_password_hash_async instead."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


async def verify_password_async(plain_password: str, hashed_password: str | None) -> bool:
    """verify_password on a worker thread. bcrypt holds the CPU for about a
    quarter of a second, and on the event loop -- the only one, with one
    worker -- nothing else was answered meanwhile: four password sign-ins a
    second was the platform's ceiling. With no hash (no such account, or one
    without a password) the dummy is checked, so the answer takes as long."""
    if not hashed_password:
        await asyncio.to_thread(verify_password, plain_password or "", DUMMY_HASH)
        return False
    return await asyncio.to_thread(verify_password, plain_password or "", hashed_password)


async def get_password_hash_async(password: str) -> str:
    """get_password_hash on a worker thread."""
    return await asyncio.to_thread(get_password_hash, password)


async def get_user(session: AsyncSession, username_or_id: str, *, password_holders: bool = False, exclude_type: UserTypes | None = None, allow_unverified_club: bool = False):
    """The live account by id, address or username.

    A club account whose club is not approved is None, unless
    `allow_unverified_club`: the auth dependency asks for it so it can answer
    403 CLUB_NOT_VERIFIED rather than a 401 that reads as a dead session."""
    query = select(Users).options(
        joinedload(Users.club), joinedload(Users.profile)
    ).where(Users.is_deleted == False)
    if exclude_type is not None:
        # The clubs dashboard, having tried the club account already.
        query = query.where(Users.user_type != exclude_type)
    if password_holders:
        # Guests never sign in and have no password, and one address can
        # stand behind several of them: an arbitrary first row must not be a
        # guest standing in front of the account that does have one.
        query = query.where(Users.user_type != UserTypes.guest, Users.password.isnot(None))
    if isinstance(username_or_id, int):  # If searching by ID
        result = (await session.execute(query.where(Users.id == username_or_id))).scalars().first()
    elif "@" in username_or_id:
        # The address as stored first; failing that, ignoring case and stray
        # spaces ("Ravi@Gmail.com" for ravi@gmail.com) -- but only when that
        # finds one account, never a guess between two.
        # Oldest first: one address can hold an app account and a club
        # account, and without an order Postgres hands back whichever row it
        # reaches first -- which changes as the table is vacuumed.
        result = (await session.execute(query.where(Users.email == username_or_id).order_by(Users.id))).scalars().first()
        if result is None:
            loose = (await session.execute(
                query.where(func.lower(func.btrim(Users.email)) == username_or_id.strip().lower()).limit(2)
            )).unique().scalars().all()
            result = loose[0] if len(loose) == 1 else None
    else:  # If searching by username
        result = (await session.execute(query.where(Users.username == username_or_id))).scalars().first()

    if result and result.user_type == UserTypes.club and not allow_unverified_club:
        if not result.club or not result.club.is_verified:
            return None
    return result


async def find_account(session: AsyncSession, username: str, user_type: UserTypes, *, password_holders: bool = False):
    """The live account of this one type behind this address or username, or None.

    One address can hold a club account and an app account. `get_user` takes
    whichever row comes first, which on a dashboard could be the person's
    app account; here only `user_type` is looked for. The address as stored
    first; failing that, ignoring case and stray spaces, but only when that
    finds one account.
    """
    query = (
        select(Users)
        .options(joinedload(Users.club), joinedload(Users.profile))
        .where(Users.is_deleted == False, Users.user_type == user_type)  # noqa: E712
    )
    if password_holders:
        query = query.where(Users.password.isnot(None))
    if "@" in username:
        user = (await session.execute(query.where(Users.email == username))).scalars().first()
        if user is None:
            loose = (await session.execute(
                query.where(func.lower(func.btrim(Users.email)) == username.strip().lower()).limit(2)
            )).unique().scalars().all()
            user = loose[0] if len(loose) == 1 else None
        return user
    return (await session.execute(query.where(Users.username == username))).scalars().first()


async def find_club_account(session: AsyncSession, username: str, *, password_holders: bool = False):
    """The club account behind this address or username, whether or not the
    club is approved yet (so the caller can say so); None when there is no
    such account or the club was rejected."""
    user = await find_account(session, username, UserTypes.club, password_holders=password_holders)
    if user is None or user.club is None or user.club.is_deleted:
        return None
    return user


async def authenticate_club_account(session: AsyncSession, username: str, password: str):
    """
    The club account behind this address or username, when the password is
    its own -- for the clubs dashboard's sign-in (scope "club") only.

    The club account is looked for alone (find_club_account). Returned
    whether or not the club is approved yet, so the caller can say so; None
    when there is no such account, the club was rejected, or the password is
    not this account's.
    """
    user = await find_club_account(session, username, password_holders=True)
    if user is None:
        # No dummy check here: the caller goes on to authenticate_user, which
        # makes one when there is no account at all.
        return None
    if not await verify_password_async(password, user.password):
        return None
    return user


async def authenticate_user(session: AsyncSession, username: str, password: str, *, exclude_type: UserTypes | None = None):
    user = await get_user(session, username, password_holders=True, exclude_type=exclude_type)
    if not await verify_password_async(password, user.password if user else None):
        return False
    return user
