"""The money ledger: ledger_entries (append-only, double entry, paise),
ledger_monthly and ledger_platform_monthly (its sums), and the trigger that
refuses UPDATE, DELETE and TRUNCATE on the entries (app/api/ledger/models.py).

Three new tables, empty: nothing here touches a table the app is using, so
no lock on a hot table is taken or waited for (lock_timeout is set all the
same, in case). Their indexes are made with them: an empty table's index is
instant, and nothing writes to these tables until this commits.

The history is not posted here: scripts/ledger_backfill.py does that, in
batches that commit, after the deploy (a migration that walked every payment
would hold the deploy for as long as it took).

Downgrade drops the three tables and the trigger function. What the ledger
held goes with them; the rows it was posted from are untouched.

Revision ID: w2_ledger
Revises: topics
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "w2_ledger"
down_revision = "topics"
branch_labels = None
depends_on = None

# The same SQL as app/api/ledger/models.APPEND_ONLY_SQL, written out: what
# this migration did must not change when the app's code does.
APPEND_ONLY_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_ledger_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'ledger_entries is append-only: % refused (post a correcting entry instead)', TG_OP
        USING ERRCODE = 'restrict_violation';
    END $$
    """,
    """
    CREATE TRIGGER trg_ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION moa_ledger_append_only()
    """,
    """
    CREATE TRIGGER trg_ledger_entries_no_truncate
    BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION moa_ledger_append_only()
    """,
)


def _sums(*key):
    return (
        *key,
        sa.Column("amount_paise", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("entries", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("txn_key", sa.String(200), nullable=False),
        sa.Column("line", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("account", sa.String(24), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="INR"),
        sa.Column("club_id", sa.Integer(), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("gateway_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(80), nullable=False),
        sa.Column("razorpay_payment_id", sa.String(64), nullable=True),
        sa.Column("razorpay_order_id", sa.String(64), nullable=True),
        sa.Column("razorpay_refund_id", sa.String(64), nullable=True),
        sa.Column("razorpay_subscription_id", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("note", sa.String(200), nullable=True),
        sa.UniqueConstraint("txn_key", "line", name="uq_ledger_entries_txn_line"),
    )
    op.create_index("ix_ledger_entries_registration", "ledger_entries", ["registration_id", "account"],
                    postgresql_where=sa.text("registration_id IS NOT NULL"))
    op.create_index("ix_ledger_entries_event", "ledger_entries", ["event_id", "account"],
                    postgresql_include=["kind", "amount_paise", "units", "occurred_at", "origin_at"],
                    postgresql_where=sa.text("event_id IS NOT NULL"))
    op.create_index("ix_ledger_entries_payment", "ledger_entries", ["razorpay_payment_id"],
                    postgresql_where=sa.text("razorpay_payment_id IS NOT NULL"))
    op.create_index("ix_ledger_entries_gateway_day", "ledger_entries", ["occurred_at"],
                    postgresql_where=sa.text("account = 'gateway'"))
    op.create_index("ix_ledger_entries_club", "ledger_entries", ["club_id", "id"])
    op.create_index("ix_ledger_entries_subscribers", "ledger_entries", ["source_id"],
                    postgresql_where=sa.text("account = 'subscribers'"))
    for sql in APPEND_ONLY_SQL:
        op.execute(sql)

    op.create_table(
        "ledger_monthly",
        *_sums(
            sa.Column("club_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("gateway_id", sa.Integer(), nullable=False),
            sa.Column("month", sa.Date(), nullable=False),
            sa.Column("origin_month", sa.Date(), nullable=False),
            sa.Column("account", sa.String(24), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
        ),
        sa.PrimaryKeyConstraint("club_id", "event_id", "gateway_id", "month", "origin_month", "account", "kind",
                                name="ledger_monthly_pkey"),
    )
    op.create_index("ix_ledger_monthly_event", "ledger_monthly", ["event_id"], postgresql_where=sa.text("event_id <> 0"))
    op.create_index("ix_ledger_monthly_month", "ledger_monthly", ["month"])

    op.create_table(
        "ledger_platform_monthly",
        *_sums(
            sa.Column("month", sa.Date(), nullable=False),
            sa.Column("origin_month", sa.Date(), nullable=False),
            sa.Column("club_gateway", sa.Boolean(), nullable=False),
            sa.Column("account", sa.String(24), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("shard", sa.SmallInteger(), nullable=False),
        ),
        sa.PrimaryKeyConstraint("month", "origin_month", "club_gateway", "account", "kind", "shard",
                                name="ledger_platform_monthly_pkey"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("ledger_platform_monthly")
    op.drop_table("ledger_monthly")
    op.drop_table("ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS moa_ledger_append_only()")
