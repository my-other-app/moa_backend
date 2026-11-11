"""The speakers directory's shared pieces: who acted, the history line,
profiles made and found, what a club may see and pick, and a profile's talks.

Who sees what (docs/SPEAKERS.md):

- A club's own speakers: profiles linked to a speaker row of one of the
  club's events (any status), or that the club's form made. A club is
  suggested only these, plus claimed public profiles by name search.
- Contacts (email, phone, notes) are the entering club's; admins see email
  and phone, never the notes; no other club sees any of it.
- The public sees active profiles with a published talk (or claimed), and
  only the talks of published, listed events.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import sqlalchemy as sa
from sqlalchemy import and_, func, or_, select, union

from app.api.events.models import Events, EventSpeakers, ends_at_sql
from app.api.speakers import names
from app.api.speakers.models import (
    ACTIVE,
    HIDDEN,
    MERGED,
    OPEN,
    SpeakerHistory,
    SpeakerMergeCandidates,
    SpeakerProfiles,
)
from app.core.storage.fields import stored_key
from app.response import CustomHTTPException

#: How alike two name keys must be (pg_trgm similarity) for the picker to offer one.
SEARCH_SIMILARITY = 0.3
#: A merge candidate between two profiles with the same name at different clubs.
SAME_NAME_OTHER_CLUB = "same_name_other_club"
#: The same link under two names that are not plainly one name.
SAME_LINK_OTHER_NAME = "same_link_other_name"


def now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Who acted ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    kind: str  # admin, club, speaker, system
    user_id: Optional[int] = None
    club_id: Optional[int] = None
    name: Optional[str] = None


SYSTEM = Actor("system", name="MyOtherApp")


def admin_actor(user) -> Actor:
    return Actor("admin", user_id=user.id, name=(user.full_name or user.email or "An admin")[:120])


def club_actor(user, club_id: Optional[int], request=None) -> Actor:
    """The club's account, or the team member behind the request."""
    from app.api.events.access import is_admin_user

    if user is not None and getattr(user, "user_type", None) is not None and is_admin_user(user):
        return admin_actor(user)
    member = None
    if request is not None:
        from app.api.club_access import service as club_access

        member = club_access.current_member(request)
    who = (getattr(member, "full_name", None) or getattr(member, "email", None)) if member else None
    who = who or getattr(user, "full_name", None) or getattr(user, "email", None)
    return Actor("club", user_id=getattr(user, "id", None), club_id=club_id, name=(who or "The club")[:120])


def speaker_actor(user) -> Actor:
    return Actor("speaker", user_id=user.id, name=(user.full_name or user.email or "The speaker")[:120])


async def record(session, profile_id: int, action: str, actor: Actor, *, before: Optional[dict] = None, after: Optional[dict] = None) -> None:
    session.add(SpeakerHistory(
        profile_id=profile_id, action=action[:40], actor_kind=actor.kind, actor_user_id=actor.user_id,
        actor_club_id=actor.club_id, actor_name=actor.name, before=before, after=after, created_at=now(),
    ))


# ─── Shapes ──────────────────────────────────────────────────────────────


def photo_out(photo) -> Optional[dict]:
    if not isinstance(photo, dict) or not photo:
        return None
    original = photo.get("original")
    return {"thumbnail": photo.get("thumbnail") or original, "medium": photo.get("medium") or original}


def photo_key(photo) -> Optional[str]:
    return stored_key(photo)


async def topic_catalogue(session, ids: Iterable[int]) -> dict[int, dict]:
    from app.api.interests.models import InterestCategory

    wanted = sorted({int(i) for i in ids or () if i is not None})
    if not wanted:
        return {}
    rows = (await session.execute(
        select(InterestCategory.id, InterestCategory.name, InterestCategory.slug)
        .where(InterestCategory.id.in_(wanted), InterestCategory.is_deleted == False)  # noqa: E712
    )).all()
    return {r.id: {"id": r.id, "name": r.name, "slug": r.slug} for r in rows}


def topics_out(ids, catalogue: dict) -> list[dict]:
    return [catalogue[i] for i in (ids or []) if i in catalogue]


async def check_topics(session, ids: Optional[list]) -> list[int]:
    """Topic ids from the topic tree, each once, in the order given; 422 for one that is not there."""
    out: list[int] = []
    for i in ids or []:
        if i not in out:
            out.append(int(i))
    if not out:
        return []
    known = await topic_catalogue(session, out)
    missing = [i for i in out if i not in known]
    if missing:
        raise CustomHTTPException(422, message="Pick topics from the topic list.", error_code="UNKNOWN_TOPIC", errors={"topics": missing})
    return out


def summary(p: SpeakerProfiles, catalogue: Optional[dict] = None) -> dict:
    """What every list shows of a profile."""
    return {
        "id": p.id, "profile_id": p.id, "slug": p.slug, "name": p.name, "headline": p.headline,
        "photo": photo_out(p.photo), "bio": p.bio, "links": dict(p.links or {}), "city": p.city,
        "topics": topics_out(p.topics, catalogue or {}), "status": p.status, "claimed": p.claimed_by_user_id is not None,
    }


# ─── Making and finding profiles ─────────────────────────────────────────


async def new_slug(session, name: str) -> str:
    """The profile's address: its name, and a few letters more when taken."""
    base = names.slug_base(name)
    taken = set((await session.scalars(
        select(SpeakerProfiles.slug).where(or_(SpeakerProfiles.slug == base, SpeakerProfiles.slug.like(f"{base}-%")))
    )).all())
    if base not in taken:
        return base
    for _ in range(20):
        candidate = f"{base}-{secrets.token_hex(2)}"
        if candidate not in taken:
            return candidate
    return f"{base}-{secrets.token_hex(4)}"


async def create_profile(
    session, *, name: str, headline: Optional[str] = None, bio: Optional[str] = None, photo=None,
    links: Optional[dict] = None, city: Optional[str] = None, topics: Optional[list] = None,
    club_id: Optional[int] = None, actor: Actor = SYSTEM, source: Optional[dict] = None,
) -> SpeakerProfiles:
    """A new profile, flushed (it has its id), with its first history line."""
    name = (name or "").strip()[:100] or "Speaker"
    profile = SpeakerProfiles(
        slug=await new_slug(session, name), name=name, name_key=names.name_key(name) or name.lower()[:120],
        headline=(headline or None) and headline[:200], bio=(bio or None) and bio[:600],
        photo=photo_key(photo), links=dict(links or {}), city=(city or None) and city[:100], topics=list(topics or []),
        status=ACTIVE, created_by_club_id=club_id,
    )
    session.add(profile)
    await session.flush()
    await record(session, profile.id, "created", actor, after={"name": profile.name, "club_id": club_id, **(source or {})})
    return profile


async def resolve(session, profile: Optional[SpeakerProfiles]) -> Optional[SpeakerProfiles]:
    """A merged profile's survivor (followed along a chain of merges)."""
    seen = set()
    while profile is not None and profile.status == MERGED and profile.merged_into_id and profile.id not in seen:
        seen.add(profile.id)
        profile = await session.get(SpeakerProfiles, profile.merged_into_id)
    return profile


def own_ids(club_id: int):
    """The club's own speakers: on one of its events (any status), or made by its form."""
    talks = (
        select(EventSpeakers.speaker_profile_id.label("id"))
        .join(Events, Events.id == EventSpeakers.event_id)
        .where(
            Events.club_id == club_id, Events.is_deleted == False,  # noqa: E712
            EventSpeakers.is_deleted == False, EventSpeakers.speaker_profile_id.isnot(None),  # noqa: E712
        )
    )
    made = select(SpeakerProfiles.id.label("id")).where(SpeakerProfiles.created_by_club_id == club_id)
    return union(talks, made)


async def is_own(session, club_id: int, profile_id: int) -> bool:
    ids = own_ids(club_id).subquery()
    return bool(await session.scalar(select(func.count()).select_from(ids).where(ids.c.id == profile_id)))


async def visible_to_club(session, club_id: int, profile_id: int) -> SpeakerProfiles:
    """A profile this club may open: one of its own, or a claimed public one. 404 otherwise."""
    profile = await resolve(session, await session.get(SpeakerProfiles, profile_id))
    if profile is None:
        raise CustomHTTPException(404, message="That speaker could not be found.", error_code="SPEAKER_NOT_FOUND")
    if await is_own(session, club_id, profile.id):
        return profile
    if profile.status == ACTIVE and profile.claimed_by_user_id is not None:
        return profile
    raise CustomHTTPException(404, message="That speaker could not be found.", error_code="SPEAKER_NOT_FOUND")


async def pickable(session, club_id: int, profile_id: int) -> SpeakerProfiles:
    """A profile this club may put on an event: one of its own, or a claimed
    one; active (a merged one is its survivor). 422 otherwise."""
    profile = await resolve(session, await session.get(SpeakerProfiles, int(profile_id)))
    refuse = CustomHTTPException(
        422, message="That speaker can't be added from the directory. Add them by name instead.",
        error_code="SPEAKER_NOT_AVAILABLE", errors={"speaker_profile_id": int(profile_id)},
    )
    if profile is None or profile.status != ACTIVE:
        raise refuse
    if profile.claimed_by_user_id is not None or await is_own(session, club_id, profile.id):
        return profile
    raise refuse


async def club_exact_match(session, club_id: int, key: str) -> Optional[SpeakerProfiles]:
    """The club's own active profile with exactly this name key, the one it used last."""
    if not key:
        return None
    ids = own_ids(club_id).subquery()
    last_use = (
        select(func.max(EventSpeakers.id)).where(EventSpeakers.speaker_profile_id == SpeakerProfiles.id)
        .correlate(SpeakerProfiles).scalar_subquery()
    )
    return await session.scalar(
        select(SpeakerProfiles)
        .where(SpeakerProfiles.id.in_(select(ids.c.id)), SpeakerProfiles.name_key == key, SpeakerProfiles.status == ACTIVE)
        .order_by(last_use.desc().nulls_last(), SpeakerProfiles.id.desc())
        .limit(1)
    )


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


async def add_candidate(session, a: int, b: int, reason: str, score: float) -> bool:
    """A possible duplicate for the admins' queue, once a pair."""
    if a == b:
        return False
    lo, hi = _pair(int(a), int(b))
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    done = await session.execute(
        pg_insert(SpeakerMergeCandidates.__table__)
        .values(a_id=lo, b_id=hi, reason=reason, score=float(score), status=OPEN, created_at=now(), updated_at=now())
        .on_conflict_do_nothing(index_elements=["a_id", "b_id"])
        .returning(SpeakerMergeCandidates.__table__.c.id)
    )
    return done.scalar_one_or_none() is not None


async def candidates_for_new(session, profile: SpeakerProfiles) -> int:
    """Profiles of other clubs with the same name: possible duplicates of a new one."""
    others = (await session.scalars(
        select(SpeakerProfiles.id).where(
            SpeakerProfiles.name_key == profile.name_key, SpeakerProfiles.id != profile.id,
            SpeakerProfiles.status != MERGED,
            or_(SpeakerProfiles.created_by_club_id.is_(None), SpeakerProfiles.created_by_club_id != profile.created_by_club_id)
            if profile.created_by_club_id is not None else sa.true(),
        ).limit(20)
    )).all()
    n = 0
    for other in others:
        n += int(await add_candidate(session, profile.id, other, SAME_NAME_OTHER_CLUB, 0.8))
    return n


# ─── Talks ───────────────────────────────────────────────────────────────


def public_event_where(now_: datetime) -> list:
    """An event the public may see (discover's rule), its club joined."""
    from app.api.discover.service import _public_where

    return _public_where(now_)


async def talks(
    session, profile_ids: Iterable[int], *, club_id: Optional[int] = None, public_only: bool = False,
    club_or_public: Optional[int] = None,
) -> dict[int, list[dict]]:
    """Each profile's talks, newest first: its event rows on events not
    deleted. `club_id`: at that club's events only. `public_only`: published,
    listed events only. `club_or_public`: that club's events, and other
    clubs' public ones (what a club may see of a speaker)."""
    from app.api.clubs.models import Clubs

    ids = sorted({int(i) for i in profile_ids if i is not None})
    if not ids:
        return {}
    t = now()
    public = and_(*public_event_where(t))
    q = (
        select(
            # The event as an entity: a poster column on its own carries its
            # type, sizes and all, into the statement's cache key, which cannot hash it.
            EventSpeakers.speaker_profile_id, EventSpeakers.id.label("event_speaker_id"), EventSpeakers.talk_title, Events,
            ends_at_sql().label("ends_at"), Clubs.id.label("club_id"), Clubs.name.label("club_name"), Clubs.slug.label("club_slug"),
            public.label("public"),
        )
        .join(Events, Events.id == EventSpeakers.event_id)
        .join(Clubs, Clubs.id == Events.club_id)
        .where(EventSpeakers.speaker_profile_id.in_(ids), EventSpeakers.is_deleted == False, Events.is_deleted == False)  # noqa: E712
        .order_by(Events.event_datetime.desc(), EventSpeakers.id)
    )
    if club_id is not None:
        q = q.where(Events.club_id == club_id)
    if public_only:
        q = q.where(public)
    if club_or_public is not None:
        q = q.where(or_(Events.club_id == club_or_public, public))
    out: dict[int, list[dict]] = {i: [] for i in ids}
    seen: set = set()
    for r in (await session.execute(q)).all():
        # One talk an event, however many rows name them there.
        e = r.Events
        if (r.speaker_profile_id, e.id) in seen:
            continue
        seen.add((r.speaker_profile_id, e.id))
        out[r.speaker_profile_id].append({
            "event_id": e.id, "event_name": e.name, "slug": e.slug, "date": e.event_datetime,
            "ends_at": r.ends_at, "talk_title": r.talk_title, "event_speaker_id": r.event_speaker_id,
            "status": getattr(e.lifecycle, "value", e.lifecycle) or "published", "public": bool(r.public),
            "poster": photo_poster(e.poster),
            "club": {"id": r.club_id, "name": r.club_name, "slug": r.club_slug},
        })
    return out


def photo_poster(poster) -> Optional[str]:
    if not isinstance(poster, dict):
        return None
    for k in ("medium", "large", "original", "thumbnail"):
        if poster.get(k):
            return poster[k]
    return None


def iso(at) -> Optional[str]:
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.isoformat()


def last_talk(rows: list[dict]) -> Optional[dict]:
    if not rows:
        return None
    r = rows[0]
    return {"event_id": r["event_id"], "event_name": r["event_name"], "date": iso(r["date"])}


async def is_public(session, profile: SpeakerProfiles) -> bool:
    """Whether the profile has a public page: active, and claimed or with a published talk."""
    if profile.status != ACTIVE:
        return False
    if profile.claimed_by_user_id is not None:
        return True
    return bool((await talks(session, [profile.id], public_only=True)).get(profile.id))


async def slugs_for(session, profile_ids: Iterable[int]) -> dict[int, str]:
    """The public address of each active profile (none for hidden or merged ones)."""
    ids = sorted({int(i) for i in profile_ids if i is not None})
    if not ids:
        return {}
    rows = (await session.execute(
        select(SpeakerProfiles.id, SpeakerProfiles.slug).where(SpeakerProfiles.id.in_(ids), SpeakerProfiles.status == ACTIVE)
    )).all()
    return {r.id: r.slug for r in rows}


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_match(column, q: str):
    """The name search: the words anywhere in the name, or a near spelling (pg_trgm `%`)."""
    key = names.search_key(q)
    return or_(column.like(f"%{like_escape(key)}%", escape="\\"), column.op("%")(key)), key


# ─── Editing a profile (admins, and the speaker once claimed) ────────────


async def edit(session, profile: SpeakerProfiles, body, actor: Actor) -> dict:
    """The fields sent, changed, with a history line of before and after.
    The event copies are not touched (a club copies a profile onto its
    upcoming events with apply-to-upcoming)."""
    sent = body.model_fields_set
    before, after = {}, {}
    if "name" in sent:
        if not body.name:
            raise CustomHTTPException(422, message="A speaker needs a name.", error_code="NAME_REQUIRED", errors={"name": "Required."})
        if body.name != profile.name:
            before["name"], after["name"] = profile.name, body.name
            profile.name = body.name
            profile.name_key = names.name_key(body.name) or body.name.lower()[:120]
    for field in ("headline", "bio", "city"):
        if field in sent and getattr(body, field) != getattr(profile, field):
            before[field], after[field] = getattr(profile, field), getattr(body, field)
            setattr(profile, field, getattr(body, field))
    if "links" in sent and (body.links or {}) != (profile.links or {}):
        before["links"], after["links"] = dict(profile.links or {}), dict(body.links or {})
        profile.links = dict(body.links or {})
    if "topics" in sent:
        topics = await check_topics(session, body.topics or [])
        if topics != list(profile.topics or []):
            before["topics"], after["topics"] = list(profile.topics or []), topics
            profile.topics = topics
    if after:
        await record(session, profile.id, "edited", actor, before=before, after=after)
    await session.commit()
    return after


async def set_photo(session, profile: SpeakerProfiles, upload, actor: Actor) -> None:
    import io

    if upload is None:
        raise CustomHTTPException(422, message="Add a photo.", error_code="PHOTO_REQUIRED")
    data = await upload.read()
    if not data:
        raise CustomHTTPException(422, message="That photo is empty.", error_code="PHOTO_REQUIRED")
    was = photo_key(profile.photo)
    profile.photo = {"bytes": io.BytesIO(data), "filename": upload.filename or "photo.jpg"}
    try:
        await session.flush()
    except (ValueError, sa.exc.StatementError, CustomHTTPException) as e:
        await session.rollback()
        if isinstance(e, CustomHTTPException):
            raise
        raise CustomHTTPException(422, message="That photo could not be read. Use a JPEG, PNG or WebP.", error_code="BAD_PHOTO") from e
    await record(session, profile.id, "photo_changed", actor, before={"photo": was}, after={"photo": photo_key(profile.photo)})
    await session.commit()


__all__ = ["ACTIVE", "HIDDEN", "MERGED"]
