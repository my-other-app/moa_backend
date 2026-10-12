"""The event store (merchandise for sale), swag details, and the two per-event switches.

Revision ID: add_event_store
Revises: add_club_threads
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_event_store"
down_revision = "add_club_threads"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("events", sa.Column("merch_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("events", sa.Column("swag_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("merch_items", sa.Column("sponsor", sa.String(120), nullable=True))
    op.add_column("merch_items", sa.Column("handout_rule", sa.String(40), nullable=True))
    op.add_column("merch_items", sa.Column("manual_handed_out", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "event_store_items",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("photo_key", sa.String(300), nullable=True),
        sa.Column("price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("sold_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_roles", sa.JSON(), nullable=True),
        sa.Column("free_roles", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("on_sale", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_store_items_event_id", "event_store_items", ["event_id"])
    op.create_index("ix_event_store_items_is_deleted", "event_store_items", ["is_deleted"])
    op.create_table(
        "event_store_variants",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("item_id", UUID, sa.ForeignKey("event_store_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sold_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", "label", name="uq_event_store_variants_item_label"),
    )
    op.create_index("ix_event_store_variants_item_id", "event_store_variants", ["item_id"])
    op.create_table(
        "event_store_orders",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("registration_id", UUID, sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("total", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handed_over_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_store_orders_event_id", "event_store_orders", ["event_id"])
    op.create_index("ix_event_store_orders_registration_id", "event_store_orders", ["registration_id"])
    op.create_table(
        "event_store_order_lines",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", UUID, sa.ForeignKey("event_store_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", UUID, sa.ForeignKey("event_store_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant_id", UUID, sa.ForeignKey("event_store_variants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("item_name", sa.String(120), nullable=False),
        sa.Column("variant_label", sa.String(40), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("line_total", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("is_free", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_store_order_lines_order_id", "event_store_order_lines", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_event_store_order_lines_order_id", table_name="event_store_order_lines")
    op.drop_table("event_store_order_lines")
    op.drop_index("ix_event_store_orders_registration_id", table_name="event_store_orders")
    op.drop_index("ix_event_store_orders_event_id", table_name="event_store_orders")
    op.drop_table("event_store_orders")
    op.drop_index("ix_event_store_variants_item_id", table_name="event_store_variants")
    op.drop_table("event_store_variants")
    op.drop_index("ix_event_store_items_is_deleted", table_name="event_store_items")
    op.drop_index("ix_event_store_items_event_id", table_name="event_store_items")
    op.drop_table("event_store_items")
    op.drop_column("merch_items", "manual_handed_out")
    op.drop_column("merch_items", "handout_rule")
    op.drop_column("merch_items", "sponsor")
    op.drop_column("events", "swag_enabled")
    op.drop_column("events", "merch_enabled")
