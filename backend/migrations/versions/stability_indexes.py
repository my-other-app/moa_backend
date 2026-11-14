"""Indexes for the lookups that scanned whole tables, and fewer writes on the
busiest insert tables. Every build and drop is CONCURRENTLY, outside a
transaction (an autocommit block), so no reader or writer waits for it.

Added (each first drops one of its name left INVALID by a build that was cut
off, so running this revision again finishes the job):

- ix_waitlist_registration: waitlist_entries (registration_id), the offer a
  paid registration came from, read on every checkout and capture.
- ix_notifications_event_type_created: notifications (event_id, type,
  created_at), a club's announcement history and its insights.
- ix_notifications_type_created: notifications (type, created_at DESC), the
  admin's list of every announcement sent.
- ix_volunteers_user, ix_volunteers_event, ix_volunteers_club: volunteers by
  person (every gate request from a signed-in volunteer), by event and by
  club (the event's team).
- ix_event_ratings_user: event_ratings_link (user_id), a person's ratings,
  read for badges after each check-in.
- ix_registration_requests_event_name / _event_email: the Review list sorted
  by name or email within a status tab, walked in order with no sort.
- ix_payment_orders_user_created: a payer's orders, newest first (the support
  desk's requester panel, the admin payments filter).
- ix_audit_actor_created: the admin audit log filtered by who acted.

Dropped, each only while the index that serves its every read is present and
valid (a left prefix of it, with the same predicate): one fewer B-tree write
per check-in, live vote, poll answer, quiz answer, quiz join and delivery.

- ix_check_ins_event_id (ix_check_ins_event_checked_in_at)
- ix_live_poll_responses_poll_id (uq_live_poll_responses_once)
- ix_live_question_votes_question_id (uq_live_question_votes_once)
- ix_live_quiz_answers_question_id (uq_live_quiz_answers_once)
- ix_live_quiz_players_quiz_id (uq_live_quiz_players_key)
- ix_deliveries_registration_id (uq_deliveries_registration_kind_channel)

Downgrade drops what this added and builds the dropped ones again, all
concurrently.

Revision ID: stability_indexes
Revises: profiles_networking_indexes
"""
import sqlalchemy as sa
from alembic import op

revision = "stability_indexes"
down_revision = "profiles_networking_indexes"
branch_labels = None
depends_on = None

#: (name, table, columns, WHERE) of each index this adds.
ADDED = (
    ("ix_waitlist_registration", "waitlist_entries", "registration_id", "registration_id IS NOT NULL"),
    ("ix_notifications_event_type_created", "notifications", "event_id, type, created_at", "NOT is_deleted"),
    ("ix_notifications_type_created", "notifications", "type, created_at DESC", "NOT is_deleted"),
    ("ix_volunteers_user", "volunteers", "user_id", "NOT is_deleted"),
    ("ix_volunteers_event", "volunteers", "event_id", "NOT is_deleted"),
    ("ix_volunteers_club", "volunteers", "club_id", "NOT is_deleted"),
    ("ix_event_ratings_user", "event_ratings_link", "user_id", "NOT is_deleted"),
    ("ix_registration_requests_event_name", "registration_requests", "event_id, status, lower(full_name), id", None),
    ("ix_registration_requests_event_email", "registration_requests", "event_id, status, lower(email), id", None),
    ("ix_payment_orders_user_created", "payment_orders", "user_id, created_at DESC", "user_id IS NOT NULL"),
    ("ix_audit_actor_created", "admin_audit_logs", "actor_id, created_at DESC", "actor_id IS NOT NULL"),
)

#: (name, table, column, the index that covers it) of each index this drops.
DROPPED = (
    ("ix_check_ins_event_id", "check_ins", "event_id", "ix_check_ins_event_checked_in_at"),
    ("ix_live_poll_responses_poll_id", "live_poll_responses", "poll_id", "uq_live_poll_responses_once"),
    ("ix_live_question_votes_question_id", "live_question_votes", "question_id", "uq_live_question_votes_once"),
    ("ix_live_quiz_answers_question_id", "live_quiz_answers", "question_id", "uq_live_quiz_answers_once"),
    ("ix_live_quiz_players_quiz_id", "live_quiz_players", "quiz_id", "uq_live_quiz_players_key"),
    ("ix_deliveries_registration_id", "deliveries", "registration_id", "uq_deliveries_registration_kind_channel"),
)

_VALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = :name AND n.nspname = current_schema() AND i.indisvalid AND i.indisready"
)


def _create(name: str, table: str, columns: str, where) -> None:
    op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    clause = f" WHERE {where}" if where else ""
    op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns}){clause}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns, where in ADDED:
            _create(name, table, columns, where)
        bind = op.get_bind()
        for name, _table, _column, covering in DROPPED:
            if bind.execute(_VALID, {"name": covering}).first() is not None:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, column, _covering in DROPPED:
            _create(name, table, column, None)
        for name, _table, _columns, _where in reversed(ADDED):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
