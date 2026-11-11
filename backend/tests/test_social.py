"""Social posting: connecting accounts, who may do what, the plans, drafts,
posters, previews, the share kit and Meta's callbacks (app/api/social).

Every platform is the fake in tests/fake_social.py; nothing reaches the
network (tests/conftest.py makes any other call fail).
"""
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.club_alerts.models import ClubAlerts
from app.api.links.models import TrackingLinks
from app.api.social import crypto
from app.api.social.models import SocialAccounts, SocialDataDeletions, SocialOAuthStates
from app.api.subscriptions import gates, service as subscriptions
from app.api.subscriptions.models import BillingCycle
from app.config import settings
from tests import fake_ai
from tests.conftest import auth_header
from tests.fake_social import META_APP_SECRET, TOKENS
from tests.social_helpers import API, DASHBOARD, account, connect, fake, files, jpeg, seed_plans, state_of, world  # noqa: F401

pytestmark = pytest.mark.asyncio


def _no_tokens(text: str) -> None:
    for t in TOKENS:
        assert t not in text, f"{t} leaked"


# ─── Platforms ───────────────────────────────────────────────────────────


async def test_every_platform_is_not_configured_without_its_settings(client, session, make_club, mint_token, monkeypatch):
    for name in ("SOCIAL_KEYS_SECRET", "META_APP_ID", "META_APP_SECRET", "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "X_CLIENT_ID", "X_CLIENT_SECRET"):
        monkeypatch.setattr(settings, name, "")
    owner, _club = await make_club(name="Quiet Club", email="quiet@example.com")
    await session.commit()
    h = auth_header(await mint_token(owner))
    r = await client.get(f"{API}/clubs/social/platforms", headers=h)
    assert r.status_code == 200, r.text
    assert {p["platform"]: (p["available"], p["reason"]) for p in r.json()} == {
        p: (False, "not_configured") for p in ("facebook", "instagram", "linkedin", "linkedin_page", "x")}
    r = await client.post(f"{API}/clubs/social/accounts/instagram/connect", json={"return_to": DASHBOARD}, headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "SOCIAL_PLATFORM_UNAVAILABLE"
    # A secret saved without the platform's app still says not configured.
    monkeypatch.setattr(settings, "SOCIAL_KEYS_SECRET", "ZmFrZS1rZXktZmFrZS1rZXktZmFrZS1rZXktZmFrZS0=")
    r = await client.get(f"{API}/clubs/social/platforms", headers=h)
    assert all(p["reason"] == "not_configured" for p in r.json())


async def test_with_settings_saved_each_platform_is_there_and_linkedin_pages_wait_for_review(client, world, monkeypatch):
    r = await client.get(f"{API}/clubs/social/platforms", headers=world.h)
    assert {p["platform"]: p["available"] for p in r.json()} == {p: True for p in ("facebook", "instagram", "linkedin", "linkedin_page", "x")}
    monkeypatch.setattr(settings, "LINKEDIN_PAGES_ENABLED", False)
    r = await client.get(f"{API}/clubs/social/platforms", headers=world.h)
    page = next(p for p in r.json() if p["platform"] == "linkedin_page")
    assert page == {"platform": "linkedin_page", "label": "LinkedIn Page", "available": False, "reason": "review_pending", "plan_feature": "social_autopost"}


# ─── Connecting ──────────────────────────────────────────────────────────


async def test_connecting_a_facebook_page_keeps_its_token_sealed_and_never_shows_it(client, session, world, caplog):
    caplog.set_level(logging.DEBUG)
    r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": DASHBOARD}, headers=world.h)
    url = urlsplit(r.json()["authorize_url"])
    q = parse_qs(url.query)
    assert url.netloc == "www.facebook.com" and url.path == "/v26.0/dialog/oauth"
    assert q["client_id"] == ["test-meta-app"] and q["scope"] == ["pages_show_list,pages_manage_posts,pages_read_engagement"]
    assert q["redirect_uri"] == ["https://api.myotherapp.in/api/v1/social/oauth/facebook/callback"]
    state = q["state"][0]
    back = await client.get(f"{API}/social/oauth/facebook/callback", params={"code": "abc", "state": state})
    assert back.status_code == 302
    where = urlsplit(back.headers["location"])
    assert f"{where.scheme}://{where.netloc}{where.path}" == DASHBOARD
    assert parse_qs(where.query)["social"] == ["connected"] and parse_qs(where.query)["platform"] == ["facebook"]
    listed = await client.get(f"{API}/clubs/social/accounts", headers=world.h)
    [a] = listed.json()
    assert a["platform"] == "facebook" and a["name"] == "Circuit Society" and a["picture_url"] == "https://fb.example/p1.jpg"
    assert a["status"] == "connected" and a["connected_by"] == {"id": None, "name": "Test User"} and a["waiting_posts"] == 0
    _no_tokens(listed.text + back.headers["location"] + r.text)
    row = await account(session, world.club.id, "facebook")
    assert row.access_token_enc and "PAGE-TOKEN-1" not in row.access_token_enc
    assert crypto.open_(row.access_token_enc) == "PAGE-TOKEN-1" and row.connector_external_id == "meta-user-1"
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "social.connect"))
    assert log is not None and log.entity_id == str(world.club.id)
    _no_tokens(json.dumps(log.details) + caplog.text)
    # The exchange went server to server, the long-lived token asked for, and the Page token used afterwards.
    assert [c.path for c in world.fake.calls][:2] == ["/v26.0/oauth/access_token", "/v26.0/oauth/access_token"]


async def test_a_state_that_was_tampered_with_or_used_once_is_refused(client, session, world):
    r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": DASHBOARD}, headers=world.h)
    state = state_of(r.json()["authorize_url"])
    sid, nonce, sig = state.split(".")
    for bad in (f"{sid}.{nonce}.{'A' * len(sig)}", f"{sid}.{nonce}x.{sig}", "nonsense", ""):
        back = await client.get(f"{API}/social/oauth/facebook/callback", params={"code": "abc", "state": bad})
        assert parse_qs(urlsplit(back.headers["location"]).query)["reason"] == ["invalid_state"], bad
    # Signed for Facebook, not for X.
    back = await client.get(f"{API}/social/oauth/x/callback", params={"code": "abc", "state": state})
    assert parse_qs(urlsplit(back.headers["location"]).query)["reason"] == ["invalid_state"]
    assert not world.fake.calls, "nothing was exchanged for a bad state"
    ok = await client.get(f"{API}/social/oauth/facebook/callback", params={"code": "abc", "state": state})
    assert parse_qs(urlsplit(ok.headers["location"]).query)["social"] == ["connected"]
    exchanges = len(world.fake.by("GET", r"/oauth/access_token$"))
    again = await client.get(f"{API}/social/oauth/facebook/callback", params={"code": "abc", "state": state})
    assert parse_qs(urlsplit(again.headers["location"]).query)["reason"] == ["invalid_state"]
    assert len(world.fake.by("GET", r"/oauth/access_token$")) == exchanges, "a replay exchanges nothing"


async def test_an_expired_state_and_a_refusal_go_back_with_a_reason(client, session, world):
    r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": DASHBOARD}, headers=world.h)
    state = state_of(r.json()["authorize_url"])
    row = await session.scalar(select(SocialOAuthStates))
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    back = await client.get(f"{API}/social/oauth/facebook/callback", params={"code": "abc", "state": state})
    assert parse_qs(urlsplit(back.headers["location"]).query)["reason"] == ["expired_state"]
    r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": DASHBOARD}, headers=world.h)
    back = await client.get(f"{API}/social/oauth/facebook/callback", params={"error": "access_denied", "state": state_of(r.json()["authorize_url"])})
    assert parse_qs(urlsplit(back.headers["location"]).query)["reason"] == ["denied"]
    assert not world.fake.calls


async def test_return_to_must_be_the_club_dashboard(client, world, monkeypatch):
    from app.core import config_checks

    monkeypatch.setattr(config_checks, "is_production", lambda s: True)
    r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": "https://evil.example/steal"}, headers=world.h)
    assert r.status_code == 422 and r.json()["error_code"] == "RETURN_TO_NOT_ALLOWED"
    r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": "http://testserver/x"}, headers=world.h)
    assert r.status_code == 422, "only the dashboard in production"


async def test_several_pages_wait_for_the_club_to_pick_one(client, session, world):
    world.fake.pages.append({"id": "page-2", "name": "Circuit Alumni", "access_token": "PAGE-TOKEN-2", "tasks": ["CREATE_CONTENT"]})
    q = await connect(client, world, "facebook")
    assert q["social"] == "choose" and q["platform"] == "facebook"
    pending = q["pending"]
    r = await client.get(f"{API}/clubs/social/accounts/pending/{pending}", headers=world.h)
    assert [c["external_id"] for c in r.json()] == ["page-1", "page-2"]
    _no_tokens(r.text)
    # Another club cannot see or take it, nor an editor.
    r = await client.get(f"{API}/clubs/social/accounts/pending/{pending}", headers=world.editor)
    assert r.status_code == 403
    r = await client.post(f"{API}/clubs/social/accounts/pending/{pending}", json={"external_id": "page-9"}, headers=world.h)
    assert r.status_code == 422
    r = await client.post(f"{API}/clubs/social/accounts/pending/{pending}", json={"external_id": "page-2"}, headers=world.h)
    assert r.status_code == 200 and r.json()["name"] == "Circuit Alumni"
    assert crypto.open_((await account(session, world.club.id, "facebook")).access_token_enc) == "PAGE-TOKEN-2"
    again = await client.post(f"{API}/clubs/social/accounts/pending/{pending}", json={"external_id": "page-1"}, headers=world.h)
    assert again.status_code == 404
    state = await session.get(SocialOAuthStates, pending)
    assert state.pending_enc is None, "the unpicked Pages' tokens are gone"


async def test_instagram_is_the_business_account_linked_to_a_page(client, session, world):
    q = await connect(client, world, "instagram")
    assert q["social"] == "connected"
    a = await account(session, world.club.id, "instagram")
    assert (a.external_id, a.handle) == ("ig-1", "circuitsoc") and crypto.open_(a.access_token_enc) == "PAGE-TOKEN-1"
    world.fake.pages[0].pop("instagram_business_account")
    await client.delete(f"{API}/clubs/social/accounts/{a.id}", headers=world.h)
    q = await connect(client, world, "instagram")
    assert q == {"social": "error", "platform": "instagram", "reason": "no_instagram_business"}


async def test_permissions_left_unticked_are_said(client, world):
    world.fake.granted = ["pages_show_list"]
    q = await connect(client, world, "facebook")
    assert q["reason"] == "missing_permissions"


async def test_x_signs_in_with_pkce(client, session, world):
    r = await client.post(f"{API}/clubs/social/accounts/x/connect", json={"return_to": DASHBOARD}, headers=world.h)
    q = parse_qs(urlsplit(r.json()["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"] and "offline.access" in q["scope"][0] and "media.write" in q["scope"][0]
    back = await client.get(f"{API}/social/oauth/x/callback", params={"code": "c", "state": q["state"][0]})
    assert parse_qs(urlsplit(back.headers["location"]).query)["social"] == ["connected"]
    [token] = world.fake.by("POST", r"/2/oauth2/token$")
    verifier = token.form["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == q["code_challenge"][0]
    assert token.headers["authorization"].startswith("Basic ")
    a = await account(session, world.club.id, "x")
    assert a.handle == "circuitsoc" and crypto.open_(a.refresh_token_enc) == "X-REFRESH-TOKEN" and a.token_expires_at is not None


async def test_linkedin_member_and_page(client, session, world):
    assert (await connect(client, world, "linkedin"))["social"] == "connected"
    a = await account(session, world.club.id, "linkedin")
    assert a.external_id == "li-member-9" and a.token_expires_at > datetime.now(timezone.utc) + timedelta(days=59)
    assert (await connect(client, world, "linkedin_page"))["social"] == "connected"
    page = await account(session, world.club.id, "linkedin_page")
    assert (page.external_id, page.name, page.handle) == ("777", "Circuit Society", "circuit-society")
    world.fake.li_orgs = []
    await client.delete(f"{API}/clubs/social/accounts/{page.id}", headers=world.h)
    assert (await connect(client, world, "linkedin_page"))["reason"] == "not_page_admin"


async def test_disconnecting_revokes_and_deletes_the_tokens(client, session, world):
    await connect(client, world, "x")
    a = await account(session, world.club.id, "x")
    r = await client.delete(f"{API}/clubs/social/accounts/{a.id}", headers=world.h)
    assert r.status_code == 200
    revoked = world.fake.by("POST", r"/2/oauth2/revoke$")
    assert {c.form["token_type_hint"] for c in revoked} == {"access_token", "refresh_token"}
    await session.refresh(a)
    assert a.status == "disconnected" and a.access_token_enc is None and a.refresh_token_enc is None
    assert (await client.get(f"{API}/clubs/social/accounts", headers=world.h)).json() == []
    assert await session.scalar(select(AdminAuditLogs.id).where(AdminAuditLogs.action == "social.disconnect")) is not None


async def test_meta_grant_is_revoked_only_when_nothing_else_rests_on_it(client, session, world):
    await connect(client, world, "facebook")
    await connect(client, world, "instagram")
    fb = await account(session, world.club.id, "facebook")
    await client.delete(f"{API}/clubs/social/accounts/{fb.id}", headers=world.h)
    assert not world.fake.by("DELETE", r"/permissions$"), "Instagram still rests on the same person's grant"
    ig = await account(session, world.club.id, "instagram")
    await client.delete(f"{API}/clubs/social/accounts/{ig.id}", headers=world.h)
    [revoke] = world.fake.by("DELETE", r"/meta-user-1/permissions$")
    assert revoke.params["access_token"] == "test-meta-app|test-meta-secret"


async def test_a_removed_member_takes_the_accounts_they_connected_with_them(client, session, world):
    await connect(client, world, "facebook", headers=world.admin)
    a = await account(session, world.club.id, "facebook")
    assert a.connected_by_member_id == world.team["admin"].id and a.connected_by_name == "Admin Person"
    r = await client.delete(f"{API}/clubs/access/{world.team['admin'].id}", headers=world.h)
    assert r.status_code == 200, r.text
    await session.refresh(a)
    assert a.status == "disconnected" and a.access_token_enc is None


# ─── Roles ───────────────────────────────────────────────────────────────


async def test_who_may_do_what(client, session, world):
    # Editors and door staff do not connect; door staff see nothing of it.
    for who in (world.editor, world.door):
        r = await client.post(f"{API}/clubs/social/accounts/facebook/connect", json={"return_to": DASHBOARD}, headers=who)
        assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN"
    for path in ("/clubs/social/accounts", f"/clubs/social/events/{world.event.id}/posts", "/clubs/social/platforms"):
        assert (await client.get(f"{API}{path}", headers=world.door)).status_code == 403
        assert (await client.get(f"{API}{path}", headers=world.editor)).status_code == 200
    assert (await client.put(f"{API}/clubs/social/settings", json={"approval_required": True}, headers=world.editor)).status_code == 403
    assert (await client.put(f"{API}/clubs/social/settings", json={"approval_required": True}, headers=world.admin)).json() == {"approval_required": True}
    assert (await connect(client, world, "facebook", headers=world.admin))["social"] == "connected"
    a = await account(session, world.club.id, "facebook")
    assert (await client.delete(f"{API}/clubs/social/accounts/{a.id}", headers=world.editor)).status_code == 403


async def test_with_approval_on_an_editors_post_waits_for_an_owner_or_admin(client, session, world):
    await client.put(f"{API}/clubs/social/settings", json={"approval_required": True}, headers=world.h)
    await connect(client, world, "facebook")
    a = await account(session, world.club.id, "facebook")
    body = {"name": "Reminder", "trigger": {"mode": "before_start", "minutes": 1440},
            "targets": [{"platform": "facebook", "account_id": str(a.id), "text": "{event_name} is tomorrow! {link}"}]}
    r = await client.post(f"{API}/clubs/social/events/{world.event.id}/posts", json=body, headers=world.editor)
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["status"] == "needs_approval" and post["created_by"]["name"] == "Editor Person"
    assert (await client.post(f"{API}/clubs/social/posts/{post['id']}/approve", headers=world.editor)).status_code == 403
    r = await client.post(f"{API}/clubs/social/posts/{post['id']}/approve", headers=world.admin)
    assert r.status_code == 200 and r.json()["status"] == "scheduled" and r.json()["approved_by"]["name"] == "Admin Person"
    assert await session.scalar(select(AdminAuditLogs.id).where(AdminAuditLogs.action == "social.approve")) is not None
    # The owner's own post needs no approval; it is approved by them.
    r = await client.post(f"{API}/clubs/social/events/{world.event.id}/posts", json={**body, "name": "Last call"}, headers=world.h)
    assert r.json()["status"] == "scheduled" and r.json()["approved_by"]["name"] == "Test User"


# ─── Plans ───────────────────────────────────────────────────────────────


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    gates.reset()
    yield
    gates.reset()


async def test_automatic_posting_needs_a_paid_plan_and_x_the_top_one(client, session, world, enforced):
    plans = await seed_plans(session)
    await session.commit()
    r = await client.get(f"{API}/clubs/social/platforms", headers=world.h)
    assert {p["platform"]: p["reason"] for p in r.json()} == {p: "plan" for p in ("facebook", "instagram", "linkedin", "linkedin_page", "x")}
    r = await client.post(f"{API}/clubs/social/accounts/instagram/connect", json={"return_to": DASHBOARD}, headers=world.h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED"
    assert r.json()["errors"]["feature"] == "social_autopost" and r.json()["errors"]["required_plan"] == "starter"
    # The share kit needs no plan.
    assert (await client.get(f"{API}/social/share-kit/{world.event.id}", headers=world.h)).status_code == 200
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=plans["growth"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    assert (await connect(client, world, "instagram"))["social"] == "connected"
    r = await client.post(f"{API}/clubs/social/accounts/x/connect", json={"return_to": DASHBOARD}, headers=world.h)
    assert r.status_code == 403 and r.json()["errors"]["feature"] == "social_x" and r.json()["errors"]["required_plan"] == "enterprise"
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=plans["enterprise"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    assert (await connect(client, world, "x"))["social"] == "connected"
    # Prices are as they were.
    assert (plans["starter"].price_monthly_paise, plans["growth"].price_monthly_paise) == (29900, 69900)


async def test_a_plan_that_lapses_stops_posting_with_share_manually(client, session, world, enforced):
    from app.api.social import scheduler
    from app.api.social.models import SocialPostTargets

    plans = await seed_plans(session)
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=plans["pro"], cycle=BillingCycle.monthly)
    await session.commit()
    await connect(client, world, "facebook")
    a = await account(session, world.club.id, "facebook")
    body = {"name": "Later", "trigger": {"mode": "before_start", "minutes": 60},
            "targets": [{"platform": "facebook", "account_id": str(a.id), "text": "{event_name} soon"}]}
    post = (await client.post(f"{API}/clubs/social/events/{world.event.id}/posts", json=body, headers=world.h)).json()
    await subscriptions.assign_plan(session, club_id=world.club.id, plan=plans["free"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    t = await session.get(SocialPostTargets, post["targets"][0]["id"])
    t.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    assert await scheduler.run_target(session, t.id) == "failed"
    await session.refresh(t)
    assert (t.error_code, t.error_action) == ("plan", "share_manually")
    assert not world.fake.sent_anything()


# ─── Drafts, posters, previews ───────────────────────────────────────────


async def test_drafts_carry_fill_in_words_hashtags_a_poster_and_a_link_per_platform(client, session, world):
    r = await client.get(f"{API}/clubs/social/events/{world.event.id}/drafts", headers=world.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fill_ins"] == ["event_name", "date", "time", "venue", "fee", "seats_left", "speakers", "link"]
    by = {d["platform"]: d for d in body["platforms"]}
    assert set(by) == {"facebook", "instagram", "linkedin", "linkedin_page", "x"}
    assert "{event_name}" in by["instagram"]["text"] and "link in bio" in by["instagram"]["text"] and "{link}" not in by["instagram"]["text"]
    assert "#CircuitSociety" in by["instagram"]["text"] and "#CircuitSociety" in by["instagram"]["hashtags"]
    assert "{link}" in by["facebook"]["text"] and "Circuit Society presents {event_name}." in by["linkedin"]["text"]
    assert by["instagram"]["variant"]["size"] == "ig_portrait" and (by["instagram"]["variant"]["width"], by["instagram"]["variant"]["height"]) == (1080, 1350)
    assert by["x"]["variant"]["size"] == "x" and by["facebook"]["variant"]["width"] == 1200
    links = list(await session.scalars(select(TrackingLinks).where(TrackingLinks.event_id == world.event.id)))
    assert sorted((l.utm_source, l.utm_medium, l.name) for l in links) == [
        ("facebook", "social", "Social · Facebook"), ("instagram", "social", "Social · Instagram"),
        ("linkedin", "social", "Social · LinkedIn"), ("x", "social", "Social · X")]
    again = await client.get(f"{API}/clubs/social/events/{world.event.id}/drafts?platforms=x", headers=world.h)
    assert [d["platform"] for d in again.json()["platforms"]] == ["x"] and again.json()["platforms"][0]["link"] == by["x"]["link"]
    assert len(list(await session.scalars(select(TrackingLinks.id).where(TrackingLinks.event_id == world.event.id)))) == 4
    # X's draft fits once filled.
    p = await client.post(f"{API}/clubs/social/events/{world.event.id}/preview", json={"platform": "x", "text": by["x"]["text"]}, headers=world.h)
    assert p.json()["counts"]["characters"] <= 280 and not p.json()["warnings"]


async def test_previews_warn_about_each_platforms_rules(client, world):
    long = "word " * 70
    p = (await client.post(f"{API}/clubs/social/events/{world.event.id}/preview", json={"platform": "x", "text": long + "{link}"}, headers=world.h)).json()
    assert [w["code"] for w in p["warnings"]] == ["x_over_280"] and p["counts"]["limit"] == 280
    tags = " ".join(f"#tag{i}" for i in range(31))
    p = (await client.post(f"{API}/clubs/social/events/{world.event.id}/preview",
                           json={"platform": "instagram", "text": f"{{event_name}} {tags} {{link}} {{nonsense}}"}, headers=world.h)).json()
    assert {w["code"] for w in p["warnings"]} == {"ig_hashtags_over_30", "ig_link_not_clickable", "unknown_fill_in"}
    assert p["text_rendered"].startswith("Hack Night ") and p["counts"]["hashtags"] == 31
    posters_ = (await client.get(f"{API}/clubs/social/events/{world.event.id}/posters", headers=world.h)).json()
    linkedin = next(v for v in posters_ if v["size"] == "linkedin")
    p = (await client.post(f"{API}/clubs/social/events/{world.event.id}/preview",
                           json={"platform": "instagram", "text": "Hi", "variant_id": linkedin["id"]}, headers=world.h)).json()
    assert [w["code"] for w in p["warnings"]] == ["image_shape"], "1200 x 627 is wider than Instagram's 1.91:1"


async def test_posters_are_made_in_every_size_by_crop_or_fit_or_from_the_clubs_own(client, session, world):
    from PIL import Image
    import io

    r = await client.get(f"{API}/clubs/social/events/{world.event.id}/posters", headers=world.h)
    sizes = {v["size"]: (v["width"], v["height"]) for v in r.json()}
    assert sizes == {"ig_portrait": (1080, 1350), "ig_square": (1080, 1080), "facebook": (1200, 630), "linkedin": (1200, 627),
                     "linkedin_square": (1080, 1080), "x": (1600, 900)}
    made = {v["size"]: v for v in r.json()}
    for v in made.values():
        assert "/events/posters/v-" in v["url"] and f".{v['size']}.jpg" in v["url"]
    key = next(k for k in world.files if k.endswith(".facebook.jpg"))
    assert Image.open(io.BytesIO(world.files[key])).size == (1200, 630)
    r = await client.put(f"{API}/clubs/social/events/{world.event.id}/posters/facebook", json={"mode": "fit", "background": "#123456"}, headers=world.h)
    assert r.status_code == 200 and r.json()["mode"] == "fit" and r.json()["background"] == "#123456"
    fitted = Image.open(io.BytesIO(world.files[next(k for k in world.files if k.endswith(".facebook.jpg") and k != key)])).convert("RGB")
    assert fitted.getpixel((5, 5)) == pytest.approx((0x12, 0x34, 0x56), abs=6), "the background shows around a fitted poster"
    r = await client.put(f"{API}/clubs/social/events/{world.event.id}/posters/x", json={"mode": "crop", "focus": {"x": 0.2, "y": 0.9}}, headers=world.editor)
    assert r.status_code == 200 and r.json()["focus"] == {"x": 0.2, "y": 0.9}
    r = await client.put(f"{API}/clubs/social/events/{world.event.id}/posters/ig_square", files={"file": ("own.png", jpeg(800, 600), "image/jpeg")}, headers=world.h)
    assert r.status_code == 200 and r.json()["own"] is True and (r.json()["width"], r.json()["height"]) == (1080, 1080)
    r = await client.put(f"{API}/clubs/social/events/{world.event.id}/posters/ig_square", json={"mode": "wobble"}, headers=world.h)
    assert r.status_code == 422
    # The link preview picture is the 1200 x 630.
    page = (await client.get(f"{API}/discover/events/{world.event.slug}")).json()
    assert page["event"]["share_image_url"].split("?")[0].endswith(".facebook.jpg")
    info = (await client.get(f"{API}/events/info/{world.event.id}")).json()
    assert info["share_image_url"] == page["event"]["share_image_url"]


async def test_ai_rewrites_for_a_platform_with_the_clubs_assist(client, world, monkeypatch):
    hub = fake_ai.install(monkeypatch)
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    r = await client.post(f"{API}/clubs/social/events/{world.event.id}/drafts/rewrite", json={"platform": "x", "text": "{event_name} {link}"}, headers=world.h)
    assert r.status_code == 403 and r.json()["error_code"] == "AI_DISABLED"
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini")
    hub.answers["write"] = {"text": "Ship it at {event_name}! {link} #HackNight", "subject": ""}
    r = await client.post(f"{API}/clubs/social/events/{world.event.id}/drafts/rewrite",
                          json={"platform": "x", "text": "{event_name} {link}", "tone": "hype"}, headers=world.editor)
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "Ship it at {event_name}! {link} #HackNight" and r.json()["warnings"] == []
    asked = hub.prompts()
    assert "Rewrite it for X" in asked and "{event_name}" in asked and "hype" in asked


# ─── The share kit ───────────────────────────────────────────────────────


async def test_the_share_kit_for_the_club_and_its_team_on_the_app(client, session, world, make_user, mint_token):
    r = await client.get(f"{API}/social/share-kit/{world.event.id}", headers=world.h)
    assert r.status_code == 200, r.text
    kit = r.json()
    assert kit["event"]["id"] == world.event.id and kit["event"]["slug"] == world.event.slug
    by = {p["platform"]: p for p in kit["platforms"]}
    assert set(by) == {"instagram", "facebook", "linkedin", "x", "whatsapp"}
    assert "link in bio" in by["instagram"]["text"] and "{" not in by["facebook"]["text"]
    assert "Hack Night" in by["whatsapp"]["text"] and by["whatsapp"]["link"] in by["whatsapp"]["text"]
    assert all(p["image_url"] for p in kit["platforms"]) and len({p["link"] for p in kit["platforms"]}) == 5
    # A team member on the app (their sign-in address is on the club's team) sees it too; a stranger does not.
    teammate = auth_header(await mint_token(world.app_user))
    assert (await client.get(f"{API}/social/share-kit/{world.event.id}", headers=teammate)).status_code == 200
    stranger = await make_user(email="someone@else.example")
    await session.commit()
    assert (await client.get(f"{API}/social/share-kit/{world.event.id}", headers=auth_header(await mint_token(stranger)))).status_code == 404
    listed = await client.get(f"{API}/social/share-kits", headers=teammate)
    assert [e["id"] for e in listed.json()] == [world.event.id]
    assert (await client.get(f"{API}/social/share-kits", headers=auth_header(await mint_token(stranger)))).json() == []


async def test_publishing_sends_the_team_its_share_kit_once(client, session, world, make_event):
    from app.api.closeout.models import EventStatus
    from app.api.notifications.models import Notifications

    draft = await make_event(club=world.club, created_by=world.owner, name="Robot Wars", event_datetime=datetime.now(timezone.utc) + timedelta(days=9),
                             lifecycle=EventStatus.draft)
    await session.commit()
    for _ in range(2):
        r = await client.patch(f"{API}/events/{draft.id}/closeout/settings", json={"status": "published"}, headers=world.h)
        assert r.status_code == 200, r.text
        await client.patch(f"{API}/events/{draft.id}/closeout/settings", json={"status": "draft"}, headers=world.h)
    rows = list(await session.scalars(select(Notifications).where(Notifications.type == "share_kit", Notifications.event_id == draft.id)))
    assert [(n.user_id, n.title) for n in rows] == [(world.app_user.id, "Robot Wars is live — share it")]


async def test_the_attendees_share_row_has_a_tracking_link_per_platform(client, session, world, make_event):
    from app.api.closeout.models import EventStatus

    r = await client.get(f"{API}/events/{world.event.id}/share")
    assert r.status_code == 200, r.text
    row = r.json()
    assert [x["platform"] for x in row] == ["x", "linkedin", "facebook", "whatsapp", "copy"]
    assert len({x["url"] for x in row}) == 5 and row[0]["intent_url"].startswith("https://x.com/intent/post?") and row[-1]["intent_url"] is None
    assert row[0]["text"].startswith("Hack Night — ")
    links = {(l.utm_source, l.utm_medium) for l in await session.scalars(select(TrackingLinks).where(TrackingLinks.event_id == world.event.id))}
    assert links == {(p, "share") for p in ("x", "linkedin", "facebook", "whatsapp", "copy")}
    draft = await make_event(club=world.club, created_by=world.owner, name="Hidden", lifecycle=EventStatus.draft)
    await session.commit()
    assert (await client.get(f"{API}/events/{draft.id}/share")).status_code == 404


# ─── Meta's callbacks ────────────────────────────────────────────────────


def signed(payload: dict, secret: str = META_APP_SECRET) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{sig}.{body}"


async def test_meta_data_deletion_verifies_the_signed_request_and_wipes_what_we_kept(client, session, world):
    await connect(client, world, "facebook")
    await connect(client, world, "instagram")
    for bad in (signed({"algorithm": "HMAC-SHA256", "user_id": "meta-user-1"}, secret="wrong"), "garbage", "",
                signed({"algorithm": "PLAIN", "user_id": "meta-user-1"})):
        r = await client.post(f"{API}/social/meta/data-deletion", data={"signed_request": bad})
        assert r.status_code == 400 and r.json()["error_code"] == "SIGNED_REQUEST_INVALID"
    assert len((await client.get(f"{API}/clubs/social/accounts", headers=world.h)).json()) == 2
    r = await client.post(f"{API}/social/meta/data-deletion",
                          data={"signed_request": signed({"algorithm": "HMAC-SHA256", "user_id": "meta-user-1", "issued_at": 1})})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"url", "confirmation_code"} and body["url"] == f"https://myotherapp.in/data-deletion?code={body['confirmation_code']}"
    rows = list(await session.scalars(select(SocialAccounts).where(SocialAccounts.club_id == world.club.id)))
    assert all(a.status == "disconnected" and a.access_token_enc is None and a.name is None and a.connector_external_id is None for a in rows)
    status = await client.get(f"{API}/social/data-deletion/{body['confirmation_code']}")
    assert status.json()["status"] == "completed" and status.json()["accounts"] == 2
    kept = await session.get(SocialDataDeletions, body["confirmation_code"])
    assert kept.subject_hash == hashlib.sha256(b"meta-user-1").hexdigest()
    assert (await client.get(f"{API}/social/data-deletion/nope")).status_code == 404
    bell = list(await session.scalars(select(ClubAlerts.kind).where(ClubAlerts.club_id == world.club.id)))
    assert "social_disconnected" in bell


async def test_meta_deauthorize_disconnects_that_persons_accounts(client, session, world):
    await connect(client, world, "facebook")
    assert (await client.post(f"{API}/social/meta/deauthorize", data={"signed_request": "x.y"})).status_code == 400
    r = await client.post(f"{API}/social/meta/deauthorize", data={"signed_request": signed({"algorithm": "HMAC-SHA256", "user_id": "meta-user-1"})})
    assert r.json() == {"disconnected": 1}
    assert (await client.get(f"{API}/clubs/social/accounts", headers=world.h)).json() == []
