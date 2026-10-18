"""Reports and blocks (app/api/reports/models.py says what each one is)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.clubs.models import Clubs
from app.api.reports.models import ContentReports, UserBlocks
from app.api.threads.models import ClubThreads, ThreadComments
from app.api.users.models import UserProfiles, Users, UserTypes
from app.config import settings
from app.db.listeners import INCLUDE_DELETED
from app.response import CustomHTTPException

# Open reports weighing settings.REPORTS_TO_HIDE (three by default) hide a
# comment from everyone but its author until an admin resolves them. A
# report from an account younger than REPORT_NEW_ACCOUNT_DAYS weighs
# REPORT_NEW_ACCOUNT_WEIGHT (a half), so three accounts made for the purpose
# are not enough; a deleted account's report weighs nothing.
#: Reports one person may make in an hour.
REPORTS_PER_HOUR = 20
RATE_WINDOW = timedelta(hours=1)
PREVIEW_CHARS = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: str | None, limit: int = PREVIEW_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ─── People, as a list shows them ───────────────────────────────────────


async def people(session, ids, include_deleted: bool = False) -> dict[int, Users]:
    """Accounts by id, with what their name and picture need loaded: a
    profile and its avatar, and a club account's club. Deleted accounts only
    when asked for (the admin's queue names who reported, whatever since)."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = await session.scalars(
        select(Users)
        .where(Users.id.in_(ids))
        .options(selectinload(Users.profile).selectinload(UserProfiles.avatar), selectinload(Users.club))
        .execution_options(**{INCLUDE_DELETED: include_deleted})
    )
    return {u.id: u for u in rows.all()}


def display_name(user: Users | None) -> str:
    """The name a comment shows for its writer (threads.service._comment_out),
    so "Block {name}" and the blocked list agree: a club account's club."""
    if user is None:
        return "Someone"
    if user.user_type == UserTypes.club and user.club is not None:
        return user.club.name
    return user.full_name or user.username or "Someone"


def avatar(user: Users | None):
    """The picture a list shows beside a name: a club account's logo, or the
    person's photo, or the avatar they picked. An image's variations by
    name ({"original", "thumbnail", ...}), or null."""
    if user is None:
        return None
    if user.user_type == UserTypes.club and user.club is not None:
        return user.club.logo
    profile = user.profile
    if profile is None:
        return None
    return profile.profile_pic or (profile.avatar.image if profile.avatar is not None else None)


# ─── Blocks ─────────────────────────────────────────────────────────────


async def blocked_by(session, viewer_id: int | None) -> set[int]:
    """Whom this person has blocked."""
    if viewer_id is None:
        return set()
    return set((await session.scalars(select(UserBlocks.blocked_id).where(UserBlocks.blocker_id == viewer_id))).all())


async def block(session, blocker, user_id: int) -> dict:
    if user_id == blocker.id:
        raise CustomHTTPException(422, message="You can't block yourself.", error_code="CANNOT_BLOCK_SELF")
    target = await session.scalar(select(Users.id).where(Users.id == user_id, Users.is_deleted == False))  # noqa: E712
    if target is None:
        raise CustomHTTPException(404, message="User not found", error_code="USER_NOT_FOUND")
    # Blocking twice is blocking once.
    await session.execute(
        pg_insert(UserBlocks).values(blocker_id=blocker.id, blocked_id=user_id).on_conflict_do_nothing(index_elements=["blocker_id", "blocked_id"])
    )
    await session.commit()
    return _block_item(user_id, (await people(session, [user_id])).get(user_id))


async def unblock(session, blocker, user_id: int) -> None:
    await session.execute(delete(UserBlocks).where(UserBlocks.blocker_id == blocker.id, UserBlocks.blocked_id == user_id))
    await session.commit()


async def list_blocks(session, blocker) -> list[dict]:
    rows = (
        await session.execute(
            select(UserBlocks.blocked_id).where(UserBlocks.blocker_id == blocker.id).order_by(UserBlocks.created_at.desc(), UserBlocks.blocked_id)
        )
    ).scalars().all()
    found = await people(session, rows)
    return [_block_item(i, found.get(i)) for i in rows]


def _block_item(user_id: int, user: Users | None) -> dict:
    return {"user_id": user_id, "name": display_name(user), "avatar": avatar(user)}


# ─── What a report is about ─────────────────────────────────────────────


@dataclass
class Target:
    type: str
    #: As stored: a UUID's text, or an id's digits.
    key: str
    author_id: int | None
    club_id: int | None
    preview: str
    removed: bool = False


def _not_found() -> CustomHTTPException:
    return CustomHTTPException(404, message="We couldn't find what you're reporting.", error_code="TARGET_NOT_FOUND")


def normalise_id(target_type: str, target_id) -> str:
    """The stored form of a target's id, or TARGET_NOT_FOUND for one that
    cannot be an id of that type."""
    if target_type in ("thread", "comment"):
        try:
            return str(uuid.UUID(str(target_id)))
        except (ValueError, AttributeError, TypeError):
            raise _not_found()
    if isinstance(target_id, bool):
        raise _not_found()
    if isinstance(target_id, int) or (isinstance(target_id, str) and target_id.strip().isdigit()):
        return str(int(target_id))
    raise _not_found()


async def live_target(session, target_type: str, key: str) -> Target:
    """The thing being reported, as it is now: refused when it is gone."""
    if target_type == "thread":
        t = await session.scalar(select(ClubThreads).where(ClubThreads.id == uuid.UUID(key), ClubThreads.is_deleted == False))  # noqa: E712
        club = await session.get(Clubs, t.club_id) if t is not None else None
        if t is None or club is None or club.is_deleted:
            raise _not_found()
        return Target("thread", key, club.user_id, club.id, _clip(t.body))
    if target_type == "comment":
        c = await session.scalar(select(ThreadComments).where(ThreadComments.id == uuid.UUID(key), ThreadComments.is_deleted == False))  # noqa: E712
        t = await session.scalar(select(ClubThreads).where(ClubThreads.id == c.thread_id, ClubThreads.is_deleted == False)) if c is not None else None  # noqa: E712
        if c is None or t is None:
            raise _not_found()
        return Target("comment", key, c.user_id, t.club_id, _clip(c.body))
    if target_type == "user":
        u = await session.scalar(select(Users).where(Users.id == int(key), Users.is_deleted == False))  # noqa: E712
        if u is None:
            raise _not_found()
        return Target("user", key, u.id, None, u.full_name or u.username or "")
    club = await session.scalar(select(Clubs).where(Clubs.id == int(key), Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise _not_found()
    return Target("club", key, club.user_id, club.id, club.name)


async def _previews(session, rows: list[ContentReports]) -> dict[tuple[str, str], tuple[str, bool]]:
    """Each reported thing's words as they stand, and whether it has been
    removed since -- the admin judges what was reported even after it went."""
    by_type: dict[str, set[str]] = {}
    for r in rows:
        by_type.setdefault(r.target_type, set()).add(r.target_id)
    out: dict[tuple[str, str], tuple[str, bool]] = {}
    seen = {INCLUDE_DELETED: True}

    def uuids(keys):
        return [uuid.UUID(k) for k in keys]

    if by_type.get("thread"):
        for t in (await session.scalars(select(ClubThreads).where(ClubThreads.id.in_(uuids(by_type["thread"]))).execution_options(**seen))).all():
            out[("thread", str(t.id))] = (_clip(t.body), bool(t.is_deleted))
    if by_type.get("comment"):
        for c in (await session.scalars(select(ThreadComments).where(ThreadComments.id.in_(uuids(by_type["comment"]))).execution_options(**seen))).all():
            out[("comment", str(c.id))] = (_clip(c.body), bool(c.is_deleted))
    if by_type.get("user"):
        for u in (await session.scalars(select(Users).where(Users.id.in_([int(k) for k in by_type["user"]])).execution_options(**seen))).all():
            out[("user", str(u.id))] = (u.full_name or u.username or "", bool(u.is_deleted))
    if by_type.get("club"):
        for c in (await session.scalars(select(Clubs).where(Clubs.id.in_([int(k) for k in by_type["club"]])).execution_options(**seen))).all():
            out[("club", str(c.id))] = (c.name, bool(c.is_deleted))
    return out


async def open_counts(session, pairs) -> dict[tuple[str, str], int]:
    """Open reports from different people, per thing."""
    pairs = set(pairs)
    if not pairs:
        return {}
    out: dict[tuple[str, str], int] = {}
    by_type: dict[str, list[str]] = {}
    for kind, key in pairs:
        by_type.setdefault(kind, []).append(key)
    for kind, keys in by_type.items():
        rows = await session.execute(
            select(ContentReports.target_id, func.count(func.distinct(ContentReports.reporter_id)))
            .where(ContentReports.target_type == kind, ContentReports.target_id.in_(keys), ContentReports.status == "open")
            .group_by(ContentReports.target_id)
        )
        for key, n in rows.all():
            out[(kind, key)] = int(n)
    return out


async def open_weights(session, target_type: str, keys) -> dict[str, float]:
    """The weight of the open reports on each thing: each reporter's report
    counts 1, or REPORT_NEW_ACCOUNT_WEIGHT when their account is younger than
    REPORT_NEW_ACCOUNT_DAYS. (One open report per person per thing: the
    unique index uq_content_reports_open.)"""
    keys = list(keys)
    if not keys:
        return {}
    young = _now() - timedelta(days=settings.REPORT_NEW_ACCOUNT_DAYS)
    weight = case((Users.created_at > young, settings.REPORT_NEW_ACCOUNT_WEIGHT), else_=1.0)
    rows = await session.execute(
        select(ContentReports.target_id, func.sum(weight))
        .join(Users, Users.id == ContentReports.reporter_id)
        .where(ContentReports.target_type == target_type, ContentReports.target_id.in_(keys), ContentReports.status == "open")
        .group_by(ContentReports.target_id)
    )
    return {key: float(w) for key, w in rows.all()}


async def hidden_comments(session, comment_ids) -> set:
    """Of these comments, the ones whose open reports weigh
    settings.REPORTS_TO_HIDE or more (open_weights)."""
    keys = {str(i): i for i in comment_ids}
    if not keys:
        return set()
    weights = await open_weights(session, "comment", keys)
    return {keys[k] for k, w in weights.items() if w >= settings.REPORTS_TO_HIDE}


# ─── Reporting ──────────────────────────────────────────────────────────


async def report(session, reporter, *, target_type: str, target_id, reason: str, note: str | None) -> tuple[ContentReports, bool]:
    """The reporter's open report on this thing: made now (True), or the one
    they already had (False)."""
    key = normalise_id(target_type, target_id)
    if target_type == "user" and int(key) == reporter.id:
        raise CustomHTTPException(422, message="You can't report yourself.", error_code="CANNOT_REPORT_SELF")
    target = await live_target(session, target_type, key)

    async def mine() -> ContentReports | None:
        return await session.scalar(
            select(ContentReports).where(
                ContentReports.reporter_id == reporter.id,
                ContentReports.target_type == target_type,
                ContentReports.target_id == key,
                ContentReports.status == "open",
            )
        )

    existing = await mine()
    if existing is not None:
        return existing, False

    since = _now() - RATE_WINDOW
    recent = await session.scalar(
        select(func.count(ContentReports.id)).where(ContentReports.reporter_id == reporter.id, ContentReports.created_at >= since)
    )
    if (recent or 0) >= REPORTS_PER_HOUR:
        oldest = await session.scalar(
            select(func.min(ContentReports.created_at)).where(ContentReports.reporter_id == reporter.id, ContentReports.created_at >= since)
        )
        wait = max(1, int(((oldest or _now()) + RATE_WINDOW - _now()).total_seconds()))
        raise CustomHTTPException(
            429,
            message="You've sent a lot of reports. Try again in a while.",
            error_code="RATE_LIMITED",
            errors={"retry_after": wait},
        )

    row = ContentReports(
        reporter_id=reporter.id,
        target_type=target_type,
        target_id=key,
        reason=reason,
        note=(note or "").strip() or None,
        status="open",
        target_author_id=target.author_id,
        club_id=target.club_id,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # The same tap twice at once: the other request made it.
        existing = await mine()
        if existing is not None:
            return existing, False
        raise

    count = (await open_counts(session, [(target_type, key)])).get((target_type, key), 1)
    from app.api.admin_alerts import service as admin_alerts

    what = {"thread": "A post", "comment": "A comment", "user": "A user", "club": "A club"}[target_type]
    await admin_alerts.notify(
        session,
        "content_reported",
        title=f"{what} was reported for {reason}" + (f" ({count} open reports)" if count > 1 else ""),
        body=target.preview,
        club=target.club_id,
        data={"target_type": target_type, "target_id": key, "reason": reason, "report_count": count},
        # One alert per thing in the window, however many people report it.
        dedupe_on={"target_type": target_type, "target_id": key},
    )
    await session.commit()
    return row, True


# ─── The admin's queue ──────────────────────────────────────────────────


async def list_reports(session, *, status: str | None, target_type: str | None, offset: int, limit: int) -> dict:
    criteria = []
    if status:
        criteria.append(ContentReports.status == status)
    if target_type:
        criteria.append(ContentReports.target_type == target_type)
    total = await session.scalar(select(func.count(ContentReports.id)).where(*criteria))
    rows = list(
        (
            await session.scalars(
                select(ContentReports).where(*criteria).order_by(ContentReports.created_at.desc(), ContentReports.id.desc()).offset(offset).limit(limit)
            )
        ).all()
    )
    return {"items": await items(session, rows), "total": int(total or 0)}


async def items(session, rows: list[ContentReports]) -> list[dict]:
    found = await people(session, [r.reporter_id for r in rows] + [r.target_author_id for r in rows], include_deleted=True)
    club_ids = {r.club_id for r in rows if r.club_id is not None}
    clubs = (
        {c.id: c for c in (await session.scalars(select(Clubs).where(Clubs.id.in_(club_ids)).execution_options(**{INCLUDE_DELETED: True}))).all()}
        if club_ids
        else {}
    )
    previews = await _previews(session, rows)
    counts = await open_counts(session, [(r.target_type, r.target_id) for r in rows])
    out = []
    for r in rows:
        reporter = found.get(r.reporter_id)
        author = found.get(r.target_author_id) if r.target_author_id is not None else None
        club = clubs.get(r.club_id) if r.club_id is not None else None
        preview, removed = previews.get((r.target_type, r.target_id), ("", True))
        out.append(
            {
                "id": r.id,
                "target_type": r.target_type,
                # Always text: a thread's or comment's UUID, a user's or club's id in digits.
                "target_id": r.target_id,
                "reason": r.reason,
                "note": r.note,
                "status": r.status,
                "reporter": {"id": r.reporter_id, "name": display_name(reporter)},
                "target_preview": preview,
                "target_author": {"id": r.target_author_id, "name": display_name(author)} if r.target_author_id is not None else None,
                "club": {"id": club.id, "name": club.name, "slug": club.slug} if club is not None else None,
                "report_count": counts.get((r.target_type, r.target_id), 0),
                "target_removed": removed,
                "created_at": r.created_at,
                "resolved_at": r.resolved_at,
                "resolution": r.resolution,
                "resolution_note": r.resolution_note,
            }
        )
    return out


async def _remove(session, target_type: str, key: str) -> bool:
    """Take the reported thread or comment down, as its club would. True
    when it was there to take down."""
    now = _now()
    if target_type == "comment":
        c = await session.scalar(select(ThreadComments).where(ThreadComments.id == uuid.UUID(key), ThreadComments.is_deleted == False))  # noqa: E712
        if c is None:
            return False
        # The post's comments_count follows by trigger (threads.models.THREAD_COUNTS_SQL).
        c.is_deleted = True
        c.deleted_at = now
        return True
    if target_type == "thread":
        t = await session.scalar(select(ClubThreads).where(ClubThreads.id == uuid.UUID(key), ClubThreads.is_deleted == False))  # noqa: E712
        if t is None:
            return False
        t.is_deleted = True
        t.deleted_at = now
        t.pinned = False
        return True
    return False


async def resolve(session, admin, report_id: int, *, action: str, note: str | None, request=None) -> dict:
    """The admin's decision on one report, applied to every open report on
    the same thing.

    dismiss         nothing is taken down; a hidden comment shows again.
    remove_content  the thread or comment is removed (not for a user or a club).
    suspend_author  its author's account is suspended, as the Users page
                    does (sign-in and every token refused, ACCOUNT_SUSPENDED),
                    and a reported thread or comment is removed too.
    """
    row = await session.scalar(select(ContentReports).where(ContentReports.id == report_id))
    if row is None:
        raise CustomHTTPException(404, message="Report not found", error_code="REPORT_NOT_FOUND")
    if row.status != "open":
        raise CustomHTTPException(409, message="This report is already resolved.", error_code="ALREADY_RESOLVED")
    if action == "remove_content" and row.target_type not in ("thread", "comment"):
        raise CustomHTTPException(
            422, message="Only a thread or a comment can be removed. Suspend the account instead.", error_code="NOT_REMOVABLE"
        )
    author_id = row.target_author_id
    if action == "suspend_author":
        live_author = (
            await session.scalar(select(Users.id).where(Users.id == author_id, Users.is_deleted == False))  # noqa: E712
            if author_id is not None
            else None
        )
        if live_author is None:
            raise CustomHTTPException(422, message="There is no account to suspend for this report.", error_code="NO_AUTHOR")
        if author_id == admin.id:
            raise CustomHTTPException(422, message="You cannot suspend your own account", error_code="CANNOT_SUSPEND_SELF")

    removed = False
    if action in ("remove_content", "suspend_author"):
        removed = await _remove(session, row.target_type, row.target_id)

    note = (note or "").strip() or None
    now = _now()
    result = await session.execute(
        update(ContentReports)
        .where(ContentReports.target_type == row.target_type, ContentReports.target_id == row.target_id, ContentReports.status == "open")
        .values(status="resolved", resolved_at=now, resolved_by=admin.id, resolution=action, resolution_note=note, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    resolved = int(result.rowcount or 0)

    from app.api.audit import service as audit

    await audit.record(
        session,
        actor=admin,
        action="report.resolve",
        entity_type=row.target_type,
        entity_id=row.target_id,
        summary=f"{action.replace('_', ' ').capitalize()}: {resolved} report{'s' if resolved != 1 else ''} on a {row.target_type}",
        details={"report_id": row.id, "action": action, "note": note, "resolved": resolved, "removed": removed, "author_id": author_id},
        request=request,
    )
    if action == "suspend_author":
        from app.api.users import service as users

        # Commits the suspension with everything above.
        await users.set_user_active(session, author_id, is_active=False, reason=f"Reported {row.target_type}: {row.reason}"[:255])
    else:
        await session.commit()

    fresh = await session.scalar(select(ContentReports).where(ContentReports.id == row.id).execution_options(populate_existing=True))
    return {"resolved": resolved, "report": (await items(session, [fresh]))[0]}
