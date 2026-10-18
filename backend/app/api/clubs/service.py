from datetime import datetime, timezone
import io
import re
from fastapi import UploadFile
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, delete, exists, or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, aliased, joinedload
import secrets


from app.core.utils.keys import generate_slug
from app.response import CustomHTTPException
from app.core.response.sorting import apply_sort
from app.api.clubs.models import (
    ClubInterestsLink,
    ClubUsersLink,
    Clubs,
    Notes,
    ClubSocials,
    ClubPartners,
    ClubTeamMembers,
)
from app.api.clubs.schemas import ClubSocialsCreate, CreateClub
from app.api.users.models import UserProfiles, UserTypes, Users
from app.api.users.service import create_user
from app.api.interests.models import Interests
from app.core.validations.schema import validate_relations
from app.api.orgs.models import Organizations
from app.api.events.models import Events, EventRegistrationsLink, EventRatingsLink
from app.api.discover import geo


async def create_club(
    session: AsyncSession,
    name: str,
    email: str,
    password: str | None = None,
    phone: str | None = None,
    about: str | None = None,
    org_id: int | None = None,
    location_name: str | None = None,
    logo: UploadFile | None = None,
    interest_ids: list[int] | None = None,
    is_admin_created: bool = False,
    requested_plan: str | None = None,
    requested_cycle: str | None = None,
    location_link: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    socials: dict | None = None,
) -> Clubs:
    """Create a new club. `requested_plan`/`requested_cycle` must already be
    checked (see subscriptions.service.requested_plan_key). `socials` is
    instagram / linkedin / youtube / website; a blank one is left out."""
    if not is_admin_created and not password:
        raise CustomHTTPException(400, "Password is required")

    existing_club = await session.scalar(
        select(Clubs).where(
            func.lower(func.replace(Clubs.name, " ", ""))
            == func.lower(func.replace(name, " ", "")),
            Clubs.is_deleted == False,
        )
    )
    if existing_club:
        raise CustomHTTPException(400, "Club with this name already exists")

    slug = generate_slug(name)

    if is_admin_created:
        password = secrets.token_urlsafe(12)  # Generate a secure random password

    club_user = await create_user(
        session,
        full_name=name,
        email=email,
        password=password,
        phone=phone,
        user_type=UserTypes.club,
    )

    club = Clubs(
        name=name,
        slug=slug,
        about=about,
        org_id=org_id,
        user_id=club_user.id,
        is_verified=is_admin_created,
        requested_plan=requested_plan,
        requested_cycle=requested_cycle,
        contact_phone=(contact_phone or "").strip() or None,
        contact_email=(contact_email or "").strip() or None,
        # Note: initial_password removed for security - use password reset flow instead
    )
    # The place as the profile form saves it: a map link that carries a point
    # gives it now; a short link or bare words are placed after the answer.
    geo.apply_place(club, link=location_link, venue=location_name)

    if logo:
        content = io.BytesIO(await logo.read())
        club.logo = {
            "bytes": content,
            "filename": logo.filename,
        }

    session.add(club)
    await session.commit()
    await session.refresh(club)

    links = {key: (value or "").strip() or None for key, value in (socials or {}).items()}
    if any(links.values()):
        session.add(ClubSocials(club_id=club.id, **links))

    if interest_ids:
        interests = await session.scalars(
            select(Interests).where(Interests.id.in_(interest_ids))
        )
        for interest in interests:
            link = ClubInterestsLink(club_id=club.id, interest_id=interest.id)
            session.add(link)

    await session.commit()
    await session.refresh(club)
    return club


HEX_COLOUR = re.compile(r"^#[0-9a-f]{6}$")


async def update_club(session: AsyncSession, club_id: int, club: CreateClub):
    await validate_relations(
        session,
        {
            "org_id": (Organizations, club.org_id),
        },
    )
    db_club = (
        select(Clubs).where(Clubs.id == club_id).options(selectinload(Clubs.interests))
    )
    db_club = await session.scalar(db_club)

    if not db_club:
        raise CustomHTTPException(404, "Club not found")

    if club.logo:
        content = io.BytesIO(await club.logo.read())
        if db_club.logo:
            db_club.logo.delete()
        db_club.logo = {
            "bytes": content,
            "filename": club.logo.filename,
        }

    if club.name:
        db_club.name = club.name
    db_club.about = club.about
    if hasattr(club, "tagline"):
        db_club.tagline = (club.tagline or "").strip() or None
    # Like every other field here, whatever came is what is kept: an empty
    # (or omitted) colour clears it. The profile form always sends both.
    for key in ("brand_primary", "brand_secondary"):
        value = (getattr(club, key, None) or "").strip().lower()
        if value and not HEX_COLOUR.match(value):
            raise CustomHTTPException(422, message=f"{key.replace('_', ' ')}: use a 6-digit hex colour, like #1db9a0.")
        setattr(db_club, key, value or None)
    # The place, by geo.apply_place's precedence (a pin, then the map link,
    # then the words). A changed map link moves a derived point (read now, or
    # followed after the save); the same link keeps it. A pin stays until new
    # pin values come or clear_point drops it.
    geo.apply_place(
        db_club, link=club.location_link, venue=club.location_name,
        manual=getattr(club, "pin", None), clear_point=bool(getattr(club, "clear_point", False)),
    )
    db_club.org_id = club.org_id
    db_club.contact_phone = club.contact_phone
    db_club.contact_email = club.contact_email

    await session.execute(
        delete(ClubInterestsLink).where(ClubInterestsLink.club_id == db_club.id)
    )

    if club.interest_ids:
        interests = await session.scalars(
            select(Interests).where(Interests.id.in_(club.interest_ids))
        )
        for interest in interests:
            link = ClubInterestsLink(club_id=db_club.id, interest_id=interest.id)
            session.add(link)
    
    # Update socials
    db_socials = await session.scalar(
        select(ClubSocials).where(
            ClubSocials.club_id == club_id,
            ClubSocials.is_deleted == False,
        )
    )
    if db_socials:
        db_socials.instagram = club.instagram
        db_socials.linkedin = club.linkedin
        db_socials.youtube = club.youtube
        db_socials.website = club.website
    else:
        if any([club.instagram, club.linkedin, club.youtube, club.website]):
            db_socials = ClubSocials(
                club_id=club_id,
                instagram=club.instagram,
                linkedin=club.linkedin,
                youtube=club.youtube,
                website=club.website,
            )
            session.add(db_socials)

    await session.commit()
    await session.refresh(db_club)
    return db_club


async def get_club(club_id: int, session: AsyncSession):
    db_club = await session.scalar(
        select(Clubs).where(Clubs.id == club_id, Clubs.is_deleted == False)
    )
    if not db_club:
        raise CustomHTTPException(404, "Club not found")
    return db_club


async def approve_club(session: AsyncSession, club_id: int) -> Clubs:
    """Verify a pending club, allowing it to log in and appear publicly."""
    club = await get_club(club_id, session)
    club.is_verified = True
    await session.commit()
    # Explicitly reload relationships used by the response schema — commit()
    # expires attributes by default, and a later *lazy* load during response
    # serialization (outside the session's async context) would raise
    # MissingGreenlet, since Pydantic can't await it.
    return await club_for_answer(session, club.id)


def send_approved_email(to: str, *, club_name: str) -> None:
    """"Your club is approved -- log in", to the club's sign-in address. Sent
    after the approval's answer; a mail that cannot go out is logged, never
    turned into a failed approval."""
    import html as html_mod
    import logging

    from app.config import settings
    from app.core.email import email as email_mod

    link = f"{settings.PUBLIC_WEB_URL.rstrip('/')}/login"
    text = (
        f"{club_name} is approved on MyOtherApp.\n\n"
        f"Log in to your club's dashboard with the email and password you signed up with: {link}\n"
    )
    safe = html_mod.escape(club_name)
    html = (
        f"<p><strong>{safe}</strong> is approved on MyOtherApp.</p>"
        f"<p>Log in to your club's dashboard with the email and password you signed up with.</p>"
        f'<p><a href="{link}">Log in</a></p>'
    )
    try:
        email_mod.send_email(recipients=[to], subject=f"{club_name} is approved — log in", text=text, html=html)
    except Exception:
        logging.getLogger(__name__).warning("approval email to %s could not be sent", to, exc_info=True)


async def reject_club(session: AsyncSession, club_id: int) -> Clubs:
    """Reject a club's (self-)registration.

    There is no separate "rejected" status column today — only
    `is_verified`. Soft-deleting reuses the existing SoftDeleteMixin
    machinery already relied on elsewhere in this codebase, and correctly
    removes the club from public listing/search and blocks its login,
    consistent with how every other visibility/auth check in this domain
    is gated on `is_deleted`/`is_verified`.
    """
    club = await get_club(club_id, session)
    club.soft_delete()
    await _stop_billing(session, club)
    await session.commit()
    return await club_for_answer(session, club.id)


async def _stop_billing(session: AsyncSession, club: Clubs) -> None:
    """A deleted club's Razorpay subscription is cancelled with it
    (subscriptions.club_deleted); restoring the club does not restore it."""
    from app.api.subscriptions.club_deleted import end_for_deleted_club

    await end_for_deleted_club(session, club)


async def club_for_answer(session: AsyncSession, club_id: int) -> Clubs:
    """
    The club as an admin answer (ClubAdminDetailResponse) reads it, with its
    interests and organisation loaded -- the schema reads `org`, and a lazy
    load while Pydantic serialises raises MissingGreenlet, so a club in an
    organisation turned its own approve, reject or delete into a 500 after the
    change had committed.

    Read with `include_deleted`: after a reject or delete the club *is*
    deleted, and the session hides deleted rows from every ORM query
    (app.db.listeners), so a plain `refresh` found no row and failed too.
    """
    return await session.scalar(
        select(Clubs)
        .where(Clubs.id == club_id)
        .options(selectinload(Clubs.interests), selectinload(Clubs.org))
        .execution_options(include_deleted=True, populate_existing=True)
    )


async def get_club_admin_detail(session: AsyncSession, club_id: int):
    """
    One club as an admin needs to see it.

    Builds on get_club_details with visibility off: the public view hides
    anything not yet verified, which is precisely the club an admin is about
    to approve or reject. On top of the public figures this adds the money and
    reach across the club's events, and who actually owns the account.
    """
    detail = await get_club_details(
        session, club_id=club_id, user_id=None, enforce_visibility=False
    )

    club = await session.scalar(
        select(Clubs).where(Clubs.id == club_id).options(joinedload(Clubs.user))
    )

    event_ids = select(Events.id).where(
        Events.club_id == club_id, Events.is_deleted == False
    )
    registration_filter = [
        EventRegistrationsLink.event_id.in_(event_ids),
        EventRegistrationsLink.is_deleted == False,
    ]

    total_registrations = await session.scalar(
        select(func.count(EventRegistrationsLink.id)).where(*registration_filter)
    )
    total_attended = await session.scalar(
        select(func.count(EventRegistrationsLink.id)).where(
            *registration_filter, EventRegistrationsLink.is_attended == True
        )
    )
    total_revenue = await session.scalar(
        select(func.coalesce(func.sum(EventRegistrationsLink.paid_amount), 0)).where(
            *registration_filter, EventRegistrationsLink.is_paid == True
        )
    )

    detail["is_verified"] = club.is_verified if club else False
    detail["requested_plan"] = club.requested_plan if club else None
    detail["requested_cycle"] = club.requested_cycle if club else None
    detail["is_deleted"] = club.is_deleted if club else False
    detail["created_at"] = club.created_at if club else None
    detail["owner"] = (
        {
            "id": club.user.id,
            "full_name": club.user.full_name,
            "username": club.user.username,
            "email": club.user.email,
        }
        if club and club.user
        else None
    )
    detail["admin_stats"] = {
        "total_registrations": total_registrations or 0,
        "total_attended": total_attended or 0,
        "total_revenue": float(total_revenue or 0),
    }
    return detail


async def delete_club(session: AsyncSession, club_id: int) -> Clubs:
    """
    Soft-delete a club as an admin.

    Same mechanism as reject_club -- there is still only `is_verified` and
    `is_deleted` -- but a separate entry point, because rejecting a pending
    registration and removing an established club are different decisions and
    the UI needs to say different things about them.
    """
    club = await get_club(club_id, session)
    club.soft_delete()
    await _stop_billing(session, club)
    await session.commit()
    return await club_for_answer(session, club.id)


async def list_deleted_clubs(session: AsyncSession, limit: int = 20, offset: int = 0):
    """
    Soft-deleted clubs, so a mistake can be found and undone.

    Everything in this product soft-deletes and nothing restored, so "reject"
    and "delete" were effectively permanent -- the dialogs said a developer
    could undo it, which was true and also an admission the product could not.

    Every statement here says `include_deleted`. The session hides deleted
    rows from every ORM query (app.db.listeners); asking for
    `is_deleted == True` without it ANDs `is_deleted IS false` on and finds
    nothing, which is why the Trash was empty in production. The organisation
    and the owner's address are read the same way: either may have been
    deleted along with the club.
    """
    see_all = {"include_deleted": True}
    total = await session.scalar(
        select(func.count(Clubs.id)).where(Clubs.is_deleted == True).execution_options(**see_all)
    )
    clubs = list(
        await session.scalars(
            select(Clubs)
            .where(Clubs.is_deleted == True)
            .options(selectinload(Clubs.interests))
            .order_by(Clubs.deleted_at.desc().nulls_last(), Clubs.id.desc())
            .limit(limit)
            .offset(offset)
            .execution_options(**see_all)
        )
    )
    org_ids = {club.org_id for club in clubs if club.org_id}
    orgs = dict(
        (
            await session.execute(
                select(Organizations.id, Organizations.name)
                .where(Organizations.id.in_(org_ids))
                .execution_options(**see_all)
            )
        ).all()
    ) if org_ids else {}
    owner_ids = {club.user_id for club in clubs if club.user_id}
    owners = dict(
        (
            await session.execute(
                select(Users.id, Users.email).where(Users.id.in_(owner_ids)).execution_options(**see_all)
            )
        ).all()
    ) if owner_ids else {}

    rows = []
    for club in clubs:
        rows.append(
            {
                "id": club.id,
                "name": club.name,
                "slug": club.slug,
                "logo": club.logo,
                "about": club.about,
                "location_name": club.location_name,
                "location_link": club.location_link,
                "contact_phone": club.contact_phone,
                "contact_email": club.contact_email,
                "created_at": club.created_at,
                "deleted_at": club.deleted_at,
                "is_verified": club.is_verified,
                "interests": [
                    {"id": i.id, "name": i.name, "icon": i.icon,
                     "icon_type": getattr(i.icon_type, "value", i.icon_type)}
                    for i in (club.interests or [])
                ],
                "org": {"id": club.org_id, "name": orgs[club.org_id]} if club.org_id in orgs else None,
                "owner_email": owners.get(club.user_id),
                "requested_plan": club.requested_plan,
                "requested_cycle": club.requested_cycle,
            }
        )
    return rows, (total or 0)


async def restore_club(session: AsyncSession, club_id: int) -> Clubs:
    """
    Undo a soft delete.

    Restoring does not re-verify: a club deleted while pending comes back
    pending, so an admin still makes the approval decision deliberately
    rather than inheriting it from an undo.

    The club's sign-in account comes back with it when it was deleted too (an
    admin deleting a club's account deletes the club: users.service), or the
    restored club could never sign in. Read with `include_deleted`, as in
    `list_deleted_clubs`: without it the deleted club is never found.

    Its subscription does not come back. Deleting the club cancelled any
    Razorpay subscription it had (subscriptions.club_deleted), and Razorpay
    cannot un-cancel one: the restored club is on Free until it subscribes
    again.
    """
    club = await session.scalar(
        select(Clubs)
        .where(Clubs.id == club_id, Clubs.is_deleted == True)
        .execution_options(include_deleted=True)
    )
    if not club:
        raise CustomHTTPException(404, "No deleted club with that id")

    club.is_deleted = False
    club.deleted_at = None
    owner = await session.scalar(
        select(Users)
        .where(Users.id == club.user_id, Users.is_deleted == True, Users.user_type == UserTypes.club)
        .execution_options(include_deleted=True)
    )
    if owner is not None:
        owner.is_deleted = False
        owner.deleted_at = None
    await session.commit()
    return await club_for_answer(session, club.id)


async def get_all_clubs(
    session: AsyncSession,
    user_id: int | None = None,
    org_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
    is_following: bool | None = None,
    is_pinned: bool | None = None,
    is_hidden: bool | None = None,
    interest_ids: list[int] | None = None,
    search: str | None = None,
    include_unverified: bool = False,
    is_verified: bool | None = None,
    sort: str | None = None,
    order: str | None = None,
    with_admin_fields: bool = False,
):
    """Get all clubs with optional filters, search, and followers count.

    `include_unverified` should only be set by admin-facing callers — the
    public listing must never surface clubs pending approval.

    `with_admin_fields` adds the organisation, the login email and an event
    count to each row. It is off by default so the public listing, which is
    the hot path, does not pay for three things it never shows.
    """
    ClubUsersLinkPinned = aliased(ClubUsersLink)
    ClubUsersLinkHidden = aliased(ClubUsersLink)
    ClubUsersLinkFollowers = aliased(ClubUsersLink)
    ClubUsersLinkFollowing = aliased(ClubUsersLink)

    query = (
        select(
            Clubs,
            func.count(func.distinct(ClubUsersLinkFollowers.id)).label(
                "followers_count"
            ),
            func.bool_or(ClubUsersLinkFollowing.id.isnot(None)).label("is_following"),
        )
        .outerjoin(
            ClubUsersLinkFollowers,
            and_(
                ClubUsersLinkFollowers.club_id == Clubs.id,
                ClubUsersLinkFollowers.is_following == True,
                ClubUsersLinkFollowers.is_deleted == False,
            ),
        )
        .outerjoin(
            ClubUsersLinkFollowing,
            and_(
                ClubUsersLinkFollowing.club_id == Clubs.id,
                ClubUsersLinkFollowing.user_id == user_id,
                ClubUsersLinkFollowing.is_following == True,
                ClubUsersLinkFollowing.is_deleted == False,
            ),
        )
        .where(Clubs.is_deleted == False)
        .options(selectinload(Clubs.interests))
    )

    if with_admin_fields:
        # selectinload, not joinedload: these would fan out against the
        # follower join above and inflate the aggregate it computes.
        query = query.options(selectinload(Clubs.org), selectinload(Clubs.user))

    # `include_unverified` is the admin gate; `is_verified` is the caller's
    # own filter on top of it, so an admin can ask for pending clubs only.
    if is_verified is not None:
        query = query.where(Clubs.is_verified == is_verified)

    if not include_unverified:
        query = query.where(Clubs.is_verified == True)
        query = query.outerjoin(
            Organizations, Organizations.id == Clubs.org_id
        ).where(or_(Clubs.org_id.is_(None), Organizations.is_blocked == False))

    # Search filter - use ILIKE for case-insensitive pattern matching on club name
    if search and search.strip():
        search_pattern = f"%{search.strip()}%"
        query = query.filter(Clubs.name.ilike(search_pattern))

    if org_id:
        query = query.filter(Clubs.org_id == org_id)

    if interest_ids:
        query = query.join(ClubInterestsLink).filter(
            ClubInterestsLink.interest_id.in_(interest_ids)
        )

    if is_following is not None and user_id:
        if is_following:
            query = query.join(ClubUsersLink).filter(
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        else:
            query = query.outerjoin(
                ClubUsersLink,
                and_(
                    ClubUsersLink.club_id == Clubs.id,
                    ClubUsersLink.user_id == user_id,
                    ClubUsersLink.is_following == True,
                    ClubUsersLink.is_deleted == False,
                ),
            ).filter(ClubUsersLink.id == None)

    if is_pinned is not None and user_id:
        query = query.join(ClubUsersLinkPinned).filter(
            ClubUsersLinkPinned.user_id == user_id,
            ClubUsersLinkPinned.is_pinned == is_pinned,
            ClubUsersLinkPinned.is_deleted == False,
        )

    if is_hidden is not None and user_id:
        query = query.join(ClubUsersLinkHidden).filter(
            ClubUsersLinkHidden.user_id == user_id,
            ClubUsersLinkHidden.is_hidden == is_hidden,
            ClubUsersLinkHidden.is_deleted == False,
        )

    # The total matching the same filters, taken from this very query rather
    # than a hand-written copy of its WHERE clause -- the two cannot drift.
    # with_only_columns keeps the FROM and joins and swaps the selected
    # columns; DISTINCT collapses the fan-out from the follower joins.
    total = await session.scalar(
        select(func.count()).select_from(
            query.with_only_columns(Clubs.id).order_by(None).distinct().subquery()
        )
    )

    query = query.group_by(Clubs.id).limit(limit).offset(offset)
    query = apply_sort(
        query,
        columns={
            "created": Clubs.created_at,
            "name": Clubs.name,
            # The aggregate is already selected and labelled, so sorting on it
            # costs nothing extra.
            "followers": func.count(func.distinct(ClubUsersLinkFollowers.id)),
        },
        default=Clubs.created_at,
        sort=sort,
        order=order,
    )

    results = await session.execute(query)

    clubs_response = []
    results_clubs = []
    for club, followers_count, is_following in results:
        results_clubs.append(club)
        club_dict = {
            "id": club.id,
            "name": club.name,
            "slug": club.slug,
            "logo": club.logo,
            "about": club.about,
            "location_name": club.location_name,
            "location_link": club.location_link,
            "contact_phone": club.contact_phone,
            "contact_email": club.contact_email,
            "brand_primary": club.brand_primary,
            "brand_secondary": club.brand_secondary,
            "created_at": club.created_at,
            "is_verified": club.is_verified,
            "followers_count": followers_count,
            "is_following": is_following,
        }
        
        # Serialize interests
        if club.interests:
            club_dict["interests"] = [
                {
                    "id": i.id,
                    "name": i.name,
                    "icon": i.icon,
                    "icon_type": i.icon_type.value if hasattr(i.icon_type, "value") else str(i.icon_type) if i.icon_type else None,
                }
                for i in club.interests
            ]
        else:
            club_dict["interests"] = []
            
        clubs_response.append(club_dict)

    if with_admin_fields:
        await _attach_admin_fields(session, results_clubs, clubs_response)

    # Returned with the total so callers can paginate. Without it
    # paginated_response falls back to "is there an extra row", which reports
    # no page count at all -- every club list was therefore stuck on its first
    # page, including the club list on an organisation's page.
    return clubs_response, total or 0


async def _attach_admin_fields(session: AsyncSession, clubs, rows: list[dict]) -> None:
    """
    Add organisation, login email and event count to an admin club page.

    The counts come from one grouped query over the page's ids rather than a
    correlated subquery on the listing itself: that listing already carries an
    outer join to the follower link table, and a second aggregate over a
    second join multiplies the rows each one counts.
    """
    if not clubs:
        return

    club_ids = [club.id for club in clubs]
    counts = dict(
        (
            await session.execute(
                select(Events.club_id, func.count(Events.id))
                .where(Events.club_id.in_(club_ids), Events.is_deleted == False)
                .group_by(Events.club_id)
            )
        ).all()
    )

    for club, row in zip(clubs, rows):
        row["events_count"] = counts.get(club.id, 0)
        row["org"] = {"id": club.org.id, "name": club.org.name} if club.org else None
        # The club's sign-in address, which is not contact_email: that one is
        # published on the club's page and is often a committee alias.
        row["owner_email"] = club.user.email if club.user else None
        row["requested_plan"] = club.requested_plan
        row["requested_cycle"] = club.requested_cycle


async def follow_club(session: AsyncSession, club_id: int, user_id: int, tracking_ref: str | None = None):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    follow = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.user_id == user_id, ClubUsersLink.club_id == club_id
        )
    )
    if follow:
        if follow.is_following:
            raise CustomHTTPException(409, "already following")
        follow.is_following = True
    else:
        follow = ClubUsersLink(user_id=user_id, club_id=club_id, is_following=True)
        session.add(follow)
    # Credited to the tracking link the page carried, if it carried one of this club's.
    from app.api.links import service as links

    await links.attribute_follow(session, follow, club_id, tracking_ref)
    await session.commit()
    await session.refresh(follow)
    return follow


async def unfollow_club(session: AsyncSession, club_id: int, user_id: int):
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    follow = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.user_id == user_id, ClubUsersLink.club_id == club_id
        )
    )

    if not follow or not follow.is_following:
        raise CustomHTTPException(409, "not following the club")

    follow.is_following = False

    await session.commit()
    await session.refresh(follow)
    return follow


async def create_note(
    session: AsyncSession, club_id: int, user_id: int, title: str, note: str
):
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id)))
    user_exists = await session.scalar(select(exists().where(Users.id == user_id)))

    if not user_exists:
        raise CustomHTTPException(status_code=401, message="Unauthorized")
    if not club_exists:
        raise CustomHTTPException(status_code=400, message="Invalid club")

    note = Notes(club_id=club_id, user_id=user_id, title=title, note=note)

    session.add(note)
    await session.commit()
    await session.refresh(note)
    return note


async def list_notes(
    session: AsyncSession, club_id: int, limit: int = 10, offset: int = 0
):
    club_exists = await session.scalar(select(exists().where(Clubs.id == club_id, Clubs.is_deleted == False)))
    if not club_exists:
        raise CustomHTTPException(status_code=404, message="Club not found")

    query = (
        select(Notes)
        .where(Notes.club_id == club_id, Notes.is_deleted == False)
        .order_by(Notes.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await session.scalars(query))


async def toggle_pin_club(
    session: AsyncSession, club_id: int, user_id: int
) -> ClubUsersLink:
    """Toggle pin status for a club."""
    link = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_deleted == False,
        )
    )
    if not link:
        link = ClubUsersLink(club_id=club_id, user_id=user_id)
        session.add(link)

    link.is_pinned = not link.is_pinned
    await session.commit()
    await session.refresh(link)
    return link


async def toggle_hide_club(
    session: AsyncSession, club_id: int, user_id: int
) -> ClubUsersLink:
    """Toggle hide status for a club."""
    link = await session.scalar(
        select(ClubUsersLink).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.user_id == user_id,
            ClubUsersLink.is_deleted == False,
        )
    )
    if not link:
        link = ClubUsersLink(club_id=club_id, user_id=user_id)
        session.add(link)

    link.is_hidden = not link.is_hidden
    await session.commit()
    await session.refresh(link)
    return link


async def list_club_followers(
    session: AsyncSession, club_id: int, limit: int = 10, offset: int = 0
):
    """List followers of a club."""
    query = (
        select(ClubUsersLink)
        .where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,
            ClubUsersLink.is_deleted == False,
        )
        .options(
            joinedload(ClubUsersLink.user).options(
                joinedload(Users.profile).options(joinedload(UserProfiles.avatar))
            )
        )
        .order_by(ClubUsersLink.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return result.scalars().all()


async def count_club_followers(session: AsyncSession, club_id: int) -> int:
    """How many follow the club, so a page of them can say where it stands."""
    return int(
        await session.scalar(
            select(func.count()).select_from(ClubUsersLink).where(
                ClubUsersLink.club_id == club_id,
                ClubUsersLink.is_following == True,
                ClubUsersLink.is_deleted == False,
            )
        )
        or 0
    )


async def get_club_details(
    session: AsyncSession,
    club_id: int,
    user_id: int | None = None,
    enforce_visibility: bool = True,
):
    """Get detailed information about a club.

    `enforce_visibility=False` is only for a club viewing its own profile —
    every other caller must not be able to see a club that isn't verified yet.
    """
    query = (
        select(Clubs)
        .where(Clubs.id == club_id)
        .options(
            selectinload(Clubs.interests),
            joinedload(Clubs.socials),
            joinedload(Clubs.org),
        )
    )
    if enforce_visibility:
        query = query.where(Clubs.is_verified == True, Clubs.is_deleted == False)
    club = await session.scalar(query)
    if not club:
        raise CustomHTTPException(404, "Club not found")

    # Get followers count
    followers_count = await session.scalar(
        select(func.count()).where(
            ClubUsersLink.club_id == club_id,
            ClubUsersLink.is_following == True,
            ClubUsersLink.is_deleted == False,
        )
    )

    # Get events statistics
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    
    # Total events
    total_events = await session.scalar(
        select(func.count()).where(
            Events.club_id == club_id,
            Events.is_deleted == False,
        )
    )

    # Live/Upcoming events
    live_events = await session.scalar(
        select(func.count()).where(
            Events.club_id == club_id,
            Events.is_deleted == False,
            Events.event_datetime >= now,
        )
    )

    # Past events
    past_events = await session.scalar(
        select(func.count()).where(
            Events.club_id == club_id,
            Events.is_deleted == False,
            Events.event_datetime < now,
        )
    )
    # Get user specific data
    user_data = None
    if user_id:
        link = await session.scalar(
            select(ClubUsersLink).where(
                ClubUsersLink.club_id == club_id,
                ClubUsersLink.user_id == user_id,
                ClubUsersLink.is_deleted == False,
            )
        )
        if link:
            user_data = {
                "is_following": link.is_following,
                "is_pinned": link.is_pinned,
                "is_hidden": link.is_hidden,
            }
        else:
            user_data = {
                "is_following": False,
                "is_pinned": False,
                "is_hidden": False,
            }

    # Calculate average rating
    # Calculate rating dynamically from EventRatingsLink
    total_ratings = await session.scalar(
        select(func.count()).where(
            EventRatingsLink.event_id.in_(
                select(Events.id).where(
                    Events.club_id == club_id,
                    Events.is_deleted == False
                )
            ),
            EventRatingsLink.is_deleted == False,
        )
    )
    avg_rating = await session.scalar(
        select(func.avg(EventRatingsLink.rating)).where(
            EventRatingsLink.event_id.in_(
                select(Events.id).where(
                    Events.club_id == club_id,
                    Events.is_deleted == False
                )
            ),
            EventRatingsLink.is_deleted == False,
        )
    )

    rating = avg_rating if avg_rating else 0.0
    total_ratings_count = total_ratings if total_ratings else 0
    
    # Manual serialization to avoid circular references and 500 errors
    club_dict = {
        "id": club.id,
        "name": club.name,
        "slug": club.slug,
        "logo": club.logo,
        "cover": club.cover,
        "tagline": club.tagline,
        "about": club.about,
        "location_name": club.location_name,
        "location_link": club.location_link,
        "lat": club.lat,
        "lng": club.lng,
        "geo_source": club.geo_source,
        "contact_phone": club.contact_phone,
        "contact_email": club.contact_email,
        "brand_primary": club.brand_primary,
        "brand_secondary": club.brand_secondary,
        "rating": rating,
        "total_ratings": total_ratings_count,
        "followers_count": followers_count,
        "total_events": total_events or 0,
        "live_events": live_events or 0,
        "past_events": past_events or 0,
        "user_data": user_data,
    }

    club_dict["partners"] = [_partner_out(p) for p in await list_partners(session, club.id)]
    club_dict["team"] = [_member_out(m) for m in await list_team(session, club.id)]
    # Manually serialize relationships
    if club.org:
        club_dict["org"] = {
            "id": club.org.id,
            "name": club.org.name,
            "type": club.org.type.value if hasattr(club.org.type, "value") else str(club.org.type),
            "address": club.org.address,
            "phone": club.org.phone,
            "email": club.org.email,
        }
    else:
        club_dict["org"] = None

    if club.socials:
        club_dict["socials"] = {
            "instagram": club.socials.instagram,
            "linkedin": club.socials.linkedin,
            "youtube": club.socials.youtube,
            "website": club.socials.website,
        }
    else:
        club_dict["socials"] = None

    if club.interests:
        club_dict["interests"] = [
            {
                "id": i.id,
                "name": i.name,
                "icon": i.icon,
                "icon_type": i.icon_type.value if hasattr(i.icon_type, "value") else str(i.icon_type) if i.icon_type else None,
            }
            for i in club.interests
        ]
    else:
        club_dict["interests"] = []

    return club_dict


async def update_club_logo(
    session: AsyncSession,
    club_id: int,
    logo: UploadFile,
) -> dict:
    """Update club logo."""
    club = await session.get(Clubs, club_id)
    if not club:
        raise CustomHTTPException(404, "Club not found")

    if club.logo:
        club.logo.delete()

    content = io.BytesIO(await logo.read())
    club.logo = {
        "bytes": content,
        "filename": logo.filename,
    }
    await session.commit()
    return {"message": "Club logo updated successfully"}


async def create_or_update_club_socials(
    session: AsyncSession,
    club_id: int,
    socials: ClubSocialsCreate,
) -> ClubSocials:
    """Create club social links."""
    club = await session.get(Clubs, club_id)
    if not club:
        raise CustomHTTPException(404, "Club not found")

    db_socials = await session.scalar(
        select(ClubSocials).where(
            ClubSocials.club_id == club_id,
            ClubSocials.is_deleted == False,
        )
    )
    if db_socials:
        db_socials.instagram = socials.instagram
        db_socials.linkedin = socials.linkedin
        db_socials.youtube = socials.youtube
        db_socials.website = socials.website
    else:
        db_socials = ClubSocials(
            club_id=club_id,
            instagram=socials.instagram,
            linkedin=socials.linkedin,
            youtube=socials.youtube,
            website=socials.website,
        )
        session.add(db_socials)
    await session.commit()
    await session.refresh(db_socials)
    return db_socials


async def get_club_events(
    session: AsyncSession, 
    club_id: int, 
    limit: int = 10, 
    offset: int = 0,
    is_ended: bool | None = None,
    search: str | None = None,
):
    """Get events of a club with optional past/upcoming filter and search."""
    from sqlalchemy.dialects.postgresql import INTERVAL
    
    # Subquery to count registrations for each event
    reg_link = aliased(EventRegistrationsLink)
    reg_count_subquery = (
        select(func.count(reg_link.id))
        .where(
            reg_link.event_id == Events.id,
            reg_link.is_deleted == False,
        )
        .correlate(Events)
        .scalar_subquery()
    )

    # Subquery to count attended registrations for each event
    attended_link = aliased(EventRegistrationsLink)
    attended_count_subquery = (
        select(func.count(attended_link.id))
        .where(
            attended_link.event_id == Events.id,
            attended_link.is_attended == True,
            attended_link.is_deleted == False,
        )
        .correlate(Events)
        .scalar_subquery()
    )
    
    query = (
        select(
            Events,
            reg_count_subquery.label("registration_count"),
            attended_count_subquery.label("attended_count"),
        )
        .where(Events.club_id == club_id, Events.is_deleted == False)
        .options(
            joinedload(Events.category),
            joinedload(Events.club),
        )
    )

    count_query = select(func.count(Events.id)).where(Events.club_id == club_id, Events.is_deleted == False)

    if search and search.strip():
        search_pattern = f"%{search.strip()}%"
        query = query.filter(Events.name.ilike(search_pattern))
        count_query = count_query.filter(Events.name.ilike(search_pattern))
    
    # Filter by ended status
    if is_ended is not None:
        event_end_time = Events.event_datetime + func.cast(
            func.concat(Events.duration, " HOURS"), INTERVAL
        )
        if is_ended:
            # Past events: event has ended
            query = query.filter(event_end_time < func.now())
            count_query = count_query.filter(event_end_time < func.now())
            query = query.order_by(Events.event_datetime.desc())
        else:
            # Upcoming events: event hasn't ended yet
            query = query.filter(event_end_time >= func.now())
            count_query = count_query.filter(event_end_time >= func.now())
            query = query.order_by(Events.event_datetime.asc())
    else:
        query = query.order_by(Events.event_datetime.desc())

    # Get total count
    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0
    
    query = query.limit(limit).offset(offset)
    
    results = await session.execute(query)
    
    # Convert results to list of dicts with counts
    items = [
        jsonable_encoder(event) | {
            "registration_count": reg_count,
            "attended_count": attended_count,
            # A property, so the encoder does not see it on its own.
            "status": event.status,
        }
        for event, reg_count, attended_count in results
    ]

    return items, total


# ─── Cover and partners ───────────────────────────────────────────────────


async def update_club_cover(session: AsyncSession, club_id: int, cover: UploadFile) -> dict:
    """The banner across the top of the club page."""
    club = await session.get(Clubs, club_id)
    if not club:
        raise CustomHTTPException(404, "Club not found")
    if club.cover:
        club.cover.delete()
    club.cover = {"bytes": io.BytesIO(await cover.read()), "filename": cover.filename}
    await session.commit()
    return {"message": "Club cover updated successfully"}


def _partner_out(p: ClubPartners) -> dict:
    return {"id": p.id, "name": p.name, "role": p.role, "since": p.since, "sort_order": p.sort_order, "logo": p.logo}


async def update_partner_logo(session: AsyncSession, p: ClubPartners, logo: UploadFile) -> dict:
    if p.logo:
        p.logo.delete()
    p.logo = {"bytes": io.BytesIO(await logo.read()), "filename": logo.filename}
    await session.commit()
    await session.refresh(p)
    return _partner_out(p)


async def list_partners(session: AsyncSession, club_id: int) -> list[ClubPartners]:
    rows = await session.scalars(
        select(ClubPartners).where(ClubPartners.club_id == club_id, ClubPartners.is_deleted == False).order_by(ClubPartners.sort_order, ClubPartners.id)  # noqa: E712
    )
    return list(rows.all())


async def partner_for(session: AsyncSession, club_id: int, partner_id: int) -> ClubPartners:
    p = await session.scalar(select(ClubPartners).where(ClubPartners.id == partner_id, ClubPartners.club_id == club_id, ClubPartners.is_deleted == False))  # noqa: E712
    if p is None:
        raise CustomHTTPException(404, message="Partner not found")
    return p


async def create_partner(session: AsyncSession, club_id: int, name: str, role: str | None, since: int | None) -> dict:
    last = await session.scalar(select(func.max(ClubPartners.sort_order)).where(ClubPartners.club_id == club_id, ClubPartners.is_deleted == False))  # noqa: E712
    p = ClubPartners(club_id=club_id, name=name.strip(), role=(role or "").strip() or None, since=since, sort_order=(last if last is not None else -1) + 1)
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return _partner_out(p)


async def update_partner(session: AsyncSession, p: ClubPartners, data: dict) -> dict:
    if "name" in data and data["name"] is not None:
        p.name = data["name"].strip()
    if "role" in data:
        p.role = (data["role"] or "").strip() or None
    if "since" in data:
        p.since = data["since"]
    if "sort_order" in data and data["sort_order"] is not None:
        p.sort_order = data["sort_order"]
    await session.commit()
    await session.refresh(p)
    return _partner_out(p)


async def delete_partner(session: AsyncSession, p: ClubPartners) -> None:
    p.is_deleted = True
    p.deleted_at = datetime.now(timezone.utc)
    await session.commit()


# ─── The team ────────────────────────────────────────────────────────────

TEAM_FIELDS = ("role", "instagram", "linkedin", "x", "website")


def _member_out(m: ClubTeamMembers) -> dict:
    return {"id": m.id, "name": m.name, "role": m.role, "instagram": m.instagram, "linkedin": m.linkedin, "x": m.x, "website": m.website, "sort_order": m.sort_order, "photo": m.photo}


async def list_team(session: AsyncSession, club_id: int) -> list[ClubTeamMembers]:
    rows = await session.scalars(
        select(ClubTeamMembers).where(ClubTeamMembers.club_id == club_id, ClubTeamMembers.is_deleted == False).order_by(ClubTeamMembers.sort_order, ClubTeamMembers.id)  # noqa: E712
    )
    return list(rows.all())


async def member_for(session: AsyncSession, club_id: int, member_id: int) -> ClubTeamMembers:
    m = await session.scalar(select(ClubTeamMembers).where(ClubTeamMembers.id == member_id, ClubTeamMembers.club_id == club_id, ClubTeamMembers.is_deleted == False))  # noqa: E712
    if m is None:
        raise CustomHTTPException(404, message="Team member not found")
    return m


async def create_member(session: AsyncSession, club_id: int, data: dict) -> dict:
    last = await session.scalar(select(func.max(ClubTeamMembers.sort_order)).where(ClubTeamMembers.club_id == club_id, ClubTeamMembers.is_deleted == False))  # noqa: E712
    m = ClubTeamMembers(club_id=club_id, name=data["name"].strip(), sort_order=(last if last is not None else -1) + 1, **{k: (data.get(k) or None) for k in TEAM_FIELDS})
    session.add(m)
    await session.commit()
    await session.refresh(m)
    return _member_out(m)


async def update_member(session: AsyncSession, m: ClubTeamMembers, data: dict) -> dict:
    if "name" in data and data["name"] is not None:
        m.name = data["name"].strip()
    for k in TEAM_FIELDS:
        if k in data:
            setattr(m, k, data[k] or None)
    if "sort_order" in data and data["sort_order"] is not None:
        m.sort_order = data["sort_order"]
    await session.commit()
    await session.refresh(m)
    return _member_out(m)


async def update_member_photo(session: AsyncSession, m: ClubTeamMembers, photo: UploadFile) -> dict:
    if m.photo:
        m.photo.delete()
    m.photo = {"bytes": io.BytesIO(await photo.read()), "filename": photo.filename}
    await session.commit()
    await session.refresh(m)
    return _member_out(m)


async def delete_member(session: AsyncSession, m: ClubTeamMembers) -> None:
    m.is_deleted = True
    m.deleted_at = datetime.now(timezone.utc)
    await session.commit()


# ─── One follower, as the club may know them ─────────────────────────────


async def follower_profile(session: AsyncSession, club_id: int, user_id: int) -> dict:
    """Who this follower is and what they did with the club: nothing they did not give the club."""
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.users.models import UserProfiles, Users

    link = await session.scalar(
        select(ClubUsersLink).where(ClubUsersLink.club_id == club_id, ClubUsersLink.user_id == user_id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False)  # noqa: E712
    )
    if link is None:
        raise CustomHTTPException(404, message="Not a follower of this club")
    user = await session.scalar(select(Users).where(Users.id == user_id).options(joinedload(Users.profile).options(joinedload(UserProfiles.avatar), joinedload(UserProfiles.org)), selectinload(Users.interests)))
    if user is None:
        raise CustomHTTPException(404, message="Not a follower of this club")
    profile = user.profile
    rows = (await session.execute(
        select(EventRegistrationsLink, Events)
        .join(Events, Events.id == EventRegistrationsLink.event_id)
        .where(EventRegistrationsLink.user_id == user_id, EventRegistrationsLink.is_deleted == False, Events.club_id == club_id, Events.is_deleted == False)  # noqa: E712
        .order_by(Events.event_datetime.desc())
    )).all()
    events = [
        dict(event_id=e.id, name=e.name, starts_at=e.event_datetime.isoformat() if e.event_datetime else None, status=e.status, attended=bool(r.is_attended), paid=bool(r.is_paid), paid_amount=float(r.paid_amount or 0), registered_at=r.created_at.isoformat() if r.created_at else None)
        for r, e in rows
    ]
    # The club learns a contact only from a registration, where the person gave it.
    contact = next((r for r, _ in rows if r.email or getattr(r, "phone", None)), None)
    avatar = None
    if profile is not None:
        avatar = profile.profile_pic or (profile.avatar.image if profile.avatar else None)
    return dict(
        user=dict(id=user.id, full_name=(profile.full_name if profile and profile.full_name else user.full_name), username=user.username, avatar=avatar, org=(profile.org.name if profile and profile.org else None), interests=[i.name for i in (user.interests or [])]),
        followed_at=link.created_at.isoformat() if link.created_at else None,
        contact=dict(email=contact.email if contact else None, phone=(getattr(contact, "phone", None) if contact else None)),
        events=events,
        totals=dict(registrations=len(events), attended=sum(1 for x in events if x["attended"]), paid=round(sum(x["paid_amount"] for x in events), 2)),
    )
