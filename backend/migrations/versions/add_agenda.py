"""The run of show (agenda items) and the programme design per club or event.

Revision ID: add_agenda
Revises: add_kiosk
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_agenda"
down_revision = "add_kiosk"
branch_labels = None
depends_on = None

AUDIENCE = postgresql.ENUM("event_default", "attendee", "speaker", "staff", name="templateaudience", create_type=False)


def upgrade() -> None:
    op.create_table(
        "agenda_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("speaker", sa.String(120), nullable=True),
        sa.Column("speaker_photo_key", sa.String(300), nullable=True),
        sa.Column("location", sa.String(120), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_agenda_items_event_id", "agenda_items", ["event_id"])
    op.create_index("ix_agenda_items_event_order", "agenda_items", ["event_id", "starts_at", "sort_order"])

    op.create_table(
        "agenda_templates",
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
    op.create_index("ix_agenda_templates_event_id", "agenda_templates", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_agenda_templates_event_id", table_name="agenda_templates")
    op.drop_table("agenda_templates")
    op.drop_index("ix_agenda_items_event_order", table_name="agenda_items")
    op.drop_index("ix_agenda_items_event_id", table_name="agenda_items")
    op.drop_table("agenda_items")
