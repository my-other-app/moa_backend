"""The admin app's alerts (the bell): read by any admin, marked read by one
who can moderate (the same line other admin writes draw).

Served at /admin/alerts, not /admin/notifications: that path already lists
the announcements clubs have sent (audit router), and the admin app's audit
screen reads it.
"""

from typing import Optional

from fastapi import APIRouter, Query

from app.api.admin_alerts import service
from app.api.admin_alerts.schemas import AlertOut, AlertsPage, ReadAllOut, UnreadCount
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ModerateAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/alerts", tags=["admin-alerts"])


@router.get("", summary="Admin alerts, newest first")
async def list_alerts(
    session: SessionDep,
    user: AdminAuth,
    unread: Optional[bool] = Query(None, description="true: unread only. false or omitted: read and unread"),
    kind: Optional[str] = Query(None, max_length=40, description="One kind, e.g. payment_failed"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> AlertsPage:
    return await service.list_alerts(session, unread=unread, kind=kind or None, offset=offset, limit=limit)


@router.get("/unread-count", summary="How many admin alerts are unread")
async def unread_count(session: SessionDep, user: AdminAuth) -> UnreadCount:
    return {"unread": await service.unread_count(session)}


@router.post("/read-all", summary="Mark every admin alert read")
async def read_all(
    session: SessionDep,
    user: ModerateAuth,
    kind: Optional[str] = Query(None, max_length=40, description="Only this kind"),
) -> ReadAllOut:
    updated = await service.mark_all_read(session, kind=kind or None)
    await session.commit()
    return {"updated": updated, "unread": await service.unread_count(session)}


@router.post("/{alert_id}/read", summary="Mark one admin alert read")
async def read_one(alert_id: int, session: SessionDep, user: ModerateAuth) -> AlertOut:
    row = await service.get_alert(session, alert_id)
    if row is None:
        raise CustomHTTPException(status_code=404, message="Alert not found")
    await service.mark_read(session, row)
    await session.commit()
    return service.to_dict(row)
