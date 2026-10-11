"""Merchandise: items with stock or sized variants, and every handover.

Revision ID: add_merch
Revises: add_agenda
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_merch"
down_revision = "add_agenda"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merch_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("photo_key", sa.String(300), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("collected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligibility_field", sa.String(80), nullable=True),
        sa.Column("eligibility_value", sa.String(80), nullable=True),
        sa.Column("variant_field", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_merch_items_event_id", "merch_items", ["event_id"])

    op.create_table(
        "merch_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("merch_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("collected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("item_id", "label", name="uq_merch_variants_item_label"),
    )
    op.create_index("ix_merch_variants_item_id", "merch_variants", ["item_id"])

    op.create_table(
        "merch_collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("merch_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("merch_variants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("door_label", sa.String(40), nullable=True),
        sa.Column("door_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("doors.id", ondelete="SET NULL"), nullable=True),
        sa.Column("collected_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("registration_id", "item_id", name="uq_merch_collections_registration_item"),
    )
    op.create_index("ix_merch_collections_registration_id", "merch_collections", ["registration_id"])
    op.create_index("ix_merch_collections_item_id", "merch_collections", ["item_id"])
    op.create_index("ix_merch_collections_door_id", "merch_collections", ["door_id"])
    op.create_index("ix_merch_collections_collected_at", "merch_collections", ["collected_at"])


def downgrade() -> None:
    op.drop_table("merch_collections")
    op.drop_table("merch_variants")
    op.drop_table("merch_items")
