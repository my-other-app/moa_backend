from datetime import date
from typing import Optional

from fastapi import APIRouter, Query, Response

from app.api.contacts import service
from app.api.contacts.schemas import ContactsPage
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep

router = APIRouter(prefix="/clubs/contacts")


@router.get("", summary="The club's contact book")
async def contacts(
    session: SessionDep, user: ClubAuth,
    q: Optional[str] = Query(None, max_length=120), event_id: Optional[int] = Query(None), attended: Optional[bool] = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    since: Optional[date] = Query(None, description="Events that ended on or after this day"),
    until: Optional[date] = Query(None, description="Events that ended on or before this day"),
) -> ContactsPage:
    """What retention kept of each event's guests -- only events whose club
    switched on "keep contacts" at close-out: name, email, phone, role, and
    whether they came."""
    return ContactsPage(**await service.page(session, user.club.id, q=q, event_id=event_id, attended=attended, page=page, page_size=page_size, since=since, until=until))


@router.get("/export.csv", summary="The contact book as a file")
async def export(session: SessionDep, user: ClubAuth, q: Optional[str] = Query(None, max_length=120), event_id: Optional[int] = Query(None), attended: Optional[bool] = Query(None),
                 since: Optional[date] = Query(None), until: Optional[date] = Query(None)) -> Response:
    body = await service.export_csv(session, user.club.id, q=q, event_id=event_id, attended=attended, since=since, until=until)
    return Response(content=body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="contacts.csv"'})
