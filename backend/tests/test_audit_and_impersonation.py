"""The admin audit log, and impersonation built on top of it.

Until this table the dashboard could delete clubs and events, approve and
reject registrations, award and revoke badges, block organisations and refund
money with no record of who did any of it. "Who deleted that club?" had no
answer and none could be reconstructed.

Impersonation was deliberately not built until the log existed: an admin
acting as another user with no trace is precisely the capability worth
refusing.
"""

import pytest
import sqlalchemy as sa

from tests.conftest import auth_header


async def entries(client, token, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = await client.get(
        f"/api/v1/admin/audit-log{'?' + query if query else ''}",
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


@pytest.mark.asyncio
async def test_approving_a_club_is_recorded(
    client, make_admin, make_club, mint_token
):
    admin = await make_admin()
    _, club = await make_club(
        name="Audited", email="audited@example.com", is_verified=False
    )
    token = await mint_token(admin)

    await client.post(
        f"/api/v1/clubs/{club.id}/approve", headers=auth_header(token)
    )

    rows = await entries(client, token, action="club.approve")
    assert len(rows) == 1
    assert rows[0]["entity_id"] == str(club.id)
    assert "Audited" in rows[0]["summary"]
    assert rows[0]["actor"]["email"] == "admin@example.com"


@pytest.mark.asyncio
async def test_deleting_a_club_is_recorded(client, make_admin, make_club, mint_token):
    admin = await make_admin()
    _, club = await make_club(name="Doomed", email="auditdoomed@example.com")
    token = await mint_token(admin)

    await client.delete(
        f"/api/v1/clubs/admin/{club.id}", headers=auth_header(token)
    )

    rows = await entries(client, token, action="club.delete")
    assert len(rows) == 1
    assert "Doomed" in rows[0]["summary"]


@pytest.mark.asyncio
async def test_blocking_an_organisation_records_the_new_state(
    client, session, make_admin, mint_token
):
    from app.api.orgs.models import Organizations, OrgTypes

    admin = await make_admin()
    org = Organizations(name="Audited Org", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    token = await mint_token(admin)

    await client.put(
        f"/api/v1/orgs/block/{org.id}",
        headers=auth_header(token),
        json={"is_blocked": True},
    )

    rows = await entries(client, token, action="org.block")
    assert rows[0]["details"]["is_blocked"] is True


@pytest.mark.asyncio
async def test_history_can_be_read_for_one_record(
    client, make_admin, make_club, mint_token
):
    """The question actually asked after something goes wrong."""
    admin = await make_admin()
    _, club = await make_club(
        name="Traced", email="traced@example.com", is_verified=False
    )
    _, other = await make_club(name="Other", email="otheraudit@example.com")
    token = await mint_token(admin)

    await client.post(f"/api/v1/clubs/{club.id}/approve", headers=auth_header(token))
    await client.delete(f"/api/v1/clubs/admin/{other.id}", headers=auth_header(token))

    rows = await entries(client, token, entity_type="club", entity_id=club.id)
    assert [r["action"] for r in rows] == ["club.approve"]


@pytest.mark.asyncio
async def test_the_log_is_newest_first(client, make_admin, make_club, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)
    for i in range(3):
        _, club = await make_club(
            name=f"Seq {i}", email=f"seq{i}@example.com", is_verified=False
        )
        await client.post(
            f"/api/v1/clubs/{club.id}/approve", headers=auth_header(token)
        )

    rows = await entries(client, token, action="club.approve")
    assert [r["summary"] for r in rows] == [
        "Approved club “Seq 2”",
        "Approved club “Seq 1”",
        "Approved club “Seq 0”",
    ]


@pytest.mark.asyncio
async def test_the_log_is_admin_only(client, make_user, mint_token):
    nosy = await make_user(email="nosyaudit@example.com")
    token = await mint_token(nosy)

    response = await client.get("/api/v1/admin/audit-log", headers=auth_header(token))

    assert response.status_code in (401, 403), response.text


@pytest.mark.asyncio
async def test_record_swallows_its_own_failures(session, make_admin):
    """
    An admin blocked from approving a club because the audit store was
    unavailable is a worse outcome than a missing line, so `record` must never
    raise into the action it is describing. `action` is a 60-character column,
    so this overflows it and the write fails inside the flush.
    """
    from app.api.audit import service as audit_service

    admin = await make_admin()

    await audit_service.record(
        session,
        actor=admin,
        action="x" * 500,
        summary="overlong action",
    )
    # And the session is still usable afterwards, which is what lets the
    # caller go on to commit the change it was recording.
    assert await session.scalar(sa.select(sa.literal(1))) == 1


@pytest.mark.asyncio
async def test_impersonation_mints_a_token_and_logs_it(
    client, make_admin, make_user, mint_token
):
    admin = await make_admin()
    target = await make_user(email="target@example.com")
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/auth/admin/impersonate/{target.id}",
        headers=auth_header(token),
        json={"reason": "debugging a report"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == target.id
    assert body["token"]

    rows = await entries(client, token, action="user.impersonate")
    assert len(rows) == 1
    assert rows[0]["details"]["reason"] == "debugging a report"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [None, {}, {"reason": None}, {"reason": ""}, {"reason": "test"}, {"reason": "    short     "}, {"reason": "a  b  c  d  e"}],
    ids=["no-body", "empty-body", "null", "blank", "four-letters", "padded-short", "spaced-out"],
)
async def test_a_session_as_someone_else_needs_a_reason_of_ten_characters(
    client, make_admin, make_user, mint_token, body
):
    """Ten characters, not counting spaces at the ends or in a row: a
    session nobody can explain afterwards is one nobody can review."""
    admin = await make_admin()
    target = await make_user(email="noreason@example.com")
    token = await mint_token(admin)

    kwargs = {} if body is None else {"json": body}
    response = await client.post(
        f"/api/v1/auth/admin/impersonate/{target.id}", headers=auth_header(token), **kwargs
    )

    assert response.status_code == 422, response.text
    answer = response.json()
    assert answer["error_code"] == "REASON_REQUIRED"
    assert "at least 10 characters" in answer["message"]
    assert answer["errors"]["fields"]["reason"]
    assert "token" not in answer
    # Refused before anything was minted: nothing to find in the log.
    assert await entries(client, token, action="user.impersonate") == []


@pytest.mark.asyncio
async def test_the_reason_is_kept_tidied_and_bounded(client, make_admin, make_user, mint_token):
    admin = await make_admin()
    target = await make_user(email="tidyreason@example.com")
    token = await mint_token(admin)
    url = f"/api/v1/auth/admin/impersonate/{target.id}"

    exactly_ten = await client.post(url, headers=auth_header(token), json={"reason": "  ticket  #42  "})
    assert exactly_ten.status_code == 200, exactly_ten.text
    too_long = await client.post(url, headers=auth_header(token), json={"reason": "x" * 301})
    assert too_long.status_code == 422 and too_long.json()["error_code"] == "REASON_TOO_LONG"

    rows = await entries(client, token, action="user.impersonate")
    assert [r["details"]["reason"] for r in rows] == ["ticket #42"]


@pytest.mark.asyncio
async def test_an_admin_cannot_impersonate_another_admin(
    client, make_admin, mint_token
):
    """Otherwise the log can be made to point at the wrong person."""
    admin = await make_admin()
    other = await make_admin(email="otheradmin@example.com")
    token = await mint_token(admin)

    response = await client.post(
        f"/api/v1/auth/admin/impersonate/{other.id}", headers=auth_header(token)
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_a_non_admin_cannot_impersonate(client, make_user, mint_token):
    nosy = await make_user(email="nosyimp@example.com")
    victim = await make_user(email="victim@example.com")
    token = await mint_token(nosy)

    response = await client.post(
        f"/api/v1/auth/admin/impersonate/{victim.id}", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text


@pytest.mark.asyncio
async def test_the_handoff_token_exchanges_for_a_session(
    client, make_admin, make_user, mint_token
):
    admin = await make_admin()
    target = await make_user(email="exchange@example.com")
    token = await mint_token(admin)

    minted = await client.post(
        f"/api/v1/auth/admin/impersonate/{target.id}",
        headers=auth_header(token),
        json={"reason": "support ticket: cannot publish"},
    )
    handoff = minted.json()["token"]

    exchanged = await client.post(
        "/api/v1/auth/impersonate/exchange", data={"token": handoff}
    )

    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["access_token"]
    assert "access_token" in exchanged.cookies


@pytest.mark.asyncio
async def test_an_ordinary_access_token_cannot_be_exchanged(
    client, make_user, mint_token
):
    """
    Otherwise any leaked access token could be laundered into a session that
    looks like a normal login.
    """
    user = await make_user(email="launder@example.com")
    token = await mint_token(user)

    response = await client.post(
        "/api/v1/auth/impersonate/exchange", data={"token": token}
    )

    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_a_garbage_token_is_rejected(client):
    response = await client.post(
        "/api/v1/auth/impersonate/exchange", data={"token": "not-a-jwt"}
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_the_notification_log_groups_one_send_into_one_row(
    client, session, make_admin, make_club, make_user, make_event, mint_token
):
    """
    Notifications are stored per recipient. Without grouping, one announcement
    to a large club would fill the page.
    """
    from app.api.notifications.models import Notifications

    admin = await make_admin()
    owner, club = await make_club(name="Shouty", email="shouty@example.com")
    event = await make_event(club=club, name="Loud Event", created_by=owner)

    for i in range(3):
        recipient = await make_user(email=f"recipient{i}@example.com")
        session.add(
            Notifications(
                user_id=recipient.id,
                title="Doors open at six",
                description="See you there",
                type="club_announcement",
                from_club_id=club.id,
                event_id=event.id,
            )
        )
    await session.flush()

    token = await mint_token(admin)
    response = await client.get(
        "/api/v1/admin/notifications", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["sent_count"] == 3
    assert items[0]["title"] == "Doors open at six"
    assert items[0]["club_name"] == "Shouty"
    assert items[0]["event_name"] == "Loud Event"


@pytest.mark.asyncio
async def test_the_notification_log_is_admin_only(client, make_user, mint_token):
    nosy = await make_user(email="nosynotif@example.com")
    token = await mint_token(nosy)

    response = await client.get(
        "/api/v1/admin/notifications", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text
