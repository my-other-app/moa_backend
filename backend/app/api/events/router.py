from fastapi import (
    APIRouter,
    Depends,
    Request,
    Query,
    Response,
    File,
    UploadFile,
)
from datetime import timedelta
import base64
import io
import qrcode
from app.core.utils.pdf import generate_image_bytes, generate_pdf_bytes
from app.api.templates_shared import render_layers
from fastapi.encoders import jsonable_encoder
from typing import Optional, List

from app.db.core import SessionDep
from app.api.events import service
from app.api.audit import service as audit
from app.core.response.sorting import apply_sort
from app.api.events.access import is_admin_user
from app.api.events import wallet_service
from app.api.users.models import UserTypes
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
    EventRatingDetailResponse,
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

router = APIRouter(prefix="/events")

router.include_router(volunteer_router, tags=["Volunteer"])
router.include_router(registration_router, tags=["Registration"])


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
    session: SessionDep = SessionDep,
    event: EventCreate = Depends(),
    speaker_photos: List[UploadFile] = File([]),
) -> EventCreateUpdateResponse:
    return await service.create_event(
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
        has_prize=event.has_prize,
        prize_amount=event.prize_amount,
        is_online=event.is_online,
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
    )


@router.put("/update/{event_id}", summary="Update an event")
async def update_event(
    event_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
    event: EventEdit = Depends(),
    speaker_photos: List[UploadFile] = File([]),
) -> EventCreateUpdateResponse:
    return await service.update_event(
        session,
        event,
        user.id,
        event_id=event_id,
        speaker_photos=speaker_photos,
        as_admin=is_admin_user(user),
    )


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

    query = (
        select(Events)
        .options(
            selectinload(Events.category),
            selectinload(Events.club),
            selectinload(Events.interests),
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
    interest_ids = [int(i) for i in interest_ids.split(",")] if interest_ids else []
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
                event_dict["ticket_id"] = reg.ticket_id
                event_dict["ticket_url"] = f"https://events.myotherapp.in/tickets/{reg.ticket_id}"
                if reg.is_attended:
                    event_dict["certificate_url"] = f"https://events.myotherapp.in/certificates/{reg.ticket_id}"
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
    debug_scores: bool = Query(False, description="Include each event's score"),
) -> PaginatedResponse[EventListResponse]:
    """Upcoming events ranked for this user.

    Replaces calling /events/list with no filters, which returned every event
    ever created oldest-first -- so the feed opened on an event from years ago.
    """
    events, scores = await recommendations.for_you_events(
        session,
        user_id=user.id,
        limit=pagination.limit,
        offset=pagination.offset,
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
    return jsonable_encoder(result)


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



@router.get("/tickets/{ticket_id}/pdf", summary="Get ticket PDF")
async def get_ticket_pdf(
    ticket_id: str,
    session: SessionDep = SessionDep,
    user: DependsAuth = None,
) -> Response:
    """
    Generate and download a PDF for the ticket.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)
    _assert_can_access_ticket(user, registration)

    # Load ticket template config for this event
    from app.api.ticket_templates.service import get_template_for_event
    style = await get_template_for_event(session, registration.event_id)

    # Construct payload for template
    event = registration.event
    event_endtime = (
        event.event_datetime + timedelta(hours=event.duration)
        if event.duration
        else None
    )
    
    email_payload = {
        "ticket_id": registration.ticket_id,
        "participant_name": registration.full_name,
        "event_name": event.name,
        "event_date": event.event_datetime.strftime("%d %b %Y"),
        "event_time": (
            event.event_datetime.strftime("%I:%M %p")
            + (" - " + event_endtime.strftime("%I:%M %p"))
            if event_endtime
            else ""
        ),
        "event_location": event.location_name,
        "event_prizes": f"Prizes worth ₹{event.prize_amount}" if event.prize_amount else None,
        "organizer_name": event.club.name,
        "contact_email": event.contact_email,
        "contact_phone": event.contact_phone,
    }

    # Generate QR Code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(registration.ticket_id)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert QR code to base64
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    email_payload["qr_code"] = qr_base64

    # Layer mode substitutes its tokens here: Jinja will not re-render the
    # contents of a variable, so `{{name}}` inside a layer would print as
    # literal braces. No-op when the template defines no layers.
    if isinstance(style, dict) and style.get("layers"):
        style = {
            **style,
            "layers": render_layers(
                style["layers"],
                {
                    "name": email_payload.get("name"),
                    "ticket_code": email_payload.get("ticket_id")
                    or email_payload.get("ticket_code"),
                    "event_name": email_payload.get("event_name"),
                    "event_title": email_payload.get("event_name"),
                    "event_date": email_payload.get("event_date"),
                    "event_time": email_payload.get("event_time"),
                    "venue": email_payload.get("venue"),
                    "club_name": email_payload.get("club_name"),
                },
            ),
        }

    pdf_bytes = generate_pdf_bytes(
        template_path="events/ticket.html",
        context={
            "email_payload": email_payload,
            "style": style,
            "qr_code_url": f"data:image/png;base64,{qr_base64}",
        },
    )

    return Response(
        content=pdf_bytes.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="ticket-{ticket_id}.pdf"'
        }
    )


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
    style = await get_template_for_event(session, registration.event_id)
    return style


async def _certificate_render_context(session, ticket_id: str) -> dict:
    """The template context for a certificate, shared by the PDF and the PNG.

    Both renders go through this so the preview the app shows and the file
    the download produces cannot drift apart -- they are the same template
    with the same payload, rendered by the same engine.
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)

    if not registration.is_attended:
        raise CustomHTTPException(
            403, message="Certificate is only available for attendees who checked in"
        )

    from app.api.certificate_templates.service import get_template_for_event

    style = await get_template_for_event(session, registration.event_id)
    event = registration.event

    email_payload = {
        "ticket_id": registration.ticket_id,
        "participant_name": registration.full_name,
        "event_name": event.name,
        "event_date": event.event_datetime.strftime("%d %b %Y"),
        "organizer_name": event.club.name if event.club else "",
    }

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(f"certificate:{registration.ticket_id}")
    qr.make(fit=True)
    buffered = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buffered, format="PNG")
    email_payload["qr_code"] = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return {"email_payload": email_payload, "style": style}


@router.get("/certificates/{ticket_id}/image", summary="Get certificate as an image")
async def get_certificate_image(
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
    context = await _certificate_render_context(session, ticket_id)

    image_bytes = generate_image_bytes(
        template_path="events/certificate.html",
        context=context,
    )

    return Response(
        content=image_bytes.getvalue(),
        media_type="image/png",
        headers={
            # Certificates do not change once issued, so this is worth
            # caching hard: the app shows it in a list and a detail view.
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get("/certificates/{ticket_id}/pdf", summary="Get certificate PDF")
async def get_certificate_pdf(
    ticket_id: str,
    session: SessionDep = SessionDep,
) -> Response:
    """
    Generate and download a PDF certificate of participation.
    Only available for attendees who have checked in (is_attended=True).
    """
    context = await _certificate_render_context(session, ticket_id)

    pdf_bytes = generate_pdf_bytes(
        template_path="events/certificate.html",
        context=context,
        options={"orientation": "Landscape"},
    )

    return Response(
        content=pdf_bytes.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="certificate-{ticket_id}.pdf"'
        }
    )


@router.get("/wallet/debug", summary="Debug wallet certificate status")
async def debug_wallet_certs() -> dict:
    """Debug endpoint to check wallet certificate configuration."""
    return {
        "certs_dir": str(wallet_service.CERTS_DIR),
        "certs_dir_exists": wallet_service.CERTS_DIR.exists(),
        "pass_certificate_exists": wallet_service.CERT_PATH.exists(),
        "pass_key_exists": wallet_service.KEY_PATH.exists(),
        "wwdr_exists": wallet_service.WWDR_PATH.exists(),
        "pass_type_id": wallet_service.PASS_TYPE_ID,
        "team_id": wallet_service.TEAM_ID or "(not set)",
        "org_name": wallet_service.ORG_NAME,
    }


# ─── Networking Discovery ─────────────────────────────────────────────

from app.api.events.schemas import (
    NetworkingAttendeesResponse,
    NetworkingAttendeeResponse,
    NetworkingInterestItem,
)
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
    """
    result = await networking_service.get_networking_attendees(
        session, event_id, user.id
    )
    return NetworkingAttendeesResponse(
        event_id=result["event_id"],
        event_name=result["event_name"],
        window_closes_at=result["window_closes_at"],
        attendees=[
            NetworkingAttendeeResponse(
                user_id=a["user_id"],
                name=a["name"],
                profile_pic_url=a["profile_pic_url"],
                organization=a["organization"],
                shared_interests=[
                    NetworkingInterestItem(**i) for i in a["shared_interests"]
                ],
                match_count=a["match_count"],
            )
            for a in result["attendees"]
        ],
        total=result["total"],
    )


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
