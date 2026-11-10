"""An event's review email, sent again (app/api/emails/review.py,
feature-register/event-review-emails-contract.md, docs/APPROVALS.md §11).

Two new tables, nothing changed in an existing one:

- review_resends: one press of "Send to N people" on an event's registration
  review email, with its words frozen and the counts of how it went;
  uq_review_resends_event_client_ref makes the same press twice one resend.
- review_resend_recipients: each person (registration request) a resend is
  for, claimed before their copy goes; uq_review_resend_recipients_once
  keeps each person to one row a resend.

An event's own copy of a review email needs no change: the rows go in
email_templates, whose uq_email_templates_event_stage already allows one per
event and stage.

Lock-safe: the tables are new and empty, so their indexes are built on
nothing. The only locks on busy tables are the brief SHARE ROW EXCLUSIVE the
foreign keys take on events, users, email_templates and registration_requests,
under lock_timeout 3s.

Downgrade drops the two tables: the record of every resend goes with them.
The emails themselves, and the event copies, are untouched.

Revision ID: review_resends
Revises: missing_answers
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "review_resends"
down_revision = "missing_answers"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
NOW = sa.text("now()")
UUID_PK = dict(primary_key=True, server_default=sa.text("gen_random_uuid()"))


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "review_resends",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("email_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("design", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default="sending"),
        sa.Column("recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("event_id", "client_ref", name="uq_review_resends_event_client_ref"),
        sa.CheckConstraint("status IN ('sending', 'done')", name="ck_review_resends_status"),
        if_not_exists=True,
    )
    op.create_index("ix_review_resends_event", "review_resends", ["event_id", "created_at"], if_not_exists=True)
    op.create_index(
        "ix_review_resends_sending", "review_resends", ["created_at"],
        postgresql_where=sa.text("status = 'sending'"), if_not_exists=True,
    )

    op.create_table(
        "review_resend_recipients",
        sa.Column("id", postgresql.UUID(as_uuid=True), **UUID_PK),
        sa.Column("resend_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("review_resends.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("registration_requests.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(400), nullable=True),
        sa.Column("provider_id", sa.String(200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("resend_id", "request_id", name="uq_review_resend_recipients_once"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'skipped', 'dead')", name="ck_review_resend_recipients_status",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_review_resend_recipients_unfinished", "review_resend_recipients", ["resend_id"],
        postgresql_where=sa.text("status IN ('pending', 'sending')"), if_not_exists=True,
    )
    op.create_index("ix_review_resend_recipients_request", "review_resend_recipients", ["request_id"], if_not_exists=True)


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("DROP TABLE IF EXISTS review_resend_recipients")
    op.execute("DROP TABLE IF EXISTS review_resends")
