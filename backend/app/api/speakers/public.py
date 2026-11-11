"""Speakers themselves and the public (`/api/v1/speakers`).

- The claim link an admin's invite emails: what it is for (anyone with the
  link), and claiming it (a signed-in app user).
- `/speakers/me`: the claimed profile, edited by its speaker. A club's later
  edits touch only its own event copy.
- `/speakers/{slug}`: the public page of an active profile with a published
  talk (or claimed): its talks at published, listed events only. A merged
  profile answers `{redirect_slug}`; a hidden one is not found.
- Following a speaker, and the list of those followed; a push when one is on
  a newly published event (app/api/speakers/notify.py).
- `/speakers/sitemap`: the pages a search engine should know.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Request, UploadFile
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.events.models import Events, EventSpeakers
from app.api.speakers import notify  # noqa: F401 - registers the push and email jobs
from app.api.speakers import service as sp
from app.api.speakers.admin import token_hash
from app.api.speakers.models import ACTIVE, MERGED, SpeakerClaims, SpeakerFollows, SpeakerProfiles
from app.api.speakers.schemas import ProfilePatch, mask_email
from app.core.auth.dependencies import OptionalUserAuth, UserAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/speakers", tags=["speakers"])

SITEMAP_MAX = 5000
FOLLOWING_MAX = 500
NOT_FOUND = dict(status_code=404, message="That speaker could not be found.", error_code="SPEAKER_NOT_FOUND")


def _app_user(user) -> None:
    value = getattr(user.user_type, "value", user.user_type)
    if value != "app_user":
        raise CustomHTTPException(403, message="Sign in with your MyOtherApp account to do this.", error_code="APP_USER_ONLY")


def _talk_card(t: dict) -> dict:
    return {
        "event_id": t["event_id"], "slug": t["slug"], "name": t["event_name"], "date": sp.iso(t["date"]),
        "ends_at": sp.iso(t["ends_at"]), "club": t["club"], "talk_title": t["talk_title"], "poster": t["poster"],
    }


def split_talks(rows: list[dict]) -> dict:
    """Upcoming (not over yet), soonest first; past, latest first."""
    now = sp.now()
    upcoming = sorted((r for r in rows if r["ends_at"] and r["ends_at"] > now), key=lambda r: r["date"])
    past = [r for r in rows if not (r["ends_at"] and r["ends_at"] > now)]
    return {"upcoming": [_talk_card(r) for r in upcoming], "past": [_talk_card(r) for r in past]}


async def page_of(session, profile: SpeakerProfiles, user=None) -> dict:
    rows = (await sp.talks(session, [profile.id], public_only=True)).get(profile.id, [])
    if not rows and profile.claimed_by_user_id is None:
        raise CustomHTTPException(**NOT_FOUND)
    followers = int(await session.scalar(select(func.count()).select_from(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id)) or 0)
    following = None
    if user is not None:
        following = bool(await session.scalar(
            select(func.count()).select_from(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id, SpeakerFollows.user_id == user.id)
        ))
    catalogue = await sp.topic_catalogue(session, profile.topics or [])
    return {
        "slug": profile.slug, "name": profile.name, "headline": profile.headline, "bio": profile.bio,
        "photo": sp.photo_out(profile.photo), "links": dict(profile.links or {}), "city": profile.city,
        "topics": sp.topics_out(profile.topics, catalogue), "claimed": profile.claimed_by_user_id is not None,
        "followers_count": followers, "following": following, "talks": split_talks(rows),
        "updated_at": sp.iso(profile.updated_at),
    }


async def _public_profile(session, slug: str) -> SpeakerProfiles:
    profile = await session.scalar(select(SpeakerProfiles).where(SpeakerProfiles.slug == slug))
    if profile is None or profile.status != ACTIVE:
        raise CustomHTTPException(**NOT_FOUND)
    return profile


# ─── Claiming ────────────────────────────────────────────────────────────


async def _claim_row(session, token: str, *, lock: bool = False) -> SpeakerClaims:
    q = select(SpeakerClaims).where(SpeakerClaims.token_hash == token_hash(token))
    if lock:
        q = q.with_for_update()
    claim = await session.scalar(q)
    if claim is None:
        raise CustomHTTPException(404, message="This link is not valid. Ask us for a new one.", error_code="CLAIM_INVALID")
    return claim


def _claim_problem(claim: SpeakerClaims, profile: Optional[SpeakerProfiles]) -> Optional[tuple[str, str]]:
    if claim.used_at is not None:
        return "used", "This link was used already."
    if claim.revoked_at is not None:
        return "replaced", "A newer link was sent for this profile. Use that one."
    if claim.expires_at <= sp.now():
        return "expired", "This link has expired. Ask us for a new one."
    if profile is None or profile.status != ACTIVE:
        return "unavailable", "This profile can't be claimed."
    if profile.claimed_by_user_id is not None:
        return "claimed", "This profile was claimed already."
    return None


@router.get("/claim/{token}", summary="What a claim link is for: the profile, and whether it can still be used (public)")
@limiter.limit("30/minute")
async def claim_preview(request: Request, session: SessionDep, token: str) -> dict:
    claim = await _claim_row(session, token)
    profile = await sp.resolve(session, await session.get(SpeakerProfiles, claim.profile_id))
    problem = _claim_problem(claim, profile)
    talks = (await sp.talks(session, [profile.id], public_only=True)).get(profile.id, []) if profile is not None else []
    return {
        "profile": {
            "slug": profile.slug, "name": profile.name, "headline": profile.headline, "photo": sp.photo_out(profile.photo),
            "bio": profile.bio, "talks_count": len(talks),
        } if profile is not None and profile.status == ACTIVE else None,
        "email_masked": mask_email(claim.email), "valid": problem is None, "reason": problem[0] if problem else None,
        "message": problem[1] if problem else None, "expires_at": sp.iso(claim.expires_at),
    }


@router.post("/claim/{token}", summary="Claim a speaker profile with an invite link (signed in)")
@limiter.limit("10/minute")
async def claim(request: Request, session: SessionDep, user: UserAuth, token: str) -> dict:
    _app_user(user)
    claim_row = await _claim_row(session, token, lock=True)
    profile = await session.scalar(
        select(SpeakerProfiles).where(SpeakerProfiles.id == claim_row.profile_id).with_for_update().execution_options(populate_existing=True)
    )
    profile = await sp.resolve(session, profile)
    problem = _claim_problem(claim_row, profile)
    if problem is not None:
        raise CustomHTTPException(409 if problem[0] != "expired" else 410, message=problem[1], error_code=f"CLAIM_{problem[0].upper()}")
    mine = await session.scalar(select(SpeakerProfiles.id).where(SpeakerProfiles.claimed_by_user_id == user.id))
    if mine is not None:
        raise CustomHTTPException(409, message="Your account already has a speaker profile. Ask us to merge the two.", error_code="ALREADY_HAS_PROFILE")
    now = sp.now()
    profile.claimed_by_user_id, profile.claimed_at = user.id, now
    claim_row.used_at, claim_row.used_by = now, user.id
    await sp.record(session, profile.id, "claimed", sp.speaker_actor(user), after={"user_id": user.id, "invite_email": mask_email(claim_row.email)})
    await session.commit()
    return {"claimed": True, "slug": profile.slug}


# ─── The speaker's own profile ───────────────────────────────────────────


async def _mine(session, user) -> SpeakerProfiles:
    profile = await session.scalar(select(SpeakerProfiles).where(SpeakerProfiles.claimed_by_user_id == user.id))
    if profile is None:
        raise CustomHTTPException(404, message="Your account has no speaker profile.", error_code="NO_SPEAKER_PROFILE")
    return profile


async def me_out(session, profile: SpeakerProfiles) -> dict:
    rows = (await sp.talks(session, [profile.id], public_only=True)).get(profile.id, [])
    catalogue = await sp.topic_catalogue(session, profile.topics or [])
    return {
        **sp.summary(profile, catalogue), "claimed_at": sp.iso(profile.claimed_at), "talks": split_talks(rows),
        "followers_count": int(await session.scalar(select(func.count()).select_from(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id)) or 0),
    }


@router.get("/me", summary="The speaker profile this account claimed")
async def get_me(session: SessionDep, user: UserAuth) -> dict:
    return await me_out(session, await _mine(session, user))


@router.patch("/me", summary="Change the profile this account claimed (name, headline, bio, links, city, topics)")
async def patch_me(session: SessionDep, user: UserAuth, body: ProfilePatch) -> dict:
    profile = await _mine(session, user)
    await sp.edit(session, profile, body, sp.speaker_actor(user))
    return await me_out(session, profile)


@router.put("/me/photo", summary="Change the photo of the profile this account claimed")
async def put_my_photo(session: SessionDep, user: UserAuth, photo: UploadFile = File(...)) -> dict:
    profile = await _mine(session, user)
    await sp.set_photo(session, profile, photo, sp.speaker_actor(user))
    return await me_out(session, profile)


# ─── Following ───────────────────────────────────────────────────────────


@router.get("/following", summary="The speakers this account follows")
async def following(session: SessionDep, user: UserAuth) -> list[dict]:
    rows = list((await session.scalars(
        select(SpeakerProfiles).join(SpeakerFollows, SpeakerFollows.profile_id == SpeakerProfiles.id)
        .where(SpeakerFollows.user_id == user.id, SpeakerProfiles.status == ACTIVE)
        .order_by(SpeakerFollows.created_at.desc()).limit(FOLLOWING_MAX)
    )).all())
    talk_rows = await sp.talks(session, [p.id for p in rows], public_only=True)
    now = sp.now()
    return [
        {"slug": p.slug, "name": p.name, "headline": p.headline, "photo": sp.photo_out(p.photo),
         "upcoming_count": sum(1 for t in talk_rows.get(p.id, []) if t["ends_at"] and t["ends_at"] > now)}
        for p in rows
    ]


@router.get("/sitemap", summary="Speaker pages for the sitemap: active, with a published talk (public)")
async def sitemap(request: Request, session: SessionDep) -> dict:
    from app.api.clubs.models import Clubs

    public = and_(*sp.public_event_where(sp.now()))
    has_talk = (
        select(EventSpeakers.id).join(Events, Events.id == EventSpeakers.event_id).join(Clubs, Clubs.id == Events.club_id)
        .where(EventSpeakers.speaker_profile_id == SpeakerProfiles.id, EventSpeakers.is_deleted == False, public)  # noqa: E712
        .exists()
    )
    rows = (await session.execute(
        select(SpeakerProfiles.slug, SpeakerProfiles.updated_at).where(SpeakerProfiles.status == ACTIVE, has_talk)
        .order_by(SpeakerProfiles.id).limit(SITEMAP_MAX)
    )).all()
    return {"speakers": [{"slug": slug, "updated_at": sp.iso(at)} for slug, at in rows]}


@router.get("/{slug}", summary="A speaker's public page: bio, links and talks at published events (public)")
async def speaker_page(session: SessionDep, user: OptionalUserAuth, slug: str) -> dict:
    profile = await session.scalar(select(SpeakerProfiles).where(SpeakerProfiles.slug == slug))
    if profile is not None and profile.status == MERGED:
        survivor = await sp.resolve(session, profile)
        if survivor is not None and survivor.status == ACTIVE:
            return {"redirect_slug": survivor.slug}
    if profile is None or profile.status != ACTIVE:
        raise CustomHTTPException(**NOT_FOUND)
    return await page_of(session, profile, user)


@router.post("/{slug}/follow", summary="Follow a speaker: a push when they are on a newly published event")
async def follow(session: SessionDep, user: UserAuth, slug: str) -> dict:
    _app_user(user)
    profile = await _public_profile(session, slug)
    await page_of(session, profile)  # 404 for a profile with no public page
    await session.execute(
        pg_insert(SpeakerFollows.__table__).values(user_id=user.id, profile_id=profile.id, created_at=sp.now())
        .on_conflict_do_nothing(index_elements=["user_id", "profile_id"])
    )
    await session.commit()
    return await _follow_state(session, profile, user)


@router.delete("/{slug}/follow", summary="Stop following a speaker")
async def unfollow(session: SessionDep, user: UserAuth, slug: str) -> dict:
    profile = await _public_profile(session, slug)
    from sqlalchemy import delete

    await session.execute(delete(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id, SpeakerFollows.user_id == user.id))
    await session.commit()
    return await _follow_state(session, profile, user)


async def _follow_state(session, profile: SpeakerProfiles, user) -> dict:
    count = int(await session.scalar(select(func.count()).select_from(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id)) or 0)
    mine = bool(await session.scalar(
        select(func.count()).select_from(SpeakerFollows).where(SpeakerFollows.profile_id == profile.id, SpeakerFollows.user_id == user.id)
    ))
    return {"slug": profile.slug, "following": mine, "followers_count": count}
