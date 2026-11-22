"""Hero Cards (feature-register/games/CONTRACT.md §5, SPEC §4 and §13): setup, reveal, the Legendary
floor, collect, deck complete, hero edits, and 300 reveals at once."""
import asyncio
import io
import uuid
from datetime import datetime, timedelta, timezone

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.checkins.models import CheckIns, CheckInVia
from app.api.games import cards, common, tokens
from app.api.games.models import GameCardScores, GameDeckItems, GameHeroes, GameReveals, GameTierCounters
from app.db.core import SESSION_KWARGS
from app.db.listeners import add_loader_criteria
from tests.conftest import _make_user
from tests.games_world import game, games, make_world, org  # noqa: F401
from tests.racing import committed_keys, sweep


async def _reveal(w, reg, headers=None):
    return await w.client.post(games(w.eid, "/cards/reveal"), headers=headers or await w.player(reg))


async def _config(w, **body):
    r = await w.client.patch(org(w.eid, "/cards/config"), json=body, headers=w.club_h)
    return r


# ─── Setup ──────────────────────────────────────────────────────────────


async def test_switching_cards_on_makes_ten_default_heroes_and_the_medium_rules(make_world):
    w = await make_world()
    s = await w.turn_on(cards_name="Squad Cards")
    assert s["cards_on"] and s["cards_name"] == "Squad Cards" and s["hunt_name"] == "Scavenger Hunt"
    heroes = (await w.client.get(org(w.eid, "/cards/heroes"), headers=w.club_h)).json()
    assert len(heroes) == 10 and [h["number"] for h in heroes] == list(range(1, 11))
    tiers = [h["tier"] for h in heroes]
    assert [tiers.count(t) for t in ("common", "uncommon", "rare", "legendary")] == [4, 3, 2, 1]
    assert all(len(h["name"]) <= 18 and len(h["tagline"]) <= 60 for h in heroes)
    config = (await w.client.get(org(w.eid, "/cards/config"), headers=w.club_h)).json()
    assert config["preset"] == "medium" and config["shares"] == {"common": 52, "uncommon": 30, "rare": 13, "legendary": 5}
    assert config["legendary_floor"] == 4 and config["pin_required"] and config["two_way"] and config["collect_on"] and config["show_holders"]
    public = (await w.client.get(games(w.eid, "/public"))).json()
    assert public["cards"] == {"on": True, "name": "Squad Cards"} and public["event"]["id"] == w.eid and public["me"] is None


async def test_shares_must_total_100_and_every_tier_with_a_share_needs_a_hero(make_world):
    w = await make_world()
    await w.turn_on()
    bad = await _config(w, shares={"common": 50, "uncommon": 30, "rare": 10, "legendary": 5})
    assert bad.status_code == 422 and bad.json()["error_code"] == "SHARES_NOT_100" and bad.json()["errors"]["total"] == 95
    ok = await _config(w, shares={"common": 50, "uncommon": 30, "rare": 15, "legendary": 5})
    assert ok.status_code == 200 and ok.json()["preset"] == "custom"
    hard = await _config(w, preset="hard")
    assert hard.json()["shares"] == {"common": 60, "uncommon": 28, "rare": 10, "legendary": 2} and hard.json()["preset"] == "hard"
    # A small event on Hard is warned about, not refused.
    assert [x["code"] for x in hard.json()["warnings"]] == ["SMALL_EVENT_HARD"]
    # Take the only Legendary hero's tier away: refused while Legendary has a share.
    legendary = [h for h in (await w.client.get(org(w.eid, "/cards/heroes"), headers=w.club_h)).json() if h["tier"] == "legendary"][0]
    moved = await w.client.patch(org(w.eid, f"/cards/heroes/{legendary['id']}"), json={"tier": "rare"}, headers=w.club_h)
    assert moved.status_code == 422 and moved.json()["error_code"] == "TIER_NEEDS_HERO"
    zero = await _config(w, shares={"common": 60, "uncommon": 30, "rare": 10, "legendary": 0})
    assert zero.status_code == 200
    assert (await w.client.patch(org(w.eid, f"/cards/heroes/{legendary['id']}"), json={"tier": "rare"}, headers=w.club_h)).status_code == 200


async def test_heroes_are_added_edited_reordered_and_removed_within_four_to_twenty(make_world):
    w = await make_world()
    await w.turn_on()
    base = org(w.eid, "/cards/heroes")
    added = await w.client.post(base, json={"name": "Quartz Hound", "tagline": "Loyal to the end.", "tier": "common"}, headers=w.club_h)
    assert added.status_code == 201 and added.json()["number"] == 11
    assert (await w.client.post(base, json={"name": "x" * 19, "tier": "common"}, headers=w.club_h)).status_code == 422
    edited = await w.client.patch(f"{base}/{added.json()['id']}", json={"name": "Quartz Wolf"}, headers=w.club_h)
    assert edited.json()["name"] == "Quartz Wolf"
    ids = [h["id"] for h in (await w.client.get(base, headers=w.club_h)).json()]
    flipped = await w.client.put(f"{base}/order", json={"ids": ids[::-1]}, headers=w.club_h)
    assert [h["id"] for h in flipped.json()] == ids[::-1] and [h["number"] for h in flipped.json()] == list(range(1, 12))
    assert (await w.client.put(f"{base}/order", json={"ids": ids[:5]}, headers=w.club_h)).json()["error_code"] == "ORDER_MISMATCH"
    assert (await w.client.delete(f"{base}/{added.json()['id']}", headers=w.club_h)).status_code == 200
    # Down to the minimum: the last four cannot go (and each tier keeps a hero).
    for h in (await w.client.get(base, headers=w.club_h)).json():
        left = (await w.client.get(base, headers=w.club_h)).json()
        r = await w.client.delete(f"{base}/{h['id']}", headers=w.club_h)
        if len(left) <= 4:
            assert r.status_code == 422 and r.json()["error_code"] in ("MIN_HEROES", "TIER_NEEDS_HERO")
    assert len((await w.client.get(base, headers=w.club_h)).json()) >= 4


def _png(w=900, h=1200, fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (90, 40, 160)).save(out, fmt)
    return out.getvalue()


async def test_hero_art_is_portrait_big_enough_and_an_image(make_world, fake_s3):
    w = await make_world()
    await w.turn_on()
    hero = (await w.client.get(org(w.eid, "/cards/heroes"), headers=w.club_h)).json()[0]
    url = org(w.eid, f"/cards/heroes/{hero['id']}/art")

    async def up(data, name="a.png", ctype="image/png"):
        return await w.client.post(url, files={"file": (name, data, ctype)}, headers=w.club_h)

    assert (await up(_png(600, 800))).json()["error_code"] == "ART_TOO_SMALL"
    assert (await up(_png(1200, 1200))).json()["error_code"] == "ART_SHAPE"
    assert (await up(b"not a picture")).json()["error_code"] == "ART_NOT_IMAGE"
    assert (await up(b"\x00" * (5 * 1024 * 1024 + 10))).json()["error_code"] == "ART_TOO_BIG"
    good = await up(_png())
    assert good.status_code == 200, good.text
    assert good.json()["art_url"] and any("events/games/heroes/" in k for k in fake_s3)


# ─── Reveal ─────────────────────────────────────────────────────────────


async def test_a_reveal_needs_a_check_in_and_is_the_same_hero_every_time(make_world):
    w = await make_world()
    await w.turn_on()
    late = await w.guest("Late Comer", checked_in=False)
    refused = await _reveal(w, late)
    assert refused.status_code == 409 and refused.json()["error_code"] == "NOT_CHECKED_IN"
    reg = await w.guest("Anjali Rao")
    first = await _reveal(w, reg)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["already"] is False and body["tier"] in ("common", "uncommon", "rare", "legendary") and body["hero"]["name"]
    assert body["deck_count"] == 1 and body["total"] == 10 and body["holders"] == 1
    again = await _reveal(w, reg)
    assert again.json()["already"] is True and again.json()["hero"]["id"] == body["hero"]["id"]
    assert (await w.session.scalar(select(func.count()).select_from(GameReveals).where(GameReveals.event_id == w.eid))) == 1
    # A checked-in-later guest can now play: the desk checks them in.
    await w.check_in(late)
    assert (await _reveal(w, late)).status_code == 200


async def test_reveals_need_a_session_and_the_game_on(make_world):
    w = await make_world()
    reg = await w.guest()
    anon = await w.client.post(games(w.eid, "/cards/reveal"))
    assert anon.status_code == 401 and anon.json()["error_code"] == "SESSION_REQUIRED"
    off = await _reveal(w, reg)
    assert off.status_code == 404 and off.json()["error_code"] == "GAME_NOT_ON"
    other = await make_world("Other Night")
    await other.turn_on()
    foreign = await other.player(await other.guest())
    wrong = await w.client.post(games(w.eid, "/cards/reveal"), headers=foreign)
    assert wrong.status_code == 401 and wrong.json()["error_code"] == "SESSION_INVALID"


async def test_the_legendary_floor_forces_legendary_when_few_are_left(make_world):
    """SPEC §13: a minimum of 4 Legendary with only 40 check-ins."""
    w = await make_world()
    await w.turn_on()
    await _config(w, preset="hard")
    regs = await w.guests(40)
    for reg in regs:
        r = await _reveal(w, reg)
        assert r.status_code == 200, r.text
    tiers = [t for (t,) in (await w.session.execute(select(GameReveals.tier).where(GameReveals.event_id == w.eid))).all()]
    assert len(tiers) == 40 and tiers.count("legendary") >= 4
    # And not many more than the floor: Hard's 2% is under one of 40.
    assert tiers.count("legendary") <= 5


async def test_a_reveal_names_a_rare_or_legendary_hero_in_the_ticker(make_world):
    w = await make_world()
    await w.turn_on()
    await _config(w, shares={"common": 0, "uncommon": 0, "rare": 100, "legendary": 0}, legendary_floor=1)
    await w.guests(5)
    reg = await w.guest("Meera Pillai")
    assert (await _reveal(w, reg)).json()["tier"] == "rare"
    feed = (await w.client.get(org(w.eid, "/feed"), headers=w.club_h)).json()
    assert feed[0]["type"] == "rare_revealed" and "Meera P." in feed[0]["message"] and "Meera Pillai" not in feed[0]["message"]


# ─── Collect ────────────────────────────────────────────────────────────


async def _two_revealed(w):
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    ra, rb = (await _reveal(w, a)).json(), (await _reveal(w, b)).json()
    return a, b, ra, rb


async def _collect(w, collector, owner, headers=None, token=None):
    return await w.client.post(games(w.eid, "/cards/collect"), json={"token": token or w.badge(owner)}, headers=headers or await w.player(collector))


async def _seed_reveal(w, reg, hero):
    """A revealed hero by hand, so a test can say who holds what."""
    hero_id, tier = hero.id, hero.tier
    await common.ensure_player(w.session, w.eid, reg.rid)
    w.session.add(GameReveals(id=uuid.uuid4(), event_id=w.eid, registration_id=reg.rid, hero_id=hero_id, tier=tier))
    w.session.add(GameDeckItems(id=uuid.uuid4(), event_id=w.eid, registration_id=reg.rid, hero_id=hero_id, source="reveal"))
    w.session.add(GameCardScores(id=uuid.uuid4(), event_id=w.eid, registration_id=reg.rid, distinct_heroes=1))
    await w.session.commit()


async def test_every_refusal_of_a_collect_says_what_is_wrong(make_world):
    w = await make_world()
    await w.turn_on()
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    heroes = await cards.heroes_of(w.session, w.eid)
    await _seed_reveal(w, a, heroes[0])
    own = await _collect(w, a, a)
    assert own.status_code == 409 and own.json()["error_code"] == "SELF_SCAN"
    # b has not unlocked a hero: neither can b collect, nor be collected.
    r = await _collect(w, a, b)
    assert r.status_code == 409 and r.json()["error_code"] == "OWNER_NOT_REVEALED" and "haven't unlocked" in r.json()["message"]
    r = await _collect(w, b, a)
    assert r.status_code == 409 and r.json()["error_code"] == "NOT_REVEALED"
    await _seed_reveal(w, b, heroes[5])
    ok = await _collect(w, a, b)
    assert ok.status_code == 200, ok.text
    again = await _collect(w, a, b)
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_MET"
    # Two-way: they have met, from either side.
    back = await _collect(w, b, a)
    assert back.status_code == 409 and back.json()["error_code"] == "ALREADY_MET"
    for token in ("garbage", "b.1.AAAAAAAAAAAAAAAAAAAAAA.0.AAAAAAAAAAAAAAAA", tokens.point(w.eid, uuid.uuid4())):
        bad = await _collect(w, a, b, token=token)
        assert bad.status_code == 422 and bad.json()["error_code"] == "BAD_CODE"
    # Another event's badge is not this event's.
    other = await make_world("Elsewhere")
    foreign = await other.guest()
    assert (await _collect(w, a, b, token=other.badge(foreign))).json()["error_code"] == "BAD_CODE"
    await _config(w, collect_on=False)
    c = await w.guest("Chitra Das")
    await _seed_reveal(w, c, heroes[7])
    assert (await _collect(w, a, c)).json()["error_code"] == "COLLECT_OFF"


async def test_a_two_way_collect_adds_both_heroes_in_one_scan_and_a_held_hero_adds_nothing(make_world):
    w = await make_world()
    await w.turn_on()
    heroes = await cards.heroes_of(w.session, w.eid)
    a, b, c = await w.guest("Asha Nair"), await w.guest("Bilal Khan"), await w.guest("Chitra Das")
    await _seed_reveal(w, a, heroes[0])
    await _seed_reveal(w, b, heroes[1])
    await _seed_reveal(w, c, heroes[0])  # the same hero as a's
    done = (await _collect(w, a, b)).json()
    assert done["got"]["new"] and done["got"]["hero"]["id"] == str(heroes[1].id) and "You collected" in done["got"]["message"]
    assert done["gave"]["new"] and done["gave"]["hero"]["id"] == str(heroes[0].id) and done["deck_count"] == 2
    deck_b = (await w.client.get(games(w.eid, "/cards/deck"), headers=await w.player(b))).json()
    assert deck_b["deck_count"] == 2 and sum(1 for c_ in deck_b["cards"] if c_["owned"]) == 2
    # c holds the hero a already holds: the meeting is recorded, nothing is added for a.
    dup = (await _collect(w, a, c)).json()
    assert dup["got"]["new"] is False and dup["got"]["message"] == "Already in your deck" and dup["deck_count"] == 2
    assert dup["gave"]["new"] is False  # c holds that hero too: both sides add nothing
    again = await _collect(w, a, c)
    assert again.json()["error_code"] == "ALREADY_MET"


async def test_one_way_collect_gives_nothing_back(make_world):
    w = await make_world()
    await w.turn_on()
    await _config(w, two_way=False)
    heroes = await cards.heroes_of(w.session, w.eid)
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    await _seed_reveal(w, a, heroes[0])
    await _seed_reveal(w, b, heroes[1])
    done = (await _collect(w, a, b)).json()
    assert done["gave"] is None and done["deck_count"] == 2
    assert (await w.client.get(games(w.eid, "/cards/deck"), headers=await w.player(b))).json()["deck_count"] == 1
    # Not met the other way round: b may scan a too.
    assert (await _collect(w, b, a)).status_code == 200


async def test_collects_are_rate_limited_per_attendee(make_world, monkeypatch):
    w = await make_world()
    await w.turn_on()
    monkeypatch.setattr(cards, "COLLECT_GAP", 5.0)
    heroes = await cards.heroes_of(w.session, w.eid)
    a, b, c = await w.guest("Asha Nair"), await w.guest("Bilal Khan"), await w.guest("Chitra Das")
    for reg, hero in zip((a, b, c), heroes):
        await _seed_reveal(w, reg, hero)
    assert (await _collect(w, a, b)).status_code == 200
    fast = await _collect(w, a, c)
    assert fast.status_code == 429 and fast.json()["error_code"] == "RATE_LIMITED" and 1 <= fast.json()["errors"]["retry_after"] <= 5
    monkeypatch.setattr(cards, "COLLECT_GAP", 0.0)
    monkeypatch.setattr(cards, "COLLECT_PER_HOUR", 1)
    hourly = await _collect(w, a, c)
    assert hourly.status_code == 429 and hourly.json()["errors"]["retry_after"] > 60


async def test_a_full_deck_is_marked_complete_and_told_in_the_ticker(make_world):
    w = await make_world()
    await w.turn_on()
    base = org(w.eid, "/cards/heroes")
    # Four heroes, one per tier.
    for h in (await w.client.get(base, headers=w.club_h)).json():
        if h["name"] in ("Moss Guard", "Dune Rider", "Ember Scout", "Tide Runner", "Iron Sage", "Frost Lynx", "Void Dancer"):
            if h["name"] in ("Ember Scout", "Storm Weaver", "Solar Warden", "Aurora Titan"):
                continue
            assert (await w.client.delete(f"{base}/{h['id']}", headers=w.club_h)).status_code in (200, 422)
    heroes = await cards.heroes_of(w.session, w.eid)
    assert len(heroes) <= 5
    regs = [await w.guest(f"Player {n}") for n in range(len(heroes))]
    for reg, hero in zip(regs, heroes):
        await _seed_reveal(w, reg, hero)
    for other in regs[1:]:
        r = await _collect(w, regs[0], other)
        assert r.status_code == 200, r.text
    last = r.json()
    assert last["complete"] is True and last["deck_count"] == last["total"] == len(heroes)
    score = await w.session.scalar(select(GameCardScores).where(GameCardScores.registration_id == regs[0].rid))
    assert score.completed_at is not None
    feed = (await w.client.get(games(w.eid, "/board?game=cards"))).json()
    assert feed["rows"][0]["score"] == len(heroes) and feed["rows"][0]["rank"] == 1
    ticker = (await w.client.get(org(w.eid, "/feed"), headers=w.club_h)).json()
    assert any(t["type"] == "deck_complete" and "Player 0".split()[0] in t["message"] for t in ticker)


async def test_hero_edits_cannot_move_or_delete_a_revealed_hero(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest()
    shown = (await _reveal(w, reg)).json()["hero"]
    base = org(w.eid, f"/cards/heroes/{shown['id']}")
    tier = (await w.session.scalar(select(GameHeroes.tier).where(GameHeroes.id == uuid.UUID(shown["id"]))))
    move = await w.client.patch(base, json={"tier": "legendary" if tier != "legendary" else "common"}, headers=w.club_h)
    assert move.status_code == 409 and move.json()["error_code"] == "HERO_REVEALED"
    assert (await w.client.delete(base, headers=w.club_h)).json()["error_code"] == "HERO_REVEALED"
    renamed = await w.client.patch(base, json={"name": "Renamed", "tagline": "New words"}, headers=w.club_h)
    assert renamed.status_code == 200
    me = (await w.client.get(games(w.eid, "/me"), headers=await w.player(reg))).json()
    assert me["cards"]["revealed"]["name"] == "Renamed"


async def test_the_deck_hides_unheld_heroes_and_the_holder_count_can_be_off(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest()
    await _reveal(w, reg)
    deck = (await w.client.get(games(w.eid, "/cards/deck"), headers=await w.player(reg))).json()
    locked = [c for c in deck["cards"] if not c["owned"]]
    assert len(locked) == 9 and all(set(c) == {"id", "number", "tier", "locked", "owned"} for c in locked)
    assert sum(deck["missing"].values()) == 9 and deck["rank"] == {"rank": 1, "of": 1, "score": 1}
    await _config(w, show_holders=False)
    other = await w.guest("Second Guest")
    assert (await _reveal(w, other)).json()["holders"] is None


async def test_a_player_can_opt_out_of_being_named(make_world):
    w = await make_world()
    await w.turn_on()
    reg = await w.guest("Anjali Rao")
    h = await w.player(reg)
    await w.client.post(games(w.eid, "/cards/reveal"), headers=h)
    assert (await w.client.get(games(w.eid, "/board?game=cards"))).json()["rows"][0]["name"] == "Anjali R."
    patched = await w.client.patch(games(w.eid, "/me"), json={"public": False}, headers=h)
    assert patched.json()["player"]["public"] is False
    name = (await w.client.get(games(w.eid, "/board?game=cards"))).json()["rows"][0]["name"]
    assert name.startswith("Player #") and "Anjali" not in name


async def test_ties_go_to_whoever_reached_the_score_first(make_world):
    w = await make_world()
    await w.turn_on()
    heroes = await cards.heroes_of(w.session, w.eid)
    a, b, c = await w.guest("Asha Nair"), await w.guest("Bilal Khan"), await w.guest("Chitra Das")
    now = datetime.now(timezone.utc)
    for reg, hero, at in ((a, heroes[0], now), (b, heroes[1], now - timedelta(minutes=5)), (c, heroes[2], now - timedelta(minutes=2))):
        await _seed_reveal(w, reg, hero)
        score = await w.session.scalar(select(GameCardScores).where(GameCardScores.registration_id == reg.rid))
        score.reached_at = at
    await w.session.commit()
    rows = (await w.client.get(games(w.eid, "/board?game=cards"), headers=await w.player(c))).json()
    assert [r["name"] for r in rows["rows"]] == ["Bilal K.", "Chitra D.", "Asha N."]
    assert rows["me"] == {"rank": 2, "of": 3, "score": 1} and [r["me"] for r in rows["rows"]] == [False, True, False]


async def test_arrows_compare_with_the_board_a_minute_earlier(make_world):
    from app.api.games.models import GameSnapshots

    w = await make_world()
    await w.turn_on()
    heroes = await cards.heroes_of(w.session, w.eid)
    a, b = await w.guest("Asha Nair"), await w.guest("Bilal Khan")
    await _seed_reveal(w, a, heroes[0])
    await _seed_reveal(w, b, heroes[1])
    first = (await w.client.get(games(w.eid, "/board?game=cards"))).json()
    assert [r["move"] for r in first["rows"]] == [None, None]
    # A board from two minutes ago, with the two the other way round.
    ranks = {str(a.rid): 2, str(b.rid): 1}
    snap = await w.session.scalar(select(GameSnapshots).where(GameSnapshots.event_id == w.eid))
    snap.taken_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    snap.bucket -= 2
    snap.ranks = ranks
    score = await w.session.scalar(select(GameCardScores).where(GameCardScores.registration_id == a.rid))
    score.reached_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await w.session.commit()
    again = (await w.client.get(games(w.eid, "/board?game=cards"))).json()
    assert {r["name"]: (r["rank"], r["move"]) for r in again["rows"]} == {"Asha N.": (1, 1), "Bilal K.": (2, -1)}
    assert (await w.session.scalar(select(func.count()).select_from(GameSnapshots).where(GameSnapshots.event_id == w.eid))) == 2


# ─── 300 at once ────────────────────────────────────────────────────────


async def test_three_hundred_reveals_at_once_on_hard_hit_every_tier_target(_engine):
    """SPEC §13, on separate committed connections: each tier within 1 percentage point of its target
    and none over by more than one card. Hard = 60 / 28 / 10 / 2."""
    before = await committed_keys(_engine)

    def make():
        s = async_sessionmaker(bind=_engine, **SESSION_KWARGS)()
        add_loader_criteria(s)
        return s

    from app.api.clubs.models import Clubs
    from app.api.events.models import EventCategories, EventRegistrationsLink, Events
    from app.api.users.models import UserTypes

    N = 300
    now = datetime.now(timezone.utc)
    try:
        async with make() as s:
            owner = await _make_user(s, email="games-race@example.com", user_type=UserTypes.club, username="gamesrace")
            club = Clubs(slug="games-race-club", name="Race Club", user_id=owner.id, is_verified=True)
            s.add(club)
            await s.flush()
            cat = EventCategories(name="games-race", created_by_id=owner.id)
            s.add(cat)
            await s.flush()
            event = Events(slug="games-race-night", name="Race Night", images=[], duration=6.0, event_datetime=now - timedelta(hours=1), club_id=club.id, category_id=cat.id)
            s.add(event)
            await s.flush()
            event_id = event.id
            settings = await common.ensure_settings(s, event_id)
            settings.cards_on = True
            config = await cards.ensure_cards(s, event_id)
            cards.apply_preset(config, "hard")
            reg_ids = []
            for i in range(N):
                guest = await _make_user(s, email=f"race-guest{i}@example.com", username=f"raceguest{i}")
                reg = EventRegistrationsLink(id=uuid.uuid4(), event_id=event_id, user_id=guest.id, full_name=f"Racer {i}", email=f"race{i}@example.com", ticket_id=f"RACEG-{uuid.uuid4().hex[:10]}")
                s.add(reg)
                await s.flush()
                s.add(CheckIns(event_id=event_id, registration_id=reg.id, via=CheckInVia.gate, checked_in_at=now))
                await common.ensure_player(s, event_id, reg.id)
                reg_ids.append(reg.id)
            await s.commit()

        async def one(reg_id):
            async with make() as s:
                reg = await common.registration_of(s, event_id, reg_id)
                ctx = common.Ctx(event=await common.event_of(s, event_id), settings=await common.settings_of(s, event_id), reg=reg, player=await common.ensure_player(s, event_id, reg_id))
                return await cards.reveal(s, ctx)

        results = await asyncio.gather(*(one(r) for r in reg_ids))
        assert len(results) == N and all(r["already"] is False for r in results)
        async with make() as s:
            counts = dict((t, int(n)) for t, n in (await s.execute(select(GameReveals.tier, func.count()).where(GameReveals.event_id == event_id).group_by(GameReveals.tier))).all())
            counters = dict((t, n) for t, n in (await s.execute(select(GameTierCounters.tier, GameTierCounters.reveals).where(GameTierCounters.event_id == event_id))).all())
            distinct = await s.scalar(select(func.count(func.distinct(GameReveals.registration_id))).where(GameReveals.event_id == event_id))
        target = {"common": 180, "uncommon": 84, "rare": 30, "legendary": 6}
        print("reveal distribution (300 at once, Hard):", {t: counts.get(t, 0) for t in target}, "target", target)
        assert sum(counts.values()) == N == distinct and counters == {t: counts.get(t, 0) for t in target}
        for tier, want in target.items():
            assert abs(counts.get(tier, 0) - want) <= 3, (tier, counts)  # +-1 percentage point of 300
            assert counts.get(tier, 0) <= want + 1, (tier, counts)  # never over by more than one card
        assert counts["legendary"] >= 4
    finally:
        await sweep(_engine, before)


# ─── No database: last in the file (tests/conftest.py's loop) ───

def test_the_tier_pick_is_by_deficit_and_never_over_target_by_a_card():
    import random

    rng = random.Random(7)
    shares = {"common": 60.0, "uncommon": 28.0, "rare": 10.0, "legendary": 2.0}
    counts = {t: 0 for t in shares}
    for r in range(500):
        t = cards.pick_tier(shares, counts, r, set(shares), rng)
        counts[t] += 1
        for tier, share in shares.items():
            assert counts[tier] < share / 100 * (r + 1) + 1 + 1e-9
    assert counts == {"common": 300, "uncommon": 140, "rare": 50, "legendary": 10}


