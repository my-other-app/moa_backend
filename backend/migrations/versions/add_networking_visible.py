"""People Like You: whether a person shows in an event's networking list.

- users.networking_visible (NOT NULL DEFAULT true): on for everyone, the
  people already here included -- the switch is something a person turns
  off, not something they have to find and turn on. Off, they never appear
  in anyone's list (app/api/events/networking.py).

A column with a constant default is a change to the catalogue only -- no
rewrite, no scan -- but ALTER TABLE still waits for an ACCESS EXCLUSIVE lock
on users, and every query on it queues behind that wait. lock_timeout
bounds it at three seconds: on a busy moment the migration fails and is run
again rather than holding up sign-ins.

Downgrade drops the column; everyone shows again.

Revision ID: add_networking_visible
Revises: mcp_server
"""
import sqlalchemy as sa
from alembic import op

revision = "add_networking_visible"
down_revision = "mcp_server"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("users", sa.Column("networking_visible", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_column("users", "networking_visible")
