"""Merging two profiles into one, and taking a merge back.

A merge moves everything the merged profile had to the kept one and writes
down exactly what moved, on the merged profile's history (`merged_into`).
Unmerge reads that line back and moves those things home: nothing is
deleted by a merge, so every merge can be undone.

What moves:
- its event rows (event_speakers.speaker_profile_id);
- its contacts, club by club: moved when the kept profile has none from that
  club, else the kept one's empty fields are filled from it (the merged one's
  row stays where it is);
- its followers who do not follow the kept one already;
- its claim, when only it was claimed (two claimed profiles are refused);
- the kept profile's empty fields (headline, bio, photo, city, links,
  topics) filled from it.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, update

from app.api.events.models import EventSpeakers
from app.api.speakers import service as sp
from app.api.speakers.models import (
    ACTIVE,
    DISMISSED,
    MERGED,
    OPEN,
    SpeakerContacts,
    SpeakerFollows,
    SpeakerHistory,
    SpeakerMergeCandidates,
    SpeakerProfiles,
)
from app.response import CustomHTTPException

_FILLABLE = ("headline", "bio", "city")
_CONTACT_FIELDS = ("email", "phone", "notes", "consent_at", "consent_by", "consent_by_name")


def _iso_or(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _locked(session, profile_id: int) -> SpeakerProfiles:
    profile = await session.scalar(
        select(SpeakerProfiles).where(SpeakerProfiles.id == profile_id).with_for_update().execution_options(populate_existing=True)
    )
    if profile is None:
        raise CustomHTTPException(404, message="That speaker could not be found.", error_code="SPEAKER_NOT_FOUND")
    return profile


async def merge(session, *, keep_id: int, merge_id: int, actor: sp.Actor) -> dict:
    if keep_id == merge_id:
        raise CustomHTTPException(422, message="Pick two different speakers.", error_code="SAME_SPEAKER")
    # Both rows held, in id order (two admins merging the pair both ways wait for each other).
    first, second = sorted((keep_id, merge_id))
    rows = {first: await _locked(session, first), second: await _locked(session, second)}
    keep, gone = rows[keep_id], rows[merge_id]
    if keep.status == MERGED or gone.status == MERGED:
        raise CustomHTTPException(409, message="One of these was merged already. Unmerge it first.", error_code="ALREADY_MERGED")
    if keep.claimed_by_user_id and gone.claimed_by_user_id and keep.claimed_by_user_id != gone.claimed_by_user_id:
        raise CustomHTTPException(409, message="Both profiles were claimed, by different people.", error_code="BOTH_CLAIMED")

    done: dict = {"keep_id": keep.id, "status_before": gone.status}
    # The event rows.
    moved = list((await session.scalars(
        select(EventSpeakers.id).where(EventSpeakers.speaker_profile_id == gone.id).execution_options(include_deleted=True)
    )).all())
    if moved:
        await session.execute(
            update(EventSpeakers).where(EventSpeakers.id.in_(moved)).values(speaker_profile_id=keep.id)
            .execution_options(synchronize_session=False)
        )
    done["event_speaker_ids"] = moved

    # Contacts, club by club.
    theirs = list((await session.scalars(select(SpeakerContacts).where(SpeakerContacts.profile_id == gone.id))).all())
    ours = {c.club_id: c for c in (await session.scalars(select(SpeakerContacts).where(SpeakerContacts.profile_id == keep.id))).all()}
    done["contacts_moved"], done["contacts_filled"] = [], []
    for c in theirs:
        mine = ours.get(c.club_id)
        if mine is None:
            c.profile_id = keep.id
            done["contacts_moved"].append(c.id)
            continue
        filled = {}
        for f in ("email", "phone", "notes"):
            if not getattr(mine, f) and getattr(c, f):
                filled[f] = None
                setattr(mine, f, getattr(c, f))
        if filled and mine.consent_at is None and c.consent_at is not None:
            for f in ("consent_at", "consent_by", "consent_by_name"):
                filled[f] = None
                setattr(mine, f, getattr(c, f))
        if filled:
            done["contacts_filled"].append({"contact_id": mine.id, "was": filled, "now": {k: _iso_or(getattr(mine, k)) for k in filled}})

    # Followers.
    already = set((await session.scalars(select(SpeakerFollows.user_id).where(SpeakerFollows.profile_id == keep.id))).all())
    follows = list((await session.scalars(select(SpeakerFollows).where(SpeakerFollows.profile_id == gone.id))).all())
    done["follows_moved"] = []
    for f in follows:
        if f.user_id not in already:
            f.profile_id = keep.id
            done["follows_moved"].append(f.id)

    # The claim.
    if gone.claimed_by_user_id and not keep.claimed_by_user_id:
        done["claim_moved"] = {"user_id": gone.claimed_by_user_id, "claimed_at": _iso_or(gone.claimed_at)}
        user_id, claimed_at = gone.claimed_by_user_id, gone.claimed_at
        # One profile a person: off the merged one first (ux_speaker_profiles_claimed_by).
        gone.claimed_by_user_id, gone.claimed_at = None, None
        await session.flush()
        keep.claimed_by_user_id, keep.claimed_at = user_id, claimed_at

    # The kept profile's empty fields.
    keep_filled = {}
    for f in _FILLABLE:
        if not getattr(keep, f) and getattr(gone, f):
            keep_filled[f] = getattr(gone, f)
            setattr(keep, f, getattr(gone, f))
    if not keep.photo and sp.photo_key(gone.photo):
        keep_filled["photo"] = sp.photo_key(gone.photo)
        keep.photo = keep_filled["photo"]
    links = dict(keep.links or {})
    added_links = {k: v for k, v in (gone.links or {}).items() if v and not links.get(k)}
    if added_links:
        keep.links = {**links, **added_links}
        keep_filled["links"] = added_links
    added_topics = [t for t in (gone.topics or []) if t not in (keep.topics or [])]
    if added_topics:
        keep.topics = list(keep.topics or []) + added_topics
        keep_filled["topics"] = added_topics
    done["keep_filled"] = keep_filled

    gone.status, gone.merged_into_id = MERGED, keep.id
    # The queue: this pair is decided; nothing else about the merged one is open.
    lo, hi = sorted((keep.id, gone.id))
    await session.execute(
        update(SpeakerMergeCandidates).where(SpeakerMergeCandidates.a_id == lo, SpeakerMergeCandidates.b_id == hi)
        .values(status=MERGED, decided_by=actor.user_id, decided_at=sp.now(), updated_at=sp.now())
    )
    await sp.record(session, gone.id, "merged_into", actor, before={"status": done["status_before"]}, after=done)
    await sp.record(session, keep.id, "merged_from", actor, after={"merged_id": gone.id, "name": gone.name, "event_speaker_ids": moved})
    await session.commit()
    return {"keep_id": keep.id, "merged_id": gone.id, "moved_talks": len(moved), "moved_contacts": len(done["contacts_moved"]),
            "moved_followers": len(done["follows_moved"])}


async def _last_merge(session, profile_id: int) -> Optional[SpeakerHistory]:
    return await session.scalar(
        select(SpeakerHistory).where(SpeakerHistory.profile_id == profile_id, SpeakerHistory.action.in_(("merged_into", "unmerged")))
        .order_by(SpeakerHistory.created_at.desc(), SpeakerHistory.id.desc()).limit(1)
    )


async def unmerge(session, *, profile_id: int, actor: sp.Actor) -> dict:
    """The merged profile back on its own, with what the merge moved from it
    (what has moved on since -- a row removed from its event, a contact the
    club deleted -- stays where it is now)."""
    gone = await _locked(session, profile_id)
    if gone.status != MERGED or gone.merged_into_id is None:
        raise CustomHTTPException(409, message="This speaker is not merged into another.", error_code="NOT_MERGED")
    line = await _last_merge(session, gone.id)
    if line is None or line.action != "merged_into":
        raise CustomHTTPException(409, message="There is no record of this merge to take back.", error_code="NO_MERGE_RECORD")
    done = dict(line.after or {})
    keep = await _locked(session, int(done.get("keep_id") or gone.merged_into_id))

    talks = [int(i) for i in done.get("event_speaker_ids") or []]
    back = []
    if talks:
        back = list((await session.scalars(
            select(EventSpeakers.id).where(EventSpeakers.id.in_(talks), EventSpeakers.speaker_profile_id == keep.id)
            .execution_options(include_deleted=True)
        )).all())
        if back:
            await session.execute(
                update(EventSpeakers).where(EventSpeakers.id.in_(back)).values(speaker_profile_id=gone.id)
                .execution_options(synchronize_session=False)
            )
    for cid in done.get("contacts_moved") or []:
        c = await session.get(SpeakerContacts, int(cid))
        if c is not None and c.profile_id == keep.id:
            c.profile_id = gone.id
    for filled in done.get("contacts_filled") or []:
        c = await session.get(SpeakerContacts, int(filled["contact_id"]))
        if c is None:
            continue
        for f, was in (filled.get("was") or {}).items():
            # Only a field still as the merge left it: a later edit by the club stays.
            if _iso_or(getattr(c, f)) == (filled.get("now") or {}).get(f):
                setattr(c, f, was)
    for fid in done.get("follows_moved") or []:
        f = await session.get(SpeakerFollows, int(fid))
        if f is not None and f.profile_id == keep.id:
            f.profile_id = gone.id
    claim = done.get("claim_moved")
    if claim and keep.claimed_by_user_id == claim.get("user_id"):
        keep.claimed_by_user_id, keep.claimed_at = None, None
        await session.flush()
        gone.claimed_by_user_id = claim["user_id"]
        gone.claimed_at = sp.now() if not claim.get("claimed_at") else _parse(claim["claimed_at"])
    for f, value in (done.get("keep_filled") or {}).items():
        if f in _FILLABLE and getattr(keep, f) == value:
            setattr(keep, f, None)
        elif f == "photo" and sp.photo_key(keep.photo) == value:
            keep.photo = None
        elif f == "links":
            links = dict(keep.links or {})
            for k, v in value.items():
                if links.get(k) == v:
                    links.pop(k)
            keep.links = links
        elif f == "topics":
            keep.topics = [t for t in (keep.topics or []) if t not in value]

    gone.status = done.get("status_before") if done.get("status_before") in (ACTIVE, "hidden") else ACTIVE
    gone.merged_into_id = None
    lo, hi = sorted((keep.id, gone.id))
    # Two people after all: the pair leaves the queue.
    await session.execute(
        update(SpeakerMergeCandidates).where(SpeakerMergeCandidates.a_id == lo, SpeakerMergeCandidates.b_id == hi)
        .values(status=DISMISSED, decided_by=actor.user_id, decided_at=sp.now(), updated_at=sp.now())
    )
    await sp.record(session, gone.id, "unmerged", actor, after={"from_id": keep.id, "event_speaker_ids": back})
    await sp.record(session, keep.id, "unmerged_from", actor, after={"restored_id": gone.id, "event_speaker_ids": back})
    await session.commit()
    return {"id": gone.id, "from_id": keep.id, "restored_talks": len(back), "status": gone.status}


def _parse(value: str):
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return sp.now()


async def dismiss(session, candidate_id: int, actor: sp.Actor) -> dict:
    row = await session.get(SpeakerMergeCandidates, candidate_id)
    if row is None:
        raise CustomHTTPException(404, message="That pair is not in the queue.", error_code="CANDIDATE_NOT_FOUND")
    if row.status != OPEN:
        raise CustomHTTPException(409, message=f"That pair was {row.status} already.", error_code="CANDIDATE_DECIDED")
    row.status, row.decided_by, row.decided_at = DISMISSED, actor.user_id, sp.now()
    for pid in (row.a_id, row.b_id):
        await sp.record(session, pid, "duplicate_dismissed", actor, after={"candidate_id": row.id, "pair": [row.a_id, row.b_id]})
    await session.commit()
    return {"id": row.id, "status": row.status}
