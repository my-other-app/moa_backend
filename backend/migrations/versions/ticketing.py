"""Ticket types, discount codes and waiting lists (app/api/ticketing, docs/TICKETING.md).

Schema:

- ticket_types: an event's kinds of ticket (name, price, capacity, sales
  window, hidden, per-person limit, order), one `is_default` per event, and
  `taken_count`, the places held, kept by a trigger (below).
- discount_codes: a club's codes, for one event or all of its events.
- waitlist_settings (one row per event that set its list up) and
  waitlist_entries (the people in line).
- event_registrations_link gains ticket_type_id, discount_code_id,
  discount_amount and ticket_label, all nullable with no default: a catalog
  change. Their foreign keys go in NOT VALID and are validated after the
  commit (SHARE UPDATE EXCLUSIVE: reads and writes go on), and their indexes
  are built CONCURRENTLY.
- trg_registrations_type_taken keeps ticket_types.taken_count: a
  registration holds a place of its type while it is live and paid or owes
  nothing. Installed before the backfill, so the backfill counts.

Data:

- Every event gets one default ticket type carrying its current price (its
  fee when it has one, else free) and capacity (max_participants), named
  "General admission". While an event has only that type, the type follows
  the event's own price and cap, so nothing changes for an event or a client
  that never adds a second one.
- Every registration is of its event's default type: in batches of a few
  thousand outside a transaction, keyed by id, each re-checking that the row
  still has no type (the old code serves meanwhile; a registration it makes
  after this runs is adopted the first time the club edits the event's types).
- Discount codes and waiting lists are switched on for the plans that
  already promised them: `coupons` where the plan allows any codes
  (coupons_per_event null or above 0: Starter and up), `waitlist` on the
  paid tiers and Enterprise. Free keeps neither. Only switched on, never off.

Downgrade drops the indexes, the trigger, the columns and the tables (types,
codes and lines are lost; the registrations keep their price), and switches
the two flags off again.

Revision ID: ticketing
Revises: club_mailbox
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ticketing"
down_revision = "club_mailbox"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
BATCH = 5000
DEFAULT_NAME = "General admission"
PAID_PLANS = ("starter", "growth", "pro", "campus", "enterprise")

FK_TYPE = "fk_registrations_ticket_type"
FK_CODE = "fk_registrations_discount_code"
IX_TYPE = "ix_registrations_ticket_type"
IX_CODE = "ix_registrations_discount_code"

TAKEN_FUNCTION = """
CREATE OR REPLACE FUNCTION moa_ticket_type_taken() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  old_holds boolean := false;
  new_holds boolean := false;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    old_holds := OLD.ticket_type_id IS NOT NULL AND NOT OLD.is_deleted AND (OLD.is_paid OR OLD.actual_amount = 0);
  END IF;
  IF TG_OP IN ('UPDATE', 'INSERT') THEN
    new_holds := NEW.ticket_type_id IS NOT NULL AND NOT NEW.is_deleted AND (NEW.is_paid OR NEW.actual_amount = 0);
  END IF;
  IF TG_OP = 'UPDATE' AND old_holds = new_holds AND OLD.ticket_type_id IS NOT DISTINCT FROM NEW.ticket_type_id THEN
    RETURN NULL;
  END IF;
  IF old_holds THEN
    UPDATE ticket_types SET taken_count = taken_count - 1 WHERE id = OLD.ticket_type_id;
  END IF;
  IF new_holds THEN
    UPDATE ticket_types SET taken_count = taken_count + 1 WHERE id = NEW.ticket_type_id;
  END IF;
  RETURN NULL;
END $$
"""

TAKEN_TRIGGER = (
    "CREATE TRIGGER trg_registrations_type_taken "
    "AFTER INSERT OR DELETE OR UPDATE OF ticket_type_id, is_deleted, is_paid, actual_amount ON event_registrations_link "
    "FOR EACH ROW EXECUTE FUNCTION moa_ticket_type_taken()"
)

DEFAULT_TYPES = f"""
INSERT INTO ticket_types (event_id, name, price, capacity, is_hidden, sort_order, is_default, taken_count,
                          created_at, updated_at, is_deleted)
SELECT e.id, '{DEFAULT_NAME}',
       CASE WHEN e.has_fee AND e.reg_fee IS NOT NULL AND e.reg_fee > 0 THEN e.reg_fee ELSE 0 END,
       NULLIF(e.max_participants, 0), false, 0, true, 0, now(), now(), false
FROM events e
WHERE NOT EXISTS (
  SELECT 1 FROM ticket_types t WHERE t.event_id = e.id AND t.is_default AND NOT t.is_deleted
)
"""


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _soft_delete():
    return [
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _adopt_registrations() -> None:
    """Each registration of its event's default type, a batch at a time."""
    bind = op.get_bind()
    after = None
    while True:
        ids = [
            str(row[0])
            for row in bind.execute(
                sa.text(
                    "SELECT id FROM event_registrations_link WHERE ticket_type_id IS NULL"
                    + (" AND id > CAST(:after AS uuid)" if after else "")
                    + " ORDER BY id LIMIT :n"
                ),
                {"after": after, "n": BATCH} if after else {"n": BATCH},
            )
        ]
        if not ids:
            return
        bind.execute(
            sa.text(
                "UPDATE event_registrations_link AS r SET ticket_type_id = t.id "
                "FROM ticket_types AS t "
                "WHERE r.id = ANY(CAST(:ids AS uuid[])) AND r.ticket_type_id IS NULL "
                "AND t.event_id = r.event_id AND t.is_default AND NOT t.is_deleted"
            ),
            {"ids": ids},
        )
        after = ids[-1]


def upgrade() -> None:
    # Hot tables below: wait for a lock briefly, and let env.py try again,
    # rather than queue every request behind it.
    op.execute(LOCK_TIMEOUT)
    op.create_table(
        "ticket_types",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("sales_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sales_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("per_person_limit", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("taken_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        *_soft_delete(),
        sa.CheckConstraint("price >= 0", name="ck_ticket_types_price"),
        sa.CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_ticket_types_capacity"),
        sa.CheckConstraint("per_person_limit IS NULL OR per_person_limit >= 1", name="ck_ticket_types_per_person"),
    )
    op.create_index("ix_ticket_types_is_deleted", "ticket_types", ["is_deleted"])
    op.create_index(
        "ix_ticket_types_event", "ticket_types", ["event_id", "sort_order", "id"], postgresql_where=sa.text("NOT is_deleted"),
    )
    op.create_index(
        "uq_ticket_types_event_default", "ticket_types", ["event_id"], unique=True,
        postgresql_where=sa.text("is_default AND NOT is_deleted"),
    )

    op.create_table(
        "discount_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("ticket_type_ids", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("per_person_limit", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_timestamps(),
        *_soft_delete(),
        sa.CheckConstraint("kind IN ('percent', 'amount')", name="ck_discount_codes_kind"),
        sa.CheckConstraint("value >= 0 AND (kind <> 'percent' OR value <= 100)", name="ck_discount_codes_value"),
        sa.CheckConstraint("max_uses IS NULL OR max_uses >= 1", name="ck_discount_codes_max_uses"),
        sa.CheckConstraint("per_person_limit IS NULL OR per_person_limit >= 1", name="ck_discount_codes_per_person"),
    )
    op.create_index("ix_discount_codes_is_deleted", "discount_codes", ["is_deleted"])
    op.create_index(
        "uq_discount_codes_club_code", "discount_codes", ["club_id", "code"], unique=True,
        postgresql_where=sa.text("NOT is_deleted"),
    )
    op.create_index("ix_discount_codes_event", "discount_codes", ["event_id"], postgresql_where=sa.text("NOT is_deleted"))

    op.create_table(
        "waitlist_settings",
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("offer_hours", sa.Integer(), nullable=False, server_default="24"),
        *_timestamps(),
        sa.CheckConstraint("offer_hours BETWEEN 1 AND 168", name="ck_waitlist_offer_hours"),
    )

    op.create_table(
        "waitlist_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_type_id", sa.Integer(), sa.ForeignKey("ticket_types.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("full_name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("whatsapp_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(12), nullable=False, server_default="waiting"),
        sa.Column("offer_token", sa.String(64), nullable=True),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("offer_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("offers_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "registration_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_registrations_link.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('waiting', 'offered', 'claimed', 'expired', 'removed', 'left')", name="ck_waitlist_status"
        ),
        sa.CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="ck_waitlist_contact"),
    )
    op.create_index(
        "ix_waitlist_line", "waitlist_entries", ["event_id", "ticket_type_id", "created_at", "id"],
        postgresql_where=sa.text("status IN ('waiting', 'offered')"),
    )
    op.create_index(
        "ix_waitlist_offer_expiry", "waitlist_entries", ["offer_expires_at"], postgresql_where=sa.text("status = 'offered'"),
    )
    op.create_index("ix_waitlist_event_created", "waitlist_entries", ["event_id", "created_at"])
    op.create_index(
        "uq_waitlist_offer_token", "waitlist_entries", ["offer_token"], unique=True,
        postgresql_where=sa.text("offer_token IS NOT NULL"),
    )
    op.create_index(
        "uq_waitlist_open_email", "waitlist_entries", ["ticket_type_id", sa.text("lower(btrim(email))")], unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND status IN ('waiting', 'offered')"),
    )

    op.add_column("event_registrations_link", sa.Column("ticket_type_id", sa.Integer(), nullable=True))
    op.add_column("event_registrations_link", sa.Column("discount_code_id", sa.Integer(), nullable=True))
    op.add_column("event_registrations_link", sa.Column("discount_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("event_registrations_link", sa.Column("ticket_label", sa.String(80), nullable=True))
    op.execute(
        f"ALTER TABLE event_registrations_link ADD CONSTRAINT {FK_TYPE} FOREIGN KEY (ticket_type_id) "
        "REFERENCES ticket_types (id) ON DELETE SET NULL NOT VALID"
    )
    op.execute(
        f"ALTER TABLE event_registrations_link ADD CONSTRAINT {FK_CODE} FOREIGN KEY (discount_code_id) "
        "REFERENCES discount_codes (id) ON DELETE SET NULL NOT VALID"
    )
    op.execute(TAKEN_FUNCTION)
    op.execute("DROP TRIGGER IF EXISTS trg_registrations_type_taken ON event_registrations_link")
    op.execute(TAKEN_TRIGGER)

    # The plans that promised them (coupons_per_event above 0, the paid tiers).
    op.execute(  # migration-check: ok full-table-update -- a handful of plan rows, the table is not hot
        sa.text(
            "UPDATE plan_features SET coupons = true, updated_at = now() WHERE plan_id IN ("
            "SELECT p.id FROM plans p JOIN plan_limits l ON l.plan_id = p.id "
            "WHERE p.key = ANY(:keys) AND (l.coupons_per_event IS NULL OR l.coupons_per_event > 0))"
        ).bindparams(keys=list(PAID_PLANS))
    )
    op.execute(
        sa.text(
            "UPDATE plan_features SET waitlist = true, updated_at = now() "
            "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"
        ).bindparams(keys=list(PAID_PLANS))
    )

    with op.get_context().autocommit_block():
        op.execute(DEFAULT_TYPES)
        _adopt_registrations()
        op.execute(f"ALTER TABLE event_registrations_link VALIDATE CONSTRAINT {FK_TYPE}")
        op.execute(f"ALTER TABLE event_registrations_link VALIDATE CONSTRAINT {FK_CODE}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {IX_TYPE}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {IX_TYPE} ON event_registrations_link (ticket_type_id) WHERE NOT is_deleted"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {IX_CODE}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {IX_CODE} ON event_registrations_link (discount_code_id) "
            "WHERE discount_code_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {IX_CODE}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {IX_TYPE}")
    op.execute(LOCK_TIMEOUT)
    op.execute(
        sa.text(
            "UPDATE plan_features SET coupons = false, waitlist = false, updated_at = now() "
            "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"
        ).bindparams(keys=list(PAID_PLANS))
    )
    op.execute("DROP TRIGGER IF EXISTS trg_registrations_type_taken ON event_registrations_link")
    op.execute("DROP FUNCTION IF EXISTS moa_ticket_type_taken()")
    op.execute(f"ALTER TABLE event_registrations_link DROP CONSTRAINT IF EXISTS {FK_CODE}")
    op.execute(f"ALTER TABLE event_registrations_link DROP CONSTRAINT IF EXISTS {FK_TYPE}")
    op.drop_column("event_registrations_link", "ticket_label")
    op.drop_column("event_registrations_link", "discount_amount")
    op.drop_column("event_registrations_link", "discount_code_id")
    op.drop_column("event_registrations_link", "ticket_type_id")
    op.drop_table("waitlist_entries")
    op.drop_table("waitlist_settings")
    op.drop_table("discount_codes")
    op.drop_table("ticket_types")
