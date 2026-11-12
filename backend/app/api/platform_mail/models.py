"""The platform's own inbox: admin@, developer@, developers@, info@ (settings.PLATFORM_INBOX_ADDRESSES).

The club mailbox's tables, without a club: each thread and each message
names its `mailbox` instead -- the address's local part (`info`).

- `platform_mail_threads`: a conversation at one of the addresses (the one
  it started at): its folder (inbox, sent, archive, spam), subject, the
  outside people in it, what is unread. Read state is the admins' together.
- `platform_mail_messages`: each mail in or out, as club_mail_messages. A
  received one is kept once per (mailbox, provider, provider's id): a mail
  to info@ and admin@ is in each, a delivery again in neither twice.
- `platform_mail_attachments`: each file, in the media bucket under
  `platform-mail/<message>/<n>` (private, never on the CDN), served only
  through the admin API.

Shown only to admins with the owner or support role (never a viewer);
nothing a mail says reaches a log or the audit log.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text

from app.api.club_mailbox.models import FOLDERS, IN, INBOX, OUT, STATUSES  # noqa: F401 - one vocabulary for both
from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlatformMailThreads(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "platform_mail_threads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    #: The address's local part it belongs to: info, admin, developer, ...
    mailbox = Column(String(64), nullable=False)
    folder = Column(String(8), nullable=False, default=INBOX, server_default=INBOX)
    subject = Column(String(300), nullable=True)
    subject_key = Column(String(300), nullable=True)
    participants = Column(JSON, nullable=True)
    message_count = Column(Integer, nullable=False, default=0, server_default="0")
    unread_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_message_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_inbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_outbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_from = Column(String(200), nullable=True)
    snippet = Column(String(200), nullable=True)
    has_attachments = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint("folder IN ('inbox', 'sent', 'archive', 'spam')", name="ck_platform_mail_threads_folder"),
        Index("ix_platform_mail_threads_mailbox_folder_last", "mailbox", "folder", text("last_message_at DESC NULLS LAST"), text("id DESC")),
        Index("ix_platform_mail_threads_folder_last", "folder", text("last_message_at DESC NULLS LAST"), text("id DESC")),
        Index("ix_platform_mail_threads_sent", text("last_outbound_at DESC"), postgresql_where=text("last_outbound_at IS NOT NULL")),
        Index("ix_platform_mail_threads_mailbox_subject", "mailbox", "subject_key"),
    )


class PlatformMailMessages(AbstractSQLModel):
    __tablename__ = "platform_mail_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    thread_id = Column(BigInteger, ForeignKey("platform_mail_threads.id", ondelete="CASCADE"), nullable=False)
    #: The address it came to, or went from (local part).
    mailbox = Column(String(64), nullable=False)
    direction = Column(String(3), nullable=False)
    status = Column(String(12), nullable=False)
    message_id = Column(String(400), nullable=True)
    alt_message_id = Column(String(400), nullable=True)
    in_reply_to = Column(String(400), nullable=True)
    references = Column(Text, nullable=True)
    provider = Column(String(16), nullable=True)
    provider_message_id = Column(String(200), nullable=True)
    delivered_to = Column(String(320), nullable=True)
    from_address = Column(String(320), nullable=True)
    from_name = Column(String(200), nullable=True)
    to_addresses = Column(JSON, nullable=True)
    cc_addresses = Column(JSON, nullable=True)
    bcc_addresses = Column(JSON, nullable=True)
    reply_to = Column(String(320), nullable=True)
    subject = Column(String(300), nullable=True)
    text_body = Column(Text, nullable=True)
    #: Sanitised as the club mailbox's (club_mailbox/sanitise.py).
    html_body = Column(Text, nullable=True)
    has_remote_images = Column(Boolean, nullable=False, default=False, server_default="false")
    snippet = Column(String(200), nullable=True)
    verdicts = Column(JSON, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    unread = Column(Boolean, nullable=False, default=False, server_default="false")
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    #: The admin who wrote it, and their name as it was.
    sent_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sent_by_name = Column(String(120), nullable=True)
    error = Column(String(400), nullable=True)

    __table_args__ = (
        CheckConstraint("direction IN ('in', 'out')", name="ck_platform_mail_messages_direction"),
        CheckConstraint(
            "status IN ('received', 'sending', 'sent', 'failed', 'bounced', 'complained')", name="ck_platform_mail_messages_status",
        ),
        UniqueConstraint("mailbox", "provider", "provider_message_id", name="uq_platform_mail_messages_provider"),
        Index("ix_platform_mail_messages_thread", "thread_id", "id"),
        Index("ix_platform_mail_messages_mailbox_message_id", "mailbox", "message_id", postgresql_where=text("message_id IS NOT NULL")),
        Index("ix_platform_mail_messages_mailbox_alt_id", "mailbox", "alt_message_id", postgresql_where=text("alt_message_id IS NOT NULL")),
        Index("ix_platform_mail_messages_provider_id", "provider_message_id", postgresql_where=text("direction = 'out'")),
        Index("ix_platform_mail_messages_out_created", "created_at", postgresql_where=text("direction = 'out'")),
        Index("ix_platform_mail_messages_sent_by", "sent_by_user_id", postgresql_where=text("sent_by_user_id IS NOT NULL")),
    )


class PlatformMailAttachments(AbstractSQLModel):
    __tablename__ = "platform_mail_attachments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, ForeignKey("platform_mail_messages.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=True)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    #: In the media bucket: platform-mail/<message>/<n>. Never public.
    storage_key = Column(String(300), nullable=False)
    content_id = Column(String(200), nullable=True)
    inline = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (Index("ix_platform_mail_attachments_message", "message_id"),)
