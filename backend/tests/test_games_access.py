"""Who may play and who may run it (CONTRACT §1, §3, §5): tokens, badge + PIN, sessions, the plan gate,
staff rights, the screens and their stream, the admin overview, ID card values."""
import asyncio
import importlib.util
import io
import pathlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import select, update
from starlette.requests import Request  # noqa: F401

from app.api.club_access import policy
from app.api.games import bus, cards, common, stream, tokens
from app.api.games.models import GamePlayers
from app.api.subscriptions import gates
from app.api.subscriptions.models import PlanFeatures, Plans
from app.config import settings as app_settings
from tests.conftest import auth_header
from tests.games_world import game, games, gp, make_world, org  # noqa: F401

API = "/api/v1"


# ─── Tokens ─────────────────────────────────────────────────────────────


# ─── Badge landing and PIN ──────────────────────────────────────────────


async def test_the_badge_landing_asks_for_the_desk_first_then_for_a_pin(make_world):
    w = await make_world()
    await w.turn_on(hunt_name="Treasure Trail")
    reg = await w.guest("Anjali Rao", checked_in=False)
    r = await w.client.post(gp("/session/badge"), json={"token": w.badge(reg)})
    assert r.status_code == 409 and r.json()["error_code"] == "NOT_CHECKED_IN" and "desk" in r.json()["message"]
    await w.check_in(reg)
    ok = await w.client.post(gp("/session/badge"), json={"token": w.badge(reg)})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["needs_pin"] is True and body["event"]["id"] == w.eid and body["cards"] == {"on": True, "name": "Hero Cards"} and body["hunt"]["name"] == "Treasure Trail"
    assert body["theme"] == "neon_arcade" and body["name"] == "Anjali" and "session" not in body
    forged = await w.client.post(gp("/session/badge"), json={"token": w.badge(reg)[:-3] + "AAA"})
    assert forged.status_code == 422 and forged.json()["error_code"] == "BAD_CODE"
    off = await make_world("No Games")
    other = await off.guest()
    assert (await w.client.post(gp("/session/badge"), json={"token": off.badge(other)})).json()["error_code"] == "GAME_NOT_ON"


async def test_five_wrong_pins_lock_ten_minutes_and_the_right_one_signs_in(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("Anjali Rao")
    badge = w.badge(reg)
    right = tokens.pin(w.eid, reg.rid, 0)
    wrong = "0000" if right != "0000" else "1111"
    left = []
    for _ in range(4):
        r = await w.client.post(gp("/session/pin"), json={"token": badge, "pin": wrong})
        assert r.status_code == 403 and r.json()["error_code"] == "PIN_WRONG"
        left.append(r.json()["errors"]["attempts_left"])
    assert left == [4, 3, 2, 1]
    fifth = await w.client.post(gp("/session/pin"), json={"token": badge, "pin": wrong})
    assert fifth.status_code == 423 and fifth.json()["error_code"] == "PIN_LOCKED"
    until = datetime.fromisoformat(fifth.json()["errors"]["locked_until"].replace("Z", "+00:00"))
    assert timedelta(minutes=9) < until - datetime.now(timezone.utc) <= timedelta(minutes=10, seconds=5)
    # Even the right PIN is refused while locked.
    locked = await w.client.post(gp("/session/pin"), json={"token": badge, "pin": right})
    assert locked.status_code == 423 and locked.json()["error_code"] == "PIN_LOCKED"
    await w.session.execute(update(GamePlayers).where(GamePlayers.registration_id == reg.rid).values(pin_locked_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await w.session.commit()
    ok = await w.client.post(gp("/session/pin"), json={"token": badge, "pin": right})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["token"] == body["session"] and body["token"].split(".")[:2] == ["g", str(w.eid)] and body["expires_at"] and body["player"]["name"] == "Anjali R."
    assert (await w.client.get(games(w.eid, "/me"), headers=game(body["token"]))).json()["player"]["player_no"] == 1


async def test_a_new_badge_pin_sign_in_ends_the_old_session_but_the_account_route_keeps_the_same_one(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("Anjali Rao")
    pin = tokens.pin(w.eid, reg.rid, 0)
    first = (await w.client.post(gp("/session/pin"), json={"token": w.badge(reg), "pin": pin})).json()["token"]
    assert (await w.client.get(games(w.eid, "/me"), headers=game(first))).status_code == 200
    second = (await w.client.post(gp("/session/pin"), json={"token": w.badge(reg), "pin": pin})).json()["token"]
    assert second != first
    old = await w.client.get(games(w.eid, "/me"), headers=game(first))
    assert old.status_code == 401 and old.json()["error_code"] == "SESSION_ENDED"
    assert (await w.client.get(games(w.eid, "/me"), headers=game(second))).status_code == 200


async def test_a_signed_in_account_gets_a_session_without_a_pin_and_public_says_whether_it_can_play(make_world, make_user, mint_token, session):
    w = await make_world()
    await w.turn_on()
    user = await make_user(email="acct-guest@example.com", username="acctguest")
    from tests.conftest import auth_header as ah

    headers = ah(await mint_token(user))
    await session.commit()
    anon = (await w.client.get(games(w.eid, "/public"))).json()
    assert anon["me"] is None
    not_reg = (await w.client.get(games(w.eid, "/public"), headers=headers)).json()["me"]
    assert not_reg == {"can_play": False, "reason": "NOT_REGISTERED"}
    reg = await w.guest("Acct Guest", checked_in=False)
    await session.execute(update(type(reg)).where(type(reg).id == reg.rid).values(user_id=user.id))
    await session.commit()
    assert (await w.client.get(games(w.eid, "/public"), headers=headers)).json()["me"] == {"can_play": False, "reason": "NOT_CHECKED_IN"}
    assert (await w.client.post(games(w.eid, "/session"), headers=headers)).json()["error_code"] == "NOT_CHECKED_IN"
    await w.check_in(reg)
    assert (await w.client.get(games(w.eid, "/public"), headers=headers)).json()["me"] == {"can_play": True, "reason": None}
    # Asking whether it can play makes no session.
    assert await session.scalar(select(GamePlayers.id).where(GamePlayers.registration_id == reg.rid)) is None
    a = await w.client.post(games(w.eid, "/session"), headers=headers)
    b = await w.client.post(games(w.eid, "/session"), headers=headers)
    assert a.status_code == 200 and a.json()["token"] == b.json()["token"] and a.json()["expires_at"]
    # A badge + PIN sign-in elsewhere ends it; the account route then answers the new one.
    pin = tokens.pin(w.eid, reg.rid, 0)
    newer = (await w.client.post(gp("/session/pin"), json={"token": w.badge(reg), "pin": pin})).json()["token"]
    assert (await w.client.get(games(w.eid, "/me"), headers=game(a.json()["token"]))).status_code == 401
    assert (await w.client.post(games(w.eid, "/session"), headers=headers)).json()["token"] == newer


async def test_my_code_shows_the_players_own_badge_and_pin_and_a_reset_kills_the_old_code(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("Anjali Rao")
    h = await w.player(reg)
    code = (await w.client.get(games(w.eid, "/me/code"), headers=h)).json()
    assert code["token"] == w.badge(reg) and code["url"].endswith(f"/g/{code['token']}") and code["pin"] == tokens.pin(w.eid, reg.rid, 0)
    # Lock them, then the help desk resets: a new code and PIN, the lock gone, the old badge dead.
    await w.session.execute(update(GamePlayers).where(GamePlayers.registration_id == reg.rid).values(pin_locked_until=datetime.now(timezone.utc) + timedelta(minutes=5)))
    await w.session.commit()
    reset = await w.client.post(org(w.eid, f"/players/{reg.rid}/reset-pin"), headers=w.club_h)
    assert reset.status_code == 200
    fresh = reset.json()
    assert fresh["token"] == w.badge(reg, 1) and fresh["pin"] == tokens.pin(w.eid, reg.rid, 1) and fresh["registration_id"] == str(reg.rid)
    old = await w.client.post(gp("/session/badge"), json={"token": w.badge(reg)})
    assert old.status_code == 422 and old.json()["error_code"] == "BAD_CODE"
    ok = await w.client.post(gp("/session/pin"), json={"token": fresh["token"], "pin": fresh["pin"]})
    assert ok.status_code == 200
    assert (await w.client.get(org(w.eid, f"/players/{reg.rid}/code"), headers=w.club_h)).json()["pin"] == fresh["pin"]


async def test_the_pin_can_be_switched_off_and_the_badge_then_signs_in(make_world):
    w = await make_world()
    await w.turn_on()
    await w.client.patch(org(w.eid, "/cards/config"), json={"pin_required": False}, headers=w.club_h)
    reg = await w.guest("Anjali Rao")
    r = (await w.client.post(gp("/session/badge"), json={"token": w.badge(reg)})).json()
    assert r["needs_pin"] is False and r["token"].startswith("g.")


# ─── The plan gate ──────────────────────────────────────────────────────


def _plans_migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "club_plans.py"
    spec = importlib.util.spec_from_file_location("club_plans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_club_without_the_plan_feature_gets_the_usual_plan_notice(make_world, session, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    w = await make_world()
    await w.turn_on()
    reg = await w.guest()
    h = await w.player(reg)
    module = _plans_migration()
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            module.seed_plans()

    await connection.run_sync(seed)
    await session.commit()
    monkeypatch.setattr(app_settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(app_settings, "PLAN_ENFORCEMENT_FROM", datetime(2020, 1, 1, tzinfo=timezone.utc))
    gates.reset()
    # Organiser writes: 403 PLAN_FEATURE_LOCKED naming the flag. Reads still work.
    for path, body, method in (("/settings", {"cards_on": True}, "patch"), ("/cards/config", {"preset": "easy"}, "patch"), ("/hunt", {"mode": "free"}, "put"), ("/screen/rotate", None, "post")):
        r = await getattr(w.client, method)(org(w.eid, path), **({"json": body} if body is not None else {}), headers=w.club_h)
        assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED", (path, r.text)
    assert r.json()["errors"]["feature"] in ("game_cards", "game_hunt") and r.json()["errors"]["upgrade_url"]
    assert (await w.client.get(org(w.eid, "/settings"), headers=w.club_h)).json()["plan"] == {"game_cards": False, "game_hunt": False}
    assert (await w.client.get(org(w.eid, "/cards/heroes"), headers=w.club_h)).status_code == 200
    # Players: the same refusal where the game is used, and the landing shows nothing on.
    assert (await w.client.post(games(w.eid, "/cards/reveal"), headers=h)).json()["error_code"] == "PLAN_FEATURE_LOCKED"
    assert (await w.client.get(games(w.eid, "/hunt"), headers=h)).json()["error_code"] == "PLAN_FEATURE_LOCKED"
    pub = (await w.client.get(games(w.eid, "/public"))).json()
    assert pub["cards"] is None and pub["hunt"] is None
    assert (await w.client.post(gp("/session/badge"), json={"token": w.badge(reg)})).json()["error_code"] == "GAME_NOT_ON"
    # A platform admin ticks the flag on the plan: it works again.
    free = await session.scalar(select(Plans).where(Plans.key == "free"))
    await session.execute(update(PlanFeatures).where(PlanFeatures.plan_id == free.id).values(game_cards=True))
    await session.commit()
    gates.reset()
    assert (await w.client.post(games(w.eid, "/cards/reveal"), headers=h)).status_code == 200
    assert (await w.client.get(games(w.eid, "/hunt"), headers=h)).json()["error_code"] == "PLAN_FEATURE_LOCKED"
    assert (await w.client.patch(org(w.eid, "/cards/config"), json={"preset": "easy"}, headers=w.club_h)).status_code == 200


# ─── Rights ─────────────────────────────────────────────────────────────


async def test_only_the_events_own_club_may_run_its_games(make_world, make_club, mint_token, session):
    w = await make_world()
    stranger, _ = await make_club(name="Strangers", email="strangers-games@example.com")
    await session.commit()
    h = auth_header(await mint_token(stranger))
    assert (await w.client.get(org(w.eid, "/settings"), headers=h)).status_code in (403, 404)
    assert (await w.client.patch(org(w.eid, "/settings"), json={"cards_on": True}, headers=h)).status_code in (403, 404)
    assert (await w.client.get(org(w.eid, "/settings"))).status_code == 401


# ─── Settings, look, screens ────────────────────────────────────────────


async def test_the_look_and_names_are_validated_and_saved(make_world):
    w = await make_world()
    base = org(w.eid, "/settings")
    first = (await w.client.get(base, headers=w.club_h)).json()
    assert first["cards_on"] is False and first["theme"] == "neon_arcade" and first["board_rows"] == 7 and first["screen_version"] == 1
    saved = await w.client.patch(base, json={"theme": "ocean_glow", "board_layout": "list", "board_rows": 10, "ticker_on": False, "join_box_on": False, "cards_name": "Squad Cards", "accent": "#00ffaa"}, headers=w.club_h)
    assert saved.status_code == 200 and saved.json()["theme"] == "ocean_glow" and saved.json()["board_rows"] == 10 and saved.json()["ticker_on"] is False
    for bad in ({"theme": "pink"}, {"board_rows": 6}, {"board_rows": 500}, {"cards_name": "x" * 41}, {"board_layout": "grid"}, {"accent": "red"}):
        assert (await w.client.patch(base, json=bad, headers=w.club_h)).status_code == 422
    pub = (await w.client.get(games(w.eid, "/public"))).json()
    assert pub["theme"] == "ocean_glow" and pub["cards"] is None


async def test_the_screen_colours_are_hex_saved_cleared_and_warned_about(make_world):
    w = await make_world()
    base = org(w.eid, "/settings")
    assert (await w.client.get(base, headers=w.club_h)).json()["colors"] == {k: None for k in ("background", "surface", "text", "accent", "gold", "silver", "bronze")}
    for bad in ({"colors": {"background": "red"}}, {"colors": {"gold": "#12345"}}, {"colors": {"text": "#GGGGGG"}}, {"colors": {"glow": "#000000"}}):
        assert (await w.client.patch(base, json=bad, headers=w.club_h)).status_code == 422
    saved = (await w.client.patch(base, json={"colors": {"background": "#0a0a0a", "surface": "#1b1b1b", "text": "#ffffff", "gold": "#ffd700"}}, headers=w.club_h)).json()
    assert saved["colors"]["background"] == "#0A0A0A" and saved["colors"]["gold"] == "#FFD700" and saved["colors"]["silver"] is None and saved["color_warnings"] == []
    murky = (await w.client.patch(base, json={"colors": {"background": "#222222", "text": "#333333"}}, headers=w.club_h)).json()
    assert murky["colors"]["surface"] is None and [x["code"] for x in murky["color_warnings"]] == ["LOW_CONTRAST"]
    await w.client.patch(base, json={"board_rows": 5}, headers=w.club_h)
    assert (await w.client.get(base, headers=w.club_h)).json()["colors"]["text"] == "#333333"
    token = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()["token"]
    assert (await w.client.get(gp(f"/screen/{token}"))).json()["colors"]["background"] == "#222222"
    cleared = (await w.client.patch(base, json={"colors": None}, headers=w.club_h)).json()
    assert set(cleared["colors"].values()) == {None} and cleared["color_warnings"] == []


async def test_a_screen_set_to_everyone_gets_the_whole_board_and_the_stream_stays_small(make_world):
    w = await make_world()
    await w.turn_on()
    for reg in await w.guests(14):
        await w.client.post(games(w.eid, "/cards/reveal"), headers=await w.player(reg))
    token = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()["token"]
    assert len((await w.client.get(gp(f"/screen/{token}"))).json()["boards"]["cards"]["rows"]) == 10
    assert (await w.client.patch(org(w.eid, "/settings"), json={"board_rows": 0}, headers=w.club_h)).json()["board_rows"] == 0
    screen = (await w.client.get(gp(f"/screen/{token}"))).json()
    rows = screen["boards"]["cards"]["rows"]
    assert screen["board_rows"] == 0 and len(rows) == 14 == screen["boards"]["cards"]["of"] and [r["rank"] for r in rows] == list(range(1, 15))
    from app.api.games import views

    settings = await common.settings_of(w.session, w.eid)
    event = await common.event_of(w.session, w.eid)
    assert len((await views.topic(w.session, event, settings, "board"))["cards"]["rows"]) == 10
    for rows_ in (25, 50):
        assert (await w.client.patch(org(w.eid, "/settings"), json={"board_rows": rows_}, headers=w.club_h)).status_code == 200


async def test_a_logo_is_uploaded_as_a_picture_and_shows_on_the_screens(make_world, fake_s3):
    w = await make_world()
    await w.turn_on()
    out = io.BytesIO()
    Image.new("RGB", (400, 400), (10, 120, 200)).save(out, "PNG")
    r = await w.client.post(org(w.eid, "/settings/logo"), files={"logo": ("logo.png", out.getvalue(), "image/png")}, headers=w.club_h)
    assert r.status_code == 200, r.text
    assert r.json()["logo_url"] and any("events/games/logos/" in k for k in fake_s3)
    assert (await w.client.get(games(w.eid, "/public"))).json()["logo_url"] == r.json()["logo_url"]
    bad = await w.client.post(org(w.eid, "/settings/logo"), files={"logo": ("x.txt", b"hello", "text/plain")}, headers=w.club_h)
    assert bad.status_code == 422 and bad.json()["error_code"] == "LOGO_NOT_IMAGE"


async def test_a_screen_link_shows_everything_read_only_and_rotating_ends_the_old_one(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("Anjali Rao")
    await w.client.post(games(w.eid, "/cards/reveal"), headers=await w.player(reg))
    link = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()
    assert link["url"].endswith(f"/screen/{link['token']}") and link["token"].split(".")[:3] == ["s", str(w.eid), "1"]
    screen = (await w.client.get(gp(f"/screen/{link['token']}"))).json()
    assert screen["boards"]["cards"]["rows"][0]["name"] == "Anjali R." and screen["boards"]["hunt"]["rows"] == []
    assert screen["theme"] == "neon_arcade" and [f["type"] for f in screen["feed"]][0] in ("legendary_revealed", "legendary_hidden")
    assert set(screen) >= {"theme", "board_layout", "board_rows", "ticker_on", "join_box_on", "boards", "feed", "hunt_timer", "cards", "hunt", "event", "server_now"}
    assert "registration" not in str(screen) and "email" not in str(screen)
    rotated = (await w.client.post(org(w.eid, "/screen/rotate"), headers=w.club_h)).json()
    assert rotated["token"] != link["token"] and rotated["url"] and (await w.client.get(org(w.eid, "/settings"), headers=w.club_h)).json()["screen_version"] == 2
    gone = await w.client.get(gp(f"/screen/{link['token']}"))
    assert gone.status_code == 404 and gone.json()["error_code"] == "SCREEN_NOT_FOUND"
    assert (await w.client.get(gp(f"/screen/{rotated['token']}"))).status_code == 200
    assert (await w.client.get(gp("/screen/s.1.1.AAAA"))).status_code == 404


async def test_the_ticker_can_be_turned_off_and_one_event_hidden(make_world):
    w = await make_world()
    await w.turn_on()
    await w.client.patch(org(w.eid, "/cards/config"), json={"shares": {"common": 0, "uncommon": 0, "rare": 100, "legendary": 0}, "legendary_floor": 1}, headers=w.club_h)
    await w.guests(4)
    reg = await w.guest("Meera Pillai")
    await w.client.post(games(w.eid, "/cards/reveal"), headers=await w.player(reg))
    token = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()["token"]
    shown = (await w.client.get(gp(f"/screen/{token}"))).json()["feed"]
    assert len(shown) == 1 and shown[0]["type"] == "rare_revealed"
    hidden = await w.client.patch(org(w.eid, f"/feed/{shown[0]['id']}"), json={"hidden": True}, headers=w.club_h)
    assert hidden.json()["hidden"] is True and (await w.client.get(gp(f"/screen/{token}"))).json()["feed"] == []
    assert len((await w.client.get(org(w.eid, "/feed"), headers=w.club_h)).json()) == 1
    await w.client.patch(org(w.eid, f"/feed/{shown[0]['id']}"), json={"hidden": False}, headers=w.club_h)
    await w.client.patch(org(w.eid, "/settings"), json={"ticker_on": False}, headers=w.club_h)
    assert (await w.client.get(gp(f"/screen/{token}"))).json()["feed"] == []


async def test_the_stream_sends_each_topic_as_a_frame_and_only_what_changed_after(make_world, session, monkeypatch):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("Anjali Rao")
    await stream.hub.close()

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(stream, "session_scope", scope)
    try:
        feed = stream.hub.feed(w.eid)
        for _ in range(100):
            if len(feed.bodies) == 4:
                break
            await asyncio.sleep(0.05)
        assert set(feed.bodies) == {"board", "feed", "hunt", "settings"}
        gen = stream._events(feed, None, 30)
        stream.hub.open += 1
        text = b""
        async for chunk in gen:
            text += chunk
            if text.count(b"event: ") >= 4:
                break
        for name in ("board", "feed", "hunt", "settings"):
            assert f"event: {name}\n".encode() in text
        assert text.startswith(b"retry: 3000") and b"id: " in text
        newest = max(feed.versions.values())
        await gen.aclose()
        # A client that has the newest version is sent nothing but a ping once it asks.
        stream.hub.open += 1
        gen2 = stream._events(feed, newest, 30)
        first = await gen2.__anext__()
        assert first.startswith(b"retry:")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen2.__anext__(), 0.3)
        # A reveal tells the board; the stream rebuilds it and wakes.
        await w.client.post(games(w.eid, "/cards/reveal"), headers=await w.player(reg))
        await bus.drain()
        for _ in range(100):
            if feed.versions["board"] > newest:
                break
            await asyncio.sleep(0.05)
        assert feed.versions["board"] > newest and b"Anjali R." in feed.bodies["board"]
    finally:
        await stream.hub.close()


# ─── Admin overview ─────────────────────────────────────────────────────


async def test_the_admin_overview_counts_events_players_and_what_they_did(make_world, make_admin, mint_token, session):
    w = await make_world()
    await w.turn_on()
    await cards.heroes_of(session, w.eid)
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    for reg in (a, b):
        assert (await w.client.post(games(w.eid, "/cards/reveal"), headers=await w.player(reg))).status_code == 200
    quiet = await make_world("Quiet Night")
    admin = await make_admin(email="games-admin@example.com")
    await session.commit()
    ah = auth_header(await mint_token(admin))
    ov = (await w.client.get(f"{API}/admin/games/overview", headers=ah)).json()
    assert ov["totals"]["cards"]["events"] == 1 and ov["totals"]["cards"]["reveals"] == 2 and ov["totals"]["connected_now"] == 2 and ov["totals"]["cards"]["players"] == 2
    row = ov["rows"][0]
    assert row["event_name"] == "Games Night" and row["club_name"].startswith("Games Night") and row["cards_on"] and row["hunt_on"] and row["players"] == 2 and row["reveals"] == 2
    assert isinstance(ov["plans"], list) and "clubs_with" in ov
    # A window that misses the event.
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    empty = (await w.client.get(f"{API}/admin/games/overview", params={"from": future}, headers=ah)).json()
    assert empty["rows"] == [] and empty["totals"]["cards"]["events"] == 0
    assert (await w.client.get(f"{API}/admin/games/overview", headers=w.club_h)).status_code == 403
    assert quiet.eid not in {r["event_id"] for r in ov["rows"]}


async def test_players_are_listed_with_their_opt_outs_and_pin_locks(make_world):
    w = await make_world()
    await w.turn_on()
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    ha, _ = await w.player(a), await w.player(b)
    await w.client.patch(games(w.eid, "/me"), json={"public": False}, headers=ha)
    listing = (await w.client.get(org(w.eid, "/players"), headers=w.club_h)).json()
    assert listing["total"] == 2 and {i["name"] for i in listing["items"]} == {"Asha Nair", "Bilal Khan"}
    one = next(i for i in listing["items"] if i["name"] == "Asha Nair")
    assert set(one) >= {"registration_id", "name", "email", "player_no", "public", "pin_locked"} and one["public"] is False and one["pin_locked"] is False
    out = (await w.client.get(org(w.eid, "/players"), params={"public": "false"}, headers=w.club_h)).json()
    assert out["total"] == 1 and out["items"][0]["name"] == "Asha Nair"
    assert (await w.client.get(org(w.eid, "/players"), params={"q": "bil"}, headers=w.club_h)).json()["total"] == 1


async def test_a_board_downloads_as_a_spreadsheet_that_cannot_run_a_formula(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("=HYPERLINK(1) Evil")
    await w.client.post(games(w.eid, "/cards/reveal"), headers=await w.player(reg))
    r = await w.client.get(org(w.eid, "/export.csv"), params={"game": "cards"}, headers=w.club_h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("rank,player,name,email") and "'=HYPERLINK" in lines[1]
    assert (await w.client.get(org(w.eid, "/export.csv"), params={"game": "nope"}, headers=w.club_h)).status_code == 422


# ─── ID cards ───────────────────────────────────────────────────────────


async def test_id_card_values_carry_the_game_qr_and_pin_only_when_a_game_is_on(make_world, session):
    from sqlalchemy.orm import joinedload

    from app.api.doc_presets import values as preset_values
    from app.api.events.models import EventRegistrationsLink, Events
    from app.api.games import card_values

    w = await make_world()
    reg = await w.guest("Anjali Rao")

    async def card(prime=True):
        row = await session.scalar(
            select(EventRegistrationsLink).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user)).where(EventRegistrationsLink.id == reg.rid)
        )
        if prime:
            await card_values.prime(session, row.event, [row])
        return preset_values.badge_values(row.event, preset_values.subject_of(row, row.event)).values

    off = await card()
    assert off["game_qr"] == "" and off["game_pin"] == "" and off["holder.game_qr"] == ""
    await w.turn_on()
    on = await card()
    assert on["game_qr"] == tokens.badge_url(w.badge(reg)) and on["holder.game_qr"] == on["game_qr"] and on["game_pin"] == tokens.pin(w.eid, reg.rid, 0)
    # The help desk's reset is what the next card prints.
    await w.client.post(org(w.eid, f"/players/{reg.rid}/reset-pin"), headers=w.club_h)
    assert (await card())["game_pin"] == tokens.pin(w.eid, reg.rid, 1)
    assert "game_qr" in preset_values.BADGE_TOKENS and "game_pin" in preset_values.BADGE_TOKENS
    sample = preset_values.badge_values(on and (await session.get(Events, w.eid)), preset_values.sample_subject(await session.get(Events, w.eid), "badge")).values
    assert sample["game_pin"] == "4821" and sample["holder.game_qr"]




# ─── No database: last in the file (tests/conftest.py's loop) ───

def test_tokens_have_the_contract_shapes_and_reject_forgeries_and_other_kinds():
    reg, point = uuid.uuid4(), uuid.uuid4()
    badge = tokens.badge(12, reg, 3)
    parts = badge.split(".")
    assert parts[0] == "b" and parts[1] == "12" and parts[3] == "3" and len(parts) == 5 and len(badge) < 70
    assert tokens.read_badge(badge) == tokens.Badge(12, reg, 3)
    p = tokens.point(12, point)
    assert p.split(".")[:2] == ["p", "12"] and tokens.read_point(p) == tokens.Point(12, point)
    s = tokens.screen(12, 4)
    assert s.split(".")[:3] == ["s", "12", "4"] and tokens.read_screen(s) == (12, 4)
    # Forged: another event's number, another registration, a changed version, a cut mac.
    assert tokens.read_badge(badge.replace(".12.", ".13.", 1)) is None
    assert tokens.read_badge(badge.replace(f".{parts[3]}.", ".4.", 1)) is None
    assert tokens.read_badge(badge[:-2]) is None and tokens.read_badge("") is None and tokens.read_badge("b.1.2") is None
    assert tokens.read_screen(s.replace(".4.", ".5.", 1)) is None
    # One kind is never another.
    assert tokens.read_point(badge) is None and tokens.read_badge(p) is None and tokens.read_screen(badge) is None and tokens.read_session(badge) is None
    # A scanned address carries the token as its last part.
    assert tokens.read_badge(f"https://myotherapp.in/g/{badge}?utm=1") == tokens.Badge(12, reg, 3)


def test_the_pin_is_four_digits_derived_and_changes_with_its_version():
    reg = uuid.uuid4()
    pin = tokens.pin(5, reg, 0)
    assert len(pin) == 4 and pin.isdigit() and pin == tokens.pin(5, reg, 0)
    assert tokens.pin_matches(5, reg, 0, pin) and tokens.pin_matches(5, reg, 0, f" {pin} ")
    assert len({tokens.pin(5, reg, v) for v in range(30)}) > 20  # a reset gives a different PIN
    assert len({tokens.pin(e, reg, 0) for e in range(30)}) > 20  # and so does another event
    assert not tokens.pin_matches(5, reg, 0, "") and not tokens.pin_matches(5, reg, 0, "abcd")
    assert len({tokens.pin(5, uuid.uuid4(), 0) for _ in range(200)}) > 150


@pytest.mark.parametrize(
    "role,method,path,allowed",
    [
        ("editor", "PATCH", "/api/v1/events/7/games/settings", True),
        ("editor", "POST", "/api/v1/events/7/games/hunt/start", True),
        ("door", "GET", "/api/v1/events/7/games/settings", True),
        ("door", "GET", "/api/v1/events/7/games/cards/live", True),
        ("door", "GET", "/api/v1/events/7/games/hunt/live", True),
        ("door", "PATCH", "/api/v1/events/7/games/settings", False),
        ("door", "POST", "/api/v1/events/7/games/hunt/start", False),
        ("door", "POST", "/api/v1/events/7/games/players/5b0c1f2e-8a43-4f0e-9d3c-2b7f6a1e9c40/reset-pin", False),
        ("door", "GET", "/api/v1/events/7/games/export.csv", False),
        ("editor", "GET", "/api/v1/events/7/games/export.csv", True),
        ("door", "GET", "/api/v1/events/7/games/hunt/points.pdf", True),
    ],
)
def test_owners_and_editors_edit_door_staff_only_look(role, method, path, allowed):
    assert policy.allows(role, method, path) is allowed


def test_a_game_qr_layer_and_the_game_tokens_validate_on_an_id_card_only():
    from app.core import layout_v2 as v2

    qr = {"id": "qr", "name": "QR code", "kind": "qr", "x": 250, "y": 440, "width": 108, "height": 108}
    game_qr = {"id": "gqr", "name": "Game QR", "kind": "qr", "x": 20, "y": 440, "width": 108, "height": 108, "bind": "holder.game_qr"}
    text = {"id": "t1", "name": "PIN", "kind": "text", "x": 20, "y": 20, "width": 200, "height": 40, "text": "PIN {{game_pin}}", "fontSize": 20}

    def lay(*layers):
        return {"version": 2, "page": {"widthMm": 101.6, "heightMm": 152.4}, "canvas": {"width": 384, "height": 576}, "layers": list(layers)}

    def codes(layout, kind):
        return {p["code"] for p in v2.problems(layout, kind) if p["level"] == "error"}

    assert "qr_profile_kind" not in codes(lay(qr, game_qr, text), "badge") and "unknown_token" not in codes(lay(qr, game_qr, text), "badge")
    assert "qr_profile_kind" in codes(lay(qr, game_qr), "ticket")
    assert "qr_extra" in codes(lay(qr, game_qr, {**game_qr, "id": "g2"}), "badge")




async def test_a_screen_shows_the_host_club_and_the_partners_with_logos_in_page_order(make_world, fake_s3):
    from app.api.event_partners.models import EventPartners

    def png():
        out = io.BytesIO()
        Image.new("RGB", (200, 200), (10, 120, 200)).save(out, "PNG")
        return {"bytes": out.getvalue(), "filename": "logo.png"}

    w = await make_world()
    await w.turn_on()
    w.club.logo = png()
    for kind, name, pos, logo in (
        ("partner", "Second Partner", 1, png()),
        ("partner", "First Partner", 0, png()),
        ("sponsor", "Title Sponsor", 0, png()),
        ("partner", "No Logo", 2, None),
    ):
        w.session.add(EventPartners(event_id=w.eid, kind=kind, name=name, position=pos, logo=logo, label="Title" if kind == "sponsor" else None))
    await w.session.commit()
    token = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()["token"]
    screen = (await w.client.get(gp(f"/screen/{token}"))).json()
    assert screen["club"]["name"] == w.club.name and "clubs/logos/" in screen["club"]["logo_url"]
    assert [p["name"] for p in screen["partners"]] == ["Title Sponsor", "First Partner", "Second Partner"]
    first = screen["partners"][0]
    assert first["kind"] == "sponsor" and first["label"] == "Title" and "events/partners/" in first["logo_url"]
    assert all(p["logo_url"] for p in screen["partners"])


async def test_a_screen_without_a_club_logo_or_partners_sends_an_empty_strip(make_world):
    w = await make_world()
    await w.turn_on()
    token = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()["token"]
    screen = (await w.client.get(gp(f"/screen/{token}"))).json()
    assert screen["club"] == {"name": w.club.name, "logo_url": None} and screen["partners"] == []


async def test_a_screen_lists_the_cohost_clubs_with_logos_and_a_footer_image_stands_in_for_the_logos(make_world, make_club, fake_s3):
    from app.api.cohosts.models import EventCohosts

    def png(w=200, h=200):
        out = io.BytesIO()
        Image.new("RGB", (w, h), (10, 120, 200)).save(out, "PNG")
        return out.getvalue()

    w = await make_world()
    await w.turn_on()
    _, with_logo = await make_club(name="Co Club", email="co-club@example.com")
    _, no_logo = await make_club(name="Plain Club", email="plain-club@example.com")
    _, invited = await make_club(name="Invited Club", email="invited-club@example.com")
    with_logo.logo = {"bytes": png(), "filename": "logo.png"}
    for club, status in ((with_logo, "accepted"), (no_logo, "accepted"), (invited, "invited")):
        w.session.add(EventCohosts(event_id=w.eid, club_id=club.id, status=status, invited_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc)))
    await w.session.commit()
    token = (await w.client.get(org(w.eid, "/screen"), headers=w.club_h)).json()["token"]
    screen = (await w.client.get(gp(f"/screen/{token}"))).json()
    assert [c["name"] for c in screen["cohosts"]] == ["Co Club"] and screen["cohosts"][0]["logo_url"]
    assert screen["footer_image_url"] is None

    r = await w.client.post(org(w.eid, "/settings/footer-image"), files={"image": ("strip.png", png(3000, 300), "image/png")}, headers=w.club_h)
    assert r.status_code == 200, r.text
    assert "events/games/footers/" in r.json()["footer_image_url"]
    assert (await w.client.get(gp(f"/screen/{token}"))).json()["footer_image_url"] == r.json()["footer_image_url"]
    bad = await w.client.post(org(w.eid, "/settings/footer-image"), files={"image": ("x.txt", b"hello", "text/plain")}, headers=w.club_h)
    assert bad.status_code == 422 and bad.json()["error_code"] == "FOOTER_NOT_IMAGE"
    gone = await w.client.delete(org(w.eid, "/settings/footer-image"), headers=w.club_h)
    assert gone.status_code == 200 and gone.json()["footer_image_url"] is None
    assert (await w.client.get(gp(f"/screen/{token}"))).json()["footer_image_url"] is None
