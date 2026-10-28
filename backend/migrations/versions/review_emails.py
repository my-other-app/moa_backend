"""The registration review emails become the club's to edit
(docs/APPROVALS.md §9, app/api/emails).

emailstage gains six values, one per email a registration under review
gets: REQUEST_RECEIVED, REQUEST_APPROVED, REQUEST_APPROVED_PAY,
REQUEST_DECLINED, REQUEST_LAPSED and REQUEST_UNANSWERED. They are kept in
email_templates as library rows only (event_id null), one per club per
stage, which uq_email_templates_library_stage already enforces (its
predicate is `stage <> 'CUSTOM'`). The rows are made from the starters the
first time a club's library is read; this migration writes none.

The values are added with ADD VALUE IF NOT EXISTS in autocommit, as
Postgres needs before a new value can be used. That touches only the
catalogue: no table is rewritten or locked beyond an instant.

Downgrade deletes the rows in the new stages (they are only the club's
edited words; the code before this sends its own). Postgres cannot drop an
enum value: the six stay in the type, unused, and the upgrade adds them only
if missing.

Revision ID: review_emails
Revises: add_event_partners
"""
from alembic import op

revision = "review_emails"
down_revision = "add_event_partners"
branch_labels = None
depends_on = None

STAGES = (
    "REQUEST_RECEIVED", "REQUEST_APPROVED", "REQUEST_APPROVED_PAY", "REQUEST_DECLINED", "REQUEST_LAPSED",
    "REQUEST_UNANSWERED",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for stage in STAGES:
            op.execute(f"ALTER TYPE emailstage ADD VALUE IF NOT EXISTS '{stage}'")


def downgrade() -> None:
    names = ", ".join(f"'{s}'" for s in STAGES)
    # Compared as text: the values stay in the type, but a database that
    # never had them answers the same.
    op.execute(f"DELETE FROM email_templates WHERE stage::text IN ({names})")
