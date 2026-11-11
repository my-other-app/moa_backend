from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Request,
    Query,
    Response,
    File,
    UploadFile,
)
import hashlib
from app.core.utils.pdf import generate_image_bytes, generate_pdf_bytes  # noqa: F401 - tests patch generate_pdf_bytes here
from app.api.events import doc_cache
from app.api.events.documents import (
    apply_layers,
    document_values,
    image_width,
    in_layout_mode,
    layout_png,
    qr_data_url,
)
from app.api.templates_images import CERTIFICATE_FIELDS, TICKET_FIELDS, embedded
from app.core.layout import pool
from app.core.middlewares.etag_middleware import matches
from app.core.net import client_address
from app.core.rate_limit import limiter
from fastapi.encoders import jsonable_encoder
import logging
from typing import Optional, List

from app.db.core import SessionDep
from app.api.events import service
from app.api.discover import geo
from app.api.audit import service as audit
from app.core.response.sorting import apply_sort
from app.api.events.access import is_admin_user
from app.api.events import wallet_service
from app.api.subscriptions import gates
from app.api.users.models import UserTypes
from app.api.templates_audience import RegistrationRole
from app.api.events.tickets import (
    TICKET_REVOKED_CODE,
    TICKET_REVOKED_MESSAGE,
    is_revoked,
    role_of,
    ticket_job,
    ticket_payload,
    ticket_style,
)
from app.api.events.certificates import (
    availability,
    certificate_context,
    certificate_job,
    certificate_style,
    number_of,
    public_url_of,
    qr_text as certificate_qr_text,
)
from app.response import CustomHTTPException
from app.api.events.schemas import (
    EventAdminListResponse,
    EventCategoryCreate,
    EventCategoryResponse,
    EventCreate,
    EventCreateUpdateResponse,
    EventPublicMin,
    EventDetailResponse,
    EventEdit,
    EventListResponse,
    EventRating,
    EventRatingCreate,
    TicketDetailsResponse,
)
from app.core.auth.dependencies import (
    AdminAuth,
    ClubAuth,
    DependsAuth,
    OptionalAuth,
    UserAuth,
)
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.events import recommendations
from app.api.events.volunteer.router import router as volunteer_router
from app.api.events.registration.router import router as registration_router

from app.core.auth.permissions import ContentAuth, ModerateAuth
from app.core import public_urls

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events")

router.include_router(volunteer_router, tags=["Volunteer"])
router.include_router(registration_router, tags=["Registration"])


def _follow_map_link(background_tasks: BackgroundTasks, event) -> None:
    """A venue still without a point is placed after the answer: its short map
    link (maps.app.goo.gl…) followed, or else its words geocoded."""
    if geo.needs_locating(event):
        background_tasks.add_task(geo.locate_and_store, "event", event.id)


def _role_of(registration) -> Optional[RegistrationRole]:
    """See app.api.events.tickets.role_of; kept under this name for the callers here."""
    return role_of(registration)


async def _assert_can_access_ticket(session, user, registration) -> None:
    """Allow the ticket's own user, the event's owning club, a club
    co-hosting it (app/api/cohosts), or an admin."""
    if user.user_type == UserTypes.admin:
        return
    if registration.user_id == user.id:
        return
    if user.club and registration.event and registration.event.club_id == user.club.id:
        return
    if user.club and registration.event:
        from app.api.cohosts import access as cohosts

        if await cohosts.is_cohost(session, registration.event.id, user.club.id):
            return
    raise CustomHTTPException(403, "You do not have permission to access this ticket.")


def _assert_ticket_not_revoked(registration) -> None:
    """Refuse the ticket's documents once the organiser has taken it back.

    410 Gone, like the emailed link's own refusal (app/api/emails/router.py:
    `ticket_file`): the ticket existed, it is the right ticket, and it is not
    coming back by trying again -- which is not what a 403 from
    `_assert_can_access_ticket` means, and not what the volunteer scanner's
    400 means either (it answers about a scan, not about a file). The app's
    ticket screen already treats 403, 404 and 410 on a document as a refusal
    and drops the copy kept on the phone, so the stale design and its QR go
    with the server's. The code is the door's own, TICKET_REVOKED.
    """
    if is_revoked(registration):
        raise CustomHTTPException(410, TICKET_REVOKED_MESSAGE, error_code=TICKET_REVOKED_CODE)


@router.post("/categories/create", summary="Create a new event category")
async def create_event_category(
    user: ContentAuth, category: EventCategoryCreate, session: SessionDep = SessionDep
) -> EventCategoryResponse:
    return await service.create_event_category(session, category, user.id)


@router.get("/categories/list", summary="List all event categories")
async def list_event_categories(
    session: SessionDep, user: DependsAuth
) -> List[EventCategoryResponse]:
    return await service.list_event_categories(session)


@router.post("/create", summary="Create a new event")
async def create_event(
    user: ClubAuth,
    background_tasks: BackgroundTasks,
    session: SessionDep = SessionDep,
    event: EventCreate = Depends(),
    speaker_photos: List[UploadFile] = File([]),
) -> EventCreateUpdateResponse:
    store_item_ids = None
    if user.club is not None:
        await gates.check_event_create(session, user.club.id, has_fee=event.has_fee, actor=user)
        # The store and the handouts switched on from the start need the
        # plan's features (a new event is never grandfathered), and the items
        # it offers must be the club's, within the plan's room.
        if event.merch_enabled:
            await gates.require_feature(session, user.club.id, "merch", actor=user)
        if event.swag_enabled:
            await gates.require_feature(session, user.club.id, "swag", actor=user)
        if event.store_item_ids is not None:
            from types import SimpleNamespace

            from app.api.store import service as store

            unsaved = SimpleNamespace(id=None, club_id=user.club.id, created_at=None, event_datetime=event.event_datetime)
            store_item_ids = await store.check_offers(session, unsaved, event.store_item_ids, actor=user)
    created = await service.create_event(
        session,
        user_id=user.id,
        name=event.name,
        event_datetime=event.event_datetime,
        duration=event.duration,
        category_id=event.category_id,
        poster=event.poster,
        has_fee=event.has_fee,
        reg_fee=event.reg_fee,
        location_name=event.location_name,
        location_link=event.location_link,
        has_prize=event.has_prize,
        prize_amount=event.prize_amount,
        is_online=event.is_online,
        merch_enabled=bool(event.merch_enabled),
        swag_enabled=bool(event.swag_enabled),
        store_item_ids=store_item_ids,
        reg_startdate=event.reg_startdate,
        reg_enddate=event.reg_enddate,
        about=event.about,
        contact_name=event.contact_name,
        contact_phone=event.contact_phone,
        contact_email=event.contact_email,
        url=event.url,
        additional_details=event.additional_details,
        interest_ids=event.interest_ids,
        max_participants=event.max_participants,
        event_guidelines=event.event_guidelines,
        event_tag=event.event_tag,
        speakers=event.speakers,
        speaker_photos=speaker_photos,
        status=event.status,
        pin=event.pin,
        requires_approval=event.requires_approval,
        high_demand_label=event.high_demand_label,
        allow_self_cancel=event.allow_self_cancel,
    )
    # Its first ticket type, at the event's own price and cap (app/api/ticketing).
    from app.api.ticketing import types as ticket_types

    await ticket_types.follow_event_edit(session, created)
    _follow_map_link(background_tasks, created)
    await _form_meta_on(session, created)
    return created


@router.put("/update/{event_id}", summary="Update an event")
async def update_event(
    request: Request,
    event_id: int,
    user: ClubAuth,
    background_tasks: BackgroundTasks,
    session: SessionDep = SessionDep,
    event: EventEdit = Depends(),
    speaker_photos: List[UploadFile] = File([]),
) -> EventCreateUpdateResponse:
    from app.api.club_access import service as club_access

    member = club_access.current_member(request)
    updated = await service.update_event(
        session,
        event,
        user.id,
        event_id=event_id,
        speaker_photos=speaker_photos,
        as_admin=is_admin_user(user),
        actor=user,
        member_id=getattr(member, "id", None),
    )
    # A single ticket type shows the event's price and cap again; with
    # several, the event's fee is what they charge (app/api/ticketing).
    from app.api.ticketing import types as ticket_types

    await ticket_types.follow_event_edit(session, updated)
    _follow_map_link(background_tasks, updated)
    await _form_meta_on(session, updated)
    return updated


async def _form_meta_on(session, event) -> None:
    """The questions' version and when they last changed, on an event the
    response is about to send (app/api/missing_answers)."""
    from app.api.missing_answers import service as missing_answers

    version, at = (await missing_answers.form_meta(session, [event.id])).get(event.id, (None, None))
    event.form_version = version
    event.form_changed_at = at


async def _hide_draft(session, event_id: str, user) -> None:
    """404 for a draft unless the caller is its host (or one of the host's
    team), a co-host or an admin: a draft is not out yet, and the page
    carries its contact details. The same 404 as an event that does not
    exist, so an id or slug says nothing about what is being prepared."""
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.api.closeout.models import EventStatus
    from app.api.cohosts import access as cohosts
    from app.api.events.models import Events

    key = Events.id == int(event_id) if str(event_id).isdigit() else Events.slug == event_id
    row = (await session.execute(select(Events.id, Events.club_id, Events.lifecycle).where(key, Events.is_deleted == False))).first()  # noqa: E712
    if row is None or row.lifecycle is not EventStatus.draft:
        return
    if user is not None and (is_admin_user(user) or await cohosts.viewer_role(session, SimpleNamespace(id=row.id, club_id=row.club_id), user)):
        return
    raise CustomHTTPException(404, message="Event not found")


@router.get("/info/{event_id}", summary="Get event info")
async def get_event(
    request: Request,
    event_id: str,
    session: SessionDep = SessionDep,
    user: OptionalAuth = None,
) -> EventDetailResponse:
    """404 for a draft, unless the caller hosts or co-hosts it or is an admin."""
    await _hide_draft(session, event_id, user)
    # Increment view count
    try:
        await service.increment_event_page_view(
            session, request, event_id, user_id=user.id if user else None
        )
    except Exception:
        # Don't fail the request if view count increment fails
        pass

    result = await service.get_event(
        session, event_id, user_id=user.id if user else None
    )
    data = jsonable_encoder(result)
    # The signed-in club's place on it: "host", "cohost" or null (app/api/cohosts).
    from types import SimpleNamespace

    from app.api.cohosts import access as cohosts

    shell = SimpleNamespace(id=data.get("id"), club_id=(data.get("club") or {}).get("id"))
    data["viewer_role"] = await cohosts.viewer_role(session, shell, user) if shell.club_id is not None else None
    data["form_version"] = data["form_changed_at"] = None
    if data["viewer_role"] is not None or (user is not None and is_admin_user(user)):
        # The club's own edit form: which version of the questions it is editing (app/api/missing_answers).
        from app.api.missing_answers import service as missing_answers

        version, at = (await missing_answers.form_meta(session, [data.get("id")])).get(data.get("id"), (None, None))
        data["form_version"], data["form_changed_at"] = version, at
    if data.get("status") == "cancelled":
        from app.api.club_ops.cancel import public_cancellation

        data["cancellation"] = await public_cancellation(session, data.get("id"))
    return data


@router.get("/admin/list", summary="List all events (admin)")
async def list_all_events_admin(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    search: Optional[str] = Query(None, description="Search by event name or category"),
    club_id: Optional[int] = Query(None, description="Filter by club"),
    cohost_club_id: Optional[int] = Query(None, description="The events this club co-hosts (app/api/cohosts)"),
    org_id: Optional[int] = Query(
        None, description="Filter by the organisation the hosting club belongs to"
    ),
    category_id: Optional[int] = Query(None, description="Filter by category"),
    is_ended: Optional[bool] = Query(None, description="Past (true) or upcoming (false)"),
    has_fee: Optional[bool] = Query(None, description="Paid events only"),
    sort: Optional[str] = Query(None, description="created, date, name or views"),
    order: Optional[str] = Query(None, description="asc or desc"),
) -> PaginatedResponse[EventAdminListResponse]:
    """List all events for an admin, with search, filters and ordering."""
    from sqlalchemy import select, func, or_
    from sqlalchemy.orm import selectinload
    from app.api.events.models import Events, EventCategories
    from app.api.interests.models import Interests

    query = (
        select(Events)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests).joinedload(Interests.category),
        )
    )
    count_query = select(func.count(Events.id))

    if search and search.strip():
        search_pattern = f"%{search.strip()}%"

        query = query.outerjoin(EventCategories, Events.category_id == EventCategories.id).filter(
            or_(
                Events.name.ilike(search_pattern),
                EventCategories.name.ilike(search_pattern),
            )
        )
        count_query = count_query.outerjoin(EventCategories, Events.category_id == EventCategories.id).filter(
            or_(
                Events.name.ilike(search_pattern),
                EventCategories.name.ilike(search_pattern),
            )
        )

    # Applied to both queries, or the reported total disagrees with the rows.
    from datetime import datetime, timezone

    filters = []
    if club_id is not None:
        filters.append(Events.club_id == club_id)
    if cohost_club_id is not None:
        from app.api.cohosts import access as cohosts

        filters.append(Events.id.in_(cohosts.cohosted_event_ids(cohost_club_id)))
    if org_id is not None:
        # Events belong to clubs, and clubs to organisations; an organisation's
        # page needs its events without first fetching all of its clubs.
        from app.api.clubs.models import Clubs

        filters.append(
            Events.club_id.in_(
                select(Clubs.id).where(
                    Clubs.org_id == org_id, Clubs.is_deleted == False
                )
            )
        )
    if category_id is not None:
        filters.append(Events.category_id == category_id)
    if has_fee is not None:
        filters.append(Events.has_fee == has_fee)
    if is_ended is not None:
        now = datetime.now(timezone.utc)
        filters.append(
            Events.event_datetime < now if is_ended else Events.event_datetime >= now
        )
    for condition in filters:
        query = query.filter(condition)
        count_query = count_query.filter(condition)

    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    query = apply_sort(
        query,
        columns={
            "created": Events.created_at,
            "date": Events.event_datetime,
            "name": Events.name,
            "views": Events.page_views,
        },
        default=Events.created_at,
        sort=sort,
        order=order,
    ).offset(pagination.offset).limit(pagination.limit)
    result = await session.execute(query)
    events = result.scalars().all()

    # Registration and attendance counts, from one grouped query over the
    # page's ids rather than a correlated subquery per row or a join on the
    # listing. A join here would fan out against the category join above and
    # multiply the page_views column it already selects.
    rows = paginated_response(
        events, request, schema=EventAdminListResponse, total=total
    )
    if events:
        from app.api.events.models import EventRegistrationsLink

        counts = (
            await session.execute(
                select(
                    EventRegistrationsLink.event_id,
                    func.count(EventRegistrationsLink.id),
                    func.count(EventRegistrationsLink.id).filter(
                        EventRegistrationsLink.is_attended == True
                    ),
                )
                .where(
                    EventRegistrationsLink.event_id.in_([e.id for e in events]),
                    EventRegistrationsLink.is_deleted == False,
                )
                .group_by(EventRegistrationsLink.event_id)
            )
        ).all()
        by_event = {event_id: (total_, present) for event_id, total_, present in counts}
        # Registration requests, waiting and all, one grouped statement for the page.
        from app.api.approvals import service as approvals

        asked = await approvals.totals(session, [e.id for e in events])
        # Each event's co-hosts (app/api/cohosts).
        from app.api.cohosts import access as cohosts
        from app.api.cohosts.schemas import CohostRef

        shared = await cohosts.refs(session, [e.id for e in events])
        for item in rows.items:
            item.cohosts = [CohostRef(**c) for c in shared.get(item.id, [])]
            registered, present = by_event.get(item.id, (0, 0))
            item.registration_count = registered
            item.attended_count = present
            item.requests_pending = asked.get(item.id, {}).get("pending", 0)
            item.requests_total = asked.get(item.id, {}).get("total", 0)

    return rows


@router.get("/list", summary="List all events")
async def list_events(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
    is_following: Optional[bool] = Query(None),
    is_registered: Optional[bool] = Query(None),
    is_ended: Optional[bool] = Query(None),
    interest_ids: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search by event name or category"),
) -> PaginatedResponse[EventListResponse]:
    """List events with optional filters and search."""
    if is_registered:
        # The app's upcoming and past tickets: guests waiting under the
        # address are folded in first, so a website ticket shows up here.
        from app.api.users import service as user_service

        await user_service.merge_guests_now(session, user)
    try:
        interest_ids = [int(i) for i in interest_ids.split(",")] if interest_ids else []
    except ValueError:
        raise CustomHTTPException(422, message="interest_ids must be numbers separated by commas")
    events = await service.list_events(
        session=session,
        user_id=user.id if user else None,
        limit=pagination.limit,
        offset=pagination.offset,
        is_following=is_following,
        is_registered=is_registered,
        is_ended=is_ended,
        interest_ids=interest_ids,
        search=search,
    )
    
    # Enhance events with user-specific registration data
    enhanced_events = []
    if user and events:
        from app.api.events.models import EventRegistrationsLink
        from sqlalchemy import select
        
        # Get all registrations for this user for these events
        event_ids = [e.id for e in events]
        registrations_query = select(EventRegistrationsLink).where(
            EventRegistrationsLink.event_id.in_(event_ids),
            EventRegistrationsLink.user_id == user.id,
            EventRegistrationsLink.is_deleted == False,
        )
        result = await session.execute(registrations_query)
        registrations = {r.event_id: r for r in result.scalars().all()}
        
        for event in events:
            event_dict = jsonable_encoder(event)
            reg = registrations.get(event.id)
            if reg:
                event_dict["is_attended"] = reg.is_attended
                event_dict["is_paid"] = bool(reg.is_paid)
                event_dict["ticket_id"] = reg.ticket_id
                event_dict["ticket_url"] = public_urls.ticket_url(reg.ticket_id)
                # Revoked by the organiser: the door refuses it by name, so
                # a ticket kept for offline use drops it on the next refresh.
                event_dict["ticket_revoked"] = reg.ticket_revoked_at is not None
                # Which ticket type it is, from the same registration row.
                event_dict["ticket_label"] = reg.ticket_label
                # The certificate as the PDF and the image decide it: one
                # issued without a check-in counts, a check-in without an
                # issuing does not, and neither does a revoked one.
                held = availability(reg)
                event_dict["has_certificate"] = held.available
                event_dict["certificate_revoked"] = held.reason == "revoked"
                if held.available:
                    # Its public page by the certificate's own public id,
                    # and the number printed on it: neither is the ticket's.
                    event_dict["certificate_url"] = public_url_of(reg)
                    event_dict["certificate_no"] = number_of(reg) or None
            enhanced_events.append(event_dict)
        # The club asking the caller for answers they're missing, on the
        # events they're registered for (app/api/missing_answers): one
        # statement for the page, more only for an event with an open ask.
        from app.api.missing_answers import service as missing_answers

        by_id = {e.id: e for e in events}
        flags = await missing_answers.flags_for_registrations(
            session, [(r.id, by_id.get(r.event_id)) for r in registrations.values() if r.ticket_revoked_at is None],
        )
        for event_dict in enhanced_events:
            reg = registrations.get(event_dict.get("id"))
            event_dict["answers_needed"] = jsonable_encoder(flags.get(reg.id)) if reg is not None else None
    else:
        enhanced_events = [jsonable_encoder(e) for e in events]
    # Each event's co-hosts (app/api/cohosts).
    from app.api.cohosts import access as cohosts

    await cohosts.attach(session, enhanced_events)
    return paginated_response(enhanced_events, request, schema=EventListResponse)



@router.get("/for-you", summary="Personalised event feed")
async def for_you(
    request: Request,
    pagination: PaginationParams,
    session: SessionDep,
    user: DependsAuth,
    background_tasks: BackgroundTasks,
    debug_scores: bool = Query(False, description="Include each event's score"),
    lat: Optional[float] = Query(None, ge=-90, le=90, description="The device's latitude: the popular events near it are candidates; without it the address places the user, roughly"),
    lng: Optional[float] = Query(None, ge=-180, le=180, description="The device's longitude"),
) -> PaginatedResponse[EventListResponse]:
    """Upcoming events ranked for this user.

    Replaces calling /events/list with no filters, which returned every event
    ever created oldest-first -- so the feed opened on an event from years ago.
    The ranking is kept for a few minutes (recommendations.KEEP_SECONDS).
    """
    # Where the user is picks the region whose popular events are candidates:
    # the device's point, else what the address lookup already knows. That
    # lookup is never waited for here; one not yet made is made after the
    # response, for next time.
    if lat is not None and lng is not None:
        where = dict(lat=lat, lng=lng)
    else:
        ip = geo.client_ip(request)
        known, where = geo.known_place(ip)
        if not known:
            background_tasks.add_task(geo.locate_ip, ip)
    events, scores = await recommendations.for_you_events(
        session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        where=where,
    )

    payload = []
    for event, score in zip(events, scores):
        item = jsonable_encoder(event)
        if debug_scores:
            item["_score"] = round(score, 3)
        payload.append(item)
    # Each event's co-hosts (app/api/cohosts).
    from app.api.cohosts import access as cohosts

    await cohosts.attach(session, payload)
    return paginated_response(payload, request, schema=EventListResponse)


@router.delete("/delete/{event_id}", summary="Delete an event")
async def delete_event(
    event_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
    request: Request = None,
):
    await service.delete_event(session, event_id, user.id, as_admin=is_admin_user(user))
    if is_admin_user(user):
        await audit.record(
            session, actor=user, action="event.delete", entity_type="event",
            entity_id=event_id, summary="Deleted event", request=request,
        )
        await session.commit()
    return {"message": "Event deleted successfully"}


@router.post("/rate/{event_id}", response_model=EventRating)
async def rate_event_endpoint(
    event_id: int,
    rating_data: EventRatingCreate,
    session: SessionDep,
    user: UserAuth,
):
    """Rate an event. Rating must be between 0 and 5."""
    return await service.rate_event(
        session=session,
        event_id=event_id,
        user_id=user.id,
        rating=rating_data.rating,
        review=rating_data.review,
    )
    
    
@router.get("/ratings/{event_id}", summary="List event ratings")
async def list_event_ratings(
    request: Request,
    event_id: int,
    pagination: PaginationParams,
    session: SessionDep,
    user: ClubAuth,
):
    """List detailed ratings for an event (Club Admin only)."""
    ratings = await service.list_event_ratings(
        session=session,
        event_id=event_id,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
        as_admin=is_admin_user(user),
    )
    # Return raw dict to avoid Pydantic schema mismatch
    return {
        "limit": pagination.limit,
        "offset": pagination.offset,
        "total": None,
        "next": None,
        "items": ratings,
    }


@router.get("/tickets/{ticket_id}", summary="Get ticket details")
async def get_ticket(
    request: Request,
    ticket_id: int | str,
    session: SessionDep = SessionDep,
    user: DependsAuth = None,  # Require authentication
) -> TicketDetailsResponse:
    result = await service.get_ticket_details(session, ticket_id=ticket_id)
    await _assert_can_access_ticket(session, user, result)
    data = jsonable_encoder(result)
    # The club asking its holder for answers they're missing: a note on the
    # ticket while an ask is open (app/api/missing_answers). The holder's own.
    data["answers_needed"] = None
    if user is not None and result.user_id == user.id and result.event is not None and not is_revoked(result):
        from app.api.missing_answers import service as missing_answers

        flags = await missing_answers.flags_for_registrations(session, [(result.id, result.event)])
        data["answers_needed"] = jsonable_encoder(flags.get(result.id))
    # Revoked by the organiser, under the name the event list already uses
    # for it (EventListResponse.ticket_revoked). The ticket's own page is the
    # only thing a guest reading the link from their email can ask -- there
    # is no list for them -- so without this they were told nothing.
    data["ticket_revoked"] = is_revoked(result)
    # The certificate's public page, once issued and not revoked, by its
    # own public id: where an old /certificates/{ticket id} address sends
    # its holder (the site reads this, signed in as them).
    data["certificate_url"] = public_url_of(result) if availability(result).available else None
    # The event's state, as its page says it, so the ticket can say
    # "Cancelled" without asking the event page as well. The event is loaded
    # with the ticket; only a cancelled one costs a query, for the reason.
    if result.event is not None and isinstance(data.get("event"), dict):
        status = result.event.status
        data["event"]["status"] = status
        if status == "cancelled":
            from app.api.club_ops.cancel import public_cancellation

            data["event"]["cancellation"] = await public_cancellation(session, result.event.id)
    return data


@router.get("/tickets/{ticket_id}/image", summary="Get ticket as an image")
async def get_ticket_image(
    request: Request,
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: DependsAuth = None,
) -> Response:
    """The ticket as a PNG, for showing on screen.

    The app drew its own ticket -- a hardcoded card with its own colours and
    its own QR -- and the event website drew a third one. Only the PDF
    download carried the club's design. This is the same template, the same
    payload and the same engine as the PDF, so what the attendee sees in the
    app is the ticket the club designed.

    Same access rule as the PDF: the ticket's owner, the owning club, or an
    admin. A QR is an entry pass, not a public image.

    Tagged like the PDF, from the same fingerprint (_document_etag, with
    "-png"): a 304 without drawing when If-None-Match names it. The
    fingerprint names the revocation too, so a revoked ticket can never be
    answered 304 against the picture a phone kept while it still worked.

    A revoked ticket is refused (410, TICKET_REVOKED): the door will not
    open on this QR, so nothing here draws one.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    await _assert_can_access_ticket(session, user, registration)
    _assert_ticket_not_revoked(registration)

    style = await ticket_style(session, registration)
    job = ticket_job(registration, style)
    etag = _document_etag(job, "png")
    headers = {"Cache-Control": "private, max-age=300"}
    if matches(_if_none_match(request), etag):
        return Response(status_code=304, headers={**headers, "ETag": etag})

    def tagged(whole: bool) -> dict:
        return {**headers, "ETag": etag} if whole else headers

    if in_layout_mode(style):
        # The PDF the download is (kept in the bucket once drawn), as a PNG.
        pdf, whole = await _drawn(job, job.cache_key("ticket", registration.id))
        png = await pool.run(layout_png, style, pdf)
        return Response(content=png, media_type="image/png", headers=tagged(whole))

    payload = ticket_payload(registration)
    styled = apply_layers(style, document_values(registration, registration.event))
    # The built-in card is 600px wide; a layered design is its artwork.
    width = image_width(styled, default=600)
    qr_url = qr_data_url(registration.ticket_id)
    complete = [True]

    def draw() -> bytes:
        inline, complete[0] = embedded(styled, fields=TICKET_FIELDS)
        return generate_image_bytes(
            template_path="events/ticket.html",
            context={"email_payload": payload, "style": inline, "qr_code_url": qr_url},
            width=width,
        ).getvalue()

    png = await pool.run(draw)
    return Response(content=png, media_type="image/png", headers=tagged(complete[0]))


@router.get("/count-view/{event_id}", summary="Increment event view count")
async def increment_view_count(
    request: Request,
    event_id: str,
    session: SessionDep = SessionDep,
    user: OptionalAuth = None,
) -> dict:
    if event_id.isdigit():
        event_id = int(event_id)

    await service.increment_event_page_view(
        session, event_id=event_id, user_id=user.id if user else None, request=request
    )
    return {"message": "Done"}



def _document_etag(job, kind: str = "") -> str:
    """A ticket's or certificate's PDF tag: the name it is kept under in the
    bucket, `{design version}-{fingerprint}` (app.api.events.doc_cache.key_for),
    where the fingerprint hashes everything the document prints. Worked out
    without drawing anything or reading the bucket. Another rendering of the
    same document (the ticket's PNG) adds its kind, "-png"."""
    suffix = f"-{kind}" if kind else ""
    return f'"{int(job.design_version or 0)}-{job.fingerprint}{suffix}"'


def _if_none_match(request: Request) -> str:
    return ", ".join(request.headers.getlist("if-none-match"))


async def _drawn(job, key: Optional[str]) -> tuple[bytes, bool]:
    """The document's bytes, kept or drawn (doc_cache.cached), and whether
    they are whole. A render that could not read one of its images goes out
    but is not kept. It gets no tag either: a phone holding the copy without
    its logo would be told it is current until the design changed. A kept
    one is always whole."""
    whole = [True]

    def draw() -> tuple[bytes, bool]:
        pdf, complete = job.draw()
        whole[0] = complete
        return pdf, complete

    return await doc_cache.cached(key, draw), whole[0]


@router.get("/tickets/{ticket_id}/pdf", summary="Get ticket PDF")
async def get_ticket_pdf(
    request: Request,
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: DependsAuth = None,
) -> Response:
    """
    Generate and download a PDF for the ticket.

    Its ETag names this ticket as drawn (_document_etag). Asked again with
    that tag in If-None-Match, an unchanged ticket is a 304, decided without
    drawing it or reading the bucket. The access check comes first.

    A revoked ticket is refused (410, TICKET_REVOKED), as the emailed link's
    own download has always been (app/api/emails/router.py). Revoking it also
    changes the fingerprint, so the copy kept in the bucket is retired and no
    held ETag matches.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    await _assert_can_access_ticket(session, user, registration)
    _assert_ticket_not_revoked(registration)

    job = ticket_job(registration, await ticket_style(session, registration))
    etag = _document_etag(job)
    if matches(_if_none_match(request), etag):
        return Response(status_code=304, headers={"ETag": etag})
    content, whole = await _drawn(job, job.cache_key("ticket", registration.id))
    headers = {"Content-Disposition": f'attachment; filename="ticket-{registration.ticket_id}.pdf"'}
    if whole:
        headers["ETag"] = etag
    return Response(content=content, media_type="application/pdf", headers=headers)


@router.get("/tickets/{ticket_id}/wallet.pkpass", summary="Get Apple Wallet pass")
async def get_wallet_pass(
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: DependsAuth = None,  # Require authentication for security
) -> Response:
    """
    Generate and download an Apple Wallet pass (.pkpass) for the ticket.
    
    Requires authentication. The pass can be added to Apple Wallet on iOS devices.

    A revoked ticket is refused (410, TICKET_REVOKED). A pass already in
    Wallet is not recalled by this -- there is no push service registered for
    these passes -- but it stops being handed out, and the door refuses the
    barcode on it (app/api/checkins).
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    await _assert_can_access_ticket(session, user, registration)
    _assert_ticket_not_revoked(registration)

    pkpass_bytes = await wallet_service.generate_wallet_pass(session, ticket_id)
    
    return Response(
        content=pkpass_bytes,
        media_type="application/vnd.apple.pkpass",
        headers={
            "Content-Disposition": f'attachment; filename="ticket-{ticket_id}.pkpass"'
        }
    )


async def _holders_registration(session, code: str, user):
    """The registration a certificate route's code names.

    By the certificate's public id (the link its holder shares, 24 URL-safe
    characters): anyone, as its public page shows it. By a ticket id: only
    whoever may see that ticket (_assert_can_access_ticket: its holder, its
    club or a co-host, an admin) -- the app's certificate screen, the club's
    list. The ticket id is what the door scans, so a link shared to show a
    certificate must never be one; an address from before with a ticket id
    in it opens nothing for a stranger (404 signed out, 403 signed in as
    someone else, as the ticket itself answers), and for its holder
    GET /events/tickets/{ticket_id} says the `certificate_url` to go to."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload, selectinload

    from app.api.certificates import service as certificates
    from app.api.events.models import EventRegistrationsLink, Events

    missing = CustomHTTPException(404, "No certificate here.", error_code="CERTIFICATE_NOT_FOUND")
    public_id = certificates.code_public_id(code)
    if public_id is not None:
        registration = await certificates.certificate_by_public_id(session, public_id)
        if registration is None:
            raise missing
        return registration
    if user is None:
        raise missing
    registration = await session.scalar(
        select(EventRegistrationsLink)
        .where(EventRegistrationsLink.ticket_id == code.strip(), EventRegistrationsLink.is_deleted == False)  # noqa: E712
        .options(
            joinedload(EventRegistrationsLink.event).options(
                joinedload(Events.club), joinedload(Events.category), selectinload(Events.cohost_clubs)
            ),
            joinedload(EventRegistrationsLink.user),
        )
    )
    if registration is None:
        raise missing
    # 403 for a session that is not the ticket's, as GET /events/tickets/{id}
    # answers it: never 401, which tells the website its session is gone.
    await _assert_can_access_ticket(session, user, registration)
    return registration


@router.get("/certificates/{ticket_id}/style", summary="Get certificate template style")
async def get_certificate_style(
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: OptionalAuth = None,
) -> dict:
    """
    Return the certificate template style config for a ticket's event: by
    the certificate's public id, or by the ticket id for the ticket's
    holder, its club or an admin (_holders_registration).
    """
    registration = await _holders_registration(session, ticket_id, user)
    from app.api.certificate_templates.service import get_template_for_event
    style = await get_template_for_event(
        session, registration.event_id, role=_role_of(registration)
    )
    return style


async def _certificate_for(session, ticket_id: str, user):
    """The registration and its certificate design, once it holds one: by
    its public id, or by ticket id for the ticket's holder, its club or an
    admin (_holders_registration).

    The PNG and the PDF both come through here, so the preview the app shows
    and the file the download produces are the same document: the same
    design, the same values, the same engine.
    """
    registration = await _holders_registration(session, ticket_id, user)

    held = availability(registration)
    if not held.available:
        raise CustomHTTPException(
            403,
            message=(
                "This certificate was revoked."
                if held.reason == "revoked"
                else "No certificate has been issued for this event yet. "
                "It will be here once the organiser issues one."
            ),
            error_code="CERTIFICATE_NOT_ISSUED" if held.reason == "not_issued" else "CERTIFICATE_REVOKED",
        )
    return registration, await certificate_style(session, registration)


async def _certificate_render_context(session, ticket_id: str, user) -> dict:
    """The HTML template's context for a certificate, as both renders build it."""
    registration, style = await _certificate_for(session, ticket_id, user)
    context = certificate_context(registration, style)
    return {**context, "registration": registration, "qr_text": certificate_qr_text(registration)}


# Each one a render: limited per address. Generous, because a campus behind
# one NAT address is many people; with max-age=300 and the ETag an app
# showing a list asks again rarely.
CERTIFICATE_LIMIT = "120/minute"


def _visitor(request: Request) -> str:
    return client_address(request) or "unknown"


def _certificate_cache(code: str) -> str:
    """By its public id the certificate is public, and a shared cache may
    keep it for the five minutes; by ticket id it is the holder's own, and
    only their device may."""
    from app.api.certificates.service import code_public_id

    return "public, max-age=300" if code_public_id(code) else "private, max-age=300"


def _certificate_headers(registration, job, kind: str, code: str = "") -> dict:
    """Five minutes, not a day: a certificate can be revoked or redesigned,
    and a day-long public cache kept serving the old one. The ETag names the
    design version, the revocation and everything printed, so a client
    asking again gets a 304 until one of them changes."""
    seed = f"{kind}|{job.design_version}|{getattr(registration, 'certificate_revoked_at', None)}|{job.fingerprint}"
    return {
        "Cache-Control": _certificate_cache(code),
        "ETag": f'"{hashlib.sha256(seed.encode()).hexdigest()[:32]}"',
    }


def _not_modified(request: Request, headers: dict) -> Optional[Response]:
    if matches(_if_none_match(request), headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return None


@router.get("/certificates/{ticket_id}/image", summary="Get certificate as an image")
@limiter.limit(CERTIFICATE_LIMIT, key_func=_visitor)
async def get_certificate_image(
    request: Request,
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: OptionalAuth = None,
) -> Response:
    """The certificate as a PNG, for previewing it in the app.

    The app used to render the PDF in a webview, which put Apple's PDF
    viewer chrome on screen -- a page counter, a page frame, and the
    certificate shrunk inside a sheet of white. An image has no viewer, so
    it crops to the certificate itself.

    Same template, same payload and the same rendering engine as the PDF,
    so what is previewed is what is downloaded. `ticket_id` is the
    certificate's public id (anyone) or the ticket id (its holder, its club
    or an admin only).
    """
    registration, style = await _certificate_for(session, ticket_id, user)
    job = certificate_job(registration, style)
    headers = _certificate_headers(registration, job, "png", ticket_id)
    if (unchanged := _not_modified(request, headers)) is not None:
        return unchanged

    if in_layout_mode(style):
        pdf = await doc_cache.cached(job.cache_key("certificate", registration.id), job.draw)
        png = await pool.run(layout_png, style, pdf)
        return Response(content=png, media_type="image/png", headers=headers)

    context = certificate_context(registration, style)
    width = image_width(context["style"], default=1400)

    def draw() -> bytes:
        inline, _ = embedded(context["style"], fields=CERTIFICATE_FIELDS)
        return generate_image_bytes(
            template_path="events/certificate.html",
            context={**context, "style": inline},
            width=width,
        ).getvalue()

    return Response(content=await pool.run(draw), media_type="image/png", headers=headers)


@router.get("/certificates/{ticket_id}/pdf", summary="Get certificate PDF")
@limiter.limit(CERTIFICATE_LIMIT, key_func=_visitor)
async def get_certificate_pdf(
    request: Request,
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: OptionalAuth = None,
) -> Response:
    """
    The certificate of participation as a PDF, once the organiser issued
    one. `ticket_id` is the certificate's public id (anyone) or the ticket
    id (its holder, its club or an admin only).

    Tagged like the ticket (_document_etag): a 304 without drawing it when
    the caller's If-None-Match names it. A revoked one is refused first.
    """
    registration, style = await _certificate_for(session, ticket_id, user)
    job = certificate_job(registration, style)
    headers = {"Cache-Control": _certificate_cache(ticket_id), "ETag": _document_etag(job)}
    if (unchanged := _not_modified(request, headers)) is not None:
        return unchanged

    content, whole = await _drawn(job, job.cache_key("certificate", registration.id))
    if not whole:
        del headers["ETag"]
    return Response(
        content=content,
        media_type="application/pdf",
        # Named by its number: a file name travels wherever the file is posted.
        headers={**headers, "Content-Disposition": f'attachment; filename="certificate-{number_of(registration)}.pdf"'},
    )


# ─── Networking Discovery ─────────────────────────────────────────────

from app.api.events.schemas import NetworkingAttendeesResponse
from app.api.events import networking as networking_service


@router.get("/{event_id}/networking", summary="Discover like-minded attendees")
async def get_networking_attendees(
    event_id: int,
    session: SessionDep,
    user: DependsAuth,
) -> NetworkingAttendeesResponse:
    """
    Returns checked-in attendees who share interests with the requesting user.
    Only available during the event + 1 hour buffer.
    Requires the requesting user to be checked in.

    Nobody who turned "Show me in People Like You" off is listed, nor anyone
    blocked either way. Refusals carry an error_code the app words itself:
    NETWORKING_UNAVAILABLE (the plan), NETWORKING_CLOSED (the window),
    NETWORKING_NOT_CHECKED_IN, and NETWORKING_HIDDEN only when
    networking.HIDDEN_CAN_BROWSE is off.
    """
    from app.api.events.models import Events

    # Networking is part of the hosting club's plan (never refused on a
    # grandfathered event); an attendee is told only that it is not on.
    hosted = await session.get(Events, event_id)
    if hosted is not None and not await gates.allows(session, hosted.club_id, "networking", actor=user, event=hosted):
        raise CustomHTTPException(
            403, message="Networking isn't available for this event.", error_code="NETWORKING_UNAVAILABLE"
        )
    result = await networking_service.get_networking_attendees(
        session, event_id, user.id
    )
    return NetworkingAttendeesResponse(**result)


@router.get("/admin/deleted/list", summary="Deleted events (admin)")
async def list_deleted_events_admin(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
) -> PaginatedResponse[EventAdminListResponse]:
    """Under /admin/deleted so it cannot collide with another admin path."""
    events, total = await service.list_deleted_events(
        session, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(
        events, request, schema=EventAdminListResponse, total=total
    )


@router.post("/admin/{event_id}/restore", summary="Restore a deleted event (admin)")
async def restore_event_admin(
    event_id: int, session: SessionDep, user: ModerateAuth, request: Request = None
) -> EventAdminListResponse:
    event = await service.restore_event(session, event_id)
    await audit.record(
        session,
        actor=user,
        action="event.restore",
        entity_type="event",
        entity_id=event_id,
        summary=f"Restored event “{event.name}”",
        request=request,
    )
    await session.commit()
    return event
