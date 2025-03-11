from io import BytesIO
from fastapi import APIRouter, BackgroundTasks, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
import pandas as pd
from sqlalchemy import select

from app.api.events.registration.schemas import (
    EventRegistrationDetailResponse,
    EventRegistrationPublicMin,
    EventRegistrationRequest,
    EventRegistrationResponse,
)
from app.core.auth.dependencies import AdminAuth, ClubAuth, OptionalUserAuth
from app.db.core import SessionDep
from app.api.users.models import UserTypes
from app.api.users import service as user_service
from app.response import CustomHTTPException
from app.api.auth import service as auth_service
from app.api.events.registration import service
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
            raise CustomHTTPException(
                400, "This email is already registered, please login to continue"
            )

        token = await auth_service.create_access_refresh_tokens(user)
        response["auth_token"] = token
    register_data = await service.register_event(
        session=session,
        background_tasks=background_tasks,
        full_name=registration.full_name,
        email=registration.email,
        phone=registration.phone,
        user_id=user.id,
        event_id=event_id,
        additional_details=registration.additional_details,
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
    is_paid: bool = Query(True),
    session: SessionDep = SessionDep,
) -> PaginatedResponse[EventRegistrationPublicMin]:
    result = await service.list_event_registrations(
        session,
        user_id=user.id,
        event_id=event_id,
        is_attended=is_attended,
        is_paid=is_paid,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return paginated_response(result, request, EventRegistrationPublicMin)


@router.get("/{event_id}/export", summary="Export event registrations")
async def export_event_registrations(
    user: ClubAuth,
    event_id: int,
    is_attended: bool | None = Query(None),
    is_paid: bool = Query(True),
    session: SessionDep = SessionDep,
) -> list[dict]:
    result = await service.list_event_registrations(
        session,
        user_id=user.id,
        event_id=event_id,
        is_attended=is_attended,
        is_paid=is_paid,
        limit=None,
        offset=None,
    )

    data = [
        {
            "Ticket ID": row.ticket_id,
            "Full Name": row.full_name,
            "Email": row.email,
            "Phone": row.phone,
            **(
                row.additional_details
                if isinstance(row.additional_details, dict)
                else {}
            ),
            "Is Paid": row.is_paid,
            "Actual Amount": row.actual_amount,
            "Paid Amount": row.paid_amount,
            "Is Attended": row.is_attended,
            "Checked In By": row.volunteer.email if row.volunteer else None,
            "Registered At": row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
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
        session, user_id=user.id, event_id=event_id, registration_id=registration_id
    )
    return jsonable_encoder(result)


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


@router.get("/{event_id}/attendance", summary="Get event attendance details")
async def get_event_attendance(
    request: Request,
    user: ClubAuth,
    event_id: int,
    session: SessionDep = SessionDep,
) -> PaginatedResponse[EventRegistrationPublicMin]:
    result = await service.get_event_attendance(
        session,
        user_id=user.id,
        event_id=event_id,
    )
    return paginated_response(result, request, EventRegistrationPublicMin)
