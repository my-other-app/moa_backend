"""The club dashboard's bell: what the platform told the club, newest first."""
from fastapi import APIRouter, Query
from pydantic import Field

from app.api.club_alerts import service
from app.api.clubs.models import Clubs
from app.core.auth.dependencies import ClubAuth
from app.core.response.base_model import CustomBaseModel
from app.db.core import SessionDep
from app.response import CustomHTTPException
from datetime import datetime
from sqlalchemy import select

router = APIRouter(prefix="/clubs/alerts", tags=["club-alerts"])


class ClubAlertOut(CustomBaseModel):
    id: int
    #: club_alerts.service.KINDS
    kind: str
    title: str
    body: str = ""
    #: Where in the dashboard it leads, or null.
    link: str | None = None
    data: dict = Field(default_factory=dict)
    created_at: datetime
    read: bool = False


class ClubAlertsOut(CustomBaseModel):
    items: list[ClubAlertOut]
    #: Unread across all of them, for the bell's badge.
    unread: int = 0


class MarkReadIn(CustomBaseModel):
    #: These notices; omitted or empty: every one.
    ids: list[int] | None = Field(None, max_length=200)


class MarkReadOut(CustomBaseModel):
    marked: int = 0


async def _club(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


@router.get("", summary="The club's notices, newest first, and how many are unread")
async def list_alerts(session: SessionDep, user: ClubAuth, limit: int = Query(service.LIMIT, ge=1, le=100)) -> ClubAlertsOut:
    club = await _club(session, user)
    return await service.listing(session, club.id, limit=limit)


@router.post("/read", summary="Mark the club's notices read (these, or all)")
async def mark_read(payload: MarkReadIn, session: SessionDep, user: ClubAuth) -> MarkReadOut:
    club = await _club(session, user)
    return {"marked": await service.mark_read(session, club.id, payload.ids)}
