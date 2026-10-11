"""Primary keys the UUID migration lost.

703b6eae394a moved four tables from integer ids to UUIDs by adding a new
column, dropping the old `id` and renaming. The old column carried the
primary key, so it went with it and nothing put one back: the ORM still
declares `id` as the key, but the database has had none since. Nothing
noticed until a table tried to reference `event_registrations_link.id`
with a foreign key and Postgres asked for a unique constraint to point at.

Idempotent: a table that already has a primary key (a database built from
the models, say) is left alone.

Revision ID: add_missing_primary_keys
Revises: add_event_template_variants
"""

from alembic import op
import sqlalchemy as sa

revision = "add_missing_primary_keys"
down_revision = "add_event_template_variants"
branch_labels = None
depends_on = None

TABLES = ("event_files", "event_ratings_link", "event_registrations_link", "notifications")


def _has_primary_key(bind, table: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
                "WHERE r.relname = :t AND c.contype = 'p'"
            ),
            {"t": table},
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        if not _has_primary_key(bind, table):
            op.create_primary_key(f"{table}_pkey", table, ["id"])


def downgrade() -> None:
    # The keys belong on these tables; the earlier migration should have
    # kept them. Nothing to take back.
    pass
