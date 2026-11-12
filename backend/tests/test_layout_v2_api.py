"""Editable designs (layout v2), wired in: feature-register/layout-v2-contract.md, "Q".

The layouts are drawn by P's real builder (app.core.layout_v2) and A's real
render client; only the render service itself is faked, at the HTTP level
(tests/render_transport_fake.py), so each test sees the page Chromium would
be sent. Saved v1 canvases and presets keep their own paths (V4).
"""
import copy
import io
from datetime import datetime, timezone

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.doc_presets import config as presets
from app.api.events.models import EventRegistrationsLink
from app.config import settings
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.renderer import pdf_text
from app.core.public_urls import site_url
from app.core.storage import objects
from tests.conftest import auth_header
from tests.legacy_layouts import certificate_starter, ticket_starter
from tests.render_transport_fake import install

# 18:00 in Kochi.
START = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
FORM = [
    {"key": "job_title", "label": "Job title", "field_type": "text", "required": False},
    {"key": "photo", "label": "Photo", "field_type": "image", "required": False},
]


@pytest.fixture
def render(monkeypatch):
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
    owner, club = await make_club(name="Yukthi", email="layout-v2@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0,
        location_name="Innovation Hall, Kochi", additional_details=FORM,
    )
    a = await make_user(email="ananya-v2@example.com")
    s = await make_user(email="sita-v2@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", ticket_label="VIP", additional_details={"job_title": "Product Designer"}, is_attended=True)
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker", is_attended=True)
    await session.commit()
    return {
        "event": event, "club_row": club, "a": r_a, "s": r_s, "user_a": a,
        "club": auth_header(await mint_token(owner)), "guest": auth_header(await mint_token(a)),
    }


def _designs(world, kind, path=""):
    return f"/api/v1/events/{world['event'].id}/designs/{kind}{path}"


# ─── Layouts made here (the starters are P's; these are the club's own) ─────


def _text(id_, text, x, y, w, h, size=24, **more):
    return {"id": id_, "name": id_, "kind": "text", "x": x, "y": y, "width": w, "height": h, "text": text, "size": size,
            "font": {"family": "Inter", "weight": 400, "style": "normal"}, **more}


def _qr(x, y, side):
    return {"id": "qr", "name": "QR code", "kind": "qr", "x": x, "y": y, "width": side, "height": side}


def _band(w):
    return {"id": "band", "name": "Band", "kind": "shape", "x": 0, "y": 0, "width": w, "height": 40, "fill": {"type": "solid", "color": "var:accent"}}


def ticket_v2(name="{{attendee_name}}", **extra) -> dict:
    return {
        "version": 2, "page": {"widthMm": 338.67, "heightMm": 158.75},
        "canvas": {"width": 1280, "height": 600, "background": {"type": "solid", "color": "#FFFFFF"}},
        "vars": {"accent": "#FF8A3D"},
        "layers": [
            _band(1280),
            _text("name", name, 60, 120, 900, 70, 48),
            _text("event", "{{event_name}} · {{date}} · {{event_time}}", 60, 220, 900, 40),
            _text("venue", "{{venue}}", 60, 280, 900, 40, showIf="venue"),
            _text("type", "{{ticket_type | upper}} · {{field.job_title}}", 60, 340, 900, 40),
            _text("code", "{{ticket_code}}", 60, 520, 600, 40),
            _qr(1020, 340, 220),
        ],
        **extra,
    }


def certificate_v2(title="Certificate of Participation") -> dict:
    return {
        "version": 2, "page": {"widthMm": 297, "heightMm": 210},
        "canvas": {"width": 1123, "height": 794, "background": {"type": "solid", "color": "#FFFFFF"}},
        "vars": {"accent": "#D90429"},
        "layers": [
            _text("title", title, 80, 80, 960, 60, 40),
            _text("name", "{{recipient_name}}", 80, 200, 960, 80, 56),
            _text("course", "for {{course_title}}, {{event_date}} in {{issue_city}}", 80, 320, 960, 40),
            _text("number", "{{certificate_id}} · {{verify_url}}", 80, 700, 700, 30, 16),
            {"id": "logo", "name": "Logo", "kind": "image", "x": 900, "y": 640, "width": 120, "height": 120, "source": "org_logo"},
            _qr(960, 60, 120),
        ],
    }


def badge_v2() -> dict:
    return {
        "version": 2, "page": {"widthMm": 101.6, "heightMm": 152.4},
        "canvas": {"width": 384, "height": 576, "background": {"type": "solid", "color": "#FFFFFF"}},
        "vars": {"accent": "#1F3BD8"},
        "layers": [
            _band(384),
            {"id": "photo", "name": "Photo", "kind": "image", "x": 132, "y": 60, "width": 120, "height": 120,
             "source": "guest_photo", "placeholder": "initials", "fit": "cover"},
            _text("first", "{{first_name}}", 20, 200, 344, 60, 40),
            _text("role", "{{role}} · {{access_level}}", 20, 270, 344, 30, 18),
            _text("event", "{{event_name}} · {{kiosk_id}}", 20, 310, 344, 30, 14),
            _qr(132, 400, 120),
        ],
    }


def v1_layout(kind="ticket"):
    s = ticket_starter("classic") if kind == "ticket" else certificate_starter("classic")
    return dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))


async def _save(client, world, kind, layout):
    r = await client.put(_designs(world, kind, "/layout"), json=layout, headers=world["club"])
    assert r.status_code == 200, r.text
    return r.json()


# ─── Every surface draws v2 ─────────────────────────────────────────────────


async def test_a_ticket_drawn_from_a_v2_layout(client, world, render, store):
    saved = await _save(client, world, "ticket", ticket_v2())
    assert saved["v2"]["from"] == "saved" and saved["v2"]["revision"] == 1 and saved["active"] == "custom"
    reg = world["a"]
    url = f"/api/v1/events/tickets/{reg.ticket_id}/pdf"
    pdf = await client.get(url, headers=world["guest"])
    assert pdf.status_code == 200, pdf.text
    assert render.renders == 1 and render.calls[0].form["paperWidth"] == "338.67mm"
    page = render.last_html()
    # P's page: the layout-v2 shell, fonts inline, every token filled.
    assert 'name="moa-design" content="layout-v2"' in page and "{{" not in page
    text = " ".join(pdf_text(pdf.content).split())
    assert "Ananya Iyer" in text and "Build Night · 20 Nov 2026 · 18:00" in text and "Innovation Hall, Kochi" in text
    assert "VIP · Product Designer" in text and reg.ticket_id in text
    # Kept (D8): a second download is the bucket's, under the same ETag.
    etag = pdf.headers["etag"]
    again = await client.get(url, headers=world["guest"])
    assert again.content == pdf.content and again.headers["etag"] == etag and render.renders == 1

    png = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/image", headers=world["guest"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    # Twice the canvas's 1280 CSS pixels (D2), from the same kept PDF.
    assert Image.open(io.BytesIO(png.content)).size[0] == 2560 and render.renders == 1

    # A new save is a new document; the old one is never served for it.
    changed = ticket_v2(name="Guest: {{attendee_name}}")
    assert (await _save(client, world, "ticket", changed))["v2"]["revision"] == 2
    third = await client.get(url, headers=world["guest"])
    assert third.headers["etag"] != etag and render.renders == 2 and "Guest: Ananya Iyer" in pdf_text(third.content)


async def test_a_speakers_ticket_and_the_values_function(client, world, render):
    await _save(client, world, "ticket", ticket_v2(name="{{name}} / {{role}} / {{first_name}} {{last_name}}"))
    pdf = await client.get(f"/api/v1/events/tickets/{world['s'].ticket_id}/pdf", headers=world["club"])
    assert pdf.status_code == 200
    text = " ".join(pdf_text(pdf.content).split())
    # v1's tokens and aliases ({{name}}; {{role}} is the ticket type on a ticket) and the designer's.
    assert "Sita Raman / Speaker / Sita Raman" in text and "SPEAKER" in text


async def test_with_the_render_service_down_the_classic_ticket_is_drawn_and_not_kept(client, world, render, store, _render_service_offline):
    # The club had a v1 canvas; a v2 layout replaced it and keeps it beside it.
    await _save(client, world, "ticket", v1_layout())
    await _save(client, world, "ticket", ticket_v2())
    render.down = True
    reg = world["a"]
    pdf = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200, pdf.text
    assert "etag" not in pdf.headers and not [k for k in store if k.startswith("docs/") or "/docs/" in k]
    assert _render_service_offline and _render_service_offline[0][0] == "ticket"
    # The v1 canvas it replaced, drawn by reportlab (D9).
    text = pdf_text(pdf.content)
    assert "Ananya Iyer" in text and "Build Night · 20 Nov" not in text
    png = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/image", headers=world["guest"])
    assert png.status_code == 200 and "etag" not in png.headers
    # Back up: drawn again, and kept.
    render.down = False
    pdf = await client.get(f"/api/v1/events/tickets/{reg.ticket_id}/pdf", headers=world["guest"])
    assert pdf.headers.get("etag") and "Build Night · 20 Nov" in " ".join(pdf_text(pdf.content).split())


async def _issue(client, world):
    r = await client.post(f"/api/v1/events/{world['event'].id}/certificates/issue", json={"audience": "everyone"}, headers=world["club"])
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_certificate_keeps_the_v2_layout_it_was_issued_with(client, session, world, render, store):
    store["clubs/logo.png"] = _png(60, 60)
    world["club_row"].logo = "clubs/logo.png"
    await session.commit()
    h, e = world["club"], world["event"].id
    await _save(client, world, "certificate", certificate_v2())
    assert (await _issue(client, world))["issued"] == 2
    await session.refresh(world["a"])
    reg = world["a"]
    pin = reg.certificate_design
    assert pin["mode"] == "layout" and pin["revision"] == 1 and pin["layout"]["layers"][0]["text"] == "Certificate of Participation"
    assert reg.certificate_design_version == 1

    pdf = await client.get(f"/api/v1/events/certificates/{reg.certificate_public_id}/pdf")
    assert pdf.status_code == 200, pdf.text
    text = " ".join(pdf_text(pdf.content).split())
    assert "Certificate of Participation" in text and "Ananya Iyer" in text
    assert "for Build Night, 20 Nov 2026 in Kochi" in text and f"{reg.certificate_no} · {site_url('/verify/' + reg.certificate_no)}" in text
    # The club's logo is in the page, as a data: URI (the renderer fetches nothing).
    assert 'src="data:image/png;base64,' in render.last_html()

    # The club edits the layout: what was issued does not change (contract §6).
    await _save(client, world, "certificate", certificate_v2(title="Certificate of Excellence"))
    again = await client.get(f"/api/v1/events/certificates/{reg.certificate_public_id}/pdf")
    assert "Certificate of Participation" in pdf_text(again.content)
    image = await client.get(f"/api/v1/verify/certificates/{reg.certificate_no}/image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    summary = (await client.get(f"/api/v1/events/{e}/certificates/summary", headers=h)).json()
    assert summary["other_design"] == 2

    applied = await client.post(f"/api/v1/events/{e}/certificates/apply-design", json={"confirm": True}, headers=h)
    assert applied.status_code == 200 and applied.json()["updated"] == 2
    after = await client.get(f"/api/v1/events/certificates/{reg.certificate_public_id}/pdf")
    assert "Certificate of Excellence" in pdf_text(after.content)
    assert (await client.get(f"/api/v1/events/{e}/certificates/summary", headers=h)).json()["other_design"] == 0
    await session.refresh(reg)
    assert reg.certificate_design["revision"] == 2 and reg.certificate_design_version == 2


async def test_a_certificate_issued_on_a_v1_canvas_keeps_it_after_a_v2_save(client, session, world, render):
    await _save(client, world, "certificate", v1_layout("certificate"))
    await _issue(client, world)
    await session.refresh(world["a"])
    assert world["a"].certificate_design == {"mode": "custom", "version": 1}
    await _save(client, world, "certificate", certificate_v2())
    pdf = await client.get(f"/api/v1/events/certificates/{world['a'].certificate_public_id}/pdf")
    text = " ".join(pdf_text(pdf.content).split())
    # The v1 canvas it was issued on, kept beside the v2 layout: reportlab, no render call.
    assert "Ananya Iyer" in text and "CERTIFICATE OF ATTENDANCE" in text and render.renders == 0


async def test_a_card_a_batch_and_the_kiosk_print_v2(client, session, world, render):
    h, e = world["club"], world["event"].id
    await _save(client, world, "badge", badge_v2())
    card = await client.get(f"/api/v1/events/{e}/idcards/{world['a'].id}.pdf", params={"source": "manual"}, headers=h)
    assert card.status_code == 200, card.text
    text = " ".join(pdf_text(card.content).split())
    assert "Ananya" in text and "ATTENDEE · VIP" in text
    # No photo: the initials tile, never an empty frame.
    assert "moa-initials" in render.last_html() and ">AI<" in render.last_html()

    batch = await client.get(f"/api/v1/events/{e}/idcards/batch.pdf", headers=h)
    assert batch.status_code == 200 and batch.headers["x-cards"] == "2"
    # Both cards in one call to the render service, a page each (by name: Ananya, then Sita).
    assert render.calls[-1].pages == 2 and len(render.calls) == 2
    page = render.last_html()
    assert page.index("Ananya") < page.index("Sita") and "SPEAKER" in page
    import pypdfium2 as pdfium

    assert len(pdfium.PdfDocument(batch.content)) == 2

    render.down = True
    down = await client.get(f"/api/v1/events/{e}/idcards/{world['a'].id}.pdf", params={"source": "manual"}, headers=h)
    assert down.status_code == 503 and down.json()["error_code"] == "RENDER_UNAVAILABLE"


async def test_the_kiosk_colour_is_the_layouts_accent(session, world):
    from app.api.kiosk.service import _brand_primary

    style = {"layout": {**badge_v2(), "vars": {"accent": "#1F3BD8"}}}
    assert _brand_primary(style, world["club_row"]) == "#1F3BD8"


# ─── Saved v1 canvases and presets ──────────────────────────────────────────


async def test_a_saved_v1_canvas_still_renders_as_before(client, world, render):
    saved = await _save(client, world, "ticket", v1_layout())
    assert saved["version"] == 1 and saved["v2"]["from"] == "v1"
    pdf = await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200 and "Ananya Iyer" in pdf_text(pdf.content)
    # reportlab, as it always was: nothing went to the render service.
    assert render.renders == 0


async def test_a_saved_preset_still_renders(client, world, monkeypatch):
    from tests.doc_designs_fake import install as install_designs

    designs = install_designs(monkeypatch)
    chosen = await client.put(_designs(world, "ticket", "/preset"), json={"id": "ticket/01-dusk-horizon"}, headers=world["club"])
    assert chosen.status_code == 200 and chosen.json()["active"] == "preset"
    pdf = await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200
    design_id, values, _, _, _ = designs.fills[-1]
    assert design_id == "ticket/01-dusk-horizon" and values["attendee_name"] == "Ananya Iyer"
    # The one values function: the canvas's tokens ride along with the designer's.
    assert values["ticket_code"] == world["a"].ticket_id and values["event_time"] == "18:00"


# ─── The editor ─────────────────────────────────────────────────────────────


async def test_the_editor_reads_everything_and_draws_nothing(client, world, render):
    t = (await client.get(_designs(world, "ticket", "/layout"), headers=world["club"])).json()
    v2 = t["v2"]
    assert render.renders == 0
    assert v2["from"] in ("default", "none") and v2["revision"] == 0
    assert any(f["family"] == "Inter" and 700 in f["weights"] for f in v2["fonts"])
    keys = {tok["key"] for tok in v2["tokens"]}
    assert {"attendee_name", "ticket_code", "app_name", "gate", "field.job_title"} <= keys
    # The sample guest, for the editor's own builder.
    assert v2["values"]["attendee_name"] == "Aisha Rahman" and v2["context"]["qr"] == "MOA-SAMPLE-0041"
    assert set(v2["context"]["colors"]) == {"primary", "secondary", "ticketType"}
    assert set(v2["images"]) == {"org_logo", "signature_1", "signature_2", "event_icon", "uploads"}
    # The old canvas editor's keys are as they were.
    assert any(layer["kind"] == "qr" for layer in t["layers"]) and t["starters"] == {}
    agenda = (await client.get(_designs(world, "agenda", "/layout"), headers=world["club"])).json()
    assert agenda["v2"] is None


async def test_saving_checks_and_refuses(client, world, store):
    h, url = world["club"], _designs(world, "ticket", "/layout")
    bad_font = ticket_v2()
    bad_font["layers"][1]["font"] = {"family": "Comic Sans", "weight": 400, "style": "normal"}
    r = await client.put(url, json=bad_font, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_LAYOUT"
    assert "layers.1.font.family" in r.json()["errors"]["fields"]

    script = ticket_v2()
    script["layers"].append({"id": "art", "name": "Art", "kind": "svg", "x": 0, "y": 0, "width": 100, "height": 100,
                             "markup": '<svg viewBox="0 0 10 10"><script>alert(1)</script><rect width="10" height="10"/></svg>'})
    r = await client.put(url, json=script, headers=h)
    assert r.status_code == 422 and "layers.7.markup" in r.json()["errors"]["fields"]
    assert r.json()["errors"]["layout"][0]["code"] == "svg_unsafe"
    handler = copy.deepcopy(script)
    handler["layers"][7]["markup"] = '<svg viewBox="0 0 10 10"><rect width="10" height="10" onclick="x()"/></svg>'
    assert (await client.put(url, json=handler, headers=h)).status_code == 422
    link = copy.deepcopy(script)
    link["layers"][7]["markup"] = '<svg viewBox="0 0 10 10"><image href="https://evil.test/a.png" width="10" height="10"/></svg>'
    assert (await client.put(url, json=link, headers=h)).status_code == 422

    many = ticket_v2()
    many["layers"] += [_text(f"t{i}", "x", 0, 0, 10, 10) for i in range(150)]
    r = await client.put(url, json=many, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_LAYOUT"

    elsewhere = ticket_v2()
    elsewhere["layers"].append({"id": "pic", "name": "Pic", "kind": "image", "x": 0, "y": 0, "width": 50, "height": 50,
                                "source": "upload", "imageKey": "events/999999/layout/image-x.png"})
    r = await client.put(url, json=elsewhere, headers=h)
    assert r.status_code == 422 and "layers.7.imageKey" in r.json()["errors"]["fields"]

    no_qr = ticket_v2()
    no_qr["layers"] = [layer for layer in no_qr["layers"] if layer["kind"] != "qr"]
    assert (await client.put(url, json=no_qr, headers=h)).status_code == 422

    # Nothing was saved by any of those.
    assert (await client.get(url, headers=h)).json()["isOwn"] is False

    # An editor's own attributes are cleaned, saved, and said.
    tidy = ticket_v2()
    tidy["layers"].append({"id": "art", "name": "Art", "kind": "svg", "x": 0, "y": 0, "width": 100, "height": 100,
                           "markup": '<svg viewBox="0 0 10 10" inkscape:label="x"><rect width="10" height="10" fill="#FF0000"/></svg>'})
    ok = await client.put(url, json=tidy, headers=h)
    assert ok.status_code == 200, ok.text
    assert [w["code"] for w in ok.json()["warnings"]] == ["svg_cleaned"]
    assert "inkscape" not in ok.json()["v2"]["layout"]["layers"][7]["markup"]


async def test_saving_v2_clears_the_preset_and_keeps_the_v1_canvas(client, session, world, monkeypatch):
    from tests.doc_designs_fake import install as install_designs

    install_designs(monkeypatch)
    h = world["club"]
    await _save(client, world, "ticket", v1_layout())
    await _save(client, world, "ticket", v1_layout())
    assert (await client.put(_designs(world, "ticket", "/preset"), json={"id": "ticket/01-dusk-horizon"}, headers=h)).status_code == 200
    saved = await _save(client, world, "ticket", ticket_v2())
    assert saved["active"] == "custom" and saved["preset"] is None and saved["v2"]["from"] == "saved"
    # The v1 canvas had been saved twice ("version": 2): the revision goes on from it.
    assert saved["v2"]["revision"] == 3 and saved["version"] == 2
    from app.api.ticket_templates.models import TicketTemplates

    row = await session.scalar(select(TicketTemplates).where(TicketTemplates.event_id == world["event"].id, TicketTemplates.is_deleted == False))  # noqa: E712
    await session.refresh(row)
    assert presets.is_v2(row.config["layout"]) and row.config["layout"]["version"] == 2
    assert not presets.is_v2(row.config["layout_v1"]) and row.config["layout_v1"]["version"] == 2
    assert "preset" not in row.config and row.config["layout_revision"] == 3


async def test_the_preview_is_html_for_the_iframe_or_one_exact_png(client, world, render):
    h, url = world["club"], _designs(world, "ticket", "/layout/preview")
    html = await client.post(url + "?format=html", json={"layout": ticket_v2(), "ticket_id": world["a"].ticket_id}, headers=h)
    assert html.status_code == 200, html.text
    page = html.json()["html"]
    assert "Ananya Iyer" in page and "/static/doc-fonts/" in page and render.renders == 0
    sample = await client.post(url + "?format=html", json=ticket_v2(), headers=h)
    assert "Aisha Rahman" in sample.json()["html"]
    png = await client.post(url + "?format=png", json={"layout": ticket_v2()}, headers=h)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).size[0] == 2560 and render.renders == 1
    bad = ticket_v2()
    bad["layers"][1]["font"]["family"] = "Nope Sans"
    assert (await client.post(url + "?format=png", json={"layout": bad}, headers=h)).status_code == 422
    assert (await client.post(url, json={"layout": ticket_v2(), "ticket_id": "MOA-NOPE"}, headers=h)).status_code == 404
    render.down = True
    down = await client.post(url + "?format=png", json={"layout": ticket_v2()}, headers=h)
    assert down.status_code == 503 and down.json()["error_code"] == "RENDER_UNAVAILABLE"
    # A v1 canvas previews as it did.
    v1 = await client.post(url, json=v1_layout(), headers=h)
    assert v1.status_code == 200 and v1.headers["content-type"] == "image/png"
    # Previewing saves nothing.
    assert (await client.get(_designs(world, "ticket", "/layout"), headers=h)).json()["isOwn"] is False


async def test_the_clubs_default_is_edited_the_same_way(client, world, render):
    h = world["club"]
    saved = await client.put("/api/v1/clubs/designs/badge/layout", json=badge_v2(), headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["isOwn"] is True and saved.json()["v2"]["from"] == "saved"
    on_event = (await client.get(_designs(world, "badge", "/layout"), headers=h)).json()
    assert on_event["v2"]["from"] == "saved" and on_event["isOwn"] is False
    preview = await client.post("/api/v1/clubs/designs/badge/layout/preview?format=html", json=badge_v2(), headers=h)
    assert preview.status_code == 200 and "Sample event" in preview.json()["html"] and "Aisha" in preview.json()["html"]
    card = await client.get(f"/api/v1/events/{world['event'].id}/idcards/{world['a'].id}.pdf", params={"source": "manual"}, headers=h)
    assert card.status_code == 200 and "Ananya" in pdf_text(card.content)


async def test_starters_are_copied_on_use(client, world, monkeypatch):
    from app.core import layout_v2
    from app.core.layout_v2 import gallery

    monkeypatch.setattr(gallery, "starter_ids", lambda kind: [f"{kind}/blank"] if kind in ("ticket", "certificate", "badge") else [])
    monkeypatch.setattr(layout_v2, "starter_ids", gallery.starter_ids)
    monkeypatch.setattr(gallery, "starter", lambda sid, wording=None: ticket_v2() if sid == "ticket/blank" else badge_v2())
    monkeypatch.setattr(layout_v2, "starter", gallery.starter)
    h = world["club"]
    got = await client.post(_designs(world, "ticket", "/layout/starters/ticket/blank"), headers=h)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == "ticket/blank" and got.json()["layout"]["version"] == 2
    # Nothing is saved until the club saves.
    assert (await client.get(_designs(world, "ticket", "/layout"), headers=h)).json()["isOwn"] is False
    assert (await client.post(_designs(world, "ticket", "/layout/starters/badge/blank"), headers=h)).status_code == 404
    assert (await client.post(_designs(world, "ticket", "/layout/starters/ticket/99-nope"), headers=h)).status_code == 404
    assert (await client.post("/api/v1/clubs/designs/badge/layout/starters/badge/blank", headers=h)).status_code == 200


async def test_a_ticket_with_no_saved_design_is_the_default_designs_starter(client, world, render, monkeypatch):
    """V4: the default is the default design's v2 starter, so the editor and
    the issued ticket agree. (Here the starter is a stand-in; the real ones
    are filled in tests/test_layout_v2_integration.py.)"""
    monkeypatch.setattr(presets, "default_layout", lambda kind: ticket_v2() if kind == "ticket" else None)
    t = (await client.get(_designs(world, "ticket", "/layout"), headers=world["club"])).json()
    assert t["v2"]["from"] == "default" and t["v2"]["layout"]["layers"][1]["text"] == "{{attendee_name}}" and t["preset"] is None
    pdf = await client.get(f"/api/v1/events/tickets/{world['a'].ticket_id}/pdf", headers=world["guest"])
    assert pdf.status_code == 200 and render.renders == 1 and "Ananya Iyer" in pdf_text(pdf.content)
    # Without the render service it would be the classic HTML ticket (D9: wkhtmltopdf, not in
    # this suite's machine), and no preset is involved.
    from app.api.ticket_templates.service import system_default_config

    default = system_default_config()
    assert presets.layout_v2_of(default) is not None and "preset" not in default
    assert "layout" not in presets.classic(default) and "preset" not in presets.classic(default)
    # The editor's first save of it forks the event's own design from exactly that.
    saved = await _save(client, world, "ticket", t["v2"]["layout"])
    assert saved["v2"]["from"] == "saved"
    assert [(x["id"], x.get("text")) for x in saved["v2"]["layout"]["layers"]] == [(x["id"], x.get("text")) for x in t["v2"]["layout"]["layers"]]


# ─── Opening a v1 canvas or a preset converts it (P's converters) ───────────


async def test_opening_a_v1_canvas_or_a_preset_converts_it(client, world):
    """The real designs (A's catalogue) and P's converters: nothing is drawn."""
    from app.core import layout_v2

    h = world["club"]
    await _save(client, world, "ticket", v1_layout())
    opened = (await client.get(_designs(world, "ticket", "/layout"), headers=h)).json()["v2"]
    try:
        layout_v2.from_v1(v1_layout())
    except NotImplementedError:
        pytest.skip("P's converter (from_v1, from_preset) has not landed on this branch yet")
    assert opened["from"] == "v1" and presets.is_v2(opened["layout"])
    names = " ".join(layer.get("text", "") for layer in opened["layout"]["layers"])
    assert "{{attendee_name}}" in names or "{{name}}" in names
    # It still prints as the v1 canvas until the club saves it in the editor (V4).
    assert (await client.get(_designs(world, "ticket", "/layout"), headers=h)).json()["version"] == 1

    chosen = await client.put(_designs(world, "certificate", "/preset"), json={"id": "certificate/01-heritage", "accent": "#123456"}, headers=h)
    assert chosen.status_code == 200, chosen.text
    preset = chosen.json()["v2"]
    # Its starter with the preset's accent applied (P's from_preset); it still prints as the preset.
    assert preset["from"] == "preset" and presets.is_v2(preset["layout"]) and preset["layout"]["vars"]["accent"] == "#123456"
    assert chosen.json()["active"] == "preset"


# ─── The pieces ─────────────────────────────────────────────────────────────


def test_a_v1_canvas_saved_twice_is_not_a_v2_layout():
    v1 = {**v1_layout(), "version": 2}
    assert not presets.is_v2(v1) and presets.is_v2(ticket_v2())
    assert presets.layout_v1_of({"layout": v1}) is v1 and presets.layout_v2_of({"layout": v1}) is None


def test_the_classic_design_is_the_kept_v1_canvas_or_the_template():
    v1 = {**v1_layout(), "version": 1}
    with_v1 = presets.with_layout({"layout": v1, "accent_color": "#123456", "preset": {"id": "ticket/x"}}, {**ticket_v2()})
    assert with_v1["layout_v1"] is v1 and "preset" not in with_v1 and with_v1["layout_revision"] == 2
    assert presets.classic(with_v1) == {"layout": v1, "accent_color": "#123456"}
    plain = presets.with_layout({"accent_color": "#123456"}, ticket_v2())
    assert presets.classic(plain) == {"accent_color": "#123456"} and plain["layout_revision"] == 1


def test_a_v1_canvas_saved_over_a_v2_layout_counts_on_from_its_revision():
    """The old editor saving over a v2 layout (or over the default starter a
    fork copied in): one past the revision, never the schema's 2 plus one."""
    over_default = presets.with_v1_layout({"layout": ticket_v2()}, v1_layout())
    assert over_default["layout"]["version"] == 1 and "layout_revision" not in over_default
    over_saved = presets.with_v1_layout(presets.with_layout(presets.with_layout({}, ticket_v2()), ticket_v2()), v1_layout())
    assert over_saved["layout"]["version"] == 3 and not presets.is_v2(over_saved["layout"]) and "layout_v1" not in over_saved


def test_the_values_function_merges_both_vocabularies():
    from types import SimpleNamespace

    from app.api.doc_presets.values import document_values, sample_subject

    club = SimpleNamespace(name="Yukthi", location_name="Kochi", logo=None)
    event = SimpleNamespace(id=7, name="Build Night", club=club, club_id=1, additional_details=FORM, location_name=None,
                            is_online=False, event_datetime=START, duration=3.0, admission_prefix=None, admission_digits=4,
                            admission_include_code=True)
    subject = sample_subject(event, "badge")
    subject.full_name = "Meera"
    m = document_values("badge", event, subject)
    v = m.values
    # The designer's meanings where a name is both; the canvas's for the rest.
    assert (v["first_name"], v["last_name"], v["role"], v["initials"]) == ("Meera", "", "ATTENDEE", "ME")
    assert v["attendee_name"] == "Meera" and v["event_time"] == "18:00" and v["venue"] == "" and v["event_venue"] == ""
    assert v["field.job_title"] == "‹Job title›" and m.qr_text == "MOA-SAMPLE-0041"
    c = document_values("certificate", event, sample_subject(event, "certificate")).values
    assert c["ticket_code"] == "CERT-7K3QF-X9PWM" == c["certificate_id"] and c["placing"] == "First" == c["rank_or_grade"]


def _png(w, h, colour=(40, 120, 200)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(b, format="PNG")
    return b.getvalue()


async def test_issued_registrations_hold_the_snapshot_once_each(session, world, client, render):
    """The pin is the layout itself (no reference to a row that may change)."""
    await _save(client, world, "certificate", certificate_v2())
    await _issue(client, world)
    rows = (await session.scalars(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == world["event"].id))).all()
    for r in rows:
        await session.refresh(r)
    assert {r.certificate_design["mode"] for r in rows} == {"layout"}
    assert all(r.certificate_design["layout"] == rows[0].certificate_design["layout"] for r in rows)


async def test_a_layer_shown_only_with_the_clubs_logo(client, session, world, store):
    """showIf may name a picture (logo_src): the page asks whether it is there,
    so the wiring reads it even when no image layer shows it."""
    layout = certificate_v2()
    layout["layers"] = [layer for layer in layout["layers"] if layer["kind"] != "image"]
    layout["layers"].append(_text("seal", "Sealed by the club", 80, 620, 400, 30, showIf="logo_src"))
    url = _designs(world, "certificate", "/layout/preview?format=html")
    without = await client.post(url, json={"layout": layout}, headers=world["club"])
    assert without.status_code == 200, without.text
    assert "Sealed by the club" not in without.json()["html"]
    store["clubs/logo.png"] = _png(40, 40)
    world["club_row"].logo = "clubs/logo.png"
    await session.commit()
    with_logo = await client.post(url, json={"layout": layout}, headers=world["club"])
    assert "Sealed by the club" in with_logo.json()["html"]
