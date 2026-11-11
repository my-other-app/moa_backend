"""The Support and Sales teams: who is in them, who is away, whose turn it
is, and what each admin may do on the desk.

| Who                  | Support cases                                   | Leads                                      |
| -------------------- | ----------------------------------------------- | ------------------------------------------ |
| Admin owner          | everything (teams, topics, rules, reassigning)  | everything, reassigning included           |
| Support team member  | read every case and its messages; answer, note  | read; turn a case into a lead              |
|                      | and close their own; take unassigned ones; log  |                                            |
|                      | one by hand                                     |                                            |
| Sales team member    | read every case and its messages                | their own leads: edit, log, move; create   |
| Any other admin      | the lists and counts, never a message's body    | read                                       |

Membership is the whole grant: a viewer-role admin in the support team
answers cases and still cannot refund a payment (the refund keeps its own
MANAGE_MONEY), suspend an account or change a club.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, or_, select, update

from app.api.support.models import SALES, SUPPORT, TEAMS, UNFINISHED, AdminTeamMembers, AdminTeams, SupportCases
from app.api.users.models import UserTypes, Users
from app.core.auth.permissions import is_desk_owner, teams_of
from app.response import CustomHTTPException


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Rights:
    user_id: int
    owner: bool
    support: bool
    sales: bool

    @property
    def reads_bodies(self) -> bool:
        """Message bodies: the teams and the owners; nobody else."""
        return self.owner or self.support or self.sales

    @property
    def any_team(self) -> bool:
        return self.support or self.sales

    def require(self, what: str, *, support: bool = False, sales: bool = False) -> None:
        """403 TEAM_REQUIRED unless an owner, or in one of the teams named."""
        if self.owner or (support and self.support) or (sales and self.sales):
            return
        teams = " or ".join(t for t, on in (("Support", support), ("Sales", sales)) if on) or "an owner's"
        raise CustomHTTPException(
            403, message=f"Only the {teams} team can {what}." if teams != "an owner's" else f"Only an admin owner can {what}.",
            error_code="TEAM_REQUIRED",
        )

    def require_owner(self, what: str) -> None:
        if not self.owner:
            raise CustomHTTPException(403, message=f"Only an admin owner can {what}.", error_code="OWNER_REQUIRED")


async def rights_of(session, user) -> Rights:
    teams = await teams_of(session, user)
    return Rights(user_id=user.id, owner=is_desk_owner(user), support=SUPPORT in teams, sales=SALES in teams)


def available(now: Optional[datetime] = None):
    """A member who takes new work: not away, or away until a moment now past."""
    now = now or _now()
    M = AdminTeamMembers
    return or_(M.away.is_(False), and_(M.away_until.isnot(None), M.away_until <= now))


def _live_admin():
    return and_(Users.user_type == UserTypes.admin, Users.is_deleted.is_(False), Users.is_active.is_(True))


async def next_member(session, team: str, now: Optional[datetime] = None) -> Optional[int]:
    """Round robin: the available member who was given work longest ago (or
    never), marked as given it now. None when nobody is available."""
    now = now or _now()
    M = AdminTeamMembers
    row = await session.scalar(
        select(M).join(Users, Users.id == M.user_id)
        .where(M.team == team, available(now), _live_admin())
        .order_by(M.last_assigned_at.asc().nullsfirst(), M.id)
        .limit(1).with_for_update(of=M, skip_locked=True)
    )
    if row is None:
        return None
    row.last_assigned_at = now
    return row.user_id


async def is_available(session, team: str, user_id: Optional[int], now: Optional[datetime] = None) -> bool:
    if not user_id:
        return False
    M = AdminTeamMembers
    found = await session.scalar(
        select(M.id).join(Users, Users.id == M.user_id)
        .where(M.team == team, M.user_id == user_id, available(now), _live_admin())
    )
    return found is not None


async def members_of(session, team: str) -> list[int]:
    return list((await session.scalars(select(AdminTeamMembers.user_id).where(AdminTeamMembers.team == team))).all())


def _member_out(m: AdminTeamMembers, u: Users) -> dict:
    return {
        "user_id": u.id, "name": u.full_name, "email": u.email,
        "admin_role": getattr(u.admin_role, "value", u.admin_role), "away": bool(m.away), "away_until": m.away_until,
    }


async def teams_out(session) -> list[dict]:
    from app.api.support import reference

    await reference.ensure(session)
    labels = dict((await session.execute(select(AdminTeams.key, AdminTeams.label))).all())
    rows = (await session.execute(
        select(AdminTeamMembers, Users).join(Users, Users.id == AdminTeamMembers.user_id)
        .where(Users.is_deleted.is_(False)).order_by(Users.full_name, Users.id)
    )).all()
    out = []
    for key in TEAMS:
        out.append({
            "key": key, "label": labels.get(key, key.title()),
            "members": [_member_out(m, u) for m, u in rows if m.team == key],
        })
    return out


async def eligible(session) -> list[dict]:
    """Every admin account an owner may put in a team, with the teams it is in."""
    admins = (await session.scalars(
        select(Users).where(_live_admin()).order_by(Users.full_name, Users.id)
    )).all()
    pairs = (await session.execute(select(AdminTeamMembers.user_id, AdminTeamMembers.team))).all()
    teams: dict[int, list[str]] = {}
    for uid, team in pairs:
        teams.setdefault(uid, []).append(team)
    return [
        {"user_id": a.id, "name": a.full_name, "email": a.email, "admin_role": getattr(a.admin_role, "value", a.admin_role),
         "teams": sorted(teams.get(a.id, []))}
        for a in admins
    ]


async def release_work(session, team: str, user_ids: Iterable[int], *, leaving: bool) -> dict:
    """What a member away (or leaving) hands back: their unfinished support
    cases go back to Unassigned; a sales member who leaves has their open
    leads' owner emptied, for an owner to reassign (an away one keeps them)."""
    ids = [int(u) for u in user_ids]
    out = {"cases": 0, "leads": 0}
    if not ids:
        return out
    if team == SUPPORT:
        result = await session.execute(
            update(SupportCases).where(SupportCases.assignee_id.in_(ids), SupportCases.status.in_(UNFINISHED))
            .values(assignee_id=None, updated_at=_now()).execution_options(synchronize_session=False)
        )
        out["cases"] = int(result.rowcount or 0)
    if team == SALES and leaving:
        from app.api.leads.models import OPEN_STAGES, FollowUps, Leads

        lead_ids = list((await session.scalars(
            select(Leads.id).where(Leads.owner_id.in_(ids), Leads.stage.in_(OPEN_STAGES), Leads.merged_into_id.is_(None))
        )).all())
        if lead_ids:
            await session.execute(
                update(Leads).where(Leads.id.in_(lead_ids)).values(owner_id=None, updated_at=_now())
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                update(FollowUps).where(FollowUps.lead_id.in_(lead_ids), FollowUps.owner_id.in_(ids), FollowUps.done.is_(False))
                .values(owner_id=None).execution_options(synchronize_session=False)
            )
        out["leads"] = len(lead_ids)
    return out


async def set_members(session, team: str, user_ids: list[int]) -> dict:
    """The team is exactly these admin accounts. 422 for one that isn't an
    admin. Returns {added, removed, released}."""
    from app.api.support import reference

    await reference.ensure(session)
    wanted = list(dict.fromkeys(int(u) for u in user_ids))
    if wanted:
        found = set((await session.scalars(select(Users.id).where(Users.id.in_(wanted), _live_admin()))).all())
        missing = [u for u in wanted if u not in found]
        if missing:
            raise CustomHTTPException(
                422, message="Only admin accounts can be in a team. Give the person an admin account (viewer) first.",
                error_code="NOT_AN_ADMIN", errors={"user_ids": missing},
            )
    M = AdminTeamMembers
    current = {m.user_id: m for m in (await session.scalars(select(M).where(M.team == team))).all()}
    added = [u for u in wanted if u not in current]
    removed = [u for u in current if u not in wanted]
    for u in added:
        session.add(M(team=team, user_id=u))
    for u in removed:
        await session.delete(current[u])
    released = await release_work(session, team, removed, leaving=True)
    await session.flush()
    return {"added": added, "removed": removed, "released": released}


async def set_away(session, user, away: bool, away_until: Optional[datetime]) -> list[dict]:
    """Mark the signed-in admin away (or back) in every team they are in.
    404 NOT_IN_A_TEAM for an admin in none. Away hands their open cases back."""
    M = AdminTeamMembers
    rows = list((await session.scalars(select(M).where(M.user_id == user.id))).all())
    if not rows:
        raise CustomHTTPException(404, message="You aren't in a team.", error_code="NOT_IN_A_TEAM")
    if away_until is not None and away_until.tzinfo is None:
        away_until = away_until.replace(tzinfo=timezone.utc)
    if away and away_until is not None and away_until <= _now():
        raise CustomHTTPException(422, message="Away until has to be later than now.", error_code="INVALID_AWAY_UNTIL")
    for m in rows:
        m.away = bool(away)
        m.away_until = away_until if away else None
    if away:
        for m in rows:
            await release_work(session, m.team, [user.id], leaving=False)
    await session.flush()
    return [{"team": m.team, "away": m.away, "away_until": m.away_until} for m in rows]


async def back_from_away(session, now: Optional[datetime] = None) -> int:
    """Members whose away_until has passed, back (the sweep)."""
    now = now or _now()
    M = AdminTeamMembers
    result = await session.execute(
        update(M).where(M.away.is_(True), M.away_until.isnot(None), M.away_until <= now)
        .values(away=False, away_until=None).execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def names(session, user_ids: Iterable[Optional[int]]) -> dict[int, str]:
    ids = sorted({int(u) for u in user_ids if u})
    if not ids:
        return {}
    return dict((await session.execute(select(Users.id, Users.full_name).where(Users.id.in_(ids)))).all())
