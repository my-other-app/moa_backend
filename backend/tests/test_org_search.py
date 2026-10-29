"""Finding, suggesting and merging organisations.

The 1,936 Kerala colleges imported into the live table could only be reached
through an unfiltered GET /orgs/list: a sign-up screen asking a student for
their college had no way to search, nobody could add the one the import
missed, and an admin had no way to put two rows for one college back
together.

The rows this exercises are the ones in that import that make it hard:

- "College of Applied Science, Chelakkara", which a student types as "appl
  sci chela" -- parts of words, in the order they come to mind;
- "College of Applied Science (IHRD)" beside "College of Applied Science
  IHRD", two rows whose names differ only by their brackets;
- "Calicut University Teacher Education Centre" in five towns, one name and
  five addresses, which are five real places and must not be offered as
  duplicates of each other;
- the same name twice where one row has no address and the other does, which
  is what a row imported twice looks like.
"""

import pytest
from sqlalchemy import select, text

from app.api.orgs.models import Organizations, OrgTypes
from tests.conftest import auth_header

SEARCH = "/api/v1/orgs/search"
SUGGEST = "/api/v1/orgs/suggest"
ADMIN_LIST = "/api/v1/orgs/admin/list"
DUPLICATES = "/api/v1/orgs/admin/duplicates"


async def _org(session, name, **kwargs):
    row = Organizations(name=name, type=kwargs.pop("type", OrgTypes.college), **kwargs)
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def colleges(session):
    """A handful of rows shaped like the ones in the real import."""
    rows = {
        "chelakkara": await _org(session, "College of Applied Science, Chelakkara", address="Chelakkara, Thrissur", is_verified=True),
        "ihrd_brackets": await _org(session, "College of Applied Science (IHRD)", address="Kozhikode", is_verified=True),
        "ihrd_plain": await _org(session, "College of Applied Science IHRD", address="Malappuram"),
        "calicut": await _org(session, "Calicut University", address="Thenhipalam", type=OrgTypes.university, is_verified=True),
        "centre_a": await _org(session, "Calicut University Teacher Education Centre", address="Aranattukara"),
        "centre_b": await _org(session, "Calicut University Teacher Education Centre", address="Kodenchery"),
        "infopark": await _org(session, "Infopark Technologies", address="Kakkanad", type=OrgTypes.company, is_verified=True),
        "blocked": await _org(session, "Blocked Applied College", address="Kannur", is_blocked=True),
        "gone": await _org(session, "Deleted Applied College", address="Kollam", is_deleted=True),
    }
    await session.commit()
    return rows


def _names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [row["name"] for row in response.json()]


# ─── The public type-ahead ────────────────────────────────────────────────


async def test_the_search_needs_no_sign_in_and_returns_the_picker_s_fields(client, colleges):
    response = await client.get(SEARCH, params={"q": "infopark"})
    assert response.status_code == 200
    row = response.json()[0]
    assert set(row) == {"id", "name", "type", "address", "is_verified"}
    assert row["name"] == "Infopark Technologies" and row["type"] == "company" and row["is_verified"] is True


async def test_parts_of_words_in_any_order_find_one_college(client, colleges):
    # What a student types: three fragments, none of them a whole word, and
    # the last of them from the address rather than the name.
    assert _names(await client.get(SEARCH, params={"q": "appl sci chela"})) == [
        "College of Applied Science, Chelakkara"
    ]
    # The same words the other way round are the same search.
    assert _names(await client.get(SEARCH, params={"q": "chela sci appl"})) == [
        "College of Applied Science, Chelakkara"
    ]


async def test_punctuation_is_not_part_of_the_name(client, colleges):
    """"College of Applied Science (IHRD)" and "College of Applied Science
    IHRD" are one query's answer, whichever way the query writes it."""
    for q in ("applied science ihrd", "applied science (ihrd)", "Applied Science, IHRD"):
        found = _names(await client.get(SEARCH, params={"q": q}))
        assert "College of Applied Science (IHRD)" in found, (q, found)
        assert "College of Applied Science IHRD" in found, (q, found)
    # And the brackets in the row are no bar to finding it by the bare word.
    assert "College of Applied Science (IHRD)" in _names(await client.get(SEARCH, params={"q": "ihrd"}))


async def test_a_typo_still_finds_it(client, colleges):
    """"Univrsity" is in no row's text, so the substring pass finds nothing
    and the typo pass answers instead."""
    found = _names(await client.get(SEARCH, params={"q": "calicut univrsity"}))
    assert "Calicut University" in found


async def test_an_exact_name_outranks_the_rows_that_merely_contain_it(client, colleges):
    found = _names(await client.get(SEARCH, params={"q": "Calicut University"}))
    # The university itself, then its teacher education centres.
    assert found[0] == "Calicut University"
    assert found[1:3] == ["Calicut University Teacher Education Centre"] * 2


async def test_verified_rows_come_before_unverified_ones_of_the_same_rank(client, colleges):
    """The two IHRD rows hold the same normalised name, so they rank the same
    and only `is_verified` separates them: a row somebody suggested never
    outranks the real one. (How well the name answers still comes first --
    the Chelakkara college below them matched by its words, not by holding
    the query, and being verified does not lift it past a better match.)"""
    found = _names(await client.get(SEARCH, params={"q": "applied science i"}))
    assert found.index("College of Applied Science (IHRD)") < found.index("College of Applied Science IHRD")
    assert found[:2] == ["College of Applied Science (IHRD)", "College of Applied Science IHRD"], found


async def test_the_type_filter_narrows_it(client, colleges):
    assert _names(await client.get(SEARCH, params={"q": "technologies", "type": "company"})) == ["Infopark Technologies"]
    # Nothing here assumes an organisation is a college: a company is found
    # by its own type, and asking for a college leaves it out.
    assert "Infopark Technologies" not in _names(await client.get(SEARCH, params={"q": "infopark", "type": "college"}))


async def test_blocked_and_deleted_rows_are_never_offered(client, colleges):
    found = _names(await client.get(SEARCH, params={"q": "applied college"}))
    assert "Blocked Applied College" not in found and "Deleted Applied College" not in found
    # Not even by their own exact name.
    assert _names(await client.get(SEARCH, params={"q": "Blocked Applied College"})) == []
    assert _names(await client.get(SEARCH, params={"q": "Deleted Applied College"})) == []


async def test_without_a_query_it_is_the_picker_s_first_screen(client, colleges):
    found = await client.get(SEARCH, params={"limit": 4})
    rows = found.json()
    assert len(rows) == 4 and all(row["is_verified"] for row in rows), rows


async def test_the_limit_is_capped(client, colleges):
    assert (await client.get(SEARCH, params={"q": "college", "limit": 26})).status_code == 422
    assert len(_names(await client.get(SEARCH, params={"q": "applied", "limit": 1}))) == 1


async def test_a_query_of_punctuation_alone_is_no_query_at_all(client, colleges):
    """Nothing is left of "()-," once punctuation is spaces, so it is the
    same as no query: the picker's first screen, not every row in the table
    and not an error."""
    punctuation = await client.get(SEARCH, params={"q": "()-,", "limit": 3})
    blank = await client.get(SEARCH, params={"limit": 3})
    assert _names(punctuation) == _names(blank) and len(_names(blank)) == 3


async def test_the_search_is_indexed_and_still_right_among_two_thousand_rows(client, session, colleges):
    """The column has its GIN trigram index, and a query over a table the
    size of the live one still answers with the one row that matches.

    What the index is for is the table growing: at 1,936 rows Postgres reads
    the table because twenty pages are cheaper than any index, and pinning
    the plan here would pin that rather than the point. The definition is
    what matters -- `gin_trgm_ops` over search_text, which is what serves
    both the substring pass and the `%>` typo pass once there are fifty
    thousand colleges, companies and communities in here.
    """
    definition = await session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_organizations_search_trgm'")
    )
    assert definition is not None, "the trigram index is missing"
    assert "gin" in definition and "search_text gin_trgm_ops" in definition, definition
    assert "NOT is_deleted" in definition, definition

    await session.execute(
        text(
            "INSERT INTO organizations (name, type, address, is_verified, is_blocked, is_deleted, created_at, updated_at) "
            "SELECT 'Government College ' || g, 'college', 'Town ' || g, false, false, false, now(), now() "
            "FROM generate_series(1, 2000) g"
        )
    )
    await session.commit()
    assert _names(await client.get(SEARCH, params={"q": "appl sci chela"})) == [
        "College of Applied Science, Chelakkara"
    ]
    # The trigger filled the new rows too, so they are searchable.
    assert _names(await client.get(SEARCH, params={"q": "government college 1999"})) == ["Government College 1999"]


# ─── Suggesting one that is missing ──────────────────────────────────────


async def test_suggesting_needs_a_signed_in_user(client):
    assert (await client.post(SUGGEST, json={"name": "New College", "type": "college"})).status_code == 401


async def test_a_suggestion_is_added_unverified_and_can_be_selected_at_once(client, session, make_user, mint_token, colleges):
    person = await make_user(email="student@example.com")
    token = await mint_token(person)
    response = await client.post(
        SUGGEST,
        json={"name": "Sahrdaya College of Engineering", "type": "college", "place": "Kodakara"},
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True and body["is_verified"] is False
    assert body["name"] == "Sahrdaya College of Engineering" and body["address"] == "Kodakara"
    # Onboarding selects it straight away, so the search has to find it now.
    assert "Sahrdaya College of Engineering" in _names(await client.get(SEARCH, params={"q": "sahrdaya"}))
    # And it is written to the audit log like the other admin-visible changes.
    from app.api.audit.models import AdminAuditLogs

    entry = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "org.suggest"))
    assert entry is not None and entry.entity_id == str(body["id"])


async def test_an_obvious_duplicate_returns_the_row_already_there(client, make_user, mint_token, colleges):
    person = await make_user(email="dupe@example.com")
    token = await mint_token(person)
    # The same name and address, spelt with different punctuation and case.
    response = await client.post(
        SUGGEST,
        json={"name": "college of applied science - chelakkara", "type": "college", "address": "chelakkara thrissur"},
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is False
    assert body["id"] == colleges["chelakkara"].id and body["is_verified"] is True


async def test_a_branch_at_another_address_is_not_a_duplicate(client, session, make_user, mint_token, colleges):
    """Five teacher education centres share one name and differ by their
    address; a sixth town is a sixth centre, not a duplicate."""
    person = await make_user(email="branch@example.com")
    token = await mint_token(person)
    response = await client.post(
        SUGGEST,
        json={"name": "Calicut University Teacher Education Centre", "type": "college", "address": "Vatakara"},
        headers=auth_header(token),
    )
    assert response.status_code == 200 and response.json()["created"] is True
    assert response.json()["id"] not in {colleges["centre_a"].id, colleges["centre_b"].id}


async def test_a_blocked_organisation_is_not_handed_back_as_a_suggestion(client, make_user, mint_token, colleges):
    person = await make_user(email="blocked@example.com")
    token = await mint_token(person)
    response = await client.post(
        SUGGEST,
        json={"name": "Blocked Applied College", "type": "college", "address": "Kannur"},
        headers=auth_header(token),
    )
    assert response.status_code == 409 and "blocked" in response.text.lower()


async def test_suggesting_is_limited_hard(client, make_user, mint_token):
    person = await make_user(email="flood@example.com")
    token = await mint_token(person)
    codes = []
    for i in range(7):
        codes.append(
            (
                await client.post(
                    SUGGEST,
                    json={"name": f"Flood College {i}", "type": "college"},
                    headers=auth_header(token),
                )
            ).status_code
        )
    assert 429 in codes, codes
    assert codes.count(200) <= 5, codes


# ─── Merging a duplicate ─────────────────────────────────────────────────


def test_every_foreign_key_to_organizations_is_known():
    """What a merge has to move. A column added later shows up here first, so
    it is thought about rather than quietly left pointing at a deleted row."""
    from app.api.orgs import service

    found = {f"{c.table.name}.{c.name}" for c in service.org_references()}
    assert found == {"clubs.org_id", "user_profiles.org_id"}, found


@pytest.fixture
async def pair(session, colleges, make_club, make_user):
    """Two rows for one college, with a club and a profile on the duplicate."""
    from app.api.users.models import UserProfiles

    keep = colleges["ihrd_brackets"]
    loser = colleges["ihrd_plain"]
    _owner, club = await make_club(name="IHRD Robotics", email="ihrd-club@example.com")
    club.org_id = loser.id
    person = await make_user(email="ihrd-student@example.com")
    profile = UserProfiles(user_id=person.id, full_name="Student", org_id=loser.id)
    session.add(profile)
    # A club an admin removed still points at the duplicate, and must move
    # too: restoring it later must not come back attached to nothing.
    _gone_owner, gone_club = await make_club(name="IHRD Old Club", email="ihrd-old@example.com")
    gone_club.org_id = loser.id
    gone_club.soft_delete()
    await session.commit()
    return dict(keep=keep, loser=loser, club=club, gone_club=gone_club, profile=profile)


async def test_a_merge_moves_every_reference_and_deletes_the_duplicate(client, session, make_admin, mint_token, pair):
    from app.api.audit.models import AdminAuditLogs

    admin = await make_admin(email="merge-admin@example.com")
    token = await mint_token(admin)
    response = await client.post(
        f"/api/v1/orgs/{pair['loser'].id}/merge",
        json={"into": pair["keep"].id},
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merged"] == pair["loser"].id and body["into"] == pair["keep"].id
    # Both the live club and the removed one, and the profile.
    assert body["moved"] == {"clubs.org_id": 2, "user_profiles.org_id": 1}

    moved = await session.execute(
        text("SELECT org_id FROM clubs WHERE id = ANY(:ids) ORDER BY id"),
        {"ids": [pair["club"].id, pair["gone_club"].id]},
    )
    assert [row[0] for row in moved.all()] == [pair["keep"].id, pair["keep"].id]
    profile_org = await session.scalar(
        text("SELECT org_id FROM user_profiles WHERE id = :id"), {"id": pair["profile"].id}
    )
    assert profile_org == pair["keep"].id

    gone = await session.scalar(
        text("SELECT is_deleted FROM organizations WHERE id = :id"), {"id": pair["loser"].id}
    )
    assert gone is True
    # And it is no longer offered.
    assert "College of Applied Science IHRD" not in _names(await client.get(SEARCH, params={"q": "ihrd"}))

    entry = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "org.merge"))
    assert entry is not None
    assert entry.entity_id == str(pair["loser"].id) and entry.details["into"] == pair["keep"].id
    assert entry.details["moved"]["clubs.org_id"] == 2
    assert "College of Applied Science IHRD" in entry.summary


async def test_a_merge_refuses_to_eat_itself(client, session, make_admin, mint_token, colleges):
    admin = await make_admin(email="self-merge@example.com")
    token = await mint_token(admin)
    org = colleges["chelakkara"]
    response = await client.post(
        f"/api/v1/orgs/{org.id}/merge", json={"into": org.id}, headers=auth_header(token)
    )
    assert response.status_code == 400 and "itself" in response.text
    still_here = await session.scalar(
        text("SELECT is_deleted FROM organizations WHERE id = :id"), {"id": org.id}
    )
    assert still_here is False


async def test_a_merge_into_a_row_that_is_not_there_changes_nothing(client, session, make_admin, mint_token, pair):
    admin = await make_admin(email="missing-merge@example.com")
    token = await mint_token(admin)
    response = await client.post(
        f"/api/v1/orgs/{pair['loser'].id}/merge", json={"into": 9_999_999}, headers=auth_header(token)
    )
    assert response.status_code == 404
    club_org = await session.scalar(
        text("SELECT org_id FROM clubs WHERE id = :id"), {"id": pair["club"].id}
    )
    assert club_org == pair["loser"].id


async def test_merging_needs_an_admin(client, make_user, mint_token, colleges):
    person = await make_user(email="not-admin@example.com")
    response = await client.post(
        f"/api/v1/orgs/{colleges['ihrd_plain'].id}/merge",
        json={"into": colleges["ihrd_brackets"].id},
        headers=auth_header(await mint_token(person)),
    )
    assert response.status_code == 403


# ─── The admin's list and its duplicates view ────────────────────────────


async def test_the_admin_list_searches_filters_and_pages(client, make_admin, mint_token, colleges):
    admin = await make_admin(email="list-admin@example.com")
    token = await mint_token(admin)

    found = await client.get(ADMIN_LIST, params={"q": "applied"}, headers=auth_header(token))
    assert found.status_code == 200, found.text
    body = found.json()
    # Blocked rows are in the admin's list -- an admin blocked them and has
    # to find them again -- and deleted ones are not.
    names = {row["name"] for row in body["items"]}
    assert "Blocked Applied College" in names and "Deleted Applied College" not in names
    assert body["total"] == len(names)
    assert {"is_verified", "is_blocked"} <= set(body["items"][0])

    by_type = await client.get(ADMIN_LIST, params={"type": "university"}, headers=auth_header(token))
    assert [row["name"] for row in by_type.json()["items"]] == ["Calicut University"]

    unverified = await client.get(ADMIN_LIST, params={"is_verified": "false"}, headers=auth_header(token))
    assert all(row["is_verified"] is False for row in unverified.json()["items"])
    assert unverified.json()["total"] == 4

    blocked = await client.get(ADMIN_LIST, params={"is_blocked": "true"}, headers=auth_header(token))
    assert [row["name"] for row in blocked.json()["items"]] == ["Blocked Applied College"]

    first = await client.get(ADMIN_LIST, params={"limit": 3, "offset": 0}, headers=auth_header(token))
    assert len(first.json()["items"]) == 3 and first.json()["next"] is not None
    second = await client.get(ADMIN_LIST, params={"limit": 3, "offset": 3}, headers=auth_header(token))
    assert not ({r["id"] for r in first.json()["items"]} & {r["id"] for r in second.json()["items"]})


async def test_the_admin_list_needs_an_admin(client, make_user, mint_token):
    assert (await client.get(ADMIN_LIST)).status_code == 401
    person = await make_user(email="list-not-admin@example.com")
    assert (await client.get(ADMIN_LIST, headers=auth_header(await mint_token(person)))).status_code == 403


async def test_the_duplicates_view_groups_by_the_normalised_name(client, session, make_admin, mint_token, colleges):
    """The punctuation variants are one group, and a group where one row has
    no address beside one that does is flagged and sorted first."""
    await _org(session, "Twice Imported College", address="Palakkad")
    await _org(session, "Twice Imported College")
    await session.commit()

    admin = await make_admin(email="dupes-admin@example.com")
    response = await client.get(DUPLICATES, headers=auth_header(await mint_token(admin)))
    assert response.status_code == 200, response.text
    groups = response.json()["items"]
    by_name = {group["name"]: group for group in groups}

    # The bracketed name and the bare one, together.
    ihrd = by_name["college of applied science ihrd"]
    assert ihrd["count"] == 2
    assert {row["name"] for row in ihrd["orgs"]} == {
        "College of Applied Science (IHRD)",
        "College of Applied Science IHRD",
    }
    # Both have an address, so this is two rows to look at, not an obvious duplicate.
    assert ihrd["needs_attention"] is False

    # The teacher education centres: one name, two towns, left alone.
    centres = by_name["calicut university teacher education centre"]
    assert centres["count"] == 2 and centres["needs_attention"] is False

    # The one that looks like a row imported twice comes first.
    assert groups[0]["name"] == "twice imported college"
    assert groups[0]["needs_attention"] is True

    # And a name that appears once is in no group.
    assert "infopark technologies" not in by_name
    assert response.json()["total"] == len(groups)


async def test_the_duplicates_view_needs_an_admin(client, make_user, mint_token):
    assert (await client.get(DUPLICATES)).status_code == 401
    person = await make_user(email="dupes-not-admin@example.com")
    assert (await client.get(DUPLICATES, headers=auth_header(await mint_token(person)))).status_code == 403


async def test_a_renamed_organisation_is_found_under_its_new_name(client, session, colleges):
    """The text is the database's, not the application's: an UPDATE through
    raw SQL rewrites it as the dashboard would."""
    await session.execute(
        text("UPDATE organizations SET name = 'Thrissur Applied Science Institute' WHERE id = :id"),
        {"id": colleges["chelakkara"].id},
    )
    await session.commit()
    # The row in memory still holds the old name; the search reads the database.
    session.expire_all()
    assert "Thrissur Applied Science Institute" in _names(
        await client.get(SEARCH, params={"q": "thrissur institute"})
    )
    assert _names(await client.get(SEARCH, params={"q": "chelakkara college applied"})) == []
