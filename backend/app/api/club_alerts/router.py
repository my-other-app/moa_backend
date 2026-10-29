"""The club dashboard's bell: what the platform told the club, newest first."""
from fastapi import APIRouter, Query, Request
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


def _hidden(request: Request) -> frozenset:
    """The bell's kinds this viewer may not see: the WhatsApp inbox's without
    the WhatsApp permission, the mailbox's without the Mail one, co-hosting's
    (invitations and answers: app/api/cohosts) below admin."""
    from app.api.club_access import service as club_access
    from app.api.club_mailbox.alerts import hidden_kinds as mail_hidden
    from app.api.club_whatsapp.alerts_inbox import hidden_kinds as whatsapp_hidden
    from app.api.cohosts.service import ALERT_KINDS as cohost_kinds

    cohosts = frozenset() if club_access.role_of(request) in ("owner", "admin") else cohost_kinds
    return whatsapp_hidden(request) | mail_hidden(request) | cohosts


async def _club(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


@router.get("", summary="The club's notices, newest first, and how many are unread")
async def list_alerts(request: Request, session: SessionDep, user: ClubAuth, limit: int = Query(service.LIMIT, ge=1, le=100)) -> ClubAlertsOut:
    """The WhatsApp inbox's and the mailbox's notices only to those who can read them."""
    club = await _club(session, user)
    return await service.listing(session, club.id, limit=limit, hide=_hidden(request))


@router.post("/read", summary="Mark the club's notices read (these, or all)")
async def mark_read(payload: MarkReadIn, request: Request, session: SessionDep, user: ClubAuth) -> MarkReadOut:
    club = await _club(session, user)
    return {"marked": await service.mark_read(session, club.id, payload.ids, hide=_hidden(request))}
