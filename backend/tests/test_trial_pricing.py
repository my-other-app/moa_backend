"""The owner's pricing of 2026-10: a free 14-day Pro trial for every new
club (no card, no Razorpay), then Free unless a paid plan was picked during
it (billed from the trial's end); Enterprise trials an admin grants; Campus
folded into Enterprise; no storage anywhere.

Razorpay is a fake throughout (FakeRazorpay of test_club_billing), mail is
captured by the `mail` fixture of test_billing_alerts.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import settings
from tests.conftest import auth_header
from tests.test_billing_alerts import _alerts, mail  # noqa: F401  (mail is a fixture)
from tests.test_club_billing import (  # noqa: F401  (razorpay is a fixture)
    FakeRazorpay,
    _row,
    _webhook,
    make_plan,
    razorpay,
)

API = "/api/v1"
DAY = timedelta(days=1)
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


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
    """Free, the three paid tiers (no plan trials: the free trial replaced
    them), and Enterprise on request."""
    free = await make_plan(session, key="free", limits={"events_per_month": 2, "registrations_per_month": 300,
                                                         "committee_seats": 2})
    starter = await make_plan(session, key="starter", monthly=29900, yearly=299000,
                              limits={"events_per_month": 5}, features=("tickets", "website"))
    growth = await make_plan(session, key="growth", monthly=69900, yearly=699000,
                             limits={"events_per_month": 15}, features=("tickets", "website", "quiz", "networking"))
    pro = await make_plan(session, key="pro", monthly=149900, yearly=1499000, limits={"events_per_month": None},
                          features=("tickets", "website", "quiz", "networking", "merch", "teams"))
    enterprise = await make_plan(session, key="enterprise", contact_sales=True,
                                 features=("tickets", "website", "quiz", "networking", "merch", "teams", "finance"))
    await session.commit()
    return SimpleNamespace(free=free, starter=starter, growth=growth, pro=pro, enterprise=enterprise)


async def _admin_header(make_admin, mint_token, email="trial-admin@example.com"):
    return auth_header(await mint_token(await make_admin(email=email)))


async def _apply_and_approve(client, header, *, email="newclub@example.com", name="New Club"):
    applied = await client.post(f"{API}/clubs/create", data={"email": email, "password": "Password123!", "name": name})
    assert applied.status_code == 200, applied.text
    club_id = applied.json()["id"]
    approved = await client.post(f"{API}/clubs/{club_id}/approve", headers=header)
    assert approved.status_code == 200, approved.text
    return club_id


async def _club_header(session, mint_token, club_id):
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    club = await session.get(Clubs, club_id)
    return auth_header(await mint_token(await session.get(Users, club.user_id)))


async def _plan_key(session, club_id):
    from app.api.subscriptions import service

    await _row(session, club_id)
    resolved = await service.resolve_entitlements(session, club_id)
    return resolved.plan.key if resolved.plan else None


async def _set(session, club_id, **fields):
    row = await _row(session, club_id)
    for key, value in fields.items():
        setattr(row, key, value)
    await session.commit()
    return row


async def _sweep(session, now=None):
    from app.api.subscriptions import sweep

    return await sweep.sweep(session, now or _now())


# ─── 1. A new club starts a 14-day Pro trial ─────────────────────────────


async def test_approval_starts_a_14_day_pro_trial_without_razorpay(client, session, tiers, razorpay, make_admin, mint_token):
    header = await _admin_header(make_admin, mint_token)
    applied = await client.post(f"{API}/clubs/create", data={"email": "fresh@example.com", "password": "Password123!",
                                                              "name": "Fresh Club"})
    club_id = applied.json()["id"]
    # Waiting for approval spends none of the trial.
    assert await _row(session, club_id) is None

    assert (await client.post(f"{API}/clubs/{club_id}/approve", headers=header)).status_code == 200

    row = await _row(session, club_id)
    assert (row.plan.key, row.status.value, row.razorpay_subscription_id) == ("pro", "trial", None)
    assert row.trial_plan_id == tiers.pro.id and row.mrr_paise == 0
    assert abs(row.trial_ends_at - (_now() + 14 * DAY)) < timedelta(minutes=1)
    assert razorpay.calls == []

    ent = (await client.get(f"{API}/clubs/entitlements", headers=await _club_header(session, mint_token, club_id))).json()
    assert ent["plan_key"] == "pro" and ent["status"] == "trial"
    assert ent["trial"]["kind"] == "free_trial" and ent["trial"]["days_left"] == 14
    assert ent["features"]["merch"] is True and ent["limits"]["events_per_month"] is None
    assert ent["chosen_plan"] is None and ent["trial_ended"] is False and ent["free_trial_days"] == 14


async def test_an_admin_created_club_starts_its_trial_at_once(client, session, tiers, make_admin, mint_token):
    header = await _admin_header(make_admin, mint_token)
    created = await client.post(f"{API}/clubs/admin/create", json={"name": "Made By Admin", "email": "made@example.com"},
                                headers=header)
    assert created.status_code == 200, created.text
    assert await _plan_key(session, created.json()["id"]) == "pro"


async def test_one_trial_per_club_ever(client, session, tiers, make_admin, mint_token, make_club):
    from app.api.subscriptions import service

    header = await _admin_header(make_admin, mint_token)
    club_id = await _apply_and_approve(client, header)
    first = await _row(session, club_id)
    started = first.trial_started_at

    # Approving again, or asking again, starts nothing new.
    assert (await client.post(f"{API}/clubs/{club_id}/approve", headers=header)).status_code == 200
    assert await service.start_free_trial(session, club_id) is None
    assert (await _row(session, club_id)).trial_started_at == started

    # Once it has ended, never again.
    await _set(session, club_id, trial_ends_at=_now() - DAY)
    await _sweep(session)
    assert await service.start_free_trial(session, club_id) is None
    assert await _plan_key(session, club_id) == "free"

    # A club that had a Razorpay plan trial before counts as having had one.
    from app.api.subscriptions.models import ClubSubscriptions

    _, old = await make_club(name="Old Trial", email="old-trial@example.com")
    session.add(ClubSubscriptions(club_id=old.id, plan_id=tiers.growth.id, status="cancelled",
                                  trial_started_at=_now() - 90 * DAY, current_period_end=_now() - 60 * DAY))
    await session.commit()
    assert await service.start_free_trial(session, old.id) is None


async def test_existing_free_clubs_get_no_trial(client, session, tiers, make_club):
    """The trial is for new clubs: nothing hands one to a club already on Free."""
    _, club = await make_club(name="Been Here", email="been-here@example.com")
    await _sweep(session)
    assert await _row(session, club.id) is None
    assert await _plan_key(session, club.id) == "free"


# ─── 2. Gates during and after the trial ─────────────────────────────────


async def test_gates_give_pro_during_the_trial_and_free_after(client, session, tiers, make_admin, mint_token):
    from app.api.subscriptions import gates
    from app.response import CustomHTTPException

    club_id = await _apply_and_approve(client, await _admin_header(make_admin, mint_token))
    await gates.require_feature(session, club_id, "merch")
    await gates.require_within(session, club_id, "events_per_month", used=40)

    # The day it ends, the plan does too -- whether or not the sweep has run.
    await _set(session, club_id, trial_ends_at=_now() - timedelta(minutes=1))
    gates.reset()
    with pytest.raises(CustomHTTPException) as locked:
        await gates.require_feature(session, club_id, "merch")
    assert locked.value.error_code == gates.FEATURE_LOCKED
    with pytest.raises(CustomHTTPException):
        await gates.require_within(session, club_id, "events_per_month", used=2)


# ─── 3. The sweep at the trial's end ─────────────────────────────────────


async def test_an_expired_trial_moves_to_free_and_says_so_once(client, session, tiers, make_admin, mint_token, mail):
    club_id = await _apply_and_approve(client, await _admin_header(make_admin, mint_token), email="ends@example.com")
    await mail.drain()
    mail.clear()

    # Three days out: "3 days left", once.
    await _set(session, club_id, trial_ends_at=_now() + 2 * DAY)
    assert (await _sweep(session))["free_trial_reminded"] == 1
    assert (await _sweep(session))["free_trial_reminded"] == 0
    [reminder] = (await mail.drain()).to("ends@example.com")
    assert reminder["subject"] == "3 days left in your Pro trial"
    assert "moves to the Free plan" in reminder["text"]

    # Past its end: Free, mailed once, the admins told.
    await _set(session, club_id, trial_ends_at=_now() - DAY)
    assert (await _sweep(session))["free_trial_ended"] == 1
    assert (await _sweep(session))["free_trial_ended"] == 0
    row = await _row(session, club_id)
    assert (row.status.value, row.trial_plan_id) == ("cancelled", None)
    assert await _plan_key(session, club_id) == "free"
    ended = [m for m in (await mail.drain()).to("ends@example.com") if "has ended" in m["subject"]]
    assert len(ended) == 1 and "is on Free" in ended[0]["subject"]
    assert "2 events and 300 registrations a month, 2 team seats" in ended[0]["text"]
    assert len(await _alerts(session, "free_trial_ended")) == 1

    ent = (await client.get(f"{API}/clubs/entitlements", headers=await _club_header(session, mint_token, club_id))).json()
    assert (ent["plan_key"], ent["trial"], ent["trial_ended"]) == ("free", None, True)


async def test_the_sweep_never_moves_a_paid_subscription(session, tiers, make_club):
    """A row Razorpay bills, with no trial on it, is not the sweep's."""
    from app.api.subscriptions.models import ClubSubscriptions

    _, club = await make_club(name="Payer", email="payer@example.com")
    session.add(ClubSubscriptions(club_id=club.id, plan_id=tiers.growth.id, status="active",
                                  razorpay_subscription_id="sub_payer", activated_at=_now() - 40 * DAY,
                                  authenticated_at=_now() - 40 * DAY, trial_started_at=_now() - 60 * DAY,
                                  trial_ends_at=_now() - 46 * DAY, current_period_end=_now() + 20 * DAY,
                                  mrr_paise=69900))
    await session.commit()
    done = await _sweep(session)
    assert done["free_trial_ended"] == 0
    row = await _row(session, club.id)
    assert (row.status.value, row.plan_id) == ("active", tiers.growth.id)


# ─── 4. Picking a paid plan during the trial ─────────────────────────────


async def test_a_plan_picked_during_the_trial_bills_from_its_end(client, session, tiers, razorpay, make_admin, mint_token, mail):
    club_id = await _apply_and_approve(client, await _admin_header(make_admin, mint_token), email="picker@example.com")
    header = await _club_header(session, mint_token, club_id)
    trial_end = (await _row(session, club_id)).trial_ends_at

    started = await client.post(f"{API}/clubs/subscription/start", json={"plan_key": "growth"}, headers=header)
    assert started.status_code == 200, started.text
    body = started.json()
    assert abs(datetime.fromisoformat(body["billing_starts_at"]) - trial_end) < timedelta(seconds=1)
    [(payload,)] = razorpay.of("subscription", "create")
    assert payload["start_at"] == int(trial_end.timestamp())

    # Still the Pro trial, with Growth waiting.
    ent = (await client.get(f"{API}/clubs/entitlements", headers=header)).json()
    assert ent["plan_key"] == "pro" and ent["trial"]["kind"] == "free_trial"
    assert ent["chosen_plan"]["plan_key"] == "growth" and ent["chosen_plan"]["status"] == "pending"

    # Razorpay takes the mandate: the status stays trial, the plan stays Pro.
    await _webhook(session, "subscription.authenticated", body["razorpay_subscription_id"])
    row = await _row(session, club_id)
    assert (row.status.value, row.plan.key, row.trial_plan_id) == ("trial", "growth", tiers.pro.id)
    assert await _plan_key(session, club_id) == "pro"
    welcome = [m for m in (await mail.drain()).to("picker@example.com") if "starts on" in m["subject"]]
    assert len(welcome) == 1 and welcome[0]["subject"].startswith("Growth starts on")
    sub = (await client.get(f"{API}/clubs/subscription", headers=header)).json()
    assert sub["free_trial"] is True and sub["trial_plan"]["key"] == "pro"
    assert abs(datetime.fromisoformat(sub["billing_starts_at"]) - trial_end) < timedelta(seconds=1)

    # The reminder names what comes next, not Free.
    await _set(session, club_id, trial_ends_at=_now() + 2 * DAY)
    await _sweep(session)
    [reminder] = [m for m in (await mail.drain()).to("picker@example.com") if m["subject"].startswith("3 days left")]
    assert "Then Growth starts" in reminder["text"]

    # The trial ends: Growth, no "you're on Free".
    await _set(session, club_id, trial_ends_at=_now() - timedelta(minutes=5))
    assert (await _sweep(session))["free_trial_ended"] == 1
    assert await _plan_key(session, club_id) == "growth"
    assert not [m for m in (await mail.drain()).to("picker@example.com") if "is on Free" in m["subject"]]
    [alert] = await _alerts(session, "free_trial_ended")
    assert alert.data["outcome"] == "chosen_plan"

    # And Razorpay's first charge makes it active.
    await _webhook(session, "subscription.charged", body["razorpay_subscription_id"], current_end=_now() + 30 * DAY)
    assert (await _row(session, club_id)).status.value == "active"


# ─── 5. Enterprise trials ────────────────────────────────────────────────


async def _paying(session, club_id, plan_id):
    from app.api.subscriptions.models import ClubSubscriptions

    now = _now()
    session.add(ClubSubscriptions(club_id=club_id, plan_id=plan_id, status="active",
                                  razorpay_subscription_id="sub_growth_live", authenticated_at=now - 40 * DAY,
                                  activated_at=now - 40 * DAY, current_period_end=now + 20 * DAY, mrr_paise=69900))
    await session.commit()


async def test_an_enterprise_trial_lays_over_a_paid_plan_and_gives_it_back(client, session, tiers, razorpay, make_club,
                                                                           make_admin, mint_token, mail):
    from app.api.audit.models import AdminAuditLogs

    owner, club = await make_club(name="Big Fest", email="bigfest@example.com")
    await _paying(session, club.id, tiers.growth.id)
    header = await _admin_header(make_admin, mint_token)

    granted = await client.post(f"{API}/admin/subscriptions/club/{club.id}/enterprise-trial", json={}, headers=header)
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["enterprise_trial"]["plan"]["key"] == "enterprise"
    assert abs(datetime.fromisoformat(body["enterprise_trial"]["ends_at"]) - (_now() + 30 * DAY)) < timedelta(minutes=1)
    assert body["enterprise_trial_by"]["id"] is not None
    # Razorpay keeps billing Growth underneath; nothing was cancelled.
    assert razorpay.calls == [] and body["status"] == "active" and body["plan"]["key"] == "growth"
    assert await _plan_key(session, club.id) == "enterprise"
    ent = (await client.get(f"{API}/clubs/entitlements", headers=auth_header(await mint_token(owner)))).json()
    assert ent["trial"]["kind"] == "enterprise_trial" and ent["features"]["finance"] is True

    # Ended early by the admin: back on Growth, mailed once, on the record.
    ended = await client.post(f"{API}/admin/subscriptions/club/{club.id}/end-trial", json={}, headers=header)
    assert ended.status_code == 200, ended.text
    assert ended.json()["enterprise_trial"] is None
    assert await _plan_key(session, club.id) == "growth"
    actions = set((await session.scalars(select(AdminAuditLogs.action))).all())
    assert {"subscription.enterprise_trial_grant", "subscription.enterprise_trial_end"} <= actions
    mails = [m for m in (await mail.drain()).to("bigfest@example.com") if "Enterprise trial" in m["subject"]]
    assert [m["subject"] for m in mails] == ["Your Enterprise trial has ended"]
    assert "is on Growth now" in mails[0]["text"]
    again = await client.post(f"{API}/admin/subscriptions/club/{club.id}/end-trial", json={}, headers=header)
    assert again.status_code == 409 and again.json()["error_code"] == "NO_TRIAL"


async def test_an_enterprise_trial_runs_out_on_its_date(client, session, tiers, make_club, make_admin, mint_token, mail):
    _, club = await make_club(name="Trial Fest", email="trialfest@example.com")
    header = await _admin_header(make_admin, mint_token)
    granted = await client.post(f"{API}/admin/subscriptions/club/{club.id}/enterprise-trial", json={"days": 10},
                                headers=header)
    assert granted.status_code == 200, granted.text
    assert await _plan_key(session, club.id) == "enterprise"

    await _set(session, club.id, enterprise_trial_ends_at=_now() + 2 * DAY)
    assert (await _sweep(session))["enterprise_trial_reminded"] == 1
    assert (await _sweep(session))["enterprise_trial_reminded"] == 0
    await _set(session, club.id, enterprise_trial_ends_at=_now() - timedelta(minutes=1))
    # Over on its date, before the sweep.
    assert await _plan_key(session, club.id) == "free"
    assert (await _sweep(session))["enterprise_trial_ended"] == 1
    assert (await _sweep(session))["enterprise_trial_ended"] == 0

    subjects = [m["subject"] for m in (await mail.drain()).to("trialfest@example.com")]
    assert subjects.count("Your Enterprise trial has ended") == 1
    assert len([s for s in subjects if s.startswith("Your Enterprise trial ends on")]) == 1
    assert len(await _alerts(session, "enterprise_trial_ending")) == 1
    assert len(await _alerts(session, "enterprise_trial_ended")) == 1


async def test_an_enterprise_trial_over_the_free_trial_hands_it_back(client, session, tiers, make_admin, mint_token):
    header = await _admin_header(make_admin, mint_token)
    club_id = await _apply_and_approve(client, header, email="both@example.com")
    await client.post(f"{API}/admin/subscriptions/club/{club_id}/enterprise-trial", json={"days": 3}, headers=header)
    assert await _plan_key(session, club_id) == "enterprise"
    await client.post(f"{API}/admin/subscriptions/club/{club_id}/end-trial", json={"trial": "enterprise"}, headers=header)
    assert await _plan_key(session, club_id) == "pro"


async def test_trials_need_the_money_permission(client, session, tiers, make_club, make_admin, mint_token):
    from app.api.users.models import AdminRoles

    _, club = await make_club(name="Perm", email="perm@example.com")
    viewer = auth_header(await mint_token(await make_admin(email="viewer@example.com", role=AdminRoles.viewer)))
    response = await client.post(f"{API}/admin/subscriptions/club/{club.id}/enterprise-trial", json={}, headers=viewer)
    assert response.status_code == 403


# ─── 6. The migration: Campus into Enterprise, plan trials off ───────────


def _migration(name):
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migration_folds_campus_into_enterprise(client, session, make_club):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle, Plans

    plans_seed, trial_pricing = _migration("club_plans"), _migration("trial_pricing")
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            plans_seed.seed_plans()

    await connection.run_sync(seed)
    await session.commit()
    campus = await session.scalar(select(Plans).where(Plans.key == "campus"))
    _, comp_club = await make_club(name="Campus Comp", email="campus-comp@example.com")
    comp_club.requested_plan = "campus"
    ends = _now() + 40 * DAY
    await service.grant_complimentary(session, club_id=comp_club.id, plan=campus, cycle=BillingCycle.yearly,
                                      reason="Fest partner", ends_at=ends, granted_by=None)
    await session.commit()

    def migrate(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            trial_pricing.retire_plan_trials(sync)
            return trial_pricing.fold_campus(sync)

    assert await connection.run_sync(migrate) == 1
    await session.commit()

    row = await _row(session, comp_club.id)
    assert row.plan.key == "enterprise" and row.complimentary and abs(row.comp_ends_at - ends) < timedelta(seconds=1)
    resolved = await service.resolve_entitlements(session, comp_club.id)
    assert resolved.plan.key == "enterprise" and resolved.allows("finance") and resolved.allows("networking")
    await session.refresh(comp_club)
    assert comp_club.requested_plan == "enterprise"
    # Campus is kept (legacy), off the pricing page; the three tiers have no plan trial.
    await session.refresh(campus)
    assert campus.visibility.value == "legacy" and not campus.is_deleted

    body = (await client.get(f"{API}/plans")).json()
    assert [p["key"] for p in body["plans"]] == ["free", "starter", "growth", "pro", "enterprise"]
    by_key = {p["key"]: p for p in body["plans"]}
    assert {k: (p["price_monthly_paise"], p["trial_days"]) for k, p in by_key.items()} == {
        "free": (0, 0), "starter": (29900, 0), "growth": (69900, 0), "pro": (149900, 0), "enterprise": (0, 0),
    }
    assert by_key["growth"]["featured"] and by_key["enterprise"]["contact_sales"]
    assert body["trial"] == {"days": 14, "plan_key": "pro", "plan_name": "Pro", "card_required": False,
                             "then_plan_key": "free"}
    assert body["enterprise_trial_days"] == 30


# ─── 7. No storage anywhere ──────────────────────────────────────────────


async def test_no_storage_in_any_plan_answer(client, session, tiers, make_admin, mint_token):
    header = await _admin_header(make_admin, mint_token)
    club_id = await _apply_and_approve(client, header, email="nostorage@example.com")
    club_header = await _club_header(session, mint_token, club_id)
    answers = [
        await client.get(f"{API}/plans"),
        await client.get(f"{API}/admin/plans", headers=header),
        await client.get(f"{API}/admin/plans/{tiers.pro.id}", headers=header),
        await client.get(f"{API}/admin/subscriptions/club/{club_id}", headers=header),
        await client.get(f"{API}/admin/subscriptions", headers=header),
        await client.get(f"{API}/clubs/entitlements", headers=club_header),
        await client.get(f"{API}/clubs/subscription", headers=club_header),
    ]
    for answer in answers:
        assert answer.status_code == 200, answer.text
        assert "storage" not in json.dumps(answer.json()), answer.request.url
    # And an admin form that still sends it is not refused (ignored).
    from app.api.subscriptions.schemas import PlanLimitsIn

    assert "storage_gb" not in PlanLimitsIn.model_fields


# ─── 8. The demo keeps everything it shows ───────────────────────────────


async def test_demo_clubs_are_put_on_enterprise_and_keep_networking(session, tiers, make_club):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import BillingCycle
    from scripts.demo.seed import Seeder

    # Campus, withdrawn, sorts after everything: never the demo's plan.
    await make_plan(session, key="campus", monthly=399900, visibility="legacy",
                    features=("networking", "finance"))
    await session.commit()
    plan = await Seeder._top_plan(SimpleNamespace(s=session))
    assert plan.key == "enterprise"

    _, club = await make_club(name="Demo Club", email="demo-club@demo.myotherapp.in")
    await service.grant_complimentary(session, club_id=club.id, plan=plan, cycle=BillingCycle.yearly,
                                      reason="Demo club", ends_at=None, granted_by=None)
    await session.commit()
    resolved = await service.resolve_entitlements(session, club.id)
    assert resolved.plan.key == "enterprise"
    assert all(resolved.allows(f) for f in ("networking", "quiz", "merch", "teams", "finance"))
    # A demo club is never handed a free trial on top.
    assert await service.start_free_trial(session, club.id) is None


async def test_no_trial_is_started_without_a_pro_plan(session, make_club):
    from app.api.subscriptions import service
    from app.api.subscriptions.models import ClubSubscriptions

    _, club = await make_club(name="No Plans", email="no-plans@example.com")
    assert await service.start_free_trial(session, club.id) is None
    assert await session.scalar(select(func.count(ClubSubscriptions.id))) == 0
