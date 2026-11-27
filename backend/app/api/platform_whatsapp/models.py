"""The admin portal's WhatsApp inbox for MyOtherApp's own number (the one
that sends every club's tickets, reminders and certificates).

- `platform_wa_conversations`: one per guest who wrote to the platform's
  number, keyed by their WhatsApp id (digits with the country code) and/or
  their BSUID. Holds the 24-hour window (`last_inbound_at`), what waits
  unread, the event and club the person was most recently messaged for
  ("probably about"), and whether they wrote STOP.
- `platform_wa_messages`: what they wrote (text, media's metadata -- never
  the file itself) and what the admins answered by hand. The automatic sends
  (tickets, reminders, certificates) are NOT copied here: the thread reads
  them from `delivery_attempts`, where the pipeline already keeps them.

Only the platform's number is ever kept here (its phone number id, from
the server's .env): a club's own number has its own inbox
(app/api/club_whatsapp), which the admins never see.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKey, Index, Integer, JSON, String, Text, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

IN, OUT = "in", "out"
RECEIVED, PENDING, SENT, DELIVERED, READ, FAILED = "received", "pending", "sent", "delivered", "read", "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlatformWaConversations(AbstractSQLModel):
    __tablename__ = "platform_wa_conversations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    wa_id = Column(String(20), nullable=True)
    bsuid = Column(String(160), nullable=True)
    profile_name = Column(String(120), nullable=True)
    username = Column(String(120), nullable=True)
    #: The 24-hour window opens from here.
    last_inbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_message_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    last_preview = Column(String(160), nullable=True)
    last_direction = Column(String(3), nullable=True)
    unread_count = Column(Integer, nullable=False, default=0, server_default="0")
    #: Probably about: the event (and its club) this number was last messaged for.
    about_event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True)
    about_club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True, index=True)
    #: They wrote STOP (START clears it): no reply goes to them from the inbox.
    opted_out_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="ck_platform_wa_conversations_who"),
        Index("uq_platform_wa_conversations_wa", "wa_id", unique=True, postgresql_where=text("wa_id IS NOT NULL")),
        Index("uq_platform_wa_conversations_bsuid", "bsuid", unique=True, postgresql_where=text("bsuid IS NOT NULL")),
        Index("ix_platform_wa_conversations_last", "last_message_at"),
    )


class PlatformWaMessages(AbstractSQLModel):
    __tablename__ = "platform_wa_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(BigInteger, ForeignKey("platform_wa_conversations.id", ondelete="CASCADE"), nullable=False)
    #: in | out
    direction = Column(String(3), nullable=False)
    #: text, image, document, ... or template
    type = Column(String(20), nullable=False)
    body = Column(Text, nullable=True)
    media_id = Column(String(128), nullable=True)
    media_mime = Column(String(100), nullable=True)
    media_filename = Column(String(255), nullable=True)
    template_name = Column(String(80), nullable=True)
    template_params = Column(JSON, nullable=True)
    wamid = Column(String(128), nullable=True)
    #: received | pending | sent | delivered | read | failed
    status = Column(String(12), nullable=False)
    error = Column(String(300), nullable=True)
    #: The label: the event and club it is (probably) about.
    event_id = Column(Integer, ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True)
    #: The admin who sent a manual reply.
    sent_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sent_by_name = Column(String(120), nullable=True)
    at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("direction IN ('in', 'out')", name="ck_platform_wa_messages_direction"),
        Index("uq_platform_wa_messages_wamid", "wamid", unique=True, postgresql_where=text("wamid IS NOT NULL")),
        Index("ix_platform_wa_messages_conversation", "conversation_id", "at"),
        Index("ix_platform_wa_messages_sent_by", "sent_by_user_id", postgresql_where=text("sent_by_user_id IS NOT NULL")),
    )
