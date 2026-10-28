"""AI assist's endpoints, for the clubs dashboard.

Under Club auth; a team member's role is checked by club_access.policy on
every request: drafting and writing are changes (an editor or an admin;
door staff are refused), the status and an event's feedback summary are
reads (anyone who sees the event), forcing a new summary is a change, and
the club's own key is the owner's and admins' alone.

Each successful draft, piece of writing or new summary is one action
(access.run); a failed one is not counted. Errors: errors.py.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from sqlalchemy import select

from app.api.ai import access, draft, errors, feedback, keys, write
from app.api.ai.models import AIFeedbackSummaries
from app.api.ai.schemas import DraftOut, FeedbackSummaryOut, KeyIn, KeyOut, StatusOut, WriteIn, WriteOut
from app.api.club_access import service as club_access
from app.api.clubs.models import Clubs
from app.api.deliveries import service as deliveries
from app.api.events.models import Events
from app.api.subscriptions.service import IST
from app.config import settings
from app.core.ai import providers
from app.core.ai.base import AIRequest, Attachment
from app.core.auth.dependencies import ClubAuth
from app.core.rate_limit import account_or_address, limiter
from app.core.storage import formats
from app.db.core import SessionDep
from app.response import CustomHTTPException

club_router = APIRouter(prefix="/clubs/ai", tags=["ai"])
event_router = APIRouter(prefix="/events/{event_id}/ai", tags=["ai"])

#: Actions a minute per account, whatever the plan: a burst is a script, not a person.
RATE = "20/minute"


async def _club(session, user) -> Clubs:
    club = await session.scalar(select(Clubs).where(Clubs.user_id == user.id, Clubs.is_deleted == False))  # noqa: E712
    if club is None:
        raise CustomHTTPException(status_code=404, message="No club for this account")
    return club


def _member_id(request: Request) -> Optional[int]:
    member = club_access.current_member(request)
    return member.id if member is not None else None


def _today() -> date:
    return datetime.now(IST).date()


@club_router.get("/status", summary="Whether AI assist is on for the club, on whose key, and this month's use")
async def status(session: SessionDep, user: ClubAuth) -> StatusOut:
    """Answers whatever the switch says (`enabled: false` when it is off),
    so the dashboard knows what to show. `used` and `limit` are the platform
    key's: a club on its own key (`byok`) is not counted."""
    club = await _club(session, user)
    key = await access.club_key(session, club.id)
    byok = key is not None and await access.byok_allowed(session, club.id, user)
    from app.api.subscriptions import gates

    return StatusOut(
        enabled=bool(settings.AI_ENABLED),
        plan_allows=await access.plan_allows(session, club.id, user, key),
        used=await access.month_used(session, club.id),
        limit=await gates.cap(session, club.id, access.LIMIT_KEY, actor=user),
        resets_at=access.resets_at(),
        byok=byok,
        provider=key.provider if byok else providers.GEMINI,
    )


@club_router.post("/event-draft", summary="A draft event from notes, a poster or a PDF (nothing is saved)")
@limiter.limit(RATE, key_func=account_or_address)
async def event_draft(
    request: Request,
    session: SessionDep,
    user: ClubAuth,
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
) -> DraftOut:
    """
    Multipart: `text` (up to 6,000 characters) and/or `file` (a picture --
    PNG, JPEG, WebP, or a HEIC, AVIF, GIF, BMP or TIFF, which is converted --
    or a PDF, up to 10 MB). The draft has only what the material states;
    anything missing is null with a line in `warnings`. 422 for no input, a
    long text, a file of another type or a larger one.
    """
    club = await _club(session, user)
    ai = await access.resolve(session, club.id, user)
    text = (text or "").strip()
    if len(text) > draft.MAX_TEXT:
        raise CustomHTTPException(422, message=f"Keep the notes under {draft.MAX_TEXT:,} characters.", errors={"text": "Too long."})
    attachment = None
    if file is not None and (file.filename or file.size):
        data = await file.read(draft.MAX_FILE_BYTES + 1)
        if len(data) > draft.MAX_FILE_BYTES:
            raise CustomHTTPException(422, message="That file is over 10 MB. Use a smaller one.", errors={"file": "Too large."})
        sent = await asyncio.to_thread(draft.attachment, data) if data else None
        if sent is None:
            raise CustomHTTPException(
                422, message=f"Add the poster as a picture ({formats.WORKS}) or a PDF.",
                errors={"file": "Not a picture or a PDF."},
            )
        attachment = Attachment(mime_type=sent[0], data=sent[1])
    if not text and attachment is None:
        raise CustomHTTPException(422, message="Paste some notes or add a poster or PDF.", errors={"text": "Give notes or a file."})

    tree, categories = await draft.catalogue(session)
    today = _today()
    ask = AIRequest(
        system=draft.system_prompt(today),
        text=draft.user_prompt(tree=tree, categories=categories, text=text or None, has_file=attachment is not None),
        attachments=(attachment,) if attachment else (),
        schema=draft.SCHEMA,
    )
    outcome = await access.run(session, ai, action="event_draft", request=ask, parse=draft.parse, user=user, member_id=_member_id(request))
    result, warnings = draft.build(outcome.value, tree=tree, categories=categories, today=today, text=bool(text), file=attachment is not None)
    return DraftOut(draft=result, warnings=warnings)


@club_router.post("/write", summary="Writing for the club: a description, an announcement, an email, a thank-you, a caption")
@limiter.limit(RATE, key_func=account_or_address)
async def write_text(request: Request, session: SessionDep, user: ClubAuth, body: WriteIn) -> WriteOut:
    """`event_id`, when given, must be one of the club's events: its public
    facts (name, date, venue, about) are what the model knows of it.
    `subject` is only for an email."""
    club = await _club(session, user)
    ai = await access.resolve(session, club.id, user)
    if not body.prompt.strip() and body.event_id is None:
        raise CustomHTTPException(422, message="Say what to write, or pick an event.", errors={"prompt": "Empty."})
    event = None
    if body.event_id is not None:
        event = await session.scalar(
            select(Events).where(Events.id == body.event_id, Events.club_id == club.id, Events.is_deleted == False)  # noqa: E712
        )
        if event is None:
            raise CustomHTTPException(404, message="Event not found")
    ask = AIRequest(
        system=write.system_prompt(body.kind, body.language, body.tone),
        text=write.user_prompt(prompt=body.prompt, event=event, club_name=club.name),
        schema=write.SCHEMA,
        max_output_tokens=8192,
    )
    outcome = await access.run(session, ai, action="write", request=ask, parse=write.parse, user=user, member_id=_member_id(request))
    text, subject = write.result(outcome.value, body.kind)
    return WriteOut(text=text, subject=subject)


async def _summary(session, request: Request, user, event_id: int, *, force: bool) -> FeedbackSummaryOut:
    if not settings.AI_ENABLED:
        raise errors.disabled()
    event = await deliveries.event_for_club(session, user, event_id)
    items = await feedback.responses(session, event)
    count = len(items)
    if count < feedback.MIN_RESPONSES:
        return FeedbackSummaryOut(summary=None, responses=count, generated_at=None, cached=False)
    kept = await session.get(AIFeedbackSummaries, event.id)
    if kept is not None and not force and kept.responses == count:
        summary = feedback.cached_summary(kept.summary)
        if summary is not None:
            return FeedbackSummaryOut(summary=summary, responses=count, generated_at=kept.generated_at, cached=True)
    ai = await access.resolve(session, event.club_id, user)
    ask = AIRequest(system=feedback.SYSTEM, text=feedback.user_prompt(event, items), schema=feedback.SCHEMA, max_output_tokens=4096)
    outcome = await access.run(session, ai, action="feedback_summary", request=ask, parse=feedback.parse, user=user, member_id=_member_id(request))
    summary = feedback.build(outcome.value, items)
    now = access.now_utc()
    kept = await session.get(AIFeedbackSummaries, event.id)
    if kept is None:
        kept = AIFeedbackSummaries(event_id=event.id)
        session.add(kept)
    kept.responses, kept.summary, kept.provider, kept.model, kept.generated_at = count, summary.model_dump(), outcome.provider, outcome.model, now
    await session.commit()
    return FeedbackSummaryOut(summary=summary, responses=count, generated_at=now, cached=False)


@event_router.get("/feedback-summary", summary="The event's survey comments, summed up")
async def feedback_summary(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> FeedbackSummaryOut:
    """Null below 5 responses with comments (and no call). The kept summary
    while the number of responses is the same (`cached: true`); a new one,
    counted as an action, once it changes."""
    return await _summary(session, request, user, event_id, force=False)


@event_router.post("/feedback-summary/refresh", summary="Sum the comments up again now (counts as an action)")
@limiter.limit(RATE, key_func=account_or_address)
async def refresh_feedback_summary(request: Request, session: SessionDep, user: ClubAuth, event_id: int) -> FeedbackSummaryOut:
    return await _summary(session, request, user, event_id, force=True)


@club_router.get("/key", summary="The club's own AI key, if any (never the key itself)")
async def get_key(session: SessionDep, user: ClubAuth) -> KeyOut:
    club = await _club(session, user)
    return KeyOut(**keys.key_out(await access.club_key(session, club.id)))


@club_router.put("/key", summary="Connect the club's own Gemini or Anthropic key, after one test call")
@limiter.limit("10/minute", key_func=account_or_address)
async def put_key(request: Request, session: SessionDep, user: ClubAuth, body: KeyIn) -> KeyOut:
    """
    Tested with one tiny call before it is saved: 422 AI_KEY_INVALID, saying
    why, and nothing stored when the provider refuses it. Saved encrypted;
    from then on the club's AI actions run on it, not counted against its
    plan. 503 AI_UNAVAILABLE when the provider cannot be reached, or the
    server cannot store keys; 403 AI_DISABLED while AI assist is off.
    """
    club = await _club(session, user)
    key = await keys.connect(session, club, user, body, member_id=_member_id(request), request=request)
    return KeyOut(**keys.key_out(key))


@club_router.delete("/key", summary="Remove the club's own AI key")
async def delete_key(request: Request, session: SessionDep, user: ClubAuth) -> KeyOut:
    """The key is deleted; the club's AI actions go back to the platform's
    key and its plan's monthly actions."""
    club = await _club(session, user)
    await keys.disconnect(session, club, user, request=request)
    return KeyOut(**keys.key_out(None))
