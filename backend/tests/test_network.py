"""The Network (feature-register/profiles-networking-contract.md): saving by
code or handle, mutual both ways round and on removal, encounters and the
event rule, the 72-hour offline met_at, rate limits, the lists, export,
the save notices' batching, the morning recap, the club's stats and People
Like You's new fields."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.api.profiles import limits, notices, recap
from app.api.profiles.models import NetworkEncounters, NetworkPeople, NetworkRecaps, NetworkSaveNotices
from app.api.reports.models import UserBlocks
from app.core import jobs
from app.core.jobs import Jobs
from tests.conftest import auth_header
from tests.profiles_world import API, NOW, live_event, person, profile_row, row, save


@pytest.fixture
async def world(client, session, make_user, mint_token, monkeypatch):
    pushes: list[dict] = []

    async def fake_push(session_, user_id, title, body, data=None):
        pushes.append({"user_id": user_id, "title": title, "body": body, "data": data})
        return True

    from app.core.notifications import service as push

    monkeypatch.setattr(push, "send_notification_to_user", fake_push)
    a, ah = await person(session, make_user, mint_token, "ananya", "Ananya Iyer", headline="ML at Acme")
    b, bh = await person(session, make_user, mint_token, "bala", "Bala Krishnan", headline="Designer")
    c, ch = await person(session, make_user, mint_token, "chitra", "Chitra Menon")
    await session.commit()
    return dict(a=a, ah=ah, b=b, bh=bh, c=c, ch=ch, pushes=pushes, session=session)


async def _encounters(session, owner, person_):
    r = await row(session, owner, person_)
    return list((await session.scalars(select(NetworkEncounters).where(NetworkEncounters.network_person_id == r.id).order_by(NetworkEncounters.met_at))).all())


# ─── Saving ─────────────────────────────────────────────────────────────


async def test_saving_by_code_and_by_handle(client, session, world):
    code = (await client.get(f"{API}/me/qr", headers=world["bh"])).json()["code"]
    r = await save(client, world["ah"], code=code, method="qr")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["new_encounter"] is True
    p = body["person"]
    assert p["person"] == {"user_id": world["b"].id, "handle": "bala", "full_name": "Bala Krishnan", "picture": None, "headline": "Designer", "org_name": None}
    assert p["mutual"] is False and p["saved_me"] is False and p["tags"] == [] and len(p["encounters"]) == 1
    assert p["encounters"][0]["event"] is None and p["encounters"][0]["method"] == "qr"
    assert body["profile"]["handle"] == "bala" and body["profile"]["relation"]["saved_by_me"] is True
    # Again: the same person, no new row, and no second encounter the same day.
    again = (await save(client, world["ah"], handle="BALA")).json()
    assert again["created"] is False and again["new_encounter"] is False and again["person"]["id"] == p["id"]

    for body_, status, code_ in (
        ({"method": "qr"}, 422, None),
        ({"method": "qr", "code": code, "handle": "bala"}, 422, None),
        ({"method": "wave", "handle": "bala"}, 422, None),
        ({"method": "search", "handle": "ananya"}, 422, "CANNOT_SAVE_SELF"),
        ({"method": "search", "handle": "nobody-at-all"}, 404, "NOT_FOUND"),
        ({"method": "qr", "code": "abcdefghjk"}, 404, "NOT_FOUND"),
    ):
        r = await client.post(f"{API}/network", json=body_, headers=world["ah"])
        assert r.status_code == status, (body_, r.text)
        if code_:
            assert r.json()["error_code"] == code_
    mine = (await client.get(f"{API}/me/qr", headers=world["ah"])).json()["code"]
    assert (await save(client, world["ah"], code=mine, method="qr")).json()["error_code"] == "CANNOT_SAVE_SELF"


async def test_replaced_codes_scanning_off_blocks_and_suspensions_refuse_a_save(client, session, world):
    old = (await client.get(f"{API}/me/qr", headers=world["bh"])).json()["code"]
    await client.post(f"{API}/me/qr/replace", headers=world["bh"])
    r = await save(client, world["ah"], code=old, method="qr")
    assert (r.status_code, r.json()["error_code"]) == (410, "PROFILE_CODE_INACTIVE")
    await client.patch(f"{API}/me/profile", json={"scanning_enabled": False}, headers=world["bh"])
    new = (await client.get(f"{API}/me/qr", headers=world["bh"])).json()["code"]
    r = await save(client, world["ah"], code=new, method="qr")
    assert (r.status_code, r.json()["error_code"]) == (403, "PROFILE_SCANNING_OFF")
    r = await save(client, world["ah"], handle="bala", method="qr")
    assert (r.status_code, r.json()["error_code"]) == (403, "PROFILE_SCANNING_OFF")
    # Scanning is QR saves: a search save still goes.
    assert (await save(client, world["ah"], handle="bala", method="search")).status_code == 200

    session.add(UserBlocks(blocker_id=world["c"].id, blocked_id=world["a"].id))
    await session.commit()
    assert (await save(client, world["ah"], handle="chitra")).status_code == 404
    assert (await save(client, world["ch"], handle="ananya")).status_code == 404
    p = await profile_row(session, world["b"])
    p.suspended_at = datetime.now(timezone.utc)
    await session.commit()
    assert (await save(client, world["ch"], handle="bala")).status_code == 404


async def test_mutual_either_way_round_and_removal_turns_it_off(client, session, world):
    a, b, c = world["a"], world["b"], world["c"]
    await save(client, world["ah"], handle="bala")
    second = (await save(client, world["bh"], handle="ananya")).json()
    assert second["person"]["mutual"] is True and second["person"]["saved_me"] is True and second["profile"]["relation"]["mutual"] is True
    assert (await row(session, a, b)).mutual and (await row(session, b, a)).mutual
    # The other order: C first saves A, A saves C.
    await save(client, world["ch"], handle="ananya")
    assert (await row(session, c, a)).mutual is False
    await save(client, world["ah"], handle="chitra")
    assert (await row(session, c, a)).mutual and (await row(session, a, c)).mutual

    # Removing is silent and turns the other side's mutual false.
    world["pushes"].clear()
    np_id = (await row(session, a, b)).id
    assert (await client.delete(f"{API}/network/{np_id}", headers=world["ah"])).status_code == 204
    assert await row(session, a, b) is None and (await row(session, b, a)).mutual is False
    assert world["pushes"] == []
    # Not mine, or gone: 404.
    assert (await client.delete(f"{API}/network/{np_id}", headers=world["ah"])).status_code == 404
    other = (await row(session, b, a)).id
    assert (await client.delete(f"{API}/network/{other}", headers=world["ah"])).status_code == 404
    assert (await client.get(f"{API}/network/{other}", headers=world["ah"])).status_code == 404
    assert (await client.patch(f"{API}/network/{other}", json={"note": "x"}, headers=world["ah"])).status_code == 404


async def test_an_encounter_names_the_event_only_when_both_are_checked_in_inside_its_window(client, session, world, make_club, make_event, make_registration):
    a, b, c = world["a"], world["b"], world["c"]
    _, club, event = await live_event(make_club, make_event, make_registration, a, b)
    await make_registration(event=event, user=c, is_attended=False)
    later = await make_event(club=club, created_by=club.user, name="Next Week", event_datetime=NOW + timedelta(days=7), duration=2.0)
    for g in (a, b):
        await make_registration(event=later, user=g, is_attended=True)
    await session.commit()

    r = (await save(client, world["ah"], handle="bala", event_id=event.id, method="qr")).json()
    e = r["person"]["encounters"][0]["event"]
    assert e == {"id": event.id, "slug": event.slug, "name": "DevFest", "starts_at": e["starts_at"]}
    # The same event again: the same encounter.
    assert (await save(client, world["ah"], handle="bala", event_id=event.id)).json()["new_encounter"] is False
    # C registered but never came in: no event.
    r = (await save(client, world["ah"], handle="chitra", event_id=event.id)).json()
    assert r["person"]["encounters"][0]["event"] is None
    # Both checked in, but now is outside that event's window: no event.
    r = (await save(client, world["bh"], handle="ananya", event_id=later.id)).json()
    assert r["person"]["encounters"][0]["event"] is None
    # An event that does not exist, or one neither is at: no event, no error.
    assert (await save(client, world["ch"], handle="bala", event_id=999999)).json()["person"]["encounters"][0]["event"] is None
    # Before the start (an offline scan from the morning): no event.
    assert (await save(client, world["ch"], handle="ananya", event_id=event.id, met_at=(NOW - timedelta(hours=3)).isoformat())).status_code == 200
    assert (await _encounters(session, world["c"], world["a"]))[0].event_id is None


async def test_an_offline_scan_keeps_its_time_up_to_72_hours(client, session, world, make_club, make_event, make_registration):
    a, b, c = world["a"], world["b"], world["c"]
    two_hours = NOW - timedelta(hours=2)
    _, _, event = await live_event(make_club, make_event, make_registration, a, b, starts=NOW - timedelta(hours=3), duration=2.0)
    await session.commit()
    r = (await save(client, world["ah"], handle="bala", event_id=event.id, met_at=two_hours.isoformat())).json()
    enc = r["person"]["encounters"][0]
    assert enc["event"]["id"] == event.id  # the scan's time is in the window, though now is not
    assert abs(datetime.fromisoformat(enc["met_at"]) - two_hours) < timedelta(seconds=1)

    seventy_one = NOW - timedelta(hours=71)
    await save(client, world["ah"], handle="chitra", met_at=seventy_one.isoformat())
    assert abs((await _encounters(session, a, c))[0].met_at - seventy_one) < timedelta(seconds=1)
    before = datetime.now(timezone.utc)
    await save(client, world["bh"], handle="chitra", met_at=(NOW - timedelta(hours=73)).isoformat())
    assert (await _encounters(session, b, c))[0].met_at >= before - timedelta(seconds=1)
    # A time in the future is now, too.
    await save(client, world["ch"], handle="bala", met_at=(NOW + timedelta(days=1)).isoformat())
    assert (await _encounters(session, c, b))[0].met_at <= datetime.now(timezone.utc)
    # Without an event, one encounter a day: yesterday's scan and today's are two.
    r = await save(client, world["ah"], handle="chitra")
    assert r.json()["new_encounter"] is True and len(await _encounters(session, a, c)) == 2


async def test_saves_are_rate_limited_per_person(client, world, monkeypatch):
    assert limits.SAVES == ("60/minute", "300/hour")
    monkeypatch.setattr(limits, "SAVES", ("3/minute", "300/hour"))
    for _ in range(3):
        assert (await save(client, world["ah"], handle="bala")).status_code == 200
    r = await save(client, world["ah"], handle="bala")
    assert (r.status_code, r.json()["error_code"]) == (429, "RATE_LIMITED") and r.json()["errors"]["retry_after"] >= 1
    # Every try counts, a refused one too; and it is per person.
    assert (await save(client, world["bh"], handle="ananya")).status_code == 200


# ─── Reading the network ────────────────────────────────────────────────


async def test_the_list_filters_sorts_pages_and_counts(client, session, world, make_user, mint_token, make_club, make_event, make_registration):
    a, ah = world["a"], world["ah"]
    _, _, event = await live_event(make_club, make_event, make_registration, a, world["b"])
    dee, dh = await person(session, make_user, mint_token, "deepa", "Deepa Rao", headline="Rust person")
    await session.commit()
    await save(client, ah, handle="bala", event_id=event.id, method="qr")
    await save(client, ah, handle="chitra")
    await save(client, ah, handle="deepa")
    await save(client, dh, handle="ananya")  # mutual with Deepa
    c_id = (await row(session, a, world["c"])).id
    r = await client.patch(f"{API}/network/{c_id}", json={"note": "Met at the\nbadge desk", "tags": ["Hiring", "hiring", " python "]}, headers=ah)
    assert r.status_code == 200 and r.json()["tags"] == ["Hiring", "python"] and r.json()["note"] == "Met at the\nbadge desk"
    assert (await client.patch(f"{API}/network/{c_id}", json={"tags": [f"t{i}" for i in range(11)]}, headers=ah)).status_code == 422
    assert (await client.patch(f"{API}/network/{c_id}", json={"note": "x" * 501}, headers=ah)).status_code == 422

    async def names(**params):
        r = await client.get(f"{API}/network", params=params, headers=ah)
        assert r.status_code == 200, r.text
        return [i["person"]["full_name"] for i in r.json()["items"]], r.json()

    got, body = await names()
    assert got == ["Deepa Rao", "Chitra Menon", "Bala Krishnan"] and body["total"] == 3
    assert body["events"] == [{"event": {"id": event.id, "slug": event.slug, "name": "DevFest", "starts_at": body["events"][0]["event"]["starts_at"]}, "count": 1}]
    assert sorted((t["tag"], t["count"]) for t in body["tags"]) == [("Hiring", 1), ("python", 1)]
    assert (await names(sort="name"))[0] == ["Bala Krishnan", "Chitra Menon", "Deepa Rao"]
    assert (await names(sort="name", order="desc"))[0] == ["Deepa Rao", "Chitra Menon", "Bala Krishnan"]
    assert (await names(sort="met", order="asc"))[0][0] == "Bala Krishnan"
    assert (await names(event_id=event.id))[0] == ["Bala Krishnan"]
    assert (await names(tag="Hiring"))[0] == ["Chitra Menon"]
    assert (await names(mutual="true"))[0] == ["Deepa Rao"]
    assert (await names(q="rust"))[0] == ["Deepa Rao"]  # the headline
    assert (await names(q="badge"))[0] == ["Chitra Menon"]  # my note
    assert (await names(q="%"))[0] == []  # taken literally
    got, body = await names(page=2, page_size=2)
    assert got == ["Bala Krishnan"] and body["total"] == 3
    assert (await client.get(f"{API}/network", params={"page_size": 101}, headers=ah)).status_code == 422
    assert (await client.get(f"{API}/network", params={"sort": "age"}, headers=ah)).status_code == 422

    # Blocked, suspended or hidden people leave the list (and their detail), and come back.
    session.add(UserBlocks(blocker_id=dee.id, blocked_id=a.id))
    await session.commit()
    assert "Deepa Rao" not in (await names())[0]
    d_id = (await row(session, a, dee)).id
    assert (await client.get(f"{API}/network/{d_id}", headers=ah)).status_code == 404
    await session.execute(UserBlocks.__table__.delete())
    world["b"].is_active = False
    await session.commit()
    assert (await names())[0] == ["Deepa Rao", "Chitra Menon"]
    world["b"].is_active = True
    await session.commit()
    detail = (await client.get(f"{API}/network/{(await row(session, a, world['b'])).id}", headers=ah)).json()
    assert detail["encounters"][0]["event"]["id"] == event.id and detail["first_met_at"] == detail["last_met_at"]


async def test_the_list_shows_three_encounters_and_the_detail_all(client, session, world):
    a, ah = world["a"], world["ah"]
    for days in (60, 50, 40, 30):
        await save(client, ah, handle="bala")
        r = await row(session, a, world["b"])
        await session.execute(
            NetworkEncounters.__table__.update().where(NetworkEncounters.network_person_id == r.id, NetworkEncounters.met_at > NOW - timedelta(days=1))
            .values(met_at=NOW - timedelta(days=days))
        )
        await session.commit()
    item = (await client.get(f"{API}/network", headers=ah)).json()["items"][0]
    assert len(item["encounters"]) == 3
    detail = (await client.get(f"{API}/network/{item['id']}", headers=ah)).json()
    assert len(detail["encounters"]) == 4
    assert datetime.fromisoformat(detail["first_met_at"]) < datetime.fromisoformat(detail["last_met_at"])


async def test_saved_me_lists_who_saved_me_that_i_have_not_saved(client, session, world, make_club, make_event, make_registration):
    a, b, c = world["a"], world["b"], world["c"]
    _, _, event = await live_event(make_club, make_event, make_registration, a, b)
    await session.commit()
    await save(client, world["bh"], handle="ananya", event_id=event.id)
    await save(client, world["ch"], handle="ananya")
    r = (await client.get(f"{API}/network/saved-me", headers=world["ah"])).json()
    assert [i["person"]["handle"] for i in r["items"]] == ["chitra", "bala"] and r["total"] == 2
    assert r["items"][1]["event"]["id"] == event.id and r["items"][0]["event"] is None
    assert [i["person"]["handle"] for i in (await client.get(f"{API}/network/saved-me", params={"event_id": event.id}, headers=world["ah"])).json()["items"]] == ["bala"]
    # Saved back: gone from the inbox.
    await save(client, world["ah"], handle="bala")
    assert [i["person"]["handle"] for i in (await client.get(f"{API}/network/saved-me", headers=world["ah"])).json()["items"]] == ["chitra"]
    session.add(UserBlocks(blocker_id=a.id, blocked_id=c.id))
    await session.commit()
    assert (await client.get(f"{API}/network/saved-me", headers=world["ah"])).json() == {"items": [], "total": 0}


async def test_the_export_holds_only_what_each_person_shares_and_never_a_formula(client, session, world):
    a, ah = world["a"], world["ah"]
    world["b"].phone = "+919800000002"
    await session.commit()
    await client.patch(f"{API}/me/profile", json={"share_email": "met", "share_phone": "mutual", "headline": "=HYPERLINK(\"http://evil\")"}, headers=world["bh"])
    await client.put(f"{API}/me/profile/links", json={"links": [{"type": "github", "url": "https://github.com/bala"}]}, headers=world["bh"])
    await client.patch(f"{API}/me/profile", json={"is_private": True, "share_email": "met"}, headers=world["ch"])
    await save(client, ah, handle="bala")
    await save(client, ah, handle="chitra")
    c_id = (await row(session, a, world["c"])).id
    await client.patch(f"{API}/network/{c_id}", json={"note": "+1 for the talk", "tags": ["@lead", "-x"]}, headers=ah)

    r = await client.get(f"{API}/network/export", params={"format": "csv"}, headers=ah)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="network.csv"'
    import csv
    import io

    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Name", "Headline", "Organisation", "Profile URL", "Links", "Email", "Phone", "Met at", "Events", "Note", "Tags"]
    by_name = {r_[0]: r_ for r_ in rows[1:]}
    bala, chitra = by_name["Bala Krishnan"], by_name["Chitra Menon"]
    assert bala[1] == "'=HYPERLINK(\"http://evil\")" and bala[3] == "https://myotherapp.in/u/bala" and bala[4] == "https://github.com/bala"
    assert bala[5] == "bala@example.com" and bala[6] == ""  # the number is for the mutual only
    assert chitra[5] == "" and chitra[4] == ""  # private, and Chitra has not saved Ananya
    assert chitra[9] == "'+1 for the talk" and chitra[10] == "'@lead, -x"
    for cell in (c for r_ in rows for c in r_):
        assert not cell.startswith(("=", "+", "-", "@", "\t", "\r"))

    v = await client.get(f"{API}/network/export", headers=ah)
    assert v.status_code == 200 and v.headers["content-type"].startswith("text/vcard")
    cards = v.text.split("END:VCARD\r\n")
    assert v.text.count("BEGIN:VCARD") == 2
    bala_card = next(card for card in cards if "FN:Bala Krishnan" in card)
    assert "EMAIL;TYPE=INTERNET:bala@example.com" in bala_card and "TEL" not in bala_card and "URL:https://github.com/bala" in bala_card
    chitra_card = next(card for card in cards if "FN:Chitra Menon" in card)
    assert "EMAIL" not in chitra_card and "CATEGORIES:@lead\\,-x" not in chitra_card and "CATEGORIES:@lead,-x" in chitra_card
    assert "NOTE:+1 for the talk\\nMet " in chitra_card
    # Mutual now: the number too.
    await save(client, world["bh"], handle="ananya")
    v = await client.get(f"{API}/network/export", headers=ah)
    assert "TEL;TYPE=CELL:+919800000002" in v.text
    assert "+919800000002" in (await client.get(f"{API}/network/export", params={"format": "csv"}, headers=ah)).text
    assert (await client.get(f"{API}/network/export", params={"format": "xls"}, headers=ah)).status_code == 422


# ─── Save notices ───────────────────────────────────────────────────────


async def test_saves_are_batched_into_one_notice_sent_once(client, session, world, make_user, mint_token, make_club, make_event, make_registration):
    a, b = world["a"], world["b"]
    d, dh = await person(session, make_user, mint_token, "deepa", "Deepa Rao")
    e, eh = await person(session, make_user, mint_token, "esha", "Esha Paul")
    _, _, event = await live_event(make_club, make_event, make_registration, a, b, d, e)
    await session.commit()
    for h in (world["bh"], dh, eh):
        await save(client, h, handle="ananya", event_id=event.id)
    await save(client, world["bh"], handle="ananya", event_id=event.id)  # a second save is no second saver
    await save(client, world["ch"], handle="ananya")  # no event: its own batch

    rows = list((await session.scalars(select(NetworkSaveNotices).where(NetworkSaveNotices.person_id == a.id).order_by(NetworkSaveNotices.id))).all())
    assert [(r.event_id, r.saver_ids) for r in rows] == [(event.id, [b.id, d.id, e.id]), (None, [world["c"].id])]
    due = rows[0].due_at - rows[0].created_at
    assert timedelta(minutes=9) < due <= timedelta(minutes=10, seconds=5)
    queued = (await session.scalars(select(Jobs).where(Jobs.name == notices.NOTICE_JOB))).all()
    assert sorted(j.dedupe_key for j in queued) == sorted(f"{notices.NOTICE_JOB}:{r.id}" for r in rows)
    assert world["pushes"] == []  # nothing before the ten minutes

    # Deepa removed her save; Esha was blocked by Ananya since: neither is named.
    await client.delete(f"{API}/network/{(await row(session, d, a)).id}", headers=dh)
    session.add(UserBlocks(blocker_id=a.id, blocked_id=e.id))
    await session.commit()
    assert await notices.send(session, rows[0].id) is True
    assert await notices.send(session, rows[0].id) is False  # the job running twice sends once
    assert world["pushes"] == [{
        "user_id": a.id, "title": "Bala saved your profile at DevFest", "body": world["pushes"][0]["body"],
        "data": {"type": "network_saves", "event_id": str(event.id)},
    }]
    # A save after the batch went starts a new one.
    await session.execute(UserBlocks.__table__.delete())
    await session.commit()
    f, fh = await person(session, make_user, mint_token, "farah", "Farah Khan")
    await make_registration(event=event, user=f, is_attended=True)
    await session.commit()
    await save(client, fh, handle="ananya", event_id=event.id)
    open_ = await session.scalar(select(NetworkSaveNotices).where(NetworkSaveNotices.person_id == a.id, NetworkSaveNotices.sent_at.is_(None), NetworkSaveNotices.event_id == event.id))
    assert open_ is not None and open_.saver_ids == [f.id]


async def test_notice_wording_and_switching_notices_off(client, session, world):
    assert notices.wording("Ananya", 0, "DevFest") == "Ananya saved your profile at DevFest"
    assert notices.wording("Ananya", 3, "DevFest") == "Ananya and 3 others saved your profile at DevFest"
    assert notices.wording("Ananya", 1, None) == "Ananya and 1 other saved your profile"
    await save(client, world["bh"], handle="ananya")
    await save(client, world["ch"], handle="ananya")
    n = await session.scalar(select(NetworkSaveNotices).where(NetworkSaveNotices.person_id == world["a"].id))
    # Through the job queue, once it is due.
    await session.execute(Jobs.__table__.update().where(Jobs.name == notices.NOTICE_JOB).values(run_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await session.commit()
    await jobs.run_due()
    assert world["pushes"][-1]["title"] == "Bala and 1 other saved your profile" and "event_id" not in world["pushes"][-1]["data"]
    assert (await session.scalar(select(NetworkSaveNotices.sent_at).where(NetworkSaveNotices.id == n.id))) is not None
    # notify_saves off: no batch is even opened, and one already open is not sent.
    world["pushes"].clear()
    await client.patch(f"{API}/me/profile", json={"notify_saves": False}, headers=world["bh"])
    await save(client, world["ah"], handle="bala")
    assert await session.scalar(select(func.count()).select_from(NetworkSaveNotices).where(NetworkSaveNotices.person_id == world["b"].id)) == 0
    await save(client, world["bh"], handle="chitra")
    open_ = await session.scalar(select(NetworkSaveNotices).where(NetworkSaveNotices.person_id == world["c"].id))
    await client.patch(f"{API}/me/profile", json={"notify_saves": False}, headers=world["ch"])
    assert await notices.send(session, open_.id) is False and world["pushes"] == []


# ─── The morning recap ──────────────────────────────────────────────────


async def test_the_recap_goes_once_per_person_the_morning_after(client, session, world, make_club, make_event, make_registration, monkeypatch):
    from app.core.email import email as email_mod

    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    a, b, c = world["a"], world["b"], world["c"]
    _, _, event = await live_event(make_club, make_event, make_registration, a, b, c, starts=NOW - timedelta(hours=3), duration=1.0)
    await session.commit()
    met = (NOW - timedelta(hours=2, minutes=30)).isoformat()
    await save(client, world["ah"], handle="bala", event_id=event.id, met_at=met)
    await save(client, world["ah"], handle="chitra", event_id=event.id, met_at=met)
    await save(client, world["bh"], handle="ananya", event_id=event.id, met_at=met)
    await client.patch(f"{API}/me/profile", json={"recap_emails": False}, headers=world["bh"])

    # 09:00 India time, the first after the window closed (an hour after the end).
    due = recap.due_at(event)
    closes = (event.event_datetime + timedelta(hours=2)).astimezone(recap.IST)
    assert due.hour == 9 and due.minute == 0 and due.utcoffset() == timedelta(hours=5, minutes=30)
    assert due > closes and due - closes <= timedelta(hours=24)
    job = await session.scalar(select(Jobs).where(Jobs.name == recap.RECAP_JOB))
    assert job.dedupe_key == f"{recap.RECAP_JOB}:{event.id}:{int(due.timestamp())}" and abs(job.run_at - due) < timedelta(seconds=1)

    # Too early: nothing, and it stands down.
    assert await recap.run_event(session, event.id, now=due - timedelta(hours=1)) == 0
    world["pushes"].clear()
    assert await recap.run_event(session, event.id, now=due + timedelta(minutes=5)) == 2  # Ananya and Bala met people
    await jobs.settle()
    assert await recap.run_event(session, event.id, now=due + timedelta(minutes=6)) == 0  # twice: nothing new
    assert await recap.send(session, a.id, event.id) is False  # the send twice: nothing
    await jobs.settle()
    recaps = {p["user_id"]: p for p in world["pushes"]}
    assert set(recaps) == {a.id, b.id}
    assert recaps[a.id]["title"] == "You met 2 people at DevFest" and recaps[a.id]["data"]["type"] == "network_recap"
    assert recaps[a.id]["data"]["event_id"] == str(event.id) and recaps[b.id]["title"] == "You met 1 person at DevFest"
    assert "Bala Krishnan" in recaps[a.id]["body"] and "Chitra Menon" in recaps[a.id]["body"]
    # The email: Ananya's only (Bala turned recap emails off), with the link to stop them.
    assert [m["recipients"] for m in sent] == [["ananya@example.com"]]
    mail = sent[0]
    assert mail["subject"] == "You met 2 people at DevFest" and "Designer" in mail["text"] and f"https://myotherapp.in/network?event={event.id}" in mail["text"]
    assert mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    link = mail["headers"]["List-Unsubscribe"].strip("<>")
    assert link == recap.unsubscribe_url(a.id) and link in mail["html"].replace("&amp;", "&")
    assert mail["idempotency_key"] == f"network-recap:{event.id}:{a.id}"
    assert {r.person_id for r in (await session.scalars(select(NetworkRecaps).where(NetworkRecaps.event_id == event.id))).all()} == {a.id, b.id}

    # A late offline save for someone with no recap yet: theirs alone, at once.
    world["pushes"].clear()
    await save(client, world["ch"], handle="bala", event_id=event.id, met_at=met)
    await recap.run_event(session, event.id, now=due + timedelta(hours=1))
    await jobs.settle()
    assert [p["user_id"] for p in world["pushes"]] == [c.id]


async def test_the_unsubscribe_link_turns_recap_emails_off(client, session, world):
    a = world["a"]
    url = recap.unsubscribe_url(a.id)
    path = url.split("api.myotherapp.in", 1)[1]
    page = await client.get(path)
    assert page.status_code == 200 and "<form method=\"post\"" in page.text
    assert (await profile_row(session, a)).recap_emails is True  # opening the link changes nothing
    done = await client.post(path)
    assert done.status_code == 200 and "off" in done.text
    assert (await profile_row(session, a)).recap_emails is False
    forged = path.replace(f"u={a.id}", f"u={world['b'].id}")
    assert "This link doesn&#x27;t work" in (await client.post(forged)).text
    assert (await profile_row(session, world["b"])).recap_emails is True


# ─── The club's numbers ─────────────────────────────────────────────────


async def _door_session(client, owner_headers, email, monkeypatch):
    import re

    from app.api.security import service as security
    from app.core.email import email as email_mod

    sent: list = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    r = await client.post(f"{API}/clubs/access/invite", json={"email": email, "role": "door"}, headers=owner_headers)
    assert r.status_code == 200, r.text
    token = re.search(r"token=([A-Za-z0-9_\-]+)", sent[-1]["text"]).group(1)
    assert (await client.post(f"{API}/clubs/access/invite/{token}/accept", json={"password": "Member-pass-1"})).status_code == 200
    signed = await client.post(f"{API}/auth/token", data={"username": email, "password": "Member-pass-1", "scope": "club"})
    assert signed.status_code == 200, signed.text
    return auth_header(signed.json()["access_token"])


async def test_stats_count_for_the_clubs_team_and_its_cohosts_only(client, session, world, make_club, make_event, make_registration, make_admin, mint_token, monkeypatch):
    from app.api.cohosts.models import ACCEPTED, EventCohosts
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    a, b, c = world["a"], world["b"], world["c"]
    host, club, event = await live_event(make_club, make_event, make_registration, a, b, c)
    co_owner, co = await make_club(name="Co Club", email="co-club@example.com")
    outsider, _ = await make_club(name="Other Club", email="other-club@example.com")
    session.add(EventCohosts(event_id=event.id, club_id=co.id, status=ACCEPTED, invited_at=NOW, expires_at=NOW + timedelta(days=1), responded_at=NOW))
    viewer = await make_admin(email="stats-viewer@example.com")
    await session.commit()
    await save(client, world["ah"], handle="bala", event_id=event.id)
    await save(client, world["bh"], handle="ananya", event_id=event.id)
    await save(client, world["ch"], handle="ananya", event_id=event.id)
    await save(client, world["ch"], handle="bala")  # not at the event

    path = f"{API}/events/{event.id}/networking/stats"
    host_h = auth_header(await mint_token(host))
    r = await client.get(path, headers=host_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in ("checked_in", "people_networked", "saves", "mutual_pairs")} == {"checked_in": 3, "people_networked": 3, "saves": 3, "mutual_pairs": 1}
    assert sum(h["saves"] for h in body["by_hour"]) == 3 and all(h["hour"].endswith(":00:00+05:30") for h in body["by_hour"])
    assert "ananya" not in r.text and "Ananya" not in r.text  # counts, never names
    door = await _door_session(client, host_h, "door-staff@example.com", monkeypatch)
    assert (await client.get(path, headers=door)).json() == body
    assert (await client.get(path, headers=auth_header(await mint_token(co_owner)))).json() == body
    assert (await client.get(path, headers=auth_header(await mint_token(viewer)))).status_code == 200
    assert (await client.get(path, headers=auth_header(await mint_token(outsider)))).status_code == 403
    assert (await client.get(path, headers=world["ah"])).status_code == 403
    client.cookies.clear()  # the door's sign-in left its cookie
    assert (await client.get(path)).status_code == 401
    assert (await client.get(f"{API}/events/999999/networking/stats", headers=host_h)).status_code == 404


async def test_people_like_you_says_each_persons_handle_and_whether_i_saved_them(client, session, world, make_club, make_event, make_registration):
    a, b, c = world["a"], world["b"], world["c"]
    _, _, event = await live_event(make_club, make_event, make_registration, a, b, c)
    await session.commit()
    await save(client, world["ah"], handle="bala", event_id=event.id)
    r = await client.get(f"{API}/events/{event.id}/networking", headers=world["ah"])
    assert r.status_code == 200, r.text
    got = {x["name"]: (x["handle"], x["saved"]) for x in r.json()["attendees"]}
    assert got == {"Bala Krishnan": ("bala", True), "Chitra Menon": ("chitra", False)}
    # No more than before: the same people, the same fields, plus these two.
    assert set(r.json()["attendees"][0]) == {"user_id", "handle", "saved", "name", "profile_pic_url", "organization", "shared_interests", "match_count"}
