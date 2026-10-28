"""Co-hosted events (docs/COHOSTS.md).

The host's side, one event: `/events/{event_id}/cohosts` -- the list (host,
co-hosts, admins), looking for clubs, inviting, cancelling or removing (the
host's owner and admins), and leaving (a co-host's owner and admins).
The invited club's side: `/clubs/cohosts/invitations` -- its invitations,
accepting and declining (its owner and admins). Roles are club_access.policy's;
what a co-host may not do is cohosts.policy's, both decided before a route runs.
"""
from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.api.cohosts import service
from app.api.cohosts.schemas import ClubPicks, Invitation, Invitations, InviteIn, InviteOut, Listing
from app.api.deliveries import service as deliveries
from app.core.auth.dependencies import ClubAuth
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/events/{event_id}/cohosts")
club_router = APIRouter(prefix="/clubs/cohosts")


@router.get("", summary="The event's host, co-hosts and invites")
async def list_cohosts(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> Listing:
    event = await deliveries.event_for_club(session, user, event_id)
    return Listing(**await service.listing(session, event, user, request))


@router.get("/search", summary="Clubs to invite, by name or handle")
async def search_clubs(session: SessionDep, user: ClubAuth, event_id: int, q: str = Query("", max_length=100)) -> ClubPicks:
    event = await deliveries.event_for_club(session, user, event_id)
    return ClubPicks(**await service.search(session, event, q))


@router.post("", summary="Invite a club to co-host the event", status_code=201)
async def invite(request: Request, session: SessionDep, user: ClubAuth, event_id: int, body: InviteIn) -> InviteOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return InviteOut(**await service.invite(session, event, user, request, club_id=body.club_id, note=body.note))


@router.post("/leave", summary="Stop co-hosting the event (a co-host)")
async def leave(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> InviteOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return InviteOut(**await service.leave(session, event, user, request))


@router.delete("/{invite_id}", summary="Withdraw a pending invite, or remove a co-host")
async def end_invite(request: Request, session: SessionDep, user: ClubAuth, event_id: int, invite_id: int) -> InviteOut:
    event = await deliveries.event_for_club(session, user, event_id)
    return InviteOut(**await service.end(session, event, user, request, invite_id))


async def _own_club(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


@club_router.get("/invitations", summary="Invitations to co-host other clubs' events")
async def invitations(session: SessionDep, user: ClubAuth, status: str = Query("pending", pattern="^(pending|all)$")) -> Invitations:
    club = await _own_club(session, user)
    return Invitations(**await service.invitations(session, club, status=status))


@club_router.post("/invitations/{invite_id}/accept", summary="Accept an invitation: co-host the event")
async def accept(request: Request, session: SessionDep, user: ClubAuth, invite_id: int) -> Invitation:
    club = await _own_club(session, user)
    return Invitation(**await service.answer(session, club, user, request, invite_id, accept=True))


@club_router.post("/invitations/{invite_id}/decline", summary="Decline an invitation")
async def decline(request: Request, session: SessionDep, user: ClubAuth, invite_id: int) -> Invitation:
    club = await _own_club(session, user)
    return Invitation(**await service.answer(session, club, user, request, invite_id, accept=False))
