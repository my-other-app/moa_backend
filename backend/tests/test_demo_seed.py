"""The demo's scripts (scripts/demo): the reset that keeps only the admins
and the reference data, the seed of _example.json, quiet mode, and the
removal of the demo.

Everything runs on the test's own session, inside its transaction, so the
reset's TRUNCATEs and the seed's commits are rolled back with the test.

Every test here takes the session, even those that do not use it: when the
first test of a run (this file on its own) takes no database fixture, the
suite's session engine and the later tests end up on different event loops
-- true of any module in the suite, not of these scripts."""
import importlib.util
import json
import pathlib
import shutil
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from app.api.clubs.models import Clubs
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.interests.models import Interests, InterestCategory
from app.api.subscriptions.models import Plans
from app.api.users.models import UserAvatars, Users, UserTypes
from app.core import jobs
from scripts.demo import remove, reset
from scripts.demo.local_bucket import local_bucket
from scripts.demo.quiet import QuietViolation, quiet
from scripts.demo.schema import DEMO_DOMAIN, DataError, load
from scripts.demo.seed import Refused, Seeder, snap, weekday_for
from tests.conftest import auth_header

DATA = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "demo" / "data"


def _migration(name):
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def reference(session):
    """The platform's reference data as production has it: the plans, the
    topic tree and the event types the product ships."""
    plans, domains, topics = _migration("club_plans"), _migration("club_domains"), _migration("topics")
    connection = await session.connection()

    def seed(sync):
        with Operations.context(MigrationContext.configure(connection=sync)):
            plans.seed_plans()
            domains.grant()
        topics.seed(sync)

    await connection.run_sync(seed)
    await session.commit()


def _data_dir(tmp_path, *, mutate=None) -> pathlib.Path:
    """A data folder with only the example club and a short people file."""
    root = tmp_path / "data"
    (root / "clubs").mkdir(parents=True)
    club = json.loads((DATA / "clubs" / "_example.json").read_text())
    if mutate:
        mutate(club)
    (root / "clubs" / "_example.json").write_text(json.dumps(club))
    people = json.loads((DATA / "people.json").read_text())
    people["attendee_names"] = people["attendee_names"][:30]
    (root / "people.json").write_text(json.dumps(people))
    return root


# ─── The reset ────────────────────────────────────────────────────────────


async def test_reset_refuses_without_each_guard(session):
    ok = dict(execute=True, confirm="moa", database="moa", env={"APP_DEMO_RESET_ALLOWED": "1"})
    assert reset.guard_problems(**ok) == []
    assert reset.guard_problems(**{**ok, "confirm": None})
    assert reset.guard_problems(**{**ok, "confirm": "moa_other"})
    assert reset.guard_problems(**{**ok, "env": {}})
    assert reset.guard_problems(**{**ok, "env": {"APP_DEMO_RESET_ALLOWED": "yes"}})
    # A dry run needs none of them.
    assert reset.guard_problems(execute=False, confirm=None, database="moa", env={}) == []


async def test_reset_cli_refuses_before_touching_anything(session, capsys):
    """The command itself: no --confirm, a wrong name, no environment flag."""
    from app.config import settings

    database = settings.DATABASE_URL.rsplit("/", 1)[1]
    assert await reset.run(execute=True, confirm=None, env={"APP_DEMO_RESET_ALLOWED": "1"}) == 2
    assert await reset.run(execute=True, confirm="not-" + database, env={"APP_DEMO_RESET_ALLOWED": "1"}) == 2
    assert await reset.run(execute=True, confirm=database, env={}) == 2
    out = capsys.readouterr().out
    assert out.count("Refused") == 3


async def test_every_model_table_is_classified(session):
    """A table a new model adds must be decided in reset.py before it merges."""
    from app.db import registry  # noqa: F401
    from app.db.base import AbstractSQLModel

    missing = sorted(set(AbstractSQLModel.metadata.tables) - set(reset.CLASSIFICATION))
    assert missing == []
    assert reset.CLASSIFICATION["user_avatars"] == reset.KEEP
    assert reset.CLASSIFICATION["interest_categories"] == reset.KEEP
    assert reset.CLASSIFICATION["interests"] == reset.KEEP
    assert reset.CLASSIFICATION["plans"] == reset.KEEP
    assert reset.CLASSIFICATION["clubs"] == reset.WIPE


async def _populate(session, make_user, make_club, make_event, make_registration):
    admin = await make_user(email="root@myotherapp.in", user_type=UserTypes.admin)
    person = await make_user(email="someone@example.com")
    owner, club = await make_club(name="Real Club", email="real-club@example.com")
    event = await make_event(club=club, created_by=owner, name="Real Night")
    await make_registration(event=event, user=person)
    session.add(UserAvatars(name="Fox", image="avatars/fox.png"))
    session.add(EventCategories(name="Admin type", created_by_id=admin.id))
    # A ledger line: append-only in normal life.
    await session.execute(text(
        "INSERT INTO ledger_entries (txn_key, line, kind, account, amount_paise, units, currency, club_id, event_id, source, source_id, occurred_at, origin_at) "
        "VALUES ('t:1', 0, 'desk', 'desk', 100, 0, 'INR', :c, :e, 'test', '1', now(), now())"
    ), {"c": club.id, "e": event.id})
    await session.commit()
    return SimpleNamespace(admin=admin, person=person, owner=owner, club=club, event=event)


async def _count(session, table, where="TRUE"):
    return int(await session.scalar(text(f'SELECT count(*) FROM "{table}" WHERE {where}')))


async def test_reset_dry_run_changes_nothing(session, reference, make_user, make_club, make_event, make_registration):
    await _populate(session, make_user, make_club, make_event, make_registration)
    conn = await session.connection()
    tables = await reset.database_tables(conn)
    before = {t: await _count(session, t) for t in tables}
    plan = await reset.make_plan(conn)
    after = {t: await _count(session, t) for t in tables}
    assert before == after
    assert plan.removes("clubs") == 1 and plan.removes("users") == 2  # the person and the club's login; not the admin
    assert plan.removes("plans") == 0 and plan.removes("interests") == 0
    assert "Real Club" not in reset.render(plan, execute=False)  # counts only, no names


async def test_reset_keeps_admins_and_reference_data_and_wipes_the_rest(session, reference, make_user, make_club, make_event, make_registration):
    w = await _populate(session, make_user, make_club, make_event, make_registration)
    kept = {t: await _count(session, t) for t in ("plans", "plan_features", "plan_limits", "interest_categories", "interests", "user_avatars")}
    shipped_types = await _count(session, "event_categories", "created_by_id IS NULL")
    conn = await session.connection()
    plan = await reset.make_plan(conn)
    await reset.execute_plan(conn, plan)

    assert [u.id for u in (await session.execute(select(Users.id))).all()] == [w.admin.id]
    for t, n in kept.items():
        assert await _count(session, t) == n, t
    assert await _count(session, "event_categories", "created_by_id IS NULL") == shipped_types
    assert await _count(session, "event_categories", f"created_by_id = {w.admin.id}") == 1
    for t in ("clubs", "events", "event_registrations_link", "ledger_entries", "organizations", "user_profiles", "jobs"):
        assert await _count(session, t) == 0, t
    # The ledger's guard is back on.
    states = dict((await session.execute(text(
        "SELECT tgname, CAST(tgenabled AS text) FROM pg_trigger WHERE tgrelid = 'ledger_entries'::regclass AND NOT tgisinternal"))).all())
    assert set(states.values()) == {"O"}


async def test_reset_refuses_an_unclassified_table(session):
    await session.execute(text("CREATE TABLE demo_mystery_rows (id int)"))
    with pytest.raises(reset.Refused, match="demo_mystery_rows"):
        await reset.make_plan(await session.connection())


async def test_reset_counts_admins_who_can_sign_in(session, make_user):
    conn = await session.connection()
    assert await reset.admins_left(conn) == 0
    await make_user(email="root@myotherapp.in", user_type=UserTypes.admin)
    assert await reset.admins_left(conn) == 1


# ─── Quiet mode ───────────────────────────────────────────────────────────


async def test_quiet_stops_mail_push_jobs_and_razorpay(session):
    import razorpay.client

    from app.core.email import transport
    from app.core.email.email import send_email_with_attachment
    from app.core.notifications import service as push

    real_deliver = transport.deliver
    with quiet() as q:
        # (send_email is stubbed for the whole suite; this one reaches the transport.)
        send_email_with_attachment(recipients=["someone@example.com"], subject="Hi", body_text="x", body_html="<p>x</p>")
        assert await push._send_to_tokens(["tok"], "T", "B") == 0
        await jobs.enqueue(session, "notifications.push", {"x": 1})
        with pytest.raises(QuietViolation):
            razorpay.client.Client(auth=("k", "s")).order.create({"amount": 100})
    assert transport.deliver is real_deliver
    assert [m["to"] for m in q.mails] == [["someone@example.com"]]
    assert len(q.pushes) == 1 and q.dropped_jobs == ["notifications.push"]
    assert await _count(session, "jobs") == 0


# ─── The content rules ────────────────────────────────────────────────────


async def test_the_real_content_loads(session):
    clubs, people = load()
    assert len(clubs) == 8 and len(people.attendee_names) == 150
    assert all(not e.past or e.attended_ratio is not None for c in clubs for e in c.events)


async def test_an_upcoming_paid_event_that_is_not_sold_out_is_refused(session, tmp_path):
    def open_paid(club):
        club["events"][1]["fee"] = 299
        club["events"][1]["sold_out"] = False

    with pytest.raises(DataError, match="must be sold_out"):
        load(_data_dir(tmp_path, mutate=open_paid), include_example=True)


async def test_a_real_inbox_as_contact_is_refused(session, tmp_path):
    def real(club):
        club["contact"]["email"] = "someone@gmail.com"

    with pytest.raises(DataError, match="demo.myotherapp.in"):
        load(_data_dir(tmp_path, mutate=real), include_example=True)


async def test_weekdays_snap_to_what_the_words_say(session):
    from datetime import date

    clubs, _ = load()
    today = date(2026, 10, 19)  # a Monday
    by_slug = {e.slug: (c, e) for c in clubs for e in c.events}
    club, crit = by_slug["friday-crit-87"]
    d = snap(crit.when.days_from_now, weekday_for(crit, club), today, past=crit.past)
    assert date.fromordinal(today.toordinal() + d).weekday() == 4 and d >= 1
    club, jam = by_slug["ponnila-game-jam-monsoon"]
    d = snap(jam.when.days_from_now, weekday_for(jam, club), today, past=True)
    assert date.fromordinal(today.toordinal() + d).weekday() == 4 and d <= -2


# ─── The seed ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded(session, reference, tmp_path, monkeypatch):
    from app.api.events import doc_cache

    monkeypatch.setattr(doc_cache.settings, "RENDER_DOC_CACHE", False, raising=False)
    clubs, people = load(_data_dir(tmp_path), include_example=True)
    with local_bucket(tmp_path / "bucket"), quiet() as q:
        seeder = Seeder(session, clubs, people, main_email=f"demo.user@{DEMO_DOMAIN}", main_password="DemoPass123!",
                        club_password="ClubPass123!", out=lambda *_: None)
        report = await seeder.run()
        yield SimpleNamespace(report=report, quiet=q, seeder=seeder, bucket=tmp_path / "bucket")


async def test_seed_makes_the_example_club(seeded, session):
    c = seeded.report.counts
    assert c["clubs"] == 1 and c["events_past"] == 1 and c["events_upcoming"] == 1
    assert c["registrations"] == 12 + 8
    assert c["check_ins"] == 9 and c["certificates"] == 9
    assert c["store_items"] == 2 and c["designs"] == 2 and c["sites"] == 1
    assert c["feedback_responses"] == 6 and c["live_polls"] == 1 and c["live_questions"] == 2
    club = await session.scalar(select(Clubs).where(Clubs.slug == "example-chai-coders"))
    assert club.listed and club.follower_count == 14 and club.tagline.startswith("The seed")
    past = await session.scalar(select(Events).where(Events.slug == "example-chai-and-rust"))
    # Every paid ticket paid at the desk: no receipt, no Razorpay order.
    regs = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == past.id))).all()
    assert all(r.is_paid and r.payment_receipt is None for r in regs)
    assert await _count(session, "payment_orders") == 0
    assert await _count(session, "ledger_entries", f"event_id = {past.id} AND kind = 'desk'") > 0
    # The club's owner signs in to the dashboard with the club password.
    owner = await session.get(Users, club.user_id)
    from app.core.auth.authentication import verify_password

    assert owner.email == f"example-chai-coders@{DEMO_DOMAIN}" and verify_password("ClubPass123!", owner.password)


async def test_seed_sends_nothing(seeded, session):
    assert seeded.quiet.sent == []
    assert await _count(session, "jobs") == 0
    assert await _count(session, "deliveries") == 0
    assert await _count(session, "email_templates") == 0
    assert await _count(session, "feedback_surveys", "auto_send") == 0
    assert seeded.report.counts["safety_checks_passed"] == 1


async def test_seeded_club_is_public(seeded, client):
    r = await client.get("/api/v1/discover/clubs/example-chai-coders")
    assert r.status_code == 200 and "Chai and Coders Example" in r.text
    for slug in ("example-chai-and-rust", "example-open-source-saturday"):
        r = await client.get(f"/api/v1/discover/events/{slug}")
        assert r.status_code == 200, slug
    r = await client.get("/api/v1/discover/home")
    assert r.status_code == 200 and "example-open-source-saturday" in r.text


async def test_seeded_store_catalogue_is_public(seeded, session, client):
    past = await session.scalar(select(Events).where(Events.slug == "example-chai-and-rust"))
    r = await client.get(f"/api/v1/events/{past.id}/store/catalog")
    assert r.status_code == 200 and "Chai Coders tee" in r.text and "Chai mug" in r.text
    # The event open for registration does not sell: a pick there is paid online.
    upcoming = await session.scalar(select(Events).where(Events.slug == "example-open-source-saturday"))
    assert upcoming.merch_enabled is False


async def test_main_user_has_tickets_and_certificates_in_the_club_design(seeded, session, client, mint_token):
    user = await session.scalar(select(Users).where(Users.email == f"demo.user@{DEMO_DOMAIN}"))
    h = auth_header(await mint_token(user))
    r = await client.get("/api/v1/user/my-events", headers=h)
    assert r.status_code == 200
    regs = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.user_id == user.id))).all()
    assert len(regs) == 2
    attended = next(r for r in regs if r.is_attended)
    assert attended.certificate_issued_at is not None
    with local_bucket(seeded.bucket):
        for reg in regs:
            r = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/image", headers=h)
            assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        r = await client.get(f"/api/v1/events/certificates/{attended.ticket_id}/image")
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        r = await client.get(f"/api/v1/events/certificates/{attended.ticket_id}/pdf")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
    # Drawn from the club's own designs, not the platform's.
    from app.api.events.certificates import certificate_style
    from app.api.events.tickets import ticket_style

    full = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.id == attended.id))
    await session.refresh(full, ["event"])
    assert (await ticket_style(session, full))["layout"]["canvas"]["templateKey"]
    assert (await certificate_style(session, full))["layout"]["canvas"]["templateKey"]


async def test_upcoming_demo_events_are_full_and_refuse_the_public(seeded, session, client):
    """Registration not open (the default): nobody from the public form gets
    a ticket to an event that is not happening."""
    upcoming = await session.scalar(select(Events).where(Events.slug == "example-open-source-saturday"))
    await session.refresh(upcoming)
    assert upcoming.max_participants == upcoming.going_count == 8
    r = await client.post(f"/api/v1/events/registration/{upcoming.id}/register",
                          json={"full_name": "Real Student", "email": "real.student@example.com"})
    assert r.status_code == 400 and r.json().get("error_code") == "EVENT_FULL", r.text
    assert "every upcoming demo event is full" in " ".join(seeded.report.notes)


async def _sign_in(client, username, password, scope=""):
    r = await client.post("/api/v1/auth/token", data={"username": username, "password": password, "scope": scope})
    assert r.status_code == 200, (username, r.text[:200])
    body = r.json()
    token = (body.get("data") or body)["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_demo_logins_need_no_email_verification(seeded, client):
    """The main user and the club logins sign in at once and count as
    verified: their mail is suppressed, so a code would never arrive."""
    h = await _sign_in(client, f"demo.user@{DEMO_DOMAIN}", "DemoPass123!")
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert (me.get("data") or me)["email_verified"] is True
    profile = (await client.get("/api/v1/user/profile/me", headers=h)).json()
    assert (profile.get("data") or profile).get("email_verified", True) is True
    h = await _sign_in(client, f"example-chai-coders@{DEMO_DOMAIN}", "ClubPass123!", scope="club")
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert (me.get("data") or me)["email_verified"] is True
    assert (await client.get("/api/v1/clubs/info", headers=h)).status_code == 200


async def test_seed_refuses_when_clubs_exist(session, reference, make_club, tmp_path):
    await make_club(name="Already Here", email="here@example.com")
    clubs, people = load(_data_dir(tmp_path), include_example=True)
    with quiet():
        seeder = Seeder(session, clubs, people, main_email=f"m@{DEMO_DOMAIN}", main_password="DemoPass123!",
                        club_password="ClubPass123!", out=lambda *_: None)
        with pytest.raises(Refused, match="already exist"):
            await seeder.run()


async def test_remove_takes_the_demo_and_leaves_real_clubs(seeded, session, make_club, make_user, make_event, make_registration):
    owner, real = await make_club(name="Real Club", email="real@example.com")
    person = await make_user(email="person@example.com")
    event = await make_event(club=real, created_by=owner, name="Real Night")
    await make_registration(event=event, user=person)
    await session.commit()
    conn = await session.connection()
    plan = await remove.make_plan(conn, None)
    assert plan.clubs == ["example-chai-coders"]
    await remove.execute_plan(conn, plan)
    assert await _count(session, "clubs") == 1
    assert await _count(session, "events") == 1
    assert await _count(session, "users", f"email LIKE '%{DEMO_DOMAIN}'") == 0
    assert await _count(session, "event_registrations_link") == 1
    assert await _count(session, "ledger_entries") == 0
    assert await session.scalar(select(func.count()).select_from(Plans)) == 6
    assert await session.scalar(select(func.count()).select_from(Interests)) > 0
    assert await session.scalar(select(func.count()).select_from(InterestCategory)) == 8


# ─── The showcase ─────────────────────────────────────────────────────────


async def test_showcase_fills_every_dashboard_section(session, reference, tmp_path, client, mint_token, monkeypatch):
    """The example club as the showcase: the dashboard's pages each have rows."""
    from app.api.events import doc_cache

    monkeypatch.setattr(doc_cache.settings, "RENDER_DOC_CACHE", False, raising=False)
    clubs, people = load(_data_dir(tmp_path), include_example=True)
    with local_bucket(tmp_path / "bucket"), quiet() as q:
        seeder = Seeder(session, clubs, people, main_email=f"demo.user@{DEMO_DOMAIN}", main_password="DemoPass123!",
                        club_password="ClubPass123!", out=lambda *_: None, showcase="example-chai-coders", open_registration=True)
        report = await seeder.run()
    c = report.counts
    assert c["showcase_access_members"] == 3 and len(report.logins) == 3
    assert c["showcase_walkups"] > 0 and c["showcase_oversold_orders"] == 1
    assert c["showcase_tracking_links"] >= 3 and c["showcase_link_clicks"] > 0
    assert c["showcase_announcements"] > 0 and c["showcase_finance_entries"] > 0 and c["showcase_closeout_reports"] == 1
    # Only the oversold order's alert was stopped; nothing else tried to leave.
    assert all("oversold" in str(m).lower() for m in q.sent)
    assert report.counts["safety_checks_passed"] == 1

    club = await session.scalar(select(Clubs).where(Clubs.slug == "example-chai-coders"))
    h = auth_header(await mint_token(await session.get(Users, club.user_id)))
    past = await session.scalar(select(Events).where(Events.slug == "example-chai-and-rust"))
    for path in ("/clubs/finance", "/clubs/notifications/page", "/clubs/links/overview?days=30", "/clubs/access",
                 "/auth/security/overview", f"/events/{past.id}/team", f"/events/{past.id}/messages", f"/events/{past.id}/store/orders",
                 f"/events/{past.id}/idcards/summary", f"/events/{past.id}/closeout/reports", "/clubs/designs/badge/layout"):
        r = await client.get("/api/v1" + path, headers=h)
        assert r.status_code == 200, (path, r.text[:200])
    orders = (await client.get(f"/api/v1/events/{past.id}/store/orders", headers=h)).text
    assert "oversold" in orders
    # --open-registration: the free event to come keeps its own capacity.
    upcoming = await session.scalar(select(Events).where(Events.slug == "example-open-source-saturday"))
    assert upcoming.max_participants == 30 and upcoming.merch_enabled is False
    # The committee signs in to the dashboard straight away.
    for _name, _role, email in report.logins:
        member = await _sign_in(client, email, "ClubPass123!", scope="club")
        assert (await client.get("/api/v1/clubs/access/me", headers=member)).status_code == 200
    # No queued work and nothing waiting to be sent for the sweeps to pick up.
    assert await _count(session, "jobs") == 0
    assert await _count(session, "deliveries", "status IN ('pending', 'sending')") == 0
    assert await _count(session, "messages", "status = 'SENDING'") == 0
