"""AI assist: what each plan allows, what each club used, and clubs' own keys.

- plan_features.ai_assist (AI assist on the platform's key) and
  plan_features.ai_byok (AI assist on the club's own key), both off by
  default like every flag; plan_limits.ai_actions_per_month (NULL is
  unlimited).
- Seeded on the six plans club_plans made (`seed`): ai_assist on Starter and
  up, with 20, 150 and 500 actions a month on Starter, Growth and Pro and no
  cap on Campus and Enterprise; Free has it off and a cap of 0. ai_byok on
  every plan there is, whatever its key -- the club pays its own provider,
  so no plan withholds it. A plan that does not exist is skipped.
- ai_usage: one row per action (models.AIUsage), indexed (club_id,
  created_at) for the monthly count.
- club_ai_keys: a club's own key, encrypted, at most one per club.
- ai_feedback_summaries: an event's last summary of its survey comments.

Every table is new or small (plans: a handful of rows); every step checks
first, so the upgrade is safe to run again. The foreign keys to clubs, users,
club_members and events take a brief SHARE ROW EXCLUSIVE on those tables as
the new tables are created, under this migration's short lock_timeout
(env.py tries again when it is not granted in time).

Downgrade drops the three tables and the three columns; the flags and caps
set here go with them.

Revision ID: ai_assist
Revises: club_merch
"""
import sqlalchemy as sa
from alembic import op

revision = "ai_assist"
down_revision = "club_merch"
branch_labels = None
depends_on = None

#: The plans with AI assist on the platform's key, and their monthly actions
#: (None: unlimited). Free is left off, at 0.
ASSIST = {
    "starter": 20,
    "growth": 150,
    "pro": 500,
    "campus": None,
    "enterprise": None,
}
NO_ASSIST = {"free": 0}


def seed(bind) -> None:
    """The flags and caps above. Safe to run again: it sets the same values."""
    bind.execute(
        sa.text(
            "UPDATE plan_features SET ai_assist = true, updated_at = now() "
            "WHERE plan_id IN (SELECT id FROM plans WHERE key = ANY(:keys))"
        ),
        dict(keys=list(ASSIST)),
    )
    for key, cap in {**ASSIST, **NO_ASSIST}.items():
        bind.execute(
            sa.text(
                "UPDATE plan_limits SET ai_actions_per_month = :cap, updated_at = now() "
                "WHERE plan_id IN (SELECT id FROM plans WHERE key = :key)"
            ),
            dict(cap=cap, key=key),
        )
    bind.execute(sa.text("UPDATE plan_features SET ai_byok = true, updated_at = now() WHERE ai_byok = false"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE plan_features ADD COLUMN IF NOT EXISTS ai_assist boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE plan_features ADD COLUMN IF NOT EXISTS ai_byok boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE plan_limits ADD COLUMN IF NOT EXISTS ai_actions_per_month integer")
    seed(op.get_bind())

    op.execute(
        "CREATE TABLE IF NOT EXISTS ai_usage ("
        " id bigserial PRIMARY KEY,"
        " club_id integer NOT NULL REFERENCES clubs (id) ON DELETE CASCADE,"
        " user_id integer REFERENCES users (id) ON DELETE SET NULL,"
        " member_id integer REFERENCES club_members (id) ON DELETE SET NULL,"
        " action varchar(32) NOT NULL,"
        " provider varchar(20) NOT NULL,"
        " model varchar(80),"
        " byok boolean NOT NULL DEFAULT false,"
        " input_tokens integer NOT NULL DEFAULT 0,"
        " output_tokens integer NOT NULL DEFAULT 0,"
        " latency_ms integer,"
        " ok boolean,"
        " error varchar(40),"
        " created_at timestamp with time zone NOT NULL DEFAULT now())"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_club_created ON ai_usage (club_id, created_at)")

    op.execute(
        "CREATE TABLE IF NOT EXISTS club_ai_keys ("
        " id serial PRIMARY KEY,"
        " club_id integer NOT NULL REFERENCES clubs (id) ON DELETE CASCADE,"
        " provider varchar(20) NOT NULL,"
        " encrypted_key text NOT NULL,"
        " model varchar(80) NOT NULL,"
        " key_last4 varchar(4) NOT NULL,"
        " verified_at timestamp with time zone,"
        " created_by integer REFERENCES users (id) ON DELETE SET NULL,"
        " created_at timestamp with time zone NOT NULL DEFAULT now(),"
        " updated_at timestamp with time zone NOT NULL DEFAULT now(),"
        " CONSTRAINT club_ai_keys_club_id_key UNIQUE (club_id))"
    )

    op.execute(
        "CREATE TABLE IF NOT EXISTS ai_feedback_summaries ("
        " event_id integer PRIMARY KEY REFERENCES events (id) ON DELETE CASCADE,"
        " responses integer NOT NULL,"
        " summary json NOT NULL,"
        " provider varchar(20),"
        " model varchar(80),"
        " generated_at timestamp with time zone NOT NULL DEFAULT now())"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("DROP TABLE IF EXISTS ai_feedback_summaries")
    op.execute("DROP TABLE IF EXISTS club_ai_keys")
    op.execute("DROP TABLE IF EXISTS ai_usage")
    op.execute("ALTER TABLE plan_limits DROP COLUMN IF EXISTS ai_actions_per_month")
    op.execute("ALTER TABLE plan_features DROP COLUMN IF EXISTS ai_byok")
    op.execute("ALTER TABLE plan_features DROP COLUMN IF EXISTS ai_assist")
