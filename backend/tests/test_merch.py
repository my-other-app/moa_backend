"""Merchandise: what each guest is owed, resolved from their own record; the handover; the log."""
from datetime import datetime, timezone

import pytest

from app.api.events.volunteer.models import Volunteer
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)
FIELDS = [{"key": "tshirt", "label": "T-shirt size", "field_type": "select", "options": ["S", "M", "L"], "required": False}]


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="merch@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Day", event_datetime=START, duration=8.0, additional_details=FIELDS)
    a = await make_user(email="ananya-m@example.com")
    s = await make_user(email="sita-m@example.com")
    b = await make_user(email="bala-m@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", additional_details={"tshirt": "L"})
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker")
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan", additional_details={"tshirt": "XL"})
    vol_user = await make_user(email="vani-m@example.com")
    session.add(Volunteer(email=vol_user.email, event_id=event.id, user_id=vol_user.id, is_approved=True, can_check_in=True, can_merch=True))
    nope_user = await make_user(email="nope-m@example.com")
    session.add(Volunteer(email=nope_user.email, event_id=event.id, user_id=nope_user.id, is_approved=True, can_check_in=True, can_merch=False))
    await session.commit()
    return {
        "event": event, "a": r_a, "s": r_s, "b": r_b,
        "club": auth_header(await mint_token(owner)), "vol": auth_header(await mint_token(vol_user)), "nope": auth_header(await mint_token(nope_user)),
    }


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/merch{path}"


def _gate(world, path=""):
    return f"/api/v1/events/{world['event'].id}/gate/merch{path}"


async def test_items_keep_stock_or_sizes_and_the_rules_hold(client, world):
    h = world["club"]
    fields = await client.get(_url(world, "/fields"), headers=h)
    assert fields.status_code == 200, fields.text
    assert [f["name"] for f in fields.json()] == ["Role", "T-shirt size"]
    assert fields.json()[1]["values"] == ["S", "M", "L"] and fields.json()[0]["values"] == ["Attendee", "Speaker", "Staff"]

    tote = await client.post(_url(world), json={"name": "Summit tote", "stock": 1}, headers=h)
    assert tote.status_code == 201, tote.text
    assert tote.json()["total"] == 1 and tote.json()["collected"] == 0 and tote.json()["variants"] == []
    tee = await client.post(_url(world), json={"name": "Build Day tee", "variant_field": "T-shirt size", "variants": [{"label": "S", "stock": 1}, {"label": "M", "stock": 1}, {"label": "L", "stock": 1}]}, headers=h)
    assert tee.status_code == 201, tee.text
    assert tee.json()["stock"] is None and tee.json()["total"] == 3 and [v["label"] for v in tee.json()["variants"]] == ["S", "M", "L"]
    gift = await client.post(_url(world), json={"name": "Speaker gift", "stock": 5, "eligibility_field": "Role", "eligibility_value": "Speaker"}, headers=h)
    assert gift.status_code == 201

    # Stock or variants, never both; both halves of an eligibility rule; distinct labels.
    assert (await client.post(_url(world), json={"name": "Odd", "stock": 2, "variant_field": "T-shirt size"}, headers=h)).status_code == 422
    assert (await client.post(_url(world), json={"name": "Odd"}, headers=h)).status_code == 422
    assert (await client.post(_url(world), json={"name": "Odd", "stock": 2, "eligibility_field": "Role"}, headers=h)).status_code == 422
    assert (await client.post(_url(world), json={"name": "Odd", "variant_field": "x", "variants": [{"label": "L", "stock": 1}, {"label": "l", "stock": 1}]}, headers=h)).status_code == 422

    # Variants come and go; a second L is refused.
    tee_id = tee.json()["id"]
    added = await client.post(_url(world, f"/{tee_id}/variants"), json={"label": "XL", "stock": 2}, headers=h)
    assert added.status_code == 200 and [v["label"] for v in added.json()["variants"]] == ["S", "M", "L", "XL"]
    assert (await client.post(_url(world, f"/{tee_id}/variants"), json={"label": "l", "stock": 2}, headers=h)).status_code == 409
    xl = next(v for v in added.json()["variants"] if v["label"] == "XL")
    assert (await client.patch(_url(world, f"/{tee_id}/variants/{xl['id']}"), json={"stock": 3}, headers=h)).json()["total"] == 6
    assert (await client.delete(_url(world, f"/{tee_id}/variants/{xl['id']}"), headers=h)).json()["total"] == 3
    assert (await client.post(_url(world, f"/{tote.json()['id']}/variants"), json={"label": "One", "stock": 1}, headers=h)).status_code == 422

    listed = await client.get(_url(world), headers=h)
    assert [i["name"] for i in listed.json()] == ["Summit tote", "Build Day tee", "Speaker gift"]
    assert (await client.get(_url(world), headers=world["nope"])).status_code in (403, 404)


async def test_the_gate_shows_what_each_guest_is_owed_and_hands_it_over_once(client, world):
    h = world["club"]
    tote = (await client.post(_url(world), json={"name": "Summit tote", "stock": 1}, headers=h)).json()
    tee = (await client.post(_url(world), json={"name": "Build Day tee", "variant_field": "T-shirt size", "variants": [{"label": "S", "stock": 1}, {"label": "M", "stock": 1}, {"label": "L", "stock": 1}]}, headers=h)).json()
    gift = (await client.post(_url(world), json={"name": "Speaker gift", "stock": 5, "eligibility_field": "Role", "eligibility_value": "Speaker"}, headers=h)).json()
    v = world["vol"]

    # The console knows there is something to hand out; a volunteer without the switch may not.
    ctx = await client.get(f"/api/v1/events/{world['event'].id}/gate/context", headers=v)
    assert ctx.status_code == 200 and ctx.json()["has_merch"] is True and ctx.json()["permissions"]["merch"] is True
    assert (await client.get(_gate(world, f"/{world['a'].id}"), headers=world["nope"])).status_code == 403

    # Ananya: a tote, a tee in L from her record, no speaker gift.
    st = await client.get(_gate(world, f"/{world['a'].id}"), headers=v)
    assert st.status_code == 200, st.text
    by_name = {r["name"]: r for r in st.json()["items"]}
    assert st.json()["attendee"]["name"] == "Ananya Iyer"
    assert by_name["Summit tote"]["status"] == "COLLECTABLE" and by_name["Summit tote"]["remaining"] == 1
    assert by_name["Build Day tee"]["status"] == "COLLECTABLE" and by_name["Build Day tee"]["variant_label"] == "L" and by_name["Build Day tee"]["on_file"] == "L"
    assert by_name["Speaker gift"]["status"] == "NOT_ELIGIBLE"

    # Sita: no size on file, so the volunteer picks one and it goes onto her record; she gets the gift.
    st = await client.get(_gate(world, f"/{world['s'].id}"), headers=v)
    by_name = {r["name"]: r for r in st.json()["items"]}
    assert by_name["Build Day tee"]["status"] == "NO_VARIANT_VALUE" and [c["label"] for c in by_name["Build Day tee"]["choices"]] == ["S", "M", "L"]
    assert by_name["Speaker gift"]["status"] == "COLLECTABLE"
    # A size that is not set up is refused before anything moves.
    assert (await client.post(_gate(world, f"/{tee['id']}/collect"), json={"registration_id": str(world["s"].id), "variant_label": "XXL"}, headers=v)).status_code == 422
    picked = await client.post(_gate(world, f"/{tee['id']}/collect"), json={"registration_id": str(world["s"].id), "variant_label": "m", "door": "Merch desk"}, headers=v)
    assert picked.status_code == 200, picked.text
    assert picked.json() == {"status": "COLLECTABLE", "collected": True, "variant_label": "M", "remaining": 0}
    st = await client.get(_gate(world, f"/{world['s'].id}"), headers=v)
    assert {r["name"]: r["status"] for r in st.json()["items"]}["Build Day tee"] == "ALREADY_COLLECTED"
    # Once collected, a second attempt is told so, whatever size it names.
    assert (await client.post(_gate(world, f"/{tee['id']}/collect"), json={"registration_id": str(world["s"].id), "variant_label": "XXL"}, headers=v)).json()["status"] == "ALREADY_COLLECTED"

    # Bala: XL matches nothing, so he may pick; the tote is gone once Ananya has it.
    st = await client.get(_gate(world, f"/{world['b'].id}"), headers=v)
    by_name = {r["name"]: r for r in st.json()["items"]}
    assert by_name["Build Day tee"]["status"] == "NO_MATCHING_VARIANT" and by_name["Build Day tee"]["on_file"] == "XL"
    assert (await client.post(_gate(world, f"/{tote['id']}/collect"), json={"registration_id": str(world["a"].id), "door": "Merch desk"}, headers=v)).json()["collected"] is True
    assert (await client.post(_gate(world, f"/{tote['id']}/collect"), json={"registration_id": str(world["a"].id)}, headers=v)).json()["status"] == "ALREADY_COLLECTED"
    assert (await client.post(_gate(world, f"/{tote['id']}/collect"), json={"registration_id": str(world["b"].id)}, headers=v)).json()["status"] == "OUT_OF_STOCK"
    assert (await client.post(_gate(world, f"/{gift['id']}/collect"), json={"registration_id": str(world["a"].id)}, headers=v)).json()["status"] == "NOT_ELIGIBLE"
    assert (await client.post(_gate(world, f"/{gift['id']}/collect"), json={"registration_id": str(world["s"].id)}, headers=h)).json()["collected"] is True

    # The log and the counts.
    log = await client.get(_url(world, "/collections"), headers=h)
    assert log.status_code == 200, log.text
    assert log.json()["total"] == 3 and log.json()["facets"]["total"] == 3
    assert {i["name"]: i["count"] for i in log.json()["facets"]["items"]} == {"Build Day tee": 1, "Summit tote": 1, "Speaker gift": 1}
    assert {d["name"]: d["count"] for d in log.json()["facets"]["doors"]} == {"Merch desk": 2}
    assert log.json()["facets"]["without_door"] == 1
    first = log.json()["items"][0]
    assert first["name"] == "Sita Raman" and first["item"]["name"] == "Speaker gift" and first["door"] is None
    only_tee = await client.get(_url(world, "/collections"), params={"item_id": tee["id"]}, headers=h)
    assert only_tee.json()["total"] == 1 and only_tee.json()["items"][0]["variant"]["label"] == "M"
    an = await client.get(_url(world, "/analytics"), headers=h)
    assert an.status_code == 200, an.text
    assert an.json()["collectors"] == 2 and an.json()["handovers"] == 3
    assert {i["name"]: i["collected"] for i in an.json()["by_item"]} == {"Build Day tee": 1, "Summit tote": 1, "Speaker gift": 1}
    assert len(an.json()["by_hour"]) == 1 and an.json()["by_hour"][0]["count"] == 3

    # Stock cannot drop below what was handed out; an item with handovers goes only when asked with them.
    assert (await client.patch(_url(world, f"/{tote['id']}"), json={"stock": 0}, headers=h)).status_code == 422
    assert (await client.patch(_url(world, f"/{tote['id']}"), json={"stock": 4, "name": "Big tote"}, headers=h)).json()["total"] == 4
    assert (await client.delete(_url(world, f"/{tote['id']}"), headers=h)).status_code == 409
    assert (await client.delete(_url(world, f"/{tote['id']}"), params={"with_collections": "true"}, headers=h)).status_code == 200
    assert (await client.get(_url(world, "/collections"), headers=h)).json()["total"] == 2
    assert [i["name"] for i in (await client.get(_url(world), headers=h)).json()] == ["Build Day tee", "Speaker gift"]
