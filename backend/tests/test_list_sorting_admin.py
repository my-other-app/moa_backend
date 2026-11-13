"""Sorting the admin portal's lists (leads, the audit log, subscriptions,
disputes, payouts) and the club's message and delivery logs: each takes
`sort` + `order`, refuses an unknown column, and keeps its own order unasked."""
import pytest
from sqlalchemy.dialects import postgresql

from tests.conftest import auth_header


def _sql(parts) -> str:
    return ", ".join(str(p.compile(dialect=postgresql.dialect())) for p in parts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/leads",
        "/api/v1/admin/audit-log",
        "/api/v1/admin/subscriptions",
        "/api/v1/admin/disputes",
        "/api/v1/admin/payouts",
    ],
)
async def test_admin_lists_take_a_sort_and_refuse_an_unknown_one(client, make_admin, mint_token, path):
    h = auth_header(await mint_token(await make_admin()))
    good = {"/api/v1/admin/leads": "name", "/api/v1/admin/audit-log": "action", "/api/v1/admin/subscriptions": "club",
            "/api/v1/admin/disputes": "amount", "/api/v1/admin/payouts": "club"}[path]
    ok = await client.get(path, params={"sort": good, "order": "asc"}, headers=h)
    assert ok.status_code in (200, 403), ok.text  # 403: a team-only list (leads) for an admin outside the teams
    assert (await client.get(path, params={"sort": "nonsense"}, headers=h)).status_code == 422
    assert (await client.get(path, params={"order": "sideways"}, headers=h)).status_code == 422


# Last in the file: a test that opens no database first in a run sets the
# suite's loop up apart from the session's engine (tests/conftest.py).
def test_the_order_builders():
    from app.api.admin_ops.disputes import _dispute_order
    from app.api.audit.service import _audit_order
    from app.api.leads.service import lead_order
    from app.api.subscriptions.router import _subscription_order

    # Leads: unasked is the view's own (None); a column with an id tie-break.
    assert lead_order(None, None) is None
    assert "lower(leads.name) ASC" in _sql(lead_order("name", None))
    assert "leads.value_inr DESC" in _sql(lead_order("value", None))
    assert "leads.updated_at ASC" in _sql(lead_order("updated", "asc"))
    # Audit log: newest first unasked, oldest first asked, or by action.
    assert "created_at DESC" in _sql(_audit_order(None, None))
    assert "created_at ASC" in _sql(_audit_order("date", "asc"))
    assert "admin_audit_logs.action ASC" in _sql(_audit_order("action", None))
    # Subscriptions: highest value first unasked; by club name; renews soonest.
    assert "mrr_paise DESC" in _sql(_subscription_order(None, None))
    assert "lower(clubs.name)" in _sql(_subscription_order("club", "asc"))
    assert "current_period_end ASC" in _sql(_subscription_order("renews", "asc"))
    # Disputes: open ones by deadline unasked; amount largest first.
    assert "respond_by ASC" in _sql(_dispute_order(None, None, "open"))
    assert "updated_at DESC" in _sql(_dispute_order(None, None, "all"))
    assert "payment_disputes.amount_paise DESC" in _sql(_dispute_order("amount", None, "open"))
