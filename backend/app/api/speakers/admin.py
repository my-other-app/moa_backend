"""The admin portal's Speakers page (`/api/v1/admin/speakers`): every
profile with its talks, clubs and topics, filtered and as a spreadsheet;
one profile with its contacts per club (never a club's notes) and history;
edit, hide, remove on request; the merge queue, merge, dismiss, unmerge;
the invite to claim; and the backfill, run once after the deploy.

Reading is any admin (VIEW); every change is MANAGE_CONTENT.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, File, Query, Request, UploadFile
from sqlalchemy import and_, delete, exists, func, or_, select, update

from app.api.audit import service as audit
from app.api.clubs.models import Clubs
from app.api.events.models import Events, EventInterestsLink, EventSpeakers
from app.api.speakers import backfill  # noqa: F401 - registers the backfill job
from app.api.speakers import merge as merging
from app.api.speakers import notify
from app.api.speakers import service as sp
from app.api.speakers.club import sessions_for, talk_out
from app.api.speakers.models import (
    ACTIVE,
    HIDDEN,
    MERGED,
    OPEN,
    STATUSES,
    SpeakerClaims,
    SpeakerContacts,
    SpeakerFollows,
    SpeakerHistory,
    SpeakerMergeCandidates,
    SpeakerProfiles,
)
from app.api.speakers.schemas import InviteIn, MergeIn, ProfilePatch, mask_email
from app.core import jobs, public_urls
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ContentAuth
from app.core.utils import spreadsheet
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin/speakers", tags=["admin-speakers"])

PAGE_MAX = 200
EXPORT_MAX = 50000
CLAIM_DAYS = 14
BACKFILL_JOB = "speakers.backfill"
SORTS = ("last_talk", "talks", "name", "created")


def _talk_stats():
    return (
        select(
            EventSpeakers.speaker_profile_id.label("pid"),
            func.count(func.distinct(Events.id)).label("talks"),
            func.max(Events.event_datetime).label("last"),
        )
        .join(Events, Events.id == EventSpeakers.event_id)
        .where(EventSpeakers.is_deleted == False, Events.is_deleted == False, EventSpeakers.speaker_profile_id.isnot(None))  # noqa: E712
        .group_by(EventSpeakers.speaker_profile_id)
        .subquery("talk_stats")
    )


def _a_talk(*where):
    return exists(
        select(EventSpeakers.id).join(Events, Events.id == EventSpeakers.event_id)
        .where(EventSpeakers.speaker_profile_id == SpeakerProfiles.id, EventSpeakers.is_deleted == False, Events.is_deleted == False, *where)  # noqa: E712
    )


async def _topic_id(session, topic: Optional[str]) -> Optional[int]:
    from app.api.interests.models import InterestCategory

    if not topic:
        return None
    if topic.isdigit():
        return int(topic)
    found = await session.scalar(select(InterestCategory.id).where(InterestCategory.slug == topic, InterestCategory.is_deleted == False))  # noqa: E712
    return found if found is not None else -1


async def list_profiles(
    session, *, q=None, topic=None, city=None, club_id=None, year=None, status=None, claimed=None,
    sort="last_talk", page=1, page_size=50, every=False,
) -> dict:
    from app.api.interests.models import Interests

    stats = _talk_stats()
    where = []
    where.append(SpeakerProfiles.status == status if status else SpeakerProfiles.status != MERGED)
    if claimed is not None:
        where.append(SpeakerProfiles.claimed_by_user_id.isnot(None) if claimed else SpeakerProfiles.claimed_by_user_id.is_(None))
    if (q or "").strip():
        cond, _key = sp.name_match(SpeakerProfiles.name_key, q)
        where.append(or_(cond, SpeakerProfiles.headline.ilike(f"%{sp.like_escape(q.strip())}%", escape="\\")))
    if (city or "").strip():
        where.append(SpeakerProfiles.city.ilike(f"%{sp.like_escape(city.strip())}%", escape="\\"))
    if club_id is not None:
        where.append(or_(SpeakerProfiles.created_by_club_id == club_id, _a_talk(Events.club_id == club_id)))
    if year is not None:
        where.append(_a_talk(func.extract("year", Events.event_datetime) == year))
    tid = await _topic_id(session, topic)
    if tid is not None:
        in_topic = exists(
            select(EventInterestsLink.id).join(Interests, Interests.id == EventInterestsLink.interest_id)
            .where(EventInterestsLink.event_id == Events.id, EventInterestsLink.is_deleted == False, Interests.category_id == tid)  # noqa: E712
        )
        where.append(or_(SpeakerProfiles.topics.contains([tid]), _a_talk(in_topic)))
    total = int(await session.scalar(select(func.count()).select_from(SpeakerProfiles).where(*where)) or 0)
    order = {
        "talks": (func.coalesce(stats.c.talks, 0).desc(), SpeakerProfiles.name),
        "name": (SpeakerProfiles.name, SpeakerProfiles.id),
        "created": (SpeakerProfiles.created_at.desc(), SpeakerProfiles.id.desc()),
    }.get(sort, (stats.c.last.desc().nulls_last(), SpeakerProfiles.id.desc()))
    query = (
        select(SpeakerProfiles, stats.c.talks, stats.c.last)
        .outerjoin(stats, stats.c.pid == SpeakerProfiles.id).where(*where).order_by(*order)
    )
    query = query.limit(EXPORT_MAX) if every else query.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(query)).all()
    ids = [r[0].id for r in rows]
    clubs: dict[int, list] = {}
    if ids:
        for pid, cid, cname in (await session.execute(
            select(EventSpeakers.speaker_profile_id, Clubs.id, Clubs.name)
            .join(Events, Events.id == EventSpeakers.event_id).join(Clubs, Clubs.id == Events.club_id)
            .where(EventSpeakers.speaker_profile_id.in_(ids), EventSpeakers.is_deleted == False, Events.is_deleted == False)  # noqa: E712
            .distinct().order_by(EventSpeakers.speaker_profile_id, Clubs.name)
        )).all():
            clubs.setdefault(pid, []).append({"id": cid, "name": cname})
    catalogue = await sp.topic_catalogue(session, [t for r in rows for t in (r[0].topics or [])])
    items = [
        {
            "id": p.id, "slug": p.slug, "name": p.name, "headline": p.headline, "photo": sp.photo_out(p.photo), "city": p.city,
            "topics": sp.topics_out(p.topics, catalogue), "talks_count": int(talks or 0), "clubs": clubs.get(p.id, []),
            "last_talk_at": sp.iso(last), "status": p.status, "claimed": p.claimed_by_user_id is not None,
            "links": dict(p.links or {}), "created_at": sp.iso(p.created_at),
        }
        for p, talks, last in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


EXPORT_HEADERS = (
    "Name", "Headline", "City", "Topics", "Talks", "Clubs", "Last talk", "Status", "Claimed",
    "Website", "LinkedIn", "X", "GitHub", "Speaker page", "Added",
)


def export_rows(items):
    for i in items:
        links = i.get("links") or {}
        yield (
            i["name"], i.get("headline"), i.get("city"), ", ".join(t["name"] for t in i.get("topics") or []),
            i.get("talks_count", 0), ", ".join(c["name"] for c in i.get("clubs") or []), (i.get("last_talk_at") or "")[:10] or None,
            i.get("status"), bool(i.get("claimed")), links.get("website"), links.get("linkedin"), links.get("x"), links.get("github"),
            public_urls.site_url(f"/speakers/{i['slug']}") if i.get("status") == ACTIVE else None, (i.get("created_at") or "")[:10] or None,
        )


async def _profile(session, profile_id: int) -> SpeakerProfiles:
    profile = await session.get(SpeakerProfiles, profile_id)
    if profile is None:
        raise CustomHTTPException(404, message="That speaker could not be found.", error_code="SPEAKER_NOT_FOUND")
    return profile


async def detail(session, profile: SpeakerProfiles) -> dict:
    from app.api.users.models import Users

    rows = (await sp.talks(session, [profile.id])).get(profile.id, [])
    sessions = await sessions_for(session, [t["event_speaker_id"] for t in rows])
    contacts = (await session.execute(
        select(SpeakerContacts, Clubs.name).join(Clubs, Clubs.id == SpeakerContacts.club_id)
        .where(SpeakerContacts.profile_id == profile.id).order_by(Clubs.name)
    )).all()
    history = list((await session.scalars(
        select(SpeakerHistory).where(SpeakerHistory.profile_id == profile.id)
        .order_by(SpeakerHistory.created_at.desc(), SpeakerHistory.id.desc()).limit(200)
    )).all())
    merged_from = list((await session.scalars(
        select(SpeakerProfiles).where(SpeakerProfiles.merged_into_id == profile.id, SpeakerProfiles.status == MERGED).order_by(SpeakerProfiles.id)
    )).all())
    merged_into = await session.get(SpeakerProfiles, profile.merged_into_id) if profile.merged_into_id else None
    made_by = await session.get(Clubs, profile.created_by_club_id) if profile.created_by_club_id else None
    claimer = await session.get(Users, profile.claimed_by_user_id) if profile.claimed_by_user_id else None
    invites = list((await session.scalars(
        select(SpeakerClaims).where(SpeakerClaims.profile_id == profile.id).order_by(SpeakerClaims.created_at.desc()).limit(20)
    )).all())
    followers = int(await session.scalar(select(func.count()).select_from(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id)) or 0)
    catalogue = await sp.topic_catalogue(session, profile.topics or [])
    return {
        **sp.summary(profile, catalogue),
        "talks": [talk_out(t, sessions) for t in rows], "talks_count": len(rows),
        # Each club's email and phone; a club's notes are its own (not shown here).
        "contacts": [
            {"club": {"id": c.club_id, "name": name}, "email": c.email, "phone": c.phone, "consent_at": sp.iso(c.consent_at),
             "consent_by": c.consent_by_name, "updated_at": sp.iso(c.updated_at)}
            for c, name in contacts
        ],
        "history": [
            {"id": h.id, "action": h.action, "actor_kind": h.actor_kind, "actor_name": h.actor_name, "club_id": h.actor_club_id,
             "before": h.before, "after": h.after, "created_at": sp.iso(h.created_at)}
            for h in history
        ],
        "merged_from": [{"id": m.id, "slug": m.slug, "name": m.name} for m in merged_from],
        "merged_into": {"id": merged_into.id, "slug": merged_into.slug, "name": merged_into.name} if merged_into else None,
        "created_by_club": {"id": made_by.id, "name": made_by.name} if made_by else None,
        "claimed_by": {"user_id": claimer.id, "name": claimer.full_name, "email": claimer.email} if claimer else None,
        "claimed_at": sp.iso(profile.claimed_at), "removed_at": sp.iso(profile.removed_at),
        "followers_count": followers, "created_at": sp.iso(profile.created_at),
        "invites": [
            {"email_masked": mask_email(i.email), "created_at": sp.iso(i.created_at), "expires_at": sp.iso(i.expires_at),
             "used_at": sp.iso(i.used_at), "revoked_at": sp.iso(i.revoked_at)}
            for i in invites
        ],
        "page_url": public_urls.site_url(f"/speakers/{profile.slug}") if profile.status == ACTIVE else None,
    }


async def duplicates(session, status: str, limit: int = 200) -> list[dict]:
    a, b = SpeakerProfiles.__table__.alias("a"), SpeakerProfiles.__table__.alias("b")
    where = [SpeakerMergeCandidates.status == status]
    if status == OPEN:
        # A pair one of which was merged since (into someone else) is no longer a question.
        where += [a.c.status != MERGED, b.c.status != MERGED]
    rows = (await session.execute(
        select(SpeakerMergeCandidates).join(a, a.c.id == SpeakerMergeCandidates.a_id).join(b, b.c.id == SpeakerMergeCandidates.b_id)
        .where(*where).order_by(SpeakerMergeCandidates.score.desc(), SpeakerMergeCandidates.id).limit(limit)
    )).scalars().all()
    ids = sorted({i for r in rows for i in (r.a_id, r.b_id)})
    profiles = {p.id: p for p in (await session.scalars(select(SpeakerProfiles).where(SpeakerProfiles.id.in_(ids)))).all()} if ids else {}
    talk_rows = await sp.talks(session, ids)
    catalogue = await sp.topic_catalogue(session, [t for p in profiles.values() for t in (p.topics or [])])

    def side(pid):
        p = profiles[pid]
        t = talk_rows.get(pid, [])
        clubs = []
        for row in t:
            if row["club"] not in clubs:
                clubs.append(row["club"])
        return {**sp.summary(p, catalogue), "talks_count": len(t), "last_talk": sp.last_talk(t), "clubs": clubs,
                "talks": [{"event_id": x["event_id"], "event_name": x["event_name"], "date": sp.iso(x["date"]), "club": x["club"]} for x in t[:5]]}

    return [
        {"id": r.id, "a": side(r.a_id), "b": side(r.b_id), "reason": r.reason, "score": r.score, "status": r.status,
         "decided_at": sp.iso(r.decided_at)}
        for r in rows if r.a_id in profiles and r.b_id in profiles
    ]


async def set_status(session, profile: SpeakerProfiles, status: str, actor: sp.Actor) -> dict:
    if profile.status == MERGED:
        raise CustomHTTPException(409, message="This speaker was merged into another. Unmerge it first.", error_code="ALREADY_MERGED")
    if profile.status != status:
        before = profile.status
        profile.status = status
        if status == ACTIVE:
            profile.removed_at = None
        await sp.record(session, profile.id, "hidden" if status == HIDDEN else "unhidden", actor, before={"status": before}, after={"status": status})
    await session.commit()
    return {"id": profile.id, "status": profile.status}


async def remove_on_request(session, profile: SpeakerProfiles, actor: sp.Actor) -> dict:
    """Hidden at once, every club's contact deleted, its followers and any
    open invite gone. The event pages keep the name each club published."""
    if profile.status == MERGED:
        raise CustomHTTPException(409, message="This speaker was merged into another: remove that one.", error_code="ALREADY_MERGED")
    contacts = (await session.execute(delete(SpeakerContacts).where(SpeakerContacts.profile_id == profile.id))).rowcount
    follows = (await session.execute(delete(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id))).rowcount
    await session.execute(
        update(SpeakerClaims).where(SpeakerClaims.profile_id == profile.id, SpeakerClaims.used_at.is_(None), SpeakerClaims.revoked_at.is_(None))
        .values(revoked_at=sp.now())
    )
    before = profile.status
    profile.status, profile.removed_at = HIDDEN, sp.now()
    await sp.record(session, profile.id, "removed_on_request", actor, before={"status": before},
                    after={"status": HIDDEN, "contacts_deleted": int(contacts or 0), "followers_removed": int(follows or 0)})
    await session.commit()
    return {"id": profile.id, "status": profile.status, "contacts_deleted": int(contacts or 0), "followers_removed": int(follows or 0)}


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


async def invite(session, profile: SpeakerProfiles, email: str, actor: sp.Actor) -> dict:
    if profile.status != ACTIVE:
        raise CustomHTTPException(409, message="Only an active profile can be claimed.", error_code="SPEAKER_NOT_ACTIVE")
    if profile.claimed_by_user_id is not None:
        raise CustomHTTPException(409, message="This profile was claimed already.", error_code="ALREADY_CLAIMED")
    now = sp.now()
    # A newer invite replaces the older ones.
    await session.execute(
        update(SpeakerClaims).where(SpeakerClaims.profile_id == profile.id, SpeakerClaims.used_at.is_(None), SpeakerClaims.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    claim = SpeakerClaims(
        profile_id=profile.id, token_hash=token_hash(token), email=email, expires_at=now + timedelta(days=CLAIM_DAYS),
        invited_by=actor.user_id, created_at=now,
    )
    session.add(claim)
    await session.flush()
    talks = len((await sp.talks(session, [profile.id], public_only=True)).get(profile.id, []))
    await jobs.enqueue(session, notify.CLAIM_EMAIL_JOB, {
        "to": email, "name": profile.name, "link": public_urls.site_url(f"/speakers/claim/{token}"), "days": CLAIM_DAYS,
        "talks": talks, "idempotency_key": f"speaker-claim-{claim.id}",
    })
    await sp.record(session, profile.id, "invited", actor, after={"email": mask_email(email), "expires_at": sp.iso(claim.expires_at)})
    await session.commit()
    return {"sent": True, "email_masked": mask_email(email), "expires_at": sp.iso(claim.expires_at)}


# ─── Routes ──────────────────────────────────────────────────────────────


@router.get("", summary="Every speaker profile, filtered (or as a spreadsheet)")
async def list_speakers(
    session: SessionDep, user: AdminAuth,
    q: Optional[str] = Query(None, max_length=100), topic: Optional[str] = Query(None, max_length=80, description="A topic's id or slug"),
    city: Optional[str] = Query(None, max_length=100), club_id: Optional[int] = Query(None), year: Optional[int] = Query(None, ge=2000, le=2100),
    status: Optional[Literal["active", "hidden", "merged"]] = Query(None, description="Left out: active and hidden"),
    claimed: Optional[bool] = Query(None), sort: Literal["last_talk", "talks", "name", "created"] = Query("last_talk"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=PAGE_MAX),
    fmt: Literal["json", "xlsx"] = Query("json", alias="format", description="json (the default) or xlsx"),
):
    args = dict(q=q, topic=topic, city=city, club_id=club_id, year=year, status=status, claimed=claimed, sort=sort)
    if fmt == "xlsx":
        data = await list_profiles(session, **args, every=True)
        path = await spreadsheet.write_xlsx(export_rows(data["items"]), title="Speakers", headers=EXPORT_HEADERS)
        return spreadsheet.xlsx_response(path, "speakers.xlsx")
    return await list_profiles(session, **args, page=page, page_size=page_size)


@router.get("/duplicates", summary="The merge queue: pairs that may be one person")
async def list_duplicates(
    session: SessionDep, user: AdminAuth, status: Literal["open", "merged", "dismissed"] = Query("open"),
) -> list[dict]:
    return await duplicates(session, status)


@router.post("/merge", summary="Merge one profile into another (undone with unmerge)")
async def merge_profiles(request: Request, session: SessionDep, user: ContentAuth, body: MergeIn) -> dict:
    done = await merging.merge(session, keep_id=body.keep_id, merge_id=body.merge_id, actor=sp.admin_actor(user))
    await audit.record(session, actor=user, action="speaker.merge", entity_type="speaker_profile", entity_id=body.keep_id,
                       summary=f"Merged speaker #{body.merge_id} into #{body.keep_id}", details=done, request=request)
    await session.commit()
    return done


@router.post("/duplicates/{candidate_id}/dismiss", summary="Two different people: the pair leaves the queue")
async def dismiss_duplicate(request: Request, session: SessionDep, user: ContentAuth, candidate_id: int) -> dict:
    done = await merging.dismiss(session, candidate_id, sp.admin_actor(user))
    await audit.record(session, actor=user, action="speaker.dismiss_duplicate", entity_type="speaker_merge_candidate", entity_id=candidate_id,
                       summary=f"Dismissed speaker pair #{candidate_id}", request=request)
    await session.commit()
    return done


@router.post("/backfill", summary="Link every event speaker to a profile: run once after the deploy (safe to run again)")
async def run_backfill(request: Request, session: SessionDep, user: ContentAuth) -> dict:
    await jobs.enqueue(session, BACKFILL_JOB, {"by": user.id}, dedupe_key="speakers:backfill")
    await audit.record(session, actor=user, action="speaker.backfill", entity_type="speaker_profile", summary="Queued the speakers backfill", request=request)
    await session.commit()
    return {"queued": True, "job": BACKFILL_JOB}


@router.get("/{profile_id}", summary="One profile: talks, contacts per club, history, merges")
async def get_speaker(session: SessionDep, user: AdminAuth, profile_id: int) -> dict:
    return await detail(session, await _profile(session, profile_id))


@router.patch("/{profile_id}", summary="Change a profile (its event copies stay as the clubs published them)")
async def edit_speaker(request: Request, session: SessionDep, user: ContentAuth, profile_id: int, body: ProfilePatch) -> dict:
    profile = await _profile(session, profile_id)
    changed = await sp.edit(session, profile, body, sp.admin_actor(user))
    if changed:
        await audit.record(session, actor=user, action="speaker.edit", entity_type="speaker_profile", entity_id=profile.id,
                           summary=f"Edited speaker {profile.name}"[:255], details={"fields": sorted(changed)}, request=request)
        await session.commit()
    return await detail(session, profile)


@router.put("/{profile_id}/photo", summary="Change a profile's photo")
async def speaker_photo(request: Request, session: SessionDep, user: ContentAuth, profile_id: int, photo: UploadFile = File(...)) -> dict:
    profile = await _profile(session, profile_id)
    await sp.set_photo(session, profile, photo, sp.admin_actor(user))
    await audit.record(session, actor=user, action="speaker.photo", entity_type="speaker_profile", entity_id=profile.id,
                       summary=f"Changed the photo of speaker {profile.name}"[:255], request=request)
    await session.commit()
    return {"id": profile.id, "photo": sp.photo_out(profile.photo)}


async def _status_route(request, session, user, profile_id: int, status: str, action: str) -> dict:
    profile = await _profile(session, profile_id)
    done = await set_status(session, profile, status, sp.admin_actor(user))
    await audit.record(session, actor=user, action=action, entity_type="speaker_profile", entity_id=profile.id,
                       summary=f"{action.split('.')[-1].title()} speaker {profile.name}"[:255], request=request)
    await session.commit()
    return done


@router.post("/{profile_id}/hide", summary="Hide a profile: not suggested, no public page; event pages keep it")
async def hide(request: Request, session: SessionDep, user: ContentAuth, profile_id: int) -> dict:
    return await _status_route(request, session, user, profile_id, HIDDEN, "speaker.hide")


@router.post("/{profile_id}/unhide", summary="Show a hidden profile again")
async def unhide(request: Request, session: SessionDep, user: ContentAuth, profile_id: int) -> dict:
    return await _status_route(request, session, user, profile_id, ACTIVE, "speaker.unhide")


@router.post("/{profile_id}/remove-on-request", summary="The speaker asked to be removed: hide it and delete every club's contact")
async def remove(request: Request, session: SessionDep, user: ContentAuth, profile_id: int) -> dict:
    profile = await _profile(session, profile_id)
    done = await remove_on_request(session, profile, sp.admin_actor(user))
    await audit.record(session, actor=user, action="speaker.remove_on_request", entity_type="speaker_profile", entity_id=profile.id,
                       summary=f"Removed speaker {profile.name} on request"[:255], details=done, request=request)
    await session.commit()
    return done


@router.post("/{profile_id}/unmerge", summary="Take a merge back: the profile and its talks on their own again")
async def unmerge(request: Request, session: SessionDep, user: ContentAuth, profile_id: int) -> dict:
    done = await merging.unmerge(session, profile_id=profile_id, actor=sp.admin_actor(user))
    await audit.record(session, actor=user, action="speaker.unmerge", entity_type="speaker_profile", entity_id=profile_id,
                       summary=f"Unmerged speaker #{profile_id}", details=done, request=request)
    await session.commit()
    return done


@router.post("/{profile_id}/invite", summary="Email the speaker a link to claim their profile")
async def invite_speaker(request: Request, session: SessionDep, user: ContentAuth, profile_id: int, body: InviteIn) -> dict:
    profile = await _profile(session, profile_id)
    done = await invite(session, profile, body.email, sp.admin_actor(user))
    await audit.record(session, actor=user, action="speaker.invite", entity_type="speaker_profile", entity_id=profile.id,
                       summary=f"Invited {mask_email(body.email)} to claim speaker {profile.name}"[:255], request=request)
    await session.commit()
    return done

