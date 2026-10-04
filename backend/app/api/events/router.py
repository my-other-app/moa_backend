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
from app.core.utils.pdf import generate_pdf_bytes
from fastapi.encoders import jsonable_encoder
from typing import Optional, List

from app.db.core import SessionDep
from app.api.events import service
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
from app.api.events.volunteer.router import router as volunteer_router
from app.api.events.registration.router import router as registration_router

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
    user: AdminAuth, category: EventCategoryCreate, session: SessionDep = SessionDep
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
) -> PaginatedResponse[EventAdminListResponse]:
    """List all events for an admin without filters, but supports search."""
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

    total_result = await session.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(Events.created_at.desc()).offset(pagination.offset).limit(pagination.limit)
    result = await session.execute(query)
    events = result.scalars().all()

    return paginated_response(events, request, schema=EventAdminListResponse, total=total)


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



@router.delete("/delete/{event_id}", summary="Delete an event")
async def delete_event(
    event_id: int,
    user: ClubAuth,
    session: SessionDep = SessionDep,
):
    await service.delete_event(session, event_id, user.id, as_admin=is_admin_user(user))
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

    pdf_bytes = generate_pdf_bytes(
        template_path="events/ticket.html",
        context={"email_payload": email_payload, "style": style},
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


@router.get("/certificates/{ticket_id}/pdf", summary="Get certificate PDF")
async def get_certificate_pdf(
    ticket_id: str,
    session: SessionDep = SessionDep,
) -> Response:
    """
    Generate and download a PDF certificate of participation.
    Only available for attendees who have checked in (is_attended=True).
    """
    registration = await service.get_ticket_details(session, ticket_id=ticket_id)

    if not registration.is_attended:
        raise CustomHTTPException(403, message="Certificate is only available for attendees who checked in")

    # Load certificate template config for this event
    from app.api.certificate_templates.service import get_template_for_event
    style = await get_template_for_event(session, registration.event_id)

    # Construct payload for template
    event = registration.event

    email_payload = {
        "ticket_id": registration.ticket_id,
        "participant_name": registration.full_name,
        "event_name": event.name,
        "event_date": event.event_datetime.strftime("%d %b %Y"),
        "organizer_name": event.club.name,
    }

    # Generate QR Code for verification
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(f"certificate:{registration.ticket_id}")
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    email_payload["qr_code"] = qr_base64

    pdf_bytes = generate_pdf_bytes(
        template_path="events/certificate.html",
        context={"email_payload": email_payload, "style": style},
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
