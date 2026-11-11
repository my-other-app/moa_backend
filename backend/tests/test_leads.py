"""The sales team's leads (app/api/leads): made by hand, imported, from the
For-clubs form and from a club's own sign-up; the duplicate check; who may
change one; stages; merging; follow-ups."""
import csv
import io
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.api.leads import service
from app.api.leads.models import FollowUps, LeadActivities, LeadContacts, Leads
from tests.spreadsheet_helpers import workbook_of
from tests.support_desk_helpers import (  # noqa: F401 - fixtures
    API,
    PDF,
    _reference_fresh,
    desk,
    desk_files,
    outbox,
    pushes,
)

NOW = datetime.now(timezone.utc)


def lead_body(**over):
    body = {"name": "Robotics Guild", "type": "club", "city": "Kochi",
            "contacts": [{"name": "Ananya Iyer", "email": "ananya@roboticsguild.org", "role": "Secretary"}]}
    body.update(over)
    return body


async def make(client, headers, **over):
    return await client.post(f"{API}/admin/leads", json=lead_body(**over), headers=headers)


async def lead_of(session, lead_id) -> Leads:
    return await session.scalar(select(Leads).where(Leads.id == lead_id).execution_options(populate_existing=True))



async def test_who_may_add_a_lead(client, session, desk):
    assert (await make(client, desk.h["outsider"])).status_code == 403
    assert (await make(client, desk.h["sup"])).status_code == 403
    r = await make(client, desk.h["sales"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["owner"]["id"] == desk.sales.id and out["stage"] == "new" and out["contacts_count"] == 1
    assert out["timeline"][0]["kind"] == "system"
    other = await make(client, desk.h["sales"], name="Chess Circle", owner_id=desk.sales2.id, contacts=[])
    assert other.status_code == 403 and other.json()["error_code"] == "OWNER_REQUIRED"
    by_owner = await make(client, desk.h["owner"], name="Chess Circle", owner_id=desk.sales2.id, contacts=[])
    assert by_owner.status_code == 201 and by_owner.json()["owner"]["id"] == desk.sales2.id
    bad = await make(client, desk.h["owner"], name="Drama Club", owner_id=desk.sup.id, contacts=[])
    assert bad.status_code == 422 and bad.json()["error_code"] == "NOT_IN_TEAM"


async def test_a_duplicate_is_409_with_its_matches_unless_forced(client, session, desk):
    first = (await make(client, desk.h["sales"])).json()
    again = await make(client, desk.h["sales2"], name="The Robotics Guild", contacts=[])
    assert again.status_code == 409 and again.json()["error_code"] == "DUPLICATE_LEAD"
    assert again.json()["errors"]["matches"] == [{"id": first["id"], "name": "Robotics Guild", "city": "Kochi", "stage": "new", "type": "club"}]
    # Same domain, another name.
    by_domain = await make(client, desk.h["sales2"], name="RG Kochi chapter", city="Kozhikode", website="https://roboticsguild.org", contacts=[])
    assert by_domain.status_code == 409
    # Another city is another chapter; a free mail address is nobody's domain.
    assert (await make(client, desk.h["sales2"], city="Delhi", contacts=[{"name": "x", "email": "x@gmail.com"}])).status_code == 201
    forced = await make(client, desk.h["sales2"], name="The Robotics Guild", contacts=[], force=True)
    assert forced.status_code == 201


async def test_only_the_owner_changes_a_lead_and_owners_reassign(client, session, desk):
    lead = (await make(client, desk.h["sales"])).json()
    lid = lead["id"]
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"value_inr": 50000}, headers=desk.h["sales2"])
    assert r.status_code == 403 and r.json()["error_code"] == "NOT_YOUR_LEAD"
    assert (await client.patch(f"{API}/admin/leads/{lid}", json={"value_inr": 1}, headers=desk.h["outsider"])).status_code == 403
    assert (await client.get(f"{API}/admin/leads/{lid}", headers=desk.h["outsider"])).status_code == 200
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"owner_id": desk.sales2.id}, headers=desk.h["sales"])
    assert r.status_code == 403 and r.json()["error_code"] == "OWNER_REQUIRED"
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"owner_id": desk.sales2.id}, headers=desk.h["owner"])
    assert r.status_code == 200 and r.json()["owner"]["id"] == desk.sales2.id
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"value_inr": 50000, "tags": ["Kerala", "kerala"]}, headers=desk.h["sales2"])
    assert r.status_code == 200 and r.json()["value_inr"] == 50000 and r.json()["tags"] == ["kerala"]


async def test_stages_lost_needs_a_reason_and_asks_for_the_next_step(client, session, desk):
    lid = (await make(client, desk.h["sales"])).json()["id"]
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"stage": "contacted"}, headers=desk.h["sales"])
    assert r.status_code == 200 and r.json()["needs_follow_up"] is True
    fu = await client.post(f"{API}/admin/follow-ups", json={"lead_id": lid, "kind": "meeting", "title": "Demo",
                                                            "due_at": (NOW + timedelta(days=2)).isoformat()}, headers=desk.h["sales"])
    assert fu.status_code == 201 and fu.json()["owner"]["id"] == desk.sales.id
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"stage": "meeting"}, headers=desk.h["sales"])
    assert r.json()["needs_follow_up"] is False and r.json()["next_follow_up"]["title"] == "Demo"
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"stage": "lost"}, headers=desk.h["sales"])
    assert r.status_code == 422 and r.json()["error_code"] == "LOST_REASON_REQUIRED"
    retry = date.today() + timedelta(days=120)
    r = await client.patch(f"{API}/admin/leads/{lid}", json={"stage": "lost", "lost_reason": "no_budget", "retry_on": retry.isoformat()},
                           headers=desk.h["sales"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stage"] == "lost" and out["lost_reason"] == "no_budget"
    open_ones = [f for f in out["follow_ups"] if not f["done"]]
    assert [f["title"] for f in open_ones] == ["Try Robotics Guild again"]
    assert any(t["kind"] == "stage_change" and t["details"]["to"] == "lost" for t in out["timeline"])


async def test_follow_ups_done_asks_for_the_next(client, session, desk):
    lid = (await make(client, desk.h["sales"])).json()["id"]
    fu = (await client.post(f"{API}/admin/follow-ups", json={"lead_id": lid, "kind": "call", "title": "Call",
                                                             "due_at": NOW.isoformat()}, headers=desk.h["sales"])).json()
    r = await client.patch(f"{API}/admin/follow-ups/{fu['id']}", json={"done": True}, headers=desk.h["sales2"])
    assert r.status_code == 403
    r = await client.patch(f"{API}/admin/follow-ups/{fu['id']}", json={"done": True}, headers=desk.h["sales"])
    assert r.status_code == 200 and r.json()["done"] is True and r.json()["needs_next"] is True
    r = await client.post(f"{API}/admin/follow-ups", json={"lead_id": lid, "case_id": 1, "kind": "call", "title": "x",
                                                           "due_at": NOW.isoformat()}, headers=desk.h["sales"])
    assert r.status_code == 422


async def test_contacts_activities_and_files(client, session, desk):
    lid = (await make(client, desk.h["sales"])).json()["id"]
    c = await client.post(f"{API}/admin/leads/{lid}/contacts", json={"name": "Ravi", "email": "Ravi@RoboticsGuild.org", "is_primary": True},
                          headers=desk.h["sales"])
    assert c.status_code == 201 and c.json()["email"] == "ravi@roboticsguild.org"
    contacts = (await client.get(f"{API}/admin/leads/{lid}/contacts", headers=desk.h["outsider"])).json()
    assert [x["is_primary"] for x in contacts] == [True, False]
    a = await client.post(f"{API}/admin/leads/{lid}/activities", json={"kind": "whatsapp", "body": "Sent the deck", "contact_id": c.json()["id"]},
                          headers=desk.h["sales"])
    assert a.status_code == 201 and a.json()["needs_follow_up"] is True
    f = await client.post(f"{API}/admin/leads/{lid}/files", files={"file": ("proposal.pdf", PDF, "application/pdf")}, headers=desk.h["sales"])
    assert f.status_code == 201 and f.json()["content_type"] == "application/pdf"
    got = await client.get(f.json()["url"], headers=desk.h["outsider"])
    assert got.status_code == 200 and got.content == PDF and got.headers["content-security-policy"] == "default-src 'none'; sandbox"
    gone = await client.delete(f"{API}/admin/leads/{lid}/contacts/{c.json()['id']}", headers=desk.h["sales"])
    assert gone.status_code == 200
    kinds = (await session.scalars(select(LeadActivities.kind).where(LeadActivities.lead_id == lid))).all()
    assert {"whatsapp", "file", "system"} <= set(kinds)


async def test_merge_keeps_both_timelines(client, session, desk):
    a = (await make(client, desk.h["sales"])).json()
    b = (await make(client, desk.h["sales"], name="Robotix Guild", city="Kochi", force=True,
                     contacts=[{"name": "Ananya", "email": "ananya@roboticsguild.org"}, {"name": "New", "email": "new@x.example.org"}])).json()
    await client.post(f"{API}/admin/leads/{b['id']}/activities", json={"kind": "call", "body": "Called them"}, headers=desk.h["sales"])
    r = await client.post(f"{API}/admin/leads/{b['id']}/merge", json={"into_id": a["id"]}, headers=desk.h["sales"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"] == a["id"] and any(t["body"] == "Called them" for t in out["timeline"])
    assert sorted(c["email"] for c in out["contacts"]) == ["ananya@roboticsguild.org", "new@x.example.org"]
    assert (await lead_of(session, b["id"])).merged_into_id == a["id"]
    listed = (await client.get(f"{API}/admin/leads", headers=desk.h["sales"])).json()
    assert [i["id"] for i in listed["items"]] == [a["id"]]


async def test_the_board_the_table_filters_and_excel(client, session, desk):
    a = (await make(client, desk.h["sales"])).json()
    b = (await make(client, desk.h["sales2"], name="Acme Foods", type="sponsor", city="Bengaluru", contacts=[])).json()
    await client.patch(f"{API}/admin/leads/{b['id']}", json={"stage": "proposal"}, headers=desk.h["sales2"])
    await client.post(f"{API}/admin/follow-ups", json={"lead_id": a["id"], "kind": "call", "title": "Late call",
                                                       "due_at": (NOW - timedelta(hours=30)).isoformat()}, headers=desk.h["sales"])
    board = (await client.get(f"{API}/admin/leads?view=board", headers=desk.h["outsider"])).json()
    assert board["by_stage"]["new"] == 1 and board["by_stage"]["proposal"] == 1 and board["total"] == 2
    assert (await client.get(f"{API}/admin/leads?type=sponsor", headers=desk.h["sales"])).json()["total"] == 1
    assert (await client.get(f"{API}/admin/leads?owner=me", headers=desk.h["sales"])).json()["total"] == 1
    overdue = (await client.get(f"{API}/admin/leads?due=overdue", headers=desk.h["sales"])).json()
    assert [i["id"] for i in overdue["items"]] == [a["id"]] and overdue["items"][0]["overdue"] is True
    assert (await client.get(f"{API}/admin/leads?q=ananya", headers=desk.h["sales"])).json()["total"] == 1
    assert (await client.get(f"{API}/admin/leads?format=xlsx", headers=desk.h["outsider"])).status_code == 403
    sheet = workbook_of(await client.get(f"{API}/admin/leads?format=xlsx", headers=desk.h["sales"])).active
    assert sheet["B1"].value == "Name" and sheet.max_row == 3


def _csv(rows) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    for r in rows:
        w.writerow(r)
    return out.getvalue().encode()


async def test_import_dry_run_changes_nothing_then_adds_the_new_rows(client, session, desk):
    await make(client, desk.h["sales"])
    data = _csv([
        ["name", "type", "city", "contact_email", "owner_email", "value_inr"],
        ["Robotics Guild", "club", "Kochi", "", "", ""],               # duplicate of the lead we have
        ["Acme Foods", "sponsor", "Bengaluru", "a@acme.example.org", "", "25000"],
        ["Acme Foods Pvt Ltd", "sponsor", "Bengaluru", "", "", ""],    # duplicate of the row above
        ["", "club", "", "", "", ""],                                   # no name
        ["Hall One", "venue", "Kochi", "", desk.sales2.email, "x"],     # bad value
        ["Hall Two", "venue", "Kochi", "", "nobody@example.org", ""],  # not a sales member
    ])
    r = await client.post(f"{API}/admin/leads/import", files={"file": ("leads.csv", data, "text/csv")}, data={"dry_run": "true"},
                          headers=desk.h["sales"])
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [x["status"] for x in rows] == ["duplicate", "new", "duplicate", "error", "error", "error"]
    assert rows[0]["match_id"] and "row 3" in rows[2]["error"] and r.json()["counts"] == {"new": 1, "duplicate": 2, "error": 3}
    assert await session.scalar(select(func.count()).select_from(Leads)) == 1
    r = await client.post(f"{API}/admin/leads/import", files={"file": ("leads.csv", data, "text/csv")}, data={"dry_run": "false"},
                          headers=desk.h["sales"])
    assert r.json()["created"] == 1
    made = await session.scalar(select(Leads).where(Leads.name == "Acme Foods"))
    assert made.source == "import" and made.value_inr == 25000 and made.owner_id == desk.sales.id and made.email_domain == "acme.example.org"
    assert (await client.post(f"{API}/admin/leads/import", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
                              headers=desk.h["sales"])).status_code == 422
    assert (await client.post(f"{API}/admin/leads/import", files={"file": ("leads.csv", data, "text/csv")},
                              headers=desk.h["sup"])).status_code == 403
    template = workbook_of(await client.get(f"{API}/admin/leads/import/template", headers=desk.h["sales"]), "leads-import-template.xlsx")
    assert [c.value for c in template["Leads"][1]][:2] == ["name", "type"] and template["Columns"]["A2"].value == "name"


async def test_talk_to_us_opens_a_lead_for_the_round_robin_then_attaches(client, session, desk):
    body = {"name": "Meera", "email": "meera@techclub.example.org", "organisation": "Tech Club, CET", "city": "Thiruvananthapuram",
            "type": "club", "message": "We'd like the Pro plan", "page_url": "https://myotherapp.in/for-clubs"}
    r = await client.post(f"{API}/leads/talk-to-us", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True}
    lead = await session.scalar(select(Leads).where(Leads.name == "Tech Club, CET"))
    assert lead.source == "for_clubs" and lead.owner_id == desk.sales.id
    fu = (await session.scalars(select(FollowUps).where(FollowUps.lead_id == lead.id))).all()
    assert len(fu) == 1 and fu[0].owner_id == desk.sales.id and fu[0].kind == "email"
    again = await client.post(f"{API}/leads/talk-to-us", json={**body, "name": "Rahul", "email": "rahul@techclub.example.org",
                                                               "organisation": "Tech Club CET"})
    assert again.status_code == 200
    assert await session.scalar(select(func.count()).select_from(Leads)) == 1
    emails = set((await session.scalars(select(LeadContacts.email).where(LeadContacts.lead_id == lead.id))).all())
    assert emails == {"meera@techclub.example.org", "rahul@techclub.example.org"}
    for n in range(3):
        await client.post(f"{API}/leads/talk-to-us", json={**body, "organisation": f"Club {n}", "email": f"x{n}@example.org"})
    assert (await client.post(f"{API}/leads/talk-to-us", json=body)).status_code == 429


async def test_a_club_that_applies_by_itself_is_a_club_lead(client, session, desk):
    r = await client.post(f"{API}/clubs/create", data={"name": "Quiz Society", "email": "quiz-lead@example.com", "password": "Password123!",
                                                       "contact_email": "hi@quizsociety.example.org", "location_name": "Kochi"})
    assert r.status_code == 200, r.text
    lead = await session.scalar(select(Leads).where(Leads.source == "signup"))
    assert lead is not None and lead.type == "club" and lead.name == "Quiz Society" and lead.club_id == r.json()["id"]
    assert lead.owner_id in (desk.sales.id, desk.sales2.id) and lead.email_domain == "quizsociety.example.org"
    assert await session.scalar(select(func.count()).select_from(FollowUps).where(FollowUps.lead_id == lead.id)) == 1


# ─── The keys (last: a first test without a session leaves the suite's
# engine on another event loop when this file runs alone) ────────────────


def test_the_duplicate_keys():
    assert service.key_of("The Robotics Guild Pvt. Ltd.") == service.key_of("robotics guild")
    assert service.domain_of("ananya@RoboticsGuild.org") == "roboticsguild.org"
    assert service.domain_of("https://www.roboticsguild.org/about") == "roboticsguild.org"
    assert service.domain_of("someone@gmail.com") is None
