"""Threads: a club posts, followers are told, readers like, vote and comment, and views count once a person."""
import io

import pytest
from PIL import Image

from app.core.notifications import service as push
from app.core.rate_limit import limiter
from tests.conftest import auth_header

POST = "/api/v1/clubs/threads"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "#17a58f").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def world(client, session, make_club, make_user, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    pushed: list[dict] = []

    async def fake_push(session_, user_ids, title, body, data=None, image_url=None):
        pushed.append(dict(user_ids=sorted(user_ids), title=title, body=body, data=data))
        return len(user_ids)

    monkeypatch.setattr(push, "send_notification_to_users", fake_push)
    owner, club = await make_club(name="Circuit Society", email="threads@example.com")
    rival_owner, rival = await make_club(name="Rivals", email="rivals-threads@example.com")
    club.is_verified = True
    rival.is_verified = True
    fan = await make_user(email="fan-threads@example.com")
    fan2 = await make_user(email="fan2-threads@example.com")
    stranger = await make_user(email="stranger-threads@example.com")
    await session.commit()
    h = auth_header(await mint_token(owner))
    fan_h = auth_header(await mint_token(fan))
    fan2_h = auth_header(await mint_token(fan2))
    for who in (fan_h, fan2_h):
        assert (await client.post(f"/api/v1/clubs/follow/{club.id}", headers=who)).status_code == 200
    return dict(owner=owner, club=club, rival=rival, fan=fan, fan2=fan2, stranger=stranger, h=h, rival_h=auth_header(await mint_token(rival_owner)), fan_h=fan_h, fan2_h=fan2_h, stranger_h=auth_header(await mint_token(stranger)), pushed=pushed)


async def test_a_post_reaches_followers_and_lists_pinned_first(client, world):
    h, club, pushed = world["h"], world["club"], world["pushed"]
    first = await client.post(POST, data={"body": "Hack Night 04 is tonight! See you at Lab 3 from 6:30.", "notify_followers": "true"}, headers=h)
    assert first.status_code == 201, first.text
    t1 = first.json()
    assert t1["kind"] == "post" and t1["poll"] is None and t1["pinned"] is False and t1["likes"] == 0 and t1["club"]["name"] == "Circuit Society"
    assert len(pushed) == 1 and pushed[0]["user_ids"] == sorted([world["fan"].id, world["fan2"].id]) and pushed[0]["title"] == "Circuit Society posted" and pushed[0]["data"]["thread_id"] == t1["id"]
    # Each follower has a line in the inbox too.
    inbox = await client.get("/api/v1/notifications/fetch-list", headers=world["fan_h"])
    assert inbox.status_code == 200 and any(n.get("type") == "club_thread" for n in (inbox.json().get("items") or inbox.json() if isinstance(inbox.json(), (list, dict)) else []))

    quiet = await client.post(POST, data={"body": "Housekeeping: lab keys back by 9.", "notify_followers": "false"}, headers=h)
    assert quiet.status_code == 201 and quiet.json()["notify_followers"] is False and len(pushed) == 1

    t2 = quiet.json()
    pinned = await client.patch(f"{POST}/{t2['id']}", json={"pinned": True}, headers=h)
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True
    page = (await client.get(POST, headers=h)).json()
    assert page["total"] == 2 and [x["id"] for x in page["items"]] == [t2["id"], t1["id"]]
    # Pinning another moves the pin; there is one top of the page.
    await client.patch(f"{POST}/{t1['id']}", json={"pinned": True}, headers=h)
    page = (await client.get(POST, headers=h)).json()
    assert [x["pinned"] for x in page["items"]] == [True, False] and page["items"][0]["id"] == t1["id"]

    assert (await client.patch(f"{POST}/{t1['id']}", json={"pinned": False}, headers=world["rival_h"])).status_code == 404
    assert (await client.delete(f"{POST}/{t1['id']}", headers=world["rival_h"])).status_code == 404
    assert (await client.delete(f"{POST}/{t2['id']}", headers=h)).json() == {"deleted": True}
    assert (await client.get(POST, headers=h)).json()["total"] == 1
    for bad in ({"body": "   "}, {"body": "x" * 501}):
        assert (await client.post(POST, data=bad, headers=h)).status_code == 422, bad


async def test_a_poll_takes_one_vote_per_person_and_a_photo_rides_along(client, world, fake_s3):
    h = world["h"]
    for bad in ('["Only one"]', '["A","B","C","D","E"]', '["Same","same"]', '["' + "x" * 61 + '","B"]', "not json"):
        assert (await client.post(POST, data={"body": "Theme?", "poll_options": bad}, headers=h)).status_code == 422, bad
    made = await client.post(POST, data={"body": "What should the theme of Hack Night 05 be?", "poll_options": '["Robotics","Wearables","Space tech"]', "notify_followers": "true"}, files={"image": ("poster.png", _png(), "image/png")}, headers=h)
    assert made.status_code == 201, made.text
    t = made.json()
    assert t["kind"] == "poll" and t["image"] is not None and [o["label"] for o in t["poll"]["options"]] == ["Robotics", "Wearables", "Space tech"]
    assert world["pushed"][-1]["title"] == "Circuit Society asked a question"
    robotics, wearables = t["poll"]["options"][0]["id"], t["poll"]["options"][1]["id"]

    v = await client.post(f"/api/v1/threads/{t['id']}/vote", json={"option_id": robotics}, headers=world["fan_h"])
    assert v.status_code == 200 and v.json()["votes"] == 1 and v.json()["voted_option_id"] == robotics
    await client.post(f"/api/v1/threads/{t['id']}/vote", json={"option_id": robotics}, headers=world["fan2_h"])
    # Changing one's mind moves the vote.
    moved = (await client.post(f"/api/v1/threads/{t['id']}/vote", json={"option_id": wearables}, headers=world["fan_h"])).json()
    assert moved["votes"] == 2 and [o["votes"] for o in moved["options"]] == [1, 1, 0] and [o["share"] for o in moved["options"]] == [50, 50, 0]
    assert (await client.post(f"/api/v1/threads/{t['id']}/vote", json={"option_id": "not-an-id"}, headers=world["fan_h"])).status_code == 404
    # The club's own list shows the shares; a reader's list shows their choice.
    mine = (await client.get(f"/api/v1/clubs/{world['club'].id}/threads", headers=world["fan_h"])).json()["items"][0]
    assert mine["poll"]["voted_option_id"] == wearables and mine["poll"]["votes"] == 2
    assert (await client.get(POST, headers=h)).json()["items"][0]["poll"]["voted_option_id"] is None


async def test_readers_like_comment_and_are_counted_once(client, world):
    h, club = world["h"], world["club"]
    t = (await client.post(POST, data={"body": "Results are in!", "notify_followers": "false"}, headers=h)).json()
    url = f"/api/v1/threads/{t['id']}"

    like = await client.post(f"{url}/like", headers=world["fan_h"])
    assert like.json() == {"liked": True, "likes": 1}
    await client.post(f"{url}/like", headers=world["fan2_h"])
    assert (await client.post(f"{url}/like", headers=world["fan_h"])).json() == {"liked": False, "likes": 1}

    c = await client.post(f"{url}/comments", json={"body": "Congrats Byte Benders!"}, headers=world["fan_h"])
    assert c.status_code == 201 and c.json()["author"]["is_club"] is False and c.json()["author"]["name"] == world["fan"].full_name
    reply = await client.post(f"{POST}/{t['id']}/comments", json={"body": "Thank you all!"}, headers=h)
    assert reply.status_code == 201 and reply.json()["author"] == {"id": world["owner"].id, "name": "Circuit Society", "is_club": True, "avatar": None}
    assert (await client.post(f"{url}/comments", json={"body": "  "}, headers=world["fan_h"])).status_code == 422
    listed = (await client.get(f"{url}/comments")).json()
    assert [x["body"] for x in listed] == ["Congrats Byte Benders!", "Thank you all!"]
    # Only the writer or the club removes a comment.
    assert (await client.delete(f"{url}/comments/{c.json()['id']}", headers=world["fan2_h"])).status_code == 403
    assert (await client.delete(f"{POST}/{t['id']}/comments/{c.json()['id']}", headers=h)).json() == {"deleted": True}
    assert (await client.delete(f"{url}/comments/{reply.json()['id']}", headers=world["fan_h"])).status_code == 403

    # Views: a person counts once however often they look; the club looking at itself does not count.
    for _ in range(3):
        await client.get(f"/api/v1/clubs/{club.id}/threads", headers=world["fan_h"])
    await client.get(f"/api/v1/clubs/{club.id}/threads", headers=world["stranger_h"])
    await client.get(f"/api/v1/clubs/{club.id}/threads", headers={"user-agent": "poster-phone", "x-forwarded-for": "10.0.0.9"})
    await client.get(f"/api/v1/clubs/{club.id}/threads", headers={"user-agent": "poster-phone", "x-forwarded-for": "10.0.0.9"})
    await client.get(f"/api/v1/clubs/{club.id}/threads", headers=h)
    mine = (await client.get(POST, headers=h)).json()["items"][0]
    assert mine["views"] == 3 and mine["likes"] == 1 and mine["comments"] == 1 and mine["liked"] is False
    assert (await client.get(f"/api/v1/clubs/{club.id}/threads", headers=world["fan2_h"])).json()["items"][0]["liked"] is True

    # The feed: what the clubs you follow posted, newest first; nothing from clubs you do not.
    await client.post(POST, data={"body": "Their news", "notify_followers": "false"}, headers=world["rival_h"])
    feed = (await client.get("/api/v1/threads/feed", headers=world["fan_h"])).json()
    assert [x["body"] for x in feed["items"]] == ["Results are in!"] and feed["total"] == 1
    assert (await client.get("/api/v1/threads/feed", headers=world["stranger_h"])).json()["items"] == []
    assert (await client.get("/api/v1/threads/feed", headers=h)).status_code == 403


async def test_the_threads_page_knows_what_waits_and_what_works(client, world):
    h = world["h"]
    first = await client.post("/api/v1/clubs/threads", data={"body": "Hack Night 04 is tonight!", "notify_followers": "true"}, headers=h)
    poll = await client.post("/api/v1/clubs/threads", data={"body": "Theme for 05?", "notify_followers": "false", "poll_options": '["Robotics","Wearables"]'}, headers=h)
    assert first.status_code == 201 and poll.status_code == 201
    t1, t2 = first.json()["id"], poll.json()["id"]
    # Readers: two likes and a comment on the first; a vote on the poll.
    assert (await client.post(f"/api/v1/threads/{t1}/like", headers=world["fan_h"])).status_code == 200
    assert (await client.post(f"/api/v1/threads/{t1}/like", headers=world["fan2_h"])).status_code == 200
    assert (await client.post(f"/api/v1/threads/{t1}/comments", json={"body": "Can't wait!"}, headers=world["fan_h"])).status_code == 201
    opt = poll.json()["poll"]["options"][0]["id"]
    assert (await client.post(f"/api/v1/threads/{t2}/vote", json={"option_id": opt}, headers=world["fan_h"])).status_code == 200

    page = (await client.get("/api/v1/clubs/threads/page", headers=h)).json()
    assert page["counts"] == {"all": 2, "pinned": 0, "polls": 1, "photos": 0, "awaiting": 1}
    by = {r["id"]: r for r in page["items"]}
    assert by[t1]["awaiting_reply"] is True and by[t1]["engagement"] == 3 and by[t1]["last_comment_at"]
    assert by[t2]["awaiting_reply"] is False and by[t2]["engagement"] == 1
    waiting = (await client.get("/api/v1/clubs/threads/page?scope=awaiting", headers=h)).json()
    assert [r["id"] for r in waiting["items"]] == [t1]

    # The club replies: nothing waits any more.
    assert (await client.post(f"/api/v1/clubs/threads/{t1}/comments", json={"body": "See you there"}, headers=h)).status_code == 201
    assert (await client.get("/api/v1/clubs/threads/page?scope=awaiting", headers=h)).json()["total"] == 0
    engaged = (await client.get("/api/v1/clubs/threads/page?sort=engaged", headers=h)).json()["items"]
    assert [r["id"] for r in engaged] == [t1, t2]
    assert (await client.get("/api/v1/clubs/threads/page?scope=vip", headers=h)).status_code == 422

    i = (await client.get("/api/v1/clubs/threads/insights", headers=h)).json()
    assert i["posts"] == 2 and i["posts_delta"] == 2 and i["awaiting"] == 0 and i["followers"] == 2
    assert i["best"]["id"] == t1 and i["best"]["engagement"] == 4  # two likes, two comments
    assert len(i["weeks"]) == 12 and i["weeks"][-1]["posts"] == 2 and i["weeks"][-1]["reactions"] == 5
    kinds = {k["kind"]: k for k in i["kinds"]}
    assert kinds["poll"]["posts"] == 1 and kinds["text"]["posts"] == 1 and kinds["photo"]["posts"] == 0 and kinds["photo"]["avg_reach"] is None
    assert i["notify"]["told"]["posts"] == 1 and i["notify"]["quiet"]["posts"] == 1
    assert i["all_time"]["posts"] == 2 and i["all_time"]["engagement"] == 5


async def test_one_post_opens_from_its_notification(client, world):
    h, club = world["h"], world["club"]
    t = (await client.post(POST, data={"body": "Hack night moved to Lab 3", "notify_followers": "true"}, headers=h)).json()
    thread_id = world["pushed"][-1]["data"]["thread_id"]
    assert thread_id == t["id"] and world["pushed"][-1]["data"]["type"] == "club_thread"

    await client.post(f"/api/v1/threads/{thread_id}/like", headers=world["fan_h"])
    one = await client.get(f"/api/v1/threads/{thread_id}", headers=world["fan_h"])
    assert one.status_code == 200
    body = one.json()
    assert body["body"] == "Hack night moved to Lab 3" and body["club"]["id"] == club.id and body["liked"] is True and body["likes"] == 1
    # Read signed out too; a view counts once a person, never the club's own.
    assert (await client.get(f"/api/v1/threads/{thread_id}")).json()["liked"] is False
    await client.get(f"/api/v1/threads/{thread_id}", headers=world["fan_h"])
    await client.get(f"/api/v1/threads/{thread_id}", headers=h)
    assert (await client.get(POST, headers=h)).json()["items"][0]["views"] == 2
    # "feed" is still the feed, not a post id.
    assert (await client.get("/api/v1/threads/feed", headers=world["fan_h"])).status_code == 200
    # Gone, or never was.
    assert (await client.delete(f"{POST}/{thread_id}", headers=h)).status_code == 200
    assert (await client.get(f"/api/v1/threads/{thread_id}")).status_code == 404
    assert (await client.get("/api/v1/threads/not-a-uuid")).status_code in (404, 422)
