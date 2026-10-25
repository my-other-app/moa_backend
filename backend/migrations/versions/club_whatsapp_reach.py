"""A club's own WhatsApp number, further: routing by kind, which number each
WhatsApp delivery went from and why, Meta's spend, consents, broadcasts, and
Meta's Embedded Signup (app/api/club_whatsapp: delivery_routes, spend,
consent, broadcasts, embedded_signup).

- club_whatsapp_accounts: a switch per kind under `routing` (tickets,
  certificates, reminders; on); Embedded Signup's token expiry, sealed
  registration PIN, registration time and WhatsApp Business app flag; the
  MM API onboarding status; the account's currency and when its spend was
  last read.
- deliveries, delivery_attempts: `via` (club | platform) and the fallback's
  code and words; an index on the attempts' fallbacks by time.
- club_whatsapp_spend: Meta's pricing analytics, one row per number and month.
- club_whatsapp_consents: "Get updates from <club> on WhatsApp", per person.
- club_whatsapp_broadcasts, club_whatsapp_broadcast_recipients.

The columns are nullable or have a constant default: catalog changes, no
rewrite, under the lock_timeout set first (deliveries and delivery_attempts
are hot; env.py retries when the lock is not granted). The attempts' index
is built CONCURRENTLY, in an autocommit block. The new tables' foreign keys
take a brief SHARE ROW EXCLUSIVE on clubs, users, club_members, events and
event_registrations_link, under the same limit.

Downgrade drops the tables, the columns and the index.

Revision ID: club_whatsapp_reach
Revises: club_whatsapp_inbox
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "club_whatsapp_reach"
down_revision = "club_whatsapp_inbox"
branch_labels = None
depends_on = None

ACCOUNT_COLUMNS = (
    ("route_tickets", sa.Boolean(), dict(nullable=False, server_default=sa.true())),
    ("route_certificates", sa.Boolean(), dict(nullable=False, server_default=sa.true())),
    ("route_reminders", sa.Boolean(), dict(nullable=False, server_default=sa.true())),
    ("token_expires_at", sa.DateTime(timezone=True), dict(nullable=True)),
    ("register_pin_enc", sa.Text(), dict(nullable=True)),
    ("registered_at", sa.DateTime(timezone=True), dict(nullable=True)),
    ("on_biz_app", sa.Boolean(), dict(nullable=False, server_default=sa.false())),
    ("mm_status", sa.String(32), dict(nullable=True)),
    ("mm_checked_at", sa.DateTime(timezone=True), dict(nullable=True)),
    ("currency", sa.String(8), dict(nullable=True)),
    ("spend_checked_at", sa.DateTime(timezone=True), dict(nullable=True)),
)
#: On deliveries and delivery_attempts alike.
VIA_COLUMNS = (("via", 8), ("fallback_code", 40), ("fallback_reason", 300))


def _now():
    return sa.text("now()")


def _set_null_index(name: str, table: str, column: str) -> None:
    """A column a deleted row elsewhere sets null (ON DELETE SET NULL): indexed
    where set, so that delete finds the rows instead of scanning the table."""
    op.create_index(name, table, [column], postgresql_where=sa.text(f"{column} IS NOT NULL"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    for name, kind, options in ACCOUNT_COLUMNS:
        op.add_column("club_whatsapp_accounts", sa.Column(name, kind, **options))
    for table in ("deliveries", "delivery_attempts"):
        for name, length in VIA_COLUMNS:
            op.add_column(table, sa.Column(name, sa.String(length), nullable=True))

    op.create_table(
        "club_whatsapp_spend",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("club_whatsapp_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("account_id", "month", name="uq_club_whatsapp_spend_month"),
    )
    op.create_index("ix_club_whatsapp_spend_club_id", "club_whatsapp_spend", ["club_id"])

    op.create_table(
        "club_whatsapp_consents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("registration_id", UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True),
        sa.Column("wa_id", sa.String(20), nullable=True),
        sa.Column("bsuid", sa.String(160), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("text_version", sa.String(32), nullable=False),
        sa.Column("consent_text", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_source", sa.String(32), nullable=True),
        sa.CheckConstraint("source IN ('registration', 'follow')", name="ck_club_whatsapp_consents_source"),
    )
    op.create_index("uq_club_whatsapp_consents_live_wa", "club_whatsapp_consents", ["club_id", "wa_id"], unique=True,
                    postgresql_where=sa.text("withdrawn_at IS NULL AND wa_id IS NOT NULL"))
    op.create_index("uq_club_whatsapp_consents_live_user", "club_whatsapp_consents", ["club_id", "user_id"], unique=True,
                    postgresql_where=sa.text("withdrawn_at IS NULL AND user_id IS NOT NULL"))
    op.create_index("ix_club_whatsapp_consents_club_live", "club_whatsapp_consents", ["club_id", "id"],
                    postgresql_where=sa.text("withdrawn_at IS NULL"))
    _set_null_index("ix_club_whatsapp_consents_user", "club_whatsapp_consents", "user_id")
    _set_null_index("ix_club_whatsapp_consents_registration", "club_whatsapp_consents", "registration_id")

    op.create_table(
        "club_whatsapp_broadcasts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("club_whatsapp_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("template_name", sa.String(512), nullable=False),
        sa.Column("template_language", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("variables", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("endpoint", sa.String(24), nullable=True),
        sa.Column("estimate", sa.JSON(), nullable=True),
        sa.Column("skipped_opted_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_no_consent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_no_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_member_id", sa.Integer(), sa.ForeignKey("club_members.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_name", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.CheckConstraint("kind IN ('event_update', 'promotion')", name="ck_club_whatsapp_broadcasts_kind"),
        sa.CheckConstraint("status IN ('scheduled', 'sending', 'done', 'cancelled', 'failed')", name="ck_club_whatsapp_broadcasts_status"),
    )
    op.create_index("ix_club_whatsapp_broadcasts_club", "club_whatsapp_broadcasts", ["club_id", sa.text("id DESC")])
    _set_null_index("ix_club_whatsapp_broadcasts_account", "club_whatsapp_broadcasts", "account_id")
    _set_null_index("ix_club_whatsapp_broadcasts_event", "club_whatsapp_broadcasts", "event_id")
    _set_null_index("ix_club_whatsapp_broadcasts_created_by", "club_whatsapp_broadcasts", "created_by")
    _set_null_index("ix_club_whatsapp_broadcasts_created_by_member", "club_whatsapp_broadcasts", "created_by_member_id")

    op.create_table(
        "club_whatsapp_broadcast_recipients",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broadcast_id", sa.BigInteger(), sa.ForeignKey("club_whatsapp_broadcasts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wa_id", sa.String(20), nullable=True),
        sa.Column("bsuid", sa.String(160), nullable=True),
        sa.Column("registration_id", UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("first_name", sa.String(60), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("skip_reason", sa.String(16), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("held", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("capped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wamid", sa.String(128), nullable=True, unique=True),
        sa.Column("error_code", sa.String(16), nullable=True),
        sa.Column("error_title", sa.String(200), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.UniqueConstraint("broadcast_id", "wa_id", name="uq_club_whatsapp_broadcast_recipients_wa"),
        sa.UniqueConstraint("broadcast_id", "bsuid", name="uq_club_whatsapp_broadcast_recipients_bsuid"),
        sa.CheckConstraint("wa_id IS NOT NULL OR bsuid IS NOT NULL", name="ck_club_whatsapp_broadcast_recipients_who"),
    )
    op.create_index("ix_club_whatsapp_broadcast_recipients_due", "club_whatsapp_broadcast_recipients",
                    ["broadcast_id", "status", "not_before"])
    op.create_index("ix_club_whatsapp_broadcast_recipients_sent", "club_whatsapp_broadcast_recipients", ["club_id", "sent_at"],
                    postgresql_where=sa.text("sent_at IS NOT NULL"))
    _set_null_index("ix_club_whatsapp_broadcast_recipients_registration", "club_whatsapp_broadcast_recipients", "registration_id")
    _set_null_index("ix_club_whatsapp_broadcast_recipients_user", "club_whatsapp_broadcast_recipients", "user_id")

    # delivery_attempts is hot: its index is built without blocking its writes.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_delivery_attempts_fallback", "delivery_attempts", ["created_at"], postgresql_concurrently=True,
            postgresql_where=sa.text("fallback_code IS NOT NULL"), if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_delivery_attempts_fallback", table_name="delivery_attempts", postgresql_concurrently=True, if_exists=True)
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_table("club_whatsapp_broadcast_recipients")
    op.drop_table("club_whatsapp_broadcasts")
    op.drop_table("club_whatsapp_consents")
    op.drop_table("club_whatsapp_spend")
    for table in ("delivery_attempts", "deliveries"):
        for name, _length in reversed(VIA_COLUMNS):
            op.drop_column(table, name)
    for name, _kind, _options in reversed(ACCOUNT_COLUMNS):
        op.drop_column("club_whatsapp_accounts", name)
