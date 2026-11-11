"""The desk's reference rows: the two teams, the seven topics, the lost
reasons. The migration (support_desk) inserts the same rows; `ensure` puts
back any that are missing (a database made by create_all, the test suite's),
never touching one an owner has changed."""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert

TEAMS = (("support", "Support"), ("sales", "Sales"))

#: (key, label, default priority): payments and reports of abuse are high.
TOPICS = (
    ("account", "Account and sign-in", "normal"),
    ("tickets", "Tickets and registration", "normal"),
    ("payments", "Payments and refunds", "high"),
    ("dashboard", "Club dashboard", "normal"),
    ("report", "Report a club or event", "high"),
    ("bug", "Bug", "normal"),
    ("other", "Other", "normal"),
)

LOST_REASONS = (
    ("no_budget", "No budget"),
    ("timing", "Timing"),
    ("chose_another", "Chose another"),
    ("no_reply", "No reply"),
    ("not_a_fit", "Not a fit"),
)

_ensured = False


async def ensure(session, *, force: bool = False) -> None:
    """Insert the reference rows that are not there. Once a process (they
    are never deleted), unless `force`."""
    global _ensured
    if _ensured and not force:
        return
    from app.api.leads.models import LeadLostReasons
    from app.api.support.models import AdminTeams, SupportTopics

    await session.execute(
        pg_insert(AdminTeams.__table__).values([{"key": k, "label": label} for k, label in TEAMS]).on_conflict_do_nothing()
    )
    await session.execute(
        pg_insert(SupportTopics.__table__).values([
            {"key": k, "label": label, "default_priority": p, "position": n} for n, (k, label, p) in enumerate(TOPICS)
        ]).on_conflict_do_nothing()
    )
    await session.execute(
        pg_insert(LeadLostReasons.__table__).values([
            {"key": k, "label": label, "position": n} for n, (k, label) in enumerate(LOST_REASONS)
        ]).on_conflict_do_nothing()
    )
    _ensured = True


def reset_for_tests() -> None:
    global _ensured
    _ensured = False
