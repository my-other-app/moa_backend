"""Giving existing clubs the free trial (subscriptions/trial_gift.py): which
clubs are eligible, the preview, applying it once and only once, the mail and
its switch, who may press the button, and what happens when the trial ends.

Mail is captured by the `mail` fixture of test_billing_alerts; nothing here
reaches Razorpay.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import settings
from tests.conftest import auth_header
from tests.test_billing_alerts import mail  # noqa: F401  (a fixture)
from tests.test_club_billing import make_plan

API = "/api/v1"
DAY = timedelta(days=1)
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
GIFT = f"{API}/admin/subscriptions/trial-gift"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    from app.api.subscriptions import gates

    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    gates.reset()
    yield
    gates.reset()


@pytest.fixture
async def tiers(session):
    """Free, Growth, Pro (the trial's plan, with the trial's own allowances)
    and Enterprise."""
    free = await make_plan(session, key="free", limits={"events_per_month": 2, "emails_per_month": 200,
                                                         "whatsapp_per_month": 0, "ai_actions_per_month": 0},
                           features=("checkin", "certificates"))
    growth = await make_plan(session, key="growth", monthly=69900, yearly=699000, limits={"events_per_month": 15},
                             features=("checkin", "certificates", "tickets", "quiz"))
    pro = await make_plan(
        session, key="pro", monthly=149900, yearly=1499000,
        limits={"events_per_month": None, "emails_per_month": 20000, "whatsapp_per_month": 1000,
                "whatsapp_marketing_per_month": 200, "ai_actions_per_month": 500,
                "trial_emails_per_month": 2000, "trial_whatsapp_per_month": 50,
                "trial_whatsapp_marketing_per_month": 10, "trial_ai_actions_per_month": 50},
        features=("checkin", "certificates", "tickets", "quiz", "merch", "teams", "ai_assist"),
    )
    enterprise = await make_plan(session, key="enterprise", contact_sales=True, features=("tickets", "finance"))
    await session.commit()
    return SimpleNamespace(free=free, growth=growth, pro=pro, enterprise=enterprise)


@pytest.fixture
async def clubs(session, tiers, make_club):
    """Two clubs that should get the trial, and one of each kind that should not."""
    from app.api.message_quota.models import ClubMessageQuotas
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle, ClubSubscriptions, SubscriptionStatus

    now = _now()
    out = {}
    for key, name, email in (
        ("plain", "Plain Club", "plain-owner@example.com"),
        ("lapsed", "Lapsed Club", "lapsed-owner@example.com"),
        ("demo", "Respawn Room", "respawn-room@demo.myotherapp.in"),
        ("had_trial", "Trialled Club", "trialled@example.com"),
        ("paying", "Paying Club", "paying@example.com"),
        ("comp", "Comp Club", "comp@example.com"),
        ("ent_trial", "Ent Trial Club", "ent-trial@example.com"),
        ("custom", "Custom Deal Club", "custom@example.com"),
        ("checkout", "Checkout Club", "checkout@example.com"),
        ("deleted", "Deleted Club", "deleted@example.com"),
    ):
        _, club = await make_club(name=name, email=email)
        out[key] = club
    _, out["pending"] = await make_club(name="Unapproved Club", email="unapproved@example.com", is_verified=False)

    # On Free with a row from a subscription that ended (no trial ever).
    session.add(ClubSubscriptions(club_id=out["lapsed"].id, plan_id=tiers.growth.id, cycle=BillingCycle.monthly,
                                  status=SubscriptionStatus.cancelled, current_period_end=now - 30 * DAY))
    trial = await service.start_free_trial(session, out["had_trial"].id, now - 40 * DAY)
    service.end_free_trial(trial, now - 26 * DAY)
    await service.assign_plan(session, club_id=out["paying"].id, plan=tiers.growth, cycle=BillingCycle.monthly)
    await service.grant_complimentary(session, club_id=out["comp"].id, plan=tiers.pro, cycle=BillingCycle.yearly,
                                      reason="Fest partner", ends_at=None, granted_by=None)
    await service.grant_enterprise_trial(session, club_id=out["ent_trial"].id, plan=tiers.enterprise, days=30,
                                         granted_by=None)
    session.add(ClubMessageQuotas(club_id=out["custom"].id, emails_per_month=50000, whatsapp_per_month=None))
    session.add(ClubSubscriptions(club_id=out["checkout"].id, plan_id=tiers.growth.id, cycle=BillingCycle.monthly,
                                  status=SubscriptionStatus.pending, razorpay_subscription_id="sub_checkout",
                                  pending_since=now))
    out["deleted"].soft_delete()
    await session.commit()
    return out


async def _admin(make_admin, mint_token, email="gift-admin@example.com", role=None):
    kwargs = {"email": email} if role is None else {"email": email, "role": role}
    return auth_header(await mint_token(await make_admin(**kwargs)))


def clubs_plan_id(row) -> int:
    """The plan the row is on, which a free trial's row also names as its trial plan."""
    return row.plan_id


async def _row(session, club_id):
    from app.api.subscriptions.models import ClubSubscriptions

    return await session.scalar(
        select(ClubSubscriptions)
        .where(ClubSubscriptions.club_id == club_id, ClubSubscriptions.is_deleted == False)  # noqa: E712
        .execution_options(populate_existing=True)
    )


async def test_the_preview_counts_real_clubs_on_free_that_never_had_a_trial(client, session, clubs, make_admin, mint_token):
    admin = await _admin(make_admin, mint_token)
    r = await client.get(GIFT, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True and (body["plan_key"], body["plan_name"], body["days"]) == ("pro", "Pro", 14)
    assert body["then_plan_key"] == "free"
    assert body["eligible"] == 2
    assert [c["name"] for c in body["sample"]] == ["Plain Club", "Lapsed Club"]
    assert {e["reason"]: e["count"] for e in body["excluded"]} == {
        "demo": 1, "had_trial": 1, "paying": 1, "complimentary": 1, "enterprise_trial": 1,
        "custom_limits": 1, "checkout": 1,
    }
    # The trial's own allowances, and what Pro has that Free has not, as the pricing page names it.
    assert body["allowances"] == {"emails_per_month": 2000, "whatsapp_per_month": 50,
                                  "whatsapp_marketing_per_month": 10, "ai_actions_per_month": 50}
    assert body["unlocks"] == [
        "Paid tickets with Razorpay", "Live room: quiz", "Merchandise store", "Competitions: teams and submissions",
        "AI assist: event drafts, writing and feedback summaries",
    ]  # in the catalogue's order; not check-in or certificates, which Free has
    # Nothing was started by looking.
    assert await _row(session, clubs["plain"].id) is None


async def test_apply_starts_each_trial_as_a_new_clubs_mails_once_and_runs_once(client, session, clubs, mail, make_admin, mint_token):  # noqa: F811
    from app.api.audit.models import AdminAuditLogs
    from app.api.subscriptions import service
    from app.api.subscriptions.models import SubscriptionEmails, SubscriptionStatus

    admin = await _admin(make_admin, mint_token)
    r = await client.post(GIFT, json={}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["started"], body["emailed"], body["skipped"], body["remaining"]) == (2, 2, 0, 0)
    assert {c["name"] for c in body["clubs"]} == {"Plain Club", "Lapsed Club"}

    for key in ("plain", "lapsed"):
        row = await _row(session, clubs[key].id)
        assert row.status == SubscriptionStatus.trial and row.trial_plan_id == clubs_plan_id(row)
        assert row.razorpay_subscription_id is None
        assert abs((row.trial_ends_at - row.trial_started_at) - 14 * DAY) < timedelta(seconds=1)
        resolved = await service.resolve_entitlements(session, clubs[key].id)
        assert resolved.plan.key == "pro" and resolved.trial[0] == "free_trial"
        assert resolved.allows("merch")
        # Pro's features, the trial's allowances.
        assert (resolved.limits["emails_per_month"], resolved.limits["whatsapp_per_month"],
                resolved.limits["whatsapp_marketing_per_month"], resolved.limits["ai_actions_per_month"]) == (2000, 50, 10, 50)
    # Nobody else was touched.
    assert await _row(session, clubs["demo"].id) is None
    assert (await _row(session, clubs["paying"].id)).trial_started_at is None
    assert await _row(session, clubs["pending"].id) is None

    await mail.drain()
    gifts = [m for m in mail if m["subject"] == "Your club has 14 days of MyOtherApp Pro, free"]
    assert sorted(m["recipients"][0] for m in gifts) == ["lapsed-owner@example.com", "plain-owner@example.com"]
    text = gifts[0]["text"]
    assert "It unlocks:" in text and "2,000 emails, 50 WhatsApp messages and 50 AI actions a month" in text
    assert "goes back to the Free plan" in text and "Nothing is charged" in text
    sent = await session.scalar(select(func.count(SubscriptionEmails.id)).where(SubscriptionEmails.kind == "free_trial_gift"))
    assert sent == 2

    audits = (await session.scalars(select(AdminAuditLogs).where(
        AdminAuditLogs.action.in_(("subscription.free_trial_gift", "subscription.free_trial_gift_run"))))).all()
    assert sorted(a.action for a in audits) == [
        "subscription.free_trial_gift", "subscription.free_trial_gift", "subscription.free_trial_gift_run"]
    run = next(a for a in audits if a.action == "subscription.free_trial_gift_run")
    assert run.details["started"] == 2 and run.details["email"] is True

    # Again: nothing is eligible, nothing starts, nothing is mailed.
    before = len(mail)
    again = await client.post(GIFT, json={"email": True}, headers=admin)
    assert again.status_code == 200 and (again.json()["started"], again.json()["emailed"]) == (0, 0)
    preview = (await client.get(GIFT, headers=admin)).json()
    assert preview["eligible"] == 0
    assert {e["reason"]: e["count"] for e in preview["excluded"]}["had_trial"] == 3
    await mail.drain()
    assert len(mail) == before


async def test_without_the_mail_nothing_is_mailed(client, session, clubs, mail, make_admin, mint_token):  # noqa: F811
    from app.api.subscriptions.models import SubscriptionEmails

    admin = await _admin(make_admin, mint_token)
    r = await client.post(GIFT, json={"email": False}, headers=admin)
    assert r.status_code == 200 and (r.json()["started"], r.json()["emailed"]) == (2, 0)
    await mail.drain()
    assert not [m for m in mail if "14 days of MyOtherApp Pro" in m["subject"]]
    assert await session.scalar(select(func.count(SubscriptionEmails.id)).where(SubscriptionEmails.kind == "free_trial_gift")) == 0


async def test_only_an_admin_with_the_money_permission_may_apply(client, session, clubs, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    support = await _admin(make_admin, mint_token, email="gift-support@example.com", role=AdminRoles.support)
    assert (await client.get(GIFT, headers=support)).status_code == 200
    assert (await client.post(GIFT, json={}, headers=support)).status_code == 403
    assert (await client.post(GIFT, json={})).status_code == 401
    assert await _row(session, clubs["plain"].id) is None


async def test_a_gifted_trial_ends_on_free_with_no_card_asked_for(client, session, clubs, mail, make_admin, mint_token):  # noqa: F811
    from app.api.subscriptions import service, sweep

    admin = await _admin(make_admin, mint_token)
    assert (await client.post(GIFT, json={"email": False}, headers=admin)).json()["started"] == 2
    club_id = clubs["plain"].id

    # The club's own pages: on the Pro trial, with its allowances and days left.
    from app.api.users.models import Users

    owner = await session.get(Users, clubs["plain"].user_id)
    h = auth_header(await mint_token(owner))
    ent = (await client.get(f"{API}/clubs/entitlements", headers=h)).json()
    assert ent["plan_key"] == "pro" and ent["trial"]["kind"] == "free_trial" and ent["trial"]["days_left"] == 14
    assert ent["limits"]["emails_per_month"] == 2000 and ent["limits"]["ai_actions_per_month"] == 50
    assert ent["chosen_plan"] is None and ent["trial_ended"] is False

    # Three days before: the reminder; after the end: Free, one mail, no charge.
    await sweep.sweep(session, _now() + 11 * DAY + timedelta(hours=1))
    await sweep.sweep(session, _now() + 15 * DAY)
    await session.commit()
    resolved = await service.resolve_entitlements(session, club_id)
    assert resolved.plan.key == "free" and resolved.trial is None
    await mail.drain()
    subjects = [m["subject"] for m in mail.to("plain-owner@example.com")]
    assert "3 days left in your Pro trial" in subjects
    assert "Your Pro trial has ended: Plain Club is on Free" in subjects
    ent = (await client.get(f"{API}/clubs/entitlements", headers=h)).json()
    assert ent["plan_key"] == "free" and ent["trial"] is None and ent["trial_ended"] is True
