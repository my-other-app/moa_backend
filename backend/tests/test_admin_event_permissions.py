"""An admin's role on a club's own routes.

The club's routes (ClubAuth) admit admins too, and behind them an admin may
act on any club's event -- but no route asked the admin's role, so a viewer
or support admin could edit or delete any event, set its winners and
download its guest list. The club-only dependency now holds an admin to what
the request does: reading needs VIEW, changing needs MANAGE_CONTENT (as
editing the club record does), deleting needs DESTROY. A download of every
guest needs EXPORT_DATA, as the users export does. The club itself is not
affected.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.users.models import AdminRoles
from app.core.auth.permissions import (
    DESTROY,
    MANAGE_CONTENT,
    VIEW,
    capability_for_method,
)
from tests.conftest import auth_header

ROLES = (AdminRoles.viewer, AdminRoles.support, AdminRoles.owner)


@pytest.fixture
async def world(session, make_club, make_admin, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Role Club", email="roleclub@example.com")
    event = await make_event(club=club, created_by=owner, name="Role Night",
                             event_datetime=datetime.now(timezone.utc) + timedelta(days=5))
    guest = await make_user(email="roleguest@example.com")
    registration = await make_registration(event=event, user=guest)
    # An event with nobody registered, which may be deleted.
    empty = await make_event(club=club, created_by=owner, name="Empty Night",
                             event_datetime=datetime.now(timezone.utc) + timedelta(days=6))
    admins = {}
    for role in ROLES:
        admin = await make_admin(email=f"{role.value}-perm@example.com", role=role)
        admins[role] = auth_header(await mint_token(admin))
    await session.commit()
    form = {
        "name": "Role Night", "event_datetime": event.event_datetime.isoformat(), "duration": "2",
        "has_fee": "false", "category_id": str(event.category_id),
    }
    return dict(
        event=event, empty=empty, registration=registration, admins=admins, form=form,
        club=auth_header(await mint_token(owner)),
    )


def _allowed(role: AdminRoles, capability: str) -> bool:
    return {
        AdminRoles.viewer: {VIEW},
        AdminRoles.support: {VIEW, "moderate"},
        AdminRoles.owner: {VIEW, MANAGE_CONTENT, DESTROY, "export_data"},
    }[role].__contains__(capability)


def _refused(response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_editing_an_event_needs_manage_content(client, world, role):
    r = await client.put(f"/api/v1/events/update/{world['event'].id}", data={**world["form"], "name": "Renamed Night"},
                         headers=world["admins"][role])
    if _allowed(role, MANAGE_CONTENT):
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Renamed Night"
    else:
        _refused(r)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_deleting_an_event_needs_destroy(client, world, role):
    r = await client.delete(f"/api/v1/events/delete/{world['empty'].id}", headers=world["admins"][role])
    if _allowed(role, DESTROY):
        assert r.status_code == 200, r.text
    else:
        _refused(r)
        # Still there.
        assert (await client.get(f"/api/v1/events/info/{world['empty'].id}")).status_code == 200


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_every_admin_role_may_read_the_guest_list(client, world, role):
    r = await client.get(f"/api/v1/events/registration/{world['event'].id}/list", headers=world["admins"][role])
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["email"] == "roleguest@example.com"
    analytics = await client.get(f"/api/v1/events/registration/{world['event'].id}/analytics", headers=world["admins"][role])
    assert analytics.status_code == 200, analytics.text


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_exporting_the_guest_list_needs_export_data(client, world, role):
    r = await client.get(f"/api/v1/events/registration/{world['event'].id}/export", headers=world["admins"][role])
    if _allowed(role, "export_data"):
        assert r.status_code == 200, r.text
    else:
        _refused(r)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_setting_a_winner_needs_manage_content(client, world, role):
    r = await client.put(
        f"/api/v1/events/registration/{world['event'].id}/winners/{world['registration'].id}",
        json={"position": 1}, headers=world["admins"][role],
    )
    if _allowed(role, MANAGE_CONTENT):
        assert r.status_code == 200, r.text
        assert r.json()["position"] == 1
    else:
        _refused(r)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_marking_attendance_needs_manage_content(client, world, role):
    r = await client.post(
        f"/api/v1/events/registration/{world['event'].id}/attendance/{world['registration'].id}",
        json={"is_attended": True}, headers=world["admins"][role],
    )
    if _allowed(role, MANAGE_CONTENT):
        assert r.status_code == 200, r.text
    else:
        _refused(r)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_the_rule_holds_on_every_club_route_not_just_the_events_ones(client, world, role):
    """The agenda, the ticket deliveries: routes reached through the shared
    ownership helpers, which every module uses."""
    event_id = world["event"].id
    read = await client.get(f"/api/v1/events/{event_id}/deliveries/summary", headers=world["admins"][role])
    assert read.status_code == 200, read.text

    block = {"starts_at": world["event"].event_datetime.isoformat(), "title": "Doors open"}
    made = await client.post(f"/api/v1/events/{event_id}/agenda", json=block, headers=world["admins"][role])
    if _allowed(role, MANAGE_CONTENT):
        assert made.status_code in (200, 201), made.text
    else:
        _refused(made)

    statement = await client.get(f"/api/v1/events/{event_id}/finance/statement.csv", headers=world["admins"][role])
    if _allowed(role, "export_data"):
        assert statement.status_code == 200, statement.text
    else:
        _refused(statement)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r.value)
async def test_the_door_console_holds_an_admin_to_the_role_too(client, world, role):
    event_id = world["event"].id
    context = await client.get(f"/api/v1/events/{event_id}/gate/context", headers=world["admins"][role])
    assert context.status_code == 200, context.text
    check_in = await client.post(
        f"/api/v1/events/{event_id}/gate/check-in/{world['registration'].id}", json={}, headers=world["admins"][role],
    )
    if _allowed(role, MANAGE_CONTENT):
        assert check_in.status_code == 200, check_in.text
    else:
        _refused(check_in)


async def test_the_club_itself_is_not_held_to_any_admin_role(client, world):
    event_id = world["event"].id
    edited = await client.put(f"/api/v1/events/update/{event_id}", data={**world["form"], "name": "Club Night"}, headers=world["club"])
    assert edited.status_code == 200, edited.text
    exported = await client.get(f"/api/v1/events/registration/{event_id}/export", headers=world["club"])
    assert exported.status_code == 200, exported.text
    deleted = await client.delete(f"/api/v1/events/delete/{world['empty'].id}", headers=world["club"])
    assert deleted.status_code == 200, deleted.text


def test_the_method_names_the_capability():
    assert capability_for_method("GET") == VIEW
    assert capability_for_method("head") == VIEW
    assert capability_for_method("POST") == MANAGE_CONTENT
    assert capability_for_method("PUT") == MANAGE_CONTENT
    assert capability_for_method("PATCH") == MANAGE_CONTENT
    assert capability_for_method("DELETE") == DESTROY
