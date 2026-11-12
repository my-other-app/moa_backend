"""The designer's ticket, certificate and ID card designs, wired in
(feature-register/doc-designs-contract.md, Backend B).

The designs themselves and the render service are Backend A's
(app.core.doc_designs): here they are tests/doc_designs_fake.py, which
records what each document was filled with and renders a PDF of it.
"""
import base64
import io
import zipfile
from datetime import datetime, timezone

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.doc_presets import config as presets
from app.api.doc_presets import engine
from app.api.doc_presets.engine import unavailable as real_unavailable
from app.api.doc_presets.photos import _event_key, square
from app.api.doc_presets.values import Subject, badge_values, certificate_values, ticket_values
from app.api.events.models import EventRegistrationsLink
from app.config import settings
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_text
from app.core.public_urls import site_url
from app.core.storage import objects
from tests.conftest import auth_header
from tests.doc_designs_fake import install
from tests.legacy_layouts import certificate_starter, ticket_starter

# 18:00 in Kochi.
START = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
FORM = [
    {"key": "job_title", "label": "Job title", "field_type": "text", "required": False},
    {"key": "photo", "label": "Photo", "field_type": "image", "required": False},
]


@pytest.fixture
def designs(monkeypatch):
    return install(monkeypatch)


@pytest.fixture
def store(monkeypatch):
    """The bucket as a dict, and rendered documents kept in it."""
    held: dict = {}
    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: held.get(key))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    return held


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="presets@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0,
        location_name="Innovation Hall, Kochi", additional_details=FORM,
    )
    a = await make_user(email="ananya-presets@example.com")
    s = await make_user(email="sita-presets@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", ticket_label="VIP", additional_details={"job_title": "Product Designer"}, is_attended=True)
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker", is_attended=True)
    await session.commit()
    return {
        "event": event, "club_row": club, "a": r_a, "s": r_s, "user_a": a,
        "club": auth_header(await mint_token(owner)), "guest": auth_header(await mint_token(a)),
    }


def _layout(kind="ticket"):
    s = ticket_starter("classic") if kind == "ticket" else certificate_starter("classic")
    return dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))


def _designs(world, kind, path=""):
    return f"/api/v1/events/{world['event'].id}/designs/{kind}{path}"


def _png(w, h, colour=(40, 120, 200)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(b, format="PNG")
    return b.getvalue()


# ─── Tickets ────────────────────────────────────────────────────────────────


async def test_a_ticket_with_no_saved_design_is_the_default_design(client, world, designs):
    reg = world["a"]
    pdf = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200, pdf.text
    assert pdf.content[:4] == b"%PDF" and pdf.headers.get("etag")

    design_id, values, _, _, _ = designs.fills[-1]
    assert design_id == "ticket/03-daylight-boarding-pass"
    assert {k: values[k] for k in ("attendee_name", "app_name", "event_name", "date", "start_time", "venue", "ticket_type", "ticket_id")} == {
        "attendee_name": "Ananya Iyer", "app_name": "Yukthi", "event_name": "Build Night", "date": "20 Nov 2026",
        "start_time": "18:00", "venue": "Innovation Hall, Kochi", "ticket_type": "VIP", "ticket_id": reg.ticket_id,
    }
    # What we do not have is empty, never made up: the design hides its block.
    assert values["gate"] == values["row"] == values["seat"] == values["tagline"] == values["doors_time"] == ""
    # The QR is A's to draw, from the code the gate scans; the answers ride along for bindings.
    assert values["qr"] == reg.ticket_id and values["field.job_title"] == "Product Designer"
    assert designs.printed[-1]["qr_code_src"] == f"QR({reg.ticket_id})"
    assert "attendee_name=Ananya Iyer" in pdf_text(pdf.content)

    png = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/image", headers=world["guest"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    # Twice the design's 1280 CSS pixels (D2).
    assert Image.open(io.BytesIO(png.content)).size[0] == 2560


async def test_the_ticket_document_for_email_and_whatsapp_is_the_same_design(session, world, designs):
    from app.api.events import service, tickets

    reg = await service.get_ticket_details(session, ticket_id=world["a"].ticket_id)
    doc = await tickets.ticket_document(session, reg)
    assert doc.content[:4] == b"%PDF" and doc.filename == f"ticket-{reg.ticket_id}.pdf" and doc.design_version == 0
    assert designs.fills[-1][0] == "ticket/03-daylight-boarding-pass"


async def test_with_the_render_service_down_the_classic_ticket_is_drawn_and_not_kept(client, world, designs, store, _render_service_offline):
    reg = world["a"]
    assert (await client.put(_designs(world, "ticket", "/layout"), json=_layout(), headers=world["club"])).status_code == 200
    chosen = await client.put(_designs(world, "ticket", "/preset"), json={"id": "ticket/03-daylight-boarding-pass"}, headers=world["club"])
    assert chosen.status_code == 200 and chosen.json()["active"] == "preset"

    designs.down = True
    pdf = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200, pdf.text
    # The club's own layout, the design without its preset (D9).
    text = pdf_text(pdf.content)
    assert "Ananya Iyer" in text and "attendee_name=" not in text
    assert "etag" not in pdf.headers
    assert not [k for k in store if "/docs/" in k or k.startswith("docs/")]
    assert _render_service_offline and _render_service_offline[0][0] == "ticket"

    designs.down = False
    again = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/pdf", headers=world["guest"])
    assert "attendee_name=Ananya Iyer" in pdf_text(again.content) and again.headers.get("etag")
    assert [k for k in store if "docs" in k and k.endswith(".pdf")]


async def test_a_saved_custom_design_is_drawn_as_it_always_was(client, world, designs):
    assert (await client.put(_designs(world, "ticket", "/layout"), json=_layout(), headers=world["club"])).status_code == 200
    pdf = await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200 and "Ananya Iyer" in pdf_text(pdf.content)
    assert designs.fills == [] and designs.renders == []


async def test_the_fingerprint_names_the_design_itself(client, world, designs):
    """A new template file or font lock (A's fingerprint) is a new document (D8)."""
    url = f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf"
    first = (await client.get(url, headers=world["guest"])).headers["etag"]
    assert (await client.get(url, headers=world["guest"])).headers["etag"] == first
    designs.version = "2"
    assert (await client.get(url, headers=world["guest"])).headers["etag"] != first


# ─── The editor ─────────────────────────────────────────────────────────────


async def test_the_editor_opens_on_the_default_design_with_the_gallery(client, world, designs):
    t = (await client.get(_designs(world, "ticket", "/layout"), headers=world["club"])).json()
    assert t["active"] == "preset" and t["preset"]["id"] == "ticket/03-daylight-boarding-pass" == t["presetDefault"]["id"]
    assert t["isOwn"] is False and t["fromStarter"] is True and t["starters"] == {} and t["hasLayout"] is False
    assert {d["id"] for d in t["catalogue"]} == {"ticket/03-daylight-boarding-pass", "ticket/01-dusk-horizon"}
    assert any(b["token"] == "gate" for b in t["bindable"])
    # The editor shape of each design is A's to_dict(); its preview a browser address.
    dusk = next(d for d in t["catalogue"] if d["id"] == "ticket/01-dusk-horizon")
    assert dusk["preview"].startswith("https://") and "gate" in dusk["bindable"] and dusk["tokens"]["venue"] == "required"
    assert t["hasPhotoQuestion"] is True
    assert {"key": "job_title", "label": "Job title", "type": "text", "slug": "job_title", "token": "field.job_title"} in t["questions"]
    # The custom canvas still opens on something valid: a blank ticket with its QR.
    assert any(layer["kind"] == "qr" for layer in t["layers"])

    b = (await client.get(_designs(world, "badge", "/layout"), headers=world["club"])).json()
    assert b["active"] == "preset" and b["presetDefault"]["id"] == "badge/06-thermal-mono"
    assert list(b["starters"]) == ["card_cr80"] and b["page"] == {"widthMm": 54, "heightMm": 85.6}
    c = (await client.get(_designs(world, "certificate", "/layout"), headers=world["club"])).json()
    assert c["presetDefault"]["id"] == "certificate/07-swiss-minimal"
    a = (await client.get(_designs(world, "agenda", "/layout"), headers=world["club"])).json()
    assert a["active"] == "custom" and a["catalogue"] == [] and a["presetDefault"] is None
    assert (await client.get(_designs(world, "ticket", "/layout/starters/classic"), headers=world["club"])).status_code == 404


async def test_choosing_a_design_is_checked_and_makes_it_the_one_in_use(client, world, designs):
    url, h = _designs(world, "ticket", "/preset"), world["club"]
    wrong_kind = await client.put(url, json={"id": "certificate/07-swiss-minimal"}, headers=h)
    assert wrong_kind.status_code == 422 and "id" in wrong_kind.json()["errors"]["fields"]
    unknown = await client.put(url, json={"id": "ticket/99-nope"}, headers=h)
    assert unknown.status_code == 422 and unknown.json()["error_code"] == "INVALID_PRESET"
    option = await client.put(url, json={"id": "ticket/01-dusk-horizon", "options": {"nope": True}}, headers=h)
    assert option.status_code == 422 and "no option nope" in option.json()["message"]
    binding = await client.put(url, json={"id": "ticket/01-dusk-horizon", "bindings": {"gate": {"source": "sometimes"}}}, headers=h)
    assert binding.status_code == 422 and "bindings.gate" in binding.json()["errors"]["fields"]
    required = await client.put(url, json={"id": "ticket/01-dusk-horizon", "bindings": {"venue": {"source": "hidden"}}}, headers=h)
    assert required.status_code == 422 and "must print venue" in required.json()["message"]
    accent = await client.put(url, json={"id": "ticket/01-dusk-horizon", "accent": "red"}, headers=h)
    assert accent.status_code == 422 and "accent" in accent.json()["errors"]["fields"]

    sent = {
        "tagline": {"source": "text", "value": "A night of building"},
        "gate": {"source": "field", "value": "job_title"},
        "seat": {"source": "hidden"},
        "row": {"source": "auto"},
    }
    chosen = await client.put(url, json={"id": "ticket/01-dusk-horizon", "bindings": sent}, headers=h)
    assert chosen.status_code == 200, chosen.text
    body = chosen.json()
    assert body["active"] == "preset" and body["isOwn"] is True and body["warnings"] == []
    # Stored as sent: A's fill applies them, once.
    assert body["preset"]["bindings"] == sent

    await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", headers=world["guest"])
    design_id, values, _, bindings, _ = designs.fills[-1]
    assert design_id == "ticket/01-dusk-horizon" and bindings == sent
    # Our values are untouched; what printed is the binding's.
    assert values["tagline"] == "" and values["field.job_title"] == "Product Designer"
    printed = designs.printed[-1]
    assert printed["tagline"] == "A night of building" and printed["gate"] == "Product Designer" and printed["seat"] == ""
    # The speaker answered no such question: their gate is empty.
    await client.get(f"/api/v1/events/tickets/{world['s'].ticket_id}/pdf", headers=world["club"])
    assert designs.printed[-1]["gate"] == "" and designs.last_values()["ticket_type"] == "Speaker"


async def test_options_follow_the_designs_schema_and_a_warning_still_saves(client, world, designs):
    url, h = _designs(world, "certificate", "/preset"), world["club"]
    own_words = {"id": "certificate/07-swiss-minimal", "options": {"wording": "achievement", "text": {"title": "Certificate of Excellence"}}}
    saved = await client.put(url, json=own_words, headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["preset"]["options"] == own_words["options"]
    flat = await client.put(url, json={"id": "certificate/07-swiss-minimal", "options": {"text": "Excellence"}}, headers=h)
    assert flat.status_code == 422 and "map a line" in flat.json()["message"]

    pale = await client.put(url, json={"id": "certificate/07-swiss-minimal", "accent": "#ffff00"}, headers=h)
    assert pale.status_code == 200, pale.text
    assert pale.json()["preset"]["accent"] == "#FFFF00"
    assert [(w["code"], w["level"]) for w in pale.json()["warnings"]] == [("low_contrast", "warning")]


async def test_the_custom_design_and_the_default_are_one_step_away(client, world, designs):
    h = world["club"]
    await client.put(_designs(world, "ticket", "/preset"), json={"id": "ticket/01-dusk-horizon"}, headers=h)
    custom = await client.put(_designs(world, "ticket", "/layout"), json=_layout(), headers=h)
    # Saving the custom canvas makes it the design in use (D6).
    assert custom.json()["active"] == "custom" and custom.json()["preset"] is None and custom.json()["hasLayout"] is True
    again = await client.put(_designs(world, "ticket", "/preset"), json={"id": "ticket/01-dusk-horizon"}, headers=h)
    assert again.json()["active"] == "preset" and again.json()["hasLayout"] is True and again.json()["version"] == 1
    # Stop using it: the layout stays and is the design in use.
    cleared = await client.delete(_designs(world, "ticket", "/preset"), headers=h)
    assert cleared.status_code == 200 and cleared.json()["active"] == "custom" and cleared.json()["isOwn"] is True
    fills = len(designs.fills)
    await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", headers=world["guest"])
    assert len(designs.fills) == fills

    # Without a layout of its own, the event follows the default again.
    await client.put(_designs(world, "certificate", "/preset"), json={"id": "certificate/01-heritage"}, headers=h)
    back = await client.delete(_designs(world, "certificate", "/preset"), headers=h)
    assert back.json()["active"] == "preset" and back.json()["preset"]["id"] == "certificate/07-swiss-minimal" and back.json()["isOwn"] is False


async def test_the_preview_is_html_for_the_iframe_or_the_exact_png(client, world, designs):
    url, h = _designs(world, "ticket", "/preset/preview"), world["club"]
    real = await client.post(url, json={"id": "ticket/01-dusk-horizon", "ticket_id": world["a"].ticket_id}, headers=h)
    assert real.status_code == 200, real.text
    assert "Ananya Iyer" in real.json()["html"] and "/static/doc-fonts/fonts.css" in real.json()["html"]
    sample = await client.post(url, json={"id": "ticket/01-dusk-horizon"}, headers=h)
    assert "Aisha Rahman" in sample.json()["html"]
    assert (await client.post(url, json={"id": "ticket/01-dusk-horizon", "ticket_id": "MOA-NOPE"}, headers=h)).status_code == 404

    png = await client.post(url + "?format=png", json={"id": "ticket/01-dusk-horizon"}, headers=h)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).size[0] == 2560
    designs.down = True
    down = await client.post(url + "?format=png", json={"id": "ticket/01-dusk-horizon"}, headers=h)
    assert down.status_code == 503 and down.json()["error_code"] == "RENDER_UNAVAILABLE"
    # Previewing never saves anything.
    assert (await client.get(_designs(world, "ticket", "/layout"), headers=h)).json()["isOwn"] is False


async def test_the_clubs_default_design_is_chosen_the_same_way(client, world, designs):
    h = world["club"]
    chosen = await client.put("/api/v1/clubs/designs/certificate/preset", json={"id": "certificate/01-heritage", "options": {"wording": "completion"}}, headers=h)
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["active"] == "preset" and chosen.json()["isOwn"] is True
    on_event = (await client.get(_designs(world, "certificate", "/layout"), headers=h)).json()
    assert on_event["preset"]["id"] == "certificate/01-heritage" and on_event["isOwn"] is False
    preview = await client.post("/api/v1/clubs/designs/certificate/preset/preview", json={"id": "certificate/01-heritage"}, headers=h)
    assert preview.status_code == 200 and "Sample event" in preview.json()["html"]
    dropped = await client.delete("/api/v1/clubs/designs/certificate/preset", headers=h)
    assert dropped.json()["preset"]["id"] == "certificate/07-swiss-minimal"


# ─── ID cards ───────────────────────────────────────────────────────────────


async def _profile(session, user, *, photo: bytes | None, store: dict, org: str = "Acme Labs"):
    from app.api.orgs.models import Organizations, OrgTypes
    from app.api.users.models import UserProfiles
    from app.core.storage.fields import variant_key

    o = Organizations(name=org, type=OrgTypes.company)
    session.add(o)
    await session.flush()
    key = f"{settings.S3_BASE_PATH}/users/profile_pic/{user.id}.png"
    if photo is not None:
        store[variant_key(key, "large")] = photo
    session.add(UserProfiles(user_id=user.id, full_name=user.full_name, org_id=o.id, profile_pic=key if photo is not None else None))
    await session.commit()


async def test_a_card_prints_the_person_with_their_profile_photo(client, session, world, designs, store):
    await _profile(session, world["user_a"], photo=_png(900, 1200), store=store)
    h, ticket_id = world["club"], world["a"].ticket_id
    assert (await client.put(_designs(world, "badge", "/preset"), json={"id": "badge/01-ticket-stub"}, headers=h)).status_code == 200

    card = await client.get(f"/api/v1/events/{world['event'].id}/idcards/{world['a'].id}.pdf", params={"source": "manual"}, headers=h)
    assert card.status_code == 200, card.text
    assert card.content[:4] == b"%PDF"
    design_id, v, _, _, _ = designs.fills[-1]
    assert design_id == "badge/01-ticket-stub"
    assert (v["first_name"], v["last_name"], v["full_name"], v["initials"]) == ("Ananya", "Iyer", "Ananya Iyer", "AI")
    assert (v["role"], v["access_level"], v["badge_id"], v["organization"]) == ("ATTENDEE", "VIP", ticket_id, "Acme Labs")
    assert (v["event_name"], v["event_date"], v["event_year"], v["org_name"], v["city"]) == ("Build Night", "20 Nov 2026", "2026", "Yukthi", "Kochi")
    assert v["member_id"] == v["badge_id"] and v["valid_thru"] == "20 Nov 2026" and v["checkin_time"]
    photo = base64.b64decode(v["photo_url"].split(",", 1)[1])
    assert v["photo_url"].startswith("data:image/jpeg;base64,") and Image.open(io.BytesIO(photo)).size == (800, 800)
    assert v["qr"] == ticket_id and designs.printed[-1]["qr_code_url"] == f"QR({ticket_id})"

    # No photo anywhere: empty, and the design draws the initials in its frame.
    await client.get(f"/api/v1/events/{world['event'].id}/idcards/{world['s'].id}.pdf", params={"record": "false"}, headers=h)
    v = designs.last_values()
    assert v["photo_url"] == "" and v["role"] == "SPEAKER" and v["organization"] == "" and v["checkin_time"] == ""


async def test_a_card_cannot_print_while_the_render_service_is_down(client, session, world, designs, _render_service_offline):
    designs.down = True
    h, event_id, reg_id = world["club"], world["event"].id, world["a"].id
    refused = await client.get(f"/api/v1/events/{event_id}/idcards/{reg_id}.pdf", headers=h)
    assert refused.status_code == 503, refused.text
    assert refused.json()["message"] == "Printing is unavailable, try again." and refused.json()["error_code"] == "RENDER_UNAVAILABLE"
    # Nothing printed, so the card is not counted as printed.
    printed = await session.scalar(select(EventRegistrationsLink.badge_printed_at).where(EventRegistrationsLink.id == reg_id))
    assert printed is None
    assert (await client.get(f"/api/v1/events/{event_id}/idcards/summary", headers=h)).json()["printed"] == 0
    assert [kind for kind, _ in _render_service_offline] == ["badge"]


async def test_a_card_whose_design_needs_what_a_guest_has_not_says_so(client, session, world, designs, make_user, make_registration):
    """A one-word name on a design that prints a last name: not an outage."""
    mono = await make_registration(event=world["event"], user=await make_user(email="mono-presets@example.com"), full_name="Madonna")
    await session.commit()
    h, event_id, reg_id = world["club"], world["event"].id, mono.id
    assert (await client.put(_designs(world, "badge", "/preset"), json={"id": "badge/01-ticket-stub"}, headers=h)).status_code == 200
    refused = await client.get(f"/api/v1/events/{event_id}/idcards/{reg_id}.pdf", headers=h)
    assert refused.status_code == 422 and refused.json()["error_code"] == "DESIGN_INCOMPLETE" and "last_name" in refused.json()["message"]
    assert await session.scalar(select(EventRegistrationsLink.badge_printed_at).where(EventRegistrationsLink.id == reg_id)) is None


async def test_a_batch_of_cards_is_one_call_to_the_render_service(client, world, designs):
    batch = await client.get(f"/api/v1/events/{world['event'].id}/idcards/batch.pdf", params={"record": "true"}, headers=world["club"])
    assert batch.status_code == 200, batch.text
    assert batch.headers["x-cards"] == "2" and designs.renders == [2]
    assert batch.content[:4] == b"%PDF"


async def test_the_cr80_card_is_still_a_custom_card(client, world, designs):
    from app.core.layout.starters import badge_starter

    st = badge_starter("card_cr80")
    body = dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"]))
    saved = await client.put(_designs(world, "badge", "/layout"), json=body, headers=world["club"])
    assert saved.status_code == 200 and saved.json()["active"] == "custom"
    card = await client.get(f"/api/v1/events/{world['event'].id}/idcards/{world['a'].id}.pdf", params={"record": "false"}, headers=world["club"])
    assert card.status_code == 200 and "Ananya" in pdf_text(card.content) and designs.renders == []


def test_a_photo_answer_is_read_only_from_the_events_own_folder():
    base = settings.S3_BASE_PATH
    # As a link to our bucket, or as the key itself.
    assert _event_key(f"{base}/events/7/registrations/a.jpg", 7).endswith("events/7/registrations/a.jpg")
    assert _event_key("events/7/registrations/a.jpg", 7) == "events/7/registrations/a.jpg"
    assert _event_key(f"{base}/events/8/registrations/a.jpg", 7) is None
    assert _event_key(f"{base}/users/profile_pic/someone-else.jpg", 7) is None
    assert _event_key("https://evil.example/a.jpg", 7) is None


def test_a_photo_is_cut_square_at_600_px_or_more():
    for size, side in (((900, 1200), 800), ((500, 700), 600), ((640, 640), 640)):
        out = Image.open(io.BytesIO(square(_png(*size))))
        assert out.size == (side, side) and out.format == "JPEG"
    assert square(b"not a picture") is None


# ─── Certificates: what is issued keeps its design (§6) ────────────────────


async def _issue(client, world):
    r = await client.post(f"/api/v1/events/{world['event'].id}/certificates/issue", json={"audience": "everyone"}, headers=world["club"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_certificate_keeps_the_design_it_was_issued_with_until_the_club_applies_another(client, session, world, designs):
    h, e = world["club"], world["event"].id
    assert (await _issue(client, world))["issued"] == 2
    await session.refresh(world["a"])
    reg = world["a"]
    assert reg.certificate_design["mode"] == "preset" and reg.certificate_design["preset"]["id"] == "certificate/07-swiss-minimal"

    pdf = await client.get(f"/api/v1/events/certificates/{reg.certificate_public_id}/pdf")
    assert pdf.status_code == 200, pdf.text
    design_id, v, _, _, _ = designs.fills[-1]
    assert design_id == "certificate/07-swiss-minimal"
    assert (v["recipient_name"], v["institute_name"], v["certificate_id"], v["course_title"], v["issue_city"]) == (
        "Ananya Iyer", "Yukthi", reg.certificate_no, "Build Night", "Kochi")
    assert v["verify_url"] == site_url(f"/verify/{reg.certificate_no}") == v["qr"] and v["grade"] == "" and v["signatory_name"] == ""

    # The club picks another design: what was issued does not change.
    assert (await client.put(_designs(world, "certificate", "/preset"), json={"id": "certificate/01-heritage"}, headers=h)).status_code == 200
    await client.get(f"/api/v1/events/certificates/{reg.certificate_public_id}/pdf")
    assert designs.fills[-1][0] == "certificate/07-swiss-minimal"
    image = await client.get(f"/api/v1/verify/certificates/{reg.certificate_no}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png" and designs.fills[-1][0] == "certificate/07-swiss-minimal"
    summary = (await client.get(f"/api/v1/events/{e}/certificates/summary", headers=h)).json()
    assert summary["other_design"] == 2

    # Only on the club's word, confirmed.
    unconfirmed = await client.post(f"/api/v1/events/{e}/certificates/apply-design", json={}, headers=h)
    assert unconfirmed.status_code == 422 and unconfirmed.json()["error_code"] == "CONFIRM_REQUIRED"
    applied = await client.post(f"/api/v1/events/{e}/certificates/apply-design", json={"confirm": True}, headers=h)
    assert applied.status_code == 200, applied.text
    assert applied.json() == {"updated": 2, "designs": {"default": "certificate/01-heritage", "attendee": "certificate/01-heritage", "speaker": "certificate/01-heritage", "staff": "certificate/01-heritage"}}
    await client.get(f"/api/v1/events/certificates/{reg.certificate_public_id}/pdf")
    assert designs.fills[-1][0] == "certificate/01-heritage"
    assert (await client.get(f"/api/v1/events/{e}/certificates/summary", headers=h)).json()["other_design"] == 0
    logged = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "certificates.apply_design"))
    assert logged is not None and logged.details["updated"] == 2

    # The bundle draws each from the design it keeps.
    before = len(designs.fills)
    bundle = await client.get(f"/api/v1/events/{e}/certificates/bundle.zip", headers=h)
    assert bundle.status_code == 200
    assert len(zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()) == 2
    assert [f[0] for f in designs.fills[before:]] == ["certificate/01-heritage"] * 2


@pytest.mark.parametrize("pin", [None, {"mode": "legacy"}])
async def test_a_certificate_issued_before_the_designs_is_drawn_as_it_was(client, session, world, designs, pin):
    h = world["club"]
    assert (await client.put(_designs(world, "certificate", "/layout"), json=_layout("certificate"), headers=h)).status_code == 200
    assert (await client.put(_designs(world, "certificate", "/preset"), json={"id": "certificate/07-swiss-minimal"}, headers=h)).status_code == 200
    reg = world["a"]
    reg.certificate_issued_at = datetime.now(timezone.utc)
    reg.certificate_design = pin
    await session.commit()

    pdf = await client.get(f"/api/v1/events/certificates/{reg.ticket_id}/pdf", headers=h)
    assert pdf.status_code == 200, pdf.text
    text = " ".join(pdf_text(pdf.content).split())
    assert "Ananya Iyer" in text and "CERTIFICATE OF ATTENDANCE" in text
    assert designs.fills == []


def test_only_owners_and_admins_put_the_current_design_on_issued_certificates():
    from app.api.club_access import policy

    path = "/api/v1/events/12/certificates/apply-design"
    assert policy.allows("admin", "POST", path) is True
    assert policy.allows("editor", "POST", path) is False and policy.allows("door", "POST", path) is False
    assert policy.can("admin")["redesign_issued_certificates"] and not policy.can("editor")["redesign_issued_certificates"]


# ─── The pieces ─────────────────────────────────────────────────────────────


class _Event:
    name = "Build Night"
    event_datetime = START
    duration = 3.0
    location_name = None
    is_online = True
    additional_details = []
    club = type("Club", (), {"name": "Yukthi", "location_name": None, "logo": None})()


def test_the_ticket_mapping_says_online_and_labels_roles():
    staff = Subject(full_name="Ravi", role="staff", ticket_code="MOA-1", ticket_label="VIP")
    m = ticket_values(_Event(), staff)
    assert m.values["venue"] == "Online" and m.values["ticket_type"] == "Staff" and m.qr_text == "MOA-1"


def test_the_badge_id_is_the_admission_number_and_the_answers_go_along():
    s = Subject(full_name="Meera Pillai", ticket_code="MOA-2", admission_no="BN-0007", answers={"field.team": "Null Pointers"})
    m = badge_values(_Event(), s, door="Kiosk 3", printed_at=START)
    assert m.values["badge_id"] == "BN-0007" and m.values["kiosk_id"] == "Kiosk 3" and m.values["checkin_time"] == "18:00"
    # For a binding {"source": "field", "value": "team"}, which A's fill reads.
    assert m.values["field.team"] == "Null Pointers" and m.qr_text == "MOA-2" and m.images == {"photo_url": ("photo", s)}


def test_a_certificate_encodes_its_public_check():
    s = Subject(full_name="Meera Pillai", certificate_no="CERT-7K3QF-X9PWM", position=2)
    m = certificate_values(_Event(), s, logo_key="logos/club.png")
    assert m.qr_text == site_url("/verify/CERT-7K3QF-X9PWM") == m.values["verify_url"]
    assert m.values["grade"] == m.values["rank_or_grade"] == "Second" and m.values["signatory_name"] == ""
    assert m.images["logo_src"] == ("key", "logos/club.png") and m.images["signature_src"] is None


def test_a_preset_is_checked_for_its_shape_and_kept_as_sent():
    assert presets.shape_problems("ticket", {"id": "ticket/01-dusk-horizon", "options": {"text": {"title": "Hi"}}}) == {}
    found = presets.shape_problems("badge", {"id": "ticket/01", "options": ["x"], "bindings": {"role": "ROLE"}, "accent": "blue"})
    assert set(found) == {"id", "options", "bindings.role", "accent"}
    sent = {"a": {"source": "auto"}, "b": {"source": "hidden"}, "c": {"source": "field", "value": "job_title"}}
    assert presets.normalised({"id": "ticket/x", "bindings": sent, "accent": "#abcdef"}) == {
        "id": "ticket/x", "options": {}, "bindings": sent, "accent": "#ABCDEF"}


async def test_the_admins_hear_of_it_at_most_once_every_ten_minutes(monkeypatch):
    sent = []

    async def send(kind, error):
        sent.append(kind)

    monkeypatch.setattr(engine, "_send_alert", send)
    monkeypatch.setattr(engine, "_last_alert", None)
    real_unavailable("ticket", "down")
    real_unavailable("badge", "down")
    import asyncio

    await asyncio.sleep(0)
    assert sent == ["ticket"]
    monkeypatch.setattr(engine, "_last_alert", engine._last_alert - engine.ALERT_EVERY - 1)
    real_unavailable("badge", "down")
    await asyncio.sleep(0)
    assert sent == ["ticket", "badge"]


async def test_the_ops_check_asks_the_render_service(client, designs, make_admin, mint_token, world):
    admin = auth_header(await mint_token(await make_admin(email="ops-render@example.com")))
    ok = await client.get("/api/v1/admin/ops/render", headers=admin)
    assert ok.status_code == 200 and ok.json()["ok"] is True and ok.json()["chromium"] == "up"
    designs.down = True
    down = (await client.get("/api/v1/admin/ops/render", headers=admin)).json()
    assert down["ok"] is False and down["status"] == "unreachable"
    assert (await client.get("/api/v1/admin/ops/render", headers=world["club"])).status_code == 403
