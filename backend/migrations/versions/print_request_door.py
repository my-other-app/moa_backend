"""The door a print request was asked for at.

- card_print_requests.door_id (uuid, nullable, FK doors ON DELETE SET NULL):
  the door console, a kiosk tablet or the dashboard records the door, and
  that door's print station takes the card first. Null for every existing
  row: any station, as before.
- ix_card_print_requests_event_door (event_id, door_id).

A nullable column with no default: a change to the catalogue only, no
rewrite. The table is small (one row per card asked for), so the index is
built in the transaction. lock_timeout bounds the wait for the lock.
Idempotent.

Downgrade drops the index and the column.

Revision ID: print_request_door
Revises: screen_footer_image
"""
from alembic import op

revision = "print_request_door"
down_revision = "screen_footer_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE card_print_requests ADD COLUMN IF NOT EXISTS door_id UUID")
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'card_print_requests_door_id_fkey') THEN
                ALTER TABLE card_print_requests ADD CONSTRAINT card_print_requests_door_id_fkey
                    FOREIGN KEY (door_id) REFERENCES doors (id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_card_print_requests_event_door ON card_print_requests (event_id, door_id)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_card_print_requests_event_door")
    op.execute("ALTER TABLE card_print_requests DROP CONSTRAINT IF EXISTS card_print_requests_door_id_fkey")
    op.execute("ALTER TABLE card_print_requests DROP COLUMN IF EXISTS door_id")
