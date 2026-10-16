"""Folding guests into the app account their address belongs to.

Guests are what the public form, the door and the organiser make for someone
who is not signed in. When the owner of the address signs in to the app with
Google or Apple, what those guests hold becomes theirs. These tests run with
the soft-delete filter production installs on every session, so retired rows
are as invisible to the merge here as they are there.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event, select

from app.api.auth.schemas import AuthTokenData
from app.api.badges.models import BadgeDefinitions, BadgeType, UserBadges
from app.api.events.models import EventRatingsLink, EventRegistrationsLink
from app.api.events.volunteer.models import Volunteer
from app.api.notifications.models import Notifications
from app.api.payments.handlers import handle_event_registration_payment
from app.api.payments.models import OrderStatus, PaymentLogs, PaymentOrders, PaymentStatus
from app.api.store.models import EventStoreOrders
from app.api.users import service as users
from app.api.users.models import AdminRoles, SigninProviders, UserProfiles, UserTypes, Users
from app.core.auth.jwt import create_access_token
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _soft_delete_filter(session):
    add_loader_criteria(session)


async def _row(session, model, row_id) -> dict:
    """A row as it is, retired or not: read past the soft-delete filter."""
    table = model.__table__
    connection = await session.connection()
    result = await connection.execute(select(table).where(table.c.id == row_id))
    return dict(result.mappings().one())


async def _app_user(make_user, session, email, provider=SigninProviders.google, **kw):
    user = await make_user(email=email, username=kw.pop("username", None) or f"app-{uuid.uuid4().hex[:8]}", **kw)
    user.provider = provider
    await session.flush()
    return user


async def _guest(make_user, email):
    return await make_user(email=email, user_type=UserTypes.guest, username=f"guest-{uuid.uuid4().hex[:8]}")


@pytest.fixture
async def club_and_events(session, make_club, make_event):
    owner, club = await make_club(name="Data Society", email=f"merge-club-{uuid.uuid4().hex[:6]}@example.com")
    events = [
        await make_event(club=club, created_by=owner, name=f"Session {i}", event_datetime=NOW + timedelta(days=3 + i))
        for i in range(4)
    ]
    await session.flush()
    return owner, club, events


async def test_every_table_moves_and_addresses_match_ignoring_case(
    session, make_user, make_registration, club_and_events
):
    _, club, (e1, e2, *_) = club_and_events
    app = await _app_user(make_user, session, "Neha@Example.com")
    a = await _guest(make_user, "neha@example.com ")
    b = await _guest(make_user, " NEHA@example.com")
    stranger = await _guest(make_user, "someone-else@example.com")

    reg_a = await make_registration(event=e1, user=a)
    reg_b = await make_registration(event=e2, user=b)
    rating = EventRatingsLink(event_id=e1.id, user_id=a.id, rating=4.5)
    order = PaymentOrders(receipt=f"er_{reg_a.ticket_id}", razorpay_order_id=f"order_{uuid.uuid4().hex[:8]}", amount=100, status=OrderStatus.paid, user_id=a.id, source="event_registration", payload={})
    shop = EventStoreOrders(event_id=e1.id, registration_id=reg_a.id, user_id=a.id, status="paid", total=Decimal("250"))
    volunteer = Volunteer(email="neha@example.com", event_id=e2.id, user_id=b.id, club_id=club.id, is_approved=True)
    badge_def = BadgeDefinitions(slug=f"first-{uuid.uuid4().hex[:6]}", name="First event", description="Came to one", emoji="*", badge_type=BadgeType.user, trigger_metric="events_attended", threshold=1)
    session.add_all([rating, order, shop, volunteer, badge_def])
    await session.flush()
    badge = UserBadges(user_id=a.id, badge_id=badge_def.id)
    note = Notifications(user_id=b.id, title="You're a volunteer", description="For Session 1", type="volunteer")
    profile = UserProfiles(user_id=a.id, full_name="Neha")
    session.add_all([badge, note, profile])
    await session.flush()

    summary = await users.merge_guest_into_user(session, app)

    assert summary["merged"] is True and summary["guests_merged"] == 2
    for model, row in [
        (EventRegistrationsLink, reg_a), (EventRegistrationsLink, reg_b), (EventRatingsLink, rating),
        (PaymentOrders, order), (EventStoreOrders, shop), (Volunteer, volunteer),
        (UserBadges, badge), (Notifications, note),
    ]:
        assert (await _row(session, model, row.id))["user_id"] == app.id, model.__name__
    assert (await _row(session, Users, a.id))["is_deleted"] is True
    assert (await _row(session, Users, b.id))["is_deleted"] is True
    assert (await _row(session, UserProfiles, profile.id))["is_deleted"] is True
    assert (await _row(session, Users, stranger.id))["is_deleted"] is False
    assert (summary["registrations_transferred"], summary["ratings_transferred"], summary["payments_transferred"],
            summary["store_orders_transferred"], summary["volunteers_transferred"], summary["badges_transferred"],
            summary["notifications_transferred"]) == (2, 1, 1, 1, 1, 1, 1)


async def test_a_second_run_changes_nothing_and_costs_one_query(session, make_user, make_registration, club_and_events):
    _, _, (e1, *_) = club_and_events
    app = await _app_user(make_user, session, "ravi@example.com")
    guest = await _guest(make_user, "Ravi@example.com")
    reg = await make_registration(event=e1, user=guest)
    assert (await users.merge_guest_into_user(session, app))["merged"] is True

    statements = []
    connection = (await session.connection()).sync_connection

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    sa_event.listen(connection, "before_cursor_execute", count)
    try:
        assert await users.merge_guest_into_user(session, app) == {"merged": False, "guests_merged": 0}
    finally:
        sa_event.remove(connection, "before_cursor_execute", count)
    assert len(statements) == 1
    assert (await _row(session, EventRegistrationsLink, reg.id))["user_id"] == app.id


async def test_the_stronger_duplicate_wins_whoever_holds_it(session, make_user, make_registration, club_and_events):
    _, _, (paid_event, attended_event, plain_event, _) = club_and_events
    app = await _app_user(make_user, session, "kavya@example.com")
    guest = await _guest(make_user, "KAVYA@example.com")

    # Paid on the website as a guest beats the unpaid one made in the app.
    app_unpaid = await make_registration(event=paid_event, user=app, actual_amount=200)
    guest_paid = await make_registration(event=paid_event, user=guest, actual_amount=200, paid_amount=200, is_paid=True)
    # Checked in beats anything.
    app_paid = await make_registration(event=attended_event, user=app, is_paid=True, paid_amount=50, actual_amount=50)
    guest_attended = await make_registration(event=attended_event, user=guest, is_attended=True)
    # Otherwise the one made first.
    guest_first = await make_registration(event=plain_event, user=guest, created_at=NOW - timedelta(days=2))
    app_later = await make_registration(event=plain_event, user=app, created_at=NOW - timedelta(days=1))

    summary = await users.merge_guest_into_user(session, app)
    assert summary["registrations_retired"] == 3

    for winner in (guest_paid, guest_attended, guest_first):
        row = await _row(session, EventRegistrationsLink, winner.id)
        assert (row["user_id"], row["is_deleted"]) == (app.id, False)
    for loser in (app_unpaid, app_paid, app_later):
        assert (await _row(session, EventRegistrationsLink, loser.id))["is_deleted"] is True
    live = list(await session.scalars(select(EventRegistrationsLink.id).where(EventRegistrationsLink.user_id == app.id)))
    assert sorted(map(str, live)) == sorted(str(r.id) for r in (guest_paid, guest_attended, guest_first))


async def test_a_retired_duplicate_finds_a_free_place_under_the_unique_constraint(
    session, make_user, make_registration, club_and_events
):
    _, _, (crowded, packed, *_) = club_and_events
    app = await _app_user(make_user, session, "isha@example.com")
    guest = await _guest(make_user, "isha@example.com")

    # (event_id, user_id, is_deleted) is unique. The app account already
    # holds a retired registration for `crowded`, so its losing live one
    # cannot be retired in place.
    await make_registration(event=crowded, user=app, is_deleted=True)
    app_loser = await make_registration(event=crowded, user=app)
    guest_winner = await make_registration(event=crowded, user=guest, is_paid=True, paid_amount=10, actual_amount=10)
    # Both already hold a retired one for `packed`: the loser goes to a retired
    # archive account rather than nowhere.
    await make_registration(event=packed, user=app, is_deleted=True)
    await make_registration(event=packed, user=guest, is_deleted=True)
    packed_loser = await make_registration(event=packed, user=app)
    packed_winner = await make_registration(event=packed, user=guest, is_attended=True)

    summary = await users.merge_guest_into_user(session, app)
    assert summary["merged"] is True and "failed" not in summary

    row = await _row(session, EventRegistrationsLink, app_loser.id)
    assert (row["user_id"], row["is_deleted"]) == (guest.id, True)
    assert (await _row(session, EventRegistrationsLink, guest_winner.id))["user_id"] == app.id
    row = await _row(session, EventRegistrationsLink, packed_loser.id)
    archive = await _row(session, Users, row["user_id"])
    assert row["is_deleted"] is True and archive["user_type"] == UserTypes.guest and archive["is_deleted"] is True
    assert archive["id"] not in (app.id, guest.id)
    assert (await _row(session, EventRegistrationsLink, packed_winner.id))["user_id"] == app.id


async def test_duplicate_ratings_keep_the_newer_one(session, make_user, club_and_events):
    _, _, (e1, *_) = club_and_events
    app = await _app_user(make_user, session, "omar@example.com")
    guest = await _guest(make_user, "omar@example.com")
    older = EventRatingsLink(event_id=e1.id, user_id=app.id, rating=2, updated_at=NOW - timedelta(days=3))
    newer = EventRatingsLink(event_id=e1.id, user_id=guest.id, rating=5, updated_at=NOW - timedelta(hours=1))
    session.add_all([older, newer])
    await session.flush()

    await users.merge_guest_into_user(session, app)
    assert (await _row(session, EventRatingsLink, newer.id))["user_id"] == app.id
    assert (await _row(session, EventRatingsLink, older.id))["is_deleted"] is True


async def test_a_duplicate_being_paid_for_is_left_until_the_payment_settles(
    session, make_user, make_registration, club_and_events
):
    _, _, (e1, e2, *_) = club_and_events
    app = await _app_user(make_user, session, "tara@example.com")
    guest = await _guest(make_user, "tara@example.com")
    await make_registration(event=e1, user=app, is_attended=True)
    paying = await make_registration(event=e1, user=guest, payment_receipt="er_paying", actual_amount=100)
    session.add(PaymentOrders(receipt="er_paying", razorpay_order_id="order_paying", amount=100, status=OrderStatus.created, user_id=guest.id, source="event_registration", payload={}))
    other = await make_registration(event=e2, user=guest)
    await session.flush()

    summary = await users.merge_guest_into_user(session, app)
    assert summary["guests_held"] == 1 and summary["guests_merged"] == 0
    row = await _row(session, EventRegistrationsLink, paying.id)
    assert (row["user_id"], row["is_deleted"]) == (guest.id, False)
    # Everything else it held has still come over, and the guest stays live.
    assert (await _row(session, EventRegistrationsLink, other.id))["user_id"] == app.id
    assert (await _row(session, Users, guest.id))["is_deleted"] is False


async def test_nothing_is_merged_into_clubs_admins_or_password_accounts(
    client, session, make_user, make_club, make_admin, make_registration, club_and_events
):
    _, _, (e1, *_) = club_and_events
    club_user, _ = await make_club(name="Club With Guest", email="shared-club@example.com")
    admin = await make_admin(email="shared-admin@example.com")
    password_user = await make_user(email="shared-password@example.com", username="shared-password")
    regs = []
    for email in ("shared-club@example.com", "shared-admin@example.com", "shared-password@example.com"):
        regs.append(await make_registration(event=e1, user=await _guest(make_user, email.upper())))
    await session.commit()

    for account in (club_user, admin, password_user):
        assert await users.merge_guest_into_user(session, account) == {"merged": False, "guests_merged": 0}

    # Signing in with the password does not fold them in either: nothing
    # ever checked that this account owns the address it typed.
    signed_in = await client.post("/api/v1/auth/token", data={"username": "shared-password@example.com", "password": "Password123!"})
    assert signed_in.status_code == 200, signed_in.text
    for reg in regs:
        assert (await _row(session, EventRegistrationsLink, reg.id))["user_id"] != password_user.id


def _fake_google(monkeypatch, email):
    from app.api.auth import service as auth_service

    monkeypatch.setattr(
        auth_service.id_token, "verify_oauth2_token",
        lambda token, request, client_id: {"email": email, "email_verified": True, "name": "Signed In"},
    )


async def test_google_sign_in_merges_and_survives_a_merge_that_fails(
    client, session, monkeypatch, make_user, make_registration, club_and_events
):
    _, _, (e1, e2, *_) = club_and_events
    guest = await _guest(make_user, "Meera@Example.com")
    reg = await make_registration(event=e1, user=guest)
    await session.commit()
    # Kept as plain values: the failed merge's rollback expires the rows it touched.
    reg_id, guest_id = reg.id, guest.id
    _fake_google(monkeypatch, "meera@example.com")

    def broken(*args, **kwargs):
        raise RuntimeError("badges table on fire")

    # Fails after the registrations have already moved inside the savepoint.
    working = users._fold_badges
    monkeypatch.setattr(users, "_fold_badges", broken)
    r = await client.post("/api/v1/auth/google", json={"id_token": "x", "platform": "android"})
    assert r.status_code == 200, r.text
    row = await _row(session, EventRegistrationsLink, reg_id)
    assert (row["user_id"], row["is_deleted"]) == (guest_id, False)
    assert (await _row(session, Users, guest_id))["is_deleted"] is False

    monkeypatch.setattr(users, "_fold_badges", working)
    r = await client.post("/api/v1/auth/google", json={"id_token": "x", "platform": "android"})
    assert r.status_code == 200, r.text
    app_id = (await _row(session, EventRegistrationsLink, reg_id))["user_id"]
    assert (await _row(session, Users, app_id))["user_type"] == UserTypes.app_user


async def test_the_app_folds_in_a_website_ticket_on_launch_and_on_refresh(
    client, session, make_user, mint_token, club_and_events
):
    _, _, (e1, e2, *_) = club_and_events
    app = await _app_user(make_user, session, "dev@example.com")
    await session.commit()
    token = await mint_token(app)

    # Bought on the website without signing in: a fresh guest holds it.
    r = await client.post(f"/api/v1/events/registration/{e1.id}/register", json={"full_name": "Dev Patel", "email": "Dev@Example.com"})
    assert r.status_code == 200, r.text
    first = uuid.UUID(r.json()["event_registration_id"])
    assert (await _row(session, EventRegistrationsLink, first))["user_id"] != app.id

    profile = await client.get("/api/v1/user/profile/me", headers=auth_header(token))
    assert profile.status_code == 200, profile.text
    assert (await _row(session, EventRegistrationsLink, first))["user_id"] == app.id
    listed = await client.get("/api/v1/events/list?is_registered=true", headers=auth_header(token))
    assert [e["id"] for e in listed.json()["items"]] == [e1.id]

    # Another one later, folded in by the token refresh the app makes on its own.
    r = await client.post(f"/api/v1/events/registration/{e2.id}/register", json={"full_name": "Dev Patel", "email": "dev@example.com"})
    second = uuid.UUID(r.json()["event_registration_id"])
    refresh = create_access_token(data=AuthTokenData(user_id=app.id, token_type="refresh_token", token_version=app.token_version).model_dump())
    refreshed = await client.post("/api/v1/auth/refresh", data={"token": refresh})
    assert refreshed.status_code == 200, refreshed.text
    assert (await _row(session, EventRegistrationsLink, second))["user_id"] == app.id


async def test_a_payment_finishes_on_a_registration_the_merge_moved_mid_checkout(
    client, session, monkeypatch, make_user, mint_token, make_club, make_event
):
    from app.api.payments import service as payments

    owner, club = await make_club(name="Paid Club", email="merge-paid@example.com")
    paid = await make_event(club=club, created_by=owner, name="Paid Night", event_datetime=NOW + timedelta(days=4), has_fee=True, reg_fee=300)
    app = await _app_user(make_user, session, "sana@example.com")
    await session.commit()
    monkeypatch.setattr(payments.razorpay_client.order, "create", lambda data: {"id": "order_mid", "amount": data["amount"], "currency": "INR"})

    r = await client.post(f"/api/v1/events/registration/{paid.id}/register", json={"full_name": "Sana Khan", "email": "sana@example.com"})
    body = r.json()
    order = await client.post(
        "/api/v1/payments/orders",
        json={"source": "event_registration", "payload": {"event_id": paid.id, "event_registration_id": body["event_registration_id"]}},
        headers=auth_header(body["auth_token"]["access_token"]),
    )
    assert order.status_code == 200, order.text

    # The app launches while the website's checkout is open.
    assert (await client.get("/api/v1/user/profile/me", headers=auth_header(await mint_token(app)))).status_code == 200
    reg_id = uuid.UUID(body["event_registration_id"])
    assert (await _row(session, EventRegistrationsLink, reg_id))["user_id"] == app.id

    # Razorpay captures; the payment is matched by receipt, not by account.
    db_order = await session.scalar(select(PaymentOrders).where(PaymentOrders.razorpay_order_id == "order_mid"))
    assert db_order.user_id == app.id
    session.add(PaymentLogs(order_id=db_order.id, razorpay_payment_id="pay_mid", status=PaymentStatus.captured, amount=Decimal(300), payment_method="upi"))
    db_order.status = OrderStatus.paid
    await session.commit()
    await handle_event_registration_payment(session, db_order)
    row = await _row(session, EventRegistrationsLink, reg_id)
    assert (row["is_paid"], row["is_deleted"]) == (True, False)


async def test_the_admin_run_folds_in_every_waiting_guest_once(
    client, session, make_user, make_admin, make_registration, mint_token, club_and_events
):
    _, _, (e1, e2, *_) = club_and_events
    first = await _app_user(make_user, session, "lena@example.com")
    second = await _app_user(make_user, session, "Arjun@Example.com", provider=SigninProviders.apple)
    unverified = await make_user(email="typed@example.com", username="typed")
    reg1 = await make_registration(event=e1, user=await _guest(make_user, "LENA@example.com"))
    reg2 = await make_registration(event=e2, user=await _guest(make_user, "arjun@example.com"))
    reg3 = await make_registration(event=e1, user=await _guest(make_user, "typed@example.com"))
    owner = await make_admin(email="merge-owner@example.com")
    viewer = await make_admin(email="merge-viewer@example.com", role=AdminRoles.viewer)
    await session.commit()

    refused = await client.post("/api/v1/admin/users/merge-guests", headers=auth_header(await mint_token(viewer)))
    assert refused.status_code == 403

    r = await client.post("/api/v1/admin/users/merge-guests", headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    totals = r.json()
    assert (totals["app_users_checked"], totals["app_users_merged"], totals["guests_merged"], totals["registrations_transferred"], totals["failed"]) == (2, 2, 2, 2, 0)
    assert (await _row(session, EventRegistrationsLink, reg1.id))["user_id"] == first.id
    assert (await _row(session, EventRegistrationsLink, reg2.id))["user_id"] == second.id
    assert (await _row(session, EventRegistrationsLink, reg3.id))["user_id"] != unverified.id

    again = await client.post("/api/v1/admin/users/merge-guests", headers=auth_header(await mint_token(owner)))
    assert again.json()["app_users_checked"] == 0

