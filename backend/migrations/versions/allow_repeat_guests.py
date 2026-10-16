"""Guests stop being unique by address and phone, and addresses get a
case-blind index.

The public registration form used to hand a session for whichever guest
already had the typed address to anyone who typed it. It now makes a fresh
guest for every sign-up without a sign-in, so one address can stand behind
several guests until they are merged into the app account it belongs to.
(email, user_type) and (phone, user_type) stay unique for every other kind
of account.

The merge looks guests up by lower(btrim(email)) on app launch; the index
keeps that one lookup cheap.

Revision ID: allow_repeat_guests
Revises: add_event_page
"""
import sqlalchemy as sa
from alembic import op

revision = "allow_repeat_guests"
down_revision = "add_event_page"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Created unnamed in the 2025-03 schema, so they carry whatever names
    # Postgres gave them then. Found by their columns rather than guessed.
    op.execute(
        """
        DO $$
        DECLARE c record;
        BEGIN
          FOR c IN
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE rel.relname = 'users'
              AND ns.nspname = current_schema()
              AND con.contype = 'u'
              AND (
                SELECT array_agg(att.attname::text ORDER BY att.attname::text)
                FROM unnest(con.conkey) AS k(attnum)
                JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
              ) IN (ARRAY['email', 'user_type'], ARRAY['phone', 'user_type'])
          LOOP
            EXECUTE format('ALTER TABLE users DROP CONSTRAINT %I', c.conname);
          END LOOP;
        END $$;
        """
    )
    op.create_index(
        "uq_users_email_user_type",
        "users",
        ["email", "user_type"],
        unique=True,
        postgresql_where=sa.text("user_type <> 'guest'"),
    )
    op.create_index(
        "uq_users_phone_user_type",
        "users",
        ["phone", "user_type"],
        unique=True,
        postgresql_where=sa.text("user_type <> 'guest'"),
    )
    op.execute("CREATE INDEX ix_users_email_normalised ON users (lower(btrim(email)))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_email_normalised")
    op.drop_index("uq_users_phone_user_type", table_name="users")
    op.drop_index("uq_users_email_user_type", table_name="users")
    # Refused while two live or retired guests share an address or a phone,
    # which registering from the website now does: merge or remove them first.
    op.create_unique_constraint("users_email_user_type_key", "users", ["email", "user_type"])
    op.create_unique_constraint("users_phone_user_type_key", "users", ["phone", "user_type"])
