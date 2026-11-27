"""The admin portal's WhatsApp inbox for MyOtherApp's own number.

Admins only, with the owner or support role (as the platform's mail inbox,
app/api/platform_mail): a viewer, a club or a guest is refused. Only the
platform's number is here: a club's own number's conversations are never
read or shown by anything in this router.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.platform_whatsapp import service
from app.core.auth.permissions import ModerateAuth
from app.core.rate_limit import limiter
from app.db.core import SessionDep

router = APIRouter(prefix="/admin/whatsapp/inbox", tags=["admin-whatsapp-inbox"])

ACTION = "platform_whatsapp"


class ReplyIn(BaseModel):
    mode: Literal["text", "template"]
    text: Optional[str] = Field(None, max_length=service.TEXT_MAX)
    template: Optional[str] = Field(None, max_length=80)
    params: Optional[dict[str, str]] = None


@router.get("", summary="The platform number's conversations, newest activity first (admin: owner, support)")
async def conversations(
    session: SessionDep, admin: ModerateAuth,
    club_id: Optional[int] = Query(None), event_id: Optional[int] = Query(None), unread: bool = Query(False),
    q: Optional[str] = Query(None, max_length=80), page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
) -> dict:
    return await service.list_conversations(session, club_id=club_id, event_id=event_id, unread=unread, q=q, page=page,
                                            page_size=page_size)


@router.get("/unread", summary="How many conversations and messages wait unread (for the sidebar)")
async def unread(session: SessionDep, admin: ModerateAuth) -> dict:
    return await service.unread_totals(session)


@router.get("/templates", summary="The templates an admin may send outside the 24-hour window, and their values")
async def templates(admin: ModerateAuth) -> dict:
    return {"templates": service.templates_out()}


@router.get("/{conversation_id}", summary="A conversation's messages, a page at a time (newest page first)")
async def get_thread(
    conversation_id: int, session: SessionDep, admin: ModerateAuth,
    before: Optional[datetime] = Query(None), limit: int = Query(50, ge=1, le=service.PAGE_MAX),
) -> dict:
    conv = await service.get_conversation(session, conversation_id)
    return await service.thread(session, conv, before=before, limit=limit)


@router.post("/{conversation_id}/read", summary="Mark a conversation read")
async def read(conversation_id: int, session: SessionDep, admin: ModerateAuth) -> dict:
    conv = await service.get_conversation(session, conversation_id)
    await service.mark_read(session, conv)
    await session.commit()
    return {"id": conv.id, "unread_count": 0, **await service.unread_totals(session)}


@router.post("/{conversation_id}/messages", summary="Reply: free text inside the 24-hour window, else an approved template")
@limiter.limit("20/minute")
async def reply(conversation_id: int, payload: ReplyIn, request: Request, session: SessionDep, admin: ModerateAuth) -> dict:
    """409 WINDOW_CLOSED (text outside the window) or OPTED_OUT, with
    `can_send` saying what is allowed and until when; 422 for an unknown
    template or missing values; 503 when the number is not set up. The
    reply is kept with the admin who sent it, and audited (never its words)."""
    from app.api.audit import service as audit

    conv = await service.get_conversation(session, conversation_id)
    row = await service.send(session, conv, mode=payload.mode, admin=admin, text=payload.text, template=payload.template,
                             params=payload.params)
    await audit.record(
        session, actor=admin, action=f"{ACTION}.reply", entity_type="platform_whatsapp", entity_id=conv.id,
        summary=f"Replied on WhatsApp from MyOtherApp's number ({payload.mode})",
        details={"conversation_id": conv.id, "message_id": row.id, "mode": payload.mode, "template": row.template_name,
                 "status": row.status, "event_id": row.event_id, "club_id": row.club_id},
        request=request,
    )
    await session.commit()
    events, clubs = await service._names(session, {row.event_id} if row.event_id else set(), {row.club_id} if row.club_id else set())
    return {"message": service.message_out(row, events, clubs), "can_send": service.can_send(conv)}
