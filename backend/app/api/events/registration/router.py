from io import BytesIO
from fastapi import APIRouter, BackgroundTasks, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
import pandas as pd
from sqlalchemy import select

from app.api.events.registration.schemas import (
    AddAttendeeRequest,
    ImportLogSummary,
    RegistrationUpdateRequest,
    EventRegistrationDetailResponse,
    EventRegistrationPublicMin,
    EventRegistrationRequest,
    EventRegistrationResponse,
    EventAnalyticsResponse,
    SetRoleRequest,
    SetWinnerRequest,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth, OptionalUserAuth
from app.db.core import SessionDep
from app.api.users.models import UserTypes
from app.api.users import service as user_service
from app.response import CustomHTTPException
from app.api.auth import service as auth_service
from app.api.events.registration import service
from app.api.events.access import is_admin_user
from app.api.templates_audience import RegistrationRole
from app.api.events.models import Events
from app.api.schemas import BackgroundTaskLogResponseSchema
from app.api.service import create_background_task_log
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.core.utils.excel import read_excel

router = APIRouter(prefix="/registration")


@router.post("/{event_id}/register", summary="Register for an event")
async def register_event(
    background_tasks: BackgroundTasks,
    registration: EventRegistrationRequest,
    event_id: int | str,
    user: OptionalUserAuth,
    session: SessionDep = SessionDep,
) -> EventRegistrationResponse:
    response = {}

    if not user:
        if not (
            user := await user_service.get_non_club_user_by_email(
                session, registration.email
            )
        ):
            user = await user_service.create_user(
                session=session,
                full_name=registration.full_name,
                email=registration.email,
                phone=registration.phone,
                provider="email",
                user_type=UserTypes.guest,
            )

        if not user:
            raise CustomHTTPException(400, "User not found")
        if user.user_type != UserTypes.guest:
            provider_hint = ""
            if hasattr(user, 'provider') and user.provider:
                provider_hint = f" (signed up via {user.provider})"
            raise CustomHTTPException(
                400,
                f"An account with this email already exists{provider_hint}. "
                "Please login through the app to register for this event."
            )

        token = await auth_service.create_access_refresh_tokens(user)
        response["auth_token"] = token
    if user.user_type != UserTypes.guest:
        if registration.email != user.email:
            raise CustomHTTPException(
                400, "Email does not match with the logged in user email"
            )
        registration.email = user.email
    # Merge organization into additional_details if provided
    additional_details = registration.additional_details or {}
    if registration.organization:
        additional_details["organization"] = registration.organization

    register_data = await service.register_event(
        session=session,
        background_tasks=background_tasks,
        full_name=registration.full_name,
        email=registration.email,
        phone=registration.phone,
        user_id=user.id,
        event_id=event_id,
        additional_details=additional_details if additional_details else None,
    )
    response = {**response, **register_data}
    return response


@router.get("/{event_id}/list", summary="Get event registration details")
async def list_event_registration(
    request: Request,
    user: ClubAuth,
    pagination: PaginationParams,
    event_id: int,
    is_attended: bool | None = Query(None),
    is_paid: bool | None = Query(None),  # Changed to None to show all by default
    search: str | None = Query(None, description="Search by name, email, phone or ticket ID"),
    role: RegistrationRole | None = Query(None),
    delivery: str | None = Query(None, pattern="^(not_delivered)$", description="Only guests not reached on any channel"),
    session: SessionDep = SessionDep,
) -> PaginatedResponse[EventRegistrationPublicMin]:
    result, total = await service.list_event_registrations(
        session,
        user_id=user.id,
        event_id=event_id,
        is_attended=is_attended,
        is_paid=is_paid,
        limit=pagination.limit,
        offset=pagination.offset,
        search=search,
        as_admin=is_admin_user(user),
        role=role,
        delivery=delivery,
    )
    return paginated_response(result, request, EventRegistrationPublicMin, total=total)


@router.get("/{event_id}/attendees", summary="Get event attendees list (checked-in only)")
async def list_event_attendees(
    request: Request,
    user: ClubAuth,
    pagination: PaginationParams,
    event_id: int,
    search: str | None = Query(None, description="Search by name, email or ticket ID"),
    session: SessionDep = SessionDep,
) -> PaginatedResponse[EventRegistrationPublicMin]:
    """List only attendees who have checked in (is_attended=True)."""
    result, total = await service.list_event_registrations(
        session,
        user_id=user.id,
        event_id=event_id,
        is_attended=True,  # Always filter for attended only
        is_paid=None,
        limit=pagination.limit,
        offset=pagination.offset,
        search=search,
        as_admin=is_admin_user(user),
    )
    return paginated_response(result, request, EventRegistrationPublicMin, total=total)


@router.get("/{event_id}/analytics", summary="Get event analytics")
async def get_event_analytics(
    user: ClubAuth,
    event_id: int,
    session: SessionDep = SessionDep,
) -> EventAnalyticsResponse:
    return await service.get_event_analytics(
        session,
        user_id=user.id,
        event_id=event_id,
        as_admin=is_admin_user(user),
    )


@router.get("/{event_id}/export", summary="Export event registrations")
async def export_event_registrations(
    user: ClubAuth,
    event_id: int,
    is_attended: bool | None = Query(None),
    is_paid: bool = Query(True),
    session: SessionDep = SessionDep,
) -> list[dict]:
    result, _ = await service.list_event_registrations(
        session,
        user_id=user.id,
        event_id=event_id,
        is_attended=is_attended,
        is_paid=is_paid,
        limit=None,
        offset=None,
        as_admin=is_admin_user(user),
    )

    # list_event_registrations returns plain dicts, not ORM rows, so every
    # field here is looked up by key. Attribute access raised
    # AttributeError: 'dict' object has no attribute 'ticket_id' and made this
    # endpoint return 500 for every export.
    data = [
        {
            "Ticket ID": row.get("ticket_id"),
            "Full Name": row.get("full_name"),
            "Email": row.get("email"),
            "Phone": row.get("phone"),
            # Answers to the event's custom registration questions, spread into
            # their own columns.
            **(
                row["additional_details"]
                if isinstance(row.get("additional_details"), dict)
                else {}
            ),
            "Is Paid": row.get("is_paid"),
            "Actual Amount": row.get("actual_amount"),
            "Paid Amount": row.get("paid_amount"),
            "Is Attended": row.get("is_attended"),
            "Checked In By": (row.get("volunteer") or {}).get("email"),
            "Registered At": (
                row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                if row.get("created_at")
                else None
            ),
        }
        for row in result
    ]

    df = pd.DataFrame(data)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Event Registrations")

    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="event_{event_id}_registrations.xlsx"'
        },
    )


@router.get(
    "/{event_id}/info/{registration_id}",
    summary="Get event registration details",
)
async def get_event_registration(
    request: Request,
    user: ClubAuth,
    event_id: int,
    registration_id: str,
    session: SessionDep = SessionDep,
) -> EventRegistrationDetailResponse:
    result = await service.get_registration(
        session,
        user_id=user.id,
        event_id=event_id,
        registration_id=registration_id,
        as_admin=is_admin_user(user),
    )
    return jsonable_encoder(await _with_deliveries(session, result))


async def _with_deliveries(session, registration) -> dict:
    """The guest in full: the record, and what was sent to them."""
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import DeliveryKind

    out = dict(jsonable_encoder(registration))
    for kind, key in ((DeliveryKind.ticket, "ticket_deliveries"), (DeliveryKind.certificate, "certificate_deliveries")):
        sent = await deliveries.by_registration(session, registration.event_id, kind, [registration.id])
        out[key] = jsonable_encoder(sent.get(registration.id, []))
    return out


@router.patch("/{event_id}/info/{registration_id}", summary="Correct a registration")
async def update_event_registration(
    user: ClubAuth,
    event_id: int,
    registration_id: str,
    body: RegistrationUpdateRequest,
    session: SessionDep = SessionDep,
) -> EventRegistrationDetailResponse:
    result = await service.update_registration(
        session,
        user_id=user.id,
        event_id=event_id,
        registration_id=registration_id,
        full_name=body.full_name,
        email=body.email,
        phone=body.phone,
        role=body.role,
        additional_details=body.additional_details,
        as_admin=is_admin_user(user),
    )
    return jsonable_encoder(await _with_deliveries(session, result))


@router.delete("/{event_id}/info/{registration_id}", summary="Take someone off the list")
async def remove_event_registration(
    user: ClubAuth,
    event_id: int,
    registration_id: str,
    confirm_slug: str = Query(..., description="The event's slug, typed to confirm"),
    session: SessionDep = SessionDep,
) -> dict:
    await service.remove_registration(
        session, user_id=user.id, event_id=event_id, registration_id=registration_id,
        confirm_slug=confirm_slug, as_admin=is_admin_user(user),
    )
    return {"message": "Removed"}


@router.post("/{event_id}/attendees", summary="Add a guest by hand")
async def add_event_attendee(
    user: ClubAuth,
    event_id: int,
    body: AddAttendeeRequest,
    session: SessionDep = SessionDep,
) -> EventRegistrationDetailResponse:
    """Not the public form: nothing is emailed from here. Send tickets
    delivers theirs, and records it."""
    result = await service.add_attendee(
        session, user_id=user.id, event_id=event_id,
        full_name=body.full_name, email=body.email, phone=body.phone, role=body.role,
        additional_details=body.additional_details, as_admin=is_admin_user(user),
    )
    return jsonable_encoder(await _with_deliveries(session, result))


@router.post("/{event_id}/import", summary="Import a guest list (CSV or Excel)")
async def import_event_registrations(
    user: ClubAuth,
    event_id: int,
    background_tasks: BackgroundTasks,
    session: SessionDep = SessionDep,
    file: UploadFile = Form(...),
) -> ImportLogSummary:
    """The organiser's import: rows become registrations, nobody is emailed
    from here, and the run is recorded against the event so the panel can
    show what happened to every row."""
    from app.api.events.access import ensure_event_access
    from sqlalchemy.orm import joinedload

    event = await session.scalar(select(Events).filter(Events.id == event_id).options(joinedload(Events.club)))
    if not event:
        raise CustomHTTPException(404, "Event not found")
    ensure_event_access(event, user.id, is_admin_user(user), message="Not authorized to manage this event")
    df = await read_excel(file)
    missing = [c for c in ("full_name", "email") if c not in df.columns]
    if missing:
        raise CustomHTTPException(422, message=f"The file needs a column for {', '.join(missing)}.", errors={"fields": {c: "missing column" for c in missing}})
    background_log = await create_background_task_log(
        session, f"Import {file.filename or 'guest list'} into '{event.name}'", "event_registrations_bulk_import",
    )
    background_log.event_id = event_id
    await session.commit()
    background_tasks.add_task(
        service.bulk_import_event_registrations, session, event_id, df, background_log, False
    )
    return service.summarise_import(background_log)


@router.get("/{event_id}/imports", summary="Every import into this event, newest first")
async def list_event_imports(
    user: ClubAuth,
    event_id: int,
    session: SessionDep = SessionDep,
) -> list[ImportLogSummary]:
    return await service.list_imports(session, user_id=user.id, event_id=event_id, as_admin=is_admin_user(user))


@router.post("/{event_id}/bulk-import", summary="Bulk import user registrations")
async def bulk_import_event_registrations(
    user: AdminAuth,
    event_id: int,
    background_tasks: BackgroundTasks,
    session: SessionDep = SessionDep,
    file: UploadFile = Form(...),
) -> BackgroundTaskLogResponseSchema:
    df = await read_excel(file)
    event = await session.scalar(select(Events).filter(Events.id == event_id))
    if not event:
        raise CustomHTTPException(404, "Event not found")
    background_log = await create_background_task_log(
        session,
        f"Bulk Import Event Registrations for '{event.name}'",
        "event_registrations_bulk_import",
    )
    background_tasks.add_task(
        service.bulk_import_event_registrations, session, event_id, df, background_log
    )
    return jsonable_encoder(background_log)


# @router.get("/{event_id}/attendance", summary="Get event attendance details")
# async def get_event_attendance(
#     request: Request,
#     user: ClubAuth,
#     event_id: int,
#     session: SessionDep = SessionDep,
# ) -> PaginatedResponse[EventRegistrationPublicMin]:
#     result = await service.get_event_attendance(
#         session,
#         user_id=user.id,
#         event_id=event_id,
#     )
#     return paginated_response(result, request, EventRegistrationPublicMin)


from app.api.events.registration.schemas import EventAttendanceUpdate

@router.post("/{event_id}/attendance/{registration_id}", summary="Mark attendance")
async def mark_attendance(
    user: ClubAuth,
    event_id: int,
    registration_id: str,
    attendance: EventAttendanceUpdate,
    session: SessionDep = SessionDep,
) -> EventRegistrationDetailResponse:
    result = await service.mark_attendance(
        session,
        user_id=user.id,
        event_id=event_id,
        registration_id=registration_id,
        is_attended=attendance.is_attended,
        as_admin=is_admin_user(user),
    )
    return jsonable_encoder(result)


# ---------------------------------------------------------------------------
# Winners.
#
# `is_won` and `position` have been columns on every registration since the
# table was written, and the badge checker reads `position` for events_won,
# runner_up_count and podium_count -- but nothing in the API ever wrote
# either, so those three badges could not be earned by anyone.
# ---------------------------------------------------------------------------


@router.get("/{event_id}/winners", summary="List an event's winners")
async def list_event_winners(
    event_id: int,
    user: ClubAuth,
    session: SessionDep,
) -> list[EventRegistrationPublicMin]:
    winners = await service.list_winners(
        session,
        user_id=user.id,
        event_id=event_id,
        as_admin=is_admin_user(user),
    )
    return [
        EventRegistrationPublicMin(
            id=w.id,
            ticket_id=w.ticket_id or "",
            event_id=w.event_id,
            is_paid=w.is_paid,
            full_name=w.full_name or "",
            email=w.email or "",
            phone=w.phone,
            actual_amount=float(w.actual_amount or 0),
            paid_amount=float(w.paid_amount or 0),
            is_attended=w.is_attended,
            is_won=w.is_won,
            position=w.position,
            institution=w.institution,
            name=w.full_name or "",
            created_at=w.created_at,
        )
        for w in winners
    ]


@router.put(
    "/{event_id}/winners/{registration_id}",
    summary="Set or clear a registration's placing",
)
async def set_event_winner(
    event_id: int,
    registration_id: str,
    body: SetWinnerRequest,
    user: ClubAuth,
    session: SessionDep,
) -> EventRegistrationDetailResponse:
    """
    `position: 1` marks first place, `position: null` clears the placing.

    Placings need not be unique within an event -- joint winners are real and
    rejecting them would be a surprise with no upside.
    """
    result = await service.set_winner(
        session,
        user_id=user.id,
        event_id=event_id,
        registration_id=registration_id,
        position=body.position,
        as_admin=is_admin_user(user),
    )
    return jsonable_encoder(result)


# ---------------------------------------------------------------------------
# Roles.
#
# An event can hold a separate ticket or certificate design for speakers and
# staff, but the renderer chooses one by the registration's role -- so until
# something could write it, every variant an organiser designed would sit
# unused while everyone received the event default.
# ---------------------------------------------------------------------------


@router.put(
    "/{event_id}/role/{registration_id}",
    summary="Set or clear which role a registration is attending as",
)
async def set_registration_role(
    event_id: int,
    registration_id: str,
    body: SetRoleRequest,
    user: ClubAuth,
    session: SessionDep,
) -> EventRegistrationDetailResponse:
    """
    `role: "speaker"` marks a speaker, `role: null` resets them to attendee.

    Every registration is an attendee until an organiser says otherwise, so
    there is no "no role" to return to. An attendee receives the event's
    default design anyway unless the club has drawn an attendee variant.
    """
    # CustomBaseModel sets use_enum_values, so an enum-annotated field
    # arrives as a plain string -- the service wants the member.
    role = RegistrationRole(body.role) if body.role else None

    result = await service.set_role(
        session,
        user_id=user.id,
        event_id=event_id,
        registration_id=registration_id,
        role=role,
        as_admin=is_admin_user(user),
    )
    return jsonable_encoder(result)
