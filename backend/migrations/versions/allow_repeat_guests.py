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
    # Production's schema has drifted from the migrations before (it once
    # lacked primary keys). If it never had these constraints and already
    # holds duplicates, a unique index would fail the whole deploy; then the
    # index is created plain, as unenforced as before, and a notice says so.
    for column in ("email", "phone"):
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM users
                WHERE user_type <> 'guest' AND {column} IS NOT NULL
                GROUP BY {column}, user_type HAVING count(*) > 1
              ) THEN
                RAISE NOTICE 'users has duplicate ({column}, user_type) outside guests; index created without UNIQUE';
                CREATE INDEX uq_users_{column}_user_type ON users ({column}, user_type) WHERE user_type <> 'guest';
              ELSE
                CREATE UNIQUE INDEX uq_users_{column}_user_type ON users ({column}, user_type) WHERE user_type <> 'guest';
              END IF;
            END $$;
            """
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
