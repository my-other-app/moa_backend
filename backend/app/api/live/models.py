"""The room while the event runs: a quiz the host drives, questions the floor
asks and votes up, polls and word clouds, the feedback survey afterwards, and
the updates pushed to phones. Everything hangs off the event; the audience
is identified by a key its browser keeps, never by an account."""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import DDL, JSON, Boolean, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel
from app.db.mixins import TimestampsMixin


def _uuid_pk():
    return Column(UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), default=uuid.uuid4)


# --- quiz ----------------------------------------------------------------------


class QuizStatus(enum.Enum):
    draft = "draft"
    live = "live"
    ended = "ended"


class LiveQuizzes(AbstractSQLModel, TimestampsMixin):
    """One quiz per event. Players join with the code on the host screen;
    the host moves it question by question."""

    __tablename__ = "live_quizzes"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, unique=True, index=True)
    title = Column(String(160), nullable=False, default="Quiz")
    intro = Column(String(300), nullable=True)
    status = Column(Enum(QuizStatus), nullable=False, default=QuizStatus.draft)
    join_code = Column(String(8), nullable=True, unique=True)
    # -1 in the lobby; the index of the open question once it is running.
    current_index = Column(Integer, nullable=False, default=-1)
    question_opened_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    ended_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    questions = relationship("LiveQuizQuestions", order_by="LiveQuizQuestions.sort_order", cascade="all, delete-orphan", lazy="selectin")


class LiveQuizQuestions(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "live_quiz_questions"

    id = _uuid_pk()
    quiz_id = Column(UUID(as_uuid=True), ForeignKey("live_quizzes.id", ondelete="CASCADE"), nullable=False, index=True)
    prompt = Column(String(300), nullable=False)
    # [{"key": "a", "label": "512 KB"}, ...] — two to six of them.
    options = Column(JSON, nullable=False, default=list)
    correct_key = Column(String(2), nullable=False)
    seconds = Column(Integer, nullable=False, default=20)
    sort_order = Column(Integer, nullable=False, default=0)


class LiveQuizPlayers(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "live_quiz_players"
    __table_args__ = (
        UniqueConstraint("quiz_id", "player_key", name="uq_live_quiz_players_key"),
        # The board: a quiz's players, best first (the room's quiz snapshot reads it in this order).
        sa.Index("ix_live_quiz_players_board", "quiz_id", sa.text("score DESC"), "created_at"),
    )

    id = _uuid_pk()
    quiz_id = Column(UUID(as_uuid=True), ForeignKey("live_quizzes.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(60), nullable=False)
    # The secret the phone keeps; it is the player's whole identity.
    player_key = Column(String(64), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    score = Column(Integer, nullable=False, default=0)


class LiveQuizAnswers(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "live_quiz_answers"
    __table_args__ = (UniqueConstraint("question_id", "player_id", name="uq_live_quiz_answers_once"),)

    id = _uuid_pk()
    question_id = Column(UUID(as_uuid=True), ForeignKey("live_quiz_questions.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(UUID(as_uuid=True), ForeignKey("live_quiz_players.id", ondelete="CASCADE"), nullable=False, index=True)
    option_key = Column(String(2), nullable=False)
    correct = Column(Boolean, nullable=False, default=False)
    points = Column(Integer, nullable=False, default=0)
    answered_at = Column(TZAwareDateTime(timezone=True), nullable=False)


# --- Q&A ----------------------------------------------------------------------


class QuestionStatus(enum.Enum):
    pending = "pending"
    approved = "approved"
    answered = "answered"
    rejected = "rejected"


class LiveQuestions(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "live_questions"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    text = Column(String(500), nullable=False)
    author_name = Column(String(60), nullable=True)
    author_key = Column(String(64), nullable=False)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True)
    status = Column(Enum(QuestionStatus), nullable=False, default=QuestionStatus.pending)
    pinned = Column(Boolean, nullable=False, default=False)
    votes = Column(Integer, nullable=False, default=0)
    answered_at = Column(TZAwareDateTime(timezone=True), nullable=True)


class LiveQuestionVotes(AbstractSQLModel):
    __tablename__ = "live_question_votes"
    __table_args__ = (
        UniqueConstraint("question_id", "voter_key", name="uq_live_question_votes_once"),
        # What one browser voted for: read once per browser, not once per question per poll.
        sa.Index("ix_live_question_votes_voter", "voter_key", "question_id"),
    )

    id = _uuid_pk()
    question_id = Column(UUID(as_uuid=True), ForeignKey("live_questions.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_key = Column(String(64), nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), server_default=sa.func.now(), nullable=False)


# --- polls ----------------------------------------------------------------------


class PollKind(enum.Enum):
    choice = "choice"
    words = "words"
    rating = "rating"


class PollStatus(enum.Enum):
    draft = "draft"
    live = "live"
    closed = "closed"


class LivePolls(AbstractSQLModel, TimestampsMixin):
    __tablename__ = "live_polls"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    kind = Column(Enum(PollKind), nullable=False, default=PollKind.choice)
    prompt = Column(String(300), nullable=False)
    # The choices of a multiple-choice poll; empty for words and ratings.
    options = Column(JSON, nullable=False, default=list)
    status = Column(Enum(PollStatus), nullable=False, default=PollStatus.draft)
    launched_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    closed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)


class LivePollResponses(AbstractSQLModel):
    __tablename__ = "live_poll_responses"
    __table_args__ = (
        UniqueConstraint("poll_id", "voter_key", name="uq_live_poll_responses_once"),
        # A poll's results by GROUP BY value, read from the index alone.
        sa.Index("ix_live_poll_responses_counts", "poll_id", "value", "created_at"),
        # What one browser answered.
        sa.Index("ix_live_poll_responses_voter", "voter_key", "poll_id"),
    )

    id = _uuid_pk()
    poll_id = Column(UUID(as_uuid=True), ForeignKey("live_polls.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_key = Column(String(64), nullable=False)
    value = Column(String(120), nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), server_default=sa.func.now(), nullable=False)


# --- feedback ----------------------------------------------------------------------


class FeedbackSurveys(AbstractSQLModel, TimestampsMixin):
    """The event's survey: its questions and when it goes out."""

    __tablename__ = "feedback_surveys"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, unique=True, index=True)
    auto_send = Column(Boolean, nullable=False, default=True)
    delay_minutes = Column(Integer, nullable=False, default=60)
    # [{"key": "overall", "kind": "rating" | "nps" | "text", "label": "..."}]
    questions = Column(JSON, nullable=False, default=list)
    sent_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    sent_count = Column(Integer, nullable=False, default=0)
    # When it goes out by itself: the event's end plus the delay. Written by
    # the database (FEEDBACK_DUE_SQL below) on every insert, every change of
    # the delay and every change of the event's start or length, so the
    # minute sweep reads only the surveys whose time has come
    # (ix_feedback_surveys_due) instead of every survey never sent.
    due_at = Column(TZAwareDateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.Index("ix_feedback_surveys_due", "due_at", postgresql_where=sa.text("auto_send AND sent_at IS NULL AND due_at IS NOT NULL")),
    )


class FeedbackSends(AbstractSQLModel):
    """Who the survey went to: one row per guest, claimed before the mail goes,
    so sending again (or the sweep and a send at once) reaches only those not
    sent yet. The mail itself is a job per guest (service.SURVEY_MAIL), which
    sets `mailed_at` once the provider took it: a job run again finds it set
    and mails nobody twice; a guest claimed whose mail never went has none."""

    __tablename__ = "feedback_sends"

    survey_id = Column(UUID(as_uuid=True), ForeignKey("feedback_surveys.id", ondelete="CASCADE"), primary_key=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), primary_key=True)
    # Claimed: the guest is to be sent it.
    sent_at = Column(TZAwareDateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    # Mailed: the provider took it.
    mailed_at = Column(TZAwareDateTime(timezone=True), nullable=True)
    # Not mailed, and why: past a Free club's monthly emails (app/api/message_quota).
    skipped_reason = Column(String(300), nullable=True)


class FeedbackResponses(AbstractSQLModel):
    __tablename__ = "feedback_responses"
    __table_args__ = (UniqueConstraint("survey_id", "registration_id", name="uq_feedback_responses_once"),)

    id = _uuid_pk()
    survey_id = Column(UUID(as_uuid=True), ForeignKey("feedback_surveys.id", ondelete="CASCADE"), nullable=False, index=True)
    registration_id = Column(UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False)
    answers = Column(JSON, nullable=False, default=dict)
    submitted_at = Column(TZAwareDateTime(timezone=True), server_default=sa.func.now(), nullable=False)


# --- live updates ----------------------------------------------------------------------


class UpdateKind(enum.Enum):
    schedule = "schedule"
    room = "room"
    general = "general"


class Audience(enum.Enum):
    checked_in = "checked_in"
    registered = "registered"


class LiveUpdates(AbstractSQLModel):
    """A line pushed to phones and shown as a banner on the event page."""

    __tablename__ = "live_updates"

    id = _uuid_pk()
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False, index=True)
    kind = Column(Enum(UpdateKind), nullable=False, default=UpdateKind.general)
    text = Column(String(500), nullable=False)
    audience = Column(Enum(Audience), nullable=False, default=Audience.checked_in)
    recipients = Column(Integer, nullable=False, default=0)
    pushed = Column(Integer, nullable=False, default=0)
    sent_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    sent_at = Column(TZAwareDateTime(timezone=True), server_default=sa.func.now(), nullable=False)


# ─── feedback_surveys.due_at, kept by the database ───────────────────────
#
# The event's end (start plus `duration` hours, as events.ends_at_sql) plus
# the survey's delay. On the survey, a BEFORE trigger works it out on insert
# and when the delay, the event or due_at itself changes; on events, a change
# of start or length clears the survey's due_at so its own trigger works it
# out again. The migration discover_integrity installs the same and fills it.

FEEDBACK_DUE_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_feedback_due() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      SELECT timezone('UTC', timezone('UTC', e.event_datetime) + coalesce(e.duration, 0) * interval '1 hour')
             + make_interval(mins => coalesce(NEW.delay_minutes, 0))
      INTO NEW.due_at FROM events e WHERE e.id = NEW.event_id;
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_feedback_due_from_event() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE feedback_surveys SET due_at = NULL WHERE event_id = NEW.id;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_feedback_surveys_due BEFORE INSERT OR UPDATE OF delay_minutes, event_id, due_at ON feedback_surveys
    FOR EACH ROW EXECUTE FUNCTION moa_feedback_due()
    """,
    """
    CREATE TRIGGER trg_events_feedback_due AFTER UPDATE OF event_datetime, duration ON events
    FOR EACH ROW WHEN (OLD.event_datetime IS DISTINCT FROM NEW.event_datetime OR OLD.duration IS DISTINCT FROM NEW.duration)
    EXECUTE FUNCTION moa_feedback_due_from_event()
    """,
)

# feedback_surveys is made after events (its event_id refers to them).
for _sql in FEEDBACK_DUE_SQL:
    sa_event.listen(FeedbackSurveys.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql"))
