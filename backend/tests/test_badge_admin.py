"""Tests for the badge admin endpoints.

Badges was the most complete admin module -- create, update, recipients,
stats, manual award and revoke all existed. Two things were wrong:

- There was no way to delete a badge definition, only to edit it.
- /recipients and /stats fetched the badge with session.get and passed the
  result straight to BadgeDefinitionResponse.model_validate, so a badge id
  that did not exist produced a ValidationError and a 500 rather than a 404.
"""

import pytest

from tests.conftest import auth_header


@pytest.fixture
async def badge(session):
    from app.api.badges.models import BadgeDefinitions, BadgeType

    row = BadgeDefinitions(
        slug="test-badge",
        name="Test Badge",
        description="Awarded in tests",
        emoji="🏅",
        badge_type=BadgeType.user,
        trigger_metric="events_attended",
        threshold=1,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_stats_for_a_missing_badge_is_a_404(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/badges/admin/999999/stats", headers=auth_header(token)
    )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_recipients_for_a_missing_badge_is_a_404(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/badges/admin/999999/recipients", headers=auth_header(token)
    )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_stats_for_a_real_badge_still_work(
    client, make_admin, mint_token, badge
):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/badges/admin/{badge.id}/stats", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["badge"]["slug"] == "test-badge"


@pytest.mark.asyncio
async def test_award_then_list_recipients(
    client, make_admin, make_user, mint_token, badge
):
    admin = await make_admin()
    holder = await make_user(email="badgeholder@example.com")
    token = await mint_token(admin)

    award = await client.post(
        f"/api/v1/badges/admin/{badge.id}/award",
        headers=auth_header(token),
        json={"entity_id": holder.id},
    )
    assert award.status_code == 200, award.text

    recipients = await client.get(
        f"/api/v1/badges/admin/{badge.id}/recipients", headers=auth_header(token)
    )
    assert recipients.status_code == 200, recipients.text
    body = recipients.json()
    assert body["total"] == 1
    assert body["recipients"][0]["entity_id"] == holder.id


@pytest.mark.asyncio
async def test_revoke_removes_the_award(
    client, make_admin, make_user, mint_token, badge
):
    admin = await make_admin()
    holder = await make_user(email="revokee@example.com")
    token = await mint_token(admin)

    await client.post(
        f"/api/v1/badges/admin/{badge.id}/award",
        headers=auth_header(token),
        json={"entity_id": holder.id},
    )
    revoke = await client.delete(
        f"/api/v1/badges/admin/{badge.id}/revoke/{holder.id}",
        headers=auth_header(token),
    )
    assert revoke.status_code == 200, revoke.text

    recipients = await client.get(
        f"/api/v1/badges/admin/{badge.id}/recipients", headers=auth_header(token)
    )
    assert recipients.json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_removes_the_badge_and_reports_the_fallout(
    client, make_admin, make_user, mint_token, badge
):
    """The award rows reference the definition, so they have to go with it."""
    admin = await make_admin()
    holder = await make_user(email="losesbadge@example.com")
    token = await mint_token(admin)

    await client.post(
        f"/api/v1/badges/admin/{badge.id}/award",
        headers=auth_header(token),
        json={"entity_id": holder.id},
    )

    response = await client.delete(
        f"/api/v1/badges/admin/{badge.id}", headers=auth_header(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["revoked_from"] == 1

    gone = await client.get(
        f"/api/v1/badges/admin/{badge.id}/stats", headers=auth_header(token)
    )
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_deleting_a_badge_nobody_holds_reports_zero(
    client, make_admin, mint_token, badge
):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.delete(
        f"/api/v1/badges/admin/{badge.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["revoked_from"] == 0


@pytest.mark.asyncio
async def test_deleting_a_missing_badge_is_a_404(client, make_admin, mint_token):
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.delete(
        "/api/v1/badges/admin/999999", headers=auth_header(token)
    )

    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_a_non_admin_cannot_delete_a_badge(
    client, make_user, mint_token, badge
):
    nosy = await make_user(email="notadmin@example.com")
    token = await mint_token(nosy)

    response = await client.delete(
        f"/api/v1/badges/admin/{badge.id}", headers=auth_header(token)
    )

    assert response.status_code in (401, 403), response.text


@pytest.mark.asyncio
async def test_admin_badge_list_still_resolves(client, make_admin, mint_token, badge):
    """`/admin` must not be swallowed by `/admin/{badge_id}`."""
    admin = await make_admin()
    token = await mint_token(admin)

    response = await client.get("/api/v1/badges/admin", headers=auth_header(token))

    assert response.status_code == 200, response.text
    assert any(b["slug"] == "test-badge" for b in response.json())
