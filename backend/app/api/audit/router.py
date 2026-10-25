"""Reading the audit log, and the admin-wide notification log."""

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from pydantic import Field

from app.api.audit import service
from app.core.auth.dependencies import AdminAuth
from app.core.response.base_model import CustomBaseModel
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.db.core import SessionDep

router = APIRouter(prefix="/admin", tags=["admin"])


class AuditActorResponse(CustomBaseModel):
    id: int | None = Field(None)
    full_name: str | None = Field(None)
    email: str | None = Field(None)
    # Their picture (reports.service.avatar): a club account's logo, else the
    # photo they uploaded, else the avatar they picked; null once the
    # account is gone. An image's links by size ({"thumbnail", ...}).
    avatar: dict | None = Field(None)


class AuditLogResponse(CustomBaseModel):
    id: int = Field(...)
    action: str = Field(...)
    entity_type: str | None = Field(None)
    entity_id: str | None = Field(None)
    summary: str | None = Field(None)
    details: dict | None = Field(None)
    ip_address: str | None = Field(None)
    created_at: datetime = Field(...)
    actor: AuditActorResponse | None = Field(None)


class AuditLogPage(PaginatedResponse[AuditLogResponse]):
    # The search applied, as the API read it (its words, one space apart),
    # or null for none. Always sent: a dashboard tells from it that this API
    # searches, and hides its search box from one that does not.
    search: Optional[str] = Field(None)


@router.get("/audit-log", summary="Admin action history")
async def list_audit_log(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    actor_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, max_length=200, description="Words to find in who acted, the summary, the record or the reason"),
) -> AuditLogPage:
    """
    Who did what, newest first.

    `entity_type` and `entity_id` together give the history of one record,
    which is the question asked after something goes wrong. `q` finds rows
    with every one of its words, in any case, in the actor's name or email,
    the summary, the record ("club #12") or the reason given; paged and
    counted like the rest.
    """
    from app.api.reports.service import avatar

    words = service.search_words(q)
    rows, total = await service.list_entries(
        session,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        q=q,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    items = [
        AuditLogResponse(
            id=row.id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            summary=row.summary,
            details=row.details,
            ip_address=row.ip_address,
            created_at=row.created_at,
            actor=AuditActorResponse(
                id=row.actor_id,
                # The stored name and email survive the account being
                # deleted; the relationship does not.
                full_name=row.actor.full_name if row.actor else row.actor_name,
                email=row.actor_email,
                avatar=avatar(row.actor),
            ),
        )
        for row in rows
    ]
    page = paginated_response(items, request, schema=AuditLogResponse, total=total)
    return AuditLogPage(
        limit=page.limit, offset=page.offset, total=page.total, next=page.next, items=page.items,
        search=" ".join(words) or None,
    )


@router.get("/audit-log/actions", summary="Distinct audit actions")
async def list_audit_actions(session: SessionDep, user: AdminAuth) -> list[str]:
    """Powers the filter without hard-coding a list that drifts from reality."""
    return await service.distinct_actions(session)


class AdminNotificationResponse(CustomBaseModel):
    title: str | None = Field(None)
    description: str | None = Field(None)
    event_id: int | None = Field(None)
    event_name: str | None = Field(None)
    club_name: str | None = Field(None)
    sent_count: int = Field(0)
    read_count: int = Field(0)
    created_at: datetime | None = Field(None)


@router.get("/notifications", summary="Every announcement sent, across all clubs")
async def list_all_notifications(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    user: AdminAuth,
    club_id: Optional[int] = Query(None),
) -> PaginatedResponse[AdminNotificationResponse]:
    """
    Announcements could be sent and never reviewed: the only history endpoint
    is scoped to the calling club, so nobody could see what the platform had
    pushed to people's phones.
    """
    from app.api.audit import notifications

    items, total = await notifications.list_sent_announcements(
        session, club_id=club_id, limit=pagination.limit, offset=pagination.offset
    )
    return paginated_response(
        [AdminNotificationResponse(**item) for item in items],
        request,
        schema=AdminNotificationResponse,
        total=total,
    )
