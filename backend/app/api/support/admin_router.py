"""The admin portal's Team work: the teams, the desk's settings, the cases,
My day, the badge and the reports -- prefix /api/v1/admin.

Every admin reads the lists and counts; message bodies only the two teams
and the owners (teams.Rights). Every change goes to the audit log, never a
word anyone wrote.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, File, Query, Request, UploadFile
from sqlalchemy import delete, func, select

from app.api.support import cases, files, hours, mail, panel, reference, reports, teams, text
from app.api.support.models import (
    REPLY_OUT,
    SUPPORT,
    TEAMS,
    AdminTeams,
    SupportAttachments,
    SupportCases,
    SupportHelpAnswers,
    SupportSavedReplies,
    SupportTopics,
)
from app.api.support.schemas import (
    AwayIn,
    CaseMessageIn,
    CasePatch,
    DeskSettingsIn,
    HelpAnswerIn,
    LostReasonIn,
    ManualCaseIn,
    MembersIn,
    MergeIn,
    SavedReplyIn,
    TopicIn,
    ToLeadIn,
)
from app.core.auth.dependencies import AdminAuth
from app.core.auth.permissions import ManageAdminsAuth
from app.core.utils import spreadsheet
from app.db.core import SessionDep
from app.response import CustomHTTPException

router = APIRouter(prefix="/admin", tags=["admin", "support"])

ListFormat = Literal["json", "xlsx"]


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(code, message=message, error_code=error_code, errors=errors)


async def _audit(session, actor, action: str, entity_type: str, entity_id, summary: str, details: Optional[dict] = None, request=None):
    from app.api.audit import service as audit

    await audit.record(session, actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, summary=summary,
                       details=details, request=request)


def _team_key(key: str) -> str:
    if key not in TEAMS:
        raise _refuse(404, "There's no such team.", "TEAM_NOT_FOUND")
    return key


# ─── Teams ───────────────────────────────────────────────────────────────


@router.get("/teams", summary="The Support and Sales teams and their members")
async def list_teams(session: SessionDep, admin: AdminAuth) -> list[dict]:
    out = await teams.teams_out(session)
    await session.commit()
    return out


@router.get("/teams/eligible", summary="Admin accounts an owner may put in a team")
async def eligible(session: SessionDep, admin: AdminAuth) -> list[dict]:
    (await teams.rights_of(session, admin)).require_owner("change the teams")
    return await teams.eligible(session)


@router.patch("/teams/me", summary="Mark myself away (or back)")
async def set_me_away(request: Request, body: AwayIn, session: SessionDep, admin: AdminAuth) -> dict:
    out = await teams.set_away(session, admin, body.away, body.away_until)
    await _audit(session, admin, "teams.away" if body.away else "teams.back", "user", admin.id,
                 f"{admin.full_name} is {'away' if body.away else 'back'}", {"away_until": str(body.away_until) if body.away_until else None},
                 request=request)
    await session.commit()
    return {"teams": out}


@router.put("/teams/{key}/members", summary="Set a team's members (owner)")
async def set_members(request: Request, key: str, body: MembersIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    team = _team_key(key)
    result = await teams.set_members(session, team, body.user_ids)
    await _audit(session, admin, "teams.members", "admin_team", team, f"{team.title()} team: {len(result['added'])} added, "
                 f"{len(result['removed'])} removed", result, request=request)
    await session.commit()
    return {"key": team, **result, "teams": await teams.teams_out(session)}


# ─── Desk settings: topics, saved replies, help answers, lost reasons ────


def _topic_out(t: SupportTopics) -> dict:
    return {"key": t.key, "label": t.label, "assign": t.assign, "assignee_id": t.assignee_id,
            "default_priority": t.default_priority, "order": t.position, "active": t.active}


@router.get("/support/topics", summary="The topics and their assignment rules")
async def list_topics(session: SessionDep, admin: AdminAuth) -> list[dict]:
    await reference.ensure(session)
    await session.commit()
    rows = (await session.scalars(select(SupportTopics).order_by(SupportTopics.position, SupportTopics.key))).all()
    return [_topic_out(t) for t in rows]


async def _check_topic_rule(session, assign: str, assignee_id: Optional[int]) -> None:
    if assign == "person":
        if not assignee_id or assignee_id not in await teams.members_of(session, SUPPORT):
            raise _refuse(422, "Pick someone from the support team.", "NOT_IN_TEAM", {"field": "assignee_id"})


@router.post("/support/topics", summary="Add a topic (owner)", status_code=201)
async def create_topic(request: Request, body: TopicIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    if not body.key or not body.label:
        raise _refuse(422, "A topic needs a key and a label.", "INVALID_TOPIC")
    await reference.ensure(session)
    if await session.get(SupportTopics, body.key) is not None:
        raise _refuse(409, "There's a topic with that key.", "TOPIC_EXISTS")
    assign = body.assign or "round_robin"
    await _check_topic_rule(session, assign, body.assignee_id)
    t = SupportTopics(key=body.key, label=body.label, assign=assign, assignee_id=body.assignee_id if assign == "person" else None,
                      default_priority=body.default_priority or "normal", position=body.order or 0)
    session.add(t)
    await _audit(session, admin, "support.topic.create", "support_topic", t.key, f"Added the topic {t.label}", request=request)
    await session.commit()
    return _topic_out(t)


@router.patch("/support/topics/{key}", summary="Change a topic or its rule (owner)")
async def update_topic(request: Request, key: str, body: TopicIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    t = await session.get(SupportTopics, key)
    if t is None:
        raise _refuse(404, "There's no such topic.", "TOPIC_NOT_FOUND")
    sent = body.model_fields_set
    assign = body.assign or t.assign
    assignee = body.assignee_id if "assignee_id" in sent else t.assignee_id
    await _check_topic_rule(session, assign, assignee)
    if body.label is not None:
        t.label = body.label
    t.assign, t.assignee_id = assign, assignee if assign == "person" else None
    if body.default_priority is not None:
        t.default_priority = body.default_priority
    if body.order is not None:
        t.position = body.order
    if body.active is not None:
        t.active = body.active
    await _audit(session, admin, "support.topic.update", "support_topic", t.key, f"Changed the topic {t.label}",
                 {"fields": sorted(sent)}, request=request)
    await session.commit()
    return _topic_out(t)


@router.delete("/support/topics/{key}", summary="Remove a topic (archived when cases have it) (owner)")
async def delete_topic(request: Request, key: str, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    t = await session.get(SupportTopics, key)
    if t is None:
        raise _refuse(404, "There's no such topic.", "TOPIC_NOT_FOUND")
    used = await session.scalar(select(func.count()).select_from(SupportCases).where(SupportCases.topic == key))
    if used:
        t.active = False
    else:
        await session.delete(t)
    await _audit(session, admin, "support.topic.delete", "support_topic", key, f"Removed the topic {t.label}",
                 {"archived": bool(used)}, request=request)
    await session.commit()
    return {"key": key, "archived": bool(used), "deleted": not used}


def _reply_out(r: SupportSavedReplies) -> dict:
    return {"id": r.id, "title": r.title, "body": r.body, "topic": r.topic, "updated_at": r.updated_at}


async def _valid_topic(session, key: Optional[str]) -> Optional[str]:
    if key and await session.get(SupportTopics, key) is None:
        raise _refuse(422, "That isn't one of the topics.", "INVALID_TOPIC", {"field": "topic"})
    return key or None


@router.get("/support/saved-replies", summary="Saved replies")
async def list_saved_replies(session: SessionDep, admin: AdminAuth, topic: Optional[str] = None) -> list[dict]:
    stmt = select(SupportSavedReplies).order_by(SupportSavedReplies.title, SupportSavedReplies.id)
    if topic:
        stmt = stmt.where((SupportSavedReplies.topic == topic) | SupportSavedReplies.topic.is_(None))
    return [_reply_out(r) for r in (await session.scalars(stmt)).all()]


@router.post("/support/saved-replies", summary="Add a saved reply (owner)", status_code=201)
async def create_saved_reply(request: Request, body: SavedReplyIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    if not body.title or not body.body:
        raise _refuse(422, "A saved reply needs a title and words.", "INVALID_SAVED_REPLY")
    r = SupportSavedReplies(title=body.title, body=body.body, topic=await _valid_topic(session, body.topic), created_by=admin.id)
    session.add(r)
    await session.flush()
    await _audit(session, admin, "support.saved_reply.create", "support_saved_reply", r.id, f"Added the saved reply {r.title}", request=request)
    await session.commit()
    return _reply_out(r)


@router.patch("/support/saved-replies/{reply_id}", summary="Change a saved reply (owner)")
async def update_saved_reply(request: Request, reply_id: int, body: SavedReplyIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    r = await session.get(SupportSavedReplies, reply_id)
    if r is None:
        raise _refuse(404, "There's no such saved reply.", "SAVED_REPLY_NOT_FOUND")
    if body.title is not None:
        r.title = body.title
    if body.body is not None:
        r.body = body.body
    if "topic" in body.model_fields_set:
        r.topic = await _valid_topic(session, body.topic)
    await _audit(session, admin, "support.saved_reply.update", "support_saved_reply", r.id, f"Changed the saved reply {r.title}", request=request)
    await session.commit()
    return _reply_out(r)


@router.delete("/support/saved-replies/{reply_id}", summary="Delete a saved reply (owner)")
async def delete_saved_reply(request: Request, reply_id: int, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    r = await session.get(SupportSavedReplies, reply_id)
    if r is None:
        raise _refuse(404, "There's no such saved reply.", "SAVED_REPLY_NOT_FOUND")
    await session.delete(r)
    await _audit(session, admin, "support.saved_reply.delete", "support_saved_reply", reply_id, f"Deleted the saved reply {r.title}", request=request)
    await session.commit()
    return {"id": reply_id, "deleted": True}


def _answer_out(a: SupportHelpAnswers) -> dict:
    return {"id": a.id, "topic": a.topic, "question": a.question, "answer": a.answer, "published": a.published,
            "order": a.position, "updated_at": a.updated_at}


@router.get("/support/help-answers", summary="Help answers, published or not")
async def list_help_answers(session: SessionDep, admin: AdminAuth) -> list[dict]:
    rows = (await session.scalars(select(SupportHelpAnswers).order_by(SupportHelpAnswers.position, SupportHelpAnswers.id))).all()
    return [_answer_out(a) for a in rows]


@router.post("/support/help-answers", summary="Add a help answer (owner)", status_code=201)
async def create_help_answer(request: Request, body: HelpAnswerIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    if not body.question or not body.answer:
        raise _refuse(422, "A help answer needs a question and an answer.", "INVALID_HELP_ANSWER")
    a = SupportHelpAnswers(topic=await _valid_topic(session, body.topic), question=body.question, answer=body.answer,
                           published=bool(body.published), position=body.order or 0)
    session.add(a)
    await session.flush()
    await _audit(session, admin, "support.help_answer.create", "support_help_answer", a.id, "Added a help answer", request=request)
    await session.commit()
    return _answer_out(a)


@router.patch("/support/help-answers/{answer_id}", summary="Change a help answer (owner)")
async def update_help_answer(request: Request, answer_id: int, body: HelpAnswerIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    a = await session.get(SupportHelpAnswers, answer_id)
    if a is None:
        raise _refuse(404, "There's no such help answer.", "HELP_ANSWER_NOT_FOUND")
    sent = body.model_fields_set
    if "topic" in sent:
        a.topic = await _valid_topic(session, body.topic)
    for field, attr in (("question", "question"), ("answer", "answer"), ("published", "published"), ("order", "position")):
        value = getattr(body, field)
        if value is not None:
            setattr(a, attr, value)
    await _audit(session, admin, "support.help_answer.update", "support_help_answer", a.id, "Changed a help answer",
                 {"fields": sorted(sent)}, request=request)
    await session.commit()
    return _answer_out(a)


@router.delete("/support/help-answers/{answer_id}", summary="Delete a help answer (owner)")
async def delete_help_answer(request: Request, answer_id: int, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    a = await session.get(SupportHelpAnswers, answer_id)
    if a is None:
        raise _refuse(404, "There's no such help answer.", "HELP_ANSWER_NOT_FOUND")
    await session.delete(a)
    await _audit(session, admin, "support.help_answer.delete", "support_help_answer", answer_id, "Deleted a help answer", request=request)
    await session.commit()
    return {"id": answer_id, "deleted": True}


def _reason_out(r) -> dict:
    return {"id": r.id, "key": r.key, "label": r.label, "order": r.position, "active": r.active}


@router.get("/leads/lost-reasons", summary="Why leads are lost")
async def list_lost_reasons(session: SessionDep, admin: AdminAuth) -> list[dict]:
    from app.api.leads.models import LeadLostReasons

    await reference.ensure(session)
    await session.commit()
    rows = (await session.scalars(select(LeadLostReasons).order_by(LeadLostReasons.position, LeadLostReasons.id))).all()
    return [_reason_out(r) for r in rows]


@router.post("/leads/lost-reasons", summary="Add a lost reason (owner)", status_code=201)
async def create_lost_reason(request: Request, body: LostReasonIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    from app.api.leads.models import LeadLostReasons

    if not body.key or not body.label:
        raise _refuse(422, "A lost reason needs a key and a label.", "INVALID_LOST_REASON")
    if await session.scalar(select(LeadLostReasons.id).where(LeadLostReasons.key == body.key)):
        raise _refuse(409, "There's a lost reason with that key.", "LOST_REASON_EXISTS")
    r = LeadLostReasons(key=body.key, label=body.label, position=body.order or 0)
    session.add(r)
    await session.flush()
    await _audit(session, admin, "leads.lost_reason.create", "lead_lost_reason", r.id, f"Added the lost reason {r.label}", request=request)
    await session.commit()
    return _reason_out(r)


@router.patch("/leads/lost-reasons/{reason_id}", summary="Change a lost reason (owner)")
async def update_lost_reason(request: Request, reason_id: int, body: LostReasonIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    from app.api.leads.models import LeadLostReasons

    r = await session.get(LeadLostReasons, reason_id)
    if r is None:
        raise _refuse(404, "There's no such lost reason.", "LOST_REASON_NOT_FOUND")
    if body.label is not None:
        r.label = body.label
    if body.order is not None:
        r.position = body.order
    if body.active is not None:
        r.active = body.active
    await _audit(session, admin, "leads.lost_reason.update", "lead_lost_reason", r.id, f"Changed the lost reason {r.label}", request=request)
    await session.commit()
    return _reason_out(r)


@router.delete("/leads/lost-reasons/{reason_id}", summary="Remove a lost reason (archived when leads have it) (owner)")
async def delete_lost_reason(request: Request, reason_id: int, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    from app.api.leads.models import LeadLostReasons, Leads

    r = await session.get(LeadLostReasons, reason_id)
    if r is None:
        raise _refuse(404, "There's no such lost reason.", "LOST_REASON_NOT_FOUND")
    used = await session.scalar(select(func.count()).select_from(Leads).where(Leads.lost_reason == r.key))
    if used:
        r.active = False
    else:
        await session.execute(delete(LeadLostReasons).where(LeadLostReasons.id == r.id))
    await _audit(session, admin, "leads.lost_reason.delete", "lead_lost_reason", reason_id, f"Removed the lost reason {r.label}",
                 {"archived": bool(used)}, request=request)
    await session.commit()
    return {"id": reason_id, "archived": bool(used), "deleted": not used}


@router.get("/support/settings", summary="The desk's settings")
async def get_settings(session: SessionDep, admin: AdminAuth) -> dict:
    return await hours.get(session)


@router.put("/support/settings", summary="Change the desk's settings (owner)")
async def put_settings(request: Request, body: DeskSettingsIn, session: SessionDep, admin: ManageAdminsAuth) -> dict:
    await reference.ensure(session)
    current = await hours.get(session)
    sent = body.model_dump(exclude_unset=True)
    value = hours.validate({**current, **{k: v for k, v in sent.items() if v is not None}})
    row = await session.get(AdminTeams, SUPPORT)
    row.settings = value
    await _audit(session, admin, "support.settings", "admin_team", SUPPORT, "Changed the desk's settings", {"settings": value}, request=request)
    await session.commit()
    return value


# ─── Cases ───────────────────────────────────────────────────────────────


@router.get("/support/cases", summary="The support queue")
async def list_cases(
    session: SessionDep, admin: AdminAuth,
    tab: str = Query("open"), topic: Optional[str] = None, priority: Optional[str] = None, assignee: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=200), age_days: Optional[int] = Query(None, ge=1, le=3650),
    status: Optional[str] = None, page: int = Query(1, ge=1, le=10000), page_size: int = Query(25, ge=1, le=200),
    fmt: ListFormat = Query("json", alias="format"),
):
    """`tab`: mine, unassigned, open, waiting, resolved, all, or spam (mail
    SES called spam, kept out of every other tab). `format=xlsx`: every case
    the filters match, for the teams and the owners."""
    rights = await teams.rights_of(session, admin)
    f = dict(topic=topic, priority=priority, assignee=assignee, q=q, age_days=age_days, status=status)
    if fmt == "xlsx":
        rights.require("export the cases", support=True, sales=True)
        if tab not in cases.TABS:
            raise _refuse(422, f"tab is one of {', '.join(cases.TABS)}.", "INVALID_FILTER", {"field": "tab"})
        rows = cases.export_rows(session, admin.id, tab=tab, **f)
        header = await rows.__anext__()
        path = await spreadsheet.write_xlsx(rows, title="Support cases", headers=header)
        return spreadsheet.xlsx_response(path, f"support-cases-{date.today().isoformat()}.xlsx", cache_control="no-store")
    out = await cases.list_cases(session, admin.id, tab=tab, page=page, page_size=page_size, **f)
    out["reads_bodies"] = rights.reads_bodies
    return out


async def _requester_for(session, body: ManualCaseIn) -> cases.Requester:
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    r = body.requester
    if r.club_id:
        club = await session.get(Clubs, r.club_id)
        if club is None:
            raise _refuse(422, "There's no such club.", "INVALID_REQUESTER", {"field": "requester.club_id"})
        return cases.Requester(kind="club", club_id=club.id, email=(str(r.email) if r.email else None) or club.contact_email,
                               name=r.name or club.name)
    if r.user_id:
        user = await session.get(Users, r.user_id)
        if user is None:
            raise _refuse(422, "There's no such account.", "INVALID_REQUESTER", {"field": "requester.user_id"})
        return cases.Requester(kind="user", user_id=user.id, email=user.email, name=r.name or user.full_name)
    if not r.email or not (r.name or "").strip():
        raise _refuse(422, "Say who it's from: an account, a club, or a name and an email.", "INVALID_REQUESTER",
                      {"field": "requester"})
    return cases.Requester(kind="guest", email=str(r.email).lower(), name=r.name.strip())


@router.post("/support/cases", summary="Log a case by hand (a call, a WhatsApp message)", status_code=201)
async def create_case(request: Request, body: ManualCaseIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    rights.require("log a case", support=True)
    requester = await _requester_for(session, body)
    links = {"user_id": requester.user_id} if requester.user_id else {}
    if requester.club_id:
        links["club_id"] = requester.club_id
    case, _ = await cases.open_case(
        session, requester=requester, topic=body.topic, subject=body.subject, body_text=text.mask_cards(body.message),
        channel="manual", priority=body.priority, links=links, actor=admin,
        assignee_id=admin.id if rights.support else None, assign=not rights.support,
    )
    await cases.audit(session, admin, "case.logged", case, f"Logged case #{case.number} by hand", request=request)
    await session.commit()
    return await _detail(session, case, rights)


def _admin_url(a: SupportAttachments) -> str:
    return f"/api/v1/admin/support/attachments/{a.id}"


async def _detail(session, case: SupportCases, rights: teams.Rights) -> dict:
    from app.api.leads.models import FollowUps

    now = cases._now()
    names = await teams.names(session, [case.assignee_id])
    out = cases.row_out(case, names, now)
    msgs = await cases.messages_of(session, case.id)
    by_msg = await cases.attachments_by_message(session, [m.id for m in msgs]) if rights.reads_bodies else {}
    out["messages"] = [cases.message_out(m, by_msg.get(m.id, []), _admin_url, bodies=rights.reads_bodies) for m in msgs]
    out["links"] = {k: (case.links or {}).get(k) for k in cases.LINK_KEYS}
    out["context"] = case.context or {}
    out["panel"] = await panel.panel(session, case)
    out["survey"] = {"sent_at": case.survey_sent_at, "solved": case.survey_solved, "comment": case.survey_comment if rights.reads_bodies else None,
                     "answered_at": case.survey_at}
    out.update({
        "first_replied_at": case.first_replied_at, "resolved_at": case.resolved_at, "closed_at": case.closed_at,
        "close_reason": case.close_reason, "waiting_since": case.waiting_since, "message_count": case.message_count,
        "parent_case_id": case.parent_case_id, "merged_into_id": case.merged_into_id, "lead_id": case.lead_id,
        "purged_at": case.purged_at, "reads_bodies": rights.reads_bodies, "can_work": cases.can_work(rights, case),
        "can_take": rights.support and case.assignee_id is None,
    })
    fus = (await session.scalars(select(FollowUps).where(FollowUps.case_id == case.id).order_by(FollowUps.done, FollowUps.due_at))).all()
    out["follow_ups"] = [{"id": f.id, "kind": f.kind, "title": f.title, "due_at": f.due_at, "owner_id": f.owner_id, "done": f.done}
                         for f in fus]
    return out


@router.get("/support/cases/{case_id}", summary="A case: its thread, links and the person's records")
async def get_case(case_id: int, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    return await _detail(session, await cases.get_case(session, case_id), rights)


@router.patch("/support/cases/{case_id}", summary="Change a case's status, priority, topic, assignee, tags or links")
async def update_case(request: Request, case_id: int, body: CasePatch, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    case = await cases.get_case(session, case_id, lock=True)
    sent = body.model_fields_set
    changed: dict = {}
    if "assignee_id" in sent and body.assignee_id != case.assignee_id:
        taking = rights.support and case.assignee_id is None and body.assignee_id == admin.id
        if not (rights.owner or taking):
            raise _refuse(403, "Only an admin owner can reassign a case.", "OWNER_REQUIRED")
        await cases.assign(session, case, body.assignee_id)
        changed["assignee_id"] = body.assignee_id
    others = sent - {"assignee_id"}
    if others:
        cases.require_work(rights, case)
    if body.status is not None and "status" in sent:
        if await cases.set_status(session, case, body.status, reason="manual"):
            changed["status"] = body.status
    if body.priority is not None and body.priority != case.priority:
        case.priority = body.priority
        changed["priority"] = body.priority
    if body.topic is not None and body.topic != case.topic:
        row = await cases.topic_row(session, body.topic)
        case.topic = row.key
        changed["topic"] = row.key
    if body.tags is not None:
        tags = cases.clean_tags(body.tags)
        not_spam = "spam" in (case.tags or []) and "spam" not in tags
        case.tags = tags
        changed["tags"] = tags
        if not_spam and case.status == "closed" and case.close_reason == "spam" and body.status is None:
            # Mail SES called spam that wasn't: into the queue.
            await cases.set_status(session, case, "open")
            changed["status"] = "open"
    if body.links is not None:
        links = await panel.check_links(session, body.links)
        case.links = links
        changed["links"] = sorted(links)
    if changed:
        case.updated_at = cases._now()
        await cases.audit(session, admin, "case.update", case, f"Changed case #{case.number}: {', '.join(sorted(changed))}",
                          {k: v for k, v in changed.items() if k != "links"}, request=request)
    await session.commit()
    return await _detail(session, case, rights)


@router.post("/support/cases/{case_id}/take", summary="Take an unassigned case")
async def take_case(request: Request, case_id: int, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    rights.require("take cases", support=True)
    if not rights.support:
        raise _refuse(403, "Cases go to members of the support team; join it first.", "TEAM_REQUIRED")
    case = await cases.get_case(session, case_id, lock=True)
    if case.assignee_id not in (None, admin.id) and not rights.owner:
        raise _refuse(409, "Someone has this case already.", "CASE_TAKEN")
    await cases.assign(session, case, admin.id)
    await cases.audit(session, admin, "case.take", case, f"Took case #{case.number}", request=request)
    await session.commit()
    return await _detail(session, case, rights)


async def _team_attachments(session, keys: list[str], admin) -> list[SupportAttachments]:
    return await files.claim(session, keys, user_id=admin.id, limit=10)


@router.post("/support/cases/{case_id}/attachments", summary="Upload a file to send with a reply", status_code=201)
async def upload_for_case(case_id: int, session: SessionDep, admin: AdminAuth, file: UploadFile = File(...)) -> dict:
    rights = await teams.rights_of(session, admin)
    case = await cases.get_case(session, case_id)
    cases.require_work(rights, case, "answer this case")
    data = await files.read_upload(file, files.TEAM_MAX_BYTES)
    kind = files.sniff(data) or "application/octet-stream"
    row = await files.keep(session, data, kind, files.safe_name(file.filename), user_id=admin.id, upload=True)
    await session.commit()
    return {"key": row.upload_key, "name": row.filename, "size": row.size_bytes}


@router.post("/support/cases/{case_id}/messages", summary="Answer the person, or add an internal note")
async def post_message(request: Request, case_id: int, body: CaseMessageIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    case = await cases.get_case(session, case_id, lock=True)
    if body.kind == REPLY_OUT or not rights.support:
        cases.require_work(rights, case, "answer this case" if body.kind == REPLY_OUT else "add notes")
    words = body.body or ""
    if body.saved_reply_id:
        saved = await session.get(SupportSavedReplies, body.saved_reply_id)
        if saved is None:
            raise _refuse(422, "There's no such saved reply.", "SAVED_REPLY_NOT_FOUND", {"field": "saved_reply_id"})
        if not words.strip():
            words = saved.body
    words = text.fill(words, name=(case.requester_name or "").split(" ")[0] or None, number=case.number, subject=case.subject).strip()
    if not words:
        raise _refuse(422, "Write something first.", "EMPTY_MESSAGE", {"field": "body"})
    if len(words) > cases.MAX_TEAM_MESSAGE:
        raise _refuse(422, "That's too long for one message.", "TEXT_TOO_LONG", {"field": "body"})
    html = text.markdown_lite(words)
    if body.preview_only:
        return {
            "preview": True, "kind": body.kind, "to": case.requester_email if body.kind == REPLY_OUT else None,
            "from": mail.from_address() if body.kind == REPLY_OUT else None,
            "subject": text.mail_subject(case.number, case.subject) if body.kind == REPLY_OUT else None,
            "text": text.plain(words), "html": html,
            "will_push": body.kind == REPLY_OUT and case.requester_kind == "user" and bool(case.requester_user_id),
        }
    if case.purged_at is not None:
        raise _refuse(409, "This case's messages were deleted (retention); open a new one.", "CASE_PURGED")
    attachments = await _team_attachments(session, body.attachments, admin)
    if body.kind == REPLY_OUT and not (case.requester_email or (case.requester_kind == "user" and case.requester_user_id)):
        raise _refuse(422, "There's no address or app account to answer; add a note instead.", "NO_WAY_TO_REPLY")
    msg = await cases.add_message(
        session, case, body.kind, body_text=words, body_html=html, author_user_id=admin.id, author_name=admin.full_name,
        attachment_rows=attachments, saved_reply_id=body.saved_reply_id,
    )
    if body.kind == REPLY_OUT:
        await mail.enqueue_reply(session, case, msg)
    await cases.audit(session, admin, "case.reply" if body.kind == REPLY_OUT else "case.note", case,
                      f"{'Answered' if body.kind == REPLY_OUT else 'Added a note to'} case #{case.number}",
                      {"message_id": msg.id, "files": len(attachments), "saved_reply_id": body.saved_reply_id}, request=request)
    await session.commit()
    files_out = await cases.attachments_by_message(session, [msg.id])
    return cases.message_out(msg, files_out.get(msg.id, []), _admin_url)


@router.post("/support/cases/{case_id}/merge", summary="Merge a case into another")
async def merge_case(request: Request, case_id: int, body: MergeIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    case = await cases.get_case(session, case_id, lock=True)
    cases.require_work(rights, case, "merge this case")
    into = await cases.get_case(session, body.into_id, lock=True)
    target = await cases.merge(session, case, into)
    await cases.audit(session, admin, "case.merge", case, f"Merged case #{case.number} into #{target.number}",
                      {"into": target.number}, request=request)
    await session.commit()
    return await _detail(session, target, rights)


@router.post("/support/cases/{case_id}/to-lead", summary="Turn a case into a sales lead", status_code=201)
async def case_to_lead(request: Request, case_id: int, body: ToLeadIn, session: SessionDep, admin: AdminAuth) -> dict:
    from app.api.leads import service as leads

    rights = await teams.rights_of(session, admin)
    rights.require("turn a case into a lead", support=True, sales=True)
    case = await cases.get_case(session, case_id, lock=True)
    lead = await leads.from_case(session, case, body.type, admin, rights, name=body.name, force=body.force)
    await cases.audit(session, admin, "case.to_lead", case, f"Case #{case.number} became lead {lead.name}", {"lead_id": lead.id},
                      request=request)
    await session.commit()
    return await leads.detail(session, lead, rights)


@router.get("/support/attachments/{attachment_id}", summary="A file on a case (the teams and owners)")
async def get_attachment(attachment_id: int, session: SessionDep, admin: AdminAuth):
    rights = await teams.rights_of(session, admin)
    rights.require("open the case's files", support=True, sales=True)
    row = await session.get(SupportAttachments, attachment_id)
    if row is None or row.case_id is None:
        raise _refuse(404, "There's no such file.", "FILE_NOT_FOUND")
    return await files.serve(row)


# ─── My day, the badge, the reports ──────────────────────────────────────


@router.get("/teamwork/my-day", summary="What is due and overdue for me, and my open cases")
async def my_day(session: SessionDep, admin: AdminAuth) -> dict:
    return await reports.my_day(session, admin.id)


@router.get("/teamwork/badge", summary="The side menu's badge: due, overdue, cases to answer")
async def badge(session: SessionDep, admin: AdminAuth) -> dict:
    return await reports.badge(session, admin.id)


@router.get("/teamwork/reports", summary="Reply and resolution times, load, the pipeline, won and lost")
async def team_reports(
    session: SessionDep, admin: AdminAuth, from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
    fmt: ListFormat = Query("json", alias="format"),
):
    if from_ and to and from_ > to:
        raise _refuse(422, "from has to be before to.", "INVALID_RANGE")
    data = await reports.reports(session, from_, to)
    if fmt == "xlsx":
        path = await spreadsheet.write_xlsx(reports.report_rows(data), title="Team work report")
        return spreadsheet.xlsx_response(path, f"teamwork-report-{date.today().isoformat()}.xlsx", cache_control="no-store")
    return data

