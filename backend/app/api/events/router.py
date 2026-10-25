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
from app.api.events.tickets import role_of, ticket_job, ticket_payload, ticket_style
from app.api.events.certificates import (
    availability,
    certificate_context,
    certificate_job,
    certificate_style,
    number_of,
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


def _assert_can_access_ticket(user, registration) -> None:
    """Allow the ticket's own user, the event's owning club, or an admin."""
    if user.user_type == UserTypes.admin:
        return
    if registration.user_id == user.id:
        return
    if user.club and registration.event and registration.event.club_id == user.club.id:
        return
    raise CustomHTTPException(403, "You do not have permission to access this ticket.")


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
    )
    # Its first ticket type, at the event's own price and cap (app/api/ticketing).
    from app.api.ticketing import types as ticket_types

    await ticket_types.follow_event_edit(session, created)
    _follow_map_link(background_tasks, created)
    return created


@router.put("/update/{event_id}", summary="Update an event")
async def update_event(
    event_id: int,
    user: ClubAuth,
    background_tasks: BackgroundTasks,
    session: SessionDep = SessionDep,
    event: EventEdit = Depends(),
    speaker_photos: List[UploadFile] = File([]),
) -> EventCreateUpdateResponse:
    updated = await service.update_event(
        session,
        event,
        user.id,
        event_id=event_id,
        speaker_photos=speaker_photos,
        as_admin=is_admin_user(user),
        actor=user,
    )
    # A single ticket type shows the event's price and cap again; with
    # several, the event's fee is what they charge (app/api/ticketing).
    from app.api.ticketing import types as ticket_types

    await ticket_types.follow_event_edit(session, updated)
    _follow_map_link(background_tasks, updated)
    return updated


@router.get("/info/{event_id}", summary="Get event info")
async def get_event(
    request: Request,
    event_id: str,
    session: SessionDep = SessionDep,
    user: OptionalAuth = None,
) -> EventDetailResponse:
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
        for item in rows.items:
            registered, present = by_event.get(item.id, (0, 0))
            item.registration_count = registered
            item.attended_count = present

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
                # issued without a check-in counts, a revoked one does not.
                held = availability(reg)
                event_dict["has_certificate"] = held.available
                event_dict["certificate_revoked"] = held.reason == "revoked"
                if held.available:
                    event_dict["certificate_url"] = public_urls.certificate_url(reg.ticket_id)
            enhanced_events.append(event_dict)
    else:
        enhanced_events = [jsonable_encoder(e) for e in events]
    
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
    _assert_can_access_ticket(user, result)
    data = jsonable_encoder(result)
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
    "-png"): a 304 without drawing when If-None-Match names it.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    _assert_can_access_ticket(user, registration)

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
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    _assert_can_access_ticket(user, registration)

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
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    _assert_can_access_ticket(user, registration)

    pkpass_bytes = await wallet_service.generate_wallet_pass(session, ticket_id)
    
    return Response(
        content=pkpass_bytes,
        media_type="application/vnd.apple.pkpass",
        headers={
            "Content-Disposition": f'attachment; filename="ticket-{ticket_id}.pkpass"'
        }
    )


@router.get("/certificates/{ticket_id}/style", summary="Get certificate template style")
async def get_certificate_style(
    ticket_id: str,
    session: SessionDep = SessionDep,
) -> dict:
    """
    Return the certificate template style config for a ticket's event.
    Public endpoint so the event website can render a styled preview.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    from app.api.certificate_templates.service import get_template_for_event
    style = await get_template_for_event(
        session, registration.event_id, role=_role_of(registration)
    )
    return style


async def _certificate_for(session, ticket_id: str):
    """The registration and its certificate design, once it holds one.

    The PNG and the PDF both come through here, so the preview the app shows
    and the file the download produces are the same document: the same
    design, the same values, the same engine.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)

    held = availability(registration)
    if not held.available:
        raise CustomHTTPException(
            403,
            message=(
                "This certificate was revoked."
                if held.reason == "revoked"
                else "Certificate is only available for attendees who checked in"
            ),
        )
    return registration, await certificate_style(session, registration)


async def _certificate_render_context(session, ticket_id: str) -> dict:
    """The HTML template's context for a certificate, as both renders build it."""
    registration, style = await _certificate_for(session, ticket_id)
    context = certificate_context(registration, style)
    return {**context, "registration": registration, "qr_text": certificate_qr_text(registration)}


# Public and unauthenticated, and each one a render: limited per address.
# Generous, because a campus behind one NAT address is many people; with
# max-age=300 and the ETag an app showing a list asks again rarely.
CERTIFICATE_LIMIT = "120/minute"


def _visitor(request: Request) -> str:
    return client_address(request) or "unknown"


def _certificate_headers(registration, job, kind: str) -> dict:
    """Five minutes, not a day: a certificate can be revoked or redesigned,
    and a day-long public cache kept serving the old one. The ETag names the
    design version, the revocation and everything printed, so a client
    asking again gets a 304 until one of them changes."""
    seed = f"{kind}|{job.design_version}|{getattr(registration, 'certificate_revoked_at', None)}|{job.fingerprint}"
    return {
        "Cache-Control": "public, max-age=300",
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
) -> Response:
    """The certificate as a PNG, for previewing it in the app.

    The app used to render the PDF in a webview, which put Apple's PDF
    viewer chrome on screen -- a page counter, a page frame, and the
    certificate shrunk inside a sheet of white. An image has no viewer, so
    it crops to the certificate itself.

    Same template, same payload and the same rendering engine as the PDF,
    so what is previewed is what is downloaded.
    """
    registration, style = await _certificate_for(session, ticket_id)
    job = certificate_job(registration, style)
    headers = _certificate_headers(registration, job, "png")
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
) -> Response:
    """
    Generate and download a PDF certificate of participation.
    Only available for attendees who have checked in (is_attended=True).

    Tagged like the ticket (_document_etag): a 304 without drawing it when
    the caller's If-None-Match names it. A revoked one is refused first.
    """
    registration, style = await _certificate_for(session, ticket_id)
    job = certificate_job(registration, style)
    headers = {"Cache-Control": "public, max-age=300", "ETag": _document_etag(job)}
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
