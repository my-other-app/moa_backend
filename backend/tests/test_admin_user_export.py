"""The admin user list's status fields, and the CSV export of the same list.

Two things the v2 Users screen needs that the list did not report:

- `is_active`, so a suspended account can be badged as one in the table
  instead of only inside the detail drawer, and filtered on.
- `admin_role`, but only for accounts that are actually admins. The column is
  NOT NULL and defaults to `owner`, so read literally every app user in the
  database is an owner -- a table driven off the raw column would badge the
  whole user base as administrators.

The export is a separate endpoint rather than the dashboard paging the list
and assembling a file client-side: that put the capability check nowhere (a
viewer can page a list) and N round trips on a CPU-throttled box.
"""

import csv
import io

import pytest

from tests.conftest import auth_header


def rows_of(response):
    """The CSV body as dicts, with the Excel BOM stripped."""
    text = response.text
    if text.startswith("﻿"):
        text = text[1:]
    return list(csv.DictReader(io.StringIO(text)))


@pytest.mark.asyncio
async def test_list_reports_suspension(client, session, make_admin, make_user, mint_token):
    admin = await make_admin()
    suspended = await make_user(email="suspended@example.com")
    suspended.is_active = False
    suspended.suspended_reason = "Spam registrations"
    await make_user(email="fine@example.com")
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/list?user_type=app_user", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    by_email = {item["email"]: item for item in response.json()["items"]}
    assert by_email["suspended@example.com"]["is_active"] is False
    assert by_email["fine@example.com"]["is_active"] is True


@pytest.mark.asyncio
async def test_list_filters_to_suspended_only(
    client, session, make_admin, make_user, mint_token
):
    admin = await make_admin()
    suspended = await make_user(email="gone@example.com")
    suspended.is_active = False
    await make_user(email="here@example.com")
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/list?is_active=false", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["email"] for item in body["items"]] == ["gone@example.com"]
    # The filter has to narrow the total too, or the pager offers pages that
    # do not exist.
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_list_hides_the_default_role_from_non_admins(
    client, make_admin, make_user, mint_token
):
    """
    The one that bites: `admin_role` defaults to `owner` for everybody.

    Without the schema nulling it, this app user reports `owner` and the
    dashboard badges them -- and anyone reading the response would conclude
    the platform had granted every signup full administrative access.
    """
    admin = await make_admin(role="support")
    await make_user(email="ordinary@example.com")
    token = await mint_token(admin)

    response = await client.get("/api/v1/user/admin/list", headers=auth_header(token))

    assert response.status_code == 200, response.text
    by_email = {item["email"]: item for item in response.json()["items"]}
    assert by_email["ordinary@example.com"]["admin_role"] is None
    assert by_email["admin@example.com"]["admin_role"] == "support"


@pytest.mark.asyncio
async def test_detail_hides_the_default_role_too(
    client, make_admin, make_user, mint_token
):
    """The drawer reads this field, so the list and the detail must agree."""
    admin = await make_admin(role="owner")
    subject = await make_user(email="detail@example.com")
    token = await mint_token(admin)

    response = await client.get(
        f"/api/v1/user/admin/{subject.id}", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["admin_role"] is None


@pytest.mark.asyncio
async def test_export_returns_the_filtered_rows_as_csv(
    client, session, make_admin, make_user, mint_token
):
    admin = await make_admin()
    suspended = await make_user(email="blocked@example.com")
    suspended.is_active = False
    suspended.suspended_reason = "Chargebacks"
    await make_user(email="active@example.com")
    await session.flush()
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/export?is_active=false", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"

    rows = rows_of(response)
    assert [row["Email"] for row in rows] == ["blocked@example.com"]
    assert rows[0]["Status"] == "suspended"
    assert rows[0]["Suspended reason"] == "Chargebacks"


@pytest.mark.asyncio
async def test_export_starts_with_a_bom(client, make_admin, make_user, mint_token):
    """Without it Excel on Windows mangles every non-ASCII name in the file."""
    admin = await make_admin()
    await make_user(email="someone@example.com")
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/export", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert response.text.startswith("﻿")


@pytest.mark.asyncio
async def test_export_honours_the_search_filter(
    client, make_admin, make_user, mint_token
):
    """An export that ignored the filters would be worse than none: the file
    looks complete and the admin cannot tell by reading it."""
    admin = await make_admin()
    await make_user(email="wanted@example.com", username="wanted")
    await make_user(email="other@example.com", username="other")
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/export?search=wanted", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    assert [row["Email"] for row in rows_of(response)] == ["wanted@example.com"]


@pytest.mark.asyncio
async def test_export_needs_the_export_capability(
    client, make_admin, make_user, mint_token
):
    """A viewer can read a page of the list; handing over the whole user base
    as one file is the act the role split cares about."""
    viewer = await make_admin(email="viewer@example.com", role="viewer")
    await make_user(email="subject@example.com")
    token = await mint_token(viewer)

    response = await client.get(
        "/api/v1/user/admin/export", headers=auth_header(token)
    )

    assert response.status_code == 403, response.text
    # And it must not have leaked a row on the way to refusing.
    assert "subject@example.com" not in response.text


@pytest.mark.asyncio
async def test_export_pages_past_one_chunk(
    client, monkeypatch, make_admin, make_user, mint_token
):
    """
    The chunked read is the part most likely to be silently wrong.

    With the chunk size forced below the row count, a missing `offset +=`
    loops forever and a missing tiebreak in the ORDER BY can repeat or drop
    rows at a boundary. Both show up here as the wrong set of emails.
    """
    from app.api.users import router as users_router

    monkeypatch.setattr(users_router, "_EXPORT_CHUNK", 2)

    admin = await make_admin()
    expected = []
    for i in range(5):
        email = f"chunk{i}@example.com"
        await make_user(email=email, username=f"chunk{i}")
        expected.append(email)
    token = await mint_token(admin)

    response = await client.get(
        "/api/v1/user/admin/export?user_type=app_user", headers=auth_header(token)
    )

    assert response.status_code == 200, response.text
    emails = [row["Email"] for row in rows_of(response)]
    assert sorted(emails) == sorted(expected)
    assert len(emails) == len(set(emails)), "a row crossed a chunk boundary twice"
