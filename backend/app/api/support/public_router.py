"""Asking for help, from the app, the website and the club dashboard:
prefix /api/v1/support (feature-register/support-leads-contract.md).

Signed in or not: an app account or a club (and its team member) is linked;
a guest gives an address and a name. Requests are limited per address on the
network and per email address, and card-like numbers are masked before
anything is stored. What a person reads of their own case: what they wrote
and what we answered, never the team's notes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from fastapi import APIRouter, File, Request, UploadFile
from limits import parse
from sqlalchemy import select

from app.api.support import cases, files, mail, text
from app.api.support.models import (
    REPLY_IN,
    REPLY_OUT,
    SupportAttachments,
    SupportCases,
    SupportHelpAnswers,
    SupportTopics,
)
from app.api.support.schemas import ReplyIn, RequestIn, SurveyIn
from app.core.auth.dependencies import DependsAuth, OptionalAuth
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.db.core import SessionDep
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/support", tags=["support"])

#: Per address on the network (slowapi), and per email address (below).
REQUEST_LIMIT = "6/hour;20/day"
EMAIL_LIMIT = ("5/hour", "10/day")
UPLOAD_LIMIT = "20/hour"
REPLY_LIMIT = "30/hour"
SURVEY_LIMIT = "20/hour"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _address(request: Request) -> str:
    return client_address(request) or "unknown"


@lru_cache(maxsize=None)
def _item(rate: str):
    return parse(rate)


def _per_email(email: str) -> None:
    """The per-email limit: one address can't open more than a few a day,
    whatever network it writes from."""
    if not limiter.enabled or not email:
        return
    for rate in EMAIL_LIMIT:
        if not limiter.limiter.hit(_item(rate), "support:request:email", email.strip().lower()):
            raise CustomHTTPException(
                429, message="You've sent a lot of requests. We'll answer the ones we have; try again later.",
                error_code="RATE_LIMITED",
            )


async def _requester(session, request: Request, user, body: RequestIn) -> cases.Requester:
    kind = getattr(getattr(user, "user_type", None), "value", None)
    if kind == "club":
        from app.api.club_access.service import current_member

        club = user.club
        member = current_member(request)
        return cases.Requester(
            kind="club", club_id=club.id, member_id=member.id if member else None,
            email=(member.email if member else None) or club.contact_email or user.email,
            name=(member.full_name if member and member.full_name else None) or club.name,
        )
    if user is not None:
        return cases.Requester(kind="user", user_id=user.id, email=user.email, name=user.full_name)
    if not body.email or not (body.name or "").strip():
        raise CustomHTTPException(
            422, message="Tell us your name and an email address to answer you at.", error_code="CONTACT_REQUIRED",
            errors={"field": "email" if not body.email else "name"},
        )
    return cases.Requester(kind="guest", email=str(body.email).lower(), name=body.name.strip())


async def _links(session, user, requester: cases.Requester, ctx) -> dict:
    """What the request is about, as far as the asker may say: their own
    registration, an event that exists, their own club."""
    from app.api.events.models import EventRegistrationsLink, Events

    links: dict = {}
    if requester.user_id:
        links["user_id"] = requester.user_id
    if requester.club_id:
        links["club_id"] = requester.club_id
    if ctx is None:
        return links
    if ctx.event_id and await session.get(Events, ctx.event_id) is not None:
        links["event_id"] = ctx.event_id
    if ctx.registration_id and requester.user_id:
        from app.api.support.panel import _uuid

        reg_id = _uuid(ctx.registration_id)
        if reg_id is not None:
            reg = await session.get(EventRegistrationsLink, reg_id)
            if reg is not None and reg.user_id == requester.user_id:
                links["registration_id"] = str(reg.id)
                links.setdefault("event_id", reg.event_id)
    return links


# ─── Answers and topics ──────────────────────────────────────────────────


@router.get("/help", summary="Published answers to common questions")
async def help_answers(session: SessionDep) -> list[dict]:
    rows = (await session.scalars(
        select(SupportHelpAnswers).where(SupportHelpAnswers.published.is_(True))
        .order_by(SupportHelpAnswers.position, SupportHelpAnswers.id)
    )).all()
    return [{"id": r.id, "topic": r.topic, "question": r.question, "answer": r.answer} for r in rows]


@router.get("/topics", summary="What a request can be about")
async def topics(session: SessionDep) -> list[dict]:
    from app.api.support import reference

    await reference.ensure(session)
    await session.commit()
    rows = (await session.scalars(
        select(SupportTopics).where(SupportTopics.active.is_(True)).order_by(SupportTopics.position, SupportTopics.key)
    )).all()
    return [{"key": r.key, "label": r.label} for r in rows]


# ─── Asking ──────────────────────────────────────────────────────────────


@router.post("/uploads", summary="Upload a screenshot or PDF for a request (up to 3, 5 MB each)", status_code=201)
@limiter.limit(UPLOAD_LIMIT, key_func=_address)
async def upload(
    request: Request, session: SessionDep, user: OptionalAuth,
    file: Optional[UploadFile] = File(None), files_: Optional[list[UploadFile]] = File(None, alias="files"),
) -> dict:
    """Multipart `file` (one) or `files` (up to three). Answers the first
    file's `{key, name, size}`, and `files` with every one's."""
    items = [f for f in ([file] if file is not None else []) + list(files_ or []) if f is not None]
    if not items:
        raise CustomHTTPException(422, message="Choose a file.", error_code="NO_FILE")
    if len(items) > files.FORM_MAX_FILES:
        raise CustomHTTPException(422, message=f"At most {files.FORM_MAX_FILES} files.", error_code="TOO_MANY_FILES")
    rows = [await files.keep_form_upload(session, f, getattr(user, "id", None)) for f in items]
    await session.commit()
    out = [{"key": r.upload_key, "name": r.filename, "size": r.size_bytes} for r in rows]
    return {**out[0], "files": out}


@router.post("/requests", summary="Ask for help: opens a case", status_code=201)
@limiter.limit(REQUEST_LIMIT, key_func=_address)
async def create_request(request: Request, body: RequestIn, session: SessionDep, user: OptionalAuth) -> dict:
    requester = await _requester(session, request, user, body)
    _per_email(requester.email or "")
    message = text.mask_cards(body.message.strip())
    if not message:
        raise CustomHTTPException(422, message="Write what you need help with.", error_code="EMPTY_MESSAGE", errors={"field": "message"})
    uploads = await files.claim(session, body.attachments, user_id=getattr(user, "id", None))
    ctx = body.context
    channel = (ctx.platform if ctx and ctx.platform else None) or ("dashboard" if requester.kind == "club" else "web")
    context = {k: v for k, v in (ctx.model_dump() if ctx else {}).items() if v not in (None, "") and k in (
        "platform", "page_url", "error_code", "track_id", "app_version")} or None
    subject = body.subject or ""
    same = await cases.recent_same(session, requester, subject or text.first_words(message, 80))
    if same is not None:
        case, _, _ = await cases.requester_reply(
            session, same, body_text=message, author_name=requester.name, author_user_id=requester.user_id, attachment_rows=uploads,
        )
        await cases.audit(session, user, "case.joined", case, f"Case #{case.number}: the same person wrote again",
                          {"channel": channel}, request=request)
        await session.commit()
        return {"id": case.id, "number": case.number, "status": case.status}
    links = await _links(session, user, requester, ctx)
    case, _ = await cases.open_case(
        session, requester=requester, topic=body.topic, subject=subject, body_text=message, channel=channel, links=links,
        context=context, actor=user, attachment_rows=uploads,
    )
    await mail.enqueue_receipt(session, case)
    await cases.audit(session, user, "case.opened", case, f"Case #{case.number} opened ({channel})",
                      {"channel": channel, "topic": case.topic, "requester_kind": case.requester_kind}, request=request)
    await session.commit()
    return {"id": case.id, "number": case.number, "status": case.status}


def _mine(user):
    """Whose cases a signed-in caller sees: a club its club's, anyone else their own."""
    kind = getattr(getattr(user, "user_type", None), "value", None)
    if kind == "club":
        return SupportCases.requester_club_id == user.club.id
    return SupportCases.requester_user_id == user.id


async def _own_case(session, user, case_id: int) -> SupportCases:
    case = await session.scalar(select(SupportCases).where(SupportCases.id == case_id, _mine(user)))
    if case is None:
        raise CustomHTTPException(404, message="There's no such request.", error_code="REQUEST_NOT_FOUND")
    if case.merged_into_id:
        target = await cases.follow_merges(session, case)
        again = await session.scalar(select(SupportCases).where(SupportCases.id == target.id, _mine(user)))
        if again is not None:
            return again
    return case


@router.get("/requests", summary="My requests (a club: the club's)")
async def my_requests(session: SessionDep, user: DependsAuth) -> list[dict]:
    rows = (await session.scalars(
        select(SupportCases).where(_mine(user), SupportCases.merged_into_id.is_(None))
        .order_by(SupportCases.updated_at.desc(), SupportCases.id.desc()).limit(200)
    )).all()
    return [
        {"id": c.id, "number": c.number, "subject": c.subject, "topic": c.topic, "status": c.status, "updated_at": c.updated_at,
         "unread": bool(c.last_outbound_at and (c.requester_read_at is None or c.last_outbound_at > c.requester_read_at))}
        for c in rows
    ]


def _attachment_url(case_id: int):
    return lambda a: f"/api/v1/support/requests/{case_id}/attachments/{a.id}"


async def _view(session, case: SupportCases) -> dict:
    msgs = await cases.messages_of(session, case.id, kinds=(REPLY_IN, REPLY_OUT))
    by_msg = await cases.attachments_by_message(session, [m.id for m in msgs])
    url = _attachment_url(case.id)
    out_msgs = []
    for m in msgs:
        item = cases.message_out(m, by_msg.get(m.id, []), url, team=False)
        if m.kind == REPLY_OUT:
            # The team's own name stays theirs; the person sees the desk.
            item["author_name"] = (m.author_name or "").split(" ")[0] + " (MyOtherApp)" if m.author_name else "MyOtherApp"
        out_msgs.append(item)
    survey = None
    if case.survey_at is not None:
        survey = {"solved": case.survey_solved, "comment": case.survey_comment}
    return {
        "id": case.id, "number": case.number, "subject": case.subject, "topic": case.topic, "status": case.status,
        "created_at": case.created_at, "updated_at": case.updated_at, "messages": out_msgs,
        "can_reply": case.purged_at is None, "can_survey": case.status in ("resolved", "closed") and case.survey_at is None,
        "survey": survey, "parent_case_id": case.parent_case_id,
    }


@router.get("/requests/{case_id}", summary="One of my requests, with its replies")
async def my_request(case_id: int, session: SessionDep, user: DependsAuth) -> dict:
    case = await _own_case(session, user, case_id)
    case.requester_read_at = _now()
    out = await _view(session, case)
    await session.commit()
    return out


@router.get("/requests/{case_id}/attachments/{attachment_id}", summary="A file on one of my requests")
async def my_attachment(case_id: int, attachment_id: int, session: SessionDep, user: DependsAuth):
    case = await _own_case(session, user, case_id)
    row = await session.scalar(
        select(SupportAttachments).where(SupportAttachments.id == attachment_id, SupportAttachments.case_id == case.id)
    )
    if row is None or row.message_id is None:
        raise CustomHTTPException(404, message="There's no such file.", error_code="FILE_NOT_FOUND")
    from app.api.support.models import SupportMessages

    kind = await session.scalar(select(SupportMessages.kind).where(SupportMessages.id == row.message_id))
    if kind not in (REPLY_IN, REPLY_OUT):
        raise CustomHTTPException(404, message="There's no such file.", error_code="FILE_NOT_FOUND")
    return await files.serve(row)


@router.post("/requests/{case_id}/replies", summary="Write again on my request", status_code=201)
@limiter.limit(REPLY_LIMIT, key_func=_address)
async def reply(request: Request, case_id: int, body: ReplyIn, session: SessionDep, user: DependsAuth) -> dict:
    case = await _own_case(session, user, case_id)
    if case.purged_at is not None:
        raise CustomHTTPException(409, message="This request is too old to answer; send a new one.", error_code="REQUEST_PURGED")
    message = text.mask_cards(body.message.strip())
    if not message:
        raise CustomHTTPException(422, message="Write something first.", error_code="EMPTY_MESSAGE", errors={"field": "message"})
    uploads = await files.claim(session, body.attachments, user_id=user.id)
    requester = await _requester(session, request, user, RequestIn(topic="other", message=message))
    target, msg, new = await cases.requester_reply(
        session, case, body_text=message, author_name=requester.name, author_user_id=requester.user_id, attachment_rows=uploads,
    )
    target.requester_read_at = _now()
    await cases.audit(session, user, "case.new_from_reply" if new else "case.replied", target,
                      f"Case #{target.number}: they wrote" + (f" (after #{case.number} closed)" if new else ""), request=request)
    await session.commit()
    files_out = await cases.attachments_by_message(session, [msg.id])
    out = cases.message_out(msg, files_out.get(msg.id, []), _attachment_url(target.id), team=False)
    return {**out, "case_id": target.id, "number": target.number, "new_case": new, "status": target.status}


async def _record_survey(session, case: SupportCases, body: SurveyIn, actor, request) -> dict:
    if case.status not in ("resolved", "closed") and case.survey_at is None:
        raise CustomHTTPException(409, message="This request isn't solved yet.", error_code="NOT_RESOLVED")
    case.survey_solved = body.solved
    case.survey_comment = text.mask_cards((body.comment or "").strip())[:500] or None
    case.survey_at = _now()
    if not body.solved and case.status == "resolved":
        await cases.set_status(session, case, "open")
        await cases.system_line(session, case, "They said it isn't solved; reopened.")
    await cases.audit(session, actor, "case.survey", case, f"Case #{case.number}: {'solved' if body.solved else 'not solved'}, they said",
                      {"solved": body.solved}, request=request)
    await session.commit()
    return {"ok": True, "number": case.number, "status": case.status}


@router.post("/survey/{token}", summary="Did we solve it? (the link in the mail)")
@limiter.limit(SURVEY_LIMIT, key_func=_address)
async def survey_by_token(request: Request, token: str, body: SurveyIn, session: SessionDep) -> dict:
    if not token or len(token) > 64:
        raise CustomHTTPException(404, message="That link isn't valid.", error_code="SURVEY_NOT_FOUND")
    case = await session.scalar(select(SupportCases).where(SupportCases.survey_token == token))
    if case is None:
        raise CustomHTTPException(404, message="That link isn't valid.", error_code="SURVEY_NOT_FOUND")
    return await _record_survey(session, case, body, None, request)


@router.post("/requests/{case_id}/survey", summary="Did we solve it? (signed in)")
async def survey_signed_in(request: Request, case_id: int, body: SurveyIn, session: SessionDep, user: DependsAuth) -> dict:
    case = await _own_case(session, user, case_id)
    return await _record_survey(session, case, body, user, request)

