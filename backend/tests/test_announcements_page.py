"""The Announcements page: every push as one row with who opened it, the figures above, and what a send would reach."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.notifications.models import NotificationStatus, Notifications
from app.api.users.models import UserDeviceTokens
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    import app.core.notifications.service as push_mod

    async def fake_push(*args, **kwargs):
        user_ids = kwargs.get("user_ids") or (args[1] if len(args) > 1 else [])
        return len(user_ids)

    monkeypatch.setattr(push_mod, "send_notification_to_users", fake_push)
    monkeypatch.setattr("app.core.notifications.triggers.send_notification_to_users", fake_push)
    owner, club = await make_club(name="Circuit Society", email="ann@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night 04", event_datetime=NOW + timedelta(days=3), duration=3.0)
    users = [await make_user(email=f"a{i}-ann@example.com") for i in range(3)]
    regs = [await make_registration(event=event, user=u, full_name=u.email) for u in users]
    session.add(CheckIns(event_id=event.id, registration_id=regs[0].id, via=CheckInVia.gate, checked_in_at=NOW, door_label="Gate A"))
    session.add(UserDeviceTokens(user_id=users[0].id, fcm_token="t0", platform="ios"))
    session.add(UserDeviceTokens(user_id=users[1].id, fcm_token="t1", platform="android"))
    await session.commit()
    return {"h": auth_header(await mint_token(owner)), "event": event, "users": users, "club": club}


async def test_a_send_is_one_row_that_says_who_opened_it(client, session, world):
    h = world["h"]
    r = await client.post(f"/api/v1/clubs/notifications/events/{world['event'].id}/send", json={"title": "Doors open at 5", "body": "Come early", "audience": "all"}, headers=h)
    assert r.status_code == 200 and r.json()["success"] is True
    # Two open it, one within the hour.
    rows = list((await session.scalars(__import__("sqlalchemy").select(Notifications).where(Notifications.event_id == world["event"].id))).all())
    assert len(rows) == 3
    rows[0].status = NotificationStatus.read
    rows[1].status = NotificationStatus.read
    await session.commit()
    rows[1].updated_at = rows[1].created_at + timedelta(hours=3)
    await session.commit()

    page = (await client.get("/api/v1/clubs/notifications/page", headers=h)).json()
    assert page["total"] == 1 and page["counts"] == {"all": 1, "announcements": 1, "live_updates": 0}
    s = page["items"][0]
    assert s["kind"] == "announcement" and s["title"] == "Doors open at 5" and s["audience"] == "all"
    assert s["sent"] == 3 and s["read"] == 2 and s["read_quickly"] == 1 and s["read_pct"] == 67
    assert s["event"]["name"] == "Hack Night 04" and s["slot"] in {"morning", "afternoon", "evening", "night"}
    assert (await client.get("/api/v1/clubs/notifications/page?search=doors", headers=h)).json()["total"] == 1
    assert (await client.get("/api/v1/clubs/notifications/page?scope=live_updates", headers=h)).json()["total"] == 0
    assert (await client.get("/api/v1/clubs/notifications/page?scope=vip", headers=h)).status_code == 422

    # A live update from the room shows beside it.
    live = await client.post(f"/api/v1/events/{world['event'].id}/live/updates", json={"kind": "room", "text": "Demos move to Lab 3B", "audience": "checked_in"}, headers=h)
    assert live.status_code == 201
    page = (await client.get("/api/v1/clubs/notifications/page", headers=h)).json()
    assert page["counts"] == {"all": 2, "announcements": 1, "live_updates": 1}
    assert page["items"][0]["kind"] == "live_update" and page["items"][0]["sent"] == 1

    i = (await client.get("/api/v1/clubs/notifications/insights", headers=h)).json()
    assert i["sends"] == 2 and i["reached"] == 4 and i["open_rate"] == 50 and i["quick_rate"] == 25
    assert i["best"]["title"] == "Doors open at 5" and i["best"]["read_pct"] == 67
    assert i["registered_ahead"] == 3 and i["reachable_ahead"] == 2 and i["coming"][0]["name"] == "Hack Night 04"
    assert len(i["slots"]) == 4 and i["best_slot"] == page["items"][0]["slot"]  # both sends landed in the same slot, so it is the best one
    assert i["all_time"] == {"sends": 2, "reached": 4, "opened": 2}


async def test_what_a_send_would_reach(client, world):
    a = (await client.get(f"/api/v1/clubs/notifications/events/{world['event'].id}/audience", headers=world["h"])).json()
    assert a["registered"] == 3 and a["checked_in"] == 1 and a["reachable"] == 2
    assert a["by_audience"]["all"] == {"people": 3, "reachable": 2}
    assert a["by_audience"]["attendees"] == {"people": 1, "reachable": 1}
    assert a["by_audience"]["non_attendees"] == {"people": 2, "reachable": 1}
    assert (await client.get("/api/v1/clubs/notifications/events/999999/audience", headers=world["h"])).status_code == 404
