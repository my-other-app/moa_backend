"""Venue screen: custom colours, and a board that can show everyone.

- game_settings.colors (JSONB, null): the screen's own background, surface,
  text, accent and medal colours over the theme; null keeps the theme's.
- ck_game_settings_rows now allows 0 (everyone, up to 500 a board), 25 and 50
  besides 5, 7 and 10.

A nullable column with no default and a CHECK swap on a small table; each
waits at most 5 s for its lock. Downgrade puts rows outside 5/7/10 back to 7.

Revision ID: games_screen_colors
Revises: games
"""
from alembic import op

revision = "games_screen_colors"
down_revision = "games"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE game_settings ADD COLUMN IF NOT EXISTS colors JSONB")
    op.execute("ALTER TABLE game_settings DROP CONSTRAINT IF EXISTS ck_game_settings_rows")
    op.create_check_constraint("ck_game_settings_rows", "game_settings", "board_rows IN (0, 5, 7, 10, 25, 50)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE game_settings SET board_rows = 7 WHERE board_rows NOT IN (5, 7, 10)")
    op.execute("ALTER TABLE game_settings DROP CONSTRAINT IF EXISTS ck_game_settings_rows")
    op.create_check_constraint("ck_game_settings_rows", "game_settings", "board_rows IN (5, 7, 10)")
    op.drop_column("game_settings", "colors")
