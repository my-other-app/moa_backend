"""Sign in with Apple's refresh token, kept so a deleted account's can be revoked.

App Review requires an app with Sign in with Apple to revoke the user's
token when they delete their account (guideline 5.1.1(v)). The code the app
sends at sign-in is now exchanged for a refresh token, kept encrypted here
(app.api.auth.apple_tokens), and revoked by DELETE /user/delete.

Schema:

- users.apple_refresh_token (text, nullable): Fernet ciphertext under
  APPLE_TOKENS_SECRET. Empty for every account until its next Apple sign-in.

Downgrade drops the column (the kept tokens go with it; they can no longer
be revoked from here).

Revision ID: apple_refresh_token
Revises: message_credits
"""
from alembic import op

revision = "apple_refresh_token"
down_revision = "message_credits"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS apple_refresh_token text")


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS apple_refresh_token")
