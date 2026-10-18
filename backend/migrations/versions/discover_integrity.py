"""Discover integrity: what the public may see, counters that cannot lose a
write, and the indexes the live room, threads, clubs and "popular" walk.

- clubs.listed: verified, not deleted, and not under a blocked organisation;
  kept by triggers on clubs and organizations (app.api.clubs.models.LISTED_SQL)
  and filled here. Every public rule reads it.
- events.popularity: attention decayed by age (app/api/discover/popularity.py),
  filled here for what can be shown and rewritten every few minutes after.
- events.link_checked_at, clubs.link_checked_at: when a map link was last
  followed for a point (geo.missing), with partial indexes over the rows that
  still want one.
- feedback_surveys.due_at, kept by triggers on the survey and the event's
  start and length (app.api.live.models.FEEDBACK_DUE_SQL), and filled here;
  feedback_sends: who each survey went to, so sending again reaches only the
  rest.
- The post counters (likes, comments, poll votes) kept by triggers
  (app.api.threads.models.THREAD_COUNTS_SQL); every post's likes, comments
  and views, every option's votes and every live question's votes are
  recounted from their rows here first.
- The front page's "cities" is counted again at the next refresh (it now
  counts places on the map, not venue strings).
- Indexes, built CONCURRENTLY (each dropped first if a failed run left it
  invalid): ix_clubs_listed_popular, ix_clubs_listed_newest, ix_clubs_unlisted,
  ix_clubs_link_unchecked, ix_events_shown_popular, ix_events_link_unchecked,
  ix_club_users_followers, ix_club_threads_club_newest,
  ix_thread_comments_thread_created, ix_live_quiz_players_board,
  ix_live_question_votes_voter, ix_live_poll_responses_counts,
  ix_live_poll_responses_voter, ix_feedback_surveys_due.

Lock-light, as discover_scale: the columns (no rewrite: a constant default
or none), the table, the functions and the triggers in one short
transaction; then, each in its own transaction, the backfills (a pass over
clubs, the shown events, the posts and the surveys) and the indexes, built
concurrently so writes go on meanwhile. Writes between the first step and
the backfills are already kept by the triggers.

Downgrade drops the indexes (concurrently), the triggers and functions, the
table and the columns.

Revision ID: discover_integrity
Revises: email_integrity
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "discover_integrity"
down_revision = "email_integrity"
branch_labels = None
depends_on = None

LISTED_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_club_listed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      NEW.listed := coalesce(NEW.is_verified, false) AND NOT coalesce(NEW.is_deleted, false)
        AND (NEW.org_id IS NULL OR NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = NEW.org_id AND o.is_blocked));
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_org_listed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE clubs SET listed = listed WHERE org_id = NEW.id;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_clubs_listed BEFORE INSERT OR UPDATE OF is_verified, is_deleted, org_id, listed ON clubs
    FOR EACH ROW EXECUTE FUNCTION moa_club_listed()
    """,
    """
    CREATE TRIGGER trg_organizations_listed AFTER UPDATE OF is_blocked ON organizations
    FOR EACH ROW WHEN (OLD.is_blocked IS DISTINCT FROM NEW.is_blocked)
    EXECUTE FUNCTION moa_org_listed()
    """,
)

FEEDBACK_DUE_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_feedback_due() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      SELECT timezone('UTC', timezone('UTC', e.event_datetime) + coalesce(e.duration, 0) * interval '1 hour')
             + make_interval(mins => coalesce(NEW.delay_minutes, 0))
      INTO NEW.due_at FROM events e WHERE e.id = NEW.event_id;
      RETURN NEW;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_feedback_due_from_event() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      UPDATE feedback_surveys SET due_at = NULL WHERE event_id = NEW.id;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_feedback_surveys_due BEFORE INSERT OR UPDATE OF delay_minutes, event_id, due_at ON feedback_surveys
    FOR EACH ROW EXECUTE FUNCTION moa_feedback_due()
    """,
    """
    CREATE TRIGGER trg_events_feedback_due AFTER UPDATE OF event_datetime, duration ON events
    FOR EACH ROW WHEN (OLD.event_datetime IS DISTINCT FROM NEW.event_datetime OR OLD.duration IS DISTINCT FROM NEW.duration)
    EXECUTE FUNCTION moa_feedback_due_from_event()
    """,
)

THREAD_COUNTS_SQL = (
    """
    CREATE OR REPLACE FUNCTION moa_thread_likes_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        UPDATE club_threads SET likes_count = likes_count + 1 WHERE id = NEW.thread_id;
      ELSIF TG_OP = 'DELETE' THEN
        UPDATE club_threads SET likes_count = likes_count - 1 WHERE id = OLD.thread_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_thread_comments_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.thread_id = NEW.thread_id AND OLD.is_deleted = NEW.is_deleted THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') AND NOT OLD.is_deleted THEN
        UPDATE club_threads SET comments_count = comments_count - 1 WHERE id = OLD.thread_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') AND NOT NEW.is_deleted THEN
        UPDATE club_threads SET comments_count = comments_count + 1 WHERE id = NEW.thread_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE OR REPLACE FUNCTION moa_thread_votes_count() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.option_id = NEW.option_id THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        UPDATE thread_poll_options SET votes_count = votes_count - 1 WHERE id = OLD.option_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'INSERT') THEN
        UPDATE thread_poll_options SET votes_count = votes_count + 1 WHERE id = NEW.option_id;
      END IF;
      RETURN NULL;
    END $$
    """,
    """
    CREATE TRIGGER trg_thread_likes_count AFTER INSERT OR DELETE ON thread_likes
    FOR EACH ROW EXECUTE FUNCTION moa_thread_likes_count()
    """,
    """
    CREATE TRIGGER trg_thread_comments_count AFTER INSERT OR DELETE OR UPDATE OF thread_id, is_deleted ON thread_comments
    FOR EACH ROW EXECUTE FUNCTION moa_thread_comments_count()
    """,
    """
    CREATE TRIGGER trg_thread_votes_count AFTER INSERT OR DELETE OR UPDATE OF option_id ON thread_votes
    FOR EACH ROW EXECUTE FUNCTION moa_thread_votes_count()
    """,
)

LISTED = """(c.is_verified AND NOT c.is_deleted
  AND (c.org_id IS NULL OR NOT EXISTS (SELECT 1 FROM organizations o WHERE o.id = c.org_id AND o.is_blocked)))"""

BACKFILLS = (
    # Each club's listed, where it is not what the rule says (the trigger works it out the same way).
    f"UPDATE clubs c SET listed = {LISTED} WHERE c.listed IS DISTINCT FROM {LISTED}",
    "VACUUM (ANALYZE) clubs",
    # Each survey's due time, worked out by its trigger.
    "UPDATE feedback_surveys SET due_at = NULL",
    # The counters the triggers keep from here on, from their rows.
    """
    UPDATE club_threads t SET
      likes_count = (SELECT count(*) FROM thread_likes l WHERE l.thread_id = t.id),
      comments_count = (SELECT count(*) FROM thread_comments c WHERE c.thread_id = t.id AND NOT c.is_deleted),
      views_count = (SELECT count(*) FROM thread_views v WHERE v.thread_id = t.id)
    WHERE t.likes_count <> (SELECT count(*) FROM thread_likes l WHERE l.thread_id = t.id)
       OR t.comments_count <> (SELECT count(*) FROM thread_comments c WHERE c.thread_id = t.id AND NOT c.is_deleted)
       OR t.views_count <> (SELECT count(*) FROM thread_views v WHERE v.thread_id = t.id)
    """,
    """
    UPDATE thread_poll_options o SET votes_count = (SELECT count(*) FROM thread_votes v WHERE v.option_id = o.id)
    WHERE o.votes_count <> (SELECT count(*) FROM thread_votes v WHERE v.option_id = o.id)
    """,
    """
    UPDATE live_questions q SET votes = (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id)
    WHERE q.votes <> (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id)
    """,
    # Popularity for what can be shown (app/api/discover/popularity.py: REFRESH_SQL).
    """
    UPDATE events AS t SET popularity = p.v
    FROM (
      SELECT e.id, CASE WHEN timezone('UTC', timezone('UTC', e.event_datetime) + e.duration * INTERVAL '1 hour') < now() THEN 0
                        ELSE ln(1.0 + e.going_count + coalesce(e.page_views, 0)) + floor(extract(epoch FROM e.created_at) / 86400.0) / 30.0 END AS v
      FROM events e
      WHERE NOT e.is_deleted AND e.status IN ('published', 'live')
    ) AS p
    WHERE t.id = p.id AND t.popularity IS DISTINCT FROM p.v
    """,
    # "cities" now counts places on the map: counted again at the next refresh.
    "UPDATE discover_stats SET computed_at = timestamptz '2000-01-01' WHERE name = 'cities'",
)

WANTS_LINK = "location_link IS NOT NULL AND location_link <> '' AND NOT is_deleted AND (lat IS NULL OR geo_source = 'geocoded')"
SHOWN = "NOT is_deleted AND status IN ('published', 'live')"

INDEXES = (
    ("ix_clubs_listed_popular", 'clubs (follower_count DESC, lower(name) COLLATE "C", id) WHERE listed'),
    ("ix_clubs_listed_newest", "clubs (created_at DESC, id DESC) WHERE listed"),
    ("ix_clubs_unlisted", "clubs (id) WHERE NOT listed"),
    ("ix_clubs_link_unchecked", f"clubs (link_checked_at NULLS FIRST, id) WHERE {WANTS_LINK}"),
    ("ix_events_shown_popular", f"events (popularity DESC, event_datetime, id) WHERE {SHOWN}"),
    ("ix_events_link_unchecked", f"events (link_checked_at NULLS FIRST, id) WHERE {WANTS_LINK}"),
    ("ix_club_users_followers", "club_users_link (club_id, created_at DESC, id DESC) WHERE is_following AND NOT is_deleted"),
    ("ix_club_threads_club_newest", "club_threads (club_id, created_at DESC, id) WHERE NOT is_deleted"),
    ("ix_thread_comments_thread_created", "thread_comments (thread_id, created_at, id) WHERE NOT is_deleted"),
    ("ix_live_quiz_players_board", "live_quiz_players (quiz_id, score DESC, created_at)"),
    ("ix_live_question_votes_voter", "live_question_votes (voter_key, question_id)"),
    ("ix_live_poll_responses_counts", "live_poll_responses (poll_id, value, created_at)"),
    ("ix_live_poll_responses_voter", "live_poll_responses (voter_key, poll_id)"),
    ("ix_feedback_surveys_due", "feedback_surveys (due_at) WHERE auto_send AND sent_at IS NULL AND due_at IS NOT NULL"),
)

DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_thread_votes_count ON thread_votes",
    "DROP TRIGGER IF EXISTS trg_thread_comments_count ON thread_comments",
    "DROP TRIGGER IF EXISTS trg_thread_likes_count ON thread_likes",
    "DROP TRIGGER IF EXISTS trg_events_feedback_due ON events",
    "DROP TRIGGER IF EXISTS trg_feedback_surveys_due ON feedback_surveys",
    "DROP TRIGGER IF EXISTS trg_organizations_listed ON organizations",
    "DROP TRIGGER IF EXISTS trg_clubs_listed ON clubs",
    "DROP FUNCTION IF EXISTS moa_thread_votes_count()",
    "DROP FUNCTION IF EXISTS moa_thread_comments_count()",
    "DROP FUNCTION IF EXISTS moa_thread_likes_count()",
    "DROP FUNCTION IF EXISTS moa_feedback_due_from_event()",
    "DROP FUNCTION IF EXISTS moa_feedback_due()",
    "DROP FUNCTION IF EXISTS moa_org_listed()",
    "DROP FUNCTION IF EXISTS moa_club_listed()",
)


def upgrade() -> None:
    op.add_column("clubs", sa.Column("listed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("clubs", sa.Column("link_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("events", sa.Column("popularity", sa.Float(), nullable=False, server_default="0"))
    op.add_column("events", sa.Column("link_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("feedback_surveys", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "feedback_sends",
        sa.Column("survey_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("feedback_surveys.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("registration_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("event_registrations_link.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for sql in LISTED_SQL + FEEDBACK_DUE_SQL + THREAD_COUNTS_SQL:
        op.execute(sql)

    with op.get_context().autocommit_block():
        for sql in BACKFILLS:
            op.execute(sql)
        for name, definition in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY {name} ON {definition}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    for sql in DROP_TRIGGERS:
        op.execute(sql)
    op.drop_table("feedback_sends")
    op.drop_column("feedback_surveys", "due_at")
    op.drop_column("events", "link_checked_at")
    op.drop_column("events", "popularity")
    op.drop_column("clubs", "link_checked_at")
    op.drop_column("clubs", "listed")
