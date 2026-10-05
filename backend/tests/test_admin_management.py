"""Admin account management: editing, deleting, roles, and restore.

Roles were enforced from the day they existed but could only be changed with
a direct database update, so the feature was half-delivered. Admins could
suspend an account but not correct a typo'd email. And only clubs could be
restored -- events and organisations soft-deleted with no undo.
"""

import pytest

from app.api.users.models import AdminRoles
from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_an_admin_can_correct_an_email(
    client, make_admin, make_user, mint_token
):
    admin = await make_admin()
    target = await make_user(email="typo@example.com")
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/user/admin/{target.id}",
        headers=auth_header(token),
        json={"email": "fixed@example.com"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "fixed@example.com"


@pytest.mark.asyncio
async def test_editing_leaves_omitted_fields_alone(
    client, make_admin, make_user, mint_token
):
    """Unlike the club and event handlers, this is a genuine partial update."""
    admin = await make_admin()
    target = await make_user(email="partial@example.com")
    token = await mint_token(admin)

    await client.put(
        f"/api/v1/user/admin/{target.id}",
        headers=auth_header(token),
        json={"full_name": "New Name"},
    )

    detail = await client.get(
        f"/api/v1/user/admin/{target.id}", headers=auth_header(token)
    )
    body = detail.json()
    assert body["full_name"] == "New Name"
    assert body["email"] == "partial@example.com"


@pytest.mark.asyncio
async def test_an_email_already_in_use_is_rejected(
    client, make_admin, make_user, mint_token
):
    """(email, user_type) is unique, so this would be a 500 from the database."""
    admin = await make_admin()
    first = await make_user(email="taken@example.com")
    second = await make_user(email="other@example.com")
    token = await mint_token(admin)

    response = await client.put(
        f"/api/v1/user/admin/{second.id}",
        headers=auth_header(token),
        json={"email": first.email},
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_a_user_can_be_deleted_and_is_signed_out(
    client, make_admin, make_user, mint_token
):
    admin = await make_admin()
    target = await make_user(email="deleteme@example.com")
    victim_token = await mint_token(target)
    admin_token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/user/admin/{target.id}", headers=auth_header(admin_token)
    )
    assert response.status_code == 200, response.text

    # The token version is bumped, so an issued session stops immediately.
    after = await client.get("/api/v1/auth/me", headers=auth_header(victim_token))
    assert after.status_code in (401, 403)


@pytest.mark.asyncio
async def test_an_admin_cannot_delete_themselves(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/user/admin/{admin.id}", headers=auth_header(token)
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_an_admin_role_can_be_granted(client, make_admin, mint_token):
    owner = await make_admin(email="granter@example.com", role=AdminRoles.owner)
    other = await make_admin(email="grantee@example.com", role=AdminRoles.owner)
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/user/admin/{other.id}/role",
        headers=auth_header(token),
        json={"role": "support"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["admin_role"] == "support"


@pytest.mark.asyncio
async def test_a_role_cannot_be_given_to_a_non_admin(
    client, make_admin, make_user, mint_token
):
    """It would be a silent no-op the caller expects to have worked."""
    owner = await make_admin()
    ordinary = await make_user(email="notadminrole@example.com")
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/user/admin/{ordinary.id}/role",
        headers=auth_header(token),
        json={"role": "support"},
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_an_owner_cannot_demote_themselves(client, make_admin, mint_token):
    """Demoting yourself removes the capability needed to undo it."""
    owner = await make_admin()
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/user/admin/{owner.id}/role",
        headers=auth_header(token),
        json={"role": "viewer"},
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_only_an_owner_can_grant_roles(client, make_admin, mint_token):
    """Anyone who can hand out `owner` can grant themselves anything."""
    support = await make_admin(email="supportgrant@example.com", role=AdminRoles.support)
    other = await make_admin(email="othergrant@example.com")
    token = await mint_token(support)

    response = await client.put(
        f"/api/v1/user/admin/{other.id}/role",
        headers=auth_header(token),
        json={"role": "owner"},
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_an_unknown_role_is_rejected(client, make_admin, mint_token):
    owner = await make_admin()
    other = await make_admin(email="unknownrole@example.com")
    token = await mint_token(owner)

    response = await client.put(
        f"/api/v1/user/admin/{other.id}/role",
        headers=auth_header(token),
        json={"role": "superuser"},
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_a_deleted_event_can_be_restored(
    client, make_admin, make_club, make_event, mint_token
):
    admin = await make_admin()
    owner, club = await make_club(name="Restorer", email="restorer@example.com")
    event = await make_event(club=club, name="Undo Me", created_by=owner)
    token = await mint_token(admin)

    await client.delete(
        f"/api/v1/events/delete/{event.id}", headers=auth_header(token)
    )

    deleted = await client.get(
        "/api/v1/events/admin/deleted/list", headers=auth_header(token)
    )
    assert [e["name"] for e in deleted.json()["items"]] == ["Undo Me"]

    restored = await client.post(
        f"/api/v1/events/admin/{event.id}/restore", headers=auth_header(token)
    )
    assert restored.status_code == 200, restored.text

    gone = await client.get(
        "/api/v1/events/admin/deleted/list", headers=auth_header(token)
    )
    assert gone.json()["total"] == 0


@pytest.mark.asyncio
async def test_a_deleted_organisation_can_be_restored(
    client, session, make_admin, mint_token
):
    from app.api.orgs.models import Organizations, OrgTypes

    admin = await make_admin()
    org = Organizations(name="Undo Org", type=OrgTypes.college)
    session.add(org)
    await session.flush()
    token = await mint_token(admin)

    await client.delete(f"/api/v1/orgs/delete/{org.id}", headers=auth_header(token))

    deleted = await client.get(
        "/api/v1/orgs/deleted/list", headers=auth_header(token)
    )
    assert [o["name"] for o in deleted.json()["items"]] == ["Undo Org"]

    restored = await client.post(
        f"/api/v1/orgs/{org.id}/restore", headers=auth_header(token)
    )
    assert restored.status_code == 200, restored.text

    live = await client.get(f"/api/v1/orgs/{org.id}", headers=auth_header(token))
    assert live.status_code == 200


@pytest.mark.asyncio
async def test_the_deleted_org_list_is_not_shadowed_by_the_detail_route(
    client, make_admin, mint_token
):
    """`/deleted/list` must win over `/{id}`."""
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/orgs/deleted/list", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert "items" in response.json()
