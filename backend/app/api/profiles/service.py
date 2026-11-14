"""A person's profile: what each viewer sees of it, and what its owner changes.

`public_profile(owner, relation)` is the one place a PublicProfile is made,
for the app, the website, the vCard and the network alike (access.py says
who sees what). Lists of people use `briefs`.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.profiles import access, rules
from app.api.profiles.access import Relation
from app.api.profiles.models import (
    MAX_LINKS, MAX_PROJECTS, SECTIONS, ProfileCodes, ProfileLinks, ProfileProjects,
)
from app.api.users.models import UserProfiles, Users
from app.core import public_urls
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: How many certificates, talks and clubs a profile lists.
ACHIEVEMENTS_LISTED = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def profile_url(handle: str) -> str:
    return public_urls.site_url(f"/u/{handle}")


def code_url(token: str) -> str:
    return public_urls.site_url(f"/p/{token}")


# ─── Pieces ─────────────────────────────────────────────────────────────


def full_name_of(user: Users) -> str:
    """The name the person goes by: their profile's (what they edit), else the account's."""
    profile = user.profile
    return (profile.full_name if profile is not None and profile.full_name else user.full_name) or user.username or ""


def picture_of(user: Users) -> Optional[str]:
    """The photo they uploaded, else the avatar they picked (People Like You's rule)."""
    from app.api.events.networking import _picture

    return _picture(user.profile)


def _image_link(image) -> Optional[str]:
    if isinstance(image, dict):
        return image.get("medium") or image.get("large") or image.get("original")
    return image if isinstance(image, str) and image else None


def project_out(p: ProfileProjects) -> dict:
    return {
        "id": p.id, "title": p.title, "summary": p.summary, "url": p.url, "image": _image_link(p.image_key),
        "tags": list(p.tags or []), "role": p.role, "month": p.month,
    }


async def _links(session, user_id: int) -> list[ProfileLinks]:
    return list((await session.scalars(
        select(ProfileLinks).where(ProfileLinks.user_id == user_id).order_by(ProfileLinks.position, ProfileLinks.id)
    )).all())


async def _projects(session, user_id: int) -> list[ProfileProjects]:
    return list((await session.scalars(
        select(ProfileProjects).where(ProfileProjects.user_id == user_id).order_by(ProfileProjects.position, ProfileProjects.id)
    )).all())


async def _interests(session, user_id: int) -> list[dict]:
    from app.api.interests.models import InterestIconType, Interests
    from app.api.users.models import UserInterests

    rows = (await session.scalars(
        select(Interests)
        .join(UserInterests, UserInterests.interest_id == Interests.id)
        .where(UserInterests.user_id == user_id, UserInterests.is_deleted == False, Interests.is_deleted == False)  # noqa: E712
        .order_by(Interests.sort_order, Interests.name)
    )).all()
    out = []
    for i in rows:
        emoji = i.icon if i.icon_type == InterestIconType.emoji and i.icon else None
        out.append({"id": i.id, "name": i.name, "emoji": emoji})
    return out


async def _achievements(session, user_id: int, settings: dict) -> dict:
    """What the person chose to show of what they did: events attended (a
    count), certificates, talks and clubs. Certificates and talks are of
    events anyone may open (discover's rule): a private event's name never
    reaches a public page."""
    from app.api.clubs.models import ClubUsersLink, Clubs
    from app.api.events.models import EventRegistrationsLink as R
    from app.api.events.models import Events
    from app.api.speakers import service as speakers
    from app.api.speakers.models import ACTIVE, SpeakerProfiles

    out: dict = {"events_attended": None, "certificates": None, "talks": None, "clubs": None}
    if settings["show_events"]:
        out["events_attended"] = int(await session.scalar(
            select(func.count(func.distinct(R.event_id)))
            .join(Events, Events.id == R.event_id)
            .where(R.user_id == user_id, R.is_attended == True, R.is_deleted == False, Events.is_deleted == False)  # noqa: E712
        ) or 0)
    if settings["show_certificates"]:
        rows = (await session.execute(
            select(Events.name, R.certificate_public_id)
            .join(Events, Events.id == R.event_id)
            .join(Clubs, Clubs.id == Events.club_id)
            .where(
                R.user_id == user_id, R.is_deleted == False,  # noqa: E712
                R.certificate_issued_at.isnot(None), R.certificate_revoked_at.is_(None), R.certificate_public_id.isnot(None),
                *speakers.public_event_where(_now()),
            )
            .order_by(Events.event_datetime.desc(), R.id.desc())
            .limit(ACHIEVEMENTS_LISTED)
        )).all()
        out["certificates"] = [{"event_name": name, "url": public_urls.certificate_url(pid)} for name, pid in rows]
    if settings["show_talks"]:
        sp_ids = list((await session.scalars(
            select(SpeakerProfiles.id).where(
                SpeakerProfiles.claimed_by_user_id == user_id, SpeakerProfiles.status == ACTIVE, SpeakerProfiles.removed_at.is_(None),
            )
        )).all())
        talks: list[dict] = []
        if sp_ids:
            for rows in (await speakers.talks(session, sp_ids, public_only=True)).values():
                talks.extend(rows)
        talks.sort(key=lambda t: t.get("date") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        out["talks"] = [
            {"event_name": t["event_name"], "title": t.get("talk_title"), "event_slug": t["slug"]} for t in talks[:ACHIEVEMENTS_LISTED]
        ]
    if settings["show_clubs"]:
        rows = (await session.execute(
            select(Clubs.name, Clubs.slug)
            .join(ClubUsersLink, ClubUsersLink.club_id == Clubs.id)
            .where(
                ClubUsersLink.user_id == user_id, ClubUsersLink.is_following == True, ClubUsersLink.is_deleted == False,  # noqa: E712
                Clubs.is_deleted == False, Clubs.is_verified == True,  # noqa: E712
            )
            .order_by(Clubs.name)
            .limit(ACHIEVEMENTS_LISTED)
        )).all()
        out["clubs"] = [{"name": name, "slug": slug} for name, slug in rows]
    return out


# ─── What a viewer sees ─────────────────────────────────────────────────


def _minimal(user: Users, rel: Relation) -> dict:
    """A private profile, to someone its owner has not saved: the handle,
    name, picture and headline. Every other key is there, empty, so a
    client reads one shape."""
    profile = user.profile
    return {
        "user_id": user.id if rel.viewer_id is not None else None,
        "handle": user.username, "full_name": full_name_of(user), "picture": picture_of(user),
        "headline": profile.headline if profile is not None else None,
        "bio": None, "city": None, "org": None, "interests": [], "links": [], "projects": [],
        "achievements": {"events_attended": None, "certificates": None, "talks": None, "clubs": None},
        "contact": {"email": None, "phone": None},
        "hidden_sections": list(SECTIONS), "private": True, "indexable": False, "is_me": False,
        "relation": rel.out(),
    }


async def public_profile(session, user: Users, rel: Relation) -> dict:
    """The PublicProfile this viewer may see of `user` (a live person: the
    caller found them with access.visible_person)."""
    settings = access.settings_of(user.profile)
    if access.minimal(settings, rel):
        return _minimal(user, rel)
    profile = user.profile
    levels = settings["visibility"]
    sees = {section: rel.sees(levels.get(section)) for section in SECTIONS}
    org = profile.org if profile is not None and profile.org_id else None
    out = {
        # Only for a signed-in viewer: a signed-out read (the site's cached page) never carries it.
        "user_id": user.id if rel.viewer_id is not None else None,
        "handle": user.username,
        "full_name": full_name_of(user),
        "picture": picture_of(user),
        "headline": profile.headline if profile is not None else None,
        "bio": profile.bio if profile is not None else None,
        "city": profile.city if profile is not None else None,
        "org": {"id": org.id, "name": org.name} if sees["org"] and org is not None else None,
        "interests": await _interests(session, user.id) if sees["interests"] else [],
        "links": [{"type": link.type, "url": link.url, "label": link.label} for link in await _links(session, user.id)] if sees["links"] else [],
        "projects": [project_out(p) for p in await _projects(session, user.id)] if sees["projects"] else [],
        "achievements": (
            await _achievements(session, user.id, settings) if sees["achievements"]
            else {"events_attended": None, "certificates": None, "talks": None, "clubs": None}
        ),
        "contact": {
            "email": user.email if rel.shares(settings["share_email"]) else None,
            "phone": user.phone if rel.shares(settings["share_phone"]) else None,
        },
        "hidden_sections": [s for s in SECTIONS if not sees[s]],
        "private": bool(settings["is_private"]),
        "indexable": bool(settings["indexable"]) and not settings["is_private"],
        "is_me": rel.is_me,
        "relation": rel.out(),
    }
    return out


def completeness(user: Users, *, has_links: bool, has_projects: bool) -> dict:
    profile = user.profile
    have = {
        "headline": bool(profile is not None and profile.headline),
        "links": has_links,
        "projects": has_projects,
        "photo": bool(profile is not None and profile.profile_pic),
    }
    missing = [k for k, ok in have.items() if not ok]
    return {"score": round(100 * (len(have) - len(missing)) / len(have)), "missing": missing}


async def my_profile(session, user_id: int) -> dict:
    """MyProfile: the owner's own view, their switches, how complete it is,
    and their own address and number."""
    user = await access.load_person_any(session, user_id)
    out = await public_profile(session, user, Relation(viewer_id=user.id, is_me=True))
    settings = access.settings_of(user.profile)
    out["settings"] = {k: settings[k] for k in access.SETTING_KEYS}
    out["completeness"] = completeness(user, has_links=bool(out["links"]), has_projects=bool(out["projects"]))
    out["email"] = user.email
    out["phone"] = user.phone
    return out


def brief(user: Users, rel: Relation) -> dict:
    """PersonBrief, for this viewer: the organisation only where its level
    allows, and never on a private profile they are a stranger to."""
    settings = access.settings_of(user.profile)
    profile = user.profile
    org_name = None
    if not access.minimal(settings, rel) and rel.sees(settings["visibility"].get("org")):
        org = profile.org if profile is not None and profile.org_id else None
        org_name = org.name if org is not None else None
    return {
        "user_id": user.id, "handle": user.username, "full_name": full_name_of(user), "picture": picture_of(user),
        "headline": profile.headline if profile is not None else None, "org_name": org_name,
    }


async def people(session, ids: Iterable[int]) -> dict[int, Users]:
    """People by id, with what a brief reads loaded (deleted ones too: the
    caller has already decided who may show)."""
    ids = {int(i) for i in ids if i is not None}
    if not ids:
        return {}
    rows = await session.scalars(select(Users).where(Users.id.in_(ids)).options(*access.person_options()))
    return {u.id: u for u in rows.all()}


# ─── Changing one's own ─────────────────────────────────────────────────


async def own_profile_locked(session, user_id: int) -> UserProfiles:
    """The person's profile row, made if missing, locked for this
    transaction: two edits of the links or projects at once take turns."""
    from app.api.users.service import _ensure_profile

    await _ensure_profile(session, user_id)
    return await session.scalar(
        select(UserProfiles).where(UserProfiles.user_id == user_id).with_for_update().execution_options(populate_existing=True)
    )


async def set_handle(session, user: Users, raw) -> None:
    """The person's handle (their username): 422 HANDLE_INVALID or
    HANDLE_RESERVED, 409 HANDLE_TAKEN (case-blind, deleted accounts' too)."""
    handle = rules.check_handle(raw)
    if handle == user.username:
        return
    taken = await session.scalar(
        select(Users.id).where(func.lower(Users.username) == handle, Users.id != user.id).limit(1)
        .execution_options(include_deleted=True)
    )
    if taken is not None:
        raise CustomHTTPException(409, message="That handle is taken.", error_code="HANDLE_TAKEN")
    try:
        async with session.begin_nested():
            await session.execute(update(Users).where(Users.id == user.id).values(username=handle).execution_options(synchronize_session=False))
    except IntegrityError:
        # Someone took it between the look and the write.
        raise CustomHTTPException(409, message="That handle is taken.", error_code="HANDLE_TAKEN")
    user.username = handle


async def update_profile(session, user: Users, body) -> dict:
    from app.api.orgs.models import Organizations

    sent = body.model_fields_set
    profile = await own_profile_locked(session, user.id)
    if "handle" in sent:
        await set_handle(session, user, body.handle)
    if "headline" in sent:
        profile.headline = rules.one_line(body.headline, 120)
    if "bio" in sent:
        profile.bio = rules.paragraph(body.bio, 300)
    if "city" in sent:
        profile.city = rules.one_line(body.city, 80)
    if "org_id" in sent:
        if body.org_id is not None:
            org = await session.scalar(select(Organizations).where(
                Organizations.id == body.org_id, Organizations.is_deleted == False, Organizations.is_blocked == False,  # noqa: E712
            ))
            if org is None:
                raise CustomHTTPException(422, message="We couldn't find that organisation.", error_code="ORG_NOT_FOUND")
        profile.org_id = body.org_id
    if "visibility" in sent and body.visibility:
        merged = access.settings_of(profile)["visibility"]
        merged.update(body.visibility)
        profile.visibility = merged
    for key in access.DEFAULTS:
        if key in sent:
            setattr(profile, key, getattr(body, key))
    await session.commit()
    return await my_profile(session, user.id)


async def put_links(session, user: Users, links) -> dict:
    """The whole ordered list of links, replacing the one before."""
    if len(links) > MAX_LINKS:
        raise CustomHTTPException(422, message=f"A profile has at most {MAX_LINKS} links.", error_code="LINK_INVALID")
    rows = []
    for i, link in enumerate(links):
        rows.append({
            "user_id": user.id, "type": link.type, "url": rules.check_url(link.url, field=f"links.{i}.url"),
            "label": rules.one_line(link.label, 60), "position": i,
        })
    await own_profile_locked(session, user.id)
    await session.execute(delete(ProfileLinks).where(ProfileLinks.user_id == user.id))
    if rows:
        await session.execute(pg_insert(ProfileLinks).values(rows))
    await session.commit()
    return await my_profile(session, user.id)


def _project_not_found() -> CustomHTTPException:
    return CustomHTTPException(404, message="We couldn't find that project.", error_code="NOT_FOUND")


async def _own_project(session, user: Users, project_id: int) -> ProfileProjects:
    row = await session.scalar(select(ProfileProjects).where(ProfileProjects.id == project_id, ProfileProjects.user_id == user.id))
    if row is None:
        raise _project_not_found()
    return row


async def add_project(session, user: Users, body) -> dict:
    await own_profile_locked(session, user.id)
    count, top = (await session.execute(
        select(func.count(ProfileProjects.id), func.max(ProfileProjects.position)).where(ProfileProjects.user_id == user.id)
    )).one()
    if (count or 0) >= MAX_PROJECTS:
        raise CustomHTTPException(422, message=f"A profile has at most {MAX_PROJECTS} projects.", error_code="PROJECT_LIMIT")
    row = ProfileProjects(
        user_id=user.id, title=body.title, summary=rules.one_line(body.summary, 160),
        url=rules.check_url(body.url) if body.url else None, tags=list(body.tags or []),
        role=rules.one_line(body.role, 60), month=body.month, position=(top + 1) if top is not None else 0,
    )
    session.add(row)
    await session.commit()
    return project_out(row)


async def edit_project(session, user: Users, project_id: int, body) -> dict:
    row = await _own_project(session, user, project_id)
    sent = body.model_fields_set
    if "title" in sent:
        row.title = body.title
    if "summary" in sent:
        row.summary = rules.one_line(body.summary, 160)
    if "url" in sent:
        row.url = rules.check_url(body.url) if body.url else None
    if "tags" in sent:
        row.tags = list(body.tags or [])
    if "role" in sent:
        row.role = rules.one_line(body.role, 60)
    if "month" in sent:
        row.month = body.month
    await session.commit()
    return project_out(row)


async def remove_project(session, user_id: int, project_id: int) -> bool:
    """Delete a project and its picture. False when it was not theirs or not there."""
    row = await session.scalar(select(ProfileProjects).where(ProfileProjects.id == project_id, ProfileProjects.user_id == user_id))
    if row is None:
        return False
    image = row.image_key
    await session.delete(row)
    await session.commit()
    if image is not None and hasattr(image, "delete"):
        image.delete()
    return True


async def order_projects(session, user: Users, ids: list[int]) -> dict:
    await own_profile_locked(session, user.id)
    mine = list((await session.scalars(select(ProfileProjects.id).where(ProfileProjects.user_id == user.id))).all())
    if len(ids) != len(set(ids)) or set(ids) != set(mine):
        raise CustomHTTPException(422, message="Send every one of your projects once, in the order you want.", error_code="ORDER_INVALID")
    for position, project_id in enumerate(ids):
        await session.execute(
            update(ProfileProjects).where(ProfileProjects.id == project_id, ProfileProjects.user_id == user.id).values(position=position)
            .execution_options(synchronize_session=False)
        )
    await session.commit()
    return await my_profile(session, user.id)


async def set_project_image(session, user: Users, project_id: int, upload) -> dict:
    from app.core.storage.fields import upload as store_upload

    row = await _own_project(session, user, project_id)
    previous = row.image_key
    # Checked, sized and stored first (storage.fields.upload, off the event
    # loop): anything that is not a picture, or is too large, is refused
    # here, before the row changes.
    row.image_key = await store_upload(ProfileProjects.image_key, {"bytes": io.BytesIO(await upload.read()), "filename": upload.filename})
    await session.commit()
    if previous is not None and hasattr(previous, "delete"):
        previous.delete()
    await session.refresh(row)
    return project_out(row)


async def clear_project_image(session, user: Users, project_id: int) -> dict:
    row = await _own_project(session, user, project_id)
    previous = row.image_key
    row.image_key = None
    await session.commit()
    if previous is not None and hasattr(previous, "delete"):
        previous.delete()
    return project_out(row)


# ─── The QR code ────────────────────────────────────────────────────────


async def live_code(session, user_id: int) -> Optional[ProfileCodes]:
    return await session.scalar(select(ProfileCodes).where(ProfileCodes.user_id == user_id, ProfileCodes.replaced_at.is_(None)))


async def _insert_code(session, user_id: int) -> None:
    """A fresh code for the person; nothing when they already have a live
    one (two first opens at once make one), a new token on the rare clash."""
    for _ in range(5):
        try:
            async with session.begin_nested():
                await session.execute(
                    pg_insert(ProfileCodes).values(user_id=user_id, token=rules.new_code(), created_at=_now())
                    .on_conflict_do_nothing(index_elements=["user_id"], index_where=ProfileCodes.replaced_at.is_(None))
                )
            return
        except IntegrityError:
            continue  # the token was taken: draw another
    raise CustomHTTPException(503, message="We couldn't make your code just now. Try again.", error_code="CODE_UNAVAILABLE")


def code_out(row: ProfileCodes) -> dict:
    return {"code": row.token, "url": code_url(row.token), "created_at": row.created_at}


async def my_code(session, user_id: int) -> dict:
    row = await live_code(session, user_id)
    if row is None:
        await _insert_code(session, user_id)
        await session.commit()
        row = await live_code(session, user_id)
    return code_out(row)


async def replace_code(session, user_id: int) -> dict:
    """A new code; the old one answers 410 from this commit on."""
    await own_profile_locked(session, user_id)
    await session.execute(
        update(ProfileCodes).where(ProfileCodes.user_id == user_id, ProfileCodes.replaced_at.is_(None)).values(replaced_at=_now())
        .execution_options(synchronize_session=False)
    )
    await _insert_code(session, user_id)
    await session.commit()
    return code_out(await live_code(session, user_id))


async def resolve_code(session, raw, viewer_id: Optional[int]) -> tuple[Users, ProfileCodes]:
    """The person behind a scanned code, for this viewer: 404 NOT_FOUND when
    there is none to show them, 410 PROFILE_CODE_INACTIVE for a replaced
    code, 403 PROFILE_SCANNING_OFF when its owner turned scanning off."""
    token = rules.normal_code(raw)
    row = await session.scalar(select(ProfileCodes).where(ProfileCodes.token == token)) if token else None
    if row is None:
        raise access.not_found()
    user = await access.visible_person(session, viewer_id, user_id=row.user_id)
    if row.replaced_at is not None:
        raise CustomHTTPException(410, message="This code was replaced by its owner.", error_code="PROFILE_CODE_INACTIVE")
    if not access.settings_of(user.profile)["scanning_enabled"]:
        raise CustomHTTPException(403, message="This person isn't taking saves by QR code.", error_code="PROFILE_SCANNING_OFF")
    return user, row
