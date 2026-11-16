"""The schema the migrations build is the schema the models describe.

The suite builds its database with `metadata.create_all` (fast), and
production's is built by the migration chain. When the two drift, the
tests run against a schema production does not have: an index, a key, a
NOT NULL or a trigger that exists on one side only. Production once ran
for months without four primary keys this way.

This builds both, side by side, on two scratch databases of its own (the
suite's database name with `_schema_head` and `_schema_models` on the
end): one by `alembic upgrade head`, the other by `create_all`, and
compares tables, columns (type and NULL), constraints, indexes, triggers,
trigger functions, enum labels and storage parameters
(tests/schema_catalog.py).

KNOWN lists every difference there is today and why it is left as it is.
A new difference fails the test: declare on the model what the migration
made (or the other way round). So does a listed one that is gone: take it
off the list.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from app.config import settings
from app.db.base import AbstractSQLModel
from tests.schema_catalog import catalogue, differences

BACKEND = Path(__file__).resolve().parents[1]

# ─── What differs today, and why ─────────────────────────────────────────

#: Tables (and their sequences) only the migrations made, from features
#: replaced long ago; nothing in the app reads or writes them. Dropping one
#: is a decision about its rows, not a schema fix.
LEGACY_TABLES = {
    ("table", "after_commit_jobs"),
    ("table", "notification_logs"),
    ("table", "notification_logs_id_seq"),
}

#: Columns only production has: the models stopped using them, the
#: migrations never dropped them (they still hold the old values).
LEGACY_COLUMNS = {
    ("column", "club_sites", "font"),
    ("column", "club_sites", "sections"),
    ("column", "club_sites", "theme"),
    ("column", "events", "kiosk_pin"),
    ("column", "user_device_tokens", "device_id"),
}

#: Types and NULLs the early migrations wrote differently from the models.
#: Putting production right rewrites the table (a type change) or reads it
#: whole under a lock (SET NOT NULL), so they wait for a quiet hour; the
#: code copes with both. user_device_tokens' times are naive in production
#: and only ever compared with each other; the template images are text in
#: production and unbounded varchar in the models, the same to Postgres.
OLD_COLUMN_SHAPES = {
    ("column", "badge_definitions", "created_at"),
    ("column", "badge_definitions", "updated_at"),
    ("column", "certificate_templates", "background_image"),
    ("column", "certificate_templates", "created_at"),
    ("column", "certificate_templates", "is_deleted"),
    ("column", "certificate_templates", "logo"),
    ("column", "certificate_templates", "secondary_logo"),
    ("column", "certificate_templates", "signature_1"),
    ("column", "certificate_templates", "signature_2"),
    ("column", "certificate_templates", "updated_at"),
    ("column", "club_badges", "created_at"),
    ("column", "club_badges", "updated_at"),
    ("column", "profile_projects", "image_key"),
    ("column", "ticket_templates", "background_image"),
    ("column", "ticket_templates", "logo"),
    ("column", "ticket_templates", "secondary_logo"),
    ("column", "user_badges", "created_at"),
    ("column", "user_badges", "updated_at"),
    ("column", "user_device_tokens", "created_at"),
    ("column", "user_device_tokens", "deleted_at"),
    ("column", "user_device_tokens", "updated_at"),
}

#: Foreign keys whose ON DELETE differs: production's is the one the
#: migration chose. Users, events and admins are soft-deleted, so none of
#: these deletes happens in the app.
FOREIGN_KEY_ACTIONS = {
    ("constraint", "admin_audit_logs", "admin_audit_logs_actor_id_fkey"),
    ("constraint", "event_speakers", "event_speakers_event_id_fkey"),
    ("constraint", "user_sessions", "user_sessions_user_id_fkey"),
}

#: The same key under another name on each side (a migration named it,
#: the model let SQLAlchemy name it). Nothing refers to these names.
NAMES = {
    ("constraint", "club_subscriptions", "club_subscriptions_granted_by_fkey"),
    ("constraint", "club_subscriptions", "fk_club_subscriptions_granted_by"),
    ("constraint", "competition_team_members", "competition_team_members_competition_id_fkey"),
    ("constraint", "competition_team_members", "fk_competition_team_members_competition"),
    ("constraint", "events", "events_ticket_template_id_fkey"),
    ("constraint", "events", "fk_events_ticket_template_id"),
    ("constraint", "user_sessions", "fk_user_sessions_impersonated_by"),
    ("constraint", "user_sessions", "fk_user_sessions_member_id"),
    ("constraint", "user_sessions", "user_sessions_impersonated_by_fkey"),
    ("constraint", "user_sessions", "user_sessions_member_id_fkey"),
    ("index", "club_subscriptions", "ix_club_subscriptions_razorpay_id"),
    ("index", "club_subscriptions", "ix_club_subscriptions_razorpay_subscription_id"),
}

#: One column unique by a UNIQUE constraint plus a plain index in
#: production, by one unique index in the models: the same rule either way.
#: Production's plain index is the redundant one (scripts/index_usage.sql).
UNIQUE_SHAPES = {
    ("constraint", "club_sites", "club_sites_club_id_key"),
    ("index", "club_sites", "club_sites_club_id_key"),
    ("index", "club_sites", "ix_club_sites_club_id"),
    ("constraint", "competitions", "competitions_event_id_key"),
    ("index", "competitions", "competitions_event_id_key"),
    ("index", "competitions", "ix_competitions_event_id"),
    ("constraint", "feedback_surveys", "feedback_surveys_event_id_key"),
    ("index", "feedback_surveys", "feedback_surveys_event_id_key"),
    ("index", "feedback_surveys", "ix_feedback_surveys_event_id"),
    ("constraint", "live_quizzes", "live_quizzes_event_id_key"),
    ("index", "live_quizzes", "live_quizzes_event_id_key"),
    ("index", "live_quizzes", "ix_live_quizzes_event_id"),
    ("constraint", "tracking_links", "uq_tracking_links_code"),
    ("index", "tracking_links", "uq_tracking_links_code"),
    ("index", "tracking_links", "ix_tracking_links_code"),
}

#: Indexes only production has. Each is a left prefix of, or the same as,
#: one the models declare, so the tests' plans are production's; whether to
#: drop them is decided from their use (scripts/index_usage.sql).
PRODUCTION_ONLY_INDEXES = {
    ("index", "agenda_items", "ix_agenda_items_event_order"),
    ("index", "club_expenses", "ix_club_expenses_kind"),
    ("index", "club_threads", "ix_club_threads_club_created"),
    ("index", "event_registrations_link", "ix_event_registrations_link_admission_no"),
    ("index", "event_speakers", "ix_event_speakers_event_id"),
    ("index", "retention_jobs", "ix_retention_jobs_state_run_at"),
}

#: The soft-delete mixin's single-column is_deleted index, which these
#: tables' first migrations never made. Such indexes are being retired, not
#: added (no query uses one: scripts/index_usage.sql lists them).
MODEL_ONLY_INDEXES = {
    ("index", "agenda_templates", "ix_agenda_templates_is_deleted"),
    ("index", "badge_templates", "ix_badge_templates_is_deleted"),
    ("index", "certificate_templates", "ix_certificate_templates_is_deleted"),
    ("index", "event_speakers", "ix_event_speakers_is_deleted"),
    ("index", "ticket_templates", "ix_ticket_templates_is_deleted"),
    ("index", "user_device_tokens", "ix_user_device_tokens_is_deleted"),
}

#: Labels added by ALTER TYPE ... ADD VALUE go last; the models list them
#: where they read best. Nothing sorts or compares by these enums' order.
ENUM_ORDER = {
    ("enum", "deliverystatus"),
    ("enum", "orgtypes"),
    ("enum", "signinproviders"),
    ("enum", "subscriptionstatus"),
}

KNOWN = (
    LEGACY_TABLES | LEGACY_COLUMNS | OLD_COLUMN_SHAPES | FOREIGN_KEY_ACTIONS | NAMES | UNIQUE_SHAPES
    | PRODUCTION_ONLY_INDEXES | MODEL_ONLY_INDEXES | ENUM_ORDER
)


# ─── The two databases ───────────────────────────────────────────────────


def _url(suffix: str) -> str:
    parts = urlsplit(settings.DATABASE_URL_SYNC)
    return urlunsplit(parts._replace(path=parts.path + suffix))


def _admin():
    conn = psycopg2.connect(_url_with_path("/postgres"))
    conn.autocommit = True
    return conn


def _url_with_path(path: str) -> str:
    return urlunsplit(urlsplit(settings.DATABASE_URL_SYNC)._replace(path=path))


def _fresh(admin, url: str) -> None:
    name = urlsplit(url).path.lstrip("/")
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{name}"')


def _drop(admin, url: str) -> None:
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{urlsplit(url).path.lstrip("/")}" WITH (FORCE)')


@pytest.fixture(scope="module")
def catalogues():
    """(the migrated database's catalogue, the models' catalogue)."""
    head_url, models_url = _url("_schema_head"), _url("_schema_models")
    try:
        admin = _admin()
    except psycopg2.Error as error:  # pragma: no cover -- no maintenance database
        pytest.skip(f"cannot reach the maintenance database: {error}")
    try:
        _fresh(admin, head_url)
        _fresh(admin, models_url)
        # The real env.py, as production runs it (its guard included), on
        # the scratch database. A Config without the .ini: fileConfig there
        # would reset the suite's loggers.
        cfg = Config()
        cfg.set_main_option("script_location", str(BACKEND / "migrations"))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "DATABASE_URL_SYNC", head_url)
            command.upgrade(cfg, "head")
        engine = create_engine(models_url)
        try:
            AbstractSQLModel.metadata.create_all(engine)
        finally:
            engine.dispose()
        out = []
        for url in (head_url, models_url):
            conn = psycopg2.connect(url)
            try:
                out.append(catalogue(conn))
            finally:
                conn.close()
        yield tuple(out)
    finally:
        _drop(admin, head_url)
        _drop(admin, models_url)
        admin.close()


def _show(key, pair) -> str:
    head, models = pair
    return f"  {key}\n      migrations: {head}\n      models:     {models}"


def test_the_migrations_build_the_schema_the_models_describe(catalogues):
    head, models = catalogues
    found = differences(head, models)
    new = {k: v for k, v in found.items() if k not in KNOWN}
    gone = sorted(KNOWN - set(found), key=str)
    message = []
    if new:
        message.append(
            "The migrations and the models disagree (declare on the model what the migration made, or the other way round):\n"
            + "\n".join(_show(k, new[k]) for k in sorted(new, key=str))
        )
    if gone:
        message.append("Listed in KNOWN but no longer different (take them off the list):\n" + "\n".join(f"  {k}" for k in gone))
    assert not message, "\n\n".join(message)


def test_the_comparison_sees_what_it_compares(catalogues):
    """The catalogue reads every kind of object it compares (a query that
    silently returned nothing would make the test above pass on anything)."""
    head, models = catalogues
    for side in (head, models):
        kinds = {key[0] for key in side}
        assert kinds >= {"table", "column", "constraint", "index", "trigger", "function", "enum", "storage"}
    # This branch's own pieces, on both sides alike.
    for side in (head, models):
        assert side[("storage", "events")] == "fillfactor=85"
        assert "WHERE (NOT is_deleted)" in side[("index", "club_users_link", "ux_club_users_link_live")]
        assert side[("constraint", "ledger_entries", "ck_ledger_entries_kind")][0] == "c"


def test_the_money_checks_are_validated_and_the_old_keys_gone(catalogues):
    """Added NOT VALID by schema_hygiene, validated by schema_hygiene_validate;
    and the unique keys that counted deleted rows are gone, their live-row
    indexes in their place."""
    conn = psycopg2.connect(_url("_schema_head"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conname, convalidated FROM pg_constraint WHERE conname = ANY(%s) ORDER BY conname",
                (["ck_club_refunds_status", "ck_event_store_orders_status", "ck_ledger_entries_kind", "ck_payment_refunds_kind"],),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows == [
        ("ck_club_refunds_status", True), ("ck_event_store_orders_status", True),
        ("ck_ledger_entries_kind", True), ("ck_payment_refunds_kind", True),
    ]
    head, _models = catalogues
    for table, name in (
        ("club_users_link", "club_users_link_club_id_user_id_is_deleted_key"),
        ("event_ratings_link", "uq_event_ratings_event_user_is_deleted"),
        ("club_socials", "club_socials_club_id_is_deleted_key"),
        ("club_subscriptions", "uq_club_subscription_live"),
    ):
        assert ("constraint", table, name) not in head
    for table, name in (
        ("club_users_link", "ux_club_users_link_live"), ("event_ratings_link", "ux_event_ratings_link_live"),
        ("club_socials", "ux_club_socials_live"), ("club_subscriptions", "ux_club_subscriptions_live"),
    ):
        assert ("index", table, name) in head


def test_the_ledger_check_allows_every_kind_the_book_posts():
    from app.api.ledger import book
    from app.api.ledger.models import ENTRY_KINDS

    assert set(ENTRY_KINDS) == set(book.KINDS)


def test_the_money_checks_allow_what_the_code_writes():
    """The words each CHECK allows are the code's own constants."""
    from typing import get_args

    from app.api.admin_ops.models import PAYMENT_REFUND_KINDS
    from app.api.admin_ops.refunds import PaymentRefundIn
    from app.api.club_ops import models as club_ops
    from app.api.store.models import ORDER_STATUSES
    from app.api.store.service import TAKEN

    assert set(get_args(PaymentRefundIn.model_fields["kind"].annotation)) == set(PAYMENT_REFUND_KINDS)
    assert set(club_ops.REFUND_STATUSES) == {club_ops.PENDING, club_ops.PROCESSING, club_ops.SUCCEEDED, club_ops.FAILED}
    assert set(TAKEN) <= set(ORDER_STATUSES)
