"""Layout v2, the one editable design model (layout-v2 contract): the schema
and its validation, the SVG sanitiser, the builder (escaping, showIf, fills,
the accent, fonts), v1 and preset layouts converted, the 35 starters, and
the conformance fixtures the TypeScript builder is held to.

No network, no Chromium: the render service is a fake transport. (The
starters' look is proved by scripts/convert_designs_v2.py --parity.)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.core import doc_designs as dd
from app.core import layout_v2 as v2
from app.core.doc_designs import qr as dqr
from app.core.doc_designs import render as drender
from app.core.layout_v2 import build, gallery, sanitize, schema, vocabulary
from app.core.layout_v2.schema import MAX_LAYERS
from tests.layout_v2_cases import CASES, normalise

FIXTURES = Path(__file__).parent / "fixtures" / "layout_v2"
PNG = CASES["images"]["context"]["images"]["upload:uploads/a.png"]


def lay(layers=(), **more) -> dict:
    out = {"version": 2, "page": {"widthMm": 101.6, "heightMm": 152.4}, "canvas": {"width": 384, "height": 576},
           "layers": list(layers)}
    out.update(more)
    return out


def text(id="t", t="{{first_name}}", **more) -> dict:
    return {"id": id, "name": id, "kind": "text", "x": 10, "y": 10, "width": 200, "height": 30, "text": t, **more}


QR = {"id": "qr", "name": "QR code", "kind": "qr", "x": 250, "y": 440, "width": 108, "height": 108}


def codes(problems, level=None) -> set[str]:
    return {p["code"] for p in problems if level is None or p["level"] == level}


# ─── Schema ──────────────────────────────────────────────────────────────────


async def test_render_goes_through_the_render_service(session, monkeypatch):
    # `session`: an async test that opens no database first in a run sets the
    # suite's loop up apart from the session's engine, and every database test
    # after it fails (tests/conftest.py, tests/test_cohost_routes.py).
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.read().decode("utf-8", "replace"))
        return httpx.Response(200, content=b"%PDF-1.7\n")

    monkeypatch.setattr(drender, "_transport", httpx.MockTransport(answer))
    layout = v2.starter("badge/06-thermal-mono")
    assert await v2.render_pdf(layout, {"first_name": "Meera"}, {"qr": "TK-1"}, kind="badge") == b"%PDF-1.7\n"
    assert re.search(r'name="paperWidth"\r\n\r\n101.6mm\r\n', seen[0])
    await v2.render_pages([(layout, {"first_name": f"P{i}"}, {"qr": "TK"}) for i in range(3)], kind="badge")
    assert seen[1].count('<div class="moa-page"') == 3 and seen[1].count('<div class="moa-doc"') == 3


def test_a_layout_parses_and_dumps_camel_case():
    layout = v2.parse(lay([text(letterSpacing=0.1, maxLines=2, showIf="first_name last_name"), QR],
                          vars={"accent": "#ff8a3d"}))
    out = v2.dump(layout)
    assert out["vars"]["accent"] == "#FF8A3D"
    t = out["layers"][0]
    assert t["letterSpacing"] == 0.1 and t["maxLines"] == 2 and t["showIf"] == "first_name last_name"
    assert v2.parse(out) == layout
    assert v2.is_v2(out) and not v2.is_v2({"canvas": {}, "layers": []})


@pytest.mark.parametrize("bad", [
    {"version": 1},
    {"canvas": {"width": 299, "height": 576}},
    {"canvas": {"width": 384, "height": 3001}},
    {"layers": [text(color="red")]},
    {"layers": [text(color="var:other")]},
    {"layers": [text(id="has space")]},
    {"layers": [{**QR, "dark": "#112233"}]},
    {"layers": [{"id": "s", "name": "s", "kind": "shape", "x": 0, "y": 0, "width": 9, "height": 9, "shape": "path",
                 "path": "M0 0 <script>"}]},
    {"layers": [{"id": "s", "name": "s", "kind": "shape", "x": 0, "y": 0, "width": 9, "height": 9, "shape": "path"}]},
    {"layers": [text(showIf="a; b")]},
    {"layers": [text(spans=[{"text": "a", "weight": 1000}])]},
    {"layers": [text(fill="none")]},
    {"layers": [text(str(i)) for i in range(MAX_LAYERS + 1)]},
    {"vars": {"accent": "var:accent"}},
])
def test_a_wrong_shape_is_refused(bad):
    with pytest.raises(ValidationError):
        v2.parse({**lay(), **bad})
    assert codes(v2.problems({**lay(), **bad})) == {"schema"}


def test_spans_are_the_text_and_min_size_never_exceeds_size():
    t = v2.parse(lay([text(t="ignored", spans=[{"text": "for "}, {"text": "{{course_title}}", "weight": 600}],
                           size=18, minSize=40)])).layers[0]
    assert t.text == "for {{course_title}}" and t.min_size == 18
    assert v2.parse(lay([text(spans=[])])).layers[0].spans is None


def test_a_shadow_is_one_or_a_list_and_padding_one_or_four():
    a, b = v2.parse(lay([text(shadow={"x": 1, "color": "#000000"}, padding=8, radius=4),
                         text("u", shadow=[{"blur": 4, "color": "#FFFFFF"}, {"spread": 2, "inset": True, "color": "#000000"}])])).layers
    assert len(a.shadows()) == 1 and a.padding == [8, 8, 8, 8] and a.radius == [4, 4, 4, 4]
    assert [s.inset for s in b.shadows()] == [False, True]


# ─── problems() ──────────────────────────────────────────────────────────────


def test_a_good_badge_has_no_problems():
    assert v2.problems(lay([text(font={"family": "Space Grotesk", "weight": 700}), QR]), "badge") == []


def test_the_canvas_must_be_the_pages_shape():
    p = v2.problems(lay([{**QR, "y": 300}], canvas={"width": 384, "height": 500}), "badge")
    assert codes(p) == {"page_shape"}
    assert v2.problems(lay([QR], canvas={"width": 384, "height": 570}), "badge") == []  # within 1.5%


def test_unknown_fonts_name_the_nearest_and_faces_must_exist():
    p = v2.problems(lay([text(font={"family": "Space Grotsk"}), QR]), "badge")
    (f,) = p
    assert f["code"] == "unknown_font" and "Space Grotesk" in f["message"] and f["field"] == "layers.0.font.family"
    (w,) = v2.problems(lay([text(font={"family": "Inter", "weight": 900}), QR]), "badge")
    assert w["code"] == "font_face" and "400, 500, 600, 700" in w["message"]
    (it,) = v2.problems(lay([text(font={"family": "Saira Condensed", "weight": 800, "style": "italic"}), QR]), "badge")
    assert it["code"] == "font_slanted" and it["level"] == "warning"
    for v1 in ("Inter", "Google Sans", "Roboto Mono", "DM Serif Display", "Bebas Neue"):
        weights = {"Google Sans": 500, "Roboto Mono": 700}.get(v1, 400)
        assert v2.problems(lay([text(font={"family": v1, "weight": weights}), QR]), "badge") == [], v1


def test_tokens_are_checked_for_the_kind():
    p = v2.problems(lay([text(t="{{zone}} {{frist_name}}"), QR]), "ticket")
    msgs = " ".join(x["message"] for x in p)
    assert codes(p) == {"template"} and "{{zone}} is not a token" in msgs and "Did you mean {{first_name}}" in msgs
    assert v2.problems(lay([text(t="{{zone}} {{field.t_shirt}} {% if role %}{{role | upper}}{% endif %}"), QR]), "badge") == []
    assert codes(v2.problems(lay([text(showIf="nope"), QR]), "badge")) == {"unknown_token"}
    assert v2.problems(lay([text(showIf="logo_src|photo_url"), QR]), "badge") == []


def test_the_qr_rules():
    assert codes(v2.problems(lay([text()]), "ticket")) == {"qr_missing"}
    assert v2.problems(lay([text()]), "certificate") == []
    assert "qr_extra" in codes(v2.problems(lay([QR, {**QR, "id": "qr2"}]), "badge"))
    assert codes(v2.problems(lay([{**QR, "hidden": True}]), "badge")) == {"qr_hidden"}
    assert codes(v2.problems(lay([{**QR, "width": 30, "height": 30}]), "badge"), "error") == {"qr_small"}
    assert codes(v2.problems(lay([{**QR, "width": 42, "height": 42}]), "badge")) == {"qr_small"}  # 11 mm: a warning
    assert codes(v2.problems(lay([{**QR, "x": 300}]), "badge")) == {"qr_outside"}


def test_other_problems():
    p = v2.problems(lay([
        text(), text(), QR,
        {"id": "u", "name": "Upload", "kind": "image", "source": "upload", "x": 0, "y": 0, "width": 9, "height": 9},
        {"id": "p", "name": "Photo", "kind": "image", "source": "guest_photo", "preprinted": True, "x": 0, "y": 0,
         "width": 9, "height": 9},
        text("far", x=900),
        text("acc", color="var:accent"),
        {"id": "s", "name": "S", "kind": "svg", "x": 0, "y": 0, "width": 9, "height": 9, "markup": "<div>no svg</div>"},
        {"id": "s2", "name": "S2", "kind": "svg", "x": 0, "y": 0, "width": 9, "height": 9,
         "markup": "<svg viewBox='0 0 1 1' onload='x()'><rect width='1' height='1'/></svg>"},
    ]), "badge")
    assert {"duplicate_id", "image_missing", "photo_preprinted", "off_canvas", "accent_missing", "svg_unsafe",
            "svg_cleaned"} <= codes(p)
    assert {x["code"]: x["level"] for x in p}["svg_cleaned"] == "warning"


# ─── The SVG sanitiser ───────────────────────────────────────────────────────


def test_the_sanitiser_refuses_scripts_handlers_and_outside_references():
    dirty = ('<svg width="20" height="10" onload="alert(1)" style="position:absolute">'
             '<script>alert(1)</script><foreignObject><div>x</div></foreignObject><style>@import url(http://x)</style>'
             '<a href="javascript:alert(1)"><rect width="1" height="1"/></a>'
             '<rect width="5" height="5" fill="url(#ok)" onclick="x()" style="fill: url(http://evil/x); stroke: #000"/>'
             '<use href="http://evil/s.svg#a"/><use xlink:href="#local"/>'
             '<image href="https://evil/x.png"/><image href="' + PNG + '"/>'
             '<linearGradient id="ok"><stop offset="0" stop-color="#FFF"/></linearGradient></svg>')
    clean = sanitize.sanitize(dirty)
    for bad in ("script", "onload", "onclick", "foreignObject", "<style", "javascript", "evil", "position"):
        assert bad not in clean, bad
    assert clean.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 10">')
    assert 'fill="url(#ok)"' in clean and 'href="#local"' in clean and PNG in clean and "<linearGradient" in clean
    assert sanitize.sanitize(clean) == clean and sanitize.problems_of(clean) == []
    assert len(sanitize.problems_of(dirty)) >= 8
    with pytest.raises(sanitize.UnsafeSvg):
        sanitize.sanitize("<div></div>")
    with pytest.raises(sanitize.UnsafeSvg):
        sanitize.sanitize("<svg><rect/></svg>")  # no viewBox, no size


def test_every_starter_svg_is_already_clean():
    for kind in ("ticket", "certificate", "badge"):
        for s in v2.starters(kind):
            for layer in s["layout"]["layers"]:
                if layer["kind"] == "svg":
                    assert sanitize.problems_of(layer["markup"]) == [], (s["id"], layer["id"])


# ─── The builder ─────────────────────────────────────────────────────────────


def test_values_are_escaped_and_tokens_filled():
    body = v2.build_body(lay([text(t="{{event_name}} {{first_name | upper}}")]),
                         {"event_name": '<b>"A" & B</b>', "first_name": "meera"}, kind="badge")
    assert "&lt;b&gt;&quot;A&quot; &amp; B&lt;/b&gt; MEERA" in body and "<b>" not in body


def test_show_if_and_empty_text_leave_layers_out():
    layers = [text("a", showIf="first_name"), text("b", showIf="zone"), text("c", showIf="zone|first_name"),
              text("d", showIf="zone first_name"), text("e", t="{{zone}}"), text("f", hidden=True),
              text("g", showIf="photo_url"), text("h", showIf="logo_src")]
    body = v2.build_body(lay(layers), {"first_name": "Meera", "zone": "  "}, kind="badge",
                         context={"images": {"guest_photo": PNG}})
    assert re.findall(r'data-layer="(\w)"', body) == ["a", "c", "g"]


def test_fills_colours_and_the_accent():
    fills = [{"type": "solid", "color": "var:accent"},
             {"type": "linear", "angle": 90, "stops": [{"offset": 0, "color": "brand.primary"}, {"offset": 1, "color": "#00000080"}]},
             {"type": "radial", "cx": 0.5, "cy": 0, "stops": [{"offset": 0, "color": "ticket_type"}, {"offset": 1, "color": "brand.secondary"}]}]
    layers = [{"id": f"s{i}", "name": "s", "kind": "shape", "x": 0, "y": 0, "width": 9, "height": 9, "fill": f}
              for i, f in enumerate(fills)]
    ctx = {"colors": {"primary": "#111111", "secondary": "#222222", "ticketType": "#333333"}}
    body = v2.build_body(lay(layers, vars={"accent": "#ff8a3d"}), {}, context=ctx)
    assert "background:#FF8A3D" in body
    assert "background:linear-gradient(90deg, #111111 0%, #00000080 100%)" in body
    assert "background:radial-gradient(circle at 50% 0%, #333333 0%, #222222 100%)" in body
    assert "background:#000000" in v2.build_body(lay(layers[:1]), {})  # no accent set


def test_the_page_carries_the_fonts_it_uses_and_nothing_fetches():
    layout = lay([text(font={"family": "Space Grotesk", "weight": 700}),
                  text("m", t="x", font={"family": "Space Mono", "weight": 400}),
                  {"id": "i", "name": "i", "kind": "image", "source": "org_logo", "x": 0, "y": 0, "width": 9, "height": 9}, QR])
    html = v2.build_html(layout, {"first_name": "Meera"}, context={"qr": "TK-1", "images": {"org_logo": "https://x/y.png"}},
                         kind="badge")
    faces = set(re.findall(r"font-family:'([^']+)';font-style:(\w+);font-weight:(\d+)", html))
    assert faces == {("Space Grotesk", "normal", "700"), ("Space Mono", "normal", "400")}
    assert "data:font/woff2;base64," in html and "https://x/y.png" not in html and 'data-layer="i"' not in html
    assert '<meta name="moa-page" content="101.6mm 152.4mm">' in html and 'window.__moaReady' in html
    browser = v2.build_html(layout, {"first_name": "Meera"}, context={"qr": "TK-1"}, kind="badge", for_browser=True,
                            base_url="https://api.example.com")
    assert "https://api.example.com/static/doc-fonts/woff2/space-grotesk-" in browser and "base64,d09G" not in browser


def test_the_qr_encodes_the_contexts_payload():
    body = v2.build_body(lay([{**QR, "quietZone": 2}]), {}, context={"qr": "TK-48213"})
    path = re.search(r'<path fill="#000" d="([^"]+)"', dqr.svg("TK-48213", "M", 2)).group(1)
    assert path in body


def test_svg_art_is_recoloured_prefixed_and_fitted():
    markup = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><defs><linearGradient id="g">'
              '<stop offset="0" stop-color="#6b1f2e"></stop></linearGradient></defs>'
              '<rect width="10" height="10" fill="url(#g)" stroke="#6B1F2E"></rect><use href="#g"></use></svg>')
    body = v2.build_body(lay([{"id": "art", "name": "a", "kind": "svg", "x": 0, "y": 0, "width": 9, "height": 9,
                               "markup": markup, "fit": "cover", "recolor": {"#6B1F2E": "var:accent"}}],
                             vars={"accent": "#123456"}), {})
    assert 'preserveAspectRatio="xMidYMid slice"' in body and "#6B1F2E" not in body.upper().replace("#123456", "")
    assert 'id="art-g"' in body and "url(#art-g)" in body and 'href="#art-g"' in body and body.count("#123456") == 2


def test_images_and_the_initials_tile():
    photo = {"id": "p", "name": "p", "kind": "image", "source": "guest_photo", "x": 0, "y": 0, "width": 90, "height": 90,
             "placeholder": "initials", "placeholderStyle": {"color": "var:accent", "family": "Space Grotesk", "weight": 700}}
    tile = v2.build_body(lay([photo], vars={"accent": "#FF8A3D"}), {"first_name": "meera", "last_name": "pillai"})
    assert "moa-initials" in tile and ">MP</span>" in tile and "color:#FF8A3D" in tile
    with_photo = v2.build_body(lay([photo]), {}, context={"images": {"guest_photo": PNG}})
    assert f'src="{PNG}"' in with_photo and "moa-initials" not in with_photo
    assert 'data-layer="p"' not in v2.build_body(lay([{**photo, "placeholder": "none"}]), {})


def test_printing_on_stock_leaves_out_what_is_preprinted():
    layout = lay([text("a", t="DEVFEST", preprinted=True), text("b", preprinted=True), text("c", t="x")])
    body = v2.build_body(layout, {"first_name": "Meera"}, skip_preprinted=True)
    assert re.findall(r'data-layer="(\w)"', body) == ["b", "c"]
    assert re.findall(r'data-layer="(\w)"', v2.build_body(layout, {"first_name": "M"})) == ["a", "b", "c"]


def test_a_page_whose_canvas_is_not_the_papers_pixels_is_scaled_in_print():
    layout = lay([QR], page={"widthMm": 54, "heightMm": 85.6}, canvas={"width": 638, "height": 1011})
    html = v2.build_html(layout, {}, context={"qr": "x"}, kind="badge")
    assert "@media print" in html and "transform:scale(0.31" in html
    assert "transform:scale" not in v2.build_html(lay([QR]), {}, context={"qr": "x"}, kind="badge")


def test_the_fingerprint_moves_with_everything_drawn():
    layout = lay([text(), QR])
    base = v2.fingerprint(layout, {"first_name": "A"}, {"qr": "1"})
    assert base == v2.fingerprint(layout, {"first_name": "A"}, {"qr": "1"})
    assert base != v2.fingerprint(layout, {"first_name": "B"}, {"qr": "1"})
    assert base != v2.fingerprint(layout, {"first_name": "A"}, {"qr": "2"})
    assert base != v2.fingerprint(layout, {"first_name": "A"}, {"qr": "1", "images": {"org_logo": PNG}})
    assert base != v2.fingerprint(lay([text(size=17), QR]), {"first_name": "A"}, {"qr": "1"})


# ─── The conformance fixtures ────────────────────────────────────────────────


def _built(case: dict) -> str:
    return build.build_body(case["layout"], case["values"], context=case["context"], kind=case["kind"],
                            skip_preprinted=case.get("skipPreprinted", False))


def test_the_fixtures_cover_every_kind_and_are_current():
    if os.environ.get("LAYOUT_V2_WRITE_FIXTURES"):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, case in CASES.items():
            out = {"name": name, **case, "expected": normalise(_built(case))}
            (FIXTURES / f"{name}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    files = sorted(FIXTURES.glob("*.json"))
    assert len(files) >= 12 and {f.stem for f in files} == set(CASES)
    kinds = set()
    for f in files:
        fx = json.loads(f.read_text())
        assert normalise(_built(fx)) == fx["expected"], f.name
        assert v2.parse(fx["layout"])
        kinds |= {layer["kind"] for layer in fx["layout"]["layers"]}
    assert kinds == {"text", "shape", "svg", "image", "qr"}


def test_the_normaliser_ignores_attribute_order_and_whitespace():
    a = '<div  style="width: 1px ;height:2px" data-layer="x"> <span>a &amp; b</span>\n</div>'
    b = '<div data-layer="x" style="width:1px;height:2px"><span>a &#38; b</span></div>'
    assert normalise(a) == normalise(b) == '<div data-layer="x" style="width:1px;height:2px"><span>a &amp; b</span></div>'


# ─── v1 and presets ──────────────────────────────────────────────────────────


def _v1(d: dict) -> dict:
    from app.core.layout.model import dump

    return {"canvas": dump(d["canvas"]), "layers": [dump(x) for x in d["layers"]], "page": dump(d["page"])}


def test_from_v1_keeps_every_layer_in_place():
    from app.core.layout import starters as v1

    card = v2.from_v1(_v1(v1.badge_starter("card_cr80")))
    assert card["canvas"]["width"] == 638 and card["canvas"]["height"] == 1011
    assert card["page"] == {"widthMm": 54.0, "heightMm": 85.6}
    assert v2.problems(card, "badge") == []
    by_id = {layer["id"]: layer for layer in card["layers"]}
    assert by_id["first"]["font"] == {"family": "Inter", "weight": 700, "style": "normal"} and by_id["first"]["size"] == 84
    assert by_id["qr"]["width"] == by_id["qr"]["height"] == 150
    assert by_id["photo"]["placeholder"] == "initials" and by_id["photo"]["radius"] == [135] * 4
    assert by_id["head"]["fill"] == {"type": "solid", "color": "brand.primary"}
    assert by_id["event"]["font"]["family"] == "Roboto Mono" and by_id["type"]["font"]["family"] == "Bebas Neue"
    # The first baseline where reportlab drew it: the box top within a pixel of v1's y.
    assert abs(by_id["first"]["y"] - 574) < 1


def test_from_v1_scales_a_big_canvas_maps_aliases_and_keeps_the_template():
    v1 = {"canvas": {"width": 3508, "height": 2480, "background": "#ffffff", "templateKey": "t/bg.png",
                     "templatePreprinted": True},
          "page": {"widthMm": 297, "heightMm": 210},
          "layers": [{"kind": "text", "id": "r", "name": "Role", "x": 100, "y": 100, "width": 800, "size": 40,
                      "text": "{{role}} · {{name | upper}} {% if role %}x{% endif %}"},
                     {"kind": "shape", "id": "s", "name": "S", "x": 0, "y": 0, "width": 200, "height": 100,
                      "fill": "#000000", "path": "M0 0 L1000 0 L1000 1000 Z"}]}
    out = v2.from_v1(v1)
    assert out["canvas"]["width"] == 3000 and abs(out["canvas"]["height"] - 2120.866) < 0.01
    tpl, role, s = out["layers"]
    assert tpl["source"] == "upload" and tpl["imageKey"] == "t/bg.png" and tpl["preprinted"]
    assert role["text"] == "{{ticket_type}} · {{name | upper}} {% if ticket_type %}x{% endif %}"
    assert s["path"] == "M0 0 L171.04 0 L171.04 85.52 Z"
    with pytest.raises(ValueError):
        v2.from_v1({**v1, "layers": [{"kind": "agenda", "id": "a", "name": "A", "x": 0, "y": 0, "width": 99, "height": 99}]})


def test_from_preset_applies_accent_words_lanyard_and_bindings():
    out = v2.from_preset({"id": "badge/06-thermal-mono", "options": {"lanyard": False},
                          "bindings": {"organization": {"source": "hidden"}, "zone": {"source": "text", "value": "B {2}"},
                                       "access_level": {"source": "field", "value": "t_shirt"}}})
    by_id = {layer["id"]: layer for layer in out["layers"]}
    assert by_id["lanyard"]["hidden"] is True
    org = [layer for layer in out["layers"] if "organization" in json.dumps(layer)]
    assert all(layer.get("hidden") or "{{organization}}" not in json.dumps(layer) for layer in org)
    flat = json.dumps(out)
    assert "ZONE B (2)" in flat and "{{zone}}" not in flat and "{{field.t_shirt}}" in flat
    assert v2.problems(out, "badge") == []
    cert = v2.from_preset({"id": "certificate/07-swiss-minimal", "accent": "#123abc",
                           "options": {"wording": "achievement", "text": {"kicker": "Well done {{recipient_name}}"}}})
    assert cert["vars"]["accent"] == "#123ABC"
    kicker = next(layer for layer in cert["layers"] if layer["id"] == "slot-kicker")
    assert kicker["text"] == "Well done {{recipient_name}}"
    assert v2.problems(cert, "certificate") == []
    assert cert["starter"] == "certificate/07-swiss-minimal"


# ─── The starters ────────────────────────────────────────────────────────────


ALL_DESIGNS = [d for kind in dd.KINDS for d in dd.catalogue(kind)]


def test_every_design_has_its_starter_and_the_others_are_there():
    for kind, extra in (("ticket", ["ticket/blank"]), ("certificate", ["certificate/blank"]),
                        ("badge", ["badge/card_cr80", "badge/blank"])):
        ids = v2.starter_ids(kind)
        assert ids == [d.id for d in dd.catalogue(kind)] + extra
    assert len([i for k in dd.KINDS for i in v2.starter_ids(k)]) == 39
    for kind in dd.KINDS:
        assert v2.default_starter_id(kind) == dd.DEFAULTS[kind]


@pytest.mark.parametrize("sid", [i for k in dd.KINDS for i in v2.starter_ids(k)])
def test_every_starter_is_valid_and_names_only_known_tokens(sid):
    kind = sid.split("/")[0]
    layout = v2.starter(sid)
    parsed = v2.parse(layout)
    assert len(parsed.layers) <= MAX_LAYERS
    errors = [p for p in v2.problems(layout, kind) if p["level"] == "error"]
    assert errors == []
    known = vocabulary.known_tokens(kind) | set(vocabulary.IMAGE_TOKENS)
    for layer in parsed.layers:
        for t in vocabulary.show_if_names(layer.show_if):
            assert t in known, (layer.id, t)
        if layer.kind == "text":
            for src in [s.text for s in layer.spans or []] or [layer.text]:
                assert vocabulary.parse(src, kind).errors == [], (layer.id, src)
    if kind in ("ticket", "badge") or sid.startswith("certificate/0") or sid.startswith("certificate/1"):
        assert sum(1 for layer in parsed.layers if layer.kind == "qr") == 1
    html = v2.build_html(layout, vocabulary.samples(kind), context={"qr": "TK-1"}, kind=kind)
    assert "{{" not in html and "{%" not in html


STARTER_FILES = sorted((Path(gallery.__file__).parent / "starters").rglob("*.json"))


@pytest.mark.parametrize("path", STARTER_FILES, ids=lambda p: str(p.relative_to(p.parents[1])))
def test_every_starter_file_round_trips(path):
    """A starter is its full form compacted, and compacting loses nothing:
    parsing it gives back every field of every layer, whatever its kind."""
    on_disk = json.loads(path.read_text())
    full = v2.dump(v2.parse(on_disk))
    assert schema.compact(full) == on_disk
    assert v2.dump(v2.parse(schema.compact(full))) == full


def _every_field_off_default() -> dict:
    """A layer of each kind with every optional field set away from its own
    kind's default, and fields set to *other* kinds' defaults."""
    fill = {"type": "solid", "color": "#123456"}
    common = {"rotation": 5, "opacity": 0.5, "hidden": True, "locked": True, "showIf": "first_name",
              "preprinted": True, "shadow": [{"x": 1, "y": 2, "blur": 3, "spread": 4, "color": "#000000", "inset": True}]}
    box = {"x": 1, "y": 2, "width": 30, "height": 40}
    return lay([
        {"id": "t", "name": "t", "kind": "text", **box, **common, "text": "a", "font": {"family": "Fraunces", "weight": 500,
         "style": "italic"}, "size": 20, "color": "#111111", "letterSpacing": 0.1, "lineHeight": 1.5, "align": "right",
         "valign": "bottom", "transform": "upper", "textWrap": "balance", "maxLines": 3, "fit": "none", "minSize": 9,
         "background": fill, "padding": [1, 2, 3, 4], "radius": [4, 3, 2, 1]},
        {"id": "t2", "name": "t2", "kind": "text", **box, "text": "b", "align": "center", "valign": "middle", "fit": "ellipsis"},
        {"id": "s", "name": "s", "kind": "shape", **box, **common, "shape": "ellipse", "fill": fill,
         "stroke": {"color": "#222222", "width": 2, "dash": [3, 1]}, "radius": [1, 1, 1, 1]},
        {"id": "s2", "name": "s2", "kind": "shape", **box, "shape": "path", "path": "M0 0H9V9Z"},
        {"id": "v", "name": "v", "kind": "svg", **box, **common, "markup": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>',
         "fit": "stretch", "recolor": {"#ABCDEF": "var:accent"}},
        {"id": "v2", "name": "v2", "kind": "svg", **box, "markup": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>',
         "fit": "cover"},
        {"id": "i", "name": "i", "kind": "image", **box, **common, "source": "upload", "imageKey": "k", "fit": "cover",
         "align": "left", "valign": "top", "radius": [2, 2, 2, 2], "border": {"color": "#333333", "width": 1},
         "placeholder": "initials", "placeholderStyle": {"color": "#444444", "background": fill, "family": "Inter", "weight": 400}},
        {"id": "i2", "name": "i2", "kind": "image", **box, "align": "right", "valign": "bottom"},
        {"id": "q", "name": "q", "kind": "qr", **box, **common, "quietZone": 4, "background": fill, "radius": [5, 5, 5, 5]},
    ], vars={"accent": "#ABCDEF"}, starter="badge/x")


def test_compacting_keeps_each_kinds_own_values():
    layout = _every_field_off_default()
    full = v2.dump(v2.parse(layout))
    small = schema.compact(layout)
    assert v2.dump(v2.parse(small)) == full
    by_id = {layer["id"]: layer for layer in small["layers"]}
    # An image's left/top are not its defaults (center/middle), whatever a text's are.
    assert by_id["i"]["align"] == "left" and by_id["i"]["valign"] == "top"
    assert by_id["i2"]["align"] == "right" and by_id["i2"]["valign"] == "bottom"
    # A text's center/middle are not its defaults; an svg's cover is not its default.
    assert by_id["t2"]["align"] == "center" and by_id["t2"]["valign"] == "middle" and by_id["v2"]["fit"] == "cover"
    # What a kind's own default is, is left out.
    assert "align" not in schema.compact(lay([{"id": "i", "name": "i", "kind": "image", "x": 0, "y": 0, "width": 1,
                                                "height": 1, "align": "center"}]))["layers"][0]
    for kind in schema.KIND_MODELS:
        assert set(schema.kind_defaults(kind)) >= {"rotation", "opacity", "hidden"}


def test_the_card_keeps_its_left_aligned_logo():
    logo = next(layer for layer in v2.starter("badge/card_cr80")["layers"] if layer["id"] == "logo")
    assert logo["align"] == "left"
    assert v2.parse(v2.starter("badge/card_cr80")).layers[1].align == "left"


def test_a_starter_is_a_copy_and_the_gallery_says_what_each_is():
    a = v2.starter("badge/06-thermal-mono")
    a["layers"].clear()
    assert v2.starter("badge/06-thermal-mono")["layers"]
    s = v2.starters("badge", base_url="https://api.example.com")
    first = s[0]
    assert first["id"] == "badge/01-ticket-stub" and first["hasPhoto"] and first["preview"].startswith("https://api.example.com/")
    assert first["accent"]["default"] == "#FF8A3D" and first["layout"]["starter"] == "badge/01-ticket-stub"
    cr80 = next(x for x in s if x["id"] == "badge/card_cr80")
    assert cr80["name"] == "Card (CR80)" and cr80["preview"] is None
    with pytest.raises(gallery.UnknownStarter):
        v2.starter("badge/99-nope")
    with pytest.raises(gallery.UnknownStarter):
        v2.starter("../etc/passwd")


def test_the_certificates_wordings_are_converted_too():
    for d in dd.catalogue("certificate"):
        for wording in d.raw["wordings"]:
            layout = v2.starter(d.id, wording)
            assert v2.problems(layout, "certificate") == [] or all(
                p["level"] == "warning" for p in v2.problems(layout, "certificate")), (d.id, wording)


def test_the_accent_is_a_variable_in_every_design_that_has_one():
    for d in ALL_DESIGNS:
        layout = v2.starter(d.id)
        if d.accent:
            assert layout["vars"]["accent"] == d.accent.default
            assert "var:accent" in json.dumps(layout), d.id
        else:
            assert "var:accent" not in json.dumps(layout), d.id


# ─── Fonts and tokens ────────────────────────────────────────────────────────


def test_fonts_available_has_the_designs_and_the_v1_five():
    fonts = {f["family"]: f for f in v2.fonts_available()}
    assert len(fonts) == 37
    assert fonts["Inter"]["v1"] == "inter" and fonts["Bebas Neue"]["weights"] == [400]
    assert fonts["Google Sans"]["weights"] == [400, 500, 600, 700] and fonts["Roboto Mono"]["weights"] == [400, 700]
    assert fonts["Fraunces"]["styles"] == ["italic", "normal"]


def test_the_token_catalogue_gains_the_designs_tokens():
    keys = {t["key"] for kind in ("ticket", "certificate", "badge") for t in v2.tokens(kind)}
    for k in ("role", "access_level", "badge_id", "zone", "hall", "table_no", "track", "bib_number", "tagline",
              "doors_time", "gate", "row", "seat", "issue_date", "issue_city", "signatory_name"):
        assert k in keys, k
    role = next(t for t in v2.tokens("badge") if t["key"] == "role")
    assert role["kinds"] == ["badge"] and "ATTENDEE" in role["label"]
    # A design's token is itself, not v1's alias of the same name.
    assert vocabulary.fill("{{role}}", {"role": "SPEAKER", "ticket_type": "VIP"}, "badge") == "SPEAKER"
    assert vocabulary.fill("{{role}}", {"role": "SPEAKER", "ticket_type": "VIP"}, "ticket") == "VIP"
    every = {t for kind in ("ticket", "certificate", "badge") for d in dd.catalogue(kind) for t in d.tokens}
    assert every - {"accent_color", "qr_code_src", "qr_code_url"} - set(vocabulary.IMAGE_TOKENS) <= keys


def test_a_v1_canvas_saved_twice_is_not_taken_for_v2():
    """v1 counted saves in `version`: its second save says 2 too."""
    from app.core.layout_v2.schema import is_v2

    v1 = {"version": 2, "canvas": {"width": 638, "height": 1011, "background": "#ffffff"}, "layers": [], "page": {"widthMm": 54, "heightMm": 85.6}}
    assert is_v2(v1) is False
    v2 = {"version": 2, "canvas": {"width": 384, "height": 576, "background": None}, "vars": {"accent": "#FF8A3D"}, "layers": []}
    assert is_v2(v2) is True
    assert is_v2({**v2, "vars": None, "canvas": {"width": 384, "height": 576, "background": {"type": "solid", "color": "#FFFFFF"}}}) is True
