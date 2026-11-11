"""Social posting: sending (app/api/social/scheduler.py and the adapters).

What each platform is sent, exactly once; what a lost answer, an outage, a
limit, a dead token or a refused picture becomes; when posts are due, quiet
hours, and what the event's changes do to them. Every platform is the fake
in tests/fake_social.py.
"""
import hashlib
import hmac
import json
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from app.api.closeout.models import EventStatus
from app.api.club_alerts.models import ClubAlerts
from app.api.links.models import TrackingLinks
from app.api.social import content, crypto, scheduler, sweep
from app.api.social.models import SocialAccounts, SocialPosts, SocialPostTargets
from app.config import settings
from app.core import jobs
from app.response import CustomHTTPException
from tests.fake_social import TOKENS
from tests.social_helpers import API, IST, account, at_ist, connect, fake, files, world  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _post(client, w, platform: str, text: str, trigger=None, headers=None, **extra) -> dict:
    a = await account(w.session, w.club.id, platform) if hasattr(w, "session") else None
    body = {"name": extra.pop("name", "Announcement"), "trigger": trigger or {"mode": "on_publish"},
            "targets": [{"platform": platform, "account_id": str(a.id), "text": text, **extra}]}
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json=body, headers=headers or w.h)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def w(world, session):
    world.session = session
    return world


async def _target(session, post: dict) -> SocialPostTargets:
    return await session.get(SocialPostTargets, post["targets"][0]["id"], populate_existing=True)


async def _due_now(session, t: SocialPostTargets) -> None:
    await session.execute(update(SocialPostTargets).where(SocialPostTargets.id == t.id)
                          .values(scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=1), next_attempt_at=None))
    await session.commit()


# ─── What each platform is sent ──────────────────────────────────────────


async def test_a_facebook_post_goes_out_on_publish_with_its_link_and_poster(client, session, w):
    await connect(client, w, "facebook")
    post = await _post(client, w, "facebook", "{event_name} on {date} at {venue}. Register: {link}")
    t = await _target(session, post)
    assert t.status == "posted" and t.external_post_id.startswith("page-1_") and t.post_url == f"https://www.facebook.com/{t.external_post_id}"
    [photo] = w.fake.by("POST", r"/page-1/photos$")
    link = await session.get(TrackingLinks, t.link_id)
    assert photo.form["message"] == f"Hack Night on {content.date_text(w.event.event_datetime)} at Main Hall. Register: {settings.PUBLIC_API_URL}/l/{link.code}"
    assert photo.form["published"] == "true" and "/events/posters/v-" in photo.form["url"] and ".facebook.jpg" in photo.form["url"]
    assert photo.headers["authorization"] == "Bearer PAGE-TOKEN-1"
    assert photo.params["appsecret_proof"] == hmac.new(b"test-meta-secret", b"PAGE-TOKEN-1", hashlib.sha256).hexdigest()
    assert (link.utm_source, link.utm_medium) == ("facebook", "social")
    listed = await client.get(f"{API}/clubs/social/events/{w.event.id}/posts", headers=w.h)
    [p] = listed.json()["posts"]
    assert p["status"] == "done" and p["targets"][0]["status"] == "posted" and p["targets"][0]["post_url"] == t.post_url
    for tok in TOKENS:
        assert tok not in listed.text


async def test_instagram_makes_a_container_waits_for_it_and_publishes_it(client, session, w):
    w.fake.container_status = ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]
    await connect(client, w, "instagram")
    post = await _post(client, w, "instagram", "{event_name} ✨ link in bio #HackNight")
    t = await _target(session, post)
    assert t.status == "posted", (t.error_code, t.error_message)
    [made] = w.fake.by("POST", r"/ig-1/media$")
    assert made.form["caption"] == "Hack Night ✨ link in bio #HackNight" and made.form["image_url"].split("?")[0].endswith(".ig_portrait.jpg")
    [published] = w.fake.by("POST", r"/ig-1/media_publish$")
    assert published.form["creation_id"] == t.container_id
    assert len(w.fake.by("GET", r"/container-\d+$")) == 3
    assert t.post_url == f"https://www.instagram.com/p/{t.external_post_id}/"


async def test_instagram_waits_at_its_daily_limit_and_says_so(client, session, w):
    w.fake.ig_quota = 100
    await connect(client, w, "instagram")
    post = await _post(client, w, "instagram", "{event_name}")
    t = await _target(session, post)
    assert (t.status, t.error_code) == ("waiting_limit", "ig_daily_limit")
    assert t.next_attempt_at > datetime.now(timezone.utc) + timedelta(minutes=50)
    assert not w.fake.by("POST", r"/media$"), "nothing is made while the day is full"


async def test_a_linkedin_post_uploads_its_picture_then_posts_as_the_member(client, session, w):
    await connect(client, w, "linkedin")
    post = await _post(client, w, "linkedin", "Join us (free!) at {event_name} #Hack {link}")
    t = await _target(session, post)
    assert t.status == "posted", (t.error_code, t.error_message)
    [init] = w.fake.by("POST", r"/rest/images$")
    assert init.params == {"action": "initializeUpload"} and init.json == {"initializeUploadRequest": {"owner": "urn:li:person:li-member-9"}}
    [upload] = w.fake.by("PUT", r"/dms-uploads/up1$")
    assert upload.raw[:3] == b"\xff\xd8\xff", "the poster's JPEG bytes"
    [create] = w.fake.by("POST", r"/rest/posts$")
    assert create.headers["linkedin-version"] == "202610" and create.headers["x-restli-protocol-version"] == "2.0.0"
    body = create.json
    assert body["author"] == "urn:li:person:li-member-9" and body["visibility"] == "PUBLIC" and body["lifecycleState"] == "PUBLISHED"
    assert body["content"] == {"media": {"id": "urn:li:image:IMG1"}}
    assert body["commentary"].startswith("Join us \\(free!\\) at Hack Night #Hack "), "reserved characters escaped, hashtags kept"
    assert t.post_url == f"https://www.linkedin.com/feed/update/{t.external_post_id}/"


async def test_an_x_post_uploads_its_picture_then_posts(client, session, w):
    await connect(client, w, "x")
    post = await _post(client, w, "x", "{event_name} · {date}. Register: {link}")
    t = await _target(session, post)
    assert t.status == "posted", (t.error_code, t.error_message)
    [upload] = w.fake.by("POST", r"/2/media/upload$")
    assert b'name="media"' in upload.raw and upload.headers["authorization"] == "Bearer X-ACCESS-TOKEN"
    [tweet] = w.fake.by("POST", r"/2/tweets$")
    assert tweet.json["media"] == {"media_ids": ["18800000001"]} and tweet.json["text"].startswith("Hack Night · ")
    assert t.post_url == f"https://x.com/circuitsoc/status/{t.external_post_id}"


async def test_x_stops_at_the_months_cap(client, session, w, monkeypatch):
    monkeypatch.setattr(settings, "X_MONTHLY_POST_CAP", 1)
    await connect(client, w, "x")
    first = await _target(session, await _post(client, w, "x", "{event_name} one"))
    second = await _target(session, await _post(client, w, "x", "{event_name} two", name="Again"))
    assert first.status == "posted"
    assert (second.status, second.error_code, second.error_action) == ("failed", "x_monthly_cap", "share_manually")
    assert len(w.fake.by("POST", r"/2/tweets$")) == 1


async def test_an_x_token_about_to_run_out_is_refreshed_before_the_post(client, session, w):
    await connect(client, w, "x")
    a = await account(session, w.club.id, "x")
    a.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    await session.commit()
    t = await _target(session, await _post(client, w, "x", "{event_name}"))
    assert t.status == "posted" and w.fake.x_refreshes == 1
    await session.refresh(a)
    assert crypto.open_(a.access_token_enc) == "X-ACCESS-TOKEN-2" and crypto.open_(a.refresh_token_enc) == "X-REFRESH-TOKEN-2"
    [tweet] = w.fake.by("POST", r"/2/tweets$")
    assert tweet.headers["authorization"] == "Bearer X-ACCESS-TOKEN-2"


# ─── Exactly once ────────────────────────────────────────────────────────


async def test_a_target_is_sent_once_however_often_its_job_runs(client, session, w):
    await connect(client, w, "facebook")
    post = await _post(client, w, "facebook", "{event_name}", trigger={"mode": "before_start", "minutes": 60})
    t = await _target(session, post)
    assert t.status == "scheduled"
    await _due_now(session, t)
    now = datetime.now(timezone.utc)
    mine = await scheduler.claim(session, t.id, now)
    assert mine is not None and await scheduler.claim(session, t.id, now) is None, "a second claim of the same target gets nothing"
    await session.execute(update(SocialPostTargets).where(SocialPostTargets.id == t.id).values(status="scheduled", claim=None))
    await session.commit()
    assert await scheduler.run_target(session, t.id) == "posted"
    for _ in range(3):
        assert await scheduler.run_target(session, t.id) == "not_due"
        await scheduler.dispatch(session)
    assert len(w.fake.by("POST", r"/photos$")) == 1


async def test_a_lost_answer_is_looked_up_before_anything_is_sent_again(client, session, w):
    await connect(client, w, "facebook")
    w.fake.fail("POST", r"/photos$", raise_=httpx.ReadTimeout("no answer"))
    post = await _post(client, w, "facebook", "{event_name} tonight")
    t = await _target(session, post)
    assert t.status == "scheduled" and t.uncertain_since is not None and t.error_code == "no_answer"
    # It had landed: the platform has it.
    w.fake.fb_posts.append({"id": "page-1_99", "message": "Hack Night tonight", "permalink_url": "https://www.facebook.com/page-1_99"})
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "posted"
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert (t.external_post_id, t.post_url) == ("page-1_99", "https://www.facebook.com/page-1_99")
    assert len(w.fake.by("POST", r"/photos$")) == 1, "never sent a second time"
    assert w.fake.by("GET", r"/page-1/published_posts$")


async def test_a_lost_answer_that_did_not_land_is_sent_after_the_lookup(client, session, w):
    await connect(client, w, "facebook")
    w.fake.fail("POST", r"/photos$", raise_=httpx.RemoteProtocolError("dropped"))
    t = await _target(session, await _post(client, w, "facebook", "{event_name} tonight"))
    assert t.uncertain_since is not None
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "posted"
    calls = [c.path for c in w.fake.calls if c.path.endswith(("/published_posts", "/photos"))]
    assert calls == ["/v26.0/page-1/photos", "/v26.0/page-1/published_posts", "/v26.0/page-1/photos"]


async def test_an_instagram_publish_whose_answer_was_lost_is_found_not_published_again(client, session, w):
    await connect(client, w, "instagram")
    w.fake.fail("POST", r"/media_publish$", raise_=httpx.ReadTimeout("no answer"))
    t = await _target(session, await _post(client, w, "instagram", "{event_name} tonight"))
    assert t.uncertain_since is not None and t.container_id, "the container is kept for the next attempt"
    w.fake.ig_media.append({"id": "igmedia-77", "caption": "Hack Night tonight", "timestamp": "2099-01-01T00:00:00+0000",
                            "permalink": "https://www.instagram.com/p/igmedia-77/"})
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "posted"
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert t.external_post_id == "igmedia-77"
    assert len(w.fake.by("POST", r"/media_publish$")) == 1 and len(w.fake.by("POST", r"/ig-1/media$")) == 1


async def test_an_outcome_that_stays_unknown_asks_the_club_after_six_hours(client, session, w):
    await connect(client, w, "instagram")
    w.fake.fail("POST", r"/media_publish$", raise_=httpx.ReadTimeout("no answer"))
    t = await _target(session, await _post(client, w, "instagram", "{event_name} tonight"))
    # Instagram says the container was published, yet the post is not found: it is never published again.
    w.fake.container_status = ["PUBLISHED"]
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "scheduled"
    await session.execute(update(SocialPostTargets).where(SocialPostTargets.id == t.id).values(
        first_attempt_at=datetime.now(timezone.utc) - timedelta(hours=7)))
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "failed"
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert (t.error_code, t.error_action) == ("unknown_outcome", "retry")
    assert len(w.fake.by("POST", r"/media_publish$")) == 1


async def test_a_claim_that_went_stale_mid_send_is_looked_up_first(client, session, w):
    await connect(client, w, "x")
    t = await _target(session, await _post(client, w, "x", "{event_name}", trigger={"mode": "before_start", "minutes": 30}))
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    await session.execute(update(SocialPostTargets).where(SocialPostTargets.id == t.id).values(
        status="posting", claimed_at=long_ago, rendered_text="Hack Night", scheduled_for=long_ago))
    await session.commit()
    w.fake.tweets.append({"id": "190555", "text": "Hack Night"})
    assert await scheduler.dispatch(session) == 1
    await jobs.settle()
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert (t.status, t.external_post_id) == ("posted", "190555")
    assert not w.fake.by("POST", r"/2/tweets$")
    [look] = w.fake.by("GET", r"/2/users/x-user-5/tweets$")
    assert look.params["start_time"].endswith("Z")


async def test_a_linkedin_member_post_whose_answer_was_lost_asks_the_club(client, session, w):
    await connect(client, w, "linkedin")
    w.fake.fail("POST", r"/rest/posts$", raise_=httpx.ReadTimeout("no answer"))
    t = await _target(session, await _post(client, w, "linkedin", "{event_name}"))
    assert t.uncertain_since is not None
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "failed"
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert (t.error_code, t.error_action) == ("unknown_outcome", "retry")
    assert len(w.fake.by("POST", r"/rest/posts$")) == 1


# ─── Failures ────────────────────────────────────────────────────────────


async def test_an_outage_is_retried_with_growing_gaps_then_fails_after_six_hours(client, session, w):
    await connect(client, w, "facebook")
    w.fake.fail("POST", r"/photos$", status=503, body={"error": {"code": 2, "message": "Service temporarily unavailable"}}, times=4)
    t = await _target(session, await _post(client, w, "facebook", "{event_name}"))
    gaps = []
    for _ in range(2):
        assert t.status == "scheduled" and t.error_code == "meta_2"
        gaps.append(round((t.next_attempt_at - datetime.now(timezone.utc)).total_seconds() / 60))
        await _due_now(session, t)
        await scheduler.run_target(session, t.id)
        t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert gaps == [1, 2]
    # Six hours of it: failed, with what to do, and the club told.
    await session.execute(update(SocialPostTargets).where(SocialPostTargets.id == t.id).values(
        first_attempt_at=datetime.now(timezone.utc) - timedelta(hours=7)))
    await _due_now(session, t)
    assert await scheduler.run_target(session, t.id) == "failed"
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert t.error_action == "retry" and "six hours" in t.error_message
    alert = await session.scalar(select(ClubAlerts).where(ClubAlerts.kind == "social_failed"))
    assert alert.title == "A Facebook post failed — share it manually" and alert.link == f"/events/{w.event.id}/promote"
    # Retry from the dashboard sends it.
    r = await client.post(f"{API}/clubs/social/targets/{t.id}/retry", headers=w.editor)
    assert r.status_code == 200, r.text
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert t.status == "posted"
    assert (await client.post(f"{API}/clubs/social/targets/{t.id}/retry", headers=w.h)).json()["error_code"] == "SOCIAL_ALREADY_POSTED"


async def test_a_dead_token_holds_the_accounts_posts_until_it_is_reconnected(client, session, w):
    await connect(client, w, "facebook")
    w.fake.fail("POST", r"/photos$", status=400, body={"error": {"code": 190, "message": "Error validating access token"}})
    t = await _target(session, await _post(client, w, "facebook", "{event_name}"))
    assert (t.status, t.error_action) == ("held", "reconnect")
    a = await account(session, w.club.id, "facebook")
    assert a.status == "needs_reconnect"
    [acct] = (await client.get(f"{API}/clubs/social/accounts", headers=w.h)).json()
    assert acct["status"] == "needs_reconnect" and acct["waiting_posts"] == 1
    alert = await session.scalar(select(ClubAlerts).where(ClubAlerts.kind == "social_disconnected"))
    assert alert.title == "Facebook disconnected — 1 post waiting"
    # Reconnecting sends what waited.
    assert (await connect(client, w, "facebook"))["social"] == "connected"
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert t.status == "posted" and len(w.fake.by("POST", r"/photos$")) == 2


async def test_a_refused_picture_asks_for_a_fix(client, session, w):
    await connect(client, w, "instagram")
    w.fake.fail("POST", r"/ig-1/media$", status=400, body={"error": {"code": 9004, "error_subcode": 2207052, "message": "Media download failed"}})
    t = await _target(session, await _post(client, w, "instagram", "{event_name}"))
    assert (t.status, t.error_action) == ("failed", "fix_image")


async def test_a_rate_limit_waits_by_itself(client, session, w):
    await connect(client, w, "x")
    w.fake.fail("POST", r"/2/tweets$", status=429, body={"title": "Too Many Requests", "detail": "Too Many Requests"})
    t = await _target(session, await _post(client, w, "x", "{event_name}"))
    assert (t.status, t.error_code) == ("waiting_limit", "x_rate_limited") and t.next_attempt_at > datetime.now(timezone.utc) + timedelta(minutes=10)


async def test_scheduling_checks_what_the_platform_would_refuse(client, session, w):
    await connect(client, w, "x")
    a = await account(session, w.club.id, "x")
    body = {"name": "Too long", "trigger": {"mode": "on_publish"}, "targets": [{"platform": "x", "account_id": str(a.id), "text": "word " * 80}]}
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json=body, headers=w.h)
    assert r.status_code == 422 and r.json()["errors"]["warnings"][0]["code"] == "x_over_280"
    # As a draft it is kept, to fix.
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json={**body, "draft": True}, headers=w.h)
    assert r.status_code == 201 and r.json()["status"] == "draft"
    r = await client.post(f"{API}/clubs/social/posts/{r.json()['id']}/schedule", headers=w.h)
    assert r.status_code == 422
    body["targets"][0]["platform"] = "facebook"
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json=body, headers=w.h)
    assert r.json()["error_code"] == "SOCIAL_PLATFORM_MISMATCH"
    body["trigger"] = {"mode": "at", "at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    body["targets"][0].update(platform="x", text="{event_name}")
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json=body, headers=w.h)
    assert r.json()["error_code"] == "TRIGGER_IN_PAST"


# ─── When ────────────────────────────────────────────────────────────────


def _event(start: datetime, *, duration=3.0, reg_close=None, status=EventStatus.published):
    from types import SimpleNamespace

    return SimpleNamespace(event_datetime=start, duration=duration, reg_enddate=reg_close, lifecycle=status, is_deleted=False)


async def test_when_each_trigger_is_due_and_quiet_hours():
    now = datetime.now(timezone.utc)
    ev = _event(at_ist(5, 18))
    assert scheduler.due_for({"mode": "on_publish"}, ev, now) == now
    assert scheduler.due_for({"mode": "on_publish"}, _event(at_ist(5, 18), status=EventStatus.draft), now) is None
    assert scheduler.due_for({"mode": "before_start", "minutes": 2880}, ev, now) == at_ist(3, 18)
    # 18:00 less 16 hours is 02:00: quiet, so 07:00 the same morning.
    assert scheduler.due_for({"mode": "before_start", "minutes": 16 * 60}, ev, now) == at_ist(5, 7)
    # 07:30 less an hour is 06:30; 07:00 would be ... still before the start: 07:00.
    early = _event(at_ist(5, 7, 30))
    assert scheduler.due_for({"mode": "before_start", "minutes": 60}, early, now) == at_ist(5, 7)
    # 01:00 less an hour is midnight; 07:00 is after the start: 22:55 the evening before.
    late = _event(at_ist(5, 1))
    assert scheduler.due_for({"mode": "before_start", "minutes": 60}, late, now) == at_ist(4, 22, 55)
    # After the end (18:00 + 3h = 21:00) + 3h = 00:00: next morning at 07:00.
    assert scheduler.due_for({"mode": "after_end", "minutes": 180}, ev, now) == at_ist(6, 7)
    # A time the club picked is never moved.
    at = at_ist(4, 23, 30)
    assert scheduler.due_for({"mode": "at", "at": at.isoformat()}, ev, now) == at
    assert scheduler.due_for({"mode": "before_reg_close", "minutes": 120}, _event(at_ist(5, 18), reg_close=at_ist(4, 20)), now) == at_ist(4, 18)
    with pytest.raises(CustomHTTPException) as e:
        scheduler.due_for({"mode": "before_reg_close", "minutes": 60}, ev, now)
    assert e.value.error_code == "NO_REG_CLOSE"


async def test_a_post_on_publish_for_a_draft_waits_then_goes_when_it_is_published(client, session, w, make_event):
    draft = await make_event(club=w.club, created_by=w.owner, name="Robot Wars", event_datetime=at_ist(9, 18), lifecycle=EventStatus.draft)
    draft.poster = w.poster
    await session.commit()
    await connect(client, w, "facebook")
    a = await account(session, w.club.id, "facebook")
    body = {"name": "Announcement", "trigger": {"mode": "on_publish"}, "targets": [{"platform": "facebook", "account_id": str(a.id), "text": "{event_name}"}]}
    r = await client.post(f"{API}/clubs/social/events/{draft.id}/posts", json=body, headers=w.h)
    t = await session.get(SocialPostTargets, r.json()["targets"][0]["id"])
    assert t.status == "scheduled" and t.scheduled_for is None and not w.fake.by("POST", r"/photos$")
    r = await client.patch(f"{API}/events/{draft.id}/closeout/settings", json={"status": "published"}, headers=w.h)
    assert r.status_code == 200, r.text
    t = await session.get(SocialPostTargets, t.id, populate_existing=True)
    assert t.status == "posted" and w.fake.by("POST", r"/photos$")[0].form["message"] == "Robot Wars"


async def test_unpublishing_or_cancelling_the_event_cancels_what_waits(client, session, w):
    await connect(client, w, "facebook")
    first = await _post(client, w, "facebook", "{event_name}")
    later = await _post(client, w, "facebook", "{event_name} soon", trigger={"mode": "before_start", "minutes": 1440}, name="Reminder")
    r = await client.patch(f"{API}/events/{w.event.id}/closeout/settings", json={"status": "draft"}, headers=w.h)
    assert r.status_code == 200, r.text
    t = await _target(session, later)
    post = await session.get(SocialPosts, later["id"], populate_existing=True)
    assert (t.status, t.error_code, post.status, post.status_reason) == ("cancelled", "event_unpublished", "cancelled", "event_unpublished")
    assert (await _target(session, first)).status == "posted", "what went out stays"
    # Cancelled: a cancellation post is offered as a draft to the accounts it posted from.
    await scheduler.on_stopped(session, w.event, "event_cancelled")
    await session.commit()
    drafts = list(await session.scalars(select(SocialPosts).where(SocialPosts.template == "cancellation")))
    assert len(drafts) == 1 and drafts[0].status == "draft"


async def test_a_new_date_moves_relative_posts_and_pauses_those_that_wrote_the_old_details(client, session, w):
    await connect(client, w, "facebook")
    with_words = await _post(client, w, "facebook", "{event_name} on {date} at {venue}", trigger={"mode": "before_start", "minutes": 1440}, name="One")
    by_hand = await _post(client, w, "facebook", "See you in Main Hall!", trigger={"mode": "before_start", "minutes": 120}, name="Two")
    before = content.snapshot(w.event)
    w.event.event_datetime = at_ist(12, 18)
    w.event.location_name = "Seminar Room"
    await session.flush()
    await scheduler.on_changed(session, w.event, before)
    await session.commit()
    t1, t2 = await _target(session, with_words), await _target(session, by_hand)
    p1, p2 = await session.get(SocialPosts, with_words["id"]), await session.get(SocialPosts, by_hand["id"])
    assert t1.scheduled_for == at_ist(11, 18) and p1.status == "scheduled", "fill-in words are filled at posting: nothing to review"
    assert (p2.status, p2.status_reason) == ("paused", "event_changed") and t2.scheduled_for == at_ist(12, 16)
    # Reviewed and scheduled again, it goes on.
    r = await client.post(f"{API}/clubs/social/posts/{by_hand['id']}/schedule", headers=w.h)
    assert r.status_code == 200 and r.json()["status"] == "scheduled"


async def test_the_standard_campaign_is_an_announcement_and_a_reminder(client, session, w):
    await connect(client, w, "facebook")
    await connect(client, w, "x")
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts/campaign", json={"platforms": ["facebook", "x"]}, headers=w.h)
    assert r.status_code == 201, r.text
    posts = r.json()["posts"]
    assert [(p["name"], p["trigger"]["mode"]) for p in posts] == [("Announcement", "on_publish"), ("Reminder", "before_start")]
    assert {t["platform"] for t in posts[0]["targets"]} == {"facebook", "x"}
    announcement = list(await session.scalars(select(SocialPostTargets).where(SocialPostTargets.post_id == posts[0]["id"])))
    assert {t.status for t in announcement} == {"posted"}


async def test_registrations_each_platform_brought(client, session, w, make_user, make_registration):
    await connect(client, w, "instagram")
    t = await _target(session, await _post(client, w, "instagram", "{event_name}"))
    for i in range(2):
        u = await make_user(email=f"fan{i}@example.com")
        await make_registration(event=w.event, user=u, tracking_link_id=t.link_id)
    await session.commit()
    r = await client.get(f"{API}/clubs/social/events/{w.event.id}/posts", headers=w.h)
    assert r.json()["attribution"] == {"instagram": 2}


# ─── Co-hosts ────────────────────────────────────────────────────────────


async def test_a_cohost_posts_from_its_own_accounts_and_sees_only_its_own_posts(client, session, w, make_club, mint_token):
    from app.api.cohosts.models import ACCEPTED, EventCohosts
    from tests.conftest import auth_header

    other_owner, other = await make_club(name="Robotics Guild", email="robotics@example.com")
    session.add(EventCohosts(event_id=w.event.id, club_id=other.id, status=ACCEPTED, invited_at=datetime.now(timezone.utc),
                             expires_at=w.event.event_datetime, responded_at=datetime.now(timezone.utc)))
    await session.commit()
    oh = auth_header(await mint_token(other_owner))
    await connect(client, w, "facebook")
    host_account = await account(session, w.club.id, "facebook")
    host_post = await _post(client, w, "facebook", "{event_name}", trigger={"mode": "before_start", "minutes": 60})
    # The host's account is not the co-host's to use.
    body = {"name": "Ours", "trigger": {"mode": "before_start", "minutes": 60},
            "targets": [{"platform": "facebook", "account_id": str(host_account.id), "text": "{event_name}"}]}
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json=body, headers=oh)
    assert r.status_code == 404 and r.json()["error_code"] == "SOCIAL_ACCOUNT_NOT_FOUND"
    await connect(client, w, "x", headers=oh)
    theirs = await account(session, other.id, "x")
    body["targets"] = [{"platform": "x", "account_id": str(theirs.id), "text": "{event_name} with friends"}]
    r = await client.post(f"{API}/clubs/social/events/{w.event.id}/posts", json=body, headers=oh)
    assert r.status_code == 201, r.text
    cohost_post = r.json()
    assert [p["id"] for p in (await client.get(f"{API}/clubs/social/events/{w.event.id}/posts", headers=oh)).json()["posts"]] == [cohost_post["id"]]
    assert [p["id"] for p in (await client.get(f"{API}/clubs/social/events/{w.event.id}/posts", headers=w.h)).json()["posts"]] == [host_post["id"]]
    assert (await client.post(f"{API}/clubs/social/posts/{host_post['id']}/cancel", headers=oh)).status_code == 404
    # The co-host leaves: its waiting posts go; the host's stay.
    r = await client.post(f"{API}/events/{w.event.id}/cohosts/leave", headers=oh)
    assert r.status_code == 200, r.text
    assert (await _target(session, cohost_post)).status == "cancelled"
    assert (await _target(session, host_post)).status == "scheduled"


# ─── The day ─────────────────────────────────────────────────────────────


async def test_the_daily_job_warns_before_access_ends_and_refreshes_what_it_can(client, session, w):
    await connect(client, w, "linkedin")
    await connect(client, w, "x")
    li, x = await account(session, w.club.id, "linkedin"), await account(session, w.club.id, "x")
    li.token_expires_at = datetime.now(timezone.utc) + timedelta(days=5, hours=1)
    await session.commit()
    found = await sweep.refresh_tokens(session)
    assert found == {"refreshed": 1, "warned": 1, "ended": 0, "refused": 0}
    alert = await session.scalar(select(ClubAlerts).where(ClubAlerts.kind == "social_expiring"))
    assert alert.title == "LinkedIn access ends in 5 days"
    assert (await sweep.refresh_tokens(session))["warned"] == 0, "once per expiry"
    await session.refresh(x)
    assert crypto.open_(x.access_token_enc) == "X-ACCESS-TOKEN-2"
    li.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.commit()
    assert (await sweep.refresh_tokens(session))["ended"] == 1
    await session.refresh(li)
    assert li.status == "needs_reconnect"


async def test_the_sweep_queues_the_day_once(session, w):
    now = datetime.combine(datetime.now(IST).date(), time(9, 0), tzinfo=IST)
    assert await sweep.maybe_daily(session, now) is True
    assert await sweep.maybe_daily(session, now + timedelta(hours=3)) is False
    assert await sweep.maybe_daily(session, now + timedelta(days=1)) is True
    early = datetime.combine(datetime.now(IST).date() + timedelta(days=2), time(5, 0), tzinfo=IST)
    assert await sweep.maybe_daily(session, early) is False


async def test_nothing_secret_reaches_a_log_the_audit_or_the_bell(client, session, w, caplog):
    import logging

    from app.api.audit.models import AdminAuditLogs

    caplog.set_level(logging.DEBUG)
    for p in ("facebook", "instagram", "linkedin", "x"):
        await connect(client, w, p)
        await _post(client, w, p, "{event_name}", name=f"To {p}")
    w.fake.fail("POST", r"/photos$", status=400, body={"error": {"code": 190, "message": "Error validating access token: the session has been invalidated"}})
    await _post(client, w, "facebook", "{event_name} again", name="Again")
    logs = list(await session.scalars(select(AdminAuditLogs)))
    alerts = list(await session.scalars(select(ClubAlerts)))
    blob = caplog.text + json.dumps([l.details for l in logs], default=str) + json.dumps([(a.title, a.body, a.data) for a in alerts], default=str)
    rows = list(await session.scalars(select(SocialAccounts)))
    for tok in TOKENS:
        assert tok not in blob, tok
        assert all(tok not in (r.access_token_enc or "") + (r.refresh_token_enc or "") for r in rows)
    assert {l.action for l in logs} >= {"social.connect", "social.schedule", "social.post"}
