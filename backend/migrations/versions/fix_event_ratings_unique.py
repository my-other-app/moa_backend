"""Fix event_ratings_link unique constraint

Revision ID: fix_event_ratings_unique
Revises: ef592d8c28eb
Create Date: 2025-01-11

The original constraint only included (user_id, is_deleted) which prevented
users from rating multiple events. This migration fixes it to include event_id.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fix_event_ratings_unique'
down_revision = 'ef592d8c28eb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old incorrect constraint if it exists
    # We use raw SQL because the constraint name might vary
    op.execute("""
        DO $$ 
        BEGIN
            -- Try to drop constraint with common naming patterns
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'event_ratings_link_user_id_is_deleted_key') THEN
                ALTER TABLE event_ratings_link DROP CONSTRAINT event_ratings_link_user_id_is_deleted_key;
            END IF;
            
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'event_ratings_link_event_id_user_id_is_deleted_key') THEN
                ALTER TABLE event_ratings_link DROP CONSTRAINT event_ratings_link_event_id_user_id_is_deleted_key;
            END IF;
            
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_event_ratings_link_user_is_deleted') THEN
                ALTER TABLE event_ratings_link DROP CONSTRAINT uq_event_ratings_link_user_is_deleted;
            END IF;
        END $$;
    """)
    
    # Create the correct unique constraint
    op.create_unique_constraint(
        'uq_event_ratings_event_user_is_deleted',
        'event_ratings_link',
        ['event_id', 'user_id', 'is_deleted']
    )


def downgrade() -> None:
    op.drop_constraint('uq_event_ratings_event_user_is_deleted', 'event_ratings_link', type_='unique')
