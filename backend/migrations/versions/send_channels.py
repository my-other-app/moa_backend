"""Email, WhatsApp or both, per event and kind.

- events.delivery_channels (json, nullable): how the ticket, the day-before
  reminder and the certificate reach guests, {"ticket": "both"|"email"|
  "whatsapp", ...}. Null for every event: "both", as before.
- deliveries.email_instead (boolean, nullable): a WhatsApp row sent in place
  of the email; when WhatsApp cannot reach the guest, the email goes instead.

Both are nullable columns with no default: a change to the catalogue only,
no rewrite, no scan. lock_timeout bounds the wait for the lock on each table.
Idempotent (IF NOT EXISTS / IF EXISTS).

Downgrade drops the two columns.

Revision ID: send_channels
Revises: games_screen_colors
"""
from alembic import op

revision = "send_channels"
down_revision = "games_screen_colors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS delivery_channels JSON")
    op.execute("ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS email_instead BOOLEAN")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE deliveries DROP COLUMN IF EXISTS email_instead")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS delivery_channels")
