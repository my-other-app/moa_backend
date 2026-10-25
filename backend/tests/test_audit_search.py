"""Searching the admin audit log (GET /admin/audit-log?q=, app.api.audit).

The Activity page drew a search box the endpoint ignored: typing put ?q= in
the address and left the same rows on screen. `q` now finds rows with every
one of its words, in any case, in who acted (name or email), the summary,
the record ("club #12") or the reason given, paged and counted like the
rest, on a trigram index over that one expression.
"""
import json

import pytest
from sqlalchemy import select, text

from app.api.audit import service as audit
from app.api.audit.models import AdminAuditLogs, search_sql
from tests.conftest import auth_header

API = "/api/v1/admin/audit-log"


@pytest.fixture
async def log(session, make_admin, make_user, mint_token):
    """Three admins' worth of history."""
    owner = await make_admin(email="priya.owner@example.com")
    owner.full_name = "Priya Raman"
    support = await make_admin(email="desk@example.com")
    support.full_name = "Support Desk"
    gone = await make_admin(email="former@example.com")
    gone.full_name = "Former Admin"
    await session.flush()
    rows = [
        (owner, "club.approve", "club", 12, "Approved club “Robotics”", None),
        (owner, "event.delete", "event", 7, "Deleted event “Hack Night”", None),
        (support, "user.suspend", "user", 412, "Suspended ann@example.com", None),
        (support, "user.impersonate", "user", 77, "Started a session as robotics@example.com", {"reason": "Ticket 4512: cannot publish"}),
        (gone, "badge.award", "badge", 3, "Awarded 100% Club", None),
    ]
    for actor, action, kind, entity, summary, details in rows:
        await audit.record(session, actor=actor, action=action, entity_type=kind, entity_id=entity, summary=summary, details=details)
    await session.commit()
    return {"owner": owner, "support": support, "gone": gone, "h": auth_header(await mint_token(owner))}


async def _search(client, log, q, **params):
    r = await client.get(API, params={"q": q, **params} if q is not None else params, headers=log["h"])
    assert r.status_code == 200, r.text
    return r.json()


def _summaries(page):
    return [row["summary"] for row in page["items"]]


@pytest.mark.parametrize(
    "q, found",
    [
        # Who, by name or address, in any case.
        ("priya", ["Deleted event “Hack Night”", "Approved club “Robotics”"]),
        ("PRIYA.OWNER@", ["Deleted event “Hack Night”", "Approved club “Robotics”"]),
        ("support desk", ["Started a session as robotics@example.com", "Suspended ann@example.com"]),
        # What: the summary's words.
        ("hack night", ["Deleted event “Hack Night”"]),
        ("robotics", ["Started a session as robotics@example.com", "Approved club “Robotics”"]),
        # Which record, as the page shows it.
        ("club #12", ["Approved club “Robotics”"]),
        ("user #412", ["Suspended ann@example.com"]),
        # Why: the reason given for a session as someone else.
        ("ticket 4512", ["Started a session as robotics@example.com"]),
        # Every word, anywhere: in any order, across fields.
        ("robotics priya", ["Approved club “Robotics”"]),
        ("desk 4512 robotics", ["Started a session as robotics@example.com"]),
        ("nothing like this", []),
    ],
)
async def test_the_log_is_found_by_who_what_which_and_why(client, log, q, found):
    page = await _search(client, log, q)
    assert _summaries(page) == found
    assert page["total"] == len(found)
    assert page["search"] == " ".join(q.split())


async def test_without_a_search_everything_comes_and_search_is_null(client, log):
    page = await _search(client, log, None)
    assert page["total"] == 5 and page["search"] is None
    blank = await _search(client, log, "   ")
    assert blank["total"] == 5 and blank["search"] is None


async def test_a_search_goes_with_the_filters_and_pages_counting_all_it_found(client, log):
    filtered = await _search(client, log, "robotics", action="club.approve")
    assert _summaries(filtered) == ["Approved club “Robotics”"]
    first = await _search(client, log, "example.com", limit=2)
    assert first["total"] == 5 and len(first["items"]) == 2 and first["next"]
    rest = await _search(client, log, "example.com", limit=2, offset=4)
    assert rest["total"] == 5 and len(rest["items"]) == 1 and rest["next"] is None


async def test_percent_and_underscore_are_only_themselves(client, log):
    assert _summaries(await _search(client, log, "100%")) == ["Awarded 100% Club"]
    # Not "anything", as % would be to LIKE; nor any one character, as _ would.
    assert _summaries(await _search(client, log, "%")) == ["Awarded 100% Club"]
    assert _summaries(await _search(client, log, "_")) == []


async def test_who_acted_is_kept_on_the_row_and_found_after_the_account_is_gone(client, session, log):
    row = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "badge.award"))
    assert row.actor_name == "Former Admin"
    # The account goes; the row keeps who it was.
    await session.execute(text("UPDATE admin_audit_logs SET actor_id = NULL WHERE id = :id"), {"id": row.id})
    await session.commit()
    page = await _search(client, log, "former admin")
    assert _summaries(page) == ["Awarded 100% Club"]
    assert page["items"][0]["actor"]["full_name"] == "Former Admin"
    assert page["items"][0]["actor"]["email"] == "former@example.com"


async def test_an_overlong_search_is_refused_and_too_many_words_are_cut(client, log):
    r = await client.get(API, params={"q": "x" * 201}, headers=log["h"])
    assert r.status_code == 422
    page = await _search(client, log, "priya " * 12)
    assert page["search"] == " ".join(["priya"] * 8)


async def test_the_search_reads_the_trigram_index(session, log):
    """The expression the query filters on is the one the index was built
    on, so the planner can look a word up in ix_audit_search_trgm. With
    sequential scans and plain index walks priced out, that lookup is the
    only way left to it: a plan without the index here means the two
    expressions differ (on a table of five rows the planner would otherwise
    be free to walk any index, so this does not ask it what it prefers)."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    haystack = search_sql("admin_audit_logs")
    raw = (await session.execute(text(f"EXPLAIN (FORMAT JSON) SELECT id FROM admin_audit_logs WHERE {haystack} ILIKE '%robotics%'"))).scalar()
    plan = json.dumps(raw if isinstance(raw, list) else json.loads(raw))
    assert "ix_audit_search_trgm" in plan, plan
    # And the API's own statement is that expression.
    from sqlalchemy import literal_column

    compiled = str(select(AdminAuditLogs.id).where(literal_column(haystack).ilike("%x%")).compile())
    assert search_sql("admin_audit_logs") in compiled
