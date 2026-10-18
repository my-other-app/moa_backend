from typing import Optional

from fastapi import APIRouter, Query, Request, Response

from app.api.reports import service
from app.api.reports.schemas import (
    AdminReportsPage,
    BlockCreate,
    BlockOut,
    ReportCreate,
    ReportOut,
    ResolveIn,
    ResolveOut,
    TargetType,
)
from app.core.auth.dependencies import AdminAuth, UserAuth
from app.core.auth.permissions import ModerateAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/reports", tags=["reports"])
blocks_router = APIRouter(prefix="/user/blocks", tags=["user"])
admin_router = APIRouter(prefix="/admin/reports", tags=["admin"])


@router.post("", summary="Report a thread, a comment, a user or a club", status_code=201)
async def create_report(body: ReportCreate, response: Response, session: SessionDep, user: UserAuth) -> ReportOut:
    """201 with the new report; 200 with the one already open when this
    person has reported the same thing before. 20 an hour per person
    (429 RATE_LIMITED, errors.retry_after in seconds)."""
    row, created = await service.report(
        session, user, target_type=body.target_type, target_id=body.target_id, reason=body.reason, note=body.note
    )
    if not created:
        response.status_code = 200
    return ReportOut(id=row.id, status=row.status)


@blocks_router.get("", summary="The people you have blocked")
async def list_blocks(session: SessionDep, user: UserAuth) -> list[BlockOut]:
    return [BlockOut(**b) for b in await service.list_blocks(session, user)]


@blocks_router.post("", summary="Block someone: their comments no longer show to you", status_code=201)
async def block(body: BlockCreate, session: SessionDep, user: UserAuth) -> BlockOut:
    return BlockOut(**await service.block(session, user, body.user_id))


@blocks_router.delete("/{user_id}", summary="Unblock someone", status_code=204)
async def unblock(user_id: int, session: SessionDep, user: UserAuth) -> Response:
    await service.unblock(session, user, user_id)
    return Response(status_code=204)


@admin_router.get("", summary="Reports, newest first (admin)")
async def list_reports(
    session: SessionDep,
    admin: AdminAuth,
    status: Optional[str] = Query(None, pattern="^(open|resolved)$"),
    target_type: Optional[TargetType] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> AdminReportsPage:
    return AdminReportsPage(**await service.list_reports(session, status=status, target_type=target_type, offset=offset, limit=limit))


@admin_router.post("/{report_id}/resolve", summary="Decide a report; every open report on the same thing goes with it (admin)")
async def resolve_report(report_id: int, body: ResolveIn, request: Request, session: SessionDep, admin: ModerateAuth) -> ResolveOut:
    return ResolveOut(**await service.resolve(session, admin, report_id, action=body.action, note=body.note, request=request))
