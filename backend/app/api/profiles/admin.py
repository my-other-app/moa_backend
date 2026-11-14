"""The admins' side of profiles: the reports queue's profile reports
(app/api/reports uses these), taking a link or a project down, and showing
a hidden profile again. Every change is in the admin audit log."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select

from app.api.audit import service as audit
from app.api.profiles.models import ProfileLinks, ProfileProjects
from app.api.users.models import UserProfiles, Users
from app.db.listeners import INCLUDE_DELETED
from app.response import CustomHTTPException


def _now() -> datetime:
    return datetime.now(timezone.utc)


def preview(user: Users, profile) -> str:
    """The queue's words for a reported profile: "<full_name> (@<handle>) — <headline>"."""
    name = (profile.full_name if profile is not None and profile.full_name else user.full_name) or user.username
    text = f"{name} (@{user.username})"
    headline = profile.headline if profile is not None else None
    return f"{text} — {headline}" if headline else text


async def reported(session, user_ids: Iterable[int]) -> dict[int, dict]:
    """For each reported person: their preview, whether the profile is
    hidden, and AdminReport.profile (its links and projects, by id)."""
    ids = {int(i) for i in user_ids}
    if not ids:
        return {}
    users = {u.id: u for u in (await session.scalars(select(Users).where(Users.id.in_(ids)).execution_options(**{INCLUDE_DELETED: True}))).all()}
    profiles = {p.user_id: p for p in (await session.scalars(select(UserProfiles).where(UserProfiles.user_id.in_(ids)))).all()}
    links: dict[int, list] = {}
    for link in (await session.scalars(select(ProfileLinks).where(ProfileLinks.user_id.in_(ids)).order_by(ProfileLinks.position, ProfileLinks.id))).all():
        links.setdefault(link.user_id, []).append({"id": link.id, "type": link.type, "url": link.url, "label": link.label})
    projects: dict[int, list] = {}
    for p in (await session.scalars(select(ProfileProjects).where(ProfileProjects.user_id.in_(ids)).order_by(ProfileProjects.position, ProfileProjects.id))).all():
        projects.setdefault(p.user_id, []).append({"id": p.id, "title": p.title, "url": p.url})
    out = {}
    for uid, user in users.items():
        profile = profiles.get(uid)
        suspended = profile is not None and profile.suspended_at is not None
        out[uid] = {
            "preview": preview(user, profile),
            "removed": suspended or bool(user.is_deleted),
            "profile": {
                "user_id": uid, "handle": user.username, "suspended": suspended,
                "links": links.get(uid, []), "projects": projects.get(uid, []),
            },
        }
    return out


async def hide(session, user_id: int) -> bool:
    """A report's "hide profile": the profile, its vCard and its code 404.
    True when it was showing. The caller commits."""
    from app.api.users.service import _ensure_profile

    if await session.scalar(select(Users.id).where(Users.id == user_id).execution_options(**{INCLUDE_DELETED: True})) is None:
        return False
    profile = await _ensure_profile(session, user_id)
    if profile.suspended_at is not None:
        return False
    profile.suspended_at = _now()
    return True


async def _profile_of(session, user_id: int) -> UserProfiles:
    profile = await session.scalar(select(UserProfiles).where(UserProfiles.user_id == user_id))
    if profile is None:
        raise CustomHTTPException(404, message="That person has no profile.", error_code="NOT_FOUND")
    return profile


async def unhide(session, admin, user_id: int, request=None) -> None:
    profile = await _profile_of(session, user_id)
    was = profile.suspended_at
    profile.suspended_at = None
    await audit.record(
        session, actor=admin, action="profile.unhide", entity_type="user", entity_id=user_id,
        summary=f"Showed the profile of user {user_id} again", details={"hidden_at": was.isoformat() if was else None}, request=request,
    )
    await session.commit()


async def remove_link(session, admin, user_id: int, link_id: int, request=None) -> None:
    row = await session.scalar(select(ProfileLinks).where(ProfileLinks.id == link_id, ProfileLinks.user_id == user_id))
    if row is None:
        raise CustomHTTPException(404, message="That link isn't on this profile.", error_code="NOT_FOUND")
    details = {"link_id": row.id, "type": row.type, "url": row.url, "label": row.label}
    await session.delete(row)
    await audit.record(
        session, actor=admin, action="profile.link_remove", entity_type="user", entity_id=user_id,
        summary=f"Removed a {row.type} link from the profile of user {user_id}", details=details, request=request,
    )
    await session.commit()


async def remove_project(session, admin, user_id: int, project_id: int, request=None) -> None:
    row = await session.scalar(select(ProfileProjects).where(ProfileProjects.id == project_id, ProfileProjects.user_id == user_id))
    if row is None:
        raise CustomHTTPException(404, message="That project isn't on this profile.", error_code="NOT_FOUND")
    image = row.image_key
    details = {"project_id": row.id, "title": row.title, "url": row.url}
    await session.delete(row)
    await audit.record(
        session, actor=admin, action="profile.project_remove", entity_type="user", entity_id=user_id,
        summary=f"Removed the project “{row.title}” from the profile of user {user_id}", details=details, request=request,
    )
    await session.commit()
    if image is not None and hasattr(image, "delete"):
        image.delete()
