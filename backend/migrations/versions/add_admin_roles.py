"""Admin roles.

Revision ID: add_admin_roles
Revises: add_user_suspension
"""

import sqlalchemy as sa
from alembic import op

revision = "add_admin_roles"
down_revision = "add_user_suspension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    admin_roles = sa.Enum("viewer", "support", "owner", name="adminroles")
    admin_roles.create(op.get_bind(), checkfirst=True)
    # Defaults to owner: existing admins keep what they have. Defaulting to
    # viewer would lock the platform out of itself the moment this deploys.
    op.add_column(
        "users",
        sa.Column(
            "admin_role", admin_roles, nullable=False, server_default="owner"
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "admin_role")
    sa.Enum(name="adminroles").drop(op.get_bind(), checkfirst=True)
