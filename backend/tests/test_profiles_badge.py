"""The badge's Personal QR (profiles-networking contract, "Badges" and the
dashboard notes): a layout v2 QR layer with `bind: "holder.profile_qr_url"`
encodes the holder's live /p/ address, beside the check-in code; the render
data carries it for a registration whose app user has scanning on, the
board's sample carries one, and designs without `bind` are unchanged."""
import json

import pytest

from app.api.doc_presets import values as preset_values
from app.core import layout_v2 as v2
from app.core.layout_v2 import build
from tests.conftest import auth_header
from tests.profiles_world import API, person

QR = {"id": "qr", "name": "QR code", "kind": "qr", "x": 250, "y": 440, "width": 108, "height": 108}
PERSONAL = {"id": "pqr", "name": "Personal QR", "kind": "qr", "x": 20, "y": 440, "width": 108, "height": 108, "bind": "holder.profile_qr_url"}


def lay(*layers) -> dict:
    return {"version": 2, "page": {"widthMm": 101.6, "heightMm": 152.4}, "canvas": {"width": 384, "height": 576}, "layers": list(layers)}


def codes(layout, kind="badge", level="error") -> set[str]:
    return {p["code"] for p in v2.problems(layout, kind) if p["level"] == level}


async def test_the_render_data_carries_the_holders_live_code(client, session, make_user, mint_token, make_club, make_event, make_registration):
    from app.api.idcards import service as idcards
    from app.api.users.models import UserTypes

    holder, hh = await person(session, make_user, mint_token, "holder", "Holder One")
    guest = await make_user(email="badge-guest@example.com", username="badge-guest", user_type=UserTypes.guest)
    owner, club = await make_club(name="Badge Club", email="badge-club@example.com")
    event = await make_event(club=club, created_by=owner)
    reg = await make_registration(event=event, user=holder)
    guest_reg = await make_registration(event=event, user=guest)
    await session.commit()

    async def value_of(registration_id):
        session.expunge_all()
        loaded = await idcards.registration_of(session, event, registration_id)
        subject = preset_values.subject_of(loaded, loaded.event)
        return preset_values.document_values("badge", loaded.event, subject).values[preset_values.PROFILE_QR_VALUE]

    assert await value_of(reg.id) == ""  # no code yet: none is made at print time
    code = (await client.get(f"{API}/me/qr", headers=hh)).json()["code"]
    assert await value_of(reg.id) == f"https://myotherapp.in/p/{code}"
    replaced = (await client.post(f"{API}/me/qr/replace", headers=hh)).json()["code"]
    assert await value_of(reg.id) == f"https://myotherapp.in/p/{replaced}"
    await client.patch(f"{API}/me/profile", json={"scanning_enabled": False}, headers=hh)
    assert await value_of(reg.id) == ""
    await client.patch(f"{API}/me/profile", json={"scanning_enabled": True}, headers=hh)
    assert await value_of(guest_reg.id) == ""  # a guest has no profile
    # Without the code loaded (another path), nothing is guessed.
    session.expunge_all()
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.api.events.models import EventRegistrationsLink, Events

    bare = await session.scalar(
        select(EventRegistrationsLink).options(joinedload(EventRegistrationsLink.event).joinedload(Events.club), joinedload(EventRegistrationsLink.user))
        .where(EventRegistrationsLink.id == reg.id)
    )
    assert preset_values.subject_of(bare, bare.event).profile_qr_url is None


async def test_the_board_gets_a_sample_personal_qr(client, session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Board Club", email="board-club@example.com")
    event = await make_event(club=club, created_by=owner)
    await session.commit()
    r = await client.get(f"{API}/events/{event.id}/designs/badge/layout", headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    v2_block = r.json().get("v2") or {}
    assert v2_block["values"]["holder.profile_qr_url"] == preset_values.SAMPLE_PROFILE_QR
    from app.api.profiles import rules

    assert rules.normal_code(preset_values.SAMPLE_PROFILE_QR.rsplit("/", 1)[1]) is None  # never a real code


@pytest.mark.parametrize("kind", ["ticket", "certificate"])
def test_tickets_and_certificates_have_no_personal_qr_value(kind):
    assert preset_values.PROFILE_QR_VALUE not in preset_values.tokens_for(kind)


# ─── The layer itself (no database: last in the file, tests/conftest.py's loop) ───


def test_a_design_without_bind_is_unchanged_byte_for_byte():
    plain = lay(QR)
    parsed = v2.parse(plain)
    assert parsed.layers[0].bind is None
    assert "bind" not in v2.dump(parsed)["layers"][0]  # no new key in what is stored
    assert codes(plain) == set()
    # Every starter still validates and dumps as it is stored.
    for sid in v2.starter_ids("badge"):
        layout = v2.starter(sid)
        assert codes(layout) == set(), sid
        assert json.dumps(v2.dump(v2.parse(layout)), sort_keys=True) == json.dumps(v2.dump(v2.parse(v2.dump(v2.parse(layout)))), sort_keys=True)
    body = build.build_body(plain, {}, context={"qr": "MOA-TICKET-1"}, kind="badge")
    assert body.count('class="moa-qr"') == 1
    assert body == build.build_body(plain, {build_value(): "https://myotherapp.in/p/abcdefghjk"}, context={"qr": "MOA-TICKET-1"}, kind="badge")


def build_value() -> str:
    return preset_values.PROFILE_QR_VALUE


def test_the_personal_qr_draws_the_holders_code_and_nothing_when_there_is_none():
    layout = lay(QR, PERSONAL)
    assert codes(layout) == set()
    ctx = {"qr": "MOA-TICKET-1"}
    with_code = build.build_body(layout, {"holder.profile_qr_url": "https://myotherapp.in/p/abcdefghjk"}, context=ctx, kind="badge")
    assert with_code.count('class="moa-qr"') == 2
    assert build.qr_svg("https://myotherapp.in/p/abcdefghjk", 0) in with_code and build.qr_svg("MOA-TICKET-1", 0) in with_code
    for empty in ({}, {"holder.profile_qr_url": ""}, {"holder.profile_qr_url": None}):
        body = build.build_body(layout, empty, context=ctx, kind="badge")
        assert body.count('class="moa-qr"') == 1 and 'data-id="pqr"' not in body and "pqr" not in body


def test_one_qr_of_each_kind_and_only_the_check_in_code_must_show():
    assert "qr_extra" in codes(lay(QR, {**QR, "id": "qr2", "x": 20}))  # a second check-in QR is still refused
    assert "qr_extra" in codes(lay(QR, PERSONAL, {**PERSONAL, "id": "pqr2", "x": 130}))
    assert "qr_missing" in codes(lay(PERSONAL))  # the personal code is not the gate's
    # A Personal QR may be hidden or shown on a condition; the check-in code may not.
    assert codes(lay(QR, {**PERSONAL, "hidden": True})) == set()
    assert codes(lay(QR, {**PERSONAL, "showIf": "first_name"})) == set()
    assert "qr_hidden" in codes(lay({**QR, "hidden": True}, PERSONAL))
    # ID cards only; small ones are refused as any QR is.
    assert "qr_profile_kind" in codes(lay(QR, PERSONAL), kind="ticket")
    assert "qr_small" in codes(lay(QR, {**PERSONAL, "width": 20, "height": 20}))
    # Only that value may be bound.
    assert "schema" in codes(lay(QR, {**PERSONAL, "bind": "email"}))
