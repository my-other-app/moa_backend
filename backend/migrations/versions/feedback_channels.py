"""The feedback survey's channels: a push instead of an email where it can.

Until now a survey was email only: every guest cost the club one of its
month's emails, even the ones whose phones have the app and would have had a
free push (app/api/live/service.send_survey).

- feedback_surveys.channels: the channels the club allows, as a JSON array
  of 'push', 'whatsapp' and 'email'. Every existing survey, and every new
  one, gets '["push", "email"]': whoever has the app is pushed, and only the
  rest are mailed. A club that wants the old behaviour sets ["email"].
- feedback_sends.channel: which channel one guest's survey actually went
  on ('push', 'whatsapp', 'email'), or null for a guest no allowed channel
  could reach (skipped_reason says why) -- and for every row written before
  this column, which was always an email.
- feedback_sends.delivered_at: the channel took it. `mailed_at` keeps its
  old meaning (an email, and only an email, went) and is still written
  beside it, so anything already reading it is unaffected. Rows from before
  are NOT backfilled: `mailed_at` is read as the delivery of a row whose
  channel is null (service.sends_by_channel, service.mail_survey), so a
  survey already sent is never sent again by this change.
- ix_feedback_sends_channel (survey_id, channel): the club's split by
  channel, read from the index alone.

Two nullable columns, one with a constant default, on a small table: no
rewrite, no backfill, no long lock. The JSON default on feedback_surveys is
a constant too (Postgres 11 and up add such a column without rewriting it).

Downgrade drops the index and the three columns. Nothing is lost that
matters: `mailed_at` still records every email that went, which is all the
table held before.

Revision ID: feedback_channels
Revises: org_search
"""
import sqlalchemy as sa
from alembic import op

revision = "feedback_channels"
down_revision = "org_search"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '3s'"
DEFAULT_CHANNELS = """'["push", "email"]'::json"""


def upgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.add_column(
        "feedback_surveys",
        sa.Column("channels", sa.JSON(), nullable=False, server_default=sa.text(DEFAULT_CHANNELS)),
    )
    op.add_column("feedback_sends", sa.Column("channel", sa.String(16), nullable=True))
    op.add_column("feedback_sends", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_feedback_sends_channel", "feedback_sends", ["survey_id", "channel"])


def downgrade() -> None:
    op.execute(LOCK_TIMEOUT)
    op.drop_index("ix_feedback_sends_channel", table_name="feedback_sends")
    op.drop_column("feedback_sends", "delivered_at")
    op.drop_column("feedback_sends", "channel")
    op.drop_column("feedback_surveys", "channels")
