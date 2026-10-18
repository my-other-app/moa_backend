"""Club plans on the public pricing page, the seeded catalogue, and the plan
a club asks for when it applies.

The pricing page reads `GET /plans` without signing in, so what it returns is
a sales promise: only public plans, every limit spelled out (null is
unlimited), and one shared feature list so a comparison table lines up.
"""
import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.conftest import auth_header

API = "/api/v1"


# ─── Helpers ─────────────────────────────────────────────────────────────


async def make_plan(
    session,
    *,
    key,
    name=None,
    monthly=0,
    yearly=0,
    visibility="public",
    sort_order=0,
    featured=False,
    contact_sales=False,
    deleted=False,
    limits=None,
    features=(),
):
    from app.api.subscriptions.models import PlanFeatures, PlanLimits, PlanVisibility, Plans

    plan = Plans(
        key=key,
        name=name or key.title(),
        price_monthly_paise=monthly,
        price_yearly_paise=yearly,
        visibility=PlanVisibility(visibility),
        sort_order=sort_order,
        featured=featured,
        contact_sales=contact_sales,
    )
    if deleted:
        plan.soft_delete()
    session.add(plan)
    await session.flush()
    session.add(PlanLimits(plan_id=plan.id, **(limits or {})))
    session.add(PlanFeatures(plan_id=plan.id, **{flag: True for flag in features}))
    await session.flush()
    return plan


def _migration(name="club_plans"):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(form: dict, **extra) -> dict:
    """A complete application form."""
    return {"password": "Password123!", **form, **extra}


# ─── The pricing page ────────────────────────────────────────────────────


async def test_the_pricing_page_lists_only_public_plans_in_order(client, session):
    await make_plan(session, key="second", sort_order=2, monthly=50000, yearly=500000)
    await make_plan(session, key="first", sort_order=1)
    await make_plan(session, key="bespoke", visibility="hidden", sort_order=0)
    await make_plan(session, key="withdrawn", visibility="legacy", sort_order=0)
    await make_plan(session, key="gone", deleted=True, sort_order=0)

    # No sign-in: the page is public.
    response = await client.get(f"{API}/plans")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=300"
    body = response.json()
    assert body["currency"] == "INR"
    assert [plan["key"] for plan in body["plans"]] == ["first", "second"]


async def test_a_plan_on_the_page_carries_every_limit_and_the_whole_catalog(client, session):
    from app.api.subscriptions.models import FEATURE_CATALOG, LIMIT_KEYS

    await make_plan(
        session,
        key="growth",
        name="Growth",
        monthly=69900,
        yearly=699000,
        featured=True,
        limits={"events_per_month": 15, "coupons_per_event": 0},
        features=("tickets", "quiz", "sso"),
    )

    plan = (await client.get(f"{API}/plans")).json()["plans"][0]

    assert set(plan) == {
        "id", "key", "name", "tagline", "price_monthly_paise", "price_yearly_paise", "trial_days",
        "contact_sales", "featured", "limits", "features",
    }
    # Every column, with null for unlimited and 0 kept apart from it.
    assert set(plan["limits"]) == set(LIMIT_KEYS)
    assert plan["limits"]["events_per_month"] == 15
    assert plan["limits"]["coupons_per_event"] == 0
    assert plan["limits"]["storage_gb"] is None
    # The whole catalog, in its order, with the product's words.
    assert [(f["key"], f["label"]) for f in plan["features"]] == list(FEATURE_CATALOG)
    included = {f["key"] for f in plan["features"] if f["included"]}
    # `sso` is on in the row but has nothing built behind it, so the page
    # does not list it at all.
    assert included == {"tickets", "quiz"}
    assert "sso" not in {f["key"] for f in plan["features"]}
    assert plan["featured"] is True and plan["contact_sales"] is False


async def test_a_priced_on_request_plan_says_so(client, session):
    await make_plan(session, key="enterprise", contact_sales=True)

    plan = (await client.get(f"{API}/plans")).json()["plans"][0]

    assert plan["contact_sales"] is True
    assert plan["price_monthly_paise"] == 0


async def test_with_no_plans_the_page_is_empty_not_an_error(client):
    body = (await client.get(f"{API}/plans")).json()
    assert body == {"currency": "INR", "yearly_months_free": None, "plans": []}


def test_months_free_is_only_claimed_when_true_of_every_paid_plan():
    from app.api.subscriptions.schemas import yearly_months_free

    def plan(monthly, yearly, contact_sales=False):
        return SimpleNamespace(price_monthly_paise=monthly, price_yearly_paise=yearly, contact_sales=contact_sales)

    ten_months = [plan(0, 0), plan(29900, 299000), plan(69900, 699000), plan(0, 0, contact_sales=True)]
    assert yearly_months_free(ten_months) == 2
    # One plan at eleven months: no single number is right for both.
    assert yearly_months_free(ten_months + [plan(10000, 110000)]) is None
    # Not a whole number of months.
    assert yearly_months_free([plan(29900, 299999)]) is None
    # No discount at all.
    assert yearly_months_free([plan(29900, 358800)]) is None
    # Nothing paid to discount.
    assert yearly_months_free([plan(0, 0), plan(0, 0, contact_sales=True)]) is None


async def test_the_pricing_page_is_rate_limited_per_visitor(client):
    from app.api.subscriptions.router import PUBLIC_LIMIT

    assert PUBLIC_LIMIT == "120/minute"
    for _ in range(120):
        assert (await client.get(f"{API}/plans")).status_code == 200
    assert (await client.get(f"{API}/plans")).status_code == 429
    # Someone else is not caught by it.
    other = await client.get(f"{API}/plans", headers={"X-Forwarded-For": "203.0.113.50"})
    assert other.status_code == 200


# ─── The catalogue ───────────────────────────────────────────────────────


def test_the_catalog_and_the_seed_only_name_flags_that_exist():
    from app.api.subscriptions.models import FEATURE_CATALOG, FEATURE_KEYS, LIMIT_KEYS, UNBUILT_FEATURES

    keys = [key for key, _ in FEATURE_CATALOG]
    assert len(keys) == len(set(keys))
    assert set(keys) <= set(FEATURE_KEYS)
    assert UNBUILT_FEATURES == {
        "waitlist", "coupons", "sessions", "nobadge", "tfa", "payouts", "sso", "api",
    }

    module = _migration()
    assert set(module.LIMIT_COLUMNS) == set(LIMIT_KEYS)
    for key, flags in module.FEATURES.items():
        # Nothing seeded on that the page would not show.
        assert set(flags) <= set(keys), key


#: The draft limits, as the owner asked for them. None is unlimited.
EXPECTED_LIMITS = {
    #            ev/mo  reg/ev  reg/mo  tix  cpn   merch seats  GB    ann   sites
    "free":       (2,    100,    300,    1,   0,    0,    2,     1,    4,    1),
    "starter":    (5,    300,    1500,   3,   2,    5,    5,     5,    15,   3),
    "growth":     (15,   1000,   8000,   5,   10,   20,   10,    20,   60,   None),
    "pro":        (None, 5000,   30000,  10,  None, 100,  25,    100,  None, None),
    "campus":     (None, 20000,  150000, None, None, None, 60,   500,  None, None),
    "enterprise": (None,) * 10,
}
LIMIT_ORDER = (
    "events_per_month", "registrations_per_event", "registrations_per_month", "ticket_types_per_event",
    "coupons_per_event", "merch_items_per_event", "committee_seats", "storage_gb",
    "announcements_per_month", "website_templates",
)
EXPECTED_FEATURES = {
    "free": {"checkin", "certificates", "regform", "reviews", "threads"},
}
EXPECTED_FEATURES["starter"] = EXPECTED_FEATURES["free"] | {"tickets", "website"}
EXPECTED_FEATURES["growth"] = EXPECTED_FEATURES["starter"] | {"quiz", "polls", "qa", "networking", "links", "report"}
EXPECTED_FEATURES["pro"] = EXPECTED_FEATURES["growth"] | {"merch", "swag", "badges", "roles", "teams", "judging", "rounds"}
EXPECTED_FEATURES["campus"] = EXPECTED_FEATURES["pro"] | {"finance"}
EXPECTED_FEATURES["enterprise"] = EXPECTED_FEATURES["campus"]


async def test_the_seeded_catalogue_as_the_pricing_page_reads_it(client, session):
    """The migration's seed, run on the suite's schema, read back through the
    public endpoint: the six draft plans exactly as the owner set them."""
    module = _migration()
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            return module.seed_plans()

    assert await connection.run_sync(seed) == ["free", "starter", "growth", "pro", "campus", "enterprise"]

    body = (await client.get(f"{API}/plans")).json()

    assert body["currency"] == "INR" and body["yearly_months_free"] == 2
    plans = {plan["key"]: plan for plan in body["plans"]}
    assert list(plans) == ["free", "starter", "growth", "pro", "campus", "enterprise"]
    assert {k: (p["name"], p["price_monthly_paise"], p["price_yearly_paise"], p["trial_days"]) for k, p in plans.items()} == {
        "free": ("Free", 0, 0, 0),
        "starter": ("Starter", 29900, 299000, 0),
        "growth": ("Growth", 69900, 699000, 14),
        "pro": ("Pro", 149900, 1499000, 14),
        "campus": ("Campus", 399900, 3999000, 0),
        "enterprise": ("Enterprise", 0, 0, 0),
    }
    assert plans["free"]["tagline"] == "For a club just starting out."
    assert plans["enterprise"]["tagline"] == "For large fests and institutions."
    assert [k for k, p in plans.items() if p["featured"]] == ["growth"]
    assert [k for k, p in plans.items() if p["contact_sales"]] == ["enterprise"]
    for key, plan in plans.items():
        assert plan["limits"] == dict(zip(LIMIT_ORDER, EXPECTED_LIMITS[key])), key
        assert {f["key"] for f in plan["features"] if f["included"]} == EXPECTED_FEATURES[key], key
        # Every plan lists the same rows, so the comparison table lines up.
        assert [f["key"] for f in plan["features"]] == [f["key"] for f in plans["free"]["features"]]

    # And the admin screen agrees: billable needs a Razorpay plan for the paid ones.
    from app.api.subscriptions import service

    rows = {plan.key: plan for plan, _ in await service.list_plans(session)}
    from app.api.subscriptions.schemas import plan_to_dict

    billable = {key: plan_to_dict(plan)["billable"] for key, plan in rows.items()}
    assert billable == {"free": True, "starter": False, "growth": False, "pro": False, "campus": False, "enterprise": True}


async def test_the_seed_keeps_what_an_admin_made_and_undoes_cleanly(session):
    """The real upgrade, seed-again and downgrade, on the subscription tables
    as production has them (built by their own migration), in a schema of
    their own inside the test's transaction."""
    subscriptions = _migration("add_subscriptions")
    module = _migration()
    assert module.down_revision == "discovery_polish"
    connection = await session.connection()

    def run(sync):
        sync.execute(text("CREATE SCHEMA club_plans_migration"))
        sync.execute(text("SET LOCAL search_path TO club_plans_migration"))
        sync.execute(text("CREATE TABLE clubs (id serial PRIMARY KEY, name text)"))
        sync.execute(text("INSERT INTO clubs (id, name) VALUES (1, 'Subscribed')"))
        context = MigrationContext.configure(connection=sync)
        with Operations.context(context):
            subscriptions.upgrade()
            # An admin made `growth` before the seed: different price, name,
            # limits and features. And `campus` was made and then deleted.
            sync.execute(text(
                "INSERT INTO plans (id, key, name, tagline, price_monthly_paise, price_yearly_paise, visibility, "
                "created_at, updated_at) VALUES (100, 'growth', 'Growth (ours)', 'Edited', 50000, 500000, 'hidden', now(), now())"
            ))
            sync.execute(text("INSERT INTO plan_limits (plan_id, events_per_month, created_at, updated_at) VALUES (100, 99, now(), now())"))
            sync.execute(text("INSERT INTO plan_features (plan_id, sso, created_at, updated_at) VALUES (100, true, now(), now())"))
            sync.execute(text(
                "INSERT INTO plans (id, key, name, visibility, is_deleted, created_at, updated_at) "
                "VALUES (101, 'campus', 'Old campus', 'public', true, now(), now())"
            ))

            module.upgrade()

            def snapshot():
                return {
                    row.key: tuple(row)[1:]
                    for row in sync.execute(text(
                        "SELECT p.key, p.name, p.tagline, p.price_monthly_paise, p.price_yearly_paise, p.trial_days, "
                        "p.visibility::text, p.sort_order, p.featured, p.contact_sales, p.is_deleted, "
                        "l.events_per_month, l.website_templates, f.tickets, f.finance, f.sso "
                        "FROM plans p LEFT JOIN plan_limits l ON l.plan_id = p.id LEFT JOIN plan_features f ON f.plan_id = p.id"
                    ))
                }

            def counts():
                return tuple(
                    sync.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                    for table in ("plans", "plan_limits", "plan_features")
                )

            first = snapshot()
            first_counts = counts()

            # Running the seed again inserts nothing; an edit since stays.
            sync.execute(text("UPDATE plans SET price_monthly_paise = 19900, name = 'Starter (edited)' WHERE key = 'starter'"))
            again = module.seed_plans()
            second = snapshot()
            second_counts = counts()

            # A club on `pro`, and a cancelled one long ago on `free`.
            pro_id = sync.execute(text("SELECT id FROM plans WHERE key = 'pro'")).scalar()
            free_id = sync.execute(text("SELECT id FROM plans WHERE key = 'free'")).scalar()
            sync.execute(text(
                "INSERT INTO club_subscriptions (club_id, plan_id, created_at, updated_at) VALUES (1, :pro, now(), now())"
            ), {"pro": pro_id})
            sync.execute(text(
                "INSERT INTO club_subscriptions (club_id, plan_id, status, is_deleted, created_at, updated_at) "
                "VALUES (1, :free, 'cancelled', true, now(), now())"
            ), {"free": free_id})

            module.downgrade()
            left = {r[0] for r in sync.execute(text("SELECT key FROM plans"))}
            orphans = sync.execute(text(
                "SELECT count(*) FROM plan_limits l WHERE NOT EXISTS (SELECT 1 FROM plans p WHERE p.id = l.plan_id)"
            )).scalar()
            columns = {r[0] for r in sync.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'club_plans_migration' "
                "AND column_name IN ('featured', 'contact_sales', 'requested_plan', 'requested_cycle')"
            ))}

            module.upgrade()
            after = {r[0] for r in sync.execute(text("SELECT key FROM plans WHERE NOT is_deleted"))}
        sync.execute(text("SET LOCAL search_path TO public"))
        return first, first_counts, again, second, second_counts, left, orphans, columns, after

    first, first_counts, again, second, second_counts, left, orphans, columns, after = await connection.run_sync(run)

    # The admin's growth is untouched: name, price, hidden, its own limits
    # and flags, and not featured -- the seed never writes over a plan.
    assert first["growth"] == ("Growth (ours)", "Edited", 50000, 500000, 0, "hidden", 0, False, False, False, 99, None, False, False, True)
    # The deleted campus stays deleted and is not seeded over.
    assert first["campus"][0] == "Old campus" and first["campus"][9] is True
    # The four new ones, as drafted.
    assert first["free"] == ("Free", "For a club just starting out.", 0, 0, 0, "public", 0, False, False, False, 2, 1, False, False, False)
    assert first["starter"] == ("Starter", "For a club running regular events.", 29900, 299000, 0, "public", 1, False, False, False, 5, 3, True, False, False)
    assert first["pro"] == ("Pro", "For big clubs and paid events at scale.", 149900, 1499000, 14, "public", 3, False, False, False, None, None, True, False, False)
    assert first["enterprise"] == ("Enterprise", "For large fests and institutions.", 0, 0, 0, "public", 5, False, True, False, None, None, True, True, False)
    # Six plans; the deleted campus never had limits or features of its own.
    assert first_counts == (6, 5, 5)

    assert again == []
    assert second_counts == first_counts
    assert second["starter"][:3] == ("Starter (edited)", "For a club running regular events.", 19900)
    assert {k: v for k, v in second.items() if k != "starter"} == {k: v for k, v in first.items() if k != "starter"}

    # Downgrade keeps any plan a subscription points at, live or not.
    assert left == {"pro", "free"}
    assert orphans == 0
    assert columns == set()
    # And upgrades again over what it left.
    assert after == {"free", "starter", "growth", "pro", "campus", "enterprise"}


# ─── Applying for a club ─────────────────────────────────────────────────


async def test_an_application_records_the_plan_it_asked_for(client, session):
    from app.api.clubs.models import Clubs
    from app.api.clubs.schemas import ClubCreateUpdateResponse

    await make_plan(session, key="growth", monthly=69900, yearly=699000)

    response = await client.post(f"{API}/clubs/create", data=_apply({
        "email": "asks-growth@example.com", "name": "Asks Growth",
        "requested_plan": " Growth ", "requested_cycle": "YEARLY",
    }))

    assert response.status_code == 200, response.text
    # The same answer as before plans: nothing new in it.
    assert set(response.json()) == set(ClubCreateUpdateResponse.model_fields)
    club = await session.get(Clubs, response.json()["id"])
    await session.refresh(club)
    assert (club.requested_plan, club.requested_cycle) == ("growth", "yearly")
    assert club.is_verified is False


@pytest.mark.parametrize(
    "plan,cycle,expected",
    [
        ("nonsense", "monthly", (None, None)),
        ("bespoke", "monthly", (None, None)),  # hidden: not on the pricing page
        ("withdrawn", "monthly", (None, None)),  # legacy: no new sign-ups
        ("gone", "monthly", (None, None)),  # deleted
        ("starter", "fortnightly", ("starter", None)),
        ("starter", None, ("starter", None)),
        (None, "yearly", (None, None)),  # a cycle of nothing
        ("x" * 200, "monthly", (None, None)),
    ],
    ids=["unknown", "hidden", "legacy", "deleted", "bad-cycle", "no-cycle", "cycle-only", "too-long"],
)
async def test_a_plan_that_is_not_on_the_page_is_dropped_not_refused(client, session, plan, cycle, expected):
    from app.api.clubs.models import Clubs

    await make_plan(session, key="starter", monthly=29900, yearly=299000)
    await make_plan(session, key="bespoke", visibility="hidden")
    await make_plan(session, key="withdrawn", visibility="legacy")
    await make_plan(session, key="gone", deleted=True)

    form = {"email": "drops@example.com", "name": "Drops Plan"}
    if plan is not None:
        form["requested_plan"] = plan
    if cycle is not None:
        form["requested_cycle"] = cycle
    response = await client.post(f"{API}/clubs/create", data=_apply(form))

    assert response.status_code == 200, response.text
    club = await session.get(Clubs, response.json()["id"])
    await session.refresh(club)
    assert (club.requested_plan, club.requested_cycle) == expected


async def test_applications_are_limited_per_caller_each_hour(client):
    from app.api.clubs.router import APPLICATION_LIMIT

    assert APPLICATION_LIMIT == "5/hour;20/day"
    for i in range(5):
        response = await client.post(f"{API}/clubs/create", data=_apply({"email": f"hourly{i}@example.com", "name": f"Hourly {i}"}))
        assert response.status_code == 200, response.text

    sixth = await client.post(f"{API}/clubs/create", data=_apply({"email": "hourly5@example.com", "name": "Hourly 5"}))
    assert sixth.status_code == 429, sixth.text

    # A different caller has their own budget.
    other = await client.post(
        f"{API}/clubs/create",
        data=_apply({"email": "elsewhere@example.com", "name": "Elsewhere"}),
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert other.status_code == 200, other.text


async def test_applications_are_limited_per_caller_each_day(client, monkeypatch):
    """Twenty a day even when spread an hour apart. The limiter's clock is
    moved forward rather than waited on. A refused application (a taken
    name) still counts: it reached the endpoint."""
    import limits.storage.memory as memory

    clock = SimpleNamespace(now=1_000_000.0)
    clock.time = lambda: clock.now
    monkeypatch.setattr(memory, "time", clock)

    def apply():
        return client.post(f"{API}/clubs/create", data=_apply({"email": "daily@example.com", "name": "Daily Club"}))

    codes = []
    for hour in range(4):
        for _ in range(5):
            codes.append((await apply()).status_code)
        clock.now += 3601
    # One application went through; the rest were refused by the handler,
    # not the limiter.
    assert codes[0] == 200 and set(codes[1:]) == {400}

    # A fresh hour, but the day's twenty are spent.
    assert (await apply()).status_code == 429
    clock.now += 86400
    assert (await apply()).status_code == 400


async def test_the_admin_and_the_approved_club_see_what_it_asked_for(client, session, make_admin, mint_token):
    from app.api.clubs.models import Clubs

    await make_plan(session, key="pro", monthly=149900, yearly=1499000)
    applied = await client.post(f"{API}/clubs/create", data=_apply({
        "email": "wants-pro@example.com", "name": "Wants Pro", "requested_plan": "pro", "requested_cycle": "monthly",
    }))
    assert applied.status_code == 200, applied.text
    club_id = applied.json()["id"]
    admin_header = auth_header(await mint_token(await make_admin()))

    queue = await client.get(f"{API}/clubs/admin/list", params={"is_verified": False}, headers=admin_header)
    row = next(item for item in queue.json()["items"] if item["id"] == club_id)
    assert (row["requested_plan"], row["requested_cycle"]) == ("pro", "monthly")

    detail = await client.get(f"{API}/clubs/admin/{club_id}", headers=admin_header)
    assert detail.status_code == 200, detail.text
    assert (detail.json()["requested_plan"], detail.json()["requested_cycle"]) == ("pro", "monthly")

    approved = await client.post(f"{API}/clubs/{club_id}/approve", headers=admin_header)
    assert approved.status_code == 200, approved.text
    assert (approved.json()["requested_plan"], approved.json()["requested_cycle"]) == ("pro", "monthly")

    # The club's dashboard, signed in after approval, can preselect it.
    club = await session.get(Clubs, club_id)
    await session.refresh(club, attribute_names=["user"])
    mine = await client.get(f"{API}/clubs/entitlements", headers=auth_header(await mint_token(club.user)))
    assert mine.status_code == 200, mine.text
    assert (mine.json()["requested_plan"], mine.json()["requested_cycle"]) == ("pro", "monthly")


async def test_a_club_that_asked_for_nothing_shows_nothing(client, make_admin, mint_token):
    applied = await client.post(f"{API}/clubs/create", data=_apply({"email": "plain@example.com", "name": "Plain Club"}))
    club_id = applied.json()["id"]
    admin_header = auth_header(await mint_token(await make_admin()))

    detail = (await client.get(f"{API}/clubs/admin/{club_id}", headers=admin_header)).json()
    assert detail["requested_plan"] is None and detail["requested_cycle"] is None


# ─── The admin editor ────────────────────────────────────────────────────


@pytest.fixture
def plan_payload():
    def _build(**overrides):
        payload = {
            "key": "growth",
            "name": "Growth",
            "tagline": "For an active club with a following.",
            "price_monthly_paise": 69900,
            "price_yearly_paise": 699000,
            "trial_days": 14,
            "visibility": "public",
            "sort_order": 2,
            "limits": {"events_per_month": 15},
            "features": {"tickets": True},
        }
        payload.update(overrides)
        return payload

    return _build


async def test_an_admin_can_feature_a_plan_and_price_one_on_request(client, make_admin, mint_token, plan_payload):
    header = auth_header(await mint_token(await make_admin()))

    created = await client.post(f"{API}/admin/plans", json=plan_payload(featured=True), headers=header)
    assert created.status_code == 200, created.text
    assert (created.json()["featured"], created.json()["contact_sales"]) == (True, False)

    enterprise = await client.post(
        f"{API}/admin/plans",
        json=plan_payload(key="enterprise", name="Enterprise", price_monthly_paise=0, price_yearly_paise=0, contact_sales=True),
        headers=header,
    )
    assert enterprise.status_code == 200, enterprise.text
    assert (enterprise.json()["featured"], enterprise.json()["contact_sales"]) == (False, True)

    listed = {p["key"]: p for p in (await client.get(f"{API}/admin/plans", headers=header)).json()}
    assert listed["growth"]["featured"] is True and listed["enterprise"]["contact_sales"] is True


async def test_a_plan_created_without_them_is_neither(client, make_admin, mint_token, plan_payload):
    header = auth_header(await mint_token(await make_admin()))
    created = (await client.post(f"{API}/admin/plans", json=plan_payload(), headers=header)).json()
    assert (created["featured"], created["contact_sales"]) == (False, False)


async def test_saving_from_todays_editor_keeps_the_highlight(client, make_admin, mint_token, plan_payload):
    """The admin dashboard sends the whole plan on every save and knows
    nothing of these two fields yet. Saving must not quietly clear them."""
    header = auth_header(await mint_token(await make_admin()))
    plan_id = (await client.post(
        f"{API}/admin/plans", json=plan_payload(featured=True, contact_sales=True), headers=header
    )).json()["id"]

    # Exactly the editor's payload today: no featured, no contact_sales.
    edited = await client.put(f"{API}/admin/plans/{plan_id}", json=plan_payload(tagline="Edited"), headers=header)
    assert edited.status_code == 200, edited.text
    assert (edited.json()["tagline"], edited.json()["featured"], edited.json()["contact_sales"]) == ("Edited", True, True)

    cleared = await client.put(
        f"{API}/admin/plans/{plan_id}", json=plan_payload(featured=False, contact_sales=False), headers=header
    )
    assert (cleared.json()["featured"], cleared.json()["contact_sales"]) == (False, False)

    again = await client.put(f"{API}/admin/plans/{plan_id}", json=plan_payload(featured=True), headers=header)
    assert (again.json()["featured"], again.json()["contact_sales"]) == (True, False)


async def test_a_copy_keeps_contact_sales_but_not_the_highlight(client, make_admin, mint_token, plan_payload):
    header = auth_header(await mint_token(await make_admin()))
    plan_id = (await client.post(
        f"{API}/admin/plans", json=plan_payload(featured=True, contact_sales=True), headers=header
    )).json()["id"]

    copy = await client.post(f"{API}/admin/plans/{plan_id}/duplicate", headers=header)

    assert copy.status_code == 200, copy.text
    assert (copy.json()["featured"], copy.json()["contact_sales"], copy.json()["visibility"]) == (False, True, "hidden")


async def test_a_priced_on_request_plan_cannot_be_started_at_checkout(client, session, make_club, mint_token, monkeypatch):
    from app.api.subscriptions import billing

    def charged(**kwargs):
        raise AssertionError("checkout reached Razorpay for a priced-on-request plan")

    monkeypatch.setattr(billing, "start_subscription", charged)
    owner, _ = await make_club(name="Big Fest", email="bigfest@example.com")
    plan = await make_plan(session, key="enterprise", monthly=999900, yearly=9999000, contact_sales=True)

    response = await client.post(
        f"{API}/clubs/subscription/start",
        json={"plan_id": plan.id, "cycle": "monthly"},
        headers=auth_header(await mint_token(owner)),
    )

    assert response.status_code == 422, response.text
    assert "Talk to us" in response.text
