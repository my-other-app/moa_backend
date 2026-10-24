"""Who can sign in to a club's dashboard: the list and its changes (the club's
own account), what the person signed in may do (everyone), and the invite
link's two pages (anyone holding the link)."""
from fastapi import APIRouter, BackgroundTasks, Request, status

from app.api.club_access import policy, service
from app.api.club_access.schemas import (
    AccessList,
    Accepted,
    InviteAccept,
    InvitePreview,
    InviteRequest,
    InviteResponse,
    Me,
    MemberRow,
    PasswordChange,
    RoleChange,
    RoleOption,
    WhatsAppPermission,
)
from app.api.club_access.models import DEFAULT_ROLE
from app.api.clubs.models import Clubs
from app.api.subscriptions import gates
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/clubs/access")


def _roles() -> list[RoleOption]:
    return [RoleOption(key=k, label=policy.ROLE_INFO[k][0], description=policy.ROLE_INFO[k][1]) for k in ("admin", "editor", "door")]


def _logo(club: Clubs) -> str | None:
    logo = club.logo
    if isinstance(logo, dict):
        return logo.get("thumbnail") or logo.get("medium") or logo.get("original")
    return logo if isinstance(logo, str) else None


async def _owners_club(session, request: Request, user) -> Clubs:
    """The club, for a change only its own account may make."""
    club = await service.club_of(session, user)
    if service.current_member(request) is not None:
        raise CustomHTTPException(status.HTTP_403_FORBIDDEN, message="Only the club's own account changes who can sign in", error_code="ROLE_FORBIDDEN")
    return club


async def _plan_allows(session, club: Clubs, role: str, user, *, new_seat: bool) -> None:
    """Every plan may add people as Editor (the default role) up to its
    committee seats; any other role is the plan's `roles`."""
    service.check_role(role)
    if role != DEFAULT_ROLE:
        await gates.require_feature(session, club.id, "roles", actor=user)
    if new_seat:
        await gates.require_within(session, club.id, "committee_seats", actor=user)


@router.get("", summary="Everyone who can sign in to this club's dashboard")
async def list_access(request: Request, session: SessionDep, user: ClubAuth) -> AccessList:
    club = await service.club_of(session, user)
    return AccessList(
        owner_email=user.email,
        members=[MemberRow(**service.to_row(m)) for m in await service.members_of(session, club)],
        roles=_roles(),
        can_manage=service.current_member(request) is None,
    )


@router.post("/invite", summary="Invite someone by email, with a role")
@limiter.limit("20/hour")
async def invite(request: Request, session: SessionDep, user: ClubAuth, body: InviteRequest, background: BackgroundTasks) -> InviteResponse:
    club = await _owners_club(session, request, user)
    await _plan_allows(session, club, body.role, user, new_seat=True)
    member, token = await service.invite(session, club, email=body.email, full_name=body.full_name, role=body.role)
    await session.commit()
    background.add_task(service.send_invite_email, member, club_name=club.name, token=token)
    return InviteResponse(member=MemberRow(**service.to_row(member)), invite_url=service.invite_url(token))


@router.post("/{member_id}/resend", summary="A new invite link, mailed again")
@limiter.limit("20/hour")
async def resend(request: Request, member_id: int, session: SessionDep, user: ClubAuth, background: BackgroundTasks) -> InviteResponse:
    club = await _owners_club(session, request, user)
    member, token = await service.resend(session, club, member_id)
    await session.commit()
    background.add_task(service.send_invite_email, member, club_name=club.name, token=token)
    return InviteResponse(member=MemberRow(**service.to_row(member)), invite_url=service.invite_url(token))


@router.patch("/{member_id}", summary="Change someone's role")
async def change_role(request: Request, member_id: int, session: SessionDep, user: ClubAuth, body: RoleChange) -> MemberRow:
    club = await _owners_club(session, request, user)
    await _plan_allows(session, club, body.role, user, new_seat=False)
    member = await service.set_role(session, club, member_id, body.role)
    await session.commit()
    return MemberRow(**service.to_row(member))


@router.put("/{member_id}/whatsapp", summary="Give or take someone's WhatsApp permission")
async def change_whatsapp(request: Request, member_id: int, session: SessionDep, user: ClubAuth, body: WhatsAppPermission) -> MemberRow:
    """The club's own WhatsApp inbox and templates are message content: only
    the people the owner names here (and the owner) read and answer them.
    Never an admin acting as the club (403 WHATSAPP_PRIVATE): otherwise one
    could invite a member, give them this and read the messages through them."""
    from app.api.audit import service as audit
    from app.api.club_whatsapp import access as whatsapp_access

    if gates.impersonated_by():
        raise CustomHTTPException(status.HTTP_403_FORBIDDEN, message=whatsapp_access.PRIVATE_MESSAGE, error_code="WHATSAPP_PRIVATE")
    club = await _owners_club(session, request, user)
    member = await service.set_whatsapp(session, club, member_id, body.allowed)
    await audit.record(
        session, actor=user, action="club_whatsapp.permission", entity_type="club", entity_id=club.id,
        summary=f"{'Gave' if body.allowed else 'Took'} the WhatsApp permission {'to' if body.allowed else 'from'} {member.email}",
        details={"member_id": member.id, "allowed": bool(body.allowed)}, request=request,
    )
    await session.commit()
    return MemberRow(**service.to_row(member))


@router.delete("/{member_id}", summary="Take someone's access away; their sessions end now")
async def remove(request: Request, member_id: int, session: SessionDep, user: ClubAuth) -> dict:
    club = await _owners_club(session, request, user)
    await service.remove(session, club, member_id)
    await session.commit()
    return {"removed": member_id}


@router.get("/me", summary="Who is signed in, and what their role may do")
async def me(request: Request, session: SessionDep, user: ClubAuth) -> Me:
    club = await service.club_of(session, user)
    member = service.current_member(request)
    role = member.role if member else "owner"
    label, description = policy.ROLE_INFO[role]
    can = policy.can(role)
    # The club's own WhatsApp inbox: the owner, or a member given the
    # permission -- never an admin acting as the club (club_whatsapp.access).
    can["whatsapp_inbox"] = not gates.impersonated_by() and (member is None or bool(member.whatsapp))
    return Me(
        role=role, role_label=label, role_description=description, can=can,
        club_id=club.id, club_name=club.name, member=MemberRow(**service.to_row(member)) if member else None,
    )


@router.post("/me/password", summary="A member changes their own password")
@limiter.limit("10/hour")
async def my_password(request: Request, session: SessionDep, user: ClubAuth, body: PasswordChange) -> dict:
    member = service.current_member(request)
    if member is None:
        raise CustomHTTPException(status.HTTP_400_BAD_REQUEST, message="The club's own password is changed on Security", error_code="NOT_A_MEMBER")
    await service.change_password(session, member, current=body.current_password, new=body.new_password)
    await session.commit()
    return {"changed": True}


@router.get("/invite/{token}", summary="What an invite link is for, before it is accepted")
@limiter.limit("30/minute")
async def preview_invite(request: Request, token: str, session: SessionDep) -> InvitePreview:
    member, club = await service.by_token(session, token)
    label, description = policy.ROLE_INFO[member.role]
    return InvitePreview(
        club_name=club.name, club_logo=_logo(club), email=member.email, full_name=member.full_name,
        role=member.role, role_label=label, role_description=description, expired=service.invite_expired(member),
    )


@router.post("/invite/{token}/accept", summary="Set a name and password and join")
@limiter.limit("10/minute")
async def accept_invite(request: Request, token: str, session: SessionDep, body: InviteAccept) -> Accepted:
    member, club = await service.by_token(session, token)
    await service.accept(session, token, full_name=body.full_name, password=body.password)
    await session.commit()
    return Accepted(email=member.email, club_name=club.name)
