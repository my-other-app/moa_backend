"""Wipe the database clean except the platform's admin accounts and its
reference data, so the demo (scripts/demo/seed.py) starts from nothing.

    python -m scripts.demo.reset                       # a dry run: what would go
    APP_DEMO_RESET_ALLOWED=1 python -m scripts.demo.reset --execute --confirm <database name>

A dry run is the default and changes nothing: it reads, in a read-only
transaction, and prints every table with what happens to it and how many
rows would go. To execute, all three are needed -- `--execute`,
`--confirm` with the exact name of the database it is connected to, and
APP_DEMO_RESET_ALLOWED=1 in the environment. Anything missing and it
refuses before it reads a row.

Every table is classified by hand in CLASSIFICATION below:

- KEEP: every row stays: the plans and what they include, the topics and
  interests, the avatar choices, the badge definitions, the migrations'
  version, and the platform's own machinery (the sweeps' last runs, the job
  runners' heartbeats, the one computed-figures row);
- FILTERED: the rows that match its filter stay, the rest go (the admins
  and what they need to sign in);
- WIPE: every row goes.

A table in the database that is not classified stops the reset, named:
a table added by a later migration is decided by a person, not swept away
by default.

All of it is one transaction. Rows are removed with TRUNCATE where nothing
kept points at the table, and with DELETE, children first, where something
does; never TRUNCATE ... CASCADE, which could reach a kept table. A kept row
that points at a row going has that column emptied first (an admin's
session that was acting for a club member), or the reset refuses if the
column cannot be empty. Before the commit, every KEEP table is counted
again and must have what it had; every FILTERED table must hold exactly
the rows its filter kept; every WIPE table must be empty. If any of that
is off, nothing is committed.

Ids carry on from where they were (TRUNCATE ... CONTINUE IDENTITY), every
table's: a user's, club's or event's id outlives the database row -- in a
signed-in device's token, a push topic its phone joined (club_{id}), a cached
page, a link in an old email -- and a new club given an old id would inherit
all of them. Not even the queues start again: the live API writes jobs and
search-outbox rows while this runs, and a sequence change is not undone by a
rollback, so restarting one could hand out an id already in use.

The bucket (S3) is left alone: pictures of what was wiped stay where they
are, unreferenced.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

KEEP, FILTERED, WIPE = "KEEP", "KEEP-FILTERED", "WIPE"

#: Who survives: the platform's admin accounts.
ADMINS = "SELECT id FROM users WHERE user_type = 'admin'"

#: What stays of each FILTERED table: a WHERE clause over its own rows.
FILTERS: dict[str, str] = {
    # The admins themselves, active or not, and everything they sign in with.
    "users": "user_type = 'admin'",
    "user_profiles": f"user_id IN ({ADMINS})",
    "user_interests": f"user_id IN ({ADMINS})",
    "user_device_tokens": f"user_id IN ({ADMINS})",
    # Their sessions, so a reset run from the admin console does not sign the admins out.
    "user_sessions": f"user_id IN ({ADMINS})",
    "notification_settings": f"user_id IN ({ADMINS})",
    "email_verification_codes": f"user_id IN ({ADMINS})",
    "password_reset_codes": f"user_id IN ({ADMINS})",
    "sign_in_attempts": f"user_id IN ({ADMINS})",
    # The admins' own trail: what they did stays on record.
    "admin_audit_logs": f"actor_id IN ({ADMINS})",
    # The event types the product ships (no author) and any an admin added; not the clubs' own.
    "event_categories": f"created_by_id IS NULL OR created_by_id IN ({ADMINS})",
}

#: Every table in the database, and what happens to it.
CLASSIFICATION: dict[str, str] = {
    # ── Kept whole: the platform's own reference data and machinery ──
    "alembic_version": KEEP,
    "plans": KEEP,
    "plan_features": KEEP,
    "plan_limits": KEEP,
    # The top-up packs on sale: catalogue, like the plans.
    "message_credit_packs": KEEP,
    "interest_categories": KEEP,
    "interests": KEEP,
    "badge_definitions": KEEP,
    # The avatar choices: the stock pictures an admin uploaded for everyone to pick from.
    "user_avatars": KEEP,
    # The platform's computed figures (one row the code expects, recomputed on its schedule).
    "discover_stats": KEEP,
    # When each sweep last ran, and the job runners' heartbeats.
    "scheduled_runs": KEEP,
    "job_workers": KEEP,
    # ── Kept in part: the admins and what they sign in with ──
    **{name: FILTERED for name in FILTERS},
    # ── Wiped: everything clubs, people and their events made ──
    "admin_notifications": WIPE,
    "after_commit_jobs": WIPE,
    # Colleges and companies: what people and clubs belonged to.
    "organizations": WIPE,
    # Addresses that bounced or complained: real people's, so they go with
    # everything else (the provider keeps its own list behind ours).
    "email_suppressions": WIPE,
    # Venue lookups for the events that are going.
    "geocode_cache": WIPE,
    "agenda_items": WIPE,
    "agenda_templates": WIPE,
    "archived_contacts": WIPE,
    "background_task_logs": WIPE,
    "badge_templates": WIPE,
    "certificate_templates": WIPE,
    # The digests of the certificate PDFs handed out: the registrations' own.
    "certificate_files": WIPE,
    "check_ins": WIPE,
    "closeout_reports": WIPE,
    "club_badges": WIPE,
    "club_domains": WIPE,
    "club_expenses": WIPE,
    "club_members": WIPE,
    "club_partners": WIPE,
    "club_payment_gateways": WIPE,
    # A club's own WhatsApp number: its keys, quality, templates and inbox
    # (people's messages). Every row is a club's; none is the platform's.
    "club_whatsapp_accounts": WIPE,
    "club_whatsapp_quality_log": WIPE,
    "club_whatsapp_templates": WIPE,
    "club_whatsapp_conversations": WIPE,
    "club_whatsapp_messages": WIPE,
    # Its inbox worked as a team: who opted out, notes, saved replies, settings, each person's mails.
    "club_whatsapp_optouts": WIPE,
    "club_whatsapp_notes": WIPE,
    "club_whatsapp_saved_replies": WIPE,
    "club_whatsapp_inbox_settings": WIPE,
    "club_whatsapp_person_prefs": WIPE,
    # Its reach: Meta's spend, consents to its updates, broadcasts and their people.
    "club_whatsapp_spend": WIPE,
    "club_whatsapp_consents": WIPE,
    "club_whatsapp_broadcasts": WIPE,
    "club_whatsapp_broadcast_recipients": WIPE,
    # A club's mailbox (app/api/club_mailbox): its address, threads, mail and
    # files' rows (the files themselves are in the bucket under mailbox/<club>/).
    "club_mailboxes": WIPE,
    "club_mailbox_addresses": WIPE,
    "club_mail_threads": WIPE,
    "club_mail_messages": WIPE,
    "club_mail_attachments": WIPE,
    # The club tools (app/api/club_ops): results, cancellations, refunds, the
    # guests told, and the dashboard's password reset codes.
    "competition_placings": WIPE,
    "event_cancellations": WIPE,
    "club_refunds": WIPE,
    # Ticketing (app/api/ticketing): an event's ticket types, a club's
    # discount codes, the waiting lists and their settings.
    "ticket_types": WIPE,
    "discount_codes": WIPE,
    "waitlist_entries": WIPE,
    "waitlist_settings": WIPE,
    "guest_notices": WIPE,
    "club_password_reset_codes": WIPE,
    "club_sites": WIPE,
    "club_socials": WIPE,
    "club_subscriptions": WIPE,
    "club_team_members": WIPE,
    "club_threads": WIPE,
    "club_users_link": WIPE,
    "clubs": WIPE,
    "clubs_interests_link": WIPE,
    "competition_judges": WIPE,
    "competition_mentors": WIPE,
    "competition_scores": WIPE,
    "competition_seekers": WIPE,
    "competition_slots": WIPE,
    "competition_submissions": WIPE,
    "competition_team_members": WIPE,
    "competition_teams": WIPE,
    "competition_tracks": WIPE,
    "competitions": WIPE,
    "content_reports": WIPE,
    "deliveries": WIPE,
    # Connected AI assistants (app/api/mcp, docs/MCP.md): each club's grants
    # and their codes and tokens (digests), the requests waiting for a
    # person's approval, and unsent announcement drafts -- all a club's or a
    # person's. And the assistants' own registrations (mcp_clients): written
    # by the apps themselves when they connected, not reference data, and of
    # no use once every grant that used them is gone (an app registers again
    # when it next connects). Nothing kept points at any of them.
    "mcp_clients": WIPE,
    "mcp_grants": WIPE,
    "mcp_auth_codes": WIPE,
    "mcp_tokens": WIPE,
    "mcp_pending_actions": WIPE,
    "announcement_drafts": WIPE,
    # AI assist (backend #91): what clubs used, clubs' own keys, cached feedback summaries.
    "ai_feedback_summaries": WIPE,
    "ai_usage": WIPE,
    "club_ai_keys": WIPE,
    "delivery_attempts": WIPE,
    "doors": WIPE,
    "email_templates": WIPE,
    "event_files": WIPE,
    "event_interests_link": WIPE,
    "event_page_views": WIPE,
    "event_ratings_link": WIPE,
    "event_registrations_link": WIPE,
    "event_speakers": WIPE,
    "event_store_items": WIPE,
    "event_store_offers": WIPE,
    "event_store_order_lines": WIPE,
    "event_store_orders": WIPE,
    "event_store_variants": WIPE,
    "event_team_members": WIPE,
    "events": WIPE,
    "feedback_responses": WIPE,
    "feedback_sends": WIPE,
    "feedback_surveys": WIPE,
    # People's IP addresses and where they placed them: personal, and only a cache.
    "ip_places": WIPE,
    # Queued work for rows that are going: a ticket mail for a wiped registration must not run.
    "jobs": WIPE,
    "kiosk_devices": WIPE,
    # The money ledger: append-only in normal life (its trigger is lifted for this one transaction).
    "ledger_entries": WIPE,
    "ledger_monthly": WIPE,
    "ledger_platform_monthly": WIPE,
    "live_poll_responses": WIPE,
    "live_polls": WIPE,
    "live_question_votes": WIPE,
    "live_questions": WIPE,
    "live_quiz_answers": WIPE,
    "live_quiz_players": WIPE,
    "live_quiz_questions": WIPE,
    "live_quizzes": WIPE,
    "live_updates": WIPE,
    "merch_collections": WIPE,
    "merch_items": WIPE,
    "merch_variants": WIPE,
    "message_deliveries": WIPE,
    "messages": WIPE,
    # What clubs sent, counted by the month, its statements and Enterprise clubs' own limits.
    "message_usage": WIPE,
    "message_usage_refs": WIPE,
    "message_statements": WIPE,
    "club_message_quotas": WIPE,
    # Credits clubs bought or were given, and their purchases.
    "message_credit_purchases": WIPE,
    "message_credit_lots": WIPE,
    # The club dashboard's bell.
    "club_alerts": WIPE,
    "notes": WIPE,
    "notification_broadcast_states": WIPE,
    "notification_broadcast_targets": WIPE,
    "notification_broadcasts": WIPE,
    "notification_logs": WIPE,
    "notifications": WIPE,
    "payment_logs": WIPE,
    "payment_orders": WIPE,
    "print_jobs": WIPE,
    "razorpay_webhook_logs": WIPE,
    "retention_jobs": WIPE,
    "search_outbox": WIPE,
    "self_check_in_sessions": WIPE,
    "subscription_charges": WIPE,
    "subscription_emails": WIPE,
    "thread_comments": WIPE,
    "thread_likes": WIPE,
    "thread_poll_options": WIPE,
    "thread_views": WIPE,
    "thread_votes": WIPE,
    "ticket_templates": WIPE,
    "tracking_clicks": WIPE,
    "tracking_links": WIPE,
    "user_badges": WIPE,
    "user_blocks": WIPE,
    "volunteers": WIPE,
}

#: Triggers that refuse a removal in normal life, lifted for this transaction
#: only and put back before the checks (which make sure they are back).
GUARD_TRIGGERS: dict[str, tuple[str, ...]] = {
    "ledger_entries": ("trg_ledger_entries_no_truncate", "trg_ledger_entries_append_only"),
}

#: Wiped tables that triggers on other tables write to while the rest is
#: deleted (the search outbox hears about every club row that goes): emptied
#: once more at the end. The search index is rebuilt after a reset anyway
#: (scripts/search_reindex.py, in the runbook).
TRIGGER_FED = ("search_outbox",)

#: Only one reset or seed at a time.
LOCK_KEY = 0x6D6F615F64656D6F  # "moa_demo"


class Refused(Exception):
    """The reset will not run; the message says why."""


@dataclass
class ForeignKey:
    name: str
    child: str
    column: str
    parent: str
    parent_column: str
    not_null: bool


@dataclass
class Plan:
    classes: dict[str, str]
    tables: list[str]
    missing: list[str]
    counts: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)
    detach: list[tuple[ForeignKey, int]] = field(default_factory=list)
    truncate: list[str] = field(default_factory=list)
    delete_order: list[str] = field(default_factory=list)
    #: The public pages that go: event slugs and ids, club slugs.
    pages: tuple[list[str], list[str]] = (list(), list())

    def removes(self, table: str) -> int:
        return self.counts[table] - self.kept.get(table, 0)


def classification() -> dict[str, str]:
    return dict(CLASSIFICATION)


def keep_clause(table: str, classes: dict[str, str]) -> str:
    """SQL: the rows of `table` that stay."""
    kind = classes[table]
    return "TRUE" if kind == KEEP else "FALSE" if kind == WIPE else f"({FILTERS[table]})"


async def database_tables(conn) -> list[str]:
    """Every table, view and materialised view in the public schema."""
    rows = await conn.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') ORDER BY c.relname"
    ))
    return [r[0] for r in rows]


async def foreign_keys(conn) -> list[ForeignKey]:
    rows = await conn.execute(text(
        """
        SELECT con.conname, ch.relname, pa.relname, con.conkey, con.confkey,
               array_agg(a.attname ORDER BY k.ord), array_agg(a.attnotnull ORDER BY k.ord),
               array_agg(pa_a.attname ORDER BY k.ord)
        FROM pg_constraint con
        JOIN pg_class ch ON ch.oid = con.conrelid
        JOIN pg_class pa ON pa.oid = con.confrelid
        JOIN pg_namespace n ON n.oid = ch.relnamespace
        CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, pattnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        JOIN pg_attribute pa_a ON pa_a.attrelid = con.confrelid AND pa_a.attnum = k.pattnum
        WHERE con.contype = 'f' AND n.nspname = 'public'
        GROUP BY con.conname, ch.relname, pa.relname, con.conkey, con.confkey
        """
    ))
    out = []
    for name, child, parent, _ck, _pk, cols, not_null, pcols in rows:
        if len(cols) != 1:
            raise Refused(f"foreign key {name} on {child} spans {len(cols)} columns; the reset only plans single-column keys")
        out.append(ForeignKey(name, child, cols[0], parent, pcols[0], bool(not_null[0])))
    return out


def order_children_first(tables: set[str], fks: list[ForeignKey]) -> list[str]:
    """`tables` in an order that deletes every child before its parent."""
    parents: dict[str, set[str]] = defaultdict(set)
    for fk in fks:
        if fk.child in tables and fk.parent in tables and fk.child != fk.parent:
            parents[fk.child].add(fk.parent)
    children: dict[str, set[str]] = defaultdict(set)
    for child, ps in parents.items():
        for p_ in ps:
            children[p_].add(child)
    out: list[str] = []
    done: set[str] = set()
    pending = set(tables)
    while pending:
        ready = sorted(t for t in pending if children[t] <= done)
        if not ready:
            raise Refused(f"the tables {sorted(pending)} point at each other; the reset cannot order their deletes")
        for t in ready:
            out.append(t)
            done.add(t)
            pending.discard(t)
    return out


async def make_plan(conn) -> Plan:
    """Read the database and decide every step; changes nothing."""
    classes = classification()
    tables = await database_tables(conn)
    unknown = [t for t in tables if t not in classes]
    if unknown:
        raise Refused(
            "these tables are not classified in scripts/demo/reset.py -- decide KEEP, KEEP-FILTERED or WIPE "
            f"for each before running: {', '.join(unknown)}"
        )
    missing = sorted(t for t in classes if t not in tables)
    classes = {t: c for t, c in classes.items() if t in tables}
    plan = Plan(classes=classes, tables=tables, missing=missing)

    for t in tables:
        plan.counts[t] = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}"')))
        if classes[t] == FILTERED:
            plan.kept[t] = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}" WHERE {FILTERS[t]}')))
        elif classes[t] == KEEP:
            plan.kept[t] = plan.counts[t]

    fks = [fk for fk in await foreign_keys(conn) if fk.child in classes and fk.parent in classes]
    kept_side = {t for t, c in classes.items() if c in (KEEP, FILTERED)}
    wiped = {t for t, c in classes.items() if c == WIPE}

    # Kept rows pointing at rows that go: emptied first, or refused.
    for fk in fks:
        if fk.child not in kept_side or classes[fk.parent] == KEEP:
            continue
        going = f'SELECT "{fk.parent_column}" FROM "{fk.parent}" WHERE ({keep_clause(fk.parent, classes)}) IS NOT TRUE'
        n = int(await conn.scalar(text(
            f'SELECT count(*) FROM "{fk.child}" WHERE {keep_clause(fk.child, classes)} '
            f'AND "{fk.column}" IS NOT NULL AND "{fk.column}" IN ({going})'
        )))
        if n and fk.not_null:
            raise Refused(f"{n} kept row(s) of {fk.child} point at {fk.parent} rows that go, through {fk.column}, which cannot be emptied")
        if n:
            plan.detach.append((fk, n))

    # TRUNCATE refuses a table that a table outside the statement points at,
    # whatever the rows: those are deleted instead, and so is every wiped
    # table they in turn point at.
    by_delete = {fk.parent for fk in fks if fk.parent in wiped and fk.child not in wiped}
    grew = True
    while grew:
        grew = False
        for fk in fks:
            if fk.child in by_delete and fk.parent in wiped and fk.parent not in by_delete:
                by_delete.add(fk.parent)
                grew = True
    plan.truncate = sorted(wiped - by_delete)
    if "events" in tables and "clubs" in tables:
        events = [str(x) for r in (await conn.execute(text("SELECT id, slug FROM events"))).all() for x in r]
        clubs = [r[0] for r in (await conn.execute(text("SELECT slug FROM clubs"))).all()]
        plan.pages = (events, clubs)
    plan.delete_order = order_children_first(by_delete | {t for t, c in classes.items() if c == FILTERED}, fks)
    return plan


async def execute_plan(conn, plan: Plan) -> None:
    """Carry the plan out on `conn`, inside the caller's transaction, and
    check the result; raises (and the caller rolls back) if anything is off."""
    await conn.execute(text("SET LOCAL lock_timeout = '15s'"))
    await conn.execute(text("SET LOCAL statement_timeout = '15min'"))
    classes = plan.classes

    for fk, _n in plan.detach:
        going = f'SELECT "{fk.parent_column}" FROM "{fk.parent}" WHERE ({keep_clause(fk.parent, classes)}) IS NOT TRUE'
        await conn.execute(text(
            f'UPDATE "{fk.child}" SET "{fk.column}" = NULL WHERE {keep_clause(fk.child, classes)} '
            f'AND "{fk.column}" IS NOT NULL AND "{fk.column}" IN ({going})'
        ))

    guarded = [t for t in GUARD_TRIGGERS if classes.get(t) == WIPE]
    for t in guarded:
        for trigger in await _existing_triggers(conn, t, GUARD_TRIGGERS[t]):
            await conn.execute(text(f'ALTER TABLE "{t}" DISABLE TRIGGER "{trigger}"'))

    if plan.truncate:
        names = ", ".join(f'"{t}"' for t in plan.truncate)
        await conn.execute(text(f"TRUNCATE {names} CONTINUE IDENTITY"))
    for t in plan.delete_order:
        if classes[t] == WIPE:
            await conn.execute(text(f'DELETE FROM "{t}"'))
        else:
            # IS NOT TRUE, not NOT: a row the filter can't decide (a sign-in
            # attempt for an unknown address has no user_id, so `NULL IN (...)`
            # is NULL) goes too. NOT (NULL) is NULL and would leave it behind,
            # neither kept nor removed -- the checks below then refuse.
            await conn.execute(text(f'DELETE FROM "{t}" WHERE ({FILTERS[t]}) IS NOT TRUE'))

    for t in TRIGGER_FED:
        if classes.get(t) == WIPE:
            await conn.execute(text(f'DELETE FROM "{t}"'))

    for t in guarded:
        for trigger in await _existing_triggers(conn, t, GUARD_TRIGGERS[t]):
            await conn.execute(text(f'ALTER TABLE "{t}" ENABLE TRIGGER "{trigger}"'))

    await verify(conn, plan)


async def _existing_triggers(conn, table: str, names: tuple[str, ...]) -> list[str]:
    rows = await conn.execute(text(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"
    ), {"t": table})
    present = {r[0] for r in rows}
    return [n for n in names if n in present]


async def verify(conn, plan: Plan) -> None:
    """Every KEEP table as it was, every FILTERED table down to exactly its
    kept rows, every WIPE table empty, the guard triggers back on."""
    problems = []
    for t, kind in plan.classes.items():
        n = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}"')))
        if kind == KEEP and n != plan.counts[t]:
            problems.append(f"{t}: kept table had {plan.counts[t]} rows, now {n}")
        elif kind == FILTERED:
            still = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}" WHERE {FILTERS[t]}')))
            if still != plan.kept[t] or n != plan.kept[t]:
                problems.append(f"{t}: should hold its {plan.kept[t]} kept rows, holds {n} ({still} kept)")
        elif kind == WIPE and n:
            problems.append(f"{t}: should be empty, holds {n}")
    for t, names in GUARD_TRIGGERS.items():
        if t in plan.classes:
            rows = await conn.execute(text(
                "SELECT tgname, CAST(tgenabled AS text) FROM pg_trigger WHERE tgrelid = CAST(:t AS regclass) AND NOT tgisinternal"
            ), {"t": t})
            for name, enabled in rows:
                if name in names and enabled != "O":
                    problems.append(f"{t}: trigger {name} was not switched back on")
    if problems:
        raise Refused("the checks after the reset failed, so nothing was committed:\n  " + "\n  ".join(problems))


async def admins_left(conn) -> int:
    """Admin accounts that can still sign in once the reset is done."""
    return int(await conn.scalar(text(
        "SELECT count(*) FROM users WHERE user_type = 'admin' AND NOT is_deleted AND is_active "
        "AND (password IS NOT NULL OR provider IN ('google', 'apple'))"
    )))


def guard_problems(*, execute: bool, confirm: str | None, database: str, env: dict) -> list[str]:
    """Why an execute may not go ahead; empty when it may."""
    if not execute:
        return []
    problems = []
    if confirm != database:
        problems.append(f"--confirm must name the database exactly: it is connected to {database!r}" + (f", not {confirm!r}" if confirm else ""))
    if env.get("APP_DEMO_RESET_ALLOWED") != "1":
        problems.append("APP_DEMO_RESET_ALLOWED=1 is not set in the environment")
    return problems


def render(plan: Plan, *, execute: bool) -> str:
    lines = []
    width = max(len(t) for t in plan.tables)
    lines.append(f"{'table'.ljust(width)}  {'class'.ljust(13)}  {'rows':>9}  {'removed' if execute else 'would go':>9}")
    lines.append("-" * (width + 38))
    order = {KEEP: 0, FILTERED: 1, WIPE: 2}
    for t in sorted(plan.tables, key=lambda t: (order[plan.classes[t]], t)):
        lines.append(f"{t.ljust(width)}  {plan.classes[t].ljust(13)}  {plan.counts[t]:>9}  {plan.removes(t):>9}")
    total = sum(plan.removes(t) for t in plan.tables)
    lines.append("-" * (width + 38))
    lines.append(f"{len(plan.tables)} tables: {sum(1 for c in plan.classes.values() if c == KEEP)} kept, "
                 f"{sum(1 for c in plan.classes.values() if c == FILTERED)} kept in part, "
                 f"{sum(1 for c in plan.classes.values() if c == WIPE)} wiped; {total} rows {'removed' if execute else 'would go'}.")
    if plan.detach:
        for fk, n in plan.detach:
            lines.append(f"  emptied first: {n} kept row(s) of {fk.child}.{fk.column} pointing at {fk.parent}")
    lines.append(f"  by TRUNCATE: {len(plan.truncate)} tables; by DELETE, children first: {', '.join(plan.delete_order)}")
    if plan.missing:
        lines.append(f"  classified but not in this database (skipped): {', '.join(plan.missing)}")
    return "\n".join(lines)


async def run(*, execute: bool, confirm: str | None, env: dict | None = None, out=print) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import settings

    env = os.environ if env is None else env
    engine = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            database = await conn.scalar(text("SELECT current_database()"))
            await conn.rollback()
            problems = guard_problems(execute=execute, confirm=confirm, database=database, env=env)
            if problems:
                out("Refused: " + "; ".join(problems) + ".")
                return 2
            async with conn.begin() as trans:
                if not execute:
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
                try:
                    plan = await make_plan(conn)
                    out(f"Database {database!r}{' -- DRY RUN, nothing will change' if not execute else ''}\n")
                    out(render(plan, execute=execute))
                    locked_out = await admins_left(conn) == 0
                    if not execute:
                        await trans.rollback()
                        if locked_out:
                            out("\nAn execute would be refused: no admin account that can sign in would survive.")
                        out("\nDry run: nothing changed. To execute: APP_DEMO_RESET_ALLOWED=1 python -m scripts.demo.reset "
                            f"--execute --confirm {database}")
                        return 0
                    if locked_out:
                        raise Refused("no admin account that can sign in would survive; the platform would be locked out")
                    await execute_plan(conn, plan)
                except Refused as e:
                    await trans.rollback()
                    out(f"Refused: {e}")
                    return 2
            out(f"\nDone: {sum(plan.removes(t) for t in plan.tables)} rows removed in one transaction, checked and committed. "
                f"{await admins_left(conn)} admin account(s) kept.")
        revalidate(*plan.pages)
        return 0
    finally:
        await engine.dispose()


def revalidate(events: list[str], clubs: list[str]) -> None:
    """Tell the public site its pages for what went are gone, and its topic
    and front pages changed (when it is set up to be told: APP_SITE_REVALIDATE_SECRET)."""
    from app.core import site_revalidate

    if site_revalidate.enabled():
        site_revalidate._queue(None, frozenset(events), frozenset(clubs), True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo.reset", description=__doc__.split("\n\n")[0])
    parser.add_argument("--execute", action="store_true", help="really do it (the default is a dry run)")
    parser.add_argument("--confirm", metavar="DATABASE", help="the exact name of the database, to execute")
    args = parser.parse_args(argv)
    return asyncio.run(run(execute=args.execute, confirm=args.confirm))


if __name__ == "__main__":
    sys.exit(main())
