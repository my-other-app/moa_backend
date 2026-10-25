"""Replying to one message: Meta's `context.message_id`, and the quoted
bubble the inbox shows above a reply (theirs or the club's).

A text or an attachment from the inbox may answer a message of the
conversation; WhatsApp then shows that message quoted above it. Meta: a
template doesn't show the quote, and a reaction is never a reply.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from app.api.club_whatsapp.models import ClubWhatsAppConversations, ClubWhatsAppMessages
from app.response import CustomHTTPException

#: What can't be answered with a quote: a reaction, a notice from WhatsApp itself.
NOT_QUOTABLE = frozenset({"reaction", "system"})


async def context_for(session, conv: ClubWhatsAppConversations, reply_to: Optional[int]) -> Optional[str]:
    """The wamid a reply names (`context.message_id`), or None for none."""
    if reply_to is None:
        return None
    msg = await session.get(ClubWhatsAppMessages, int(reply_to))
    if msg is None or msg.conversation_id != conv.id:
        raise CustomHTTPException(status_code=404, message="No such message in this conversation.", error_code="MESSAGE_NOT_FOUND")
    if not msg.wamid or msg.type in NOT_QUOTABLE:
        raise CustomHTTPException(status_code=409, message="That message can't be replied to.", error_code="CANT_REPLY_TO_IT")
    return msg.wamid


async def quotes(session, conv_id: int, rows: list[ClubWhatsAppMessages]) -> dict[str, dict]:
    """The messages these reply to (or react to), by wamid: who sent it and a
    line of it, for the quoted bubble. Only this conversation's."""
    from app.api.club_whatsapp import inbox

    wanted = {m.context_wamid for m in rows if m.context_wamid}
    if not wanted:
        return {}
    M = ClubWhatsAppMessages
    found = (await session.scalars(select(M).where(M.conversation_id == conv_id, M.wamid.in_(wanted)))).all()
    return {
        m.wamid: {"id": m.id, "direction": m.direction, "type": m.type,
                  "preview": inbox.preview(m.type, m.body, filename=m.media_filename, template=m.template_name)}
        for m in found
    }
