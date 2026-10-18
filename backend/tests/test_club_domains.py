"""A club's own domain for its website (app/api/club_domains).

The club types `www.theirclub.org`; it is made a Cloudflare for SaaS custom
hostname, pending until the club adds a CNAME and two TXT records; once
Cloudflare says it and its certificate are active, the site is served there,
the club is mailed and the admins alerted. A plan without `custom_domain`
pauses it (the Worker redirects to the club's subdomain); thirty days paused
removes it. Cloudflare is the in-memory fake in tests/fake_cloudflare.py.

Plans are the six the club_plans migration seeds, with this feature switched
on by the club_domains migration's own `grant`: the plans production has.
"""
import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import idna
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.admin_alerts.models import AdminNotifications
from app.api.audit.models import AdminAuditLogs
from app.api.auth.schemas import AuthTokenData
from app.api.club_access import policy
from app.api.club_access.models import ClubMembers
from app.api.club_domains import hosts, sweep
from app.api.club_domains.models import ClubDomains
from app.api.subscriptions import gates
from app.api.subscriptions import service as subs
from app.api.subscriptions.models import FEATURE_CATALOG, UNBUILT_FEATURES, BillingCycle, Plans
from app.api.users.models import AdminRoles
from app.config import settings
from app.core import after_commit
from app.core.auth.jwt import create_access_token
from app.response import CustomHTTPException
from tests.conftest import auth_header
from tests.fake_cloudflare import install

API = "/api/v1"
D = f"{API}/clubs/site/domain"
RESOLVE = f"{API}/sites/resolve"
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
CNAME_TARGET = "sites.myotherapp.in"


# ─── The world ───────────────────────────────────────────────────────────


def _migration(name: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _enforced(monkeypatch):
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    gates.reset()
    yield
    gates.reset()


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


@pytest.fixture
def cloudflare(monkeypatch):
    return install(monkeypatch)


@pytest_asyncio.fixture
async def plans(session):
    seed, domains = _migration("club_plans"), _migration("club_domains")
    connection = await session.connection()

    def run(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            seed.seed_plans()
            domains.grant()

    await connection.run_sync(run)
    rows = (
        await session.execute(
            select(Plans).options(selectinload(Plans.limits), selectinload(Plans.features)).execution_options(populate_existing=True)
        )
    ).scalars().all()
    return {p.key: p for p in rows}


@pytest_asyncio.fixture
async def world(session, make_club, mint_token, plans, mail):
    owner, club = await make_club(name="Circuit Society", email="domains-owner@example.com")
    other_owner, other = await make_club(name="Robotics Guild", email="domains-other@example.com")
    await session.commit()
    return SimpleNamespace(
        owner=owner, club=club, other=other, plans=plans, mail=mail, slug=club.slug,
        h=auth_header(await mint_token(owner)), other_h=auth_header(await mint_token(other_owner)),
    )


async def on_plan(session, club, plan) -> None:
    await subs.assign_plan(session, club_id=club.id, plan=plan, cycle=BillingCycle.monthly)
    await session.commit()


async def connect(client, h, host: str) -> dict:
    r = await client.put(D, json={"host": host}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def live(client, session, w, cloudflare, host="www.circuitsociety.org", *, publish=True) -> dict:
    """The club on Growth, its site published, `host` connected and verified."""
    await on_plan(session, w.club, w.plans["growth"])
    if publish:
        assert (await client.post(f"{API}/clubs/site/publish", headers=w.h)).status_code == 200
    await connect(client, w.h, host)
    cloudflare.go_live(host)
    r = await client.post(f"{D}/check", headers=w.h)
    assert r.status_code == 200 and r.json()["domain"]["status"] == "active", r.text
    return r.json()


async def row_for(session, host: str) -> ClubDomains:
    return await session.scalar(
        select(ClubDomains).where(ClubDomains.host == host).order_by(ClubDomains.id.desc()).limit(1).execution_options(populate_existing=True)
    )


async def mails(subject: str, w) -> list[dict]:
    await after_commit.drain()
    return [m for m in w.mail if m["subject"] == subject]


# ─── The plan ────────────────────────────────────────────────────────────


async def test_growth_and_up_include_a_domain_of_their_own(plans):
    assert {key for key, plan in plans.items() if plan.features.custom_domain} == {"growth", "pro", "campus", "enterprise"}
    assert dict(FEATURE_CATALOG)["custom_domain"] == "Your own domain for the club website"
    assert "custom_domain" not in UNBUILT_FEATURES and "custom_domain" in gates.CLUB_WIDE_FEATURES


async def test_admins_switch_it_on_and_off_per_plan_like_any_flag(client, session, world, make_admin, mint_token, cloudflare):
    h = auth_header(await mint_token(await make_admin(email="domains-plans@example.com")))
    starter = world.plans["starter"]
    plan = (await client.get(f"{API}/admin/plans/{starter.id}", headers=h)).json()
    assert plan["features"]["custom_domain"] is False and "custom_domain" in plan["feature_groups"]["club"]
    await on_plan(session, world.club, starter)
    assert (await client.put(D, json={"host": "www.circuitsociety.org"}, headers=world.h)).status_code == 403

    body = {k: plan[k] for k in ("key", "name", "tagline", "price_monthly_paise", "price_yearly_paise", "trial_days", "visibility", "sort_order")}
    body.update(limits=plan["limits"], features={**plan["features"], "custom_domain": True})
    r = await client.put(f"{API}/admin/plans/{starter.id}", json=body, headers=h)
    assert r.status_code == 200 and r.json()["features"]["custom_domain"] is True, r.text
    gates.reset()
    await connect(client, world.h, "www.circuitsociety.org")


async def test_a_club_without_the_feature_sees_why_and_cannot_connect(client, world, cloudflare):
    r = await client.get(D, headers=world.h)
    assert r.status_code == 200
    assert r.json() == {"available": True, "entitled": False, "default_host": f"{world.slug}.myotherapp.in", "domain": None}
    r = await client.put(D, json={"host": "www.circuitsociety.org"}, headers=world.h)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error_code"] == "PLAN_FEATURE_LOCKED"
    assert body["errors"]["feature"] == "custom_domain" and body["errors"]["required_plan"] == "growth"
    assert body["message"] == "Your own domain for the club website is part of Growth and up."
    assert cloudflare.calls == []


async def test_without_cloudflare_settings_the_feature_is_unavailable(client, session, world):
    await on_plan(session, world.club, world.plans["growth"])
    r = await client.get(D, headers=world.h)
    assert r.json()["available"] is False and r.json()["entitled"] is True
    r = await client.put(D, json={"host": "www.circuitsociety.org"}, headers=world.h)
    assert r.status_code == 503 and r.json()["error_code"] == "DOMAINS_UNAVAILABLE"
    assert (await client.post(f"{D}/check", headers=world.h)).status_code == 503


# ─── Connect, check, live ────────────────────────────────────────────────


async def test_a_domain_is_connected_checked_and_goes_live(client, session, world, cloudflare):
    await on_plan(session, world.club, world.plans["growth"])
    assert (await client.post(f"{API}/clubs/site/publish", headers=world.h)).status_code == 200

    body = await connect(client, world.h, "  https://WWW.CircuitSociety.org/about ")
    assert body["available"] is True and body["entitled"] is True and body["default_host"] == f"{world.slug}.myotherapp.in"
    d = body["domain"]
    made = cloudflare.by_host("www.circuitsociety.org")
    assert d["host"] == "www.circuitsociety.org" and d["status"] == "pending" and d["ssl_status"] == "pending_validation"
    assert d["records"] == [
        {"type": "CNAME", "name": "www.circuitsociety.org", "value": CNAME_TARGET, "purpose": "route"},
        {"type": "TXT", "name": "_cf-custom-hostname.www.circuitsociety.org", "value": made["ownership_verification"]["value"], "purpose": "ownership"},
        {"type": "TXT", "name": "_acme-challenge.www.circuitsociety.org", "value": made["ssl"]["validation_records"][0]["txt_value"], "purpose": "ssl"},
    ]
    assert d["last_error"] == "custom hostname does not CNAME to this zone." and d["verified_at"] is None
    method, _, sent = cloudflare.calls[0]
    assert method == "POST" and sent == {"hostname": "www.circuitsociety.org", "ssl": {"method": "txt", "type": "dv", "settings": {"min_tls_version": "1.2"}}}
    assert (await client.get(D, headers=world.h)).json()["domain"] == d

    # The same domain again is nothing new.
    calls = len(cloudflare.calls)
    assert (await connect(client, world.h, "www.circuitsociety.org"))["domain"]["status"] == "pending"
    assert len(cloudflare.calls) == calls

    # Not live yet: Check asks Cloudflare, and asks it to look now.
    r = await client.post(f"{D}/check", headers=world.h)
    assert r.status_code == 200 and r.json()["domain"]["status"] == "pending"
    assert cloudflare.methods()[-2:] == ["GET", "PATCH"]
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).status_code == 404

    # The records are in.
    cloudflare.go_live("www.circuitsociety.org")
    r = await client.post(f"{D}/check", headers=world.h)
    d = r.json()["domain"]
    assert d["status"] == "active" and d["ssl_status"] == "active" and d["verified_at"] and d["last_error"] is None
    assert d["records"][0] == {"type": "CNAME", "name": "www.circuitsociety.org", "value": CNAME_TARGET, "purpose": "route"}
    assert cloudflare.methods()[-1] == "GET"  # live already: no nudge

    sent = await mails("Your site is live at www.circuitsociety.org", world)
    assert len(sent) == 1 and sent[0]["recipients"] == ["domains-owner@example.com"]
    assert "https://www.circuitsociety.org" in sent[0]["text"] and CNAME_TARGET in sent[0]["text"]
    alerts = (await session.scalars(select(AdminNotifications).where(AdminNotifications.kind == "club_domain_active"))).all()
    assert len(alerts) == 1 and alerts[0].club_id == world.club.id and alerts[0].data["host"] == "www.circuitsociety.org"

    # Checking again is quiet.
    await client.post(f"{D}/check", headers=world.h)
    assert len(await mails("Your site is live at www.circuitsociety.org", world)) == 1

    # The page now says it lives there.
    html = (await client.get(f"{API}/sites/{world.slug}")).text
    assert '<link rel="canonical" href="https://www.circuitsociety.org">' in html
    assert '<meta property="og:url" content="https://www.circuitsociety.org">' in html
    assert '"url": "https://www.circuitsociety.org"' in html
    assert (await client.get(f"{API}/clubs/site", headers=world.h)).json()["url"] == "https://www.circuitsociety.org"

    # What the Worker asks.
    r = await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})
    assert r.status_code == 200 and r.json() == {"club_slug": world.slug, "status": "active", "canonical_host": "www.circuitsociety.org"}
    assert "s-maxage=300" in r.headers["cache-control"] and "public" in r.headers["cache-control"]
    r = await client.get(RESOLVE, params={"host": f"{world.slug}.myotherapp.in"})
    assert r.json() == {"club_slug": world.slug, "status": "active", "canonical_host": "www.circuitsociety.org"}


async def test_the_certificates_record_is_fetched_when_cloudflare_is_slow_to_give_it(client, session, world, cloudflare):
    await on_plan(session, world.club, world.plans["growth"])
    cloudflare.ssl_records_at_once = False
    d = (await connect(client, world.h, "www.circuitsociety.org"))["domain"]
    assert [r["purpose"] for r in d["records"]] == ["route", "ownership", "ssl"]
    assert cloudflare.methods() == ["POST", "GET"]


async def test_a_new_domain_replaces_the_old_and_removing_it_clears_cloudflare(client, session, world, cloudflare):
    await live(client, session, world, cloudflare, "www.one-circuit.org")
    d = (await connect(client, world.h, "www.two-circuit.org"))["domain"]
    assert d["host"] == "www.two-circuit.org" and d["status"] == "pending"
    assert cloudflare.by_host("www.one-circuit.org") is None and cloudflare.by_host("www.two-circuit.org") is not None
    assert (await row_for(session, "www.one-circuit.org")).status == "removed"
    assert (await client.get(RESOLVE, params={"host": "www.one-circuit.org"})).status_code == 404
    # The page is back on its subdomain while the new one is pending.
    assert f'<link rel="canonical" href="https://{world.slug}.myotherapp.in">' in (await client.get(f"{API}/sites/{world.slug}")).text

    r = await client.delete(D, headers=world.h)
    assert r.status_code == 200 and r.json()["domain"] is None
    assert cloudflare.hostnames == {}
    assert (await client.get(D, headers=world.h)).json()["domain"] is None
    # Removing nothing is fine.
    assert (await client.delete(D, headers=world.h)).status_code == 200
    actions = (await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.action.like("club_domain.%")).order_by(AdminAuditLogs.id))).all()
    assert actions == ["club_domain.connect", "club_domain.connect", "club_domain.remove"]


async def test_what_is_refused_and_why(client, session, world, cloudflare):
    await on_plan(session, world.club, world.plans["growth"])
    await on_plan(session, world.other, world.plans["growth"])

    r = await client.put(D, json={"host": "circuit.myotherapp.in"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_HOST"
    r = await client.put(D, json={"host": "203.0.113.7"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_HOST"
    r = await client.put(D, json={"host": "circuitsociety.org"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "APEX_NOT_SUPPORTED"
    assert r.json()["message"] == "Use www.circuitsociety.org (or another subdomain) and forward circuitsociety.org to it at your registrar."

    # Another club's.
    await connect(client, world.other_h, "www.robotics.org")
    r = await client.put(D, json={"host": "https://WWW.Robotics.org"}, headers=world.h)
    assert r.status_code == 409 and r.json()["error_code"] == "HOST_TAKEN"

    # Cloudflare says no: its words, nothing kept.
    cloudflare.fail_next = (400, 1415, "Invalid custom hostname. Custom hostnames must be a valid domain.")
    r = await client.put(D, json={"host": "www.circuitsociety.org"}, headers=world.h)
    assert r.status_code == 502 and r.json()["error_code"] == "CLOUDFLARE_ERROR"
    assert r.json()["errors"] == {"cloudflare": "Invalid custom hostname. Custom hostnames must be a valid domain."}
    assert "Invalid custom hostname" in r.json()["message"]
    cloudflare.down = True
    r = await client.put(D, json={"host": "www.circuitsociety.org"}, headers=world.h)
    assert r.status_code == 502 and r.json()["errors"]["cloudflare"] == "Cloudflare could not be reached. Try again in a minute."
    cloudflare.down = False
    assert (await client.get(D, headers=world.h)).json()["domain"] is None

    # Nothing to check.
    r = await client.post(f"{D}/check", headers=world.h)
    assert r.status_code == 404 and r.json()["error_code"] == "NO_DOMAIN"

    # A hostname Cloudflare still has from a delete that failed is taken over, not refused.
    left = cloudflare.add("www.circuitsociety.org")
    d = (await connect(client, world.h, "www.circuitsociety.org"))["domain"]
    assert d["status"] == "pending" and (await row_for(session, "www.circuitsociety.org")).cf_hostname_id == left["id"]
    assert len(cloudflare.hostnames) == 2

    # Cloudflare down while checking: 502, the domain as it was.
    cloudflare.down = True
    r = await client.post(f"{D}/check", headers=world.h)
    assert r.status_code == 502 and r.json()["error_code"] == "CLOUDFLARE_ERROR"
    cloudflare.down = False
    assert (await client.get(D, headers=world.h)).json()["domain"]["status"] == "pending"


async def test_only_the_owner_and_admin_members_see_or_change_the_domain(client, session, world, cloudflare):
    for method in ("GET", "PUT", "DELETE"):
        assert policy.allows("admin", method, D) is True
        assert policy.allows("editor", method, D) is False
        assert policy.allows("door", method, D) is False
    assert policy.allows("editor", "POST", f"{D}/check") is False and policy.allows("admin", "POST", f"{D}/check") is True
    assert policy.allows("editor", "PATCH", f"{API}/clubs/site") is True  # the rest of the website stays the editors'
    assert policy.can("admin")["site_domain"] is True and policy.can("editor")["site_domain"] is False
    assert policy.can("owner")["site_domain"] is True

    await on_plan(session, world.club, world.plans["growth"])
    await session.refresh(world.owner)

    async def member(role: str) -> dict:
        m = ClubMembers(club_id=world.club.id, email=f"{role}@circuit.example", role=role, status="active")
        session.add(m)
        await session.commit()
        token = create_access_token(data=AuthTokenData(
            user_id=world.owner.id, token_type="access_token", token_version=world.owner.token_version, member_id=m.id,
        ).model_dump())
        return auth_header(token)

    editor, admin = await member("editor"), await member("admin")
    r = await client.get(D, headers=editor)
    assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await client.put(D, json={"host": "www.circuitsociety.org"}, headers=editor)).status_code == 403
    assert (await client.get(D, headers=admin)).status_code == 200
    assert (await connect(client, admin, "www.circuitsociety.org"))["domain"]["status"] == "pending"


# ─── The sweep ───────────────────────────────────────────────────────────


async def test_the_sweep_makes_a_pending_domain_live_once(client, session, world, cloudflare):
    await on_plan(session, world.club, world.plans["growth"])
    assert (await client.post(f"{API}/clubs/site/publish", headers=world.h)).status_code == 200
    await connect(client, world.h, "www.circuitsociety.org")

    done = await sweep.run_sweep(session)
    assert done["polled"] == 1 and (await row_for(session, "www.circuitsociety.org")).status == "pending"

    cloudflare.go_live("www.circuitsociety.org")
    await sweep.run_sweep(session)
    row = await row_for(session, "www.circuitsociety.org")
    assert row.status == "active" and row.verified_at is not None and row.checked_at is not None
    assert len(await mails("Your site is live at www.circuitsociety.org", world)) == 1
    assert '<link rel="canonical" href="https://www.circuitsociety.org">' in (await client.get(f"{API}/sites/{world.slug}")).text

    # Checked a moment ago: the next runs leave it alone, and say nothing again.
    calls = len(cloudflare.calls)
    await sweep.run_sweep(session)
    assert len(cloudflare.calls) == calls
    assert len(await mails("Your site is live at www.circuitsociety.org", world)) == 1


async def test_a_domain_never_verified_fails_after_seven_days_and_frees_the_host(client, session, world, cloudflare):
    await on_plan(session, world.club, world.plans["growth"])
    await on_plan(session, world.other, world.plans["growth"])
    await connect(client, world.h, "www.slow-circuit.org")
    row = await row_for(session, "www.slow-circuit.org")
    row.created_at = datetime.now(timezone.utc) - timedelta(days=6)
    await session.commit()
    await sweep.run_sweep(session)
    assert (await row_for(session, "www.slow-circuit.org")).status == "pending"

    row.created_at = datetime.now(timezone.utc) - timedelta(days=7, hours=1)
    await session.commit()
    await sweep.run_sweep(session)
    row = await row_for(session, "www.slow-circuit.org")
    assert row.status == "failed" and "within 7 days" in row.last_error and row.cf_hostname_id is None
    assert cloudflare.by_host("www.slow-circuit.org") is None
    d = (await client.get(D, headers=world.h)).json()["domain"]
    assert d["status"] == "failed" and "within 7 days" in d["last_error"]

    # A failed domain holds nothing: whoever really owns it can connect it.
    assert (await connect(client, world.other_h, "www.slow-circuit.org"))["domain"]["status"] == "pending"


async def test_a_live_domain_is_rechecked_daily_and_fails_if_cloudflare_drops_it(client, session, world, cloudflare):
    await live(client, session, world, cloudflare)
    row = await row_for(session, "www.circuitsociety.org")
    cloudflare.set("www.circuitsociety.org", ssl_status="pending_validation", errors=["custom hostname does not CNAME to this zone."])
    row.checked_at = datetime.now(timezone.utc) - timedelta(hours=25)
    await session.commit()
    assert (await sweep.run_sweep(session))["rechecked"] == 1
    row = await row_for(session, "www.circuitsociety.org")
    # Still served, with what is wrong said where the club looks.
    assert row.status == "active" and row.ssl_status == "pending_validation" and row.last_error == "custom hostname does not CNAME to this zone."

    cloudflare.drop("www.circuitsociety.org")
    row.checked_at = datetime.now(timezone.utc) - timedelta(days=2)
    await session.commit()
    await sweep.run_sweep(session)
    row = await row_for(session, "www.circuitsociety.org")
    assert row.status == "failed" and "no longer has this domain" in row.last_error
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).status_code == 404
    assert f'<link rel="canonical" href="https://{world.slug}.myotherapp.in">' in (await client.get(f"{API}/sites/{world.slug}")).text


async def test_a_domain_cloudflare_blocks_fails(client, session, world, cloudflare):
    await on_plan(session, world.club, world.plans["growth"])
    await connect(client, world.h, "www.circuitsociety.org")
    cloudflare.set("www.circuitsociety.org", status="blocked", errors=["This hostname is on a block list."])
    await sweep.run_sweep(session)
    row = await row_for(session, "www.circuitsociety.org")
    assert row.status == "failed" and row.last_error.startswith("Cloudflare blocked www.circuitsociety.org")
    assert cloudflare.by_host("www.circuitsociety.org") is None


# ─── The plan changes ────────────────────────────────────────────────────


async def test_a_downgrade_pauses_the_domain_and_an_upgrade_brings_it_back(client, session, world, cloudflare):
    await live(client, session, world, cloudflare)
    await on_plan(session, world.club, world.plans["free"])
    assert (await sweep.run_sweep(session))["synced"] == 1
    row = await row_for(session, "www.circuitsociety.org")
    assert row.status == "paused" and row.paused_at is not None
    assert cloudflare.by_host("www.circuitsociety.org") is not None  # kept, for an upgrade

    sub = f"{world.slug}.myotherapp.in"
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).json() == {"club_slug": world.slug, "status": "paused", "canonical_host": sub}
    assert (await client.get(RESOLVE, params={"host": sub})).json() == {"club_slug": world.slug, "status": "active", "canonical_host": sub}
    assert f'<link rel="canonical" href="https://{sub}">' in (await client.get(f"{API}/sites/{world.slug}")).text
    body = (await client.get(D, headers=world.h)).json()
    assert body["entitled"] is False and body["domain"]["status"] == "paused"
    # Connecting another is refused on Free; removing it never is.
    assert (await client.put(D, json={"host": "www.other-circuit.org"}, headers=world.h)).status_code == 403

    await on_plan(session, world.club, world.plans["pro"])
    assert (await sweep.run_sweep(session))["synced"] == 1
    row = await row_for(session, "www.circuitsociety.org")
    assert row.status == "active" and row.paused_at is None
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).json()["status"] == "active"
    assert '<link rel="canonical" href="https://www.circuitsociety.org">' in (await client.get(f"{API}/sites/{world.slug}")).text
    # Back, not new: the club is not told its site went live a second time.
    assert len(await mails("Your site is live at www.circuitsociety.org", world)) == 1


async def test_a_paused_domain_never_points_back_at_itself(client, session, world, cloudflare):
    """A slug too long to be a subdomain has none to fall back to: the public site."""
    world.club.slug = "a" * 70
    await session.commit()
    await live(client, session, world, cloudflare, publish=False)
    await on_plan(session, world.club, world.plans["free"])
    await sweep.run_sweep(session)
    answer = (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).json()
    assert answer == {"club_slug": "a" * 70, "status": "paused", "canonical_host": "myotherapp.in"}


async def test_check_brings_a_paused_domain_back_after_an_upgrade_without_waiting(client, session, world, cloudflare):
    await live(client, session, world, cloudflare)
    await on_plan(session, world.club, world.plans["starter"])
    await sweep.run_sweep(session)
    assert (await row_for(session, "www.circuitsociety.org")).status == "paused"
    await on_plan(session, world.club, world.plans["growth"])
    gates.reset()
    assert (await client.post(f"{D}/check", headers=world.h)).json()["domain"]["status"] == "active"


async def test_thirty_days_paused_removes_the_domain_and_tells_the_club(client, session, world, cloudflare):
    await live(client, session, world, cloudflare)
    await on_plan(session, world.club, world.plans["free"])
    await sweep.run_sweep(session)
    row = await row_for(session, "www.circuitsociety.org")
    row.paused_at = datetime.now(timezone.utc) - timedelta(days=29)
    await session.commit()
    await sweep.run_sweep(session)
    assert (await row_for(session, "www.circuitsociety.org")).status == "paused"

    row.paused_at = datetime.now(timezone.utc) - timedelta(days=30, hours=1)
    await session.commit()
    assert (await sweep.run_sweep(session))["removed"] == 1
    row = await row_for(session, "www.circuitsociety.org")
    assert row.status == "removed" and row.removed_at is not None
    assert cloudflare.by_host("www.circuitsociety.org") is None
    sent = await mails("www.circuitsociety.org is no longer connected to your club's site", world)
    assert len(sent) == 1 and sent[0]["recipients"] == ["domains-owner@example.com"]
    assert f"https://{world.slug}.myotherapp.in" in sent[0]["text"]
    assert (await client.get(D, headers=world.h)).json()["domain"] is None
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).status_code == 404


# ─── The Worker's question ───────────────────────────────────────────────


async def test_the_resolver_knows_live_domains_and_real_clubs_only(client, session, world, cloudflare):
    sub = f"{world.slug}.myotherapp.in"
    for host in (sub, sub.upper(), f"{sub}.", f"{sub}:443"):
        r = await client.get(RESOLVE, params={"host": host})
        assert r.status_code == 200 and r.json() == {"club_slug": world.slug, "status": "active", "canonical_host": sub}, host
    for host in ("www.nobody.org", "nobody-here.myotherapp.in", "a.b.myotherapp.in", "myotherapp.in", ""):
        r = await client.get(RESOLVE, params={"host": host})
        assert r.status_code == 404, host
        assert "s-maxage=60" in r.headers["cache-control"]

    # Pending is not served.
    await on_plan(session, world.club, world.plans["growth"])
    await connect(client, world.h, "www.circuitsociety.org")
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).status_code == 404

    # A deleted club's domain is nobody's.
    cloudflare.go_live("www.circuitsociety.org")
    await client.post(f"{D}/check", headers=world.h)
    assert (await client.get(RESOLVE, params={"host": "WWW.CircuitSociety.org"})).status_code == 200
    world.club.is_deleted = True
    await session.commit()
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).status_code == 404
    assert (await client.get(RESOLVE, params={"host": sub})).status_code == 404


# ─── Where links point ───────────────────────────────────────────────────


async def test_the_websites_own_link_and_the_directory_follow_the_active_domain(client, session, world, cloudflare):
    from app.api.discover.service import club_cards

    await on_plan(session, world.club, world.plans["growth"])
    assert (await client.post(f"{API}/clubs/site/publish", headers=world.h)).status_code == 200
    link = (await client.get(f"{API}/clubs/site/analytics", headers=world.h)).json()["link"]
    assert link["destination"].startswith(f"https://myotherapp.in/clubs/{world.slug}?")
    now = datetime.now(timezone.utc)
    cards = await club_cards(session, [world.club], now, since=now - timedelta(hours=72))
    assert cards[0]["website_url"] == f"https://{world.slug}.myotherapp.in"

    await connect(client, world.h, "www.circuitsociety.org")
    cloudflare.go_live("www.circuitsociety.org")
    await client.post(f"{D}/check", headers=world.h)

    link = (await client.get(f"{API}/clubs/site/analytics", headers=world.h)).json()["link"]
    assert link["destination"].startswith("https://www.circuitsociety.org/?utm_source=website&utm_medium=club_site")
    r = await client.get(f"/l/{link['code']}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"https://www.circuitsociety.org/?utm_source=website&utm_medium=club_site&ref={link['code']}&via=l"
    now = datetime.now(timezone.utc)
    cards = await club_cards(session, [world.club], now, since=now - timedelta(hours=72))
    assert cards[0]["website_url"] == "https://www.circuitsociety.org"
    # The club's other links still land on its page on the public site.
    made = await client.post(f"{API}/clubs/links", json={"name": "Bio", "utm_source": "insta", "utm_medium": "bio"}, headers=world.h)
    assert made.status_code in (200, 201), made.text
    assert made.json()["destination"].startswith(f"https://myotherapp.in/clubs/{world.slug}?")


# ─── The admins ──────────────────────────────────────────────────────────


async def test_admins_list_recheck_and_remove_domains(client, session, world, cloudflare, make_admin, mint_token):
    await live(client, session, world, cloudflare, publish=False)
    await on_plan(session, world.other, world.plans["growth"])
    await connect(client, world.other_h, "www.robotics.org")
    owner = auth_header(await mint_token(await make_admin(email="domains-admin@example.com")))
    support = auth_header(await mint_token(await make_admin(email="domains-support@example.com", role=AdminRoles.support)))
    viewer = auth_header(await mint_token(await make_admin(email="domains-viewer@example.com", role=AdminRoles.viewer)))
    A = f"{API}/admin/club-domains"

    page = (await client.get(A, headers=viewer)).json()
    assert page["total"] == 2 and [i["host"] for i in page["items"]] == ["www.robotics.org", "www.circuitsociety.org"]
    item = page["items"][1]
    assert item["club"] == {"id": world.club.id, "name": "Circuit Society", "slug": world.slug}
    assert item["status"] == "active" and item["ssl_status"] == "active" and item["verified_at"] and item["created_at"]
    assert [i["host"] for i in (await client.get(A, params={"status": "pending"}, headers=viewer)).json()["items"]] == ["www.robotics.org"]
    assert (await client.get(A, params={"q": "Robotics"}, headers=viewer)).json()["total"] == 1
    assert (await client.get(A, params={"q": "circuitsociety"}, headers=viewer)).json()["total"] == 1
    assert (await client.get(A, params={"status": "lost"}, headers=viewer)).status_code == 422
    assert (await client.get(A, headers=world.h)).status_code in (401, 403)

    pending_id = page["items"][0]["id"]
    # Rechecking only reads Cloudflare again: any admin. Removing needs moderate.
    r = await client.post(f"{A}/{pending_id}/recheck", headers=viewer)
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["club"]["slug"] == world.other.slug, r.text
    assert (await client.delete(f"{A}/{pending_id}", headers=viewer)).status_code == 403
    assert (await client.post(f"{A}/{pending_id}/recheck", headers=world.h)).status_code in (401, 403)

    cloudflare.go_live("www.robotics.org")
    r = await client.post(f"{A}/{pending_id}/recheck", headers=viewer)
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    assert r.json()["host"] == "www.robotics.org" and r.json()["ssl_status"] == "active"
    r = await client.delete(f"{A}/{pending_id}", headers=support)
    assert r.status_code == 200 and r.json()["status"] == "removed"

    r = await client.delete(f"{A}/{item['id']}", headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "removed"
    assert cloudflare.by_host("www.circuitsociety.org") is None
    assert (await client.get(RESOLVE, params={"host": "www.circuitsociety.org"})).status_code == 404
    assert (await client.get(A, params={"status": "removed"}, headers=viewer)).json()["total"] == 2
    assert (await client.get(A, headers=viewer)).json()["total"] == 0
    logged = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_domain.admin_remove").order_by(AdminAuditLogs.id))).all()
    assert [(row.entity_id, row.details["host"]) for row in logged] == [
        (str(world.other.id), "www.robotics.org"), (str(world.club.id), "www.circuitsociety.org"),
    ]
    assert (await client.delete(f"{A}/999999", headers=owner)).status_code == 404


# ─── What a club may type ────────────────────────────────────────────────
# Last in the file: the suite's database fixtures fail in a run whose first
# test touches no database (its engine is then made on another loop).


@pytest.mark.parametrize("raw, host", [
    ("www.theirclub.org", "www.theirclub.org"),
    ("  https://WWW.TheirClub.org/about?x=1#top ", "www.theirclub.org"),
    ("http://www.theirclub.org:8080/", "www.theirclub.org"),
    ("www.theirclub.org.", "www.theirclub.org"),
    ("events.cs.iitb.ac.in", "events.cs.iitb.ac.in"),
    ("club.example.co.uk", "club.example.co.uk"),
    ("www.myotherapp.in.theirclub.org", "www.myotherapp.in.theirclub.org"),
])
async def test_a_host_is_normalised(raw, host):
    assert hosts.normalise(raw) == host


async def test_an_international_domain_is_kept_in_punycode():
    host = hosts.normalise("https://WWW.Bücherclub.de/")
    assert host == idna.encode("www.bücherclub.de", uts46=True).decode() and host.startswith("www.xn--")


@pytest.mark.parametrize("raw, code", [
    ("theirclub.org", "APEX_NOT_SUPPORTED"),
    ("https://TheirClub.org/", "APEX_NOT_SUPPORTED"),
    ("iitb.ac.in", "APEX_NOT_SUPPORTED"),
    ("bücherclub.de", "APEX_NOT_SUPPORTED"),
    ("circuit.myotherapp.in", "INVALID_HOST"),
    ("myotherapp.in", "INVALID_HOST"),
    ("WWW.MYOTHERAPP.IN", "INVALID_HOST"),
    ("sites.myotherapp.in", "INVALID_HOST"),
    ("events.myotherapp.in.", "INVALID_HOST"),
    ("www.finnet.app", "INVALID_HOST"),
    ("203.0.113.9", "INVALID_HOST"),
    ("https://203.0.113.9:8443/", "INVALID_HOST"),
    ("[2001:db8::1]", "INVALID_HOST"),
    ("co.in", "INVALID_HOST"),
    ("org", "INVALID_HOST"),
    ("localhost", "INVALID_HOST"),
    ("*.theirclub.org", "INVALID_HOST"),
    ("www.their_club.org", "INVALID_HOST"),
    ("www..theirclub.org", "INVALID_HOST"),
    ("a" * 64 + ".theirclub.org", "INVALID_HOST"),
    ("https://user:pass@www.theirclub.org/", "INVALID_HOST"),
    ("   ", "INVALID_HOST"),
])
async def test_a_host_is_refused(raw, code):
    with pytest.raises(CustomHTTPException) as refused:
        hosts.normalise(raw)
    assert refused.value.status_code == 422 and refused.value.error_code == code


async def test_an_apex_is_refused_with_the_www_to_use_instead():
    with pytest.raises(CustomHTTPException) as refused:
        hosts.normalise("TheirClub.org")
    assert refused.value.message == "Use www.theirclub.org (or another subdomain) and forward theirclub.org to it at your registrar."
    assert refused.value.errors == {"host": "theirclub.org", "suggestion": "www.theirclub.org"}
