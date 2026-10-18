"""Email integrity: sends claimed once, resumed after a restart, matched by
their provider id through an index, and a platform-wide suppression list
(app/api/deliveries/pipeline.py, app/core/mail/suppression.py,
app/core/after_commit.py).

- deliverystatus gains `sending` (a worker has claimed the row) and `dead`
  (given up on after MAIL_MAX_ATTEMPTS tries: the dead-letter queue).
- deliveries: tries, queued_at, claimed_at, next_attempt_at.
- message_deliveries: attempts, claimed_at, next_attempt_at.
- delivery_attempts: every mail a provider took, by its id, with the address
  it went to -- a late bounce of a send since repeated still finds it.
- email_suppressions (email = lower(email), primary key): hard bounces and
  complaints, checked by every event mail.
- after_commit_jobs: the outbox for the after-commit mails that must go.
- Indexes, built CONCURRENTLY so writes go on meanwhile:
  ix_deliveries_provider_id and ix_message_deliveries_provider_id (WHERE
  provider_id IS NOT NULL) -- every SES or Resend notification used to read
  both tables from end to end, two to four times; ix_deliveries_unfinished,
  ix_message_deliveries_unfinished (status pending or sending) and
  ix_messages_sending -- what the workers claim from and the minute resumer
  reads, without walking every send ever made. Each is dropped first if a
  failed run left it invalid.

Lock-light, in three steps: the enum values in autocommit (a new value
cannot be used inside the transaction that adds it, and the partial indexes
name `sending`); then the columns and the three new tables in one short
transaction (columns that are nullable, or have a constant default, only
touch the catalog); then the indexes on the existing tables, each
CONCURRENTLY in autocommit. The new tables are empty, so their own indexes
are made with them.

Downgrade drops the indexes (concurrently), the tables and the columns, and
moves rows in the new statuses to the nearest old ones (sending -> pending,
dead -> failed). Postgres cannot drop an enum value; `sending` and `dead`
stay in the type, unused, and the upgrade adds them only if missing.

Revision ID: email_integrity
Revises: attendance_integrity
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "email_integrity"
down_revision = "attendance_integrity"
branch_labels = None
depends_on = None

OUTCOME = postgresql.ENUM("delivered", "read", "undelivered", "bounced", "complained", name="deliveryoutcome", create_type=False)

INDEXES = (
    ("ix_deliveries_provider_id", "deliveries (provider_id) WHERE provider_id IS NOT NULL"),
    ("ix_message_deliveries_provider_id", "message_deliveries (provider_id) WHERE provider_id IS NOT NULL"),
    ("ix_deliveries_unfinished", "deliveries (event_id, kind) WHERE status IN ('pending', 'sending')"),
    ("ix_message_deliveries_unfinished", "message_deliveries (message_id) WHERE status IN ('pending', 'sending')"),
    ("ix_messages_sending", "messages (created_at) WHERE status = 'SENDING'"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE deliverystatus ADD VALUE IF NOT EXISTS 'sending'")
        op.execute("ALTER TYPE deliverystatus ADD VALUE IF NOT EXISTS 'dead'")

    op.add_column("deliveries", sa.Column("tries", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("deliveries", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("message_deliveries", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("message_deliveries", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("message_deliveries", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "delivery_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider", sa.String(20), nullable=True),
        sa.Column("provider_id", sa.String(200), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=True),
        sa.Column("message_delivery_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("message_deliveries.id", ondelete="CASCADE"), nullable=True),
        sa.Column("address", sa.String(320), nullable=True),
        sa.Column("outcome", OUTCOME, nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(400), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("provider_id", name="uq_delivery_attempts_provider_id"),
    )
    op.create_index("ix_delivery_attempts_delivery_id", "delivery_attempts", ["delivery_id"])
    op.create_index("ix_delivery_attempts_message_delivery_id", "delivery_attempts", ["message_delivery_id"])

    op.create_table(
        "email_suppressions",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("email = lower(email)", name="ck_email_suppressions_lower"),
    )
    # The bounces and complaints already on record, so the list starts with
    # what the registrations know.
    op.execute(
        """
        INSERT INTO email_suppressions (email, reason, detail, created_at)
        SELECT DISTINCT ON (lower(trim(email))) lower(trim(email)), 'bounce', left(email_bounce_reason, 300), email_bounced_at
        FROM event_registrations_link
        WHERE email_bounced_at IS NOT NULL AND email IS NOT NULL AND trim(email) <> '' AND position('@' in email) > 0
        ORDER BY lower(trim(email)), email_bounced_at
        ON CONFLICT (email) DO NOTHING
        """
    )

    op.create_table(
        "after_commit_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(400), nullable=True),
        sa.Column("dead", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_after_commit_jobs_due", "after_commit_jobs", ["next_attempt_at"], postgresql_where=sa.text("NOT dead"))

    with op.get_context().autocommit_block():
        for name, definition in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    for table in ("deliveries", "message_deliveries"):
        op.execute(f"UPDATE {table} SET status = 'pending' WHERE status = 'sending'")
        op.execute(f"UPDATE {table} SET status = 'failed' WHERE status = 'dead'")
    op.drop_index("ix_after_commit_jobs_due", table_name="after_commit_jobs")
    op.drop_table("after_commit_jobs")
    op.drop_table("email_suppressions")
    op.drop_index("ix_delivery_attempts_message_delivery_id", table_name="delivery_attempts")
    op.drop_index("ix_delivery_attempts_delivery_id", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
    for column in ("next_attempt_at", "claimed_at", "attempts"):
        op.drop_column("message_deliveries", column)
    for column in ("next_attempt_at", "claimed_at", "queued_at", "tries"):
        op.drop_column("deliveries", column)
