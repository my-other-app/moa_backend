"""Club payouts (app/api/admin_ops/payouts.py): what MyOtherApp owes each
club for sales on its own Razorpay account -- gross, fees, refunds,
chargebacks, net -- the payouts recorded against it, the statement file,
and Razorpay's fees fetched (never from the real Razorpay)."""
import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.admin_ops import payouts
from app.api.admin_ops.models import ClubPayouts, PaymentDisputes, PaymentFees, PaymentRefunds
from app.api.audit.models import AdminAuditLogs
from app.api.payment_gateways.models import ClubPaymentGateways
from app.api.payments import service as payments_service
from app.api.payments.models import PaymentStatus
from tests.admin_ops_support import captured_payment, role_headers

API = "/api/v1"


@pytest.fixture
async def shop(session, make_user, make_club, make_event, make_registration):
    """A club with three platform payments -- ₹500 with its fee known, ₹500
    refunded in full, ₹300 with ₹100 given back and a lost chargeback of
    ₹50 -- and one ₹999 payment on its own Razorpay account, which is the
    club's already."""
    club_user, club = await make_club(name="Payout Club", email=f"payout-{uuid.uuid4().hex[:6]}@example.com")
    kw = dict(make_user=make_user, make_event=make_event, make_registration=make_registration, club=club, club_user=club_user)
    _, _, _, known = await captured_payment(session, amount=500, **kw)
    session.add(PaymentFees(razorpay_payment_id=known.razorpay_payment_id, fee_paise=1180, tax_paise=180))
    _, _, _, refunded = await captured_payment(session, amount=500, status=PaymentStatus.refunded, **kw)
    _, reg, _, part = await captured_payment(session, amount=300, **kw)
    session.add(PaymentRefunds(payment_log_id=part.id, razorpay_payment_id=part.razorpay_payment_id, razorpay_refund_id="rfnd_p1",
                               kind="ticket", amount=Decimal("100"), reason="late start"))
    session.add(PaymentDisputes(razorpay_dispute_id="disp_1", razorpay_payment_id=part.razorpay_payment_id, amount_paise=5000,
                                status="lost", club_id=club.id, notes=[]))
    gateway = ClubPaymentGateways(club_id=club.id, key_id="rzp_test_club", webhook_secret_enc="x", webhook_token=uuid.uuid4().hex)
    session.add(gateway)
    await session.flush()
    await captured_payment(session, amount=999, gateway_id=gateway.id, **kw)
    await session.commit()
    return {"club": club, "known": known, "refunded": refunded, "part": part}


async def test_the_overview_shows_what_is_owed_from_platform_payments_only(client, make_admin, mint_token, shop):
    h = await role_headers(make_admin, mint_token)
    r = await client.get(f"{API}/admin/payouts", headers=h["viewer"])
    assert r.status_code == 200, r.text
    row = next(i for i in r.json()["items"] if i["club"]["id"] == shop["club"].id)
    # 500 + 500 + 300; the club's own account's 999 is not ours.
    assert row["gross"] == 1300.0 and row["payments"] == 3
    # 11.80 known; 2.36% estimated on the other two: 11.80 + 7.08.
    assert row["fees"] == pytest.approx(11.80 + 11.80 + 7.08)
    assert row["fees_estimated"] == 2
    assert row["refunds"] == 600.0 and row["chargebacks"] == 50.0
    assert row["net"] == pytest.approx(1300 - 30.68 - 600 - 50)
    assert row["outstanding"] == row["net"] and row["paid_out"] == 0


async def test_a_payout_is_recorded_in_part_and_comes_off_the_balance(client, session, make_admin, mint_token, shop):
    h = await role_headers(make_admin, mint_token)
    club_id = shop["club"].id
    body = {"amount": "300.00", "paid_on": date.today().isoformat(), "reference": "UTR123", "note": "first half"}
    for role in ("viewer", "support"):
        assert (await client.post(f"{API}/admin/payouts/clubs/{club_id}", json=body, headers=h[role])).status_code == 403
    r = await client.post(f"{API}/admin/payouts/clubs/{club_id}", json=body, headers=h["owner"])
    assert r.status_code == 201, r.text
    assert r.json()["amount"] == 300.0 and r.json()["reference"] == "UTR123"
    statement = (await client.get(f"{API}/admin/payouts/clubs/{club_id}", headers=h["viewer"])).json()
    assert statement["summary"]["paid_out"] == 300.0
    assert statement["summary"]["outstanding"] == pytest.approx(1300 - 30.68 - 600 - 50 - 300)
    assert len(statement["payments"]) == 3 and statement["payments_total"] == 3
    assert {p["razorpay_payment_id"] for p in statement["payments"]} == {shop[k].razorpay_payment_id for k in ("known", "refunded", "part")}
    audit = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "payout.record"))
    assert audit is not None and audit.details["amount"] == "300.00"


@pytest.mark.parametrize("now, today", [
    (datetime(2026, 10, 26, 18, 29, tzinfo=timezone.utc), date(2026, 10, 26)),  # 23:59 IST
    (datetime(2026, 10, 26, 18, 31, tzinfo=timezone.utc), date(2026, 10, 27)),  # 00:01 IST: UTC is still on the 26th
    (datetime(2026, 10, 26, 23, 59, tzinfo=timezone.utc), date(2026, 10, 27)),  # 05:29 IST
])
async def test_a_payout_dated_today_in_india_is_not_in_the_future(client, make_admin, mint_token, shop, monkeypatch, now, today):
    """Between midnight and 05:30 in India, UTC's date is still yesterday;
    a payout an admin dated today was refused as dated in the future."""
    monkeypatch.setattr(payouts, "_now", lambda: now)
    h = await role_headers(make_admin, mint_token)
    url = f"{API}/admin/payouts/clubs/{shop['club'].id}"
    body = {"amount": "1", "paid_on": today.isoformat(), "reference": "UTR-TODAY"}
    r = await client.post(url, json=body, headers=h["owner"])
    assert r.status_code == 201, r.text
    tomorrow = await client.post(url, json={**body, "paid_on": (today + timedelta(days=1)).isoformat()}, headers=h["owner"])
    assert tomorrow.status_code == 422 and tomorrow.json()["error_code"] == "PAYOUT_IN_FUTURE"


async def test_paying_more_than_is_owed_needs_saying_so(client, make_admin, mint_token, shop):
    h = await role_headers(make_admin, mint_token)
    club_id = shop["club"].id
    body = {"amount": "5000", "paid_on": date.today().isoformat(), "reference": "UTR9"}
    over = await client.post(f"{API}/admin/payouts/clubs/{club_id}", json=body, headers=h["owner"])
    assert over.status_code == 409 and over.json()["error_code"] == "PAYOUT_EXCEEDS_BALANCE"
    forced = await client.post(f"{API}/admin/payouts/clubs/{club_id}", json={**body, "force": True}, headers=h["owner"])
    assert forced.status_code == 201
    future = await client.post(f"{API}/admin/payouts/clubs/{club_id}",
                               json={**body, "amount": "1", "paid_on": (date.today() + timedelta(days=2)).isoformat()}, headers=h["owner"])
    assert future.status_code == 422


async def test_a_payout_is_voided_never_deleted(client, session, make_admin, mint_token, shop):
    h = await role_headers(make_admin, mint_token)
    club_id = shop["club"].id
    made = (await client.post(f"{API}/admin/payouts/clubs/{club_id}",
                              json={"amount": "100", "paid_on": date.today().isoformat(), "reference": "WRONG"}, headers=h["owner"])).json()
    assert (await client.post(f"{API}/admin/payouts/{made['id']}/void", json={"reason": "typo"}, headers=h["support"])).status_code == 403
    v = await client.post(f"{API}/admin/payouts/{made['id']}/void", json={"reason": "wrong club"}, headers=h["owner"])
    assert v.status_code == 200 and v.json()["voided"] is True and v.json()["void_reason"] == "wrong club"
    again = await client.post(f"{API}/admin/payouts/{made['id']}/void", json={"reason": "again"}, headers=h["owner"])
    assert again.status_code == 409
    assert await session.get(ClubPayouts, made["id"]) is not None
    statement = (await client.get(f"{API}/admin/payouts/clubs/{club_id}", headers=h["owner"])).json()
    assert statement["summary"]["paid_out"] == 0
    assert statement["payouts"][0]["voided"] is True


async def test_the_statement_file_has_every_line_and_the_running_balance(client, make_admin, mint_token, shop):
    h = await role_headers(make_admin, mint_token)
    club_id = shop["club"].id
    await client.post(f"{API}/admin/payouts/clubs/{club_id}",
                      json={"amount": "200", "paid_on": date.today().isoformat(), "reference": "UTR-CSV"}, headers=h["owner"])
    for role in ("viewer", "support"):
        assert (await client.get(f"{API}/admin/payouts/clubs/{club_id}/statement.csv", headers=h[role])).status_code == 403
    r = await client.get(f"{API}/admin/payouts/clubs/{club_id}/statement.csv", headers=h["owner"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    kinds = [row["type"] for row in rows]
    assert kinds.count("payment") == 3 and kinds.count("refund") == 2 and kinds.count("chargeback") == 1 and kinds.count("payout") == 1
    assert "fee (estimated)" in kinds and "fee" in kinds
    assert Decimal(rows[-1]["balance"]) == Decimal("1300") - Decimal("30.68") - Decimal("600") - Decimal("50") - Decimal("200")
    # Payment ids only: no guest's address.
    assert "@" not in r.text


async def test_fees_are_read_from_razorpay_by_a_job(client, session, make_admin, mint_token, shop, monkeypatch):
    h = await role_headers(make_admin, mint_token)
    asked = []

    def fetch(payment_id, *a, **kw):
        asked.append(payment_id)
        return {"id": payment_id, "fee": 1180, "tax": 180}

    monkeypatch.setattr(payments_service.razorpay_client.payment, "fetch", fetch)
    assert (await client.post(f"{API}/admin/payouts/fees/fetch", headers=h["support"])).status_code == 403
    r = await client.post(f"{API}/admin/payouts/fees/fetch", headers=h["owner"])
    assert r.status_code == 200 and r.json()["missing"] == 2
    # The job ran after the answer (inline in the suite).
    assert sorted(asked) == sorted([shop["refunded"].razorpay_payment_id, shop["part"].razorpay_payment_id])
    fees = (await session.scalars(select(PaymentFees))).all()
    assert len(fees) == 3
    row = next(i for i in (await client.get(f"{API}/admin/payouts", headers=h["owner"])).json()["items"] if i["club"]["id"] == shop["club"].id)
    assert row["fees_estimated"] == 0 and row["fees"] == pytest.approx(35.40)


async def test_a_payment_razorpay_will_not_describe_waits_for_the_next_batch(session, shop, monkeypatch):
    def fetch(payment_id, *a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(payments_service.razorpay_client.payment, "fetch", fetch)
    assert await payouts.fetch_fee_batch(session) == 0


async def test_the_overview_filters_by_name_and_by_owing(client, make_admin, mint_token, shop):
    h = await role_headers(make_admin, mint_token)
    found = (await client.get(f"{API}/admin/payouts", params={"q": "payout"}, headers=h["viewer"])).json()
    assert [i["club"]["id"] for i in found["items"]] == [shop["club"].id]
    none = (await client.get(f"{API}/admin/payouts", params={"q": "nothing-like-it"}, headers=h["viewer"])).json()
    assert none["items"] == [] and none["total"] == 0
    owing = (await client.get(f"{API}/admin/payouts", params={"owing": "true"}, headers=h["viewer"])).json()
    assert owing["owing_clubs"] >= 1
