"""Per-event ticket and certificate designs, with per-role variants.

Templates were owned by a club and chosen per event, so two events sharing
one template could not differ -- editing either changed both. An event's
design now forks into a row of its own the first time it is edited, keyed by
the role it is for.

Registrations gain a role for the same reason: a speaker's badge is not an
attendee's, and nothing recorded which someone was.

Revision ID: add_event_template_variants
Revises: add_subscriptions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_event_template_variants"
down_revision = "add_subscriptions"
branch_labels = None
depends_on = None

AUDIENCE = postgresql.ENUM(
    "event_default", "attendee", "speaker", "staff",
    name="templateaudience",
    create_type=False,
)
ROLE = postgresql.ENUM(
    "attendee", "speaker", "staff",
    name="registrationrole",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    # create_type=False above, so the types are made once here rather than
    # implicitly by each add_column -- which is what raises DuplicateObject
    # when a table uses the same enum twice.
    AUDIENCE.create(bind, checkfirst=True)
    ROLE.create(bind, checkfirst=True)

    for table in ("ticket_templates", "certificate_templates"):
        op.add_column(
            table,
            sa.Column("event_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            f"ix_{table}_event_id", table, ["event_id"], unique=False
        )
        op.create_foreign_key(
            f"fk_{table}_event_id", table, "events", ["event_id"], ["id"]
        )
        op.add_column(
            table,
            sa.Column(
                "audience",
                AUDIENCE,
                nullable=False,
                server_default="event_default",
            ),
        )
        # A club template keeps its club; an event's fork has none, so the
        # column can no longer be required.
        op.alter_column(table, "club_id", existing_type=sa.Integer(), nullable=True)

    op.add_column(
        "event_registrations_link",
        sa.Column("role", ROLE, nullable=False, server_default="attendee"),
    )


def downgrade() -> None:
    op.drop_column("event_registrations_link", "role")

    for table in ("ticket_templates", "certificate_templates"):
        # Event-owned templates have no club to fall back to, so they go
        # rather than being left orphaned by a NOT NULL that cannot hold.
        op.execute(sa.text(f"DELETE FROM {table} WHERE event_id IS NOT NULL"))
        op.alter_column(table, "club_id", existing_type=sa.Integer(), nullable=False)
        op.drop_column(table, "audience")
        op.drop_constraint(f"fk_{table}_event_id", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_event_id", table_name=table)
        op.drop_column(table, "event_id")

    bind = op.get_bind()
    ROLE.drop(bind, checkfirst=True)
    AUDIENCE.drop(bind, checkfirst=True)
