"""The room while the event runs: a quiz the host drives, questions the floor
asks and votes up, polls and word clouds, the feedback survey afterwards, and
the updates pushed to phones. Everything hangs off the event; the audience
is identified by a key its browser keeps, never by an account."""
import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Column, Enum, ForeignKey, Integer, String, UniqueConstraint
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
    __table_args__ = (UniqueConstraint("quiz_id", "player_key", name="uq_live_quiz_players_key"),)

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
    __table_args__ = (UniqueConstraint("question_id", "voter_key", name="uq_live_question_votes_once"),)

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
    __table_args__ = (UniqueConstraint("poll_id", "voter_key", name="uq_live_poll_responses_once"),)

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
