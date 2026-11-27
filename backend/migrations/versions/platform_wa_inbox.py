"""The admin WhatsApp inbox for MyOtherApp's own number (app/api/platform_whatsapp).

- platform_wa_conversations: one per guest who writes to the platform's
  number (wa_id and/or BSUID), with the window, unread count, the event and
  club it is probably about, and STOP.
- platform_wa_messages: what they wrote and the admins' manual replies
  (who sent each). The automatic sends stay in delivery_attempts.
- ix_delivery_attempts_wa_address: delivery_attempts(address, created_at)
  for WhatsApp attempts only, so a thread reads its automatic sends by index.

Two new tables and one partial index; nothing existing changes. lock_timeout
bounds the wait for delivery_attempts' lock while the index is built.
Idempotent.

Downgrade drops the index and both tables.

Revision ID: platform_wa_inbox
Revises: print_request_door
"""
from alembic import op

revision = "platform_wa_inbox"
down_revision = "print_request_door"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_wa_conversations (
            id BIGSERIAL PRIMARY KEY,
            wa_id VARCHAR(20),
            bsuid VARCHAR(160),
            profile_name VARCHAR(120),
            username VARCHAR(120),
            last_inbound_at TIMESTAMPTZ,
            last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_preview VARCHAR(160),
            last_direction VARCHAR(3),
            unread_count INTEGER NOT NULL DEFAULT 0,
            about_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            about_club_id INTEGER REFERENCES clubs(id) ON DELETE SET NULL,
            opted_out_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_platform_wa_conversations_who CHECK (wa_id IS NOT NULL OR bsuid IS NOT NULL)
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_wa_conversations_wa ON platform_wa_conversations (wa_id) WHERE wa_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_wa_conversations_bsuid ON platform_wa_conversations (bsuid) WHERE bsuid IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_wa_conversations_last ON platform_wa_conversations (last_message_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_wa_conversations_about_event_id ON platform_wa_conversations (about_event_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_wa_conversations_about_club_id ON platform_wa_conversations (about_club_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_wa_messages (
            id BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL REFERENCES platform_wa_conversations(id) ON DELETE CASCADE,
            direction VARCHAR(3) NOT NULL,
            type VARCHAR(20) NOT NULL,
            body TEXT,
            media_id VARCHAR(128),
            media_mime VARCHAR(100),
            media_filename VARCHAR(255),
            template_name VARCHAR(80),
            template_params JSON,
            wamid VARCHAR(128),
            status VARCHAR(12) NOT NULL,
            error VARCHAR(300),
            event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
            club_id INTEGER REFERENCES clubs(id) ON DELETE SET NULL,
            sent_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            sent_by_name VARCHAR(120),
            at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_platform_wa_messages_direction CHECK (direction IN ('in', 'out'))
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_wa_messages_wamid ON platform_wa_messages (wamid) WHERE wamid IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_wa_messages_conversation ON platform_wa_messages (conversation_id, at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_wa_messages_sent_by ON platform_wa_messages (sent_by_user_id) WHERE sent_by_user_id IS NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_delivery_attempts_wa_address ON delivery_attempts (address, created_at) "
        "WHERE provider = 'whatsapp'"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_delivery_attempts_wa_address")
    op.execute("DROP TABLE IF EXISTS platform_wa_messages")
    op.execute("DROP TABLE IF EXISTS platform_wa_conversations")
