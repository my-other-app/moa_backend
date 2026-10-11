"""ID cards: badge designs, print jobs, the event's print rules and when each guest's card was printed.

Revision ID: add_id_cards
Revises: add_doors_check_ins
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_id_cards"
down_revision = "add_doors_check_ins"
branch_labels = None
depends_on = None

AUDIENCE = postgresql.ENUM("event_default", "attendee", "speaker", "staff", name="templateaudience", create_type=False)
REPRINT = postgresql.ENUM("anyone", "organiser", "never", name="reprintpolicy", create_type=False)
SOURCE = postgresql.ENUM("checkin", "manual", "batch", "walkup", "kiosk", "test", name="printsource", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for t in (REPRINT, SOURCE):
        t.create(bind, checkfirst=True)

    op.create_table(
        "badge_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("club_id", sa.Integer(), sa.ForeignKey("clubs.id"), nullable=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("audience", AUDIENCE, nullable=False, server_default="event_default"),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_badge_templates_event_id", "badge_templates", ["event_id"])

    op.create_table(
        "print_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id"), nullable=True),
        sa.Column("source", SOURCE, nullable=False, server_default="manual"),
        sa.Column("door_label", sa.String(40), nullable=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cards", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("design_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("overrode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.String(400), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_print_jobs_event_id", "print_jobs", ["event_id"])
    op.create_index("ix_print_jobs_registration_id", "print_jobs", ["registration_id"])

    op.add_column("events", sa.Column("reprint_policy", REPRINT, nullable=False, server_default="anyone"))
    op.add_column("events", sa.Column("auto_print_badges", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("events", sa.Column("preprinted_carries", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("event_registrations_link", sa.Column("badge_printed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("event_registrations_link", "badge_printed_at")
    for col in ("preprinted_carries", "auto_print_badges", "reprint_policy"):
        op.drop_column("events", col)
    op.drop_table("print_jobs")
    op.drop_table("badge_templates")
    bind = op.get_bind()
    for t in (SOURCE, REPRINT):
        t.drop(bind, checkfirst=True)
