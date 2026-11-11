"""Support cases: opened from any of the five doors, answered, moved through
their statuses, merged, and read.

The doors -- the app, the website, the club dashboard, mail to support@ and a
case logged by hand -- all open a case here (`open_case`). Each is assigned
at once by its topic's rule: a named person while they are available, else
round robin across the support team, skipping anyone away; nobody available
leaves it Unassigned.

Statuses: new -> open -> waiting (we answered) -> open (they answered) ...
-> resolved -> closed. A reply from the person within REOPEN_WITHIN of
Resolved reopens the case; a later one, or one to a closed case, starts a
new case pointing at the old (`parent_case_id`). The same person writing
again about the same subject within AUTO_JOIN_WITHIN joins the case they
opened rather than starting another.

Every change is written to the audit log -- never a word of what anyone wrote.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_, select, update

from app.api.support import hours, reference, teams, text
from app.api.support.models import (
    ACTIVE,
    CASE_NUMBER,
    CHANNELS,
    CLOSED,
    NEW,
    NOTE,
    NOT_DELIVERED,
    OPEN,
    PRIORITIES,
    REPLY_IN,
    REPLY_OUT,
    RESOLVED,
    SPAM_TAG,
    STATUSES,
    SUPPORT,
    SYSTEM,
    UNFINISHED,
    WAITING,
    SupportAttachments,
    SupportCases,
    SupportMessages,
    SupportTopics,
)
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

REOPEN_WITHIN = timedelta(days=7)
AUTO_JOIN_WITHIN = timedelta(hours=1)
MAX_SUBJECT = 200
MAX_PUBLIC_MESSAGE = 5000
MAX_TEAM_MESSAGE = 20000
MAX_TAGS = 20
LINK_KEYS = ("user_id", "club_id", "event_id", "registration_id", "payment_id", "refund_id", "dispute_id")
ENTITY = "support_case"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(code, message=message, error_code=error_code, errors=errors)


@dataclass
class Requester:
    kind: str  # "user" | "guest" | "club"
    user_id: Optional[int] = None
    club_id: Optional[int] = None
    member_id: Optional[int] = None
    email: Optional[str] = None
    name: Optional[str] = None


@dataclass
class MailFields:
    """What an inbound mail carries for threading and for keeping it once."""
    from_address: Optional[str] = None
    to_addresses: list = field(default_factory=list)
    email_message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None


async def audit(session, actor, action: str, case: SupportCases, summary: str, details: Optional[dict] = None, request=None):
    from app.api.audit import service as audit_service

    await audit_service.record(
        session, actor=actor, action=f"support.{action}", entity_type=ENTITY, entity_id=case.id,
        summary=summary, details={"number": case.number, **(details or {})}, request=request,
    )


# ─── Reading one ─────────────────────────────────────────────────────────


async def get_case(session, case_id: int, *, lock: bool = False) -> SupportCases:
    stmt = select(SupportCases).where(SupportCases.id == int(case_id))
    if lock:
        stmt = stmt.with_for_update()
    case = await session.scalar(stmt.execution_options(populate_existing=True))
    if case is None:
        raise _refuse(404, "There's no such case.", "CASE_NOT_FOUND")
    return case


async def by_number(session, number: int) -> Optional[SupportCases]:
    return await session.scalar(select(SupportCases).where(SupportCases.number == int(number)))


async def follow_merges(session, case: SupportCases) -> SupportCases:
    """The case a merged one went into (and on, at most a few hops)."""
    seen = {case.id}
    while case.merged_into_id and case.merged_into_id not in seen:
        nxt = await session.get(SupportCases, case.merged_into_id)
        if nxt is None:
            break
        seen.add(nxt.id)
        case = nxt
    return case


async def topic_row(session, key: Optional[str], *, required: bool = True) -> Optional[SupportTopics]:
    await reference.ensure(session)
    if not key:
        if required:
            raise _refuse(422, "Pick what it's about.", "INVALID_TOPIC", {"field": "topic"})
        return None
    row = await session.get(SupportTopics, key)
    if row is None or not row.active:
        if required:
            raise _refuse(422, "That isn't one of the topics.", "INVALID_TOPIC", {"field": "topic"})
        return None
    return row


# ─── Opening one ─────────────────────────────────────────────────────────


async def assignee_for(session, topic: Optional[SupportTopics], now: datetime) -> Optional[int]:
    if topic is not None and topic.assign == "person" and await teams.is_available(session, SUPPORT, topic.assignee_id, now):
        return topic.assignee_id
    return await teams.next_member(session, SUPPORT, now)


def _clean_links(links: Optional[dict]) -> dict:
    out = {}
    for key in LINK_KEYS:
        value = (links or {}).get(key)
        if value in (None, ""):
            continue
        out[key] = str(value) if key in ("registration_id", "payment_id") else int(value)
    return out


async def open_case(
    session, *, requester: Requester, topic: Optional[str], subject: Optional[str], body_text: str,
    channel: str, body_html: Optional[str] = None, priority: Optional[str] = None, links: Optional[dict] = None,
    context: Optional[dict] = None, actor=None, attachment_rows: Iterable[SupportAttachments] = (),
    mail: Optional[MailFields] = None, flags: Iterable[str] = (), tags: Iterable[str] = (), status: str = NEW,
    parent_case_id: Optional[int] = None, assign: bool = True, assignee_id: Optional[int] = None,
    now: Optional[datetime] = None, topic_required: bool = True,
) -> tuple[SupportCases, SupportMessages]:
    """A new case with its first message (from the person), assigned. The caller commits."""
    now = now or _now()
    if channel not in CHANNELS:
        raise _refuse(422, "Unknown channel.", "INVALID_CHANNEL")
    row = await topic_row(session, topic, required=topic_required)
    if row is None and topic and not topic_required:
        topic = None
    if priority is not None and priority not in PRIORITIES:
        raise _refuse(422, "Priority is low, normal, high or urgent.", "INVALID_PRIORITY", {"field": "priority"})
    desk = await hours.get(session)
    first_due, resolution_due = hours.timers(now, desk)
    clean_subject = text.mask_cards(" ".join((subject or "").split()))[:MAX_SUBJECT] or text.first_words(text.mask_cards(body_text), 80) or "Help"
    if status in (CLOSED,):
        assignee = None
    elif assignee_id is not None:
        assignee = assignee_id
    else:
        assignee = await assignee_for(session, row, now) if assign else None
    number = int(await session.scalar(select(CASE_NUMBER.next_value())))
    case = SupportCases(
        number=number, subject=clean_subject, status=status, priority=priority or (row.default_priority if row else "normal"),
        topic=row.key if row is not None else None, channel=channel, requester_kind=requester.kind,
        requester_user_id=requester.user_id, requester_club_id=requester.club_id, requester_member_id=requester.member_id,
        requester_email=(requester.email or "").strip().lower()[:254] or None, requester_name=(requester.name or "")[:120] or None,
        assignee_id=assignee, tags=list(dict.fromkeys(tags))[:MAX_TAGS], flags=list(dict.fromkeys(flags)),
        links=_clean_links(links), context=context or None, first_reply_due=first_due, resolution_due=resolution_due,
        parent_case_id=parent_case_id, created_by=getattr(actor, "id", None), created_at=now, updated_at=now,
        closed_at=now if status == CLOSED else None, close_reason=SPAM_TAG if status == CLOSED and SPAM_TAG in tags else None,
    )
    session.add(case)
    await session.flush()
    msg = await add_message(
        session, case, REPLY_IN, body_text=body_text, body_html=body_html, author_name=requester.name or requester.email,
        author_user_id=requester.user_id, mail=mail, attachment_rows=attachment_rows, now=now, touch_status=False,
    )
    return case, msg


# ─── Messages ────────────────────────────────────────────────────────────


async def add_message(
    session, case: SupportCases, kind: str, *, body_text: Optional[str], body_html: Optional[str] = None,
    author_user_id: Optional[int] = None, author_name: Optional[str] = None, mail: Optional[MailFields] = None,
    attachment_rows: Iterable[SupportAttachments] = (), saved_reply_id: Optional[int] = None,
    now: Optional[datetime] = None, touch_status: bool = True,
) -> SupportMessages:
    """One message on the case, its files attached, the case's counters and
    timers moved. A reply from us puts the case Waiting on them; one from
    them puts a Waiting (or New) one Open. The caller commits."""
    now = now or _now()
    m = mail or MailFields()
    msg = SupportMessages(
        case_id=case.id, kind=kind, body_text=body_text, body_html=body_html, author_user_id=author_user_id,
        author_name=(author_name or "")[:120] or None, from_address=m.from_address, to_addresses=m.to_addresses or None,
        email_message_id=m.email_message_id, in_reply_to=m.in_reply_to, references=m.references, provider=m.provider,
        provider_message_id=m.provider_message_id, saved_reply_id=saved_reply_id, created_at=now,
    )
    session.add(msg)
    await session.flush()
    for a in attachment_rows:
        a.case_id, a.message_id = case.id, msg.id
    case.message_count = (case.message_count or 0) + 1
    case.updated_at = now
    if kind in (REPLY_IN, REPLY_OUT):
        case.last_message_at = now
    if kind == REPLY_IN:
        case.last_inbound_at = now
        case.reminded_at = None
        if touch_status and case.status == WAITING:
            case.status, case.waiting_since = OPEN, None
    elif kind == REPLY_OUT:
        case.last_outbound_at = now
        if case.first_replied_at is None:
            case.first_replied_at = now
        case.flags = [f for f in (case.flags or []) if f != NOT_DELIVERED]
        if case.status in (NEW, OPEN):
            case.status, case.waiting_since, case.reminded_at = WAITING, now, None
    await session.flush()
    return msg


async def system_line(session, case: SupportCases, words: str, now: Optional[datetime] = None) -> SupportMessages:
    return await add_message(session, case, SYSTEM, body_text=words, author_name="MyOtherApp", now=now, touch_status=False)


async def requester_reply(
    session, case: SupportCases, *, body_text: str, body_html: Optional[str] = None, author_name: Optional[str] = None,
    author_user_id: Optional[int] = None, mail: Optional[MailFields] = None, attachment_rows: Iterable[SupportAttachments] = (),
    now: Optional[datetime] = None,
) -> tuple[SupportCases, SupportMessages, bool]:
    """What the person wrote on a case they have. Returns (the case it went
    on, the message, whether that is a new case). Within REOPEN_WITHIN of
    Resolved it reopens the case; to a closed case, or later, it opens a new
    one linked to the old. The caller commits."""
    now = now or _now()
    case = await follow_merges(session, case)
    resolved_long_ago = case.status == RESOLVED and case.resolved_at is not None and case.resolved_at < now - REOPEN_WITHIN
    if case.status == CLOSED or resolved_long_ago or case.purged_at is not None:
        requester = Requester(
            kind=case.requester_kind, user_id=case.requester_user_id, club_id=case.requester_club_id,
            member_id=case.requester_member_id, email=case.requester_email, name=case.requester_name,
        )
        new, msg = await open_case(
            session, requester=requester, topic=case.topic, subject=case.subject, body_text=body_text, body_html=body_html,
            channel=case.channel, links=case.links, context=case.context, attachment_rows=attachment_rows, mail=mail,
            parent_case_id=case.id, now=now, topic_required=False,
        )
        await system_line(session, case, f"They wrote again after this was closed: case #{new.number}.", now)
        await system_line(session, new, f"Follows on from case #{case.number}.", now)
        return new, msg, True
    if case.status == RESOLVED:
        case.status, case.resolved_at = OPEN, None
        await system_line(session, case, "Reopened: they replied.", now)
    msg = await add_message(
        session, case, REPLY_IN, body_text=body_text, body_html=body_html, author_name=author_name,
        author_user_id=author_user_id, mail=mail, attachment_rows=attachment_rows, now=now,
    )
    if case.status == NEW:
        case.status = NEW
    return case, msg, False


async def recent_same(session, requester: Requester, subject: str, now: Optional[datetime] = None) -> Optional[SupportCases]:
    """A case this person opened within AUTO_JOIN_WITHIN about the same
    subject, still unfinished: the one a second message joins."""
    now = now or _now()
    key = text.subject_key(subject)
    if not key:
        return None
    C = SupportCases
    who = []
    if requester.user_id:
        who.append(C.requester_user_id == requester.user_id)
    if requester.club_id:
        who.append(C.requester_club_id == requester.club_id)
    if requester.email:
        who.append(func.lower(C.requester_email) == requester.email.strip().lower())
    if not who:
        return None
    rows = (await session.scalars(
        select(C).where(or_(*who), C.created_at >= now - AUTO_JOIN_WITHIN, C.status.in_(UNFINISHED), C.merged_into_id.is_(None))
        .order_by(C.created_at.desc()).limit(5)
    )).all()
    for c in rows:
        if text.subject_key(c.subject) == key:
            return c
    return None


# ─── Changing one ────────────────────────────────────────────────────────


def can_work(rights: teams.Rights, case: SupportCases) -> bool:
    """Answer, note on, change and close: an owner, or the support member it is assigned to."""
    return rights.owner or (rights.support and case.assignee_id == rights.user_id)


def require_work(rights: teams.Rights, case: SupportCases, what: str = "change this case") -> None:
    if can_work(rights, case):
        return
    if not rights.support:
        rights.require(what, support=True)
    if case.assignee_id is None:
        raise _refuse(403, "Take the case first.", "NOT_ASSIGNED_TO_YOU")
    raise _refuse(403, f"Only the person it's assigned to (or an admin owner) can {what}.", "NOT_ASSIGNED_TO_YOU")


async def set_status(session, case: SupportCases, status: str, *, now: Optional[datetime] = None, reason: Optional[str] = None) -> bool:
    """Move the case to `status`. True when it changed. Resolved sets off
    the survey mail (once); closed records why."""
    now = now or _now()
    if status not in STATUSES:
        raise _refuse(422, "Unknown status.", "INVALID_STATUS", {"field": "status"})
    if status == case.status:
        return False
    before = case.status
    case.status, case.updated_at = status, now
    if status == WAITING:
        case.waiting_since, case.reminded_at = now, None
    elif before == WAITING:
        case.waiting_since = None
    if status == RESOLVED:
        case.resolved_at = now
        from app.api.support import mail

        await mail.enqueue_survey(session, case)
    if status == CLOSED:
        case.closed_at, case.close_reason = now, (reason or case.close_reason or "closed")[:20]
    elif before == CLOSED:
        case.closed_at, case.close_reason = None, None
    if status in (NEW, OPEN, WAITING) and before in (RESOLVED, CLOSED):
        case.resolved_at = None
    return True


async def assign(session, case: SupportCases, assignee_id: Optional[int], now: Optional[datetime] = None) -> None:
    """Give the case to a support team member (or nobody). 422 for anyone outside the team."""
    if assignee_id is not None:
        if assignee_id not in await teams.members_of(session, SUPPORT):
            raise _refuse(422, "Cases go to members of the support team.", "NOT_IN_TEAM", {"field": "assignee_id"})
    case.assignee_id = assignee_id
    case.updated_at = now or _now()


def clean_tags(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        t = " ".join(str(v).split()).lower()[:40]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


async def merge(session, case: SupportCases, into: SupportCases, now: Optional[datetime] = None) -> SupportCases:
    """Move every message, file and follow-up of `case` into `into`; `case`
    closes as merged and points at it. The caller commits."""
    from app.api.leads.models import FollowUps

    now = now or _now()
    into = await follow_merges(session, into)
    if into.id == case.id:
        raise _refuse(422, "A case can't be merged into itself.", "SAME_CASE")
    moved = int((await session.execute(
        update(SupportMessages).where(SupportMessages.case_id == case.id).values(case_id=into.id)
        .execution_options(synchronize_session=False)
    )).rowcount or 0)
    await session.execute(
        update(SupportAttachments).where(SupportAttachments.case_id == case.id).values(case_id=into.id)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(FollowUps).where(FollowUps.case_id == case.id).values(case_id=into.id).execution_options(synchronize_session=False)
    )
    into.message_count = (into.message_count or 0) + moved
    into.tags = clean_tags([*(into.tags or []), *(case.tags or [])])
    for when in ("last_message_at", "last_inbound_at", "last_outbound_at"):
        a, b = getattr(into, when), getattr(case, when)
        if b is not None and (a is None or b > a):
            setattr(into, when, b)
    if case.first_replied_at and (into.first_replied_at is None):
        into.first_replied_at = case.first_replied_at
    case.message_count = 0
    case.merged_into_id = into.id
    await set_status(session, case, CLOSED, now=now, reason="merged")
    await system_line(session, case, f"Merged into case #{into.number}.", now)
    await system_line(session, into, f"Case #{case.number} was merged into this one.", now)
    if into.status in (RESOLVED, CLOSED):
        await set_status(session, into, OPEN, now=now)
    return into


# ─── The lists ───────────────────────────────────────────────────────────


TABS = ("mine", "unassigned", "open", "waiting", "resolved", "all", "spam")


def _not_spam():
    return ~SupportCases.tags.contains([SPAM_TAG])


def overdue_clause(now: datetime):
    C = SupportCases
    return or_(
        and_(C.first_replied_at.is_(None), C.status.in_(ACTIVE), C.first_reply_due < now),
        and_(C.status.in_(UNFINISHED), C.resolution_due < now),
    )


def is_overdue(case: SupportCases, now: datetime) -> bool:
    if case.first_replied_at is None and case.status in ACTIVE and case.first_reply_due and case.first_reply_due < now:
        return True
    return bool(case.status in UNFINISHED and case.resolution_due and case.resolution_due < now)


def tab_clause(tab: str, user_id: int):
    C = SupportCases
    if tab == "mine":
        return and_(C.assignee_id == user_id, C.status.in_(UNFINISHED), _not_spam())
    if tab == "unassigned":
        return and_(C.assignee_id.is_(None), C.status.in_(ACTIVE), _not_spam())
    if tab == "open":
        return and_(C.status.in_(ACTIVE), _not_spam())
    if tab == "waiting":
        return and_(C.status == WAITING, _not_spam())
    if tab == "resolved":
        return and_(C.status == RESOLVED, _not_spam())
    if tab == "spam":
        return C.tags.contains([SPAM_TAG])
    return _not_spam()


def filters(*, topic=None, priority=None, assignee=None, q=None, age_days=None, status=None, now=None) -> list:
    C = SupportCases
    now = now or _now()
    out = []
    if topic:
        out.append(C.topic == topic)
    if priority:
        out.append(C.priority == priority)
    if status:
        out.append(C.status == status)
    if assignee == "none":
        out.append(C.assignee_id.is_(None))
    elif assignee not in (None, ""):
        try:
            out.append(C.assignee_id == int(assignee))
        except (TypeError, ValueError):
            raise _refuse(422, "assignee is an admin's id or none.", "INVALID_FILTER", {"field": "assignee"}) from None
    if age_days:
        out.append(C.created_at <= now - timedelta(days=int(age_days)))
    words = (q or "").strip()
    if words:
        digits = words.lstrip("#")
        like = f"%{words.replace('%', '').replace('_', '')}%"
        clause = or_(C.subject.ilike(like), C.requester_name.ilike(like), C.requester_email.ilike(like))
        if digits.isdigit() and len(digits) <= 12:
            clause = or_(clause, C.number == int(digits))
        out.append(clause)
    return out


async def counts(session, user_id: int) -> dict:
    C = SupportCases
    row = (await session.execute(
        select(
            func.count().filter(tab_clause("mine", user_id)),
            func.count().filter(tab_clause("unassigned", user_id)),
            func.count().filter(tab_clause("open", user_id)),
            func.count().filter(tab_clause("waiting", user_id)),
            func.count().filter(tab_clause("resolved", user_id)),
        ).select_from(C)
    )).one()
    return dict(zip(("mine", "unassigned", "open", "waiting", "resolved"), (int(v) for v in row)))


def requester_out(case: SupportCases) -> dict:
    ident = case.requester_club_id if case.requester_kind == "club" else case.requester_user_id
    return {"kind": case.requester_kind, "id": ident, "name": case.requester_name, "email": case.requester_email}


def row_out(case: SupportCases, names: dict, now: datetime) -> dict:
    return {
        "id": case.id, "number": case.number, "subject": case.subject, "status": case.status, "priority": case.priority,
        "topic": case.topic, "channel": case.channel, "requester": requester_out(case),
        "assignee": {"id": case.assignee_id, "name": names.get(case.assignee_id)} if case.assignee_id else None,
        "tags": list(case.tags or []), "created_at": case.created_at, "updated_at": case.updated_at,
        "first_reply_due": case.first_reply_due, "resolution_due": case.resolution_due,
        "overdue": is_overdue(case, now), "flags": list(case.flags or []),
    }


async def list_cases(session, user_id: int, *, tab: str = "open", page: int = 1, page_size: int = 25, **f) -> dict:
    if tab not in TABS:
        raise _refuse(422, f"tab is one of {', '.join(TABS)}.", "INVALID_FILTER", {"field": "tab"})
    now = _now()
    C = SupportCases
    where = [tab_clause(tab, user_id), *filters(now=now, **f)]
    total = int(await session.scalar(select(func.count()).select_from(C).where(*where)) or 0)
    order = (C.updated_at.desc(), C.id.desc()) if tab in ("resolved", "all", "spam") else (
        func.coalesce(C.first_reply_due, C.created_at).asc(), C.id.asc()) if tab in ("unassigned",) else (C.updated_at.desc(), C.id.desc())
    rows = (await session.scalars(
        select(C).where(*where).order_by(*order).limit(page_size).offset((page - 1) * page_size)
    )).all()
    names = await teams.names(session, [r.assignee_id for r in rows])
    return {"items": [row_out(r, names, now) for r in rows], "total": total, "counts": await counts(session, user_id),
            "page": page, "page_size": page_size}


async def export_rows(session, user_id: int, *, tab: str = "all", **f):
    """Every case the filters match, as the sheet's rows (header first)."""
    now = _now()
    C = SupportCases
    where = [tab_clause(tab, user_id), *filters(now=now, **f)]
    yield ["Number", "Subject", "Status", "Priority", "Topic", "Channel", "Requester", "Requester email", "Requester kind",
           "Assignee", "Tags", "Created", "Updated", "First reply due", "First replied", "Resolution due", "Resolved",
           "Overdue", "Flags"]
    last_id = None
    while True:
        stmt = select(C).where(*where).order_by(C.id.desc()).limit(500)
        if last_id is not None:
            stmt = stmt.where(C.id < last_id)
        rows = (await session.scalars(stmt)).all()
        if not rows:
            break
        names = await teams.names(session, [r.assignee_id for r in rows])
        for r in rows:
            yield [
                f"#{r.number}", r.subject, r.status, r.priority, r.topic or "", r.channel, r.requester_name or "",
                r.requester_email or "", r.requester_kind, names.get(r.assignee_id, ""), ", ".join(r.tags or []),
                r.created_at, r.updated_at, r.first_reply_due, r.first_replied_at, r.resolution_due, r.resolved_at,
                is_overdue(r, now), ", ".join(r.flags or []),
            ]
        last_id = rows[-1].id


# ─── Messages out ────────────────────────────────────────────────────────


async def attachments_by_message(session, message_ids: list[int]) -> dict[int, list[SupportAttachments]]:
    if not message_ids:
        return {}
    rows = (await session.scalars(
        select(SupportAttachments).where(SupportAttachments.message_id.in_(message_ids)).order_by(SupportAttachments.id)
    )).all()
    out: dict[int, list[SupportAttachments]] = {}
    for a in rows:
        out.setdefault(a.message_id, []).append(a)
    return out


async def messages_of(session, case_id: int, *, kinds: Optional[tuple] = None) -> list[SupportMessages]:
    stmt = select(SupportMessages).where(SupportMessages.case_id == case_id)
    if kinds:
        stmt = stmt.where(SupportMessages.kind.in_(kinds))
    return list((await session.scalars(stmt.order_by(SupportMessages.created_at, SupportMessages.id))).all())


def message_out(m: SupportMessages, files: list[SupportAttachments], url_of, *, bodies: bool = True, team: bool = True) -> dict:
    out = {
        "id": m.id, "kind": m.kind, "author_name": m.author_name, "created_at": m.created_at,
        "body_text": m.body_text if bodies else None, "body_html": m.body_html if bodies else None,
        "attachments": [{"id": a.id, "name": a.filename, "size": a.size_bytes, "content_type": a.content_type, "url": url_of(a)} for a in files]
        if bodies else [],
    }
    if team:
        out.update({
            "author_user_id": m.author_user_id, "from_address": m.from_address, "delivery_status": m.delivery_status,
            "delivery_error": m.delivery_error, "hidden": not bodies,
        })
    return out
