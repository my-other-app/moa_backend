"""The games venue screen's own footer image.

- game_settings.footer_image_url (varchar 500, nullable): one wide picture
  the organiser uploads to stand in for the logo strip at the screen's foot.
  Null for every event: the strip shows the logos, as before.

(The live room's big screen keeps its footer image in live_themes.screen,
a JSON column that already exists: no change there.)

A nullable column with no default: a change to the catalogue only, no
rewrite. lock_timeout bounds the wait for the lock. Idempotent.

Downgrade drops the column.

Revision ID: screen_footer_image
Revises: send_channels
"""
from alembic import op

revision = "screen_footer_image"
down_revision = "send_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE game_settings ADD COLUMN IF NOT EXISTS footer_image_url VARCHAR(500)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE game_settings DROP COLUMN IF EXISTS footer_image_url")
