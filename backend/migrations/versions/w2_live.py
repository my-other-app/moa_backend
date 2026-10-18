"""The live room's survey mails become jobs: feedback_sends.mailed_at.

- feedback_sends.mailed_at (nullable): set by the job that mails a guest the
  survey (live.survey_mail) once the provider took it. A row is still
  claimed (`sent_at`) in the transaction that sends the survey, and each
  claimed guest gets a job in that transaction; the job skips a guest whose
  mail went already, so a job run twice -- the queue is at least once --
  mails nobody twice.
- Every row there already was claimed by the code before, which mailed it
  from memory right after the commit: those are taken as mailed
  (mailed_at = sent_at), in batches that each commit, so nothing will look
  for them again.

Adding a nullable column without a default rewrites nothing; it waits at
most `lock_timeout` for the table, which only the survey's send and the
minute sweep write. The backfill holds each batch's rows only until it
commits.

Downgrade drops the column.

Revision ID: w2_live
Revises: w2_search
"""
import sqlalchemy as sa
from alembic import op

revision = "w2_live"
down_revision = "w2_search"
branch_labels = None
depends_on = None

BATCH = 5000


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("feedback_sends", sa.Column("mailed_at", sa.DateTime(timezone=True), nullable=True))
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while True:
            done = bind.execute(sa.text(
                "UPDATE feedback_sends SET mailed_at = sent_at WHERE ctid IN "
                "(SELECT ctid FROM feedback_sends WHERE mailed_at IS NULL LIMIT :batch)"
            ), {"batch": BATCH}).rowcount
            if not done or done < BATCH:
                break


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.drop_column("feedback_sends", "mailed_at")
