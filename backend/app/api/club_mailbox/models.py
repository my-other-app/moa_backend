"""A club's mailbox: one address on the platform's domain, its mail, its threads.

Pro and Enterprise (plan flag `mailbox`). The club picks a handle and gets
`<handle>@mail.myotherapp.in` (settings.MAILBOX_DOMAIN). Mail sent to it
arrives through SES (receipt rule -> S3 -> SNS -> our webhook, inbound.py)
and is kept here; the club's team -- the owner and the members given the
Mail permission -- reads it in the dashboard, answers it and writes new mail,
which goes through the platform's own sending provider from that address.

- `club_mailboxes`: one per club: the name it sends as, its signature, how
  long its mail is kept, and -- when its plan stops including the mailbox --
  when that was found (`lapsed_at`): from then nothing new is accepted or
  sent, the mail stays readable for 30 days, then only exported or deleted.
- `club_mailbox_addresses`: the handles, current and past. The current one
  has `retired_at` null; a changed one keeps forwarding into the same mailbox
  until `forward_until` (90 days), and holds its handle until the sweep lets
  it go. A handle is unique across every row, so no club can take another's
  address, or an old one still forwarding.
- `club_mail_threads`: a conversation: its folder (inbox, sent, archive,
  spam), subject, the outside people in it, what is unread.
- `club_mail_messages`: each mail in or out: headers for threading
  (Message-ID, In-Reply-To, References; SES's own Message-ID for one it
  sent), who from and to, the text and the sanitised HTML, SES's verdicts on
  one received, the status of one sent.
- `club_mail_attachments`: each file, kept in the media bucket under a
  private folder (mailbox/<club>/..., never on the CDN), served only through
  the authenticated proxy.

Mail is personal data: shown only to the club's own account and the members
it gave the Mail permission, never to a platform admin (impersonating or
not), never written to a log or the audit log. Deleted after the club's
retention period (12 months unless it chose 1 to 36).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin

#: Folders a thread is in. `sent`: started by the club, nothing back yet.
INBOX = "inbox"
SENT = "sent"
ARCHIVE = "archive"
SPAM = "spam"
FOLDERS = (INBOX, SENT, ARCHIVE, SPAM)

IN = "in"
OUT = "out"

#: A received mail is `received`. One the club sends: `sending`, then
#: `sent`, or `failed` (the provider refused it, or couldn't be reached);
#: later `bounced` or `complained` when the provider says so.
RECEIVED = "received"
SENDING = "sending"
SENT_STATUS = "sent"
FAILED = "failed"
BOUNCED = "bounced"
COMPLAINED = "complained"
STATUSES = (RECEIVED, SENDING, SENT_STATUS, FAILED, BOUNCED, COMPLAINED)

DEFAULT_RETENTION_MONTHS = 12


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClubMailboxes(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_mailboxes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, unique=True)
    #: The name in From ("Yukthi"); null: the club's name.
    display_name = Column(String(80), nullable=True)
    #: Added under what the team writes (plain text).
    signature = Column(Text, nullable=True)
    retention_months = Column(Integer, nullable=False, default=DEFAULT_RETENTION_MONTHS, server_default=str(DEFAULT_RETENTION_MONTHS))
    #: When the handle last changed: once a month at most.
    handle_changed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the plan was found not to include the mailbox (null: it does).
    lapsed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: The owner told the mailbox closed, and later that its 30 days are up.
    lapse_told_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    expiry_told_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Mail that came while it was closed, dropped (a count, nothing of it kept).
    dropped_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_received_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        CheckConstraint("retention_months BETWEEN 1 AND 36", name="ck_club_mailboxes_retention"),
        Index("ix_club_mailboxes_created_by", "created_by", postgresql_where=text("created_by IS NOT NULL")),
        Index("ix_club_mailboxes_created_by_member", "created_by_member_id", postgresql_where=text("created_by_member_id IS NOT NULL")),
    )


class ClubMailboxAddresses(AbstractSQLModel):
    __tablename__ = "club_mailbox_addresses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    mailbox_id = Column(Integer, ForeignKey("club_mailboxes.id", ondelete="CASCADE"), nullable=False)
    #: Lower case, the part before the @.
    handle = Column(String(30), nullable=False, unique=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    #: Set when another handle replaced it.
    retired_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Mail to it still arrives in the mailbox until then.
    forward_until = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_club_mailbox_addresses_current", "club_id", unique=True, postgresql_where=text("retired_at IS NULL")),
        Index("ix_club_mailbox_addresses_mailbox", "mailbox_id"),
        CheckConstraint("handle = lower(handle)", name="ck_club_mailbox_addresses_lower"),
    )


class ClubMailThreads(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "club_mail_threads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    folder = Column(String(8), nullable=False, default=INBOX, server_default=INBOX)
    subject = Column(String(300), nullable=True)
    #: The subject without Re:/Fwd: and the like, lower case: what a reply
    #: without its headers is matched by (with its people, within 30 days).
    subject_key = Column(String(300), nullable=True)
    #: The outside addresses in it (lower case): who wrote, and to whom.
    participants = Column(JSON, nullable=True)
    message_count = Column(Integer, nullable=False, default=0, server_default="0")
    unread_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_message_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_inbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_outbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Who the latest mail is from, and its first words.
    last_from = Column(String(200), nullable=True)
    snippet = Column(String(200), nullable=True)
    has_attachments = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint("folder IN ('inbox', 'sent', 'archive', 'spam')", name="ck_club_mail_threads_folder"),
        Index("ix_club_mail_threads_club_folder_last", "club_id", "folder", text("last_message_at DESC NULLS LAST"), text("id DESC")),
        Index("ix_club_mail_threads_club_sent", "club_id", text("last_outbound_at DESC"), postgresql_where=text("last_outbound_at IS NOT NULL")),
        Index("ix_club_mail_threads_club_subject", "club_id", "subject_key"),
    )


class ClubMailMessages(AbstractSQLModel):
    __tablename__ = "club_mail_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    thread_id = Column(BigInteger, ForeignKey("club_mail_threads.id", ondelete="CASCADE"), nullable=False)
    direction = Column(String(3), nullable=False)
    status = Column(String(12), nullable=False)
    #: The RFC 5322 Message-ID, with its angle brackets; and, for one SES
    #: sent, the Message-ID SES replaced it with (what a reply quotes).
    message_id = Column(String(400), nullable=True)
    alt_message_id = Column(String(400), nullable=True)
    in_reply_to = Column(String(400), nullable=True)
    references = Column(Text, nullable=True)
    #: The provider and its id: SES's message id (the S3 object key) for one
    #: received, the sending provider's for one sent.
    provider = Column(String(16), nullable=True)
    provider_message_id = Column(String(200), nullable=True)
    #: Which of the club's addresses it came to (an old one still forwarding, say).
    delivered_to = Column(String(320), nullable=True)
    from_address = Column(String(320), nullable=True)
    from_name = Column(String(200), nullable=True)
    to_addresses = Column(JSON, nullable=True)
    cc_addresses = Column(JSON, nullable=True)
    #: Kept only for one the club sends (never shown to the other people).
    bcc_addresses = Column(JSON, nullable=True)
    reply_to = Column(String(320), nullable=True)
    subject = Column(String(300), nullable=True)
    text_body = Column(Text, nullable=True)
    #: Sanitised (sanitise.py): no script, no form, no event handler; remote
    #: pictures stay as they are and are blocked where it is shown until the
    #: reader asks for them (`has_remote_images`).
    html_body = Column(Text, nullable=True)
    has_remote_images = Column(Boolean, nullable=False, default=False, server_default="false")
    snippet = Column(String(200), nullable=True)
    #: SES's verdicts on one received: spam, virus, spf, dkim, dmarc.
    verdicts = Column(JSON, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    unread = Column(Boolean, nullable=False, default=False, server_default="false")
    #: The Date header (one received), or when it went (one sent).
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    #: Who of the team wrote it.
    sent_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sent_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    sent_by_name = Column(String(120), nullable=True)
    #: Why one sent failed or bounced, in the provider's words.
    error = Column(String(400), nullable=True)

    __table_args__ = (
        CheckConstraint("direction IN ('in', 'out')", name="ck_club_mail_messages_direction"),
        CheckConstraint(
            "status IN ('received', 'sending', 'sent', 'failed', 'bounced', 'complained')", name="ck_club_mail_messages_status",
        ),
        # The same mail delivered twice (SNS retries) is kept once.
        UniqueConstraint("club_id", "provider", "provider_message_id", name="uq_club_mail_messages_provider"),
        Index("ix_club_mail_messages_thread", "thread_id", "id"),
        Index("ix_club_mail_messages_club_created", "club_id", "created_at"),
        Index("ix_club_mail_messages_club_message_id", "club_id", "message_id", postgresql_where=text("message_id IS NOT NULL")),
        Index("ix_club_mail_messages_club_alt_id", "club_id", "alt_message_id", postgresql_where=text("alt_message_id IS NOT NULL")),
        Index("ix_club_mail_messages_provider_id", "provider_message_id", postgresql_where=text("direction = 'out'")),
        Index("ix_club_mail_messages_sent_by", "sent_by_user_id", postgresql_where=text("sent_by_user_id IS NOT NULL")),
        Index("ix_club_mail_messages_sent_by_member", "sent_by_member_id", postgresql_where=text("sent_by_member_id IS NOT NULL")),
    )


class ClubMailAttachments(AbstractSQLModel):
    __tablename__ = "club_mail_attachments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(BigInteger, ForeignKey("club_mail_messages.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=True)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    #: In the media bucket: mailbox/<club>/<message>/<n>. Never public.
    storage_key = Column(String(300), nullable=False)
    #: A picture the HTML shows by `cid:` (its Content-ID, without brackets).
    content_id = Column(String(200), nullable=True)
    inline = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (Index("ix_club_mail_attachments_message", "message_id"), Index("ix_club_mail_attachments_club", "club_id"))
