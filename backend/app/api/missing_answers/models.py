"""Missing answers (feature-register/missing-answers-contract.md, docs/MISSING_ANSWERS.md).

- `event_form_revisions`: every version of an event's sign-up questions, one
  row a change, with what the change did to the people already signed up
  (the impact summary of §1.3, against the version before).
- `answer_asks`: a club asking the people who are missing answers for them.
- `answer_ask_people`: who one ask is to, and when each opened their link
  and when nothing it asked was left for them.
- `answer_ask_sends`: what went to each of them, by channel and round (0 is
  the first send, 1 and 2 the reminders).

A person is a registration (`ref_kind` "registration") or an open
registration request ("request"), named by its id; the opaque `ref` the
clients see is the registration's id, or "q" and the request's id.
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel

REGISTRATION, REQUEST = "registration", "request"
REF_KINDS = (REGISTRATION, REQUEST)

OPEN, CLOSED = "open", "closed"
#: Why an ask closed: the club closed it, its deadline passed, nobody was
#: left to answer, or the event ended.
BY_CLUB, DEADLINE, DONE, EVENT_ENDED = "closed_by_club", "deadline", "done", "event_ended"
CLOSED_REASONS = (BY_CLUB, DEADLINE, DONE, EVENT_ENDED)

PUSH, EMAIL, WHATSAPP = "push", "email", "whatsapp"
CHANNELS = (PUSH, EMAIL, WHATSAPP)

QUEUED, SENT, FAILED, SKIPPED = "queued", "sent", "failed", "skipped"
SEND_STATUSES = (QUEUED, SENT, FAILED, SKIPPED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)


class EventFormRevisions(AbstractSQLModel):
    """One version of an event's questions, as stored (questions.stored)."""

    __tablename__ = "event_form_revisions"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    #: 1, 2, ... per event. The first change after this shipped writes 1 (the
    #: form as it was, no summary) and 2 (the new one) together.
    version = Column(Integer, nullable=False)
    questions = Column(JSONB, nullable=False)
    #: The impact of this version against the one before (§1.3); null for a first.
    summary = Column(JSONB, nullable=True)
    #: Who saved it (null for a version 1 nobody saved: the form as it was).
    changed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.Index("uq_event_form_revisions_version", "event_id", "version", unique=True),
    )


class AnswerAsks(AbstractSQLModel):
    """The club asking for answers: which questions, by which channels, in
    what words, until when."""

    __tablename__ = "answer_asks"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_member_id = Column(Integer, ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True)
    #: As the dashboard names them (a team member's own name).
    created_by_name = Column(String(120), nullable=True)
    #: [{"key", "label"}], the labels as they were when asked.
    questions = Column(JSONB, nullable=False)
    channels = Column(JSONB, nullable=False)
    message = Column(String(300), nullable=False)
    deadline = Column(TZAwareDateTime(timezone=True), nullable=True)
    remind_after_hours = Column(Integer, nullable=True)
    include_requests = Column(Boolean, nullable=False, default=True, server_default=sa.true())
    #: The refs it was limited to ("Ask again" for one person); null: everyone askable.
    refs = Column(JSONB, nullable=True)
    #: The dashboard's own id for the click, so a double click makes one ask.
    client_ref = Column(String(64), nullable=True)
    status = Column(String(8), nullable=False, default=OPEN, server_default=OPEN)
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    closed_reason = Column(String(16), nullable=True)
    #: Reminders sent (the automatic one included), at most 2.
    reminders_sent = Column(Integer, nullable=False, default=0, server_default="0")
    #: When the last round (the first send or a reminder) went.
    last_round_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: When the automatic reminder is due; null when there is none to come.
    next_reminder_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        # An event's asks, newest first (the dashboard's list, the open ones for a person).
        sa.Index("ix_answer_asks_event", "event_id", "created_at"),
        sa.Index("uq_answer_asks_client_ref", "event_id", "client_ref", unique=True, postgresql_where=sa.text("client_ref IS NOT NULL")),
        CheckConstraint("status IN ('open', 'closed')", name="ck_answer_asks_status"),
    )


class AnswerAskPeople(AbstractSQLModel):
    """One person one ask is to."""

    __tablename__ = "answer_ask_people"

    ask_id = Column(UUID(as_uuid=True), ForeignKey("answer_asks.id", ondelete="CASCADE"), primary_key=True)
    ref_kind = Column(String(12), primary_key=True)
    ref_id = Column(UUID(as_uuid=True), primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    added_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))
    #: They opened their link (the first time).
    opened_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    #: Nothing this ask asked was left for them (the first time).
    answered_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        # A person's asks across the event: their link, their ticket's flag.
        sa.Index("ix_answer_ask_people_person", "event_id", "ref_kind", "ref_id"),
        CheckConstraint("ref_kind IN ('registration', 'request')", name="ck_answer_ask_people_kind"),
    )


class AnswerAskSends(AbstractSQLModel):
    """One message to one person on one channel in one round: claimed
    (queued) before anything is sent, so a job run again sends nobody twice."""

    __tablename__ = "answer_ask_sends"

    id = _uuid_pk()
    ask_id = Column(UUID(as_uuid=True), ForeignKey("answer_asks.id", ondelete="CASCADE"), nullable=False)
    ref_kind = Column(String(12), nullable=False)
    ref_id = Column(UUID(as_uuid=True), nullable=False)
    channel = Column(String(10), nullable=False)
    #: 0 the first send, 1 and 2 the reminders.
    round = Column(Integer, nullable=False, default=0)
    status = Column(String(8), nullable=False, default=QUEUED, server_default=QUEUED)
    error = Column(String(300), nullable=True)
    #: The provider's id for it (an email's, a WhatsApp's wamid), when it gave one.
    provider_id = Column(String(200), nullable=True)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, default=_now, server_default=sa.text("now()"))

    __table_args__ = (
        sa.Index("uq_answer_ask_sends_once", "ask_id", "ref_kind", "ref_id", "channel", "round", unique=True),
        CheckConstraint("status IN ('queued', 'sent', 'failed', 'skipped')", name="ck_answer_ask_sends_status"),
        CheckConstraint("channel IN ('push', 'email', 'whatsapp')", name="ck_answer_ask_sends_channel"),
    )
