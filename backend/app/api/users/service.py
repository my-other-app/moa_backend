from datetime import datetime, timedelta, timezone
import io
import logging
import re
import secrets
from fastapi import File, UploadFile
from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, joinedload

from app.response import CustomHTTPException
from app.api.users.models import (
    ADDRESS_CHECKING_PROVIDERS,
    SigninProviders,
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

logger = logging.getLogger(__name__)


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


def same_email(column, email: str):
    """`column` holds `email`, ignoring case and stray spaces.

    Both sides go through the database's own lower() and btrim(), so the
    comparison cannot disagree with ix_users_email_normalised about what
    matches, and the index serves it.
    """
    return func.lower(func.btrim(column)) == func.lower(func.btrim(email))


def emails_match(a: str | None, b: str | None) -> bool:
    """The Python side of same_email, for two addresses already in hand."""
    return (a or "").strip().lower() == (b or "").strip().lower()


async def get_non_club_user_by_email(session: AsyncSession, email: str):
    """The account a staff-entered address belongs to: the door, the
    organiser's own list, an import, a volunteer invite.

    An app account is preferred, so the ticket shows up in the app; otherwise
    the newest guest. Never call this from a public form: whoever typed the
    address has not shown that it is theirs.
    """
    query = (
        select(Users)
        .where(
            same_email(Users.email, email),
            Users.user_type != UserTypes.club,
            Users.is_deleted == False,
        )
        .order_by(
            case((Users.user_type == UserTypes.app_user, 0), else_=1),
            Users.id.desc(),
        )
        .limit(1)
    )
    return await session.scalar(query)


async def email_has_club_account(session: AsyncSession, email: str) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    same_email(Users.email, email),
                    Users.user_type == UserTypes.club,
                    Users.is_deleted == False,
                )
            )
        )
    )


async def new_guest(
    session: AsyncSession, full_name: str, email: str, phone: str | None = None
) -> Users:
    """A fresh guest for one sign-up from the public form.

    Flushed, not committed: it is kept only if the registration it was made
    for is, so a refused sign-up leaves no account behind.
    """
    guest = Users(
        full_name=full_name,
        email=email,
        phone=phone,
        user_type=UserTypes.guest,
        provider="email",
    )
    # A random tail rather than generate_username's counter: every sign-up
    # without a sign-in makes one of these, and counting up from guest_rahul
    # past every earlier Rahul is a query per Rahul.
    base = re.sub(r"[^a-zA-Z0-9_-]", "", full_name.lower().replace(" ", "_"))[:60] or "user"
    while True:
        username = f"guest_{base}_{secrets.token_hex(3)}"
        if not await session.scalar(select(exists().where(Users.username == username))):
            break
    guest.username = username
    session.add(guest)
    await session.flush()
    return guest


async def create_user(
    session: AsyncSession,
    full_name: str,
    email: str,
    phone: str | None = None,
    password: str | None = None,
    provider: str = "email",
    user_type: UserTypes = UserTypes.app_user,
):
    # Guests may share an address or a phone; every other kind may not.
    if user_type != UserTypes.guest:
        # An address taken in another case ("Ravi@x" against "ravi@x") is
        # taken: two such accounts would split one person's tickets and make
        # signing in depend on how the address was typed.
        taken = await session.scalar(
            select(Users.id).where(
                same_email(Users.email, email), Users.user_type == user_type, Users.is_deleted == False  # noqa: E712
            ).limit(1)
        )
        if taken is not None:
            raise CustomHTTPException(status_code=400, message="Invalid Request", errors={"email": "email already exists"})
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
    if getattr(provider, "value", provider) in ADDRESS_CHECKING_PROVIDERS:
        # Google and Apple checked the address before handing it over.
        user_obj.email_verified_at = datetime.now(timezone.utc)
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


# --- Folding guests into the app account ----------------------------------
#
# A guest is the account the public form, the door, the organiser's list or a
# volunteer invite makes for someone who is not signed in. Once the owner of
# the address signs in to the app, what those guests hold is theirs.

#: Providers that have shown the account owns its address. A password
#: account's address was typed at sign-up, so folding the guests under it
#: into that account would hand their tickets to whoever typed it -- the
#: hole the public form had, one request further on -- until its owner
#: types back the code sent there (Users.email_verified_at).
VERIFIED_PROVIDERS = (SigninProviders.google, SigninProviders.apple)

#: How long an unfinished payment order counts as a payment under way. Within
#: it, a duplicate registration that is being paid for is not retired: the
#: payment would land on a retired row and the guest would have paid for
#: nothing. Razorpay retries its webhooks for a day.
PAYMENT_UNDER_WAY = timedelta(hours=24)

#: First key of the advisory lock that keeps two merges for one account apart.
_MERGE_LOCK = 7_316_203

_NOTHING = {"merged": False, "guests_merged": 0}


def merges_into(user) -> bool:
    """Whether guests are ever folded into this account: an app account
    whose provider vouched for its address, or whose owner verified it."""
    if user is None or user.is_deleted or not (user.email or "").strip():
        return False
    # By value: an account made in this request still holds the plain string
    # it was created with, not the enum member a load would give.
    kind = getattr(user.user_type, "value", user.user_type)
    return kind == UserTypes.app_user.value and user.email_verified


def _guests_of(app_user: Users):
    return select(Users).where(
        same_email(Users.email, app_user.email),
        Users.user_type == UserTypes.guest,
        Users.is_deleted == False,
        Users.id != app_user.id,
    )


async def merge_guest_into_user(session: AsyncSession, app_user: Users) -> dict:
    """Fold every guest under this app account's address into it.

    Registrations, ratings, payment orders, shop orders, volunteer places,
    badges and notifications move over, and the guests are retired. Rows
    kept by registration (check-ins, certificates, deliveries, team places,
    merch) follow their registration. Staff and audit columns -- who checked
    someone in, who created what -- stay as they are.

    Addresses match ignoring case and stray spaces. Only an app account that
    has shown it owns its address takes part (merges_into): its provider
    vouched for it, or its owner typed back the code sent there. For any
    other account this does nothing.

    Safe to call on every request: one indexed query when there is nothing to
    fold, and running it again changes nothing. It never raises. The work
    runs in a SAVEPOINT, so a failure is logged and undone on its own and the
    sign-in or page that called it carries on. The caller commits.
    """
    if not merges_into(app_user):
        return dict(_NOTHING)
    try:
        if await session.scalar(_guests_of(app_user).with_only_columns(Users.id).limit(1)) is None:
            return dict(_NOTHING)
        async with session.begin_nested():
            # Two requests from one app launch can both find the same guests;
            # the second waits here, then finds nothing left to fold.
            await session.execute(select(func.pg_advisory_xact_lock(_MERGE_LOCK, app_user.id)))
            guests = list(await session.scalars(_guests_of(app_user).order_by(Users.id)))
            if not guests:
                return dict(_NOTHING)
            summary = await _fold(session, app_user, guests)
    except Exception:
        logger.exception("Could not merge guests into user %s", app_user.id)
        return {**_NOTHING, "failed": True}
    logger.info("Merged guests into user %s: %s", app_user.id, summary)
    return summary


async def merge_guests_now(session: AsyncSession, user: Users) -> dict:
    """merge_guest_into_user for a request that otherwise only reads: what
    was folded is committed, so the read that follows sees it."""
    summary = await merge_guest_into_user(session, user)
    if summary.get("merged"):
        try:
            await session.commit()
        except Exception:
            logger.exception("Could not keep the guests merged into user %s", user.id)
            await session.rollback()
            await session.refresh(user)
    return summary


async def merge_all_guests(session: AsyncSession) -> dict:
    """The merge for every app account that has guests waiting, including
    guests an older, case-sensitive merge left behind. One account at a
    time, each kept as it finishes."""
    from sqlalchemy.orm import aliased

    guest = aliased(Users)
    accounts = list(
        await session.scalars(
            select(Users)
            .where(
                Users.user_type == UserTypes.app_user,
                or_(Users.provider.in_(VERIFIED_PROVIDERS), Users.email_verified_at.is_not(None)),
                Users.is_deleted == False,
                exists().where(
                    same_email(guest.email, Users.email),
                    guest.user_type == UserTypes.guest,
                    guest.is_deleted == False,
                    guest.id != Users.id,
                ),
            )
            .order_by(Users.id)
        )
    )
    totals: dict = {"app_users_checked": len(accounts), "app_users_merged": 0, "failed": 0}
    for account in accounts:
        summary = await merge_guest_into_user(session, account)
        if summary.get("failed"):
            totals["failed"] += 1
            continue
        if not summary.get("merged"):
            continue
        await session.commit()
        totals["app_users_merged"] += 1
        for key, value in summary.items():
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


async def _fold(session: AsyncSession, app_user: Users, guests: list[Users]) -> dict:
    from app.api.events.models import EventRatingsLink
    from app.api.events.volunteer.models import Volunteer
    from app.api.notifications.models import Notifications
    from app.api.payments.models import PaymentOrders
    from app.api.store.models import EventStoreOrders

    guest_ids = [g.id for g in guests]
    archive = _Archive(session, app_user)

    regs_moved, regs_retired, held = await _fold_registrations(session, app_user, guest_ids, archive)
    ratings_moved, ratings_retired, _ = await _fold_per_event(
        session,
        EventRatingsLink,
        app_user,
        guest_ids,
        archive,
        # The newer opinion stands; on a tie, the one given signed in.
        best_first=lambda r: (-(r.updated_at or r.created_at).timestamp(), r.user_id != app_user.id, str(r.id)),
    )
    summary = {
        "merged": True,
        "registrations_transferred": regs_moved,
        "registrations_retired": regs_retired,
        "ratings_transferred": ratings_moved,
        "ratings_retired": ratings_retired,
        "payments_transferred": await _move(session, PaymentOrders, guest_ids, app_user.id),
        "store_orders_transferred": await _move(session, EventStoreOrders, guest_ids, app_user.id),
        "volunteers_transferred": await _move(session, Volunteer, guest_ids, app_user.id),
        "notifications_transferred": await _move(session, Notifications, guest_ids, app_user.id),
        "badges_transferred": await _fold_badges(session, app_user, guest_ids),
    }

    # A guest still holding a registration that is being paid for stays live
    # until the payment settles; a later run finishes it.
    retired = [g for g in guests if g.id not in held]
    for g in retired:
        g.soft_delete()
    if retired:
        await session.execute(
            update(UserProfiles)
            .where(UserProfiles.user_id.in_([g.id for g in retired]), UserProfiles.is_deleted == False)
            .values(is_deleted=True, deleted_at=datetime.now(timezone.utc))
            .execution_options(synchronize_session=False)
        )
    await session.flush()
    summary["guests_merged"] = len(retired)
    summary["guests_held"] = len(guests) - len(retired)
    return summary


async def _fold_registrations(session: AsyncSession, app_user: Users, guest_ids: list[int], archive) -> tuple[int, int, set]:
    """Registrations, one per event. Where the account and its guests hold
    several for one event, the strongest stays and the rest are retired:

    1. checked in;  2. paid;  3. being paid for right now;  4. holding an
    issued certificate;  5. not revoked;  6. made first.

    A duplicate that is being paid for is never retired: the whole clash
    waits for a later run (see PAYMENT_UNDER_WAY)."""
    from app.api.payments.models import OrderStatus, PaymentOrders

    rows = await _live_rows(session, EventRegistrationsLink, [app_user.id, *guest_ids])
    clashing = [r for rs in _by_event(rows).values() if len(rs) > 1 for r in rs]
    receipts = {r.payment_receipt: r.id for r in clashing if r.payment_receipt}
    paying = set()
    if receipts:
        open_orders = await session.scalars(
            select(PaymentOrders.receipt).where(
                PaymentOrders.receipt.in_(list(receipts)),
                PaymentOrders.status.in_([OrderStatus.created, OrderStatus.attempted]),
                PaymentOrders.created_at >= datetime.now(timezone.utc) - PAYMENT_UNDER_WAY,
                PaymentOrders.is_deleted == False,
            )
        )
        paying = {receipts[receipt] for receipt in open_orders}

    def strongest_first(r):
        return (
            not r.is_attended,
            not (r.is_paid or (r.paid_amount or 0) > 0),
            r.id not in paying,
            not (r.certificate_issued_at is not None and r.certificate_revoked_at is None),
            r.ticket_revoked_at is not None,
            r.created_at,
            str(r.id),
        )

    return await _fold_per_event(
        session,
        EventRegistrationsLink,
        app_user,
        guest_ids,
        archive,
        best_first=strongest_first,
        rows=rows,
        hold=lambda r: r.id in paying,
    )


async def _fold_per_event(session, model, app_user, guest_ids, archive, *, best_first, rows=None, hold=None) -> tuple[int, int, set]:
    """Moves `model`'s live rows from the guests to the account, keeping one
    per event: the first by `best_first`. The others are retired. A clash
    with a row `hold` picks out among the losers is left alone.

    (event_id, user_id, is_deleted) is unique, so a retired row needs an
    account with no retired row for that event yet: its own account if free,
    else another guest being folded, else the app account, else a retired
    archive guest made for the purpose. Losers are retired and flushed
    before winners move, so no statement sees two live rows for one event.
    Returns (moved, retired, ids of guests whose clash was held)."""
    owners = [app_user.id, *guest_ids]
    if rows is None:
        rows = await _live_rows(session, model, owners)
    groups = _by_event(rows)
    taken = await _retired_slots(session, model, [e for e, rs in groups.items() if len(rs) > 1], owners)

    winners, losers, held = [], [], set()
    for rs in groups.values():
        rs.sort(key=best_first)
        best, rest = rs[0], rs[1:]
        if hold is not None and any(hold(r) for r in rest):
            held.update(r.user_id for r in rs if r.user_id != app_user.id)
            continue
        if best.user_id != app_user.id:
            winners.append(best)
        losers.extend(rest)

    for row in losers:
        candidates = [row.user_id, *(g for g in guest_ids if g != row.user_id)]
        if row.user_id != app_user.id:
            candidates.append(app_user.id)
        holder = next((c for c in candidates if (row.event_id, c) not in taken), None)
        if holder is None:
            holder = await archive.free_for(row.event_id, taken)
        taken.add((row.event_id, holder))
        row.user_id = holder
        row.soft_delete()
    await session.flush()
    for row in winners:
        row.user_id = app_user.id
    await session.flush()
    return len(winners), len(losers), held


class _Archive:
    """Retired guests made only to hold a retired duplicate when every account
    in the merge already holds one for that event. Practically never needed;
    without it the merge would have no legal place to put the row."""

    def __init__(self, session: AsyncSession, app_user: Users):
        self.session = session
        self.app_user = app_user
        self.made: list[int] = []

    async def free_for(self, event_id: int, taken: set) -> int:
        for user_id in self.made:
            if (event_id, user_id) not in taken:
                return user_id
        archived = Users(
            full_name=self.app_user.full_name or "Merged guest",
            username=f"merged_{self.app_user.id}_{secrets.token_hex(4)}",
            email=self.app_user.email,
            user_type=UserTypes.guest,
            is_deleted=True,
            deleted_at=datetime.now(timezone.utc),
        )
        self.session.add(archived)
        await self.session.flush()
        self.made.append(archived.id)
        return archived.id


async def _live_rows(session: AsyncSession, model, owners: list[int]) -> list:
    return list(await session.scalars(select(model).where(model.user_id.in_(owners), model.is_deleted == False)))


def _by_event(rows) -> dict:
    groups: dict = {}
    for row in rows:
        groups.setdefault(row.event_id, []).append(row)
    return groups


async def _retired_slots(session: AsyncSession, model, event_ids: list[int], owners: list[int]) -> set:
    """(event_id, user_id) pairs that already hold a retired row. Read on the
    connection rather than through the session: the session hides retired
    rows from every ORM query (app.db.listeners)."""
    if not event_ids:
        return set()
    table = model.__table__
    connection = await session.connection()
    result = await connection.execute(
        select(table.c.event_id, table.c.user_id).where(
            table.c.event_id.in_(event_ids),
            table.c.user_id.in_(owners),
            table.c.is_deleted.is_(True),
        )
    )
    return {(event_id, user_id) for event_id, user_id in result}


async def _move(session: AsyncSession, model, guest_ids: list[int], app_user_id: int) -> int:
    """Every live row of `model` the guests hold, to the account."""
    query = update(model).where(model.user_id.in_(guest_ids))
    if hasattr(model, "is_deleted"):
        query = query.where(model.is_deleted == False)
    result = await session.execute(
        query.values(user_id=app_user_id).execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


async def _fold_badges(session: AsyncSession, app_user: Users, guest_ids: list[int]) -> int:
    """Badges the account does not have yet. (user_id, badge_id) is unique and
    badges cannot be retired, so a badge it already has stays with the
    retired guest."""
    have = set(await session.scalars(select(UserBadges.badge_id).where(UserBadges.user_id == app_user.id)))
    moved = 0
    for badge in await session.scalars(
        select(UserBadges).where(UserBadges.user_id.in_(guest_ids)).order_by(UserBadges.awarded_at, UserBadges.id)
    ):
        if badge.badge_id in have:
            continue
        badge.user_id = app_user.id
        have.add(badge.badge_id)
        moved += 1
    await session.flush()
    return moved


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
        # What was verified was the old address; the new one has to be shown
        # to be theirs before anything booked under it comes in.
        user.email_verified_at = None

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
    if getattr(user.user_type, "value", user.user_type) == "club":
        # A club's account is the club: without it nobody can sign in to it.
        # The club goes to the Trash with it, where restoring the club brings
        # the account back too (clubs.service.restore_club). Left live, it
        # stayed on every list while nobody could run it, and never showed up
        # in the Trash.
        from app.api.clubs.models import Clubs

        for club in await session.scalars(
            select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False)  # noqa: E712
        ):
            club.is_deleted = True
            club.deleted_at = user.deleted_at
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

