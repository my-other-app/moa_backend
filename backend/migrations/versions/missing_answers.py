"""Missing answers (app/api/missing_answers, docs/MISSING_ANSWERS.md).

Four new tables, nothing changed in an existing one:

- event_form_revisions: every version of an event's sign-up questions
  (1, 2, ... per event, uq_event_form_revisions_version), with the impact of
  each change on the people already signed up.
- answer_asks: a club asking people for the answers they are missing.
- answer_ask_people: who an ask is to, and when each opened their link and
  answered (ix_answer_ask_people_person finds a person's asks).
- answer_ask_sends: each message, by channel and round, claimed before it is
  sent (uq_answer_ask_sends_once).

Lock-safe: the tables are new and empty, so their indexes are built on
nothing. The only lock on a busy table is the brief SHARE ROW EXCLUSIVE a
foreign key takes on events, users and club_members, under lock_timeout 3s,
so a busy table fails the migration fast rather than queue requests behind it.

Downgrade drops the four tables: every form version and ask goes with them.
The answers people gave live on the registrations and requests, untouched.

Revision ID: missing_answers
Revises: certificate_public_ids
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "missing_answers"
down_revision = "certificate_public_ids"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
UUID_PK = dict(primary_key=True, server_default=sa.text("gen_random_uuid()"))


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "event_form_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=True),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("changed_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        if_not_exists=True,
    )
    op.create_index(
        "uq_event_form_revisions_version", "event_form_revisions", ["event_id", "version"], unique=True, if_not_exists=True,
    )

    op.create_table(
        "answer_asks",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column("channels", postgresql.JSONB(), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remind_after_hours", sa.Integer(), nullable=True),
        sa.Column("include_requests", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("refs", postgresql.JSONB(), nullable=True),
        sa.Column("client_ref", sa.String(64), nullable=True),
        sa.Column("status", sa.String(8), nullable=False, server_default="open"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(16), nullable=True),
        sa.Column("reminders_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_round_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'closed')", name="ck_answer_asks_status"),
        if_not_exists=True,
    )
    op.create_index("ix_answer_asks_event", "answer_asks", ["event_id", "created_at"], if_not_exists=True)
    op.create_index(
        "uq_answer_asks_client_ref", "answer_asks", ["event_id", "client_ref"], unique=True,
        postgresql_where=sa.text("client_ref IS NOT NULL"), if_not_exists=True,
    )

    op.create_table(
        "answer_ask_people",
        sa.Column("ask_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("answer_asks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ref_kind", sa.String(12), primary_key=True),
        sa.Column("ref_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("ref_kind IN ('registration', 'request')", name="ck_answer_ask_people_kind"),
        if_not_exists=True,
    )
    op.create_index("ix_answer_ask_people_person", "answer_ask_people", ["event_id", "ref_kind", "ref_id"], if_not_exists=True)

    op.create_table(
        "answer_ask_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("ask_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("answer_asks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ref_kind", sa.String(12), nullable=False),
        sa.Column("ref_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(8), nullable=False, server_default="queued"),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("status IN ('queued', 'sent', 'failed', 'skipped')", name="ck_answer_ask_sends_status"),
        sa.CheckConstraint("channel IN ('push', 'email', 'whatsapp')", name="ck_answer_ask_sends_channel"),
        if_not_exists=True,
    )
    op.create_index(
        "uq_answer_ask_sends_once", "answer_ask_sends", ["ask_id", "ref_kind", "ref_id", "channel", "round"], unique=True,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP TABLE IF EXISTS answer_ask_sends")
    op.execute("DROP TABLE IF EXISTS answer_ask_people")
    op.execute("DROP TABLE IF EXISTS answer_asks")
    op.execute("DROP TABLE IF EXISTS event_form_revisions")
