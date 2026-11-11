"""The team's support desk: its teams, topics, saved replies, help answers,
and the cases with their messages and files (docs/SUPPORT_AND_LEADS.md).

A case is one question from one person -- an app account, a club, or a guest
known only by an address -- with its whole thread: what they wrote
(`reply_in`), what we answered (`reply_out`), internal notes they never see
(`note`) and the desk's own lines (`system`). Its `number` is a sequence of
its own, shown as #1042, and goes in the subject of every mail about it so
an answer by mail finds its way back.

Teams sit on top of the admin roles (app/core/auth/permissions.py): the role
says what an admin may change on the platform, the team which queue they
work. The two teams are reference rows the migration inserts.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

SUPPORT, SALES = "support", "sales"
TEAMS = (SUPPORT, SALES)

# Case statuses, priorities, channels and message kinds: strings in JSON.
NEW, OPEN, WAITING, RESOLVED, CLOSED = "new", "open", "waiting", "resolved", "closed"
STATUSES = (NEW, OPEN, WAITING, RESOLVED, CLOSED)
#: What the desk still has to act on.
ACTIVE = (NEW, OPEN)
#: Not done with: in a queue (active, or waiting on them).
UNFINISHED = (NEW, OPEN, WAITING)
PRIORITIES = ("low", "normal", "high", "urgent")
CHANNELS = ("app", "web", "dashboard", "email", "manual")
REPLY_IN, REPLY_OUT, NOTE, SYSTEM = "reply_in", "reply_out", "note", "system"
KINDS = (REPLY_IN, REPLY_OUT, NOTE, SYSTEM)
#: Who asked: an app account, a club (and its team member), or a guest by address.
REQUESTER_KINDS = ("user", "guest", "club")
SPAM_TAG = "spam"
# Flags on a case, shown on its row.
UNKNOWN_SENDER, NOT_DELIVERED = "unknown_sender", "not_delivered"

#: A case's number: its own sequence, so #1042 never depends on the row's id.
CASE_NUMBER = sa.Sequence("support_case_number_seq", start=1001, metadata=AbstractSQLModel.metadata)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AdminTeams(AbstractSQLModel):
    """The two teams (`support`, `sales`), and the desk's settings on the
    support row (working hours, retention, reply targets: app/api/support/hours.py)."""

    __tablename__ = "admin_teams"

    key = Column(String(20), primary_key=True)
    label = Column(String(60), nullable=False)
    settings = Column(JSONB, nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))


class AdminTeamMembers(AbstractSQLModel):
    """An admin account in a team. `away` (until `away_until`, when set)
    takes them out of round robin and sends their open cases back to
    Unassigned. `last_assigned_at` is the round robin's turn."""

    __tablename__ = "admin_team_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team = Column(String(20), ForeignKey("admin_teams.key", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    away = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    away_until = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_assigned_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (sa.UniqueConstraint("team", "user_id", name="uq_admin_team_members_team_user"),)


class SupportTopics(AbstractSQLModel):
    """What a case is about, and who gets it: a named person, or round robin
    across the support team. Seven are reference rows the migration inserts."""

    __tablename__ = "support_topics"

    key = Column(String(30), primary_key=True)
    label = Column(String(80), nullable=False)
    assign = Column(String(12), nullable=False, default="round_robin", server_default="round_robin")
    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    default_priority = Column(String(8), nullable=False, default="normal", server_default="normal")
    position = Column(Integer, nullable=False, default=0, server_default="0")
    # A topic in use is archived, not deleted: old cases keep it.
    active = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint("assign IN ('round_robin', 'person')", name="ck_support_topics_assign"),
        sa.CheckConstraint(_in("default_priority", PRIORITIES), name="ck_support_topics_priority"),
    )


class SupportSavedReplies(AbstractSQLModel):
    __tablename__ = "support_saved_replies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(120), nullable=False)
    body = Column(Text, nullable=False)
    topic = Column(String(30), ForeignKey("support_topics.key", ondelete="SET NULL"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))


class SupportHelpAnswers(AbstractSQLModel):
    """A short answer to a common question, shown before the form in the
    app, on the website and in the club dashboard once published."""

    __tablename__ = "support_help_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String(30), ForeignKey("support_topics.key", ondelete="SET NULL"), nullable=True)
    question = Column(String(300), nullable=False)
    answer = Column(Text, nullable=False)
    published = Column(Boolean, nullable=False, default=False, server_default=sa.false())
    position = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))


class SupportCases(AbstractSQLModel):
    __tablename__ = "support_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    number = Column(BigInteger, CASE_NUMBER, server_default=CASE_NUMBER.next_value(), nullable=False, unique=True)
    subject = Column(String(200), nullable=False)
    status = Column(String(10), nullable=False, default=NEW, server_default=NEW)
    priority = Column(String(8), nullable=False, default="normal", server_default="normal")
    topic = Column(String(30), ForeignKey("support_topics.key"), nullable=True)
    channel = Column(String(10), nullable=False)

    requester_kind = Column(String(8), nullable=False)
    requester_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    requester_club_id = Column(Integer, ForeignKey("clubs.id", ondelete="SET NULL"), nullable=True, index=True)
    # The club team member who wrote (club_members.id), when a member's session did.
    requester_member_id = Column(Integer, nullable=True)
    requester_email = Column(String(254), nullable=True)
    requester_name = Column(String(120), nullable=True)

    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    tags = Column(JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb"))
    flags = Column(JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb"))
    # What it is about: {user_id, club_id, event_id, registration_id, payment_id, refund_id, dispute_id}.
    links = Column(JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb"))
    # Where they were when they asked: {platform, page_url, error_code, track_id, app_version}.
    context = Column(JSONB, nullable=True)

    # The timers, in working hours from when it came in (hours.py).
    first_reply_due = Column(TZAwareDateTime(timezone=True), nullable=True)
    resolution_due = Column(TZAwareDateTime(timezone=True), nullable=True)
    first_replied_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    waiting_since = Column(TZAwareDateTime(timezone=True), nullable=True)
    reminded_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    resolved_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    close_reason = Column(String(20), nullable=True)

    last_message_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_inbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    last_outbound_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # When the person last opened it in the app, website or dashboard: an
    # answer after this is unread for them.
    requester_read_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    message_count = Column(Integer, nullable=False, default=0, server_default="0")

    # "Did we solve it?": the link's token, when it went, and the answer.
    survey_token = Column(String(64), nullable=True, unique=True)
    survey_sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    survey_solved = Column(Boolean, nullable=True)
    survey_comment = Column(String(500), nullable=True)
    survey_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    # A reply long after it was closed starts a new case pointing here.
    parent_case_id = Column(Integer, ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True)
    merged_into_id = Column(Integer, ForeignKey("support_cases.id", ondelete="SET NULL"), nullable=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="SET NULL", use_alter=True, name="fk_support_cases_lead"), nullable=True)
    # Retention: the messages and files deleted, the counts kept for the reports.
    purged_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    updated_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint(_in("status", STATUSES), name="ck_support_cases_status"),
        sa.CheckConstraint(_in("priority", PRIORITIES), name="ck_support_cases_priority"),
        sa.CheckConstraint(_in("channel", CHANNELS), name="ck_support_cases_channel"),
        sa.CheckConstraint(_in("requester_kind", REQUESTER_KINDS), name="ck_support_cases_requester_kind"),
        # The queues: by status, newest change first.
        sa.Index("ix_support_cases_status_updated", "status", "updated_at"),
        # The sweep's reads: waiting ones, resolved ones, closed ones past retention.
        sa.Index("ix_support_cases_waiting", "waiting_since", postgresql_where=sa.text("status = 'waiting'")),
        sa.Index("ix_support_cases_resolved", "resolved_at", postgresql_where=sa.text("status = 'resolved'")),
        sa.Index("ix_support_cases_closed", "closed_at", postgresql_where=sa.text("status = 'closed' AND purged_at IS NULL")),
        sa.Index("ix_support_cases_requester_email", sa.text("lower(requester_email)")),
    )


class SupportMessages(AbstractSQLModel):
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(10), nullable=False)
    body_text = Column(Text, nullable=True)
    # Sanitised (an inbound mail's, through the club mailbox's cleaner), or
    # rendered from what the team wrote (a reply).
    body_html = Column(Text, nullable=True)
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name = Column(String(120), nullable=True)
    from_address = Column(String(254), nullable=True)
    to_addresses = Column(JSONB, nullable=True)
    saved_reply_id = Column(Integer, ForeignKey("support_saved_replies.id", ondelete="SET NULL"), nullable=True)

    # Threading: our Message-ID (or the one the mail came with), what it answered, its chain.
    email_message_id = Column(String(400), nullable=True)
    in_reply_to = Column(String(400), nullable=True)
    references = Column(Text, nullable=True)
    # The provider and its id: for an inbound mail, kept once; for a reply,
    # what the bounce notice names. `alt_message_id`: SES's own Message-ID.
    provider = Column(String(12), nullable=True)
    provider_message_id = Column(String(200), nullable=True)
    alt_message_id = Column(String(400), nullable=True)
    # A reply's mail: sending, sent, failed, bounced, complained; null for no mail.
    delivery_status = Column(String(12), nullable=True)
    delivery_error = Column(String(400), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.CheckConstraint(_in("kind", KINDS), name="ck_support_messages_kind"),
        sa.Index("ix_support_messages_case", "case_id", "created_at"),
        # One mail kept once, however often SNS delivers its notice.
        sa.Index(
            "uq_support_messages_inbound", "provider", "provider_message_id", unique=True,
            postgresql_where=sa.text("kind = 'reply_in' AND provider_message_id IS NOT NULL"),
        ),
        sa.Index("ix_support_messages_provider_id", "provider_message_id", postgresql_where=sa.text("provider_message_id IS NOT NULL")),
        sa.Index("ix_support_messages_email_id", "email_message_id", postgresql_where=sa.text("email_message_id IS NOT NULL")),
        sa.Index("ix_support_messages_alt_id", "alt_message_id", postgresql_where=sa.text("alt_message_id IS NOT NULL")),
    )


class SupportAttachments(AbstractSQLModel):
    """A file on a case: a screenshot from the form (uploaded before the
    request, by its key), a mail's attachment, or a file sent with a reply.
    Private: read only through the API, never a public link."""

    __tablename__ = "support_attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # The opaque key the form's upload answered with; how a request names it.
    upload_key = Column(String(64), nullable=True, unique=True)
    case_id = Column(Integer, ForeignKey("support_cases.id", ondelete="CASCADE"), nullable=True)
    message_id = Column(Integer, ForeignKey("support_messages.id", ondelete="CASCADE"), nullable=True)
    storage_key = Column(String(300), nullable=False)
    filename = Column(String(255), nullable=True)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0, server_default="0")
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.Index("ix_support_attachments_case", "case_id"),
        sa.Index("ix_support_attachments_message", "message_id"),
        # The sweep's: uploads never attached to a request.
        sa.Index("ix_support_attachments_loose", "created_at", postgresql_where=sa.text("case_id IS NULL")),
    )
