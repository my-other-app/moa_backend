"""The ledger takes income too: each entry is an expense or an income.

Revision ID: add_ledger_income
Revises: add_live_room
"""
import sqlalchemy as sa
from alembic import op

revision = "add_ledger_income"
down_revision = "add_live_room"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("club_expenses", sa.Column("kind", sa.String(length=10), nullable=False, server_default="expense"))
    op.create_index("ix_club_expenses_kind", "club_expenses", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_club_expenses_kind", table_name="club_expenses")
    op.drop_column("club_expenses", "kind")
