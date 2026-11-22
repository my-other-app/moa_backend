"""scripts/copy_event_for_testing.py: a test copy of an event, in a test club.

It writes to whichever database the app is pointed at, which on the server is
production, so what it must not do is the point of these tests: copy a person,
send anything, show the test event to the public, or take away anything it did
not make.
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.types import TypeDecorator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.clubs.models import ClubUsersLink, Clubs  # noqa: E402
from app.api.closeout.models import EventStatus  # noqa: E402
from app.api.events.models import EventCategories, Events  # noqa: E402
from app.api.live.models import PollStatus, QuizStatus  # noqa: E402
from app.api.subscriptions import service as subscriptions  # noqa: E402
from app.api.subscriptions.models import FEATURE_KEYS, Plans, PlanFeatures, PlanLimits, PlanVisibility  # noqa: E402
from app.api.users.models import UserTypes, Users  # noqa: E402
from app.db.base import AbstractSQLModel  # noqa: E402
from scripts import copy_event_for_testing as script  # noqa: E402
from scripts.copy_event_for_testing import Abort, MARKER, Options, copy_event, remove_test_club  # noqa: E402

API = "/api/v1"
TABLES = AbstractSQLModel.metadata.tables
SENTINELS = ("Real Person Name", "real.person@example.org", "+919800011122", "Real Team Mate")

#: Where the club's stored pictures are: the copy must name the same files.
KEYS = {
    "poster": "events/posters/v-real-poster.png", "logo": "clubs/logos/v-real-logo.png",
    "cover": "clubs/covers/v-real-cover.png", "ticket_bg": "doc/ticket-bg.png",
    "partner": "events/partners/v-duk.png", "file": "events/files/brief.pdf",
    "merch": "merch/shirt.png", "hero": "games/hero-1.png",
}


# ───────────────────────────── building a world ─────────────────────────────


def _filler(column):
    kind = column.type.impl if isinstance(column.type, TypeDecorator) else column.type
    name = type(kind).__name__.upper()
    if name == "ENUM":
        return kind.enums[0] if kind.enums else "x"
    if name in ("INTEGER", "BIGINTEGER", "SMALLINTEGER"):
        return 0
    if name in ("FLOAT", "NUMERIC"):
        return 0
    if name == "BOOLEAN":
        return False
    if name == "DATETIME":
        return datetime.now(timezone.utc)
    if name in ("JSON", "JSONB"):
        return {}
    if name == "ARRAY":
        return []
    if name == "UUID":
        return uuid.uuid4()
    return "x"


async def put(session, _table, /, **values):
    name = _table
    """A row made straight in the table; what is not given and must be is filled."""
    table = TABLES[name]
    for c in table.columns:
        if c.name in values or c.nullable or c.default is not None or c.server_default is not None or c.autoincrement is True:
            continue
        if c.primary_key and c.name == "id":
            continue
        if c.foreign_keys:
            raise AssertionError(f"{name}.{c.name} is needed")
        values[c.name] = _filler(c)
    pk = list(table.primary_key.columns)
    result = await session.execute(table.insert().values(**values).returning(*pk))
    row = result.one()
    return row[0] if len(row) == 1 else tuple(row)


async def count(session, _table, /, **where) -> int:
    name = _table
    table = TABLES[name]
    query = select(func.count()).select_from(table)
    for column, value in where.items():
        query = query.where(table.c[column] == value)
    return await session.scalar(query)


@pytest.fixture
async def world(session, make_club, make_user, make_event, make_registration):
    """A real-looking event: every kind of setup, and people and activity around it."""
    now = datetime.now(timezone.utc)
    owner, club = await make_club(name="Real Club", email="owner@real-club.example")
    club.brand_primary, club.brand_secondary = "#112233", "#445566"
    await session.execute(TABLES["clubs"].update().where(TABLES["clubs"].c.id == club.id).values(logo=KEYS["logo"], cover=KEYS["cover"]))

    # Plans: a small one and the top one, which has every flag.
    free = Plans(key="free", name="Free", price_monthly_paise=0, price_yearly_paise=0, visibility=PlanVisibility.public, sort_order=0)
    top = Plans(key="enterprise", name="Enterprise", price_monthly_paise=999900, price_yearly_paise=9999000, visibility=PlanVisibility.public, sort_order=9)
    session.add_all([free, top])
    await session.flush()
    session.add(PlanFeatures(plan_id=free.id, tickets=True))
    session.add(PlanFeatures(plan_id=top.id, **{k: True for k in FEATURE_KEYS}))
    session.add_all([PlanLimits(plan_id=free.id, events_per_month=1), PlanLimits(plan_id=top.id)])

    guest = await make_user(email="guest-one@example.com")
    guest2 = await make_user(email="guest-two@example.com")
    follower = await make_user(email="follower@example.com")
    category = EventCategories(name="Conference", created_by_id=owner.id)
    session.add(category)
    from app.api.interests.models import InterestCategory, Interests

    topic = InterestCategory(name="Tech")
    session.add(topic)
    await session.flush()
    interest = Interests(name="Coding", category_id=topic.id)
    session.add(interest)
    await session.flush()

    start = now + timedelta(days=30)
    event = Events(
        slug="devfest-source", name="DevFest Source", images=[KEYS["poster"]], event_datetime=start, duration=6.0,
        club_id=club.id, category_id=category.id, about="A day of talks.", has_fee=True, reg_fee=500, max_participants=300,
        location_name="Real Hall, Kochi", location_link="https://maps.example/real", lat=9.93, lng=76.26, geo_source="manual",
        contact_name="Real Person Name", contact_phone="+919800011122", contact_email="real.person@example.org",
        report_recipients=["real.person@example.org"], kiosk_pin_hash="salt$hash", requires_approval=True, high_demand_label=True,
        merch_enabled=True, swag_enabled=True, door_mode="auto", close_after_minutes=120, reprint_policy="organiser",
        auto_print_badges=True, preprinted_carries=["name"], self_check_in_enabled=True, self_check_in_print=False,
        retention_mode="keep", retention_hours=48, admission_prefix="DF", admission_digits=5, admission_seq=7, page_views=900,
        reg_startdate=now - timedelta(days=10), reg_enddate=start - timedelta(days=1), event_tag="Featured",
        event_guidelines="Bring a laptop.", url="https://devfest.example",
        additional_details=[
            {"key": "tshirt", "label": "T-shirt size", "field_type": "select", "required": True, "options": ["S", "M"]},
            {"key": "diet", "label": "Diet", "field_type": "text", "required": False, "options": None, "show_if": {"key": "tshirt", "equals": "M"}},
        ],
    )
    session.add(event)
    await session.flush()
    await session.execute(TABLES["events"].update().where(TABLES["events"].c.id == event.id).values(poster=KEYS["poster"]))
    await put(session, "event_interests_link", event_id=event.id, interest_id=interest.id)

    # Tickets.
    free_type = await put(session, "ticket_types", event_id=event.id, name="Free", price=0, capacity=100, taken_count=4, is_default=True)
    paid_type = await put(session, "ticket_types", event_id=event.id, name="Early bird", price=500, capacity=50, taken_count=9,
                          sales_start=now - timedelta(days=9), sales_end=start - timedelta(days=5), per_person_limit=2)
    await put(session, "waitlist_settings", event_id=event.id, enabled=True, offer_hours=12)
    code = await put(session, "discount_codes", club_id=club.id, event_id=event.id, code="HALF", kind="percent", value=50,
                     ticket_type_ids=[paid_type], starts_at=now - timedelta(days=3), ends_at=start - timedelta(days=2))
    await put(session, "discount_codes", club_id=club.id, event_id=None, code="CLUBWIDE", kind="percent", value=10)

    # Doors.
    gate = await put(session, "doors", event_id=event.id, name="Main gate", kind="door", link_hash="h" * 64, link_prefix="abcd1234", link_can_walkup=True)
    station = await put(session, "doors", event_id=event.id, name="Booth 1", kind="door", purpose="station")
    desk = await put(session, "doors", event_id=event.id, name="Swag desk", kind="door", purpose="swag_desk")

    # Designs: the club's own, and the event's, per role.
    club_ticket = await put(session, "ticket_templates", club_id=club.id, name="Club ticket", is_default=True, logo=KEYS["ticket_bg"], config={"preset": {"id": "ticket/03-daylight-boarding-pass"}})
    await put(session, "ticket_templates", club_id=club.id, name="Unused club ticket", is_default=False, config={})
    club_cert = await put(session, "certificate_templates", club_id=club.id, name="Club cert", is_default=True, config={"layout": {"version": 2, "layers": []}})
    await put(session, "badge_templates", club_id=club.id, name="Club badge", is_default=True, config={})
    await put(session, "agenda_templates", club_id=club.id, name="Club agenda", is_default=True, config={})
    await session.execute(TABLES["events"].update().where(TABLES["events"].c.id == event.id).values(ticket_template_id=club_ticket, certificate_template_id=club_cert))
    for kind in ("ticket_templates", "certificate_templates", "badge_templates", "agenda_templates"):
        await put(session, kind, event_id=event.id, club_id=None, name=f"Event {kind}", audience="event_default", is_default=False, config={"preset": {"id": f"{kind}/x"}, "layout_revision": 3})
        await put(session, kind, event_id=event.id, club_id=None, name=f"Speaker {kind}", audience="speaker", is_default=False, config={"role": "speaker"})

    # Emails: the club's library, the event's own, the six review emails.
    library = await put(session, "email_templates", club_id=club.id, event_id=None, stage="INVITE", name="Library invite", subject="Hello", design={"blocks": []}, created_by=owner.id)
    for stage in ("REQUEST_RECEIVED", "REQUEST_APPROVED", "REQUEST_APPROVED_PAY", "REQUEST_DECLINED", "REQUEST_LAPSED", "REQUEST_UNANSWERED", "REMINDER"):
        await put(session, "email_templates", club_id=club.id, event_id=event.id, stage=stage, name=stage, subject=f"{stage} subject", design={"blocks": []},
                  source_id=library if stage == "REMINDER" else None, created_by=owner.id,
                  trigger={"mode": "BEFORE_START", "minutes": 60} if stage == "REMINDER" else {"mode": "MANUAL"})

    # Partners, files, survey, form revisions.
    partner = await put(session, "club_partners", club_id=club.id, name="DUK", role="Community", logo=KEYS["partner"])
    await put(session, "event_partners", event_id=event.id, kind="partner", name="DUK", club_partner_id=partner, logo=KEYS["partner"])
    await put(session, "event_partners", event_id=event.id, kind="sponsor", name="MyOtherApp", label="Platform")
    await put(session, "event_files", event_id=event.id, file=KEYS["file"], name="Brief")
    survey = await put(session, "feedback_surveys", event_id=event.id, questions=[{"key": "overall", "kind": "rating", "label": "How was it?"}], sent_at=now, sent_count=5)
    await put(session, "event_form_revisions", event_id=event.id, version=1, questions=[{"key": "tshirt"}])
    await put(session, "event_form_revisions", event_id=event.id, version=2, questions=[{"key": "tshirt"}, {"key": "diet"}], changed_by_user_id=owner.id)

    # Merch and the store.
    shirt = await put(session, "merch_items", event_id=event.id, name="Shirt", photo_key=KEYS["merch"], stock=100, collected_count=12, reserved_count=3, variant_field="tshirt")
    await put(session, "merch_variants", item_id=shirt, label="M", stock=40, collected_count=5, reserved_count=1)
    store_item = await put(session, "event_store_items", club_id=club.id, event_id=None, name="Mug", price=250, stock=50, sold_count=11, photo_key=KEYS["merch"])
    await put(session, "event_store_variants", item_id=store_item, label="Blue", stock=20, sold_count=4)
    await put(session, "event_store_offers", event_id=event.id, item_id=store_item, sort_order=1)
    await put(session, "event_store_items", club_id=club.id, event_id=None, name="Not offered here", price=1)

    # Speakers and the agenda: real people the copy must not carry.
    from app.api.speakers import service as speakers

    profile = await speakers.create_profile(session, name="Real Person Name", headline="Real job", club_id=club.id)
    real_speaker = await put(session, "event_speakers", event_id=event.id, name="Real Person Name", designation="Real job", bio="Real bio", speaker_profile_id=profile.id)
    agenda = []
    for i, label in enumerate(("Opening", "Keynote", "Closing")):
        agenda.append(await put(session, "agenda_items", event_id=event.id, starts_at=start + timedelta(hours=i), ends_at=start + timedelta(hours=i, minutes=45),
                                title=label, speaker="Real Person Name" if i == 1 else None, sort_order=i))
    await put(session, "agenda_item_speakers", agenda_item_id=agenda[1], event_speaker_id=real_speaker)

    # Live room.
    await put(session, "live_themes", event_id=event.id, theme={"accent": "#ff0000"}, screen={"layout": "wide"})
    quiz = await put(session, "live_quizzes", event_id=event.id, title="Round 1", status="live", join_code="ABC123", current_index=1, question_opened_at=now, asked=["x"])
    await put(session, "live_quiz_questions", quiz_id=quiz, prompt="2+2?", options=[{"key": "a", "text": "4"}], correct_key="a", sort_order=0)
    await put(session, "live_polls", event_id=event.id, prompt="Best talk?", options=[{"key": "a", "text": "One"}], status="live", launched_at=now)
    await put(session, "live_media", event_id=event.id, kind="photo", image="live/p.png")
    await put(session, "live_updates", event_id=event.id, text="We are live")
    await put(session, "live_questions", event_id=event.id, text="Real Person Name asks?")

    # Quest.
    quest = await put(session, "quests", event_id=event.id, enabled=True, title="Collect", starts_at=start, ends_at=start + timedelta(hours=5), updated_by=owner.id)
    task = await put(session, "quest_tasks", quest_id=quest, event_id=event.id, kind="station", title="Visit booth", points=10, door_id=station, config={"door_id": str(station)}, code="BOOTH")
    await put(session, "quest_tasks", quest_id=quest, event_id=event.id, kind="quiz_played", title="Play a quiz", points=5)
    milestone = await put(session, "quest_milestones", quest_id=quest, title="Bronze", threshold=10)
    await put(session, "quest_rewards", quest_id=quest, event_id=event.id, milestone_id=milestone, merch_item_id=shirt)

    # Games.
    await put(session, "game_settings", event_id=event.id, cards_on=True, hunt_on=True, player_seq=9)
    await put(session, "game_card_config", event_id=event.id)
    hero = await put(session, "game_heroes", event_id=event.id, number=1, name="Nova", tier="common", art=KEYS["hero"])
    await put(session, "game_hunts", event_id=event.id, status="live", starts_at=now)
    await put(session, "game_hunt_points", event_id=event.id, number=1, label="Fountain", clue="Wet")
    await put(session, "game_tier_counters", event_id=event.id, tier="common", reveals=4)

    # Posters.
    await put(session, "poster_settings", event_id=event.id, enabled=True, updated_by=owner.id)
    await put(session, "poster_designs", event_id=event.id, variant="square", layouts={"a": 1}, updated_by=owner.id)
    await put(session, "poster_variants", event_id=event.id, size="ig_portrait", width=1080, height=1350, key="posters/story.png", source_key=KEYS["poster"])

    # People and what they did.
    reg = await make_registration(event=event, user=guest, full_name="Real Person Name", email="real.person@example.org", phone="+919800011122",
                                  ticket_type_id=free_type, discount_code_id=code)
    reg2 = await make_registration(event=event, user=guest2)
    await put(session, "registration_requests", event_id=event.id, full_name="Real Person Name", email="real.person@example.org", token="t" * 40)
    await put(session, "waitlist_entries", event_id=event.id, ticket_type_id=paid_type, user_id=guest2.id, full_name="Real Person Name", email="real.person@example.org")
    device = await put(session, "kiosk_devices", event_id=event.id, name="Kiosk 1", door_id=gate, door_label="Main gate", token_hash="k" * 64, token_prefix="kkkkkkkk", expires_at=start)
    await put(session, "check_ins", event_id=event.id, registration_id=reg.id, door_id=gate, checked_in_at=now, kiosk_device_id=device)
    await put(session, "feedback_responses", survey_id=survey, registration_id=reg.id)
    await put(session, "quest_entries", quest_id=quest, registration_id=reg.id, user_id=guest.id)
    await put(session, "game_players", event_id=event.id, registration_id=reg.id, player_no=1)
    await put(session, "guest_posters", event_id=event.id, registration_id=reg.id)
    await put(session, "merch_collections", registration_id=reg.id, item_id=shirt, door_id=desk)
    await put(session, "volunteers", email="real.person@example.org", event_id=event.id, club_id=club.id, user_id=guest.id)
    session.add(ClubUsersLink(club_id=club.id, user_id=follower.id, is_following=True))
    await put(session, "event_ratings_link", event_id=event.id, user_id=guest.id, rating=5)
    await put(session, "notifications", user_id=guest.id, event_id=event.id, from_club_id=club.id, title="Hi", description="Real Person Name")
    await session.flush()
    await session.commit()
    return SimpleNamespace(
        club=club, owner=owner, event=event, start=start, plan_top=top, plan_free=free, guest=guest, reg=reg, reg2=reg2,
        ticket_types=(free_type, paid_type), doors=(gate, station, desk), quest=quest, task=task, shirt=shirt, library=library,
        paid_type=paid_type, code=code, hero=hero,
    )


def options(world, **overrides) -> Options:
    base = dict(source=str(world.event.id), club_email="tester@example.com", starts=world.start + timedelta(days=1))
    base.update(overrides)
    return Options(**base)


async def snapshot(session) -> dict:
    """How many rows every table holds, and how many of them name a sentinel."""
    out = {}
    for name, table in TABLES.items():
        if name == "search_outbox":
            continue  # a change to a listed row queues the index to catch up, and a deletion must stay queued
        out[name] = await session.scalar(select(func.count()).select_from(table))
    return out


async def sentinel_rows(session) -> dict:
    out = {}
    for name in TABLES:
        for sentinel in SENTINELS:
            n = await session.scalar(text(f'SELECT count(*) FROM "{name}" t WHERE t::text LIKE :s'), {"s": f"%{sentinel}%"})
            if n:
                out[(name, sentinel)] = n
    return out


async def new_event(session, report):
    return (await session.execute(select(Events).where(Events.id == report.event_id))).scalar_one()


# ───────────────────────────── the copy ─────────────────────────────


async def test_configuration_is_copied_with_ids_remapped(session, world):
    report = await copy_event(session, options(world))
    await session.flush()
    new_id, club_id = report.event_id, report.club_id
    src = world.event.id
    assert new_id != src and club_id != world.club.id

    ev = await new_event(session, report)
    # Every setting came across; the place, the time, the counts and the people did not.
    assert ev.name.startswith(MARKER) and ev.slug != world.event.slug
    assert ev.club_id == club_id
    assert ev.about == "A day of talks." and ev.has_fee and ev.requires_approval and ev.high_demand_label
    assert ev.admission_prefix == "DF" and ev.admission_digits == 5 and ev.admission_seq == 0
    assert ev.reprint_policy.value == "organiser" and ev.auto_print_badges and ev.preprinted_carries == ["name"]
    assert ev.self_check_in_enabled and ev.self_check_in_print is False and ev.kiosk_pin_hash == "salt$hash"
    assert ev.door_mode.value == "auto" and ev.close_after_minutes == 120
    assert ev.retention_hours == 48 and ev.max_participants == 300
    assert ev.additional_details == world.event.additional_details
    assert ev.status == "published" and ev.status_clock
    assert ev.page_views == 0 and ev.going_count == 0 and ev.paid_count == 0
    assert ev.event_datetime == world.start + timedelta(days=1) and ev.duration == 9.0
    assert "Test Hall" in ev.location_name and "Kochi" not in ev.location_name and ev.location_link is None
    assert ev.contact_email == "tester@example.com" and ev.contact_phone is None and ev.report_recipients == []
    assert ev.reg_enddate == ev.event_datetime and ev.reg_startdate <= datetime.now(timezone.utc)
    assert await session.scalar(select(TABLES["events"].c.search_text).where(TABLES["events"].c.id == new_id))  # filled by the trigger, not copied

    # The same stored pictures, by key.
    raw = (await session.execute(text("SELECT poster, images FROM events WHERE id = :i"), {"i": new_id})).one()
    assert raw.poster == KEYS["poster"] and raw.images == [KEYS["poster"]]
    club_row = (await session.execute(text("SELECT logo, cover, brand_primary FROM clubs WHERE id = :i"), {"i": club_id})).one()
    assert (club_row.logo, club_row.cover, club_row.brand_primary) == (KEYS["logo"], KEYS["cover"], "#112233")

    # Row counts of every table that is configuration.
    for name in (
        "event_interests_link", "ticket_types", "waitlist_settings", "doors", "event_partners", "event_files", "feedback_surveys",
        "merch_items", "live_themes", "live_quizzes", "live_polls", "quests", "quest_tasks", "quest_rewards",
        "game_settings", "game_card_config", "game_heroes", "game_hunts", "game_hunt_points", "poster_settings", "poster_designs",
        "poster_variants", "agenda_items",
    ):
        assert await count(session, name, event_id=new_id) == await count(session, name, event_id=src), name
    assert await count(session, "discount_codes", event_id=new_id) == 1  # the club-wide code stays behind
    assert await count(session, "discount_codes", club_id=club_id) == 1
    assert await count(session, "event_form_revisions", event_id=new_id) == 1  # the latest
    version = await session.scalar(select(TABLES["event_form_revisions"].c.version).where(TABLES["event_form_revisions"].c.event_id == new_id))
    assert version == 2
    assert await count(session, "email_templates", event_id=new_id) == 7
    for kind in ("ticket_templates", "certificate_templates", "badge_templates", "agenda_templates"):
        assert await count(session, kind, event_id=new_id) == 2  # event default + a role's
        assert await count(session, kind, club_id=club_id) >= 1
    assert await count(session, "event_store_offers", event_id=new_id) == 1
    assert await count(session, "event_store_items", club_id=club_id) == 1  # only the one on offer
    assert await count(session, "event_store_variants") == 2  # the source's and the copy's
    assert await count(session, "club_partners", club_id=club_id) == 1
    assert await count(session, "email_templates", club_id=club_id, event_id=None) == 1

    # Ids are the new ones.
    t = TABLES
    new_types = set((await session.scalars(select(t["ticket_types"].c.id).where(t["ticket_types"].c.event_id == new_id))).all())
    assert new_types and not new_types & set(world.ticket_types)
    new_doors = set((await session.scalars(select(t["doors"].c.id).where(t["doors"].c.event_id == new_id))).all())
    assert new_doors and not new_doors & set(world.doors)
    door_rows = (await session.execute(select(t["doors"].c.link_hash, t["doors"].c.link_prefix, t["doors"].c.purpose).where(t["doors"].c.event_id == new_id))).all()
    assert {r.link_hash for r in door_rows} == {None} and {r.purpose for r in door_rows} == {None, "station", "swag_desk"}
    tasks = (await session.execute(select(t["quest_tasks"]).where(t["quest_tasks"].c.event_id == new_id))).all()
    station = next(r for r in tasks if r.kind == "station")
    assert station.door_id in new_doors and station.config["door_id"] == str(station.door_id)
    new_merch = set((await session.scalars(select(t["merch_items"].c.id).where(t["merch_items"].c.event_id == new_id))).all())
    reward = (await session.execute(select(t["quest_rewards"]).where(t["quest_rewards"].c.event_id == new_id))).one()
    assert reward.merch_item_id in new_merch and reward.merch_item_id != world.shirt
    assert reward.quest_id != world.quest
    code = (await session.execute(select(t["discount_codes"]).where(t["discount_codes"].c.event_id == new_id))).one()
    assert set(code.ticket_type_ids) <= new_types and code.ticket_type_ids and code.club_id == club_id
    library_new = await session.scalar(select(t["email_templates"].c.id).where(t["email_templates"].c.club_id == club_id, t["email_templates"].c.event_id.is_(None)))
    reminder = (await session.execute(select(t["email_templates"]).where(t["email_templates"].c.event_id == new_id, t["email_templates"].c.stage == "REMINDER"))).one()
    assert reminder.source_id == library_new != world.library and reminder.created_by is None and reminder.due_at is not None
    assert reminder.due_at == ev.event_datetime - timedelta(minutes=60)  # the trigger counted from the new date
    # The event's designs answer to the test club's, not the source's.
    ticket = (await session.execute(select(t["ticket_templates"].c.club_id).where(t["ticket_templates"].c.id == ev.ticket_template_id))).scalar_one()
    assert ticket == club_id
    cert = (await session.execute(select(t["certificate_templates"].c.club_id).where(t["certificate_templates"].c.id == ev.certificate_template_id))).scalar_one()
    assert cert == club_id
    ticket_type_rows = (await session.execute(select(t["ticket_types"]).where(t["ticket_types"].c.event_id == new_id))).all()
    assert all(r.taken_count == 0 for r in ticket_type_rows)
    early = next(r for r in ticket_type_rows if r.name == "Early bird")
    assert early.sales_start < datetime.now(timezone.utc) and early.sales_end == ev.event_datetime and early.per_person_limit == 2
    free_row = next(r for r in ticket_type_rows if r.name == "Free")
    assert free_row.sales_start is None and free_row.sales_end is None and free_row.is_default
    # Partners point at the test club's partner.
    ep = (await session.execute(select(t["event_partners"]).where(t["event_partners"].c.event_id == new_id, t["event_partners"].c.kind == "partner"))).one()
    new_partner = await session.scalar(select(t["club_partners"].c.id).where(t["club_partners"].c.club_id == club_id))
    assert ep.club_partner_id == new_partner
    assert (await session.scalar(text("SELECT logo FROM event_partners WHERE id = :i"), {"i": ep.id})) == KEYS["partner"]
    # Stock is reset; what was sold is not carried.
    shirt = (await session.execute(select(t["merch_items"]).where(t["merch_items"].c.event_id == new_id))).one()
    assert (shirt.stock, shirt.collected_count, shirt.reserved_count) == (100, 0, 0) and shirt.photo_key == KEYS["merch"]
    variant = (await session.execute(select(t["merch_variants"]).where(t["merch_variants"].c.item_id == shirt.id))).one()
    assert (variant.stock, variant.collected_count, variant.reserved_count) == (40, 0, 0)
    mug = (await session.execute(select(t["event_store_items"]).where(t["event_store_items"].c.club_id == club_id))).one()
    assert (mug.stock, mug.sold_count, mug.reserved_count) == (50, 0, 0)

    # Games, live room, quest and survey start as drafts, nothing played.
    quiz = (await session.execute(select(t["live_quizzes"]).where(t["live_quizzes"].c.event_id == new_id))).one()
    assert quiz.status == QuizStatus.draft and quiz.join_code is None and quiz.current_index == -1 and quiz.asked is None and quiz.question_opened_at is None
    assert await count(session, "live_quiz_questions", quiz_id=quiz.id) == 1
    poll = (await session.execute(select(t["live_polls"]).where(t["live_polls"].c.event_id == new_id))).one()
    assert poll.status == PollStatus.draft and poll.launched_at is None
    hunt = (await session.execute(select(t["game_hunts"]).where(t["game_hunts"].c.event_id == new_id))).one()
    assert hunt.status == "draft" and hunt.starts_at is None
    assert (await session.scalar(select(t["game_settings"].c.player_seq).where(t["game_settings"].c.event_id == new_id))) == 0
    survey = (await session.execute(select(t["feedback_surveys"]).where(t["feedback_surveys"].c.event_id == new_id))).one()
    assert survey.sent_at is None and survey.sent_count == 0
    hero = (await session.execute(select(t["game_heroes"]).where(t["game_heroes"].c.event_id == new_id))).one()
    assert (await session.scalar(text("SELECT art FROM game_heroes WHERE id = :i"), {"i": hero.id})) == KEYS["hero"] and hero.id != world.hero
    qrow = (await session.execute(select(t["quests"]).where(t["quests"].c.event_id == new_id))).one()
    assert qrow.starts_at == ev.event_datetime and qrow.updated_by is None

    # The agenda moved with the date.
    items = (await session.execute(select(t["agenda_items"]).where(t["agenda_items"].c.event_id == new_id).order_by(t["agenda_items"].c.sort_order))).all()
    assert [i.starts_at for i in items] == [ev.event_datetime + timedelta(hours=k) for k in range(3)]
    assert report.counts["events"] == 1 and report.counts["ticket_types"] == 2


async def test_nothing_about_a_person_is_copied(session, world):
    before = await snapshot(session)
    leaks = await sentinel_rows(session)
    assert leaks  # the world does hold them, or this proves nothing
    report = await copy_event(session, options(world))
    await session.flush()
    after = await snapshot(session)
    new_id, club_id = report.event_id, report.club_id

    # No row of an activity or people table was made, for the event or the club.
    people = (
        "event_registrations_link", "registration_requests", "waitlist_entries", "payment_logs", "payment_orders", "check_ins", "deliveries",
        "kiosk_devices", "print_jobs", "card_print_requests", "feedback_responses", "feedback_sends", "quest_entries", "quest_progress",
        "quest_claims", "quest_unlocks", "quest_station_visits", "game_players", "game_reveals", "game_deck_items", "game_meetings",
        "game_hunt_claims", "game_hunt_scores", "game_feed", "game_snapshots", "game_tier_counters", "guest_posters", "volunteers",
        "club_users_link", "merch_collections", "event_store_orders", "event_ratings_link", "notifications", "live_questions", "live_quiz_players",
        "live_quiz_answers", "live_poll_responses", "live_media", "live_updates", "event_team_members", "event_cohosts", "tracking_links",
        "messages", "closeout_reports", "ticket_cancellations", "answer_asks", "club_team_members", "club_members", "leads",
        "ledger_entries", "event_cancellations", "club_refunds", "club_expenses", "event_page_views", "network_encounters",
    )
    for name in people:
        assert after[name] == before[name], f"{name} grew: the copy made a person's or an activity row"

    # What was made is exactly the configuration and the test club's own rows.
    grew = {n for n in after if after[n] != before[n]}
    allowed = {
        "users", "clubs", "club_subscriptions", "organizations", "events", "event_interests_link", "ticket_types", "waitlist_settings",
        "discount_codes", "doors", "ticket_templates", "certificate_templates", "badge_templates", "agenda_templates", "email_templates",
        "club_partners", "event_partners", "event_files", "event_form_revisions", "feedback_surveys", "merch_items", "merch_variants",
        "event_store_items", "event_store_variants", "event_store_offers", "event_speakers", "speaker_profiles", "speaker_history",
        "agenda_items", "agenda_item_speakers", "live_themes", "live_quizzes", "live_quiz_questions", "live_polls", "quests",
        "quest_tasks", "quest_milestones", "quest_rewards", "game_settings", "game_card_config", "game_heroes", "game_hunts",
        "game_hunt_points", "poster_settings", "poster_designs", "poster_variants", "club_threads", "notes", "clubs_interests_link",
        "search_outbox", "slug_aliases",
    }
    assert grew <= allowed, f"unexpected tables grew: {sorted(grew - allowed)}"

    # No person's name, email or phone of the source was written anywhere new.
    assert await sentinel_rows(session) == leaks
    ev = await new_event(session, report)
    assert "Real Person Name" not in (ev.contact_name or "")

    # The speakers and the agenda use invented people only.
    speakers = (await session.execute(select(TABLES["event_speakers"].c.name).where(TABLES["event_speakers"].c.event_id == new_id))).scalars().all()
    assert sorted(speakers) == sorted(s["name"] for s in script.SPEAKERS)
    keynote = (await session.execute(select(TABLES["agenda_items"].c.speaker, TABLES["agenda_items"].c.title).where(TABLES["agenda_items"].c.event_id == new_id))).all()
    by_title = {r.title: r.speaker for r in keynote}
    assert by_title["Keynote"] in {s["name"] for s in script.SPEAKERS} and by_title["Opening"] is None
    linked = await session.scalar(
        select(func.count()).select_from(TABLES["agenda_item_speakers"]).join(
            TABLES["agenda_items"], TABLES["agenda_items"].c.id == TABLES["agenda_item_speakers"].c.agenda_item_id).where(TABLES["agenda_items"].c.event_id == new_id)
    )
    assert linked == 1
    profiles = (await session.execute(select(TABLES["speaker_profiles"].c.name, TABLES["speaker_profiles"].c.created_by_club_id).where(TABLES["speaker_profiles"].c.created_by_club_id == club_id))).all()
    assert len(profiles) == 3

    # Posts are invented, and the source club's are not copied.
    posts = (await session.execute(select(TABLES["club_threads"].c.body, TABLES["club_threads"].c.notify_followers).where(TABLES["club_threads"].c.club_id == club_id))).all()
    assert len(posts) == 2 and not any(p.notify_followers for p in posts)
    assert await count(session, "notes", club_id=club_id) == 1


async def test_no_row_is_added_to_the_source(session, world):
    """Every table that names an event or a club holds, for the source's, the
    rows it held: the copy added rows of its own and none to the source."""
    scoped = [
        (name, column) for name, table in TABLES.items() for column in ("event_id", "club_id")
        if column in table.c and any(f.column.table.name == column[:-3] + "s" for f in table.c[column].foreign_keys)
    ]
    ids = {"event_id": world.event.id, "club_id": world.club.id}
    before = {(n, c): await count(session, n, **{c: ids[c]}) for n, c in scoped}
    report = await copy_event(session, options(world))
    await session.flush()
    for (name, column), was in before.items():
        assert await count(session, name, **{column: ids[column]}) == was, f"{name}.{column} of the source changed"
    assert report.event_id != world.event.id


async def test_the_source_is_untouched(session, world):
    before_rows = {
        name: (await session.execute(text(f'SELECT md5(string_agg(t::text, \'|\' ORDER BY t::text)) FROM "{name}" t WHERE {where}'))).scalar()
        for name, where in (
            ("events", f"id = {world.event.id}"), ("ticket_types", f"event_id = {world.event.id}"), ("doors", f"event_id = {world.event.id}"),
            ("email_templates", f"club_id = {world.club.id}"), ("quest_tasks", f"event_id = {world.event.id}"),
            ("agenda_items", f"event_id = {world.event.id}"), ("event_speakers", f"event_id = {world.event.id}"),
            ("merch_items", f"event_id = {world.event.id}"), ("clubs", f"id = {world.club.id}"), ("live_quizzes", f"event_id = {world.event.id}"),
            ("event_registrations_link", f"event_id = {world.event.id}"),
        )
    }
    await copy_event(session, options(world))
    await session.flush()
    for name, where in (
        ("events", f"id = {world.event.id}"), ("ticket_types", f"event_id = {world.event.id}"), ("doors", f"event_id = {world.event.id}"),
        ("email_templates", f"club_id = {world.club.id}"), ("quest_tasks", f"event_id = {world.event.id}"),
        ("agenda_items", f"event_id = {world.event.id}"), ("event_speakers", f"event_id = {world.event.id}"),
        ("merch_items", f"event_id = {world.event.id}"), ("clubs", f"id = {world.club.id}"), ("live_quizzes", f"event_id = {world.event.id}"),
        ("event_registrations_link", f"event_id = {world.event.id}"),
    ):
        now = (await session.execute(text(f'SELECT md5(string_agg(t::text, \'|\' ORDER BY t::text)) FROM "{name}" t WHERE {where}'))).scalar()
        assert now == before_rows[name], f"{name} of the source changed"


# ───────────────────────────── the club, its plan, its login ─────────────────────────────


async def test_the_test_club_has_its_login_its_plan_and_every_feature(session, world):
    report = await copy_event(session, options(world, club_name="Playground", plan=None))
    await session.flush()
    club = (await session.execute(select(Clubs).where(Clubs.id == report.club_id))).scalar_one()
    assert club.name == f"{MARKER} Playground" and club.is_verified and club.slug

    user = await session.get(Users, club.user_id)
    assert user.email == "tester@example.com" and user.user_type == UserTypes.club and user.full_name.startswith(MARKER)
    # A password nobody knows: bcrypt of random bytes, so no guess signs in.
    from app.core.auth.authentication import verify_password

    assert user.password and user.password.startswith("$2")
    for guess in ("Password123!", "tester@example.com", "", "password"):
        assert not verify_password(guess, user.password)
    # And "Forgot password?" finds the account, so the owner can set one.
    from app.api.club_ops import password as club_password

    found_user, member = await club_password.find_login(session, "Tester@Example.com")
    assert found_user is not None and found_user.id == user.id and member is None

    entitlements = await subscriptions.resolve_entitlements(session, club.id)
    assert entitlements.plan.key == "enterprise" == report.plan_key
    assert all(entitlements.allows(k) for k in FEATURE_KEYS) and report.missing_features == []
    sub = await subscriptions.get_club_subscription(session, club.id)
    assert sub.complimentary and sub.mrr_paise == 0 and sub.price_paise == 0 and sub.razorpay_subscription_id is None
    assert MARKER in sub.comp_reason
    assert await count(session, "payment_logs") == 0 and await count(session, "subscription_charges") == 0


async def test_a_named_plan_is_used_and_missing_features_are_said(session, world):
    report = await copy_event(session, options(world, plan="free"))
    assert report.plan_key == "free" and "quiz" in report.missing_features
    with pytest.raises(Abort):
        await copy_event(session, options(world, club_name="Other", plan="nope"))


async def test_it_refuses_what_would_clash(session, world):
    with pytest.raises(Abort, match="club login"):
        await copy_event(session, options(world, club_email="OWNER@real-club.example"))
    with pytest.raises(Abort, match="not found"):
        await copy_event(session, options(world, source="no-such-event"))
    with pytest.raises(Abort, match="future"):
        await copy_event(session, options(world, starts=datetime.now(timezone.utc) - timedelta(days=1)))
    await copy_event(session, options(world))
    await session.flush()
    with pytest.raises(Abort, match="exists already"):
        await copy_event(session, options(world, club_email="second@example.com"))


async def test_the_source_can_be_named_by_slug(session, world):
    report = await copy_event(session, options(world, source=world.event.slug))
    assert report.event_id != world.event.id


# ───────────────────────────── private, unlisted, silent ─────────────────────────────


async def test_the_copy_is_hidden_from_every_public_list(session, world, client):
    report = await copy_event(session, options(world))
    await session.flush()
    club = await session.get(Clubs, report.club_id)
    await session.refresh(club)
    assert club.listed is False and club.is_verified is True

    from app.api.discover import service as discover
    from app.api.search import docs

    # The sitemap, the lists, the club pages and the event page.
    sitemap = await discover.sitemap(session)
    assert report.event_slug not in {e["slug"] for e in sitemap["events"]}
    assert report.club_slug not in {c["slug"] for c in sitemap["clubs"]}
    events = (await client.get(f"{API}/discover/events", params={"limit": 50})).json()
    assert report.event_slug not in str(events)
    clubs = (await client.get(f"{API}/discover/clubs")).json()
    assert report.club_slug not in str(clubs)
    assert (await client.get(f"{API}/discover/events/{report.event_slug}")).status_code == 404
    assert (await client.get(f"{API}/discover/clubs/{report.club_slug}")).status_code == 404
    home = (await client.get(f"{API}/discover/home")).json()
    assert report.event_slug not in str(home)
    # The search index would hold it as not listed.
    event_docs, _ = await docs.event_docs(session, [report.event_id])
    assert event_docs and event_docs[0]["listed"] is False
    club_docs, _ = await docs.club_docs(session, [report.club_id])
    assert club_docs and club_docs[0]["listed"] is False
    # The organisation that hides it is made for it alone and is not pickable.
    org = await session.scalar(select(TABLES["organizations"].c.is_blocked).where(TABLES["organizations"].c.name == script.hidden_org_name(report.club_id)))
    assert org is True
    # The source's own listing is not disturbed.
    assert world.event.slug in str((await client.get(f"{API}/discover/events", params={"limit": 50})).json())


async def test_the_copy_can_be_shown_and_hidden_again(session, world, client):
    report = await copy_event(session, options(world))
    await session.flush()
    club = await session.get(Clubs, report.club_id)
    await script.set_visible(session, club, True)
    await session.refresh(club)
    assert club.listed is True and club.org_id is None
    page = await client.get(f"{API}/discover/events/{report.event_slug}")
    assert page.status_code == 200
    await script.set_visible(session, club, False)
    await session.refresh(club)
    assert club.listed is False
    assert (await client.get(f"{API}/discover/events/{report.event_slug}")).status_code == 404
    # Hiding twice makes no second organisation.
    await script.set_visible(session, club, False)
    n = await session.scalar(select(func.count()).select_from(TABLES["organizations"]).where(TABLES["organizations"].c.name == script.hidden_org_name(report.club_id)))
    assert n == 1


async def test_a_visible_copy_is_listed_from_the_start(session, world):
    report = await copy_event(session, options(world, visible=True))
    await session.flush()
    club = await session.get(Clubs, report.club_id)
    await session.refresh(club)
    assert club.listed is True and report.visible
    assert await count(session, "organizations") == 0


async def test_no_notification_is_ever_sent(session, world, monkeypatch):
    from app.core import jobs, notifications  # noqa: F401
    from app.core.notifications import triggers, fanout
    from app.core.email import email as email_mod

    def boom(*a, **k):
        raise AssertionError("something tried to notify someone")

    async def aboom(*a, **k):
        raise AssertionError("something tried to notify someone")

    for name in ("notify_followers_of_new_event", "notify_users_by_interest", "notify_cohost_followers", "notify_event_participants", "notify_user_added_as_volunteer"):
        monkeypatch.setattr(triggers, name, aboom)
    for name in ("enqueue_push", "enqueue_broadcast", "insert_for_audience"):
        monkeypatch.setattr(fanout, name, aboom)
    monkeypatch.setattr(email_mod, "send_email", boom)
    from app.api.speakers import notify as speaker_notify

    for name in dir(speaker_notify):
        if name.startswith("notify") and callable(getattr(speaker_notify, name)):
            monkeypatch.setattr(speaker_notify, name, aboom)
    enqueued = []
    real = jobs.enqueue

    async def watch(*args, **kwargs):
        enqueued.append(args[1] if len(args) > 1 else kwargs.get("name"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(jobs, "enqueue", watch)
    follows = await count(session, "club_users_link")
    notices_before = await count(session, "notifications")
    report = await copy_event(session, options(world))
    await session.flush()
    assert enqueued == []
    assert await count(session, "notifications") == notices_before and await count(session, "club_users_link") == follows
    assert await count(session, "jobs") == 0
    assert report.event_id


# ───────────────────────────── dry run and removal ─────────────────────────────


async def test_a_dry_run_writes_nothing(session, world, capsys, monkeypatch):
    before = await snapshot(session)
    source_id, slug, start = str(world.event.id), world.event.slug, (world.start + timedelta(days=1)).isoformat()
    await copy_event(session, options(world))
    await session.rollback()
    assert await snapshot(session) == before

    # Through the command: the default is a dry run that prints counts and no names.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def same_session():
        yield session

    import app.db.core as core

    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: same_session())
    parse = script.build_parser().parse_args
    code = await script.run(parse(["--source", source_id, "--club-email", "tester@example.com", "--starts", start]))
    out = capsys.readouterr().out
    assert code == 0 and "Dry run" in out and "ticket_types" in out
    for secret in ("tester@example.com", "Real Person Name", "real.person", slug, "Playground", "devfest"):
        assert secret not in out
    assert await snapshot(session) == before


async def test_a_confirmed_run_prints_where_to_log_in_and_no_secret(session, world, capsys, monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def same_session():
        yield session

    import app.db.core as core

    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: same_session())
    start = (world.start + timedelta(days=1)).isoformat()
    parse = script.build_parser().parse_args
    code = await script.run(parse(["--source", str(world.event.id), "--club-email", "tester@example.com", "--starts", start, "--confirm"]))
    out = capsys.readouterr().out
    club = await session.scalar(select(Clubs).where(Clubs.name.like(f"{MARKER}%")))
    assert code == 0 and club is not None
    from app.config import settings

    assert f"{settings.PUBLIC_WEB_URL.rstrip('/')}/login" in out and "Forgot password" in out
    assert club.slug in out and "/events/" in out
    user = await session.get(Users, club.user_id)
    assert user.password not in out and "tester@example.com" not in out


async def test_remove_deletes_what_was_made_and_leaves_the_rest(session, world, monkeypatch):
    # Stored pictures must never be touched.
    touched = []
    from app.core.storage import bucket, clients

    for module, names in ((bucket, ("delete", "delete_object", "delete_objects", "remove")), ):
        for n in names:
            if hasattr(module, n):
                monkeypatch.setattr(module, n, lambda *a, **k: touched.append(n))
    client = clients.media_client()
    for n in ("delete_object", "delete_objects"):
        monkeypatch.setattr(client, n, lambda *a, **k: touched.append(n), raising=False)

    before = await snapshot(session)
    leaks = await sentinel_rows(session)
    report = await copy_event(session, options(world))
    await session.flush()
    new_id, club_id = report.event_id, report.club_id

    # What the owner does while testing: a registration, a check-in, a message, a follower, a quiz played.
    from app.api.events.models import EventRegistrationsLink

    tester = await session.scalar(select(Users).where(Users.email == "tester@example.com"))
    reg = EventRegistrationsLink(event_id=new_id, user_id=world.guest.id, full_name="Test Guest", email="guest@example.com", ticket_id="T-" + uuid.uuid4().hex[:8])
    session.add(reg)
    await session.flush()
    door = await session.scalar(select(TABLES["doors"].c.id).where(TABLES["doors"].c.event_id == new_id).limit(1))
    await put(session, "check_ins", event_id=new_id, registration_id=reg.id, door_id=door, checked_in_at=datetime.now(timezone.utc))
    await put(session, "kiosk_devices", event_id=new_id, name="K", door_id=door, door_label="d", token_hash="z" * 64, token_prefix="zzzzzzzz", expires_at=datetime.now(timezone.utc))
    await put(session, "club_users_link", club_id=club_id, user_id=world.guest.id, is_following=True)
    await put(session, "messages", event_id=new_id, created_by=tester.id)
    await put(session, "notifications", user_id=world.guest.id, event_id=new_id, from_club_id=club_id, title="t", description="b")
    await put(session, "tracking_links", club_id=club_id, event_id=new_id, name="l", code="abcd", utm_source="s", utm_medium="m", created_by=tester.id)
    await put(session, "quest_entries", quest_id=await session.scalar(select(TABLES["quests"].c.id).where(TABLES["quests"].c.event_id == new_id)), registration_id=reg.id)
    await put(session, "user_sessions", user_id=tester.id, session_id=uuid.uuid4().hex, refresh_token_hash="r") if "user_sessions" in TABLES and False else None
    await session.flush()
    slug_before = world.event.slug

    counts = await remove_test_club(session, club_id)
    await session.flush()
    assert counts["events"] == 1 and counts["clubs"] == 1 and counts["users"] == 1 and counts["event_registrations_link"] == 1
    assert touched == []

    # Back to exactly what was there: every table, and the source's pictures and rows.
    assert await snapshot(session) == before
    assert await sentinel_rows(session) == leaks
    ev = (await session.execute(select(Events).where(Events.id == world.event.id))).scalar_one()
    assert ev.slug == slug_before
    keys = (await session.execute(text("SELECT poster FROM events WHERE id = :i"), {"i": world.event.id})).scalar_one()
    assert keys == KEYS["poster"]
    assert await count(session, "organizations") == 0
    assert (await session.scalar(select(Clubs.id).where(Clubs.id == club_id))) is None


async def test_remove_dry_run_changes_nothing(session, world):
    report = await copy_event(session, options(world))
    await session.commit()
    before = await snapshot(session)
    counts = await remove_test_club(session, report.club_id)
    assert counts["events"] == 1
    await session.rollback()
    assert await snapshot(session) == before


async def test_remove_refuses_a_club_that_is_not_a_test_club(session, world):
    with pytest.raises(Abort, match="no test club"):
        await remove_test_club(session, world.club.id)
    await copy_event(session, options(world))
    await session.flush()
    # Two test clubs: it will not guess.
    await copy_event(session, options(world, club_name="Second", club_email="second@example.com"))
    await session.flush()
    with pytest.raises(Abort, match="more than one"):
        await remove_test_club(session, None)


async def test_remove_refuses_when_money_was_taken(session, world):
    report = await copy_event(session, options(world))
    await session.flush()
    await put(session, "ledger_entries", txn_key="t1", line=1, kind="payment", account="cash", amount_paise=100, club_id=report.club_id,
              event_id=report.event_id, source="razorpay", source_id="p1", occurred_at=datetime.now(timezone.utc), origin_at=datetime.now(timezone.utc))
    with pytest.raises(Abort, match="money"):
        await remove_test_club(session, report.club_id)
    assert await count(session, "events", id=report.event_id) == 1


async def test_after_remove_the_same_email_and_name_can_be_used_again(session, world):
    first = await copy_event(session, options(world))
    await session.flush()
    await remove_test_club(session, first.club_id)
    await session.flush()
    second = await copy_event(session, options(world))
    assert second.club_id != first.club_id and second.event_id != first.event_id


def test_arguments_come_from_the_environment_when_piped(monkeypatch):
    monkeypatch.setenv("COPY_EVENT_ARGS", "--source 12 --club-email me@example.com --hours 6")
    seen = {}

    async def fake_run(args):
        seen["args"] = args
        return 0

    def run_it(coroutine):
        # Not asyncio.run: it would close the loop the other tests share.
        import asyncio

        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coroutine)

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script.asyncio, "run", run_it)
    assert script.main([]) == 0
    assert seen["args"].source == "12" and seen["args"].hours == 6.0 and not seen["args"].confirm
    with pytest.raises(SystemExit):
        script.main(["--source", "12"])  # no --club-email


async def test_the_commands_for_removing_and_showing(session, world, capsys, monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def same_session():
        yield session

    import app.db.core as core

    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: same_session())
    parse = script.build_parser().parse_args
    report = await copy_event(session, options(world))
    await session.commit()
    club_id, before = report.club_id, await snapshot(session)

    assert await script.run(parse(["--remove"])) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "events" in out and "tester@example.com" not in out and MARKER not in out
    assert await snapshot(session) == before

    assert await script.run(parse(["--set-visible", "on", "--confirm"])) == 0
    assert (await session.scalar(select(Clubs.listed).where(Clubs.id == club_id))) is True
    assert await script.run(parse(["--set-visible", "off", "--confirm"])) == 0
    assert (await session.scalar(select(Clubs.listed).where(Clubs.id == club_id))) is False

    assert await script.run(parse(["--remove", "--confirm"])) == 0
    assert "Removed" in capsys.readouterr().out
    assert (await session.scalar(select(Clubs.id).where(Clubs.id == club_id))) is None
    # Nothing left to remove: it says so and stops.
    assert await script.run(parse(["--remove", "--confirm"])) == 1
    assert "no test club" in capsys.readouterr().err
