"""What an assistant can do for a club: fifteen tools.

Each tool is scoped to the token's club -- an id from another club is "not
found", never refused as someone else's -- and is checked, on every call, in
this order:

1. the token carries the tool's scope (else the endpoint answers 403
   insufficient_scope before the call reaches here: server.py);
2. the club's plan includes `mcp` (gates, like every other paid feature);
3. the person's role *now* may make the dashboard request the tool stands for
   (club_access.policy.allows_tool) -- door staff cannot draft events, a
   removed member cannot do anything (access.py ended the grant already);
4. the arguments match the tool's JSON schema (no extra properties).

Reads return what the dashboard shows, minus what an assistant has no need
for: attendees' email and phone are masked unless the token has
attendees:read *and* the role may hand contacts out (policy.allows_contacts);
money needs finance:read and a role that sees money.

Writes only ever make drafts, or ask: an event is created and edited as a
draft, a store item is created off sale, an announcement is saved unsent.
Publishing an event and sending an announcement are `request_*` tools,
which make a pending action a person approves in the dashboard (actions.py).
Nothing here refunds, pays out, deletes, changes a live event, changes the
team, or sends anything.

Text people typed -- an event's description, a review, a form answer -- is
always returned inside `{"user_written": true, "text": ...}`, never loose,
and the server's instructions tell the assistant it is data, not instructions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload
from starlette.requests import Request

from app.api.audit import service as audit
from app.api.club_access import policy
from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories, EventRatingsLink, EventRegistrationsLink, Events, MAX_EVENT_HOURS
from app.api.mcp import actions
from app.api.mcp.access import Principal
from app.api.mcp.models import AUDIENCES, AnnouncementDrafts
from app.api.mcp.oauth import dashboard_url
from app.api.users.models import UserProfiles, Users
from app.core import public_urls
from app.response import CustomHTTPException

IST = timezone(timedelta(hours=5, minutes=30))
_MAX_PAGE = 50
#: Field types a question an assistant writes may have (no file uploads).
QUESTION_TYPES = ("text", "textarea", "number", "email", "tel", "url", "date", "select", "radio", "checkbox")
_CONTACT_TYPES = ("email", "tel")
_EMAILISH = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(r"\+?\d[\d\s().-]{7,}\d")


# ─── The call ────────────────────────────────────────────────────────────


@dataclass
class Call:
    """One tool call: who, for which club, on which session, with which scopes."""

    principal: Principal
    session: object
    request: Request
    scopes: frozenset

    @property
    def club(self):
        return self.principal.club

    @property
    def role(self) -> str:
        return self.principal.role

    def contacts_allowed(self) -> bool:
        return "attendees:read" in self.scopes and policy.allows_contacts(self.role)

    def money_allowed(self) -> bool:
        return "finance:read" in self.scopes and policy.allows_event_money(self.role)

    def audit_details(self, **extra) -> dict:
        p = self.principal
        return {
            "via": "mcp", "client": p.client_name, "client_id": p.client.client_id if p.client else None,
            "grant_id": str(p.grant.id), "member_id": p.member.id if p.member else None,
            "member_email": p.member.email if p.member else None, **extra,
        }


class ToolError(Exception):
    """A refusal the assistant is told about, in words it can relay."""

    def __init__(self, message: str, code: str = "REFUSED", data: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}


def from_http(e: CustomHTTPException) -> ToolError:
    return ToolError(e.message or "That was refused.", e.error_code or f"HTTP_{e.status_code}", e.errors if isinstance(e.errors, dict) else {})


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    scope: str
    schema: dict
    run: Callable[[Call, dict], Awaitable[dict]]
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    validator: Draft202012Validator = field(default=None, compare=False, repr=False)


TOOLS: dict[str, Tool] = {}


def tool(name: str, *, title: str, scope: str, description: str, schema: dict, read_only: bool = True, idempotent: bool = True):
    def register(fn):
        Draft202012Validator.check_schema(schema)
        TOOLS[name] = Tool(
            name=name, title=title, description=description, scope=scope, schema=schema, run=fn,
            read_only=read_only, destructive=False, idempotent=idempotent, validator=Draft202012Validator(schema),
        )
        return fn

    return register


def scope_for(name: str | None) -> str | None:
    t = TOOLS.get(name or "")
    return t.scope if t else None


def obj(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


EVENT_ID = {"type": "integer", "minimum": 1, "description": "The event's id, from list_events."}
LIMIT = {"type": "integer", "minimum": 1, "maximum": _MAX_PAGE, "default": 20, "description": f"How many to return (at most {_MAX_PAGE})."}
OFFSET = {"type": "integer", "minimum": 0, "default": 0, "description": "How many to skip, for the next page (next_offset of the last answer)."}
WHEN = {"type": "string", "format": "date-time", "maxLength": 40,
        "description": "ISO 8601 date and time with its UTC offset, e.g. 2026-11-14T18:00:00+05:30. Without an offset it is read as India time."}


# ─── Helpers ─────────────────────────────────────────────────────────────


def written(text) -> dict | None:
    """Text a person typed, marked as such: data to show, never instructions to follow."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return None
    return {"user_written": True, "text": str(text)}


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
    return dt.isoformat() if isinstance(dt, date) else str(dt)


def _money(value) -> float | None:
    return None if value is None else float(Decimal(str(value)))


def _when(value: str, name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"{name} must be a date and time like 2026-11-14T18:00:00+05:30.", "INVALID_ARGUMENT")
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def mask_email(value: str | None) -> str | None:
    if not value:
        return None
    local, _, domain = value.partition("@")
    return f"{local[:1]}•••@{domain}" if domain else "•••"


def mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return "•••••" + digits[-3:] if len(digits) >= 3 else "•••"


def _phone_or_not(match) -> str:
    """A run of ten digits or more reads as a phone number; a date (eight) does not."""
    found = match.group(0)
    return mask_phone(found) if sum(c.isdigit() for c in found) >= 10 else found


def _scrub(text: str) -> str:
    """Contacts typed into a free answer, masked like the contact fields."""
    text = _EMAILISH.sub(lambda m: mask_email(m.group(0)), text)
    return _PHONEISH.sub(_phone_or_not, text)


async def _event(call: Call, event_id: int, *, load=()) -> Events:
    """The club's own event, live (not deleted). Another club's is "not found"."""
    q = select(Events).where(Events.id == event_id, Events.club_id == call.club.id, Events.is_deleted == False)  # noqa: E712
    if load:
        q = q.options(*load)
    event = await call.session.scalar(q.execution_options(populate_existing=True))
    if event is None:
        raise ToolError("There is no event with that id in this club.", "EVENT_NOT_FOUND")
    return event


def _status(event: Events) -> str:
    return (event.lifecycle or EventStatus.published).value


def _ends(event: Events) -> datetime:
    return event.event_datetime + timedelta(hours=float(event.duration or 0))


def _links(event: Events) -> dict:
    out = {"dashboard_url": dashboard_url(f"/events/{event.id}")}
    if _status(event) != "draft":
        out["public_url"] = public_urls.event_url(event.slug)
    return out


def _require_draft(event: Events) -> None:
    if event.lifecycle is not EventStatus.draft:
        raise ToolError(
            f"“{event.name}” is not a draft (it is {_status(event)}), so it can't be changed from an assistant. "
            f"Edit it in the dashboard: {dashboard_url(f'/events/{event.id}/edit')}",
            "EVENT_NOT_DRAFT",
            {"dashboard_url": dashboard_url(f"/events/{event.id}/edit"), "status": _status(event)},
        )


def _page(limit: int, offset: int, total: int) -> dict:
    return {"total": total, "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < total else None}


async def _record(call: Call, action: str, *, entity_type: str, entity_id, summary: str, **details) -> None:
    """The audit line every write leaves: the club account, via mcp, which app, which member."""
    await audit.record(
        call.session, actor=call.principal.user, action=action, entity_type=entity_type, entity_id=entity_id,
        summary=summary[:255], details=call.audit_details(**details), request=call.request,
    )


# ─── Reads ───────────────────────────────────────────────────────────────


@tool(
    "list_events",
    title="List events",
    scope="events:read",
    description=(
        "List the club's events: upcoming (not over yet, drafts excluded), past (over), draft (not published) or all. "
        "Each with its status, dates, venue, capacity, registrations and check-ins. Paged: pass next_offset to get more."
    ),
    schema=obj({
        "status": {"type": "string", "enum": ["upcoming", "past", "draft", "all"], "default": "upcoming"},
        "search": {"type": "string", "maxLength": 100, "description": "Words in the event's name."},
        "limit": LIMIT, "offset": OFFSET,
    }),
)
async def list_events(call: Call, args: dict) -> dict:
    from app.api.clubs import events_list

    status, limit, offset = args.get("status", "upcoming"), args.get("limit", 20), args.get("offset", 0)
    # The club's own events: an assistant does not act on events it co-hosts (app/api/cohosts).
    rows, total = await events_list.list_events(call.session, call.club.id, scope=status, search=args.get("search"), limit=limit, offset=offset, role="hosted")
    events = [
        {
            "id": r["id"], "name": r["name"], "status": r["status"], "starts_at": _iso(r.get("event_datetime")),
            "ends_at": _iso(r.get("ends_at")), "venue": r.get("location_name"), "online": bool(r.get("is_online")),
            "paid": bool(r.get("has_fee")), "fee": _money(r.get("reg_fee")) if r.get("has_fee") else None,
            "capacity": r.get("max_participants"), "registrations": r.get("registration_count", 0),
            "checked_in": max(r.get("checked_in", 0) or 0, r.get("attended_count", 0) or 0),
            "registration_open": bool(r.get("registration_open")), "dashboard_url": dashboard_url(f"/events/{r['id']}"),
        }
        for r in rows
    ]
    return {"status": status, **_page(limit, offset, total), "events": events}


@tool(
    "get_event",
    title="Get an event",
    scope="events:read",
    description="Everything about one event: dates, venue, fee, capacity, registration window, contact, description, registration form questions, speakers and the store items it offers.",
    schema=obj({"event_id": EVENT_ID}, ("event_id",)),
)
async def get_event(call: Call, args: dict) -> dict:
    from app.api.store import service as store

    event = await _event(call, args["event_id"], load=(joinedload(Events.category), selectinload(Events.speakers)))
    questions = [
        {"key": q.get("key"), "label": q.get("label"), "type": q.get("field_type"), "required": bool(q.get("required", True)), "options": q.get("options")}
        for q in (event.additional_details or []) if isinstance(q, dict)
    ]
    return {
        "id": event.id, "name": event.name, "status": _status(event), "starts_at": _iso(event.event_datetime), "ends_at": _iso(_ends(event)),
        "duration_hours": float(event.duration or 0), "category": event.category.name if event.category else None,
        "venue": {"name": event.location_name, "map_link": event.location_link, "online": bool(event.is_online), "online_url": event.url},
        "paid": bool(event.has_fee), "fee": _money(event.reg_fee) if event.has_fee else None, "capacity": event.max_participants,
        "registration": {"opens_at": _iso(event.reg_startdate), "closes_at": _iso(event.reg_enddate)},
        "contact": {"name": event.contact_name, "email": event.contact_email, "phone": event.contact_phone},
        "prize": _money(event.prize_amount) if event.has_prize else None,
        "about": written(event.about), "guidelines": written(event.event_guidelines),
        "questions": questions,
        "speakers": [{"name": s.name, "designation": s.designation, "bio": written(s.bio)} for s in (event.speakers or []) if not getattr(s, "is_deleted", False)],
        "store": {"enabled": bool(event.merch_enabled), "offered_item_ids": [str(i) for i in await store.offered_ids(call.session, event)]},
        **_links(event),
    }


@tool(
    "event_stats",
    title="Event figures",
    scope="events:read",
    description="An event's numbers: registrations, check-ins, paying guests and capacity, registrations under review and their outcomes, by status, when the club reviews every registration, and its money (tickets, store, refunds, owed) when the connection has finance:read and your role sees money.",
    schema=obj({"event_id": EVENT_ID}, ("event_id",)),
)
async def event_stats(call: Call, args: dict) -> dict:
    from app.api.checkins import service as checkins

    event = await _event(call, args["event_id"])
    counts = await checkins.counters(call.session, event.id)
    regs = EventRegistrationsLink
    attended, paying = (await call.session.execute(
        select(func.count().filter(regs.is_attended == True), func.count().filter(regs.is_paid == True))  # noqa: E712
        .where(regs.event_id == event.id, regs.is_deleted == False)  # noqa: E712
    )).one()
    registered, checked_in = counts["registered"], max(counts["checked_in"], attended or 0)
    capacity = event.max_participants
    out = {
        "event_id": event.id, "name": event.name, "status": _status(event), "starts_at": _iso(event.event_datetime),
        "capacity": capacity, "registrations": registered, "checked_in": checked_in, "paying": paying or 0,
        "filled_percent": round(100 * registered / capacity) if capacity else None,
        "checked_in_percent": round(100 * checked_in / registered) if registered else None,
        "revenue": None,
    }
    # Confirm each registration (app/api/approvals): the requests by status, one grouped count.
    from app.api.approvals import service as approvals

    asked = await approvals.counts(call.session, event.id)
    out["requires_approval"] = bool(event.requires_approval)
    out["requests"] = asked if (asked["total"] or event.requires_approval) else None
    if call.money_allowed():
        from app.api.finance import service as finance

        figures = (await finance.event_summary(call.session, event))["figures"]
        out["revenue"] = {
            "currency": "INR", "tickets": figures["tickets"], "store": figures["merch"], "other_income": figures["other_income"],
            "refunds": figures["refunds"], "expenses": figures["expenses"], "net": figures["net"],
            "outstanding": figures["outstanding"], "outstanding_count": figures["outstanding_count"],
        }
    else:
        out["revenue_hidden"] = "Money needs the finance:read scope and a role that sees the club's money (owner or admin)."
    return out


@tool(
    "list_registrations",
    title="List registrations",
    scope="events:read",
    description=(
        "The people registered for an event: name, college, status (paid, unpaid, free, refunded, revoked), whether they "
        "checked in, and their answers to the registration form. Email and phone are masked unless the connection has "
        "attendees:read and your role may share contacts. Paged."
    ),
    schema=obj({
        "event_id": EVENT_ID,
        "checked_in": {"type": "boolean", "description": "Only those who checked in (true) or did not (false)."},
        "search": {"type": "string", "maxLength": 100, "description": "Part of a name."},
        "limit": LIMIT, "offset": OFFSET,
    }, ("event_id",)),
)
async def list_registrations(call: Call, args: dict) -> dict:
    from app.api.payments.models import PaymentLogs, PaymentOrders, PaymentStatus

    event = await _event(call, args["event_id"])
    limit, offset = args.get("limit", 20), args.get("offset", 0)
    contacts = call.contacts_allowed()
    reg = EventRegistrationsLink
    where = [reg.event_id == event.id, reg.is_deleted == False]  # noqa: E712
    if args.get("checked_in") is not None:
        where.append(reg.is_attended == bool(args["checked_in"]))
    if args.get("search"):
        pattern = f"%{args['search'].strip()}%"
        # By name only, unless contacts may be shown: searching by an address
        # would tell whether it registered.
        where.append(reg.full_name.ilike(pattern) | reg.email.ilike(pattern) | reg.phone.ilike(pattern) if contacts else reg.full_name.ilike(pattern))
    total = await call.session.scalar(select(func.count()).select_from(reg).where(*where)) or 0
    rows = list((await call.session.scalars(
        select(reg).where(*where)
        .options(joinedload(reg.user).joinedload(Users.profile).joinedload(UserProfiles.org))
        .order_by(func.lower(reg.full_name), reg.id).limit(limit).offset(offset)
    )).unique().all())
    receipts = [r.payment_receipt for r in rows if r.payment_receipt]
    refunded = set()
    if receipts:
        refunded = set((await call.session.scalars(
            select(PaymentOrders.receipt).join(PaymentLogs, PaymentLogs.order_id == PaymentOrders.id)
            .where(PaymentLogs.status == PaymentStatus.refunded, PaymentOrders.receipt.in_(receipts))
        )).all())
    questions = {q.get("key"): q for q in (event.additional_details or []) if isinstance(q, dict)}

    def status_of(r) -> str:
        if r.ticket_revoked_at is not None:
            return "revoked"
        if r.is_paid:
            return "paid"
        if r.payment_receipt and r.payment_receipt in refunded:
            return "refunded"
        return "unpaid" if event.has_fee else "free"

    def answers_of(r) -> list:
        out = []
        for key, value in (r.additional_details or {}).items() if isinstance(r.additional_details, dict) else []:
            q = questions.get(key) or {}
            text = ", ".join(str(v) for v in value) if isinstance(value, list) else ("" if value is None else str(value))
            if not contacts:
                text = mask_email(text) if q.get("field_type") == "email" else mask_phone(text) if q.get("field_type") == "tel" else _scrub(text)
            out.append({"question": q.get("label") or key, "answer": written(text)})
        return out

    people = []
    for r in rows:
        profile = getattr(r.user, "profile", None) if r.user else None
        college = (profile.org.name if profile is not None and profile.org is not None else None) or r.institution
        people.append({
            "name": r.full_name, "college": college, "status": status_of(r), "checked_in": bool(r.is_attended),
            "checked_in_at": _iso(r.attended_on), "registered_at": _iso(r.created_at),
            "email": r.email if contacts else mask_email(r.email), "phone": r.phone if contacts else mask_phone(r.phone),
            "answers": answers_of(r),
        })
    return {
        "event_id": event.id, "event": event.name, **_page(limit, offset, total),
        "contacts": "shown" if contacts else "masked (needs the attendees:read scope and an owner, admin or editor role)",
        "registrations": people,
    }


@tool(
    "finance_summary",
    title="Finance summary",
    scope="finance:read",
    description=(
        "The club's money for a stretch of months: income (tickets, store, other), refunds, expenses, net and what is "
        "still owed, in total, month by month and event by event. An event's money sits in the month it was held. "
        "Default: four months back to the end of next month."
    ),
    schema=obj({
        "from_month": {"type": "string", "pattern": r"^\d{4}-(0[1-9]|1[0-2])$", "description": "First month, YYYY-MM."},
        "to_month": {"type": "string", "pattern": r"^\d{4}-(0[1-9]|1[0-2])$", "description": "Last month, YYYY-MM."},
    }),
)
async def finance_summary(call: Call, args: dict) -> dict:
    from app.api.finance import service as finance

    frm = to = None
    if args.get("from_month"):
        y, m = map(int, args["from_month"].split("-"))
        frm = date(y, m, 1)
    if args.get("to_month"):
        y, m = map(int, args["to_month"].split("-"))
        to = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    try:
        frm, to = finance.resolve_period(frm, to)
    except CustomHTTPException as e:
        raise from_http(e)
    if (to.year - frm.year) * 12 + to.month - frm.month >= 24:
        raise ToolError("Ask for at most 24 months at a time.", "PERIOD_TOO_LONG")
    s = await finance.club_summary(call.session, call.club, frm, to)
    f = s["figures"]
    return {
        "currency": "INR", "period": {"from": frm.isoformat(), "to": to.isoformat()},
        "totals": {k: f.get(k) for k in ("gross", "tickets", "merch", "other_income", "refunds", "expenses", "net", "margin", "outstanding", "outstanding_count", "regs", "paying")},
        "events": f.get("events"),
        "by_month": s["by_month"],
        "by_event": [
            {k: e.get(k) for k in ("id", "name", "starts_at", "status", "regs", "paying", "tickets", "merch", "other_income", "refunds", "expenses", "net", "outstanding")}
            for e in s["by_event"]
        ],
    }


@tool(
    "list_store_items",
    title="List store items",
    scope="store:read",
    description="The club's merchandise catalogue: each item's price, stock and what is left (per size), whether it is on sale, and which events offer it.",
    schema=obj({}),
)
async def list_store_items(call: Call, args: dict) -> dict:
    from app.api.store import service as store

    money = call.money_allowed()
    items = []
    for m in await store.club_items(call.session, call.club.id):
        item = {
            "id": m["id"], "name": m["name"], "description": written(m.get("description")), "price": m["price"],
            "on_sale": bool(m["on_sale"]), "stock": m.get("stock"), "sold": m["sold"], "held": m["held"], "left": m["left"],
            "sizes": [{"label": v["label"], "stock": v["stock"], "sold": v["sold"], "left": v["left"]} for v in m.get("variants") or []],
            "offered_on": [{"event_id": e["id"], "name": e["name"]} for e in m.get("events") or []],
        }
        if money:
            item["earned"] = m.get("earned")
        items.append(item)
    return {"currency": "INR", "items": items, "dashboard_url": dashboard_url("/merchandise")}


@tool(
    "list_feedback",
    title="List feedback",
    scope="events:read",
    description="What people said about an event: its star ratings and reviews (newest first, paged), and the results of its feedback survey when one was sent.",
    schema=obj({"event_id": EVENT_ID, "limit": LIMIT, "offset": OFFSET}, ("event_id",)),
)
async def list_feedback(call: Call, args: dict) -> dict:
    from app.api.checkins import service as checkins
    from app.api.live import service as live
    from app.api.live.models import FeedbackSurveys

    event = await _event(call, args["event_id"])
    limit, offset = args.get("limit", 20), args.get("offset", 0)
    rating = EventRatingsLink
    live_rows = [rating.event_id == event.id, rating.is_deleted == False]  # noqa: E712
    count, average = (await call.session.execute(select(func.count(), func.avg(rating.rating)).where(*live_rows))).one()
    rows = list((await call.session.scalars(
        select(rating).where(*live_rows).options(joinedload(rating.user)).order_by(rating.created_at.desc()).limit(limit).offset(offset)
    )).all())
    reviews = [
        {"rating": r.rating, "review": written(r.review), "by": ((r.user.full_name or "").split(" ")[0] or None) if r.user else None, "at": _iso(r.created_at)}
        for r in rows
    ]
    survey = await call.session.scalar(select(FeedbackSurveys).where(FeedbackSurveys.event_id == event.id))
    results = None
    if survey is not None:
        invited = (await checkins.counters(call.session, event.id))["checked_in"]
        r = await live._results(call.session, survey, invited)
        results = {
            "sent_at": _iso(survey.sent_at), "responses": r["responses"], "invited": r["invited"], "nps": r["nps"],
            "averages": r["averages"], "comments": [written(t) for t in r["texts"]],
        }
    return {
        "event_id": event.id, "event": event.name,
        "ratings": {"count": count or 0, "average": round(float(average), 2) if average is not None else None},
        **_page(limit, offset, count or 0), "reviews": reviews, "survey": results,
    }


@tool(
    "list_team",
    title="List the team",
    scope="team:read",
    description="Who can sign in to the club's dashboard: the club's own account, and each team member with their role (admin, editor, door) and whether they have joined.",
    schema=obj({}),
)
async def list_team(call: Call, args: dict) -> dict:
    from app.api.club_access import service as club_access

    members = await club_access.members_of(call.session, call.club)
    return {
        "owner": {"account": call.principal.user.email, "role": "owner"},
        "members": [
            {"name": m.full_name, "email": m.email, "role": m.role, "status": m.status, "last_seen_at": _iso(m.last_seen_at)}
            for m in members
        ],
        "roles": {k: v[1] for k, v in policy.ROLE_INFO.items()},
    }


# ─── Writes: drafts only ─────────────────────────────────────────────────

QUESTION = obj({
    "label": {"type": "string", "minLength": 1, "maxLength": 200},
    "type": {"type": "string", "enum": list(QUESTION_TYPES)},
    "required": {"type": "boolean", "default": True},
    "options": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 100}, "maxItems": 30,
                "description": "The choices, for select, radio and checkbox."},
}, ("label", "type"))

EVENT_FIELDS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 100},
    "starts_at": WHEN,
    "duration_hours": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_EVENT_HOURS},
    "category": {"type": "string", "maxLength": 50, "description": "The event's type, by name (e.g. Workshop). Left out: the first type."},
    "about": {"type": "string", "maxLength": 5000, "description": "The description guests read."},
    "venue": {"type": "string", "maxLength": 200},
    "map_link": {"type": "string", "maxLength": 500, "description": "A maps link to the venue."},
    "online": {"type": "boolean"},
    "online_url": {"type": "string", "maxLength": 500},
    "capacity": {"type": "integer", "minimum": 1, "maximum": 1000000},
    "fee": {"type": "number", "minimum": 0, "maximum": 1000000, "description": "Ticket price in rupees; 0 for free. Paid tickets need the plan's paid tickets."},
    "registration_opens_at": WHEN,
    "registration_closes_at": WHEN,
    "contact_name": {"type": "string", "maxLength": 100},
    "contact_email": {"type": "string", "maxLength": 100},
    "contact_phone": {"type": "string", "maxLength": 20},
    "guidelines": {"type": "string", "maxLength": 5000},
    "questions": {"type": "array", "items": QUESTION, "maxItems": 20, "description": "The registration form's questions (replaces all of them)."},
}
#: What update_event_draft may empty. A list rather than nulls: one `type`
#: per property keeps the schemas portable to clients that allow no unions.
CLEARABLE = ("about", "venue", "map_link", "online_url", "capacity", "registration_opens_at", "registration_closes_at",
             "contact_name", "contact_email", "contact_phone", "guidelines")


async def _category(call: Call, name: str | None) -> EventCategories:
    rows = list((await call.session.scalars(
        select(EventCategories).where(EventCategories.is_deleted == False).order_by(EventCategories.id)  # noqa: E712
    )).all())
    if not rows:
        raise ToolError("There are no event types to file this event under. Create the event in the dashboard.", "NO_CATEGORIES")
    if not name:
        return rows[0]
    for row in rows:
        if row.name.strip().lower() == name.strip().lower():
            return row
    raise ToolError(f"There is no event type called “{name}”. The types are: {', '.join(r.name for r in rows)}.", "UNKNOWN_CATEGORY")


def _questions(items: list) -> list[dict]:
    out, seen = [], set()
    for q in items:
        kind = q["type"]
        options = [o.strip() for o in q.get("options") or [] if o.strip()]
        if kind in ("select", "radio", "checkbox") and not options:
            raise ToolError(f"The question “{q['label']}” needs options.", "INVALID_ARGUMENT")
        base = re.sub(r"[^a-z0-9]+", "_", q["label"].lower()).strip("_")[:40] or "question"
        key, n = base, 2
        while key in seen:
            key, n = f"{base}_{n}", n + 1
        seen.add(key)
        out.append({"key": key, "label": q["label"].strip(), "field_type": kind, "required": bool(q.get("required", True)), "options": options or None})
    return out


def _apply(event: Events, args: dict) -> list[str]:
    """Write the given fields onto a draft. Returns the names changed."""
    from app.api.discover import geo

    changed = []
    for name in args.get("clear") or []:
        args = {**args, name: None}
    simple = {
        "name": "name", "about": "about", "venue": "location_name", "online": "is_online", "online_url": "url",
        "capacity": "max_participants", "contact_name": "contact_name", "contact_email": "contact_email",
        "contact_phone": "contact_phone", "guidelines": "event_guidelines",
    }
    for arg, column in simple.items():
        if arg in args and arg != "clear":
            value = args[arg]
            setattr(event, column, value.strip() if isinstance(value, str) else value)
            changed.append(arg)
    if "starts_at" in args:
        event.event_datetime = _when(args["starts_at"], "starts_at")
        if event.event_datetime <= datetime.now(timezone.utc):
            raise ToolError("starts_at is in the past; a draft is for an event still to come.", "INVALID_ARGUMENT")
        changed.append("starts_at")
    if "duration_hours" in args:
        # At most MAX_EVENT_HOURS: the schema's maximum, as events.service.check_duration has it.
        event.duration = float(args["duration_hours"])
        changed.append("duration_hours")
    if "fee" in args:
        event.has_fee = args["fee"] > 0
        event.reg_fee = Decimal(str(args["fee"])) if args["fee"] > 0 else None
        changed.append("fee")
    for arg, column in (("registration_opens_at", "reg_startdate"), ("registration_closes_at", "reg_enddate")):
        if arg in args:
            setattr(event, column, _when(args[arg], arg) if args[arg] else (datetime.now(timezone.utc) if column == "reg_startdate" else None))
            changed.append(arg)
    if "map_link" in args:
        geo.apply_place(event, link=(args["map_link"] or "").strip() or None)
        changed.append("map_link")
    if "questions" in args:
        event.additional_details = _questions(args["questions"])
        changed.append("questions")
    if event.reg_enddate is not None and event.reg_enddate > event.event_datetime + timedelta(hours=float(event.duration or 0)):
        raise ToolError("Registration can't close after the event ends.", "INVALID_ARGUMENT")
    return changed


def _draft_out(event: Events, changed: list[str] | None = None) -> dict:
    out = {
        "event_id": event.id, "name": event.name, "status": _status(event), "starts_at": _iso(event.event_datetime),
        "dashboard_url": dashboard_url(f"/events/{event.id}"), "edit_url": dashboard_url(f"/events/{event.id}/edit"),
        "next_step": "It is a draft: nobody can see it. To put it live, call request_publish_event and have someone approve it in the dashboard.",
    }
    if changed is not None:
        out["changed"] = changed
    return out


@tool(
    "create_event_draft",
    title="Create a draft event",
    scope="events:write",
    description=(
        "Create a new event as an unpublished DRAFT: nobody outside the club sees it and nobody is told. Returns its id "
        "and its dashboard link. To publish it later, use request_publish_event (a person approves it)."
    ),
    schema=obj(EVENT_FIELDS, ("name", "starts_at", "duration_hours")),
    read_only=False,
    idempotent=False,
)
async def create_event_draft(call: Call, args: dict) -> dict:
    from app.api.subscriptions import gates
    from app.core.utils.keys import generate_slug

    fee = args.get("fee") or 0
    try:
        await gates.check_event_create(call.session, call.club.id, has_fee=fee > 0)
    except CustomHTTPException as e:
        raise from_http(e)
    category = await _category(call, args.get("category"))
    starts = _when(args["starts_at"], "starts_at")
    event = Events(
        name=args["name"].strip(), slug=generate_slug(args["name"]), event_datetime=starts, duration=float(args["duration_hours"]),
        category_id=category.id, club_id=call.club.id, images=[], has_fee=False, reg_startdate=datetime.now(timezone.utc),
        additional_details=[], lifecycle=EventStatus.draft, status_clock=True,
    )
    _apply(event, {k: v for k, v in args.items() if k not in ("category",)})
    call.session.add(event)
    await call.session.flush()
    await _record(call, "mcp.event.draft_created", entity_type="event", entity_id=event.id, summary=f"Draft event “{event.name}” created by {call.principal.client_name}", tool="create_event_draft")
    await call.session.commit()
    return _draft_out(event)


@tool(
    "update_event_draft",
    title="Edit a draft event",
    scope="events:write",
    description=(
        "Change fields of a DRAFT event: only the fields given change, and those named in `clear` are emptied. An event "
        "that is already published can't be changed here: the answer says to edit it in the dashboard, with the link."
    ),
    schema=obj({
        "event_id": EVENT_ID, **EVENT_FIELDS,
        "clear": {"type": "array", "items": {"type": "string", "enum": list(CLEARABLE)}, "maxItems": len(CLEARABLE), "uniqueItems": True,
                  "description": "Fields to empty."},
    }, ("event_id",)),
    read_only=False,
)
async def update_event_draft(call: Call, args: dict) -> dict:
    from app.api.subscriptions import gates

    event = await _event(call, args["event_id"])
    _require_draft(event)
    fields = {k: v for k, v in args.items() if k != "event_id"}
    if not fields:
        raise ToolError("Give at least one field to change.", "INVALID_ARGUMENT")
    if fields.get("fee") and not event.has_fee:
        try:
            await gates.check_event_update(call.session, event, has_fee=True, event_datetime=_when(fields["starts_at"], "starts_at") if fields.get("starts_at") else None)
        except CustomHTTPException as e:
            raise from_http(e)
    if "category" in fields:
        event.category_id = (await _category(call, fields.pop("category"))).id
        changed = ["category"]
    else:
        changed = []
    changed += _apply(event, fields)
    await _record(call, "mcp.event.draft_updated", entity_type="event", entity_id=event.id, summary=f"Draft event “{event.name}” edited by {call.principal.client_name}", tool="update_event_draft", fields=changed)
    await call.session.commit()
    return _draft_out(event, changed)


@tool(
    "create_store_item",
    title="Add a store item",
    scope="store:write",
    description=(
        "Add an item to the club's merchandise catalogue. It is created OFF SALE: nobody can buy it until someone puts "
        "it on sale in the dashboard. Give one stock for the item, or sizes with a stock each."
    ),
    schema=obj({
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "description": {"type": "string", "maxLength": 300},
        "price": {"type": "number", "minimum": 0, "maximum": 1000000, "description": "In rupees."},
        "stock": {"type": "integer", "minimum": 0, "maximum": 1000000},
        "sizes": {"type": "array", "maxItems": 20, "items": obj({
            "label": {"type": "string", "minLength": 1, "maxLength": 40}, "stock": {"type": "integer", "minimum": 0, "maximum": 1000000},
        }, ("label", "stock"))},
    }, ("name", "price")),
    read_only=False,
    idempotent=False,
)
async def create_store_item(call: Call, args: dict) -> dict:
    from pydantic import ValidationError

    from app.api.store import service as store
    from app.api.store.schemas import StoreItemIn
    from app.api.subscriptions import gates

    try:
        await gates.require_feature(call.session, call.club.id, "merch")
        body = StoreItemIn(
            name=args["name"], description=args.get("description"), price=Decimal(str(args["price"])), stock=args.get("stock"),
            variants=[{"label": s["label"], "stock": s["stock"]} for s in args.get("sizes") or []], on_sale=False,
        )
    except CustomHTTPException as e:
        raise from_http(e)
    except ValidationError as e:
        raise ToolError("; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors()), "INVALID_ARGUMENT")
    item = await store.create_item(call.session, call.club.id, body)
    await _record(call, "mcp.store.item_created", entity_type="store_item", entity_id=item.id, summary=f"Store item “{item.name}” created (off sale) by {call.principal.client_name}", tool="create_store_item")
    await call.session.commit()
    return {
        "item_id": str(item.id), "name": item.name, "price": _money(item.price), "on_sale": False,
        "dashboard_url": dashboard_url("/merchandise"),
        "next_step": "It is off sale. Someone puts it on sale in the dashboard's merchandise page; offer_store_items adds it to a draft event.",
    }


@tool(
    "offer_store_items",
    title="Offer store items on a draft event",
    scope="store:write",
    description="Choose which of the club's catalogue items a DRAFT event's store offers: add some, take some away. The items' stock is shared across every event offering them.",
    schema=obj({
        "event_id": EVENT_ID,
        "add_item_ids": {"type": "array", "items": {"type": "string", "maxLength": 36}, "maxItems": 50, "description": "Item ids from list_store_items."},
        "remove_item_ids": {"type": "array", "items": {"type": "string", "maxLength": 36}, "maxItems": 50},
    }, ("event_id",)),
    read_only=False,
)
async def offer_store_items(call: Call, args: dict) -> dict:
    from app.api.store import service as store

    event = await _event(call, args["event_id"])
    _require_draft(event)
    current = [str(i) for i in await store.offered_ids(call.session, event)]
    remove = set(args.get("remove_item_ids") or [])
    wanted = [i for i in current if i not in remove] + [i for i in args.get("add_item_ids") or [] if i not in current]
    try:
        ids = await store.check_offers(call.session, event, wanted, current=await store.offered_ids(call.session, event))
    except CustomHTTPException as e:
        raise from_http(e)
    await store.write_offers(call.session, event.id, ids)
    await _record(call, "mcp.store.offers_changed", entity_type="event", entity_id=event.id, summary=f"Store offers on “{event.name}” changed by {call.principal.client_name}", tool="offer_store_items", item_ids=[str(i) for i in ids])
    await call.session.commit()
    return {
        "event_id": event.id, "offered_item_ids": [str(i) for i in ids], "store_switched_on": bool(event.merch_enabled),
        "note": None if event.merch_enabled else "The event's store is switched off; switch it on in the dashboard for guests to see these.",
    }


@tool(
    "draft_announcement",
    title="Draft an announcement",
    scope="comms:write",
    description=(
        "Write an announcement to an event's people (everyone registered, only those who checked in, or only those who "
        "did not) and SAVE it as a draft. Nothing is sent. Use request_send_announcement to ask a person to send it."
    ),
    schema=obj({
        "event_id": EVENT_ID,
        "title": {"type": "string", "minLength": 1, "maxLength": 255},
        "body": {"type": "string", "minLength": 1, "maxLength": 1000},
        "audience": {"type": "string", "enum": list(AUDIENCES), "default": "all"},
    }, ("event_id", "title", "body")),
    read_only=False,
    idempotent=False,
)
async def draft_announcement(call: Call, args: dict) -> dict:
    from app.api.clubs import announcements_page

    event = await _event(call, args["event_id"])
    audience = args.get("audience", "all")
    draft = AnnouncementDrafts(
        club_id=call.club.id, event_id=event.id, title=args["title"].strip(), body=args["body"].strip(), audience=audience,
        status="draft", created_by_user_id=call.principal.user.id, created_by_member_id=call.principal.member.id if call.principal.member else None,
        created_via="mcp", client_name=call.principal.client_name[:200],
    )
    call.session.add(draft)
    await call.session.flush()
    reach = (await announcements_page.audience(call.session, event))["by_audience"][audience]
    await _record(call, "mcp.announcement.drafted", entity_type="announcement_draft", entity_id=draft.id, summary=f"Announcement drafted for “{event.name}” by {call.principal.client_name}", tool="draft_announcement", event_id=event.id)
    await call.session.commit()
    return {
        "draft_id": str(draft.id), "event_id": event.id, "status": "draft", "audience": audience,
        "would_reach": {"people": reach["people"], "with_the_app": reach["reachable"]},
        "next_step": "Nothing was sent. Call request_send_announcement with this draft_id to ask a person to send it.",
    }


@tool(
    "request_publish_event",
    title="Ask to publish a draft event",
    scope="events:write",
    description=(
        "Ask a person to publish a DRAFT event. Nothing is published now: this returns a confirm_url for someone on the "
        "club's team to open in the dashboard and approve within 24 hours. Share the link with the person you are helping."
    ),
    schema=obj({"event_id": EVENT_ID}, ("event_id",)),
    read_only=False,
    idempotent=False,
)
async def request_publish_event(call: Call, args: dict) -> dict:
    event = await _event(call, args["event_id"])
    _require_draft(event)
    row, token = await actions.request(
        call.session, call.principal, kind="publish_event", target_type="event", target_id=event.id,
        payload={"event_id": event.id, "name": event.name, "starts_at": _iso(event.event_datetime)}, request=call.request,
    )
    await call.session.commit()
    return actions.requested_out(row, token)


@tool(
    "request_send_announcement",
    title="Ask to send an announcement",
    scope="comms:write",
    description=(
        "Ask a person to send a drafted announcement. Nothing is sent now: this returns a confirm_url for someone on the "
        "club's team to open in the dashboard and approve within 24 hours. Share the link with the person you are helping."
    ),
    schema=obj({"draft_id": {"type": "string", "maxLength": 36, "description": "From draft_announcement."}}, ("draft_id",)),
    read_only=False,
    idempotent=False,
)
async def request_send_announcement(call: Call, args: dict) -> dict:
    draft = await actions.draft_of(call.session, call.club.id, args["draft_id"])
    if draft is None:
        raise ToolError("There is no announcement draft with that id in this club.", "DRAFT_NOT_FOUND")
    if draft.status != "draft":
        raise ToolError(f"That announcement was already {draft.status}.", "DRAFT_NOT_PENDING")
    row, token = await actions.request(
        call.session, call.principal, kind="send_announcement", target_type="announcement_draft", target_id=draft.id,
        payload={"draft_id": str(draft.id), "event_id": draft.event_id, "title": draft.title, "audience": draft.audience}, request=call.request,
    )
    await call.session.commit()
    return actions.requested_out(row, token)
