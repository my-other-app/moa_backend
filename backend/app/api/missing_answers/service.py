"""Missing answers: keeping the answers people gave when a club edits its
questions, finding who is missing what, and letting them answer
(feature-register/missing-answers-contract.md; docs/MISSING_ANSWERS.md).

Soft, always: a missing answer never stops a registration, a ticket or a
check-in. The door sees it; the club may ask for it (asks.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select, update

from app.api.approvals.models import RegistrationRequests
from app.api.closeout.models import EventStatus
from app.api.events import questions as qmod
from app.api.events.models import EventRegistrationsLink, Events
from app.api.events.registration import answers as answers_mod
from app.api.missing_answers import people as people_mod
from app.api.missing_answers.models import (
    DEADLINE,
    DONE,
    EVENT_ENDED,
    OPEN,
    REGISTRATION,
    REQUEST,
    AnswerAskPeople,
    AnswerAsks,
    EventFormRevisions,
)
from app.api.missing_answers.people import Person
from app.api.missing_answers.state import ANSWERED, BLANK, HIDDEN, INVALID, Form, blank
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

#: The statuses after which an event takes no more answers.
_OVER = (EventStatus.closed, EventStatus.archived, EventStatus.cancelled)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ends_at(event) -> Optional[datetime]:
    if event.event_datetime is None:
        return None
    return event.event_datetime + timedelta(hours=float(event.duration or 0))


def event_over(event, now: Optional[datetime] = None) -> bool:
    """Ended, or closed, archived, cancelled or deleted: nothing more is asked."""
    if getattr(event, "is_deleted", False) or (event.lifecycle or EventStatus.published) in _OVER:
        return True
    end = ends_at(event)
    return end is not None and (now or now_utc()) >= end


def stored_form(questions) -> list[dict]:
    """Wire questions (as a client sends them) as stored; a 422 naming the
    first that isn't a question."""
    from pydantic import ValidationError

    from app.api.events.schemas import EventAdditionalDetail

    out = []
    for i, q in enumerate(questions or []):
        try:
            out.append(EventAdditionalDetail.model_validate(q).to_store())
        except ValidationError as exc:
            where = (q.get("key") if isinstance(q, dict) else None) or f"additional_details.{i}"
            raise CustomHTTPException(
                422, message=f"Question {i + 1} isn't a complete question.", error_code="INVALID_FORM",
                errors={str(where): exc.errors()[0].get("msg", "Not a question.")},
            ) from None
    return out


# ─── Impact (§1.3) ──────────────────────────────────────────────────────


def _chosen(answer, option) -> bool:
    if blank(answer):
        return False
    picked = answer if isinstance(answer, list) else [answer]
    return any(v is not None and str(v) == str(option) for v in picked)


def impact_of(before, after, everyone: list[Person]) -> dict:
    """What saving `after` over `before` does to the people already signed up.
    Both forms as stored. Nothing is saved."""
    old, new = Form(before), Form(after)
    out: dict = {
        "people": people_mod.counts(everyone), "added": [], "removed": [], "now_required": [],
        "options_removed": [], "relabelled": [], "type_changed": [],
    }
    states_new = [new.states(p.answers) for p in everyone]
    for key in new.keys:
        q = new.by_key[key]
        if key not in old:
            askable = sum(1 for s in states_new if s.get(key) in (BLANK, INVALID))
            out["added"].append({"key": key, "label": new.label(key), "required": new.required(key), "askable": askable})
            continue
        was = old.by_key[key]
        if new.required(key) and not old.required(key):
            missing = sum(1 for s in states_new if s.get(key) in (BLANK, INVALID))
            out["now_required"].append({"key": key, "label": new.label(key), "missing": missing})
        if qmod.kind_of(was) in qmod.CHOICE_TYPES:
            kept = [str(o) for o in (q.get("options") or []) if o is not None]
            for option in was.get("options") or []:
                if option is None or str(option) in kept:
                    continue
                chosen = sum(1 for p in everyone if _chosen(p.answers.get(key), option))
                out["options_removed"].append({"key": key, "label": new.label(key), "option": option, "chosen_by": chosen})
        if (was.get("label") or "") != (q.get("label") or ""):
            out["relabelled"].append({"key": key, "from": was.get("label") or "", "to": q.get("label") or ""})
        if qmod.kind_of(was) != qmod.kind_of(q):
            invalid = sum(1 for p in everyone if not blank(p.answers.get(key)) and new.problem(key, p.answers.get(key)))
            out["type_changed"].append({
                "key": key, "label": new.label(key), "from": qmod.kind_of(was), "to": qmod.kind_of(q), "invalid_after": invalid,
            })
    for key in old.keys:
        if key not in new:
            answered = sum(1 for p in everyone if not blank(p.answers.get(key)))
            out["removed"].append({"key": key, "label": old.label(key), "answered": answered})
    return out


async def impact(session, event, wire_questions) -> dict:
    """POST /events/{id}/form/impact: the save's effect, before it is made.
    The form is first given the stored keys a save would keep (§1.1), and
    checked as a save checks it -- a 422 with the rule's words if it breaks one."""
    after = stored_form(wire_questions)
    try:
        after = qmod.keep_keys(after, event.additional_details)
        qmod.check_options(after)
        qmod.check_form(after, before=event.additional_details)
    except CustomHTTPException as exc:
        raise CustomHTTPException(422, message=exc.message, error_code=exc.error_code or "INVALID_FORM", errors=exc.errors) from None
    everyone = await people_mod.load(session, event)
    return impact_of(event.additional_details or [], after, everyone)


# ─── Revisions (§1.2) ────────────────────────────────────────────────────


def _normal(form: list) -> list:
    """A form as a save would store it, so a question kept from before the
    stored shape settled (an `options` left out) is not a change."""
    from app.api.events.schemas import EventAdditionalDetail

    out = []
    for q in form:
        try:
            out.append(EventAdditionalDetail.model_validate(q).to_store())
        except Exception:  # noqa: BLE001 - a question this server can't read is compared as it is
            out.append(q)
    return out


async def record_revision(session, event, before, after, *, user_id: Optional[int], member_id: Optional[int]) -> Optional[int]:
    """When the questions changed: the new version, with what it did to the
    people already signed up. The first change since versions began writes
    the form as it was too (version 1, no summary), so there is something to
    compare with. In the caller's transaction; the event's row is locked so
    two saves at once number their versions one after the other. Returns
    the version written, or None when nothing changed."""
    before = list(before or [])
    after = list(after or [])
    if _normal(before) == _normal(after):
        return None
    await session.execute(select(Events.id).where(Events.id == event.id).with_for_update())
    latest = await session.scalar(select(func.max(EventFormRevisions.version)).where(EventFormRevisions.event_id == event.id))
    everyone = await people_mod.load(session, event)
    if latest is None:
        session.add(EventFormRevisions(event_id=event.id, version=1, questions=before, summary=None))
        latest = 1
    version = int(latest) + 1
    session.add(EventFormRevisions(
        event_id=event.id, version=version, questions=after, summary=impact_of(before, after, everyone),
        changed_by_user_id=user_id, changed_by_member_id=member_id,
    ))
    await session.flush()
    return version


async def form_meta(session, event_ids: Iterable) -> dict[int, tuple[int, datetime]]:
    """{event_id: (form_version, form_changed_at)} for the events whose
    questions have a version, in one statement."""
    ids = sorted({int(i) for i in event_ids or () if i is not None})
    if not ids:
        return {}
    V = EventFormRevisions
    newest = (
        select(V.event_id, func.max(V.version).label("version")).where(V.event_id.in_(ids)).group_by(V.event_id).subquery()
    )
    rows = (await session.execute(
        select(V.event_id, V.version, V.created_at).join(newest, (newest.c.event_id == V.event_id) & (newest.c.version == V.version))
    )).all()
    return {int(e): (int(v), at) for e, v, at in rows}


async def attach_form_meta(session, items: list) -> list:
    """`form_version` and `form_changed_at` on a page of event dicts (each with an `id`)."""
    meta = await form_meta(session, [i.get("id") for i in items if isinstance(i, dict)])
    for i in items:
        if isinstance(i, dict) and i.get("id") is not None:
            version, at = meta.get(int(i["id"]), (None, None))
            i["form_version"] = version
            i["form_changed_at"] = at
    return items


# ─── The summary (§1.4) ──────────────────────────────────────────────────


async def open_asks_count(session, event_id: int, now: Optional[datetime] = None) -> int:
    now = now or now_utc()
    return int(await session.scalar(
        select(func.count()).select_from(AnswerAsks).where(
            AnswerAsks.event_id == event_id, AnswerAsks.status == OPEN,
            (AnswerAsks.deadline.is_(None)) | (AnswerAsks.deadline > now),
        )
    ) or 0)


async def summary(session, event) -> dict:
    form = Form(event.additional_details)
    everyone = await people_mod.load(session, event)
    per = {k: {s: 0 for s in (ANSWERED, BLANK, INVALID, HIDDEN)} | {"missing": 0} for k in form.keys}
    stray: dict[str, int] = {}
    people_missing = 0
    for p in everyone:
        states = form.states(p.answers)
        missing = False
        for key, s in states.items():
            per[key][s] += 1
            if s in (BLANK, INVALID) and form.required(key):
                per[key]["missing"] += 1
                missing = True
        people_missing += int(missing)
        for key in form.stray(p.answers):
            stray[key] = stray.get(key, 0) + 1
    return {
        "people": people_mod.counts(everyone),
        "people_missing": people_missing,
        "questions": [
            {"key": k, "label": form.label(k), "required": form.required(k), **{s: per[k][s] for s in (ANSWERED, BLANK, INVALID, HIDDEN)},
             "missing": per[k]["missing"]}
            for k in form.keys
        ],
        "stray": [{"key": k, "count": n} for k, n in sorted(stray.items(), key=lambda kv: (-kv[1], kv[0]))],
        "open_asks": 0 if event_over(event) else await open_asks_count(session, event.id),
    }


# ─── The lists' filter and markers (§1.4) ────────────────────────────────


def check_missing_filter(form: Form, missing: Optional[str]) -> Optional[str]:
    """`missing=1` (anything missing) or `missing=<key>`; None for no filter.
    422 for a key the form does not ask."""
    if missing is None or str(missing).strip() == "":
        return None
    value = str(missing).strip()
    if value in ("1", "true", "any"):
        return "1"
    if value not in form:
        raise CustomHTTPException(422, message="That question isn't on this event's form.", errors={"missing": "Not a question of this form."})
    return value


def is_missing(form: Form, answers, wanted: str) -> bool:
    missing = form.missing(answers)
    return bool(missing) if wanted == "1" else wanted in missing


async def missing_registration_ids(session, event, wanted: str) -> list:
    """The live registrations of the event missing `wanted` ("1": anything)."""
    form = Form(event.additional_details)
    everyone = await people_mod.load(session, event, requests=False)
    return [p.id for p in everyone if is_missing(form, p.answers, wanted)]


async def missing_request_ids(session, event, wanted: str) -> list:
    """The event's requests missing `wanted`, whatever their status: the
    Review list filters each of its tabs by it."""
    form = Form(event.additional_details)
    rows = (await session.execute(
        select(RegistrationRequests.id, RegistrationRequests.answers).where(RegistrationRequests.event_id == event.id)
    )).all()
    return [rid for rid, answers in rows if is_missing(form, answers, wanted)]


# ─── Moving stray answers (§1.5) ─────────────────────────────────────────


async def move(session, event, source: str, target: str, *, actor=None, request=None) -> dict:
    """Answers kept under a key the form no longer asks, moved to one it
    does: for every registration and open request, copied when the new
    question has no answer yet, and the old key taken off; left alone when
    both have one (`skipped`). Audited."""
    from app.api.audit import service as audit

    form = Form(event.additional_details)
    source, target = (source or "").strip(), (target or "").strip()
    if not source or not target or source == target:
        raise CustomHTTPException(422, message="Pick an old question and a current one.", errors={"to": "Pick a different question."})
    if target not in form:
        raise CustomHTTPException(422, message="Answers can only be moved to a question on the form.", errors={"to": "Not a question of this form."})
    if source in form:
        raise CustomHTTPException(422, message="That question is still on the form.", errors={"from": "Still a question of this form."})
    moved = skipped = 0
    R, Q = EventRegistrationsLink, RegistrationRequests
    everyone = await people_mod.load(session, event)
    for p in everyone:
        answers = dict(p.answers)
        if source not in answers:
            continue
        if not blank(answers.get(source)) and not blank(answers.get(target)):
            skipped += 1
            continue
        value = answers.pop(source)
        if not blank(value):
            answers[target] = value
            moved += 1
        if p.kind == REGISTRATION:
            await session.execute(update(R).where(R.id == p.id).values(additional_details=answers).execution_options(synchronize_session=False))
        else:
            await session.execute(update(Q).where(Q.id == p.id).values(answers=answers).execution_options(synchronize_session=False))
    member = None
    if request is not None:
        from app.api.club_access import service as club_access

        member = club_access.current_member(request)
    await audit.record(
        session, actor=actor, action="event.answers_moved", entity_type="event", entity_id=event.id,
        summary=f"Moved answers from '{source}' to '{target}' ({moved} moved, {skipped} skipped)",
        details={"from": source, "to": target, "moved": moved, "skipped": skipped, "member_id": getattr(member, "id", None)},
        request=request,
    )
    await session.commit()
    logger.info("event %s: answers moved from %r to %r by %s: %d moved, %d skipped",
                event.id, source, target, getattr(actor, "id", None), moved, skipped)
    await after_answers_changed_many(session, event)
    return {"moved": moved, "skipped": skipped}


# ─── A person's asks, and what is asked of them (§2.3, §2.4) ────────────


def live(ask: AnswerAsks, event, now: datetime) -> bool:
    """Open, before its deadline, and the event not over."""
    return ask.status == OPEN and (ask.deadline is None or ask.deadline > now) and not event_over(event, now)


def ask_keys(ask: AnswerAsks) -> list[str]:
    return [q.get("key") for q in (ask.questions or []) if isinstance(q, dict) and q.get("key")]


async def asks_of(session, event_id: int, kind: str, id_) -> list[tuple[AnswerAsks, AnswerAskPeople]]:
    rows = (await session.execute(
        select(AnswerAsks, AnswerAskPeople)
        .join(AnswerAskPeople, AnswerAskPeople.ask_id == AnswerAsks.id)
        .where(AnswerAskPeople.event_id == event_id, AnswerAskPeople.ref_kind == kind, AnswerAskPeople.ref_id == id_)
        .order_by(AnswerAsks.created_at.desc())
    )).all()
    return [(a, p) for a, p in rows]


def asked_of(form: Form, person: Person, asks: list[AnswerAsks], event, now: datetime) -> list[str]:
    """What is asked of them now (§2.3): the still-askable questions of their
    live asks, and anything required they are missing, in form order."""
    keys: set[str] = set(form.missing(person.answers))
    for ask in asks:
        if live(ask, event, now):
            keys.update(form.askable(person.answers, ask_keys(ask)))
    return form.in_order(keys)


def status_of(form: Form, person: Optional[Person], asks: list[AnswerAsks], event, now: datetime) -> tuple[str, list[str]]:
    """(status, asked): open | done | closed | nothing."""
    if person is None:
        return "nothing", []
    asked = asked_of(form, person, asks, event, now)
    if event_over(event, now):
        return "closed", asked
    if not asked:
        return "done", asked
    if any(live(a, event, now) for a in asks):
        return "open", asked
    if any(a.deadline is not None and a.deadline <= now and a.closed_reason in (None, DEADLINE) for a in asks):
        return "closed", asked
    return "open", asked


async def answers_needed(session, event, person: Optional[Person], now: Optional[datetime] = None) -> Optional[dict]:
    """§2.4's flag: {"count", "url", "deadline"} while the person has an
    OPEN ask with something still to answer; null otherwise. The club
    decides when attendees are bothered: a missing answer with no ask is
    never flagged."""
    if person is None:
        return None
    now = now or now_utc()
    form = Form(event.additional_details)
    asks = [a for a, _p in await asks_of(session, event.id, person.kind, person.id)]
    open_ones = [a for a in asks if live(a, event, now) and form.askable(person.answers, ask_keys(a))]
    if not open_ones:
        return None
    asked = asked_of(form, person, asks, event, now)
    deadlines = [a.deadline for a in open_ones if a.deadline is not None]
    return {"count": len(asked), "url": people_mod.personal_link(event.id, person.ref), "deadline": min(deadlines) if deadlines else None}


async def flagged_refs(session, refs: list[tuple[str, object]], now: Optional[datetime] = None) -> set:
    """Of these (kind, id), those in any open ask not past its deadline: the
    only ones worth working out a flag for (one statement for a page)."""
    if not refs:
        return set()
    now = now or now_utc()
    ids = {i for _k, i in refs}
    rows = (await session.execute(
        select(AnswerAskPeople.ref_kind, AnswerAskPeople.ref_id)
        .join(AnswerAsks, AnswerAsks.id == AnswerAskPeople.ask_id)
        .where(
            AnswerAskPeople.ref_id.in_(list(ids)), AnswerAsks.status == OPEN,
            (AnswerAsks.deadline.is_(None)) | (AnswerAsks.deadline > now),
        )
        .distinct()
    )).all()
    return {(k, i) for k, i in rows}


async def flags_for_registrations(session, pairs: list) -> dict:
    """{registration id: answers_needed} for a page of (registration id,
    event): null for most, and one statement to know so."""
    candidates = await flagged_refs(session, [(REGISTRATION, rid) for rid, _e in pairs])
    out: dict = {}
    for rid, event in pairs:
        if (REGISTRATION, rid) not in candidates or event is None:
            continue
        person = await people_mod.load_one(session, event, REGISTRATION, rid)
        out[rid] = await answers_needed(session, event, person)
    return out


async def flags_for_requests(session, reqs: list, events: dict) -> dict:
    """{request id: answers_needed} for a list of requests, their events
    already read ({event id: event}): one statement for the list."""
    candidates = await flagged_refs(session, [(REQUEST, r.id) for r in reqs])
    out: dict = {}
    for r in reqs:
        event = events.get(r.event_id)
        if (REQUEST, r.id) not in candidates or event is None:
            continue
        out[r.id] = await answers_needed(session, event, await people_mod.load_one(session, event, REQUEST, r.id))
    return out


async def flag_for_request(session, req) -> Optional[dict]:
    if not await flagged_refs(session, [(REQUEST, req.id)]):
        return None
    event = await session.get(Events, req.event_id)
    if event is None:
        return None
    return await answers_needed(session, event, await people_mod.load_one(session, event, REQUEST, req.id))


# ─── After an answer changed, by any path ─────────────────────────────────


async def after_answers_changed(session, event, person: Optional[Person], now: Optional[datetime] = None) -> None:
    """The person's asks with nothing left for them are answered for them;
    an ask whose people have all answered closes (done). Commits."""
    if person is None:
        return
    now = now or now_utc()
    form = Form(event.additional_details)
    touched = []
    for ask, row in await asks_of(session, event.id, person.kind, person.id):
        if row.answered_at is None and not form.askable(person.answers, ask_keys(ask)):
            row.answered_at = now
            touched.append(ask)
    await session.flush()
    for ask in touched:
        if ask.status != OPEN:
            continue
        left = await session.scalar(
            select(func.count()).select_from(AnswerAskPeople).where(AnswerAskPeople.ask_id == ask.id, AnswerAskPeople.answered_at.is_(None))
        )
        if not left:
            ask.status, ask.closed_at, ask.closed_reason, ask.next_reminder_at = "closed", now, DONE, None
    await session.commit()


async def after_answers_changed_many(session, event) -> None:
    """after_answers_changed for everyone in the event's open asks (a move)."""
    rows = (await session.execute(
        select(AnswerAskPeople.ref_kind, AnswerAskPeople.ref_id)
        .join(AnswerAsks, AnswerAsks.id == AnswerAskPeople.ask_id)
        .where(AnswerAsks.event_id == event.id, AnswerAsks.status == OPEN, AnswerAskPeople.answered_at.is_(None))
        .distinct()
    )).all()
    if not rows:
        return
    everyone = {p.key: p for p in await people_mod.load(session, event, refs=[(k, i) for k, i in rows])}
    for key in rows:
        await after_answers_changed(session, event, everyone.get((key[0], key[1])))


# ─── Answering (§2.3) ────────────────────────────────────────────────────


def _poster(poster) -> Optional[str]:
    if isinstance(poster, dict):
        return poster.get("large") or poster.get("medium") or poster.get("original") or poster.get("thumbnail")
    return poster if isinstance(poster, str) else None


async def _event_for_answers(session, event_id: int):
    from sqlalchemy.orm import joinedload

    event = await session.scalar(
        select(Events).options(joinedload(Events.club)).where(Events.id == event_id).execution_options(include_deleted=True)
    )
    return event


def _not_valid() -> CustomHTTPException:
    return CustomHTTPException(404, message="This link is not valid.", error_code="NOT_FOUND")


async def _holder_may(session, person_row, user) -> bool:
    """The signed-in caller is the person: their account, or (an app account)
    the address the guest registered with, as the guest merge folds it in."""
    if user is None or person_row is None:
        return False
    if person_row.user_id is not None and person_row.user_id == user.id:
        return True
    from app.api.users import service as user_service

    return bool(user_service.merges_into(user)) and user_service.emails_match(person_row.email, user.email)


async def _raw_row(session, event_id: int, kind: str, id_):
    if kind == REGISTRATION:
        return await session.scalar(
            select(EventRegistrationsLink).where(EventRegistrationsLink.id == id_, EventRegistrationsLink.event_id == event_id)
            .execution_options(include_deleted=True)
        )
    return await session.scalar(select(RegistrationRequests).where(RegistrationRequests.id == id_, RegistrationRequests.event_id == event_id))


async def resolve(session, event_id: int, ref: str, k: Optional[str], user=None):
    """(event, kind, id, person) for a link, or 404: a ref that names no row
    of the event, or neither a good `k` nor the holder's own sign-in."""
    parsed = people_mod.parse_ref(ref)
    if parsed is None:
        raise _not_valid()
    kind, id_ = parsed
    canonical = people_mod.ref_of(kind, id_)
    event = await _event_for_answers(session, event_id)
    if event is None:
        raise _not_valid()
    row = await _raw_row(session, event.id, kind, id_)
    if row is None:
        raise _not_valid()
    if not (k and people_mod.signature_ok(event.id, canonical, k)) and not await _holder_may(session, row, user):
        raise _not_valid()
    person = None if getattr(event, "is_deleted", False) else await people_mod.load_one(session, event, kind, id_)
    return event, kind, id_, person, row


def _view(event, kind: str, person: Optional[Person], row, status: str, asked: list[str], asks: list[AnswerAsks], now: datetime) -> dict:
    from app.core import public_urls

    form = Form(event.additional_details)
    live_asks = [a for a in asks if live(a, event, now)]
    deadlines = [a.deadline for a in live_asks if a.deadline is not None]
    club = getattr(event, "club", None)
    gone = status == "nothing"
    ticket_url = None
    request_url = None
    if kind == REGISTRATION and person is not None and person.ticket_id:
        ticket_url = public_urls.ticket_url(person.ticket_id)
    if kind == REQUEST and row is not None and getattr(row, "token", None):
        from app.api.approvals import service as approvals

        request_url = approvals.status_url(row.token)
    return {
        "status": status,
        "event": {
            "id": event.id, "name": event.name, "starts_at": event.event_datetime, "org_name": club.name if club is not None else "",
            "poster": _poster(event.poster), "slug": event.slug,
        },
        "person": {"first_name": (getattr(row, "full_name", None) or "").strip().split(" ")[0] if row is not None else "", "kind": kind},
        "deadline": min(deadlines) if deadlines else None,
        "message": live_asks[0].message if live_asks else None,
        "questions": [] if gone else qmod.wire_all(event.additional_details),
        "asked": [] if gone else asked,
        "answers": {} if gone else form.current(person.answers if person is not None else {}),
        "ticket_url": ticket_url,
        "request_url": request_url,
    }


async def public_get(session, event_id: int, ref: str, k: Optional[str], user=None) -> dict:
    now = now_utc()
    event, kind, id_, person, row = await resolve(session, event_id, ref, k, user)
    form = Form(event.additional_details)
    pairs = await asks_of(session, event.id, kind, id_)
    asks = [a for a, _p in pairs]
    status, asked = status_of(form, person, asks, event, now)
    opened = False
    for ask, mine in pairs:
        if mine.opened_at is None and live(ask, event, now):
            mine.opened_at = now
            opened = True
    if opened:
        await session.commit()
    return _view(event, kind, person, row, status, asked, asks, now)


def _refuse_answers(errors: dict, message: str = "Check your answers.") -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code="INVALID_ANSWERS", errors=errors)


def check_some(form: Form, merged: dict, keys: Iterable[str], *, require: bool) -> None:
    """The registration form's rules, for `keys` alone: a shown one that is
    required and blank (when `require`), or of the wrong type, is a 422
    naming it. The others are left as they are."""
    shown = qmod.visibility(form.questions, merged)
    errors: dict = {}
    for key in keys:
        if key not in form or not shown.get(key, True):
            continue
        value = merged.get(key)
        if blank(value):
            if require and form.required(key):
                errors[key] = "This field is required"
            continue
        why = form.problem(key, value)
        if why:
            errors[key] = why
    if errors:
        labels = [form.label(k) for k in form.keys if k in errors]
        raise _refuse_answers(errors, f"Check your answers: {', '.join(labels)}.")


async def _save_answers(session, kind: str, id_, answers: dict) -> None:
    if kind == REGISTRATION:
        R = EventRegistrationsLink
        await session.execute(update(R).where(R.id == id_).values(additional_details=answers).execution_options(synchronize_session=False))
    else:
        Q = RegistrationRequests
        await session.execute(update(Q).where(Q.id == id_).values(answers=answers).execution_options(synchronize_session=False))


async def public_post(session, event_id: int, ref: str, k: Optional[str], sent, user=None) -> dict:
    """The person's answers: to what is asked of them, and to the questions
    those answers reveal; anything else sent is ignored. Checked by the
    registration form's rules (only these required), merged into what they
    had, and what the new answers hide is dropped. 409 CLOSED past the
    deadline or once the event is over."""
    now = now_utc()
    event, kind, id_, person, row = await resolve(session, event_id, ref, k, user)
    form = Form(event.additional_details)
    asks = [a for a, _p in await asks_of(session, event.id, kind, id_)]
    status, asked = status_of(form, person, asks, event, now)
    if status == "closed":
        raise CustomHTTPException(409, message="This form is closed: the deadline has passed.", error_code="CLOSED")
    if status != "open" or person is None:
        return _view(event, kind, person, row, status, asked, asks, now)
    sent = answers_mod.bound(sent) if sent is not None else {}
    accepted = asked + form.descendants(asked)
    merged = dict(person.answers)
    for key in accepted:
        if key in sent:
            if blank(sent[key]):
                merged.pop(key, None)
            else:
                merged[key] = sent[key]
    shown = qmod.visibility(form.questions, merged)
    check_some(form, merged, [k_ for k_ in accepted if shown.get(k_, True)], require=True)
    merged = answers_mod.bound(qmod.prune(form.questions, merged))
    was = dict(person.answers or {})
    await _save_answers(session, kind, id_, merged)
    await session.flush()
    person.answers = merged
    if kind == REGISTRATION and merged != was:
        # The answers given later (app/api/webhooks_out).
        from app.api.webhooks_out import signals as webhooks

        await webhooks.registration_answers_changed(session, event, id_, was)
    await after_answers_changed(session, event, person, now)
    status, asked = status_of(form, person, asks, event, now)
    return _view(event, kind, person, row, status, asked, asks, now)


# ─── The door (§2.5) ─────────────────────────────────────────────────────


async def door_answers(session, event, registration_id, sent) -> dict:
    """Answers filled at check-in: any of the form's questions; types checked,
    nothing required; blanks leave an answer as it was. Merged, and what the
    new answers hide is dropped. Counts as answered for any open ask."""
    from app.api.checkins import service as checkins

    reg = await checkins.registration_of(session, event, registration_id)
    from app.api.webhooks_out import signals as webhooks

    webhooks_before = await webhooks.registration_before(session, reg, club_id=event.club_id)
    form = Form(event.additional_details)
    sent = answers_mod.bound(sent) if sent is not None else {}
    merged = dict(reg.additional_details) if isinstance(reg.additional_details, dict) else {}
    touched = [k for k in form.keys if k in sent and not blank(sent[k])]
    for key in touched:
        merged[key] = sent[key]
    check_some(form, merged, touched, require=False)
    merged = answers_mod.bound(qmod.prune(form.questions, merged))
    reg.additional_details = merged
    await webhooks.registration_updated(session, reg, webhooks_before, club_id=event.club_id)
    await session.commit()
    person = await people_mod.load_one(session, event, REGISTRATION, reg.id)
    await after_answers_changed(session, event, person)
    return {"missing_answers": form.missing_brief(merged)}
