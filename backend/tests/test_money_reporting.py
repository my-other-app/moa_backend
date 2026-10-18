"""The rest of the money audit: finance's refunds joined rather than looked up
per row, a statement that streams and never sends a parameter per
registration, the swag counter that cannot lose a tap, club gateways' clients
kept and their changes serialised, and the indexes the payment paths read."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text, update

from app.api.closeout.models import EventStatus
from tests.conftest import auth_header
from tests.test_finance import _paid, _refund

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def statements(session):
    """Every statement sent on the test's connection, with its parameters."""
    seen: list[tuple] = []

    def before(_conn, _cursor, statement, parameters, *_args):
        seen.append((statement, parameters))

    conn = await session.connection()
    sa_event.listen(conn.sync_connection, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn.sync_connection, "before_cursor_execute", before)


async def _event_with(session, make_club, make_event, make_user, make_registration, n, *, tag):
    owner, club = await make_club(name=f"Statement Club {tag}", email=f"statement-{tag}@example.com")
    event = await make_event(club=club, created_by=owner, name=f"Statement Night {tag}", event_datetime=NOW - timedelta(days=2), has_fee=True, reg_fee=300, lifecycle=EventStatus.closed)
    regs = []
    for i in range(n):
        user = await make_user(email=f"stmt-{tag}-{i}@example.com")
        regs.append(await _paid(make_registration, event, user, 300))
    await session.commit()
    return owner, event, regs


async def test_the_event_statement_streams_without_a_parameter_per_registration(
    client, session, statements, make_club, make_event, make_user, make_registration, mint_token
):
    """The refunded receipts were looked up with IN (one parameter per
    registration): past asyncpg's 32,767 parameters the statement failed."""
    seen = statements
    widest = {}
    for n, tag in ((3, "small"), (9, "big")):
        owner, event, regs = await _event_with(session, make_club, make_event, make_user, make_registration, n, tag=tag)
        await _refund(session, regs[0], 300)
        await session.commit()
        token = await mint_token(owner)
        seen.clear()
        r = await client.get(f"/api/v1/events/{event.id}/finance/statement.csv", headers=auth_header(token))
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        body = r.text
        assert body.startswith("﻿Statement")
        lines = [line for line in body.splitlines() if line.count(",") >= 6 and "@example.com" in line]
        assert len(lines) == n
        assert sum(1 for line in lines if line.endswith(",refunded")) == 1
        assert sum(1 for line in lines if line.endswith(",paid")) == n - 1
        widest[n] = max(len(p or ()) if isinstance(p, (tuple, list)) else len(p or {}) for _, p in seen)
    assert widest[3] == widest[9]


async def test_outstanding_leaves_out_refunded_registrations(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, event, regs = await _event_with(session, make_club, make_event, make_user, make_registration, 2, tag="owed")
    unpaid_user = await make_user(email="owed-unpaid@example.com")
    await _paid(make_registration, event, unpaid_user, 300, is_paid=False)
    await _refund(session, regs[0], 300)  # unpaid now, but refunded: owes nothing
    await session.commit()
    fin = (await client.get(f"/api/v1/events/{event.id}/finance", headers=auth_header(await mint_token(owner)))).json()
    assert (fin["figures"]["outstanding_count"], fin["figures"]["outstanding"]) == (1, 300.0)
    assert (fin["figures"]["refund_count"], fin["figures"]["refunds"]) == (1, 300.0)


async def test_a_swag_tap_is_never_lost_to_another_at_the_same_moment(client, session, make_club, make_event, mint_token, monkeypatch):
    """The manual counter was read, then written back plus one: two staff
    tapping at once counted one piece."""
    from app.api.merch import service as merch
    from app.api.merch.models import MerchItems

    owner, club = await make_club(name="Swag Club", email="swag-race@example.com")
    event = await make_event(club=club, created_by=owner, name="Swag Night", event_datetime=NOW + timedelta(days=2))
    await session.commit()
    h = auth_header(await mint_token(owner))
    item = (await client.post(f"/api/v1/events/{event.id}/merch", json={"name": "Sticker", "stock": 2}, headers=h)).json()

    real = merch._item
    raced = {"done": False}

    async def read_then_another_tap(session_, event_, item_id):
        m = await real(session_, event_, item_id)
        if not raced["done"]:
            # The other member of staff's tap commits between this read and its write.
            raced["done"] = True
            await session_.execute(update(MerchItems.__table__).where(MerchItems.__table__.c.id == m.id).values(manual_handed_out=MerchItems.__table__.c.manual_handed_out + 1))
        return m

    monkeypatch.setattr(merch, "_item", read_then_another_tap)
    url = f"/api/v1/events/{event.id}/merch/{item['id']}/handout"
    r = await client.post(url, headers=h)
    assert r.status_code == 200 and r.json()["handed_out"] == 2
    # Two pieces of two: the next tap is refused, not counted past the stock.
    assert (await client.post(url, headers=h)).status_code == 422


async def test_a_club_gateways_client_is_kept_and_its_changes_take_the_clubs_lock(client, session, make_club, monkeypatch, statements):
    from cryptography.fernet import Fernet

    from app.api.payment_gateways import crypto
    from app.api.payment_gateways import service as gateways
    from app.api.payment_gateways.models import ClubPaymentGateways
    from app.config import settings

    monkeypatch.setattr(settings, "PAYMENT_KEYS_SECRET", Fernet.generate_key().decode())
    built = []
    monkeypatch.setattr(gateways, "make_client", lambda key_id, secret: built.append(key_id) or object())
    owner, club = await make_club(name="Gateway Club", email="gateway-lru@example.com")
    gateway = ClubPaymentGateways(
        club_id=club.id, key_id="rzp_live_LruClub111111", key_secret_enc=crypto.encrypt("secretsecret"),
        webhook_secret_enc=crypto.encrypt("hook"), webhook_token="lru-token-0123456789abcdef", status="active",
    )
    session.add(gateway)
    await session.commit()
    first = gateways.client_for(gateway)
    gateway.last_webhook_at = NOW  # a webhook landing changes nothing that matters
    await session.commit()
    assert gateways.client_for(gateway) is first and built == ["rzp_live_LruClub111111"]
    gateway.key_secret_enc = crypto.encrypt("anothersecret")
    assert gateways.client_for(gateway) is not first

    statements.clear()
    await gateways.disconnect(session, club, owner)
    assert any("pg_advisory_xact_lock" in s for s, _ in statements)


async def test_the_payment_paths_indexes_exist(session):
    names = set((await session.scalars(text(
        "SELECT indexname FROM pg_indexes WHERE tablename IN "
        "('event_registrations_link', 'payment_logs', 'payment_orders', 'notifications', 'event_store_orders')"
    ))).all())
    for name in (
        "ix_registrations_payment_receipt", "ix_payment_logs_order_id", "ix_payment_logs_refunded_order",
        "ix_payment_logs_refund_due", "ix_payment_orders_open_created", "ix_payment_orders_paid_updated",
        "ix_notifications_event_id", "ix_event_store_orders_held_until",
    ):
        assert name in names, name
