"""The sales team's leads in the admin portal (prefix /api/v1/admin) and the
For-clubs page's "Talk to us" form (prefix /api/v1/leads)."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from sqlalchemy import select

from app.api.leads import imports, service
from app.api.leads.models import FollowUps, LeadContacts, LeadFiles
from app.api.leads.schemas import ActivityIn, ContactIn, FollowUpIn, FollowUpPatch, LeadIn, LeadPatch, MergeIn, TalkToUsIn
from app.api.support import teams
from app.api.support.models import SALES
from app.core.auth.dependencies import AdminAuth
from app.core.net import client_address
from app.core.rate_limit import limiter
from app.core.utils import spreadsheet
from app.db.core import SessionDep
from app.response import CustomHTTPException

public_router = APIRouter(prefix="/leads", tags=["leads"])
router = APIRouter(prefix="/admin", tags=["admin", "leads"])

TALK_LIMIT = "5/hour;20/day"
ListFormat = Literal["json", "xlsx"]


def _refuse(code: int, message: str, error_code: str, errors: Optional[dict] = None) -> CustomHTTPException:
    return CustomHTTPException(code, message=message, error_code=error_code, errors=errors)


def _address(request: Request) -> str:
    return client_address(request) or "unknown"


@public_router.post("/talk-to-us", summary="The For-clubs page's Talk to us form: a lead for the sales team")
@limiter.limit(TALK_LIMIT, key_func=_address)
async def talk_to_us(request: Request, body: TalkToUsIn, session: SessionDep) -> dict:
    await service.talk_to_us(session, body, request=request)
    await session.commit()
    return {"ok": True}


# ─── The lists ───────────────────────────────────────────────────────────


@router.get("/leads/import/template", summary="The import's template (xlsx), its columns explained")
async def import_template(admin: AdminAuth) -> Response:
    return Response(
        content=imports.template_bytes(), media_type=spreadsheet.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="leads-import-template.xlsx"', "Cache-Control": "no-store"},
    )


@router.post("/leads/import", summary="Leads from an Excel or CSV sheet (dry_run first)")
async def import_leads(
    request: Request, session: SessionDep, admin: AdminAuth, file: UploadFile = File(...), dry_run: bool = Form(True),
) -> dict:
    rights = await teams.rights_of(session, admin)
    data = await file.read(imports.MAX_BYTES + 1)
    out = await imports.run(session, data, file.filename or "", dry_run=dry_run, actor=admin, rights=rights)
    if not dry_run:
        from app.api.audit import service as audit

        await audit.record(session, actor=admin, action="leads.import", entity_type="lead", entity_id=None,
                           summary=f"Imported {out.get('created', 0)} leads", details=out["counts"], request=request)
        await session.commit()
    else:
        await session.rollback()
    return out


@router.get("/leads", summary="The leads: a board by stage or a table")
async def list_leads(
    session: SessionDep, admin: AdminAuth, view: Literal["board", "table"] = "table", type: Optional[str] = None,
    stage: Optional[str] = None, owner: Optional[str] = None, city: Optional[str] = Query(None, max_length=120),
    due: Optional[Literal["today", "overdue", "week"]] = None, q: Optional[str] = Query(None, max_length=200),
    page: int = Query(1, ge=1, le=10000), page_size: Optional[int] = Query(None, ge=1, le=500),
    fmt: ListFormat = Query("json", alias="format"),
    sort: Optional[str] = Query(None, pattern="^(name|type|stage|city|value|close|created|updated)$", description="The table's column; unset: latest activity first"),
    order: Optional[str] = Query(None, pattern="^(asc|desc)$"),
):
    f = dict(type=type, stage=stage, owner=owner, city=city, due=due, q=q)
    if fmt == "xlsx":
        rights = await teams.rights_of(session, admin)
        rights.require("export the leads", support=True, sales=True)
        rows = service.export_rows(session, admin.id, **f)
        header = await rows.__anext__()
        path = await spreadsheet.write_xlsx(rows, title="Leads", headers=header)
        return spreadsheet.xlsx_response(path, f"leads-{date.today().isoformat()}.xlsx", cache_control="no-store")
    size = page_size or (300 if view == "board" else 50)
    return await service.list_leads(session, admin.id, view=view, page=page, page_size=size, sort=sort, order=order, **f)


@router.post("/leads", summary="Add a lead (409 DUPLICATE_LEAD unless force)", status_code=201)
async def create_lead(request: Request, body: LeadIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    lead = await service.create(session, body, admin, rights, request=request)
    await session.commit()
    return await service.detail(session, lead, rights)


@router.get("/leads/{lead_id}", summary="A lead: contacts, timeline, files, follow-ups and linked records")
async def get_lead(lead_id: int, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    return await service.detail(session, await service.get_lead(session, lead_id), rights)


@router.patch("/leads/{lead_id}", summary="Change a lead, move its stage (lost needs lost_reason)")
async def update_lead(request: Request, lead_id: int, body: LeadPatch, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    lead = await service.get_lead(session, lead_id, lock=True)
    if lead.merged_into_id:
        raise _refuse(409, "This lead was merged into another.", "MERGED_LEAD", {"into_id": lead.merged_into_id})
    flags = await service.update_lead(session, lead, body, admin, rights, request=request)
    await session.commit()
    return {**await service.detail(session, lead, rights), **flags}


@router.post("/leads/{lead_id}/merge", summary="Merge a lead into another, keeping both timelines")
async def merge_lead(request: Request, lead_id: int, body: MergeIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights = await teams.rights_of(session, admin)
    lead = await service.get_lead(session, lead_id, lock=True)
    into = await service.get_lead(session, body.into_id, lock=True)
    service.require_edit(rights, lead, "merge this lead")
    service.require_edit(rights, into, "merge into that lead")
    target = await service.merge(session, lead, into, admin)
    await service.audit(session, admin, "merge", lead, f"Merged the lead {lead.name} into {target.name}", {"into_id": target.id}, request)
    await session.commit()
    return await service.detail(session, target, rights)


# ─── Contacts ────────────────────────────────────────────────────────────


@router.get("/leads/{lead_id}/contacts", summary="A lead's people")
async def list_contacts(lead_id: int, session: SessionDep, admin: AdminAuth) -> list[dict]:
    await service.get_lead(session, lead_id)
    rows = (await session.scalars(
        select(LeadContacts).where(LeadContacts.lead_id == lead_id).order_by(LeadContacts.is_primary.desc(), LeadContacts.id)
    )).all()
    return [service.contact_out(c) for c in rows]


async def _editable(session, admin, lead_id: int, what: str):
    rights = await teams.rights_of(session, admin)
    lead = await service.get_lead(session, lead_id, lock=True)
    service.require_edit(rights, lead, what)
    return rights, lead


async def _primary_only(session, lead_id: int, keep_id: int) -> None:
    from sqlalchemy import update

    await session.execute(update(LeadContacts).where(LeadContacts.lead_id == lead_id, LeadContacts.id != keep_id)
                          .values(is_primary=False).execution_options(synchronize_session=False))


@router.post("/leads/{lead_id}/contacts", summary="Add a person to a lead", status_code=201)
async def add_contact(request: Request, lead_id: int, body: ContactIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights, lead = await _editable(session, admin, lead_id, "change its people")
    if not body.name and not body.email:
        raise _refuse(422, "A contact needs a name or an email.", "INVALID_CONTACT")
    c = LeadContacts(lead_id=lead.id, name=(body.name or str(body.email))[:120], role=body.role,
                     email=str(body.email).lower() if body.email else None, phone=body.phone, whatsapp=body.whatsapp,
                     preferred_channel=body.preferred_channel, is_primary=bool(body.is_primary))
    session.add(c)
    await session.flush()
    if c.is_primary:
        await _primary_only(session, lead.id, c.id)
    await service.refresh_domain(session, lead)
    await service.log(session, lead, "system", f"Added {c.name} as a contact.", actor=admin, contact_id=c.id)
    await service.audit(session, admin, "contact.add", lead, f"Added a contact to {lead.name}", {"contact_id": c.id}, request)
    await session.commit()
    return service.contact_out(c)


@router.patch("/leads/{lead_id}/contacts/{contact_id}", summary="Change a lead's contact")
async def update_contact(request: Request, lead_id: int, contact_id: int, body: ContactIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights, lead = await _editable(session, admin, lead_id, "change its people")
    c = await session.scalar(select(LeadContacts).where(LeadContacts.id == contact_id, LeadContacts.lead_id == lead.id))
    if c is None:
        raise _refuse(404, "There's no such contact.", "CONTACT_NOT_FOUND")
    for field in body.model_fields_set:
        value = getattr(body, field)
        if field == "email":
            value = str(value).lower() if value else None
        if field == "name" and not value:
            continue
        setattr(c, field, value if field != "is_primary" else bool(value))
    await session.flush()
    if c.is_primary:
        await _primary_only(session, lead.id, c.id)
    await service.refresh_domain(session, lead)
    await service.audit(session, admin, "contact.update", lead, f"Changed a contact of {lead.name}",
                        {"contact_id": c.id, "fields": sorted(body.model_fields_set)}, request)
    await session.commit()
    return service.contact_out(c)


@router.delete("/leads/{lead_id}/contacts/{contact_id}", summary="Remove a contact (removal on request too)")
async def delete_contact(request: Request, lead_id: int, contact_id: int, session: SessionDep, admin: AdminAuth) -> dict:
    rights, lead = await _editable(session, admin, lead_id, "change its people")
    c = await session.scalar(select(LeadContacts).where(LeadContacts.id == contact_id, LeadContacts.lead_id == lead.id))
    if c is None:
        raise _refuse(404, "There's no such contact.", "CONTACT_NOT_FOUND")
    await session.delete(c)
    await service.refresh_domain(session, lead)
    await service.log(session, lead, "system", "A contact was removed.", actor=admin)
    await service.audit(session, admin, "contact.remove", lead, f"Removed a contact from {lead.name}", {"contact_id": contact_id}, request)
    await session.commit()
    return {"id": contact_id, "deleted": True}


# ─── Timeline and files ──────────────────────────────────────────────────


@router.post("/leads/{lead_id}/activities", summary="Log a call, meeting, email, WhatsApp message or note", status_code=201)
async def add_activity(request: Request, lead_id: int, body: ActivityIn, session: SessionDep, admin: AdminAuth) -> dict:
    rights, lead = await _editable(session, admin, lead_id, "log on this lead")
    if body.contact_id is not None:
        found = await session.scalar(select(LeadContacts.id).where(LeadContacts.id == body.contact_id, LeadContacts.lead_id == lead.id))
        if found is None:
            raise _refuse(422, "That contact isn't on this lead.", "CONTACT_NOT_FOUND", {"field": "contact_id"})
    occurred = body.occurred_at
    if occurred is not None and occurred.tzinfo is None:
        from datetime import timezone

        occurred = occurred.replace(tzinfo=timezone.utc)
    a = await service.log(session, lead, body.kind, body.body, actor=admin, occurred_at=occurred, contact_id=body.contact_id)
    await service.audit(session, admin, "activity", lead, f"Logged a {body.kind} on {lead.name}", {"activity_id": a.id}, request)
    await session.commit()
    return {"id": a.id, "kind": a.kind, "body": a.body, "occurred_at": a.occurred_at, "contact_id": a.contact_id,
            "author_name": a.author_name, "needs_follow_up": lead.stage in ("new", "contacted", "meeting", "proposal", "negotiation")
            and await service.open_follow_ups(session, lead.id) == 0}


@router.post("/leads/{lead_id}/files", summary="Attach a file to a lead (private)", status_code=201)
async def add_file(request: Request, lead_id: int, session: SessionDep, admin: AdminAuth, file: UploadFile = File(...)) -> dict:
    from app.api.support import files

    rights, lead = await _editable(session, admin, lead_id, "add files to this lead")
    data = await files.read_upload(file, files.TEAM_MAX_BYTES)
    kind = files.sniff(data) or "application/octet-stream"
    name = files.safe_name(file.filename)
    row = await service.store_file(session, lead, data, kind, name, admin)
    await service.log(session, lead, "file", name, actor=admin, file_id=row.id)
    await service.audit(session, admin, "file.add", lead, f"Added a file to {lead.name}", {"file_id": row.id, "bytes": len(data)}, request)
    await session.commit()
    return {"id": row.id, "name": row.filename, "size": row.size_bytes, "content_type": row.content_type,
            "url": f"/api/v1/admin/leads/{lead.id}/files/{row.id}"}


@router.get("/leads/{lead_id}/files/{file_id}", summary="One of a lead's files")
async def get_file(lead_id: int, file_id: int, session: SessionDep, admin: AdminAuth):
    from app.api.support import files
    from app.api.support.models import SupportAttachments

    row = await session.scalar(select(LeadFiles).where(LeadFiles.id == file_id, LeadFiles.lead_id == lead_id))
    if row is None:
        raise _refuse(404, "There's no such file.", "FILE_NOT_FOUND")
    proxy = SupportAttachments(storage_key=row.storage_key, filename=row.filename, content_type=row.content_type)
    return await files.serve(proxy)


@router.delete("/leads/{lead_id}/files/{file_id}", summary="Delete a lead's file")
async def delete_file(request: Request, lead_id: int, file_id: int, session: SessionDep, admin: AdminAuth) -> dict:
    import asyncio

    from app.api.support import store

    rights, lead = await _editable(session, admin, lead_id, "delete its files")
    row = await session.scalar(select(LeadFiles).where(LeadFiles.id == file_id, LeadFiles.lead_id == lead.id))
    if row is None:
        raise _refuse(404, "There's no such file.", "FILE_NOT_FOUND")
    key = row.storage_key
    await session.delete(row)
    await service.audit(session, admin, "file.delete", lead, f"Deleted a file of {lead.name}", {"file_id": file_id}, request)
    await session.commit()
    await asyncio.to_thread(store.delete, [key])
    return {"id": file_id, "deleted": True}


# ─── Follow-ups ──────────────────────────────────────────────────────────


async def _follow_up_rights(session, admin, rights, f: FollowUps):
    """Whose follow-up it is: its owner, the lead's owner or the case's assignee, or an admin owner."""
    if rights.owner or f.owner_id == admin.id:
        return
    if f.lead_id:
        service.require_edit(rights, await service.get_lead(session, f.lead_id), "change its follow-ups")
        return
    from app.api.support import cases

    cases.require_work(rights, await cases.get_case(session, f.case_id), "change its follow-ups")


async def _owner_for(session, rights, admin, owner_id: Optional[int], team: str) -> Optional[int]:
    if owner_id is None or owner_id == admin.id:
        return owner_id if owner_id is not None else admin.id
    if not rights.owner:
        raise _refuse(403, "Only an admin owner can give someone else a follow-up.", "OWNER_REQUIRED", {"field": "owner_id"})
    if owner_id not in await teams.members_of(session, team):
        raise _refuse(422, "Follow-ups go to members of the team.", "NOT_IN_TEAM", {"field": "owner_id"})
    return owner_id


@router.post("/follow-ups", summary="A follow-up on a lead or a case", status_code=201)
async def create_follow_up(request: Request, body: FollowUpIn, session: SessionDep, admin: AdminAuth) -> dict:
    from app.api.support import cases
    from app.api.support.models import SUPPORT

    if (body.lead_id is None) == (body.case_id is None):
        raise _refuse(422, "A follow-up is on a lead or on a case.", "INVALID_FOLLOW_UP")
    rights = await teams.rights_of(session, admin)
    lead = None
    if body.lead_id is not None:
        lead = await service.get_lead(session, body.lead_id, lock=True)
        service.require_edit(rights, lead, "add follow-ups to this lead")
        owner = await _owner_for(session, rights, admin, body.owner_id, SALES)
    else:
        case = await cases.get_case(session, body.case_id)
        cases.require_work(rights, case, "add follow-ups to this case")
        owner = await _owner_for(session, rights, admin, body.owner_id, SUPPORT)
    f = await service.add_follow_up(session, lead=lead, case_id=body.case_id, kind=body.kind, title=body.title, due_at=body.due_at,
                                    owner_id=owner, actor=admin)
    if lead is not None:
        await service.log(session, lead, "system", f"Follow-up: {f.title}", actor=admin, details={"follow_up_id": f.id})
    from app.api.audit import service as audit

    await audit.record(session, actor=admin, action="follow_up.create", entity_type="follow_up", entity_id=f.id,
                       summary=f"Added a follow-up: {f.title}"[:255], details={"lead_id": f.lead_id, "case_id": f.case_id}, request=request)
    await session.commit()
    return service.follow_up_out(f, await teams.names(session, [f.owner_id]))


@router.patch("/follow-ups/{follow_up_id}", summary="Mark a follow-up done, or move it")
async def update_follow_up(request: Request, follow_up_id: int, body: FollowUpPatch, session: SessionDep, admin: AdminAuth) -> dict:
    from app.api.support.models import SUPPORT

    rights = await teams.rights_of(session, admin)
    f = await session.scalar(select(FollowUps).where(FollowUps.id == follow_up_id).with_for_update())
    if f is None:
        raise _refuse(404, "There's no such follow-up.", "FOLLOW_UP_NOT_FOUND")
    await _follow_up_rights(session, admin, rights, f)
    sent = body.model_fields_set
    if "owner_id" in sent and body.owner_id != f.owner_id:
        f.owner_id = await _owner_for(session, rights, admin, body.owner_id, SALES if f.lead_id else SUPPORT)
    if body.title is not None:
        f.title = body.title
    if body.kind is not None:
        f.kind = body.kind
    if body.due_at is not None:
        f.due_at = body.due_at
        f.overdue_flagged_at = None
    needs_next = False
    if body.done is not None and body.done != f.done:
        f.done = body.done
        f.done_at = service._now() if body.done else None
        f.done_by = admin.id if body.done else None
        if f.lead_id:
            lead = await service.get_lead(session, f.lead_id)
            await service.log(session, lead, "system", f"{'Done' if body.done else 'Not done after all'}: {f.title}", actor=admin,
                              details={"follow_up_id": f.id})
            if body.done:
                needs_next = lead.stage in ("new", "contacted", "meeting", "proposal", "negotiation") and \
                    await service.open_follow_ups(session, lead.id, exclude=f.id) == 0
    from app.api.audit import service as audit

    await audit.record(session, actor=admin, action="follow_up.update", entity_type="follow_up", entity_id=f.id,
                       summary=f"Changed a follow-up: {f.title}"[:255], details={"fields": sorted(sent), "done": f.done}, request=request)
    await session.commit()
    return {**service.follow_up_out(f, await teams.names(session, [f.owner_id])), "needs_next": needs_next}
