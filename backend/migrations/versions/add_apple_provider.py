"""The sign-in provider type gains 'apple'.

The model has had SigninProviders.apple since Apple sign-in shipped, but no
migration ever added it: a database built from the migrations alone holds
only 'google' and 'email', and saving a new Apple account there fails.
Production presumably had it added by hand; IF NOT EXISTS makes this a no-op
there and a fix everywhere else.

Revision ID: add_apple_provider
Revises: add_email_verification
"""
from alembic import op

revision = "add_apple_provider"
down_revision = "add_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE signinproviders ADD VALUE IF NOT EXISTS 'apple'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum; leaving it is harmless.
    pass
