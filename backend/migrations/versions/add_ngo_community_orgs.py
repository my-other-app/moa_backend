"""add ngo and community to org types

Revision ID: add_ngo_community_orgs
Revises: add_blocked_to_orgs
Create Date: 2026-02-23 17:35:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'add_ngo_community_orgs'
down_revision: Union[str, None] = 'add_blocked_to_orgs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # Disable transactions since ALTER TYPE cannot run inside a transaction block
    op.execute("ALTER TYPE orgtypes ADD VALUE IF NOT EXISTS 'ngo';")
    op.execute("ALTER TYPE orgtypes ADD VALUE IF NOT EXISTS 'community';")

def downgrade() -> None:
    # PostgreSQL does not support removing values from an ENUM type.
    pass
