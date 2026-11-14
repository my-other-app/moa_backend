"""Profiles in the admin portal: profile reports in the queue (POST /reports
with target_type "profile"), hide profile, the account suspension, removing
a link or a project, unhide -- each in the audit log -- and the Networking
summary."""
import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.profiles.models import ProfileLinks, ProfileProjects
from app.api.users.models import AdminRoles
from app.core.rate_limit import limiter
from tests.conftest import auth_header
from tests.profiles_world import API, person, save


@pytest.fixture
async def world(client, session, make_user, make_admin, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    owner, oh = await person(session, make_user, mint_token, "asha", "Asha Nair", headline="Builds robots")
    fan, fh = await person(session, make_user, mint_token, "fan", "Fan Person")
    support = await make_admin(email="support-prof@example.com", role=AdminRoles.support)
    viewer = await make_admin(email="viewer-prof@example.com", role=AdminRoles.viewer)
    await session.commit()
    await client.put(f"{API}/me/profile/links", json={"links": [{"type": "x", "url": "https://x.com/asha", "label": "Spam"}]}, headers=oh)
    project = (await client.post(f"{API}/me/profile/projects", json={"title": "Bad project", "url": "https://bad.example.com"}, headers=oh)).json()
    code = (await client.get(f"{API}/me/qr", headers=oh)).json()["code"]
    return dict(
        owner=owner, oh=oh, fan=fan, fh=fh, project=project, code=code,
        admin_h=auth_header(await mint_token(support)), viewer_h=auth_header(await mint_token(viewer)),
    )


async def _report(client, headers, target_id, reason="spam"):
    return await client.post(f"{API}/reports", json={"target_type": "profile", "target_id": target_id, "reason": reason}, headers=headers)


async def _queue(client, world):
    r = await client.get(f"{API}/admin/reports", params={"target_type": "profile"}, headers=world["admin_h"])
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _audit(session, action):
    return list((await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == action))).all())


async def test_a_profile_is_reported_by_its_owners_id_in_digits(client, world):
    uid = world["owner"].id
    first = await _report(client, world["fh"], str(uid))
    assert first.status_code == 201, first.text
    assert (await _report(client, world["fh"], uid)).status_code == 200  # the same open report, as a number too
    me = await _report(client, world["oh"], str(uid))
    assert (me.status_code, me.json()["error_code"]) == (422, "CANNOT_REPORT_SELF")
    assert (await _report(client, world["fh"], "999999")).json()["error_code"] == "TARGET_NOT_FOUND"
    assert (await _report(client, world["fh"], "asha")).json()["error_code"] == "TARGET_NOT_FOUND"

    [item] = await _queue(client, world)
    assert item["target_type"] == "profile" and item["target_id"] == str(uid)
    assert item["target_preview"] == "Asha Nair (@asha) — Builds robots"
    assert item["target_author"]["id"] == uid and item["target_removed"] is False and item["club"] is None
    link = item["profile"]["links"][0]
    assert item["profile"]["user_id"] == uid and item["profile"]["handle"] == "asha" and item["profile"]["suspended"] is False
    assert {k: link[k] for k in ("type", "url", "label")} == {"type": "x", "url": "https://x.com/asha", "label": "Spam"} and link["id"]
    assert item["profile"]["projects"] == [{"id": world["project"]["id"], "title": "Bad project", "url": "https://bad.example.com"}]
    # Other reports carry no profile block.
    others = (await client.get(f"{API}/admin/reports", headers=world["admin_h"])).json()["items"]
    assert all(("profile" not in i) or i["profile"] is None or i["target_type"] == "profile" for i in others)


async def test_hide_profile_takes_the_profile_its_vcard_and_its_code_down(client, session, world):
    uid = world["owner"].id
    rid = (await _report(client, world["fh"], str(uid))).json()["id"]
    r = await client.post(f"{API}/admin/reports/{rid}/resolve", json={"action": "remove_content", "note": "Spam links"}, headers=world["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["report"]["target_removed"] is True and r.json()["report"]["profile"]["suspended"] is True
    for path in (f"{API}/profiles/asha", f"{API}/profiles/asha/vcard", f"{API}/profiles/by-code/{world['code']}"):
        assert (await client.get(path)).status_code == 404, path
    assert (await save(client, world["fh"], handle="asha")).status_code == 404
    assert (await _audit(session, "report.resolve"))[-1].entity_type == "profile"
    # The account still works: the owner reads their own profile.
    assert (await client.get(f"{API}/me/profile", headers=world["oh"])).status_code == 200
    # A hidden profile cannot be reported again.
    assert (await _report(client, world["fh"], str(uid))).status_code == 404

    assert (await client.post(f"{API}/admin/profiles/{uid}/unhide", headers=world["viewer_h"])).status_code == 403
    assert (await client.post(f"{API}/admin/profiles/{uid}/unhide", headers=world["admin_h"])).status_code == 204
    assert (await client.get(f"{API}/profiles/asha")).status_code == 200
    assert (await client.get(f"{API}/profiles/by-code/{world['code']}")).status_code == 200
    [entry] = await _audit(session, "profile.unhide")
    assert entry.entity_type == "user" and entry.entity_id == str(uid)
    assert (await client.post(f"{API}/admin/profiles/999999/unhide", headers=world["admin_h"])).status_code == 404


async def test_dismiss_leaves_it_and_suspend_author_suspends_the_account(client, session, world):
    uid = world["owner"].id
    rid = (await _report(client, world["fh"], str(uid))).json()["id"]
    assert (await client.post(f"{API}/admin/reports/{rid}/resolve", json={"action": "dismiss"}, headers=world["admin_h"])).status_code == 200
    assert (await client.get(f"{API}/profiles/asha")).status_code == 200
    rid = (await _report(client, world["fh"], str(uid), reason="harassment")).json()["id"]
    r = await client.post(f"{API}/admin/reports/{rid}/resolve", json={"action": "suspend_author"}, headers=world["admin_h"])
    assert r.status_code == 200, r.text
    await session.refresh(world["owner"])
    assert world["owner"].is_active is False
    assert (await client.get(f"{API}/profiles/asha")).status_code == 404


async def test_an_admin_removes_one_link_or_project(client, session, world):
    uid = world["owner"].id
    link_id = await session.scalar(select(ProfileLinks.id).where(ProfileLinks.user_id == uid))
    pid = world["project"]["id"]
    assert (await client.delete(f"{API}/admin/profiles/{uid}/links/{link_id}", headers=world["viewer_h"])).status_code == 403
    assert (await client.delete(f"{API}/admin/profiles/{world['fan'].id}/links/{link_id}", headers=world["admin_h"])).status_code == 404
    assert (await client.delete(f"{API}/admin/profiles/{uid}/links/{link_id}", headers=world["admin_h"])).status_code == 204
    assert (await client.delete(f"{API}/admin/profiles/{uid}/links/{link_id}", headers=world["admin_h"])).status_code == 404
    assert (await client.delete(f"{API}/admin/profiles/{uid}/projects/{pid}", headers=world["admin_h"])).status_code == 204
    assert await session.scalar(select(ProfileProjects.id).where(ProfileProjects.id == pid)) is None
    body = (await client.get(f"{API}/profiles/asha")).json()
    assert body["links"] == [] and body["projects"] == []
    [link_log] = await _audit(session, "profile.link_remove")
    assert link_log.details["url"] == "https://x.com/asha" and link_log.entity_id == str(uid)
    [project_log] = await _audit(session, "profile.project_remove")
    assert project_log.details["title"] == "Bad project"


async def test_the_networking_summary_is_counts(client, session, world):
    await save(client, world["fh"], handle="asha")
    await save(client, world["oh"], handle="fan")
    await _report(client, world["fh"], str(world["owner"].id))
    r = await client.get(f"{API}/admin/networking/summary", headers=world["viewer_h"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profiles_total"] == 2 and body["profiles_completed"] == 0  # no photo yet
    assert body["codes_active"] == 1 and body["saves_7d"] == 2 and body["saves_30d"] == 2
    assert body["mutual_rate"] == 100.0 and body["reports_open"] == 1
    assert (await client.get(f"{API}/admin/networking/summary", headers=world["oh"])).status_code == 403
