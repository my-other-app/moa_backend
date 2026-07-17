"""add_order_status_refunded

Add 'refunded' to the orderstatus enum, for the new admin-triggered refund
flow. Not used within this same migration transaction, which Postgres 12+
allows for ALTER TYPE ... ADD VALUE.

Revision ID: add_order_status_refunded
Revises: money_columns_to_numeric
"""
from alembic import op


revision = 'add_order_status_refunded'
down_revision = 'money_columns_to_numeric'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'refunded'")


def downgrade() -> None:
    # Postgres doesn't support removing an enum value directly; rebuilding
    # the type is destructive and unnecessary for a downgrade path here.
    pass
