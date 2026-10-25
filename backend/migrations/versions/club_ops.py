"""Club tools (app/api/club_ops): results, cancelling an event, club refunds,
a club login's forgotten password.

- eventstatus gains 'cancelled' (ADD VALUE: a catalog change, no rewrite).
- competition_placings: a team's 1st, 2nd or 3rd within its track.
- club_refunds: a club's refunds, whole or in part, and those of a
  cancellation (one per payment: a partial unique index).
- event_cancellations: one per cancelled event.
- guest_notices: each guest told once per channel and thing told.
- club_password_reset_codes: the dashboard's reset codes (keyed hashes only).

Every table is new. The foreign keys take a brief SHARE ROW EXCLUSIVE on
events, users, clubs, club_members, competitions, event_registrations_link,
payment_logs and messages, under the lock_timeout set first (env.py retries
when it is not granted).

Downgrade drops the tables and a cancelled event goes back to closed; the
type keeps its unused value (see downgrade).

Revision ID: club_ops
Revises: apple_refresh_token
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "club_ops"
down_revision = "apple_refresh_token"
branch_labels = None
depends_on = None


def _stamps() -> list:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TYPE eventstatus ADD VALUE IF NOT EXISTS 'cancelled'")

    op.create_table(
        "competition_placings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("competition_id", sa.Integer(), sa.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("competition_teams.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("competition_tracks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("set_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.CheckConstraint("position BETWEEN 1 AND 3", name="ck_competition_placings_position"),
    )
    op.create_index("ix_competition_placings_competition_id", "competition_placings", ["competition_id"])

    op.create_table(
        "club_refunds",
        _uuid_pk(),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=True),
        sa.Column("payment_log_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payment_logs.id"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="club"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("razorpay_refund_id", sa.String(64), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("client_ref", postgresql.UUID(as_uuid=True), nullable=True, unique=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("impersonated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_by_name", sa.String(120), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.CheckConstraint("amount > 0", name="ck_club_refunds_amount"),
    )
    op.create_index("ix_club_refunds_club_id", "club_refunds", ["club_id"])
    op.create_index("ix_club_refunds_event_id", "club_refunds", ["event_id"])
    op.create_index("ix_club_refunds_payment_log_id", "club_refunds", ["payment_log_id"])
    op.create_index(
        "uq_club_refunds_cancellation_payment", "club_refunds", ["payment_log_id"], unique=True,
        postgresql_where=sa.text("source = 'cancellation'"),
    )
    op.create_index("ix_club_refunds_open", "club_refunds", ["event_id"], postgresql_where=sa.text("status <> 'succeeded'"))

    op.create_table(
        "event_cancellations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False, unique=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("previous_status", sa.String(16), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cancelled_by_name", sa.String(120), nullable=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("impersonated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("registrations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_sent", sa.Integer(), nullable=True),
        *_stamps(),
    )
    op.create_index("ix_event_cancellations_club_id", "event_cancellations", ["club_id"])

    op.create_table(
        "guest_notices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ref", sa.String(80), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="sending"),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("provider_id", sa.String(200), nullable=True),
        *_stamps(),
        sa.UniqueConstraint("ref", "registration_id", "channel", name="uq_guest_notices_ref_registration_channel"),
    )
    op.create_index("ix_guest_notices_event_id", "guest_notices", ["event_id"])

    op.create_table(
        "club_password_reset_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email_key", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_club_password_reset_codes_key_sent", "club_password_reset_codes", ["email_key", "sent_at"])
    op.create_index("ix_club_password_reset_codes_sent", "club_password_reset_codes", ["sent_at"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("club_password_reset_codes")
    op.drop_table("guest_notices")
    op.drop_table("event_cancellations")
    op.drop_table("club_refunds")
    op.drop_table("competition_placings")
    # Postgres cannot drop one value of a type, and remaking eventstatus would
    # rebuild every partial index on events that names it: the value stays,
    # unused, and an upgrade's ADD VALUE IF NOT EXISTS finds it there.
    op.execute("UPDATE events SET status = 'closed' WHERE status = 'cancelled'")
