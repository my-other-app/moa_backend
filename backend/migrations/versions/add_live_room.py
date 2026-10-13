"""The live room: quiz, Q&A, polls, feedback survey, pushed updates; the event's Q&A moderation switch.

Revision ID: add_live_room
Revises: add_event_store
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_live_room"
down_revision = "add_event_store"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
QUIZ_STATUS = postgresql.ENUM("draft", "live", "ended", name="quizstatus", create_type=False)
QUESTION_STATUS = postgresql.ENUM("pending", "approved", "answered", "rejected", name="questionstatus", create_type=False)
POLL_KIND = postgresql.ENUM("choice", "words", "rating", name="pollkind", create_type=False)
POLL_STATUS = postgresql.ENUM("draft", "live", "closed", name="pollstatus", create_type=False)
UPDATE_KIND = postgresql.ENUM("schedule", "room", "general", name="updatekind", create_type=False)
AUDIENCE = postgresql.ENUM("checked_in", "registered", name="audience", create_type=False)
ENUMS = (QUIZ_STATUS, QUESTION_STATUS, POLL_KIND, POLL_STATUS, UPDATE_KIND, AUDIENCE)


def _id():
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for e in ENUMS:
        e.create(bind, checkfirst=True)

    op.add_column("events", sa.Column("qa_approve_first", sa.Boolean(), nullable=False, server_default=sa.true()))

    op.create_table(
        "live_quizzes",
        _id(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("title", sa.String(160), nullable=False, server_default="Quiz"),
        sa.Column("intro", sa.String(300), nullable=True),
        sa.Column("status", QUIZ_STATUS, nullable=False, server_default="draft"),
        sa.Column("join_code", sa.String(8), nullable=True, unique=True),
        sa.Column("current_index", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("question_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_table(
        "live_quiz_questions",
        _id(),
        sa.Column("quiz_id", UUID, sa.ForeignKey("live_quizzes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("prompt", sa.String(300), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("correct_key", sa.String(2), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_index("ix_live_quiz_questions_quiz_id", "live_quiz_questions", ["quiz_id"])
    op.create_table(
        "live_quiz_players",
        _id(),
        sa.Column("quiz_id", UUID, sa.ForeignKey("live_quizzes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("player_key", sa.String(64), nullable=False),
        sa.Column("registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("quiz_id", "player_key", name="uq_live_quiz_players_key"),
        *_stamps(),
    )
    op.create_index("ix_live_quiz_players_quiz_id", "live_quiz_players", ["quiz_id"])
    op.create_table(
        "live_quiz_answers",
        _id(),
        sa.Column("question_id", UUID, sa.ForeignKey("live_quiz_questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("player_id", UUID, sa.ForeignKey("live_quiz_players.id", ondelete="CASCADE"), nullable=False),
        sa.Column("option_key", sa.String(2), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("question_id", "player_id", name="uq_live_quiz_answers_once"),
        *_stamps(),
    )
    op.create_index("ix_live_quiz_answers_question_id", "live_quiz_answers", ["question_id"])
    op.create_index("ix_live_quiz_answers_player_id", "live_quiz_answers", ["player_id"])

    op.create_table(
        "live_questions",
        _id(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("text", sa.String(500), nullable=False),
        sa.Column("author_name", sa.String(60), nullable=True),
        sa.Column("author_key", sa.String(64), nullable=False),
        sa.Column("registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", QUESTION_STATUS, nullable=False, server_default="pending"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("votes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_live_questions_event_id", "live_questions", ["event_id"])
    op.create_table(
        "live_question_votes",
        _id(),
        sa.Column("question_id", UUID, sa.ForeignKey("live_questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("voter_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("question_id", "voter_key", name="uq_live_question_votes_once"),
    )
    op.create_index("ix_live_question_votes_question_id", "live_question_votes", ["question_id"])

    op.create_table(
        "live_polls",
        _id(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("kind", POLL_KIND, nullable=False, server_default="choice"),
        sa.Column("prompt", sa.String(300), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("status", POLL_STATUS, nullable=False, server_default="draft"),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_index("ix_live_polls_event_id", "live_polls", ["event_id"])
    op.create_table(
        "live_poll_responses",
        _id(),
        sa.Column("poll_id", UUID, sa.ForeignKey("live_polls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("voter_key", sa.String(64), nullable=False),
        sa.Column("value", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("poll_id", "voter_key", name="uq_live_poll_responses_once"),
    )
    op.create_index("ix_live_poll_responses_poll_id", "live_poll_responses", ["poll_id"])

    op.create_table(
        "feedback_surveys",
        _id(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("auto_send", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("delay_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        *_stamps(),
    )
    op.create_table(
        "feedback_responses",
        _id(),
        sa.Column("survey_id", UUID, sa.ForeignKey("feedback_surveys.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("survey_id", "registration_id", name="uq_feedback_responses_once"),
    )
    op.create_index("ix_feedback_responses_survey_id", "feedback_responses", ["survey_id"])

    op.create_table(
        "live_updates",
        _id(),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("kind", UPDATE_KIND, nullable=False, server_default="general"),
        sa.Column("text", sa.String(500), nullable=False),
        sa.Column("audience", AUDIENCE, nullable=False, server_default="checked_in"),
        sa.Column("recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pushed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_live_updates_event_id", "live_updates", ["event_id"])


def downgrade() -> None:
    for t in ("live_updates", "feedback_responses", "feedback_surveys", "live_poll_responses", "live_polls", "live_question_votes", "live_questions", "live_quiz_answers", "live_quiz_players", "live_quiz_questions", "live_quizzes"):
        op.drop_table(t)
    op.drop_column("events", "qa_approve_first")
    bind = op.get_bind()
    for e in ENUMS:
        e.drop(bind, checkfirst=True)
