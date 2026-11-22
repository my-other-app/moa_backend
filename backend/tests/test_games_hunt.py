"""The Scavenger Hunt (CONTRACT §5, SPEC §5 and §13): points, states, claims, the early bonus, ordered mode,
the start rotation, hints, the end and extend, the QR sheet."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.api.games import tokens
from app.api.games.models import GameHuntClaims, GameHunts, GameHuntScores
from tests.games_world import game, games, make_world, org  # noqa: F401


async def _setup(w, n_points=5, mode="ordered", **hunt_fields):
    await w.turn_on()
    r = await w.client.put(org(w.eid, "/hunt"), json={"mode": mode, "duration_minutes": 60, **hunt_fields}, headers=w.club_h)
    assert r.status_code == 200, r.text
    ids = []
    for i in range(1, n_points + 1):
        p = await w.client.post(org(w.eid, "/hunt/points"), json={"label": f"Booth {i}", "clue": f"Clue number {i}", "zone": f"Zone {i}", "base_points": 30, "hint": f"Look near {i}"}, headers=w.club_h)
        assert p.status_code == 201, p.text
        ids.append(uuid.UUID(p.json()["id"]))
    return ids


async def _start(w):
    r = await w.client.post(org(w.eid, "/hunt/start"), headers=w.club_h)
    assert r.status_code == 200, r.text
    return r.json()


async def _scan(w, headers, point_id, token=None):
    return await w.client.post(games(w.eid, "/hunt/scan"), json={"token": token or tokens.point(w.eid, point_id)}, headers=headers)


async def _view(w, headers):
    return (await w.client.get(games(w.eid, "/hunt"), headers=headers)).json()


def _by_number(ids):
    return {i + 1: pid for i, pid in enumerate(ids)}


async def test_a_hunt_is_set_up_with_points_and_never_shows_a_label_to_a_player(make_world):
    w = await make_world()
    ids = await _setup(w)
    view = (await w.client.get(org(w.eid, "/hunt"), headers=w.club_h)).json()
    assert view["status"] == "draft" and len(view["points"]) == 5 and [p["number"] for p in view["points"]] == [1, 2, 3, 4, 5]
    assert view["points"][0]["code"] == tokens.point(w.eid, ids[0]) and view["mode"] == "ordered"
    assert view["bonus_n"] == 10 and view["bonus_pts"] == 20 and view["hint_cost"] == 10
    reg = await w.guest()
    h = await w.player(reg)
    early = await _view(w, h)
    assert early["status"] == "draft" and early["current"] is None
    await _start(w)
    live = await _view(w, h)
    assert live["status"] == "live" and live["current"]["clue"].startswith("Clue number") and "label" not in live["current"] and "Booth" not in str(live)
    assert live["ends_at"] and live["server_now"] and live["total_points"] == 5 and live["points"] == 0 and live["rank"] is None


async def test_one_claim_per_point_per_player_and_the_first_n_get_the_early_bonus(make_world):
    w = await make_world()
    ids = await _setup(w, 1, bonus_n=2, bonus_pts=20)
    await _start(w)
    got = []
    for i in range(3):
        reg = await w.guest(f"Racer {chr(65 + i)}one")
        r = await _scan(w, await w.player(reg), ids[0])
        assert r.status_code == 200, r.text
        got.append(r.json())
    assert [g["awarded"] for g in got] == [50, 50, 30] and [g["claim_rank"] for g in got] == [1, 2, 3] and [g["bonus"] for g in got] == [20, 20, 0]
    assert all(g["complete"] and g["total"] == g["awarded"] for g in got)
    again = await _scan(w, await w.player(w.regs[0]), ids[0])
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_CLAIMED" and "already found" in again.json()["message"]
    board = (await w.client.get(games(w.eid, "/board?game=hunt"))).json()
    assert [r["score"] for r in board["rows"]] == [50, 50, 30] and board["rows"][0]["rank"] == 1


async def test_ordered_mode_refuses_an_out_of_order_point_without_using_it_up(make_world):
    w = await make_world()
    ids = await _setup(w, 4)
    await _start(w)
    reg = await w.guest()
    h = await w.player(reg)
    current = (await _view(w, h))["current"]
    mine = next(i for i, pid in enumerate(ids) if str(pid) == str(current["point_id"]))
    wrong = ids[(mine + 1) % 4]
    refused = await _scan(w, h, wrong)
    assert refused.status_code == 409 and refused.json()["error_code"] == "OUT_OF_ORDER" and "follow your clue" in refused.json()["message"].lower()
    assert await w.session.scalar(select(GameHuntClaims.id).where(GameHuntClaims.point_id == wrong)) is None
    assert (await _view(w, h))["current"]["point_id"] == current["point_id"]
    ok = await _scan(w, h, ids[mine])
    assert ok.status_code == 200 and ok.json()["found"] == 1
    # The point refused earlier is now exactly the next clue and can still be claimed.
    assert (await _view(w, h))["current"]["point_id"] == str(wrong)
    assert (await _scan(w, h, wrong)).status_code == 200


async def test_starting_clues_are_spread_over_the_players_and_wrap(make_world):
    w = await make_world()
    ids = await _setup(w, 4)
    await _start(w)
    starts = {}
    for i in range(8):
        reg = await w.guest(f"Spread {chr(65 + i)}one")
        h = await w.player(reg)
        view = await _view(w, h)
        starts[i] = view["current"]["number"]
    nums = [starts[i] for i in range(8)]
    assert sorted(set(nums)) == [1, 2, 3, 4]
    assert nums[4:] == nums[:4]  # players 5-8 wrap round to the same starts as 1-4
    assert all(nums[i] != nums[i + 1] for i in range(7))
    # A player's own order after the first: next in number, wrapping.
    reg = w.regs[0]
    h = await w.player(reg)
    first = (await _view(w, h))["current"]["number"]
    await _scan(w, h, _by_number(ids)[first])
    assert (await _view(w, h))["current"]["number"] == first % 4 + 1


async def test_free_mode_lists_every_clue_and_takes_any_order(make_world):
    w = await make_world()
    ids = await _setup(w, 3, mode="free")
    await _start(w)
    reg = await w.guest()
    h = await w.player(reg)
    view = await _view(w, h)
    assert view["current"] is None and [c["number"] for c in view["clues"]] == [1, 2, 3] and all(c["found"] is False for c in view["clues"])
    assert (await _scan(w, h, ids[2])).status_code == 200
    assert (await _scan(w, h, ids[0])).status_code == 200
    again = await _view(w, h)
    assert [c["found"] for c in again["clues"]] == [True, False, True] and again["points"] == 100
    done = await _scan(w, h, ids[1])
    assert done.json()["complete"] is True
    feed = (await w.client.get(org(w.eid, "/feed"), headers=w.club_h)).json()
    assert any(f["type"] == "hunt_complete" for f in feed)


async def test_tokens_that_are_not_this_hunts_are_refused(make_world):
    w = await make_world()
    ids = await _setup(w, 2)
    await _start(w)
    reg = await w.guest()
    h = await w.player(reg)
    other = await make_world("Elsewhere")
    other_ids = await _setup(other, 1)
    for token in ("nonsense", tokens.badge(w.eid, reg.rid, 0), tokens.point(other.eid, other_ids[0]), tokens.point(w.eid, uuid.uuid4()), tokens.point(w.eid, ids[0])[:-3] + "AAA"):
        r = await _scan(w, h, ids[0], token=token)
        assert r.status_code == 422 and r.json()["error_code"] == "BAD_CODE", token
    # A scanned address works as well as the bare token.
    cur = (await _view(w, h))["current"]["point_id"]
    assert (await _scan(w, h, ids[0], token=f"https://myotherapp.in/h/{tokens.point(w.eid, uuid.UUID(cur))}")).status_code == 200


async def test_scans_before_start_while_paused_and_after_the_end_are_refused_and_extending_reopens(make_world):
    w = await make_world()
    ids = await _setup(w, 3, mode="free")
    reg = await w.guest()
    h = await w.player(reg)
    early = await _scan(w, h, ids[0])
    assert early.status_code == 409 and early.json()["error_code"] == "HUNT_NOT_LIVE"
    await _start(w)
    paused = await w.client.post(org(w.eid, "/hunt/pause"), headers=w.club_h)
    assert paused.json()["status"] == "paused"
    refused = await _scan(w, h, ids[0])
    assert refused.status_code == 409 and refused.json()["error_code"] == "HUNT_PAUSED" and refused.json()["message"] == "Hunt paused."
    ends_before = (await w.client.get(org(w.eid, "/hunt"), headers=w.club_h)).json()["ends_at"]
    resumed = await w.client.post(org(w.eid, "/hunt/resume"), headers=w.club_h)
    assert resumed.json()["status"] == "live" and resumed.json()["ends_at"] >= ends_before
    assert (await _scan(w, h, ids[0])).status_code == 200
    ended = await w.client.post(org(w.eid, "/hunt/end"), headers=w.club_h)
    assert ended.json()["status"] == "ended"
    late = await _scan(w, h, ids[1])
    assert late.status_code == 409 and late.json()["error_code"] == "HUNT_NOT_LIVE" and "over" in late.json()["message"]
    reopened = await w.client.post(org(w.eid, "/hunt/extend"), json={"minutes": 15}, headers=w.club_h)
    assert reopened.json()["status"] == "live"
    assert (await _scan(w, h, ids[1])).status_code == 200
    assert (await w.client.post(org(w.eid, "/hunt/extend"), json={"minutes": 0}, headers=w.club_h)).status_code == 422


async def test_the_clock_ends_the_hunt_and_starts_a_scheduled_one(make_world):
    w = await make_world()
    ids = await _setup(w, 2, mode="free")
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    sched = await w.client.put(org(w.eid, "/hunt"), json={"starts_at": soon.isoformat()}, headers=w.club_h)
    assert sched.json()["status"] == "scheduled"
    reg = await w.guest()
    h = await w.player(reg)
    assert (await _scan(w, h, ids[0])).json()["error_code"] == "HUNT_NOT_LIVE"
    row = await w.session.scalar(select(GameHunts).where(GameHunts.event_id == w.eid))
    row.starts_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    row.ends_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await w.session.commit()
    assert (await _scan(w, h, ids[0])).status_code == 200  # live by the clock, nobody pressed Start
    row = await w.session.scalar(select(GameHunts).where(GameHunts.event_id == w.eid))
    row.ends_at = datetime.now(timezone.utc) - timedelta(seconds=30)  # past the 2 s grace
    await w.session.commit()
    over = await _scan(w, h, ids[1])
    assert over.status_code == 409 and over.json()["error_code"] == "HUNT_NOT_LIVE"
    assert (await _view(w, h))["status"] == "ended"
    # Inside the grace, a scan still counts.
    row = await w.session.scalar(select(GameHunts).where(GameHunts.event_id == w.eid))
    row.status, row.ends_at = "live", datetime.now(timezone.utc) - timedelta(milliseconds=800)
    await w.session.commit()
    assert (await _scan(w, h, ids[1])).status_code == 200


async def test_a_hint_costs_points_once_and_never_takes_the_total_below_zero(make_world):
    w = await make_world()
    ids = await _setup(w, 3, mode="free", hint_cost=10)
    await _start(w)
    reg = await w.guest()
    h = await w.player(reg)
    broke = await w.client.post(games(w.eid, "/hunt/hint"), json={"point_id": str(ids[0])}, headers=h)
    assert broke.status_code == 409 and broke.json()["error_code"] == "NOT_ENOUGH_POINTS"
    assert (await w.client.post(games(w.eid, "/hunt/hint"), json={}, headers=h)).json()["error_code"] == "POINT_REQUIRED"
    r = await _scan(w, h, ids[0])
    assert r.status_code == 200 and r.json()["awarded"] == 50  # 30 + the early bonus
    bought = await w.client.post(games(w.eid, "/hunt/hint"), json={"point_id": str(ids[1])}, headers=h)
    assert bought.status_code == 200 and bought.json() == {"point_id": str(ids[1]), "hint": "Look near 2", "cost": 10, "charged": True, "points": 40}
    again = await w.client.post(games(w.eid, "/hunt/hint"), json={"point_id": str(ids[1])}, headers=h)
    assert again.json()["charged"] is False and again.json()["points"] == 40 and again.json()["hint"] == "Look near 2"
    view = await _view(w, h)
    assert view["points"] == 40 and next(c for c in view["clues"] if c["number"] == 2)["hint"] == "Look near 2"
    # With 40 points, three more hints would not fit under zero: a cost over the total is refused, never negative.
    await w.client.put(org(w.eid, "/hunt"), json={"hint_cost": 500}, headers=w.club_h)
    expensive = await w.client.post(games(w.eid, "/hunt/hint"), json={"point_id": str(ids[2])}, headers=h)
    assert expensive.json()["error_code"] == "NOT_ENOUGH_POINTS"
    assert (await w.session.scalar(select(GameHuntScores.total).where(GameHuntScores.registration_id == reg.rid))) == 40
    claimed_hint = await w.client.post(games(w.eid, "/hunt/hint"), json={"point_id": str(ids[0])}, headers=h)
    assert claimed_hint.json()["error_code"] == "ALREADY_CLAIMED"


async def test_a_new_leader_is_announced_once_and_scores_change_rank(make_world):
    w = await make_world()
    ids = await _setup(w, 2, mode="free", bonus_n=0)
    await _start(w)
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    ha, hb = await w.player(a), await w.player(b)
    await _scan(w, ha, ids[0])
    await _scan(w, hb, ids[0])
    await _scan(w, hb, ids[1])
    feed = [f for f in (await w.client.get(org(w.eid, "/feed"), headers=w.club_h)).json() if f["type"] == "hunt_leader"]
    # One announcement for each change of leader (the suite's clock is one transaction, so the order is not asserted).
    assert sorted(("Asha N." in f["message"], "Bilal K." in f["message"]) for f in feed) == [(False, True), (True, False)]
    me = (await w.client.get(games(w.eid, "/me"), headers=ha)).json()
    assert me["hunt"] == {"name": "Scavenger Hunt", "status": "live", "points": 30, "found": 1, "rank": 2, "of": 2}


async def test_points_are_fixed_once_the_hunt_starts_and_the_qr_sheet_has_one_page_each(make_world):
    w = await make_world()
    ids = await _setup(w, 3)
    pdf = await w.client.get(org(w.eid, "/hunt/points.pdf"), headers=w.club_h)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
    import pypdfium2

    doc = pypdfium2.PdfDocument(pdf.content)
    assert len(doc) == 3
    text = [doc[i].get_textpage().get_text_range() for i in range(3)]
    assert "#1" in text[0] and "Booth 1" in text[0] and "Clue number" not in "".join(text)
    await _start(w)
    added = await w.client.post(org(w.eid, "/hunt/points"), json={"label": "Late", "clue": "Too late"}, headers=w.club_h)
    assert added.status_code == 409 and added.json()["error_code"] == "HUNT_STARTED"
    edited = await w.client.patch(org(w.eid, f"/hunt/points/{ids[0]}"), json={"clue": "A better clue"}, headers=w.club_h)
    assert edited.status_code == 200 and edited.json()["clue"] == "A better clue"
    reg = await w.guest()
    hh = await w.player(reg)
    now_clue = (await _view(w, hh))["current"]["point_id"]
    assert (await _scan(w, hh, uuid.UUID(now_clue))).status_code == 200
    assert (await w.client.put(org(w.eid, "/hunt"), json={"mode": "free"}, headers=w.club_h)).json()["error_code"] == "HUNT_HAS_CLAIMS"
    live = (await w.client.get(org(w.eid, "/hunt/live"), headers=w.club_h)).json()
    assert live["scans"] == 1 and sum(p["scans"] for p in live["points"]) == 1 and live["flagged"] == []


async def test_three_claims_in_different_zones_inside_a_minute_are_flagged(make_world):
    w = await make_world()
    ids = await _setup(w, 4, mode="free")
    await _start(w)
    cheat, honest = await w.guest("Quick Fingers"), await w.guest("Slow Walker")
    hc, hh = await w.player(cheat), await w.player(honest)
    for pid in ids[:3]:
        assert (await _scan(w, hc, pid)).status_code == 200
    await _scan(w, hh, ids[0])
    flagged = (await w.client.get(org(w.eid, "/hunt/live"), headers=w.club_h)).json()["flagged"]
    assert [f["name"] for f in flagged] == ["Quick Fingers"] and flagged[0]["claims"] == 3 and len(flagged[0]["zones"]) == 3
