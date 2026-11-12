"""The ticket, certificate and ID card designs (app/core/doc_designs; the
doc-designs contract, D1-D4, D8, D9): manifests and templates agree, values
are escaped, a token is never left in a page, optional blocks go, the cache
key moves with what is printed, the render service client keeps to its
timeouts and tries, the fonts are the locked files and are served publicly.

No network: the render service is a fake transport.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest

from app.core import doc_designs as dd
from app.core.doc_designs import compile as dcompile
from app.core.doc_designs import designs, fonts, render
from app.core.doc_designs.fill import IMAGE_TOKENS, tokens_of

TEMPLATES = Path(dd.__file__).parent / "templates"
ALL = [d for kind in dd.KINDS for d in dd.catalogue(kind)]
LOGO = "data:image/svg+xml;base64," + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()
PHOTO = LOGO


def sample(d) -> dict:
    """A value for every token the design prints."""
    values = {t: f"{t} value" for t in d.tokens if t not in IMAGE_TOKENS and t != "accent_color"}
    values["qr"] = "TK-48213"
    for t in IMAGE_TOKENS & set(d.tokens) - {d.qr["token"]}:
        values[t] = LOGO
    return values


def required_only(d) -> dict:
    values = {t: f"{t} value" for t, need in d.tokens.items()
              if need == "required" and t not in IMAGE_TOKENS and t != "accent_color"}
    values["qr"] = "TK-1"
    return values


# ─── Manifests and templates ─────────────────────────────────────────────────


def test_there_are_the_35_designs_and_the_defaults_exist():
    assert [len(dd.catalogue(k)) for k in dd.KINDS] == [10, 10, 15]
    for kind, design_id in dd.DEFAULTS.items():
        assert dd.get(design_id).kind == kind
    with pytest.raises(dd.UnknownDesign):
        dd.get("ticket/99-nope")


@pytest.mark.parametrize("d", ALL, ids=lambda d: d.id)
def test_every_token_is_in_the_manifest_and_every_optional_one_can_go(d):
    named = tokens_of(d.id) | {d.qr["token"]} | ({"initials"} if d.has_photo else set())  # the initials tile
    assert named - {"accent_color"} == set(d.tokens) - {"accent_color"}, "manifest and template disagree"
    body = designs.compiled(d.id).body
    photo_tokens = {t for b in dcompile.blocks(body, "photo") for t in dcompile.tokens_in(b.open)}
    for token, guards in dcompile.guarded(body):
        if d.tokens.get(token) == "optional" and token not in photo_tokens:
            assert token in guards, f"{token} is optional but nothing removes it when empty"
    for wording in (d.raw.get("wordings") or {}).values():
        for html in (wording or {}).values():
            for token, guards in dcompile.guarded(dcompile.compile_fragment(html)):
                if d.tokens.get(token) == "optional":
                    assert token in guards, f"{token} in a wording is optional but not removable"


@pytest.mark.parametrize("d", ALL, ids=lambda d: d.id)
def test_every_file_the_manifest_names_exists_and_no_template_calls_google(d):
    assert d.file.is_file()
    assert d.preview is not None and d.preview.is_file(), "every design has a preview image"
    text = d.file.read_text()
    assert "fonts.googleapis.com" not in text and "fonts.gstatic.com" not in text
    for face in d.fonts:
        fonts._face(*face)  # in the lock
    if d.accent:
        assert re.fullmatch(r"#[0-9A-F]{6}", d.accent.default)
        assert dd.validate(d.id, accent=d.accent.default) == [], "the designer's own accent passes"


def test_to_dict_carries_no_paths_and_an_absolute_preview():
    d = dd.get("certificate/07-swiss-minimal").to_dict(base_url="https://api.example.in")
    blob = json.dumps(d)
    assert "/Users/" not in blob and "templates/" not in blob and "<" not in blob
    assert d["preview"].startswith("https://api.example.in/static/doc-designs/certificate/07-swiss-minimal.png?v=")
    assert d["options"]["wording"]["default"] == "participation"
    assert "signatory_name" in d["bindable"]


# ─── Filling ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("d", ALL, ids=lambda d: d.id)
def test_every_design_fills_with_everything_and_with_only_what_it_needs(d):
    full = dd.fill(d.id, sample(d))
    assert "{{" not in full and "[QR CODE]" not in full and "[SPONSOR LOGOS]" not in full
    assert "window.__moaReady" in full and "@font-face" in full
    assert "fonts.googleapis.com" not in full
    minimal = dd.fill(d.id, required_only(d))
    assert "{{" not in minimal
    assert len(minimal) < len(full)


def test_values_are_escaped_and_cannot_pose_as_tokens():
    d = dd.get("ticket/03-daylight-boarding-pass")
    values = {**sample(d), "attendee_name": '<script>alert("x")</script> & {{gate}}', "venue": "O'Brien's"}
    html = dd.fill(d.id, values)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#123;&#123;gate&#125;&#125;" in html
    assert "O&#x27;Brien&#x27;s" in html


def test_images_must_be_data_uris():
    d = dd.get("certificate/01-heritage")
    with pytest.raises(dd.BadValue):
        dd.fill(d.id, {**sample(d), "logo_src": "https://evil.example/x.png"})
    with pytest.raises(dd.BadValue):
        dd.fill(d.id, sample(d), accent="red")


def test_a_missing_required_value_or_a_leftover_token_fails(monkeypatch):
    d = dd.get("ticket/03-daylight-boarding-pass")
    values = sample(d)
    del values["attendee_name"]
    with pytest.raises(dd.MissingValue):
        dd.fill(d.id, values)
    broken = dcompile.Compiled("t", [], dcompile.compile_fragment("<div>{{ spaced }}</div>"))
    monkeypatch.setattr(designs, "compiled", lambda _id: broken)
    with pytest.raises(dd.LeftoverToken):
        dd.fill(d.id, {})
    placeholder = dcompile.Compiled("t", [], dcompile.compile_fragment("<div>[QR CODE]</div>"))
    monkeypatch.setattr(designs, "compiled", lambda _id: placeholder)
    with pytest.raises(dd.LeftoverToken):
        dd.fill(d.id, {})


def test_general_admission_hides_gate_row_and_seat():
    d = dd.get("ticket/03-daylight-boarding-pass")
    values = {k: v for k, v in sample(d).items() if k not in ("gate", "row", "seat", "doors_time")}
    html = dd.fill(d.id, values)
    assert ">SEAT<" not in html  # the seat block is left out
    assert re.search(r'visibility: hidden; [^"]*">.*?GATE', html)  # the gate keeps its grid cell, unseen
    one = dd.fill(d.id, {**values, "row": "F"})
    assert ">SEAT<" in one and ">F</span>" in one and " · " not in one.split(">SEAT<")[1][:400]


def test_a_missing_job_title_takes_its_separator_with_it():
    d = dd.get("badge/01-ticket-stub")
    values = {**sample(d), "job_title": ""}
    html = dd.fill(d.id, values)
    assert "organization value" in html and "job_title value" not in html
    assert "job_title value · " not in html and "> · <" not in html
    neither = dd.fill(d.id, {**values, "organization": ""})
    assert "organization value" not in neither


def test_no_signatory_no_signature_block_and_no_photo_an_initials_tile():
    cert = dd.get("certificate/07-swiss-minimal")
    html = dd.fill(cert.id, required_only(cert))
    assert 'data-slot="signature"' not in html
    badge = dd.get("badge/07-polaroid")
    values = {**sample(badge), "photo_url": "", "full_name": "Meera Pillai", "initials": ""}
    html = dd.fill(badge.id, values)
    assert 'class="moa-initials"' in html and ">MP</span>" in html
    with_photo = dd.fill(badge.id, {**values, "photo_url": PHOTO})
    assert "moa-initials" not in with_photo and PHOTO in with_photo


def test_wording_options_and_a_clubs_own_words():
    d = dd.get("certificate/07-swiss-minimal")
    designer = dd.fill(d.id, sample(d), {"wording": "completion"})
    assert "Of completion &middot; Awarded to" in designer
    participation = dd.fill(d.id, sample(d))  # the default
    assert "Of participation &middot; Awarded to" in participation and "for taking part in" in participation
    own = dd.fill(d.id, sample(d), {"text": {"kicker": "For <b>{{recipient_name}}</b>"}})
    assert "For &lt;b&gt;recipient_name value&lt;/b&gt;" in own


def test_bindings_auto_text_field_hidden():
    d = dd.get("badge/09-boarding-pass")
    values = {**sample(d), "field.hall-choice": "HALL C", "zone": "B2"}
    html = dd.fill(d.id, values, bindings={
        "hall": {"source": "field", "value": "hall-choice"},
        "day_label": {"source": "text", "value": "DAY 2"},
        "zone": {"source": "hidden"},
    })
    assert ">HALL C<" in html and ">DAY 2<" in html and ">B2<" not in html


def test_lanyard_option_and_the_accent_variable():
    d = dd.get("badge/13-sash")
    html = dd.fill(d.id, sample(d), {"lanyard": False}, accent="#d63a22")
    assert re.search(r'<div data-lanyard aria-hidden="true" style="visibility: hidden; ', html)
    assert '<div class="moa-page" style="--accent:#D63A22">' in html
    assert re.search(r'<div data-lanyard aria-hidden="true" style="align-self', dd.fill(d.id, sample(d)))


def test_the_qr_is_black_on_white_svg_of_the_payload():
    import qrcode

    d = dd.get("ticket/03-daylight-boarding-pass")
    html = dd.fill(d.id, {**sample(d), "qr": "TK-48213"})
    src = re.search(r'<img src="(data:image/svg\+xml;base64,[^"]+)"', html).group(1)
    svg = base64.b64decode(src.split(",", 1)[1]).decode()
    assert 'fill="#fff"' in svg and 'fill="#000"' in svg
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data("TK-48213")
    qr.make(fit=True)
    n = len(qr.get_matrix())
    assert f'viewBox="0 0 {n} {n}"' in svg


def test_only_the_subsets_the_page_needs_are_inlined():
    d = dd.get("certificate/07-swiss-minimal")
    plain = dd.fill(d.id, sample(d))
    accented = dd.fill(d.id, {**sample(d), "recipient_name": "Łőrinc Ő"})
    assert plain.count("@font-face") < accented.count("@font-face")
    assert "data:font/woff2;base64," in plain


def test_the_browser_preview_links_the_fonts_absolutely():
    d = dd.get("ticket/01-dusk-horizon")
    html = dd.preview_html(d.id, sample(d), base_url="https://api.example.in")
    assert "url(https://api.example.in/static/doc-fonts/woff2/" in html
    assert "data:font/woff2" not in html
    faces = set(re.findall(r"font-family:'([^']+)';font-style:(\w+);font-weight:(\d+)", html))
    assert faces == {(f, s, str(w)) for f, s, w in d.fonts}, "only the design's own faces"


# ─── Checking choices ────────────────────────────────────────────────────────


def test_validate_names_each_problem():
    d = "certificate/07-swiss-minimal"
    assert dd.validate(d, {"wording": "achievement"}, {"grade": {"source": "text", "value": "A+"}}) == []
    codes = {p["code"] for p in dd.validate(d, {"wording": "nope", "colour": 1},
                                            {"recipient_name": {"source": "hidden"}, "nope": {"source": "auto"},
                                             "grade": {"source": "field", "value": "Bad Slug!"},
                                             "qr_code_src": {"source": "text", "value": "x"}})}
    assert codes == {"bad_option", "unknown_option", "required", "unknown_token", "bad_field", "not_bindable"}
    low = dd.validate(d, accent="#FFE0E0")
    assert low and low[0]["code"] == "low_contrast" and low[0]["level"] == "warning"
    assert dd.validate("badge/06-thermal-mono", accent="#000000")[0]["code"] == "no_accent"
    assert dd.validate("ticket/99")[0]["code"] == "unknown_design"


# ─── The cache key (D8) ──────────────────────────────────────────────────────


def test_the_fingerprint_moves_with_everything_printed(monkeypatch):
    d = dd.get("certificate/07-swiss-minimal")
    v = sample(d)
    base = dd.fingerprint(d.id, v)
    assert base == dd.fingerprint(d.id, dict(v))
    others = {
        dd.fingerprint(d.id, {**v, "recipient_name": "Someone Else"}),
        dd.fingerprint(d.id, v, {"wording": "achievement"}),
        dd.fingerprint(d.id, v, bindings={"grade": {"source": "hidden"}}),
        dd.fingerprint(d.id, v, accent="#0B4FD8"),
        dd.fingerprint("certificate/01-heritage", v),
    }
    assert base not in others and len(others) == 5
    monkeypatch.setattr(fonts, "digest", lambda: "other-fonts")
    assert dd.fingerprint(d.id, v) != base


# ─── The render service (D1, D9) ─────────────────────────────────────────────


class FakeGotenberg:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


PDF = b"%PDF-1.7\n%fake\n"


@pytest.fixture
def gotenberg(monkeypatch):
    def install(*answers):
        fake = FakeGotenberg(*answers)
        monkeypatch.setattr(render, "_transport", httpx.MockTransport(fake))
        return fake
    return install


async def test_render_posts_the_page_with_the_designs_paper(gotenberg):
    fake = gotenberg(httpx.Response(200, content=PDF))
    d = dd.get("certificate/07-swiss-minimal")
    assert await dd.render_pdf(dd.fill(d.id, sample(d))) == PDF
    (req,) = fake.requests
    assert req.url.path == "/forms/chromium/convert/html"
    body = req.read().decode("utf-8", "replace")
    for field, value in (("paperWidth", "297mm"), ("paperHeight", "210mm"), ("marginTop", "0"),
                         ("printBackground", "true"), ("preferCssPageSize", "false"),
                         ("waitForExpression", "window.__moaReady === true")):
        assert re.search(rf'name="{field}"\r\n\r\n{re.escape(value)}\r\n', body), field
    assert 'filename="index.html"' in body


async def test_a_batch_is_one_document_with_its_fonts_once(gotenberg):
    fake = gotenberg(httpx.Response(200, content=PDF))
    d = dd.get("badge/06-thermal-mono")
    pages = [dd.fill(d.id, {**sample(d), "first_name": f"P{i}"}) for i in range(3)]
    await dd.render_pdf(pages)
    body = fake.requests[0].read().decode("utf-8", "replace")
    assert body.count('<div class="moa-page"') == 3
    assert body.count("@font-face") == pages[0].count("@font-face")
    with pytest.raises(ValueError):
        dd.combine([pages[0], dd.fill("certificate/07-swiss-minimal", sample(dd.get("certificate/07-swiss-minimal")))])


async def test_one_failure_is_tried_again_two_are_unavailable(gotenberg):
    fake = gotenberg(httpx.Response(503, text="queue full"), httpx.Response(200, content=PDF))
    page = dd.fill("badge/06-thermal-mono", sample(dd.get("badge/06-thermal-mono")))
    assert await dd.render_pdf(page) == PDF and len(fake.requests) == 2
    gotenberg(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"))
    with pytest.raises(dd.RenderUnavailable):
        await dd.render_pdf(page)
    gotenberg(httpx.Response(400, text="bad form"))
    with pytest.raises(dd.RenderError) as err:
        await dd.render_pdf(page)
    assert not isinstance(err.value, dd.RenderUnavailable)


def test_the_timeout_grows_with_the_pages_and_is_capped():
    assert render.timeout_for(1) == pytest.approx(float(render.settings.RENDER_TIMEOUT))
    assert render.timeout_for(41) == pytest.approx(float(render.settings.RENDER_TIMEOUT) + 10)
    assert render.timeout_for(100_000) == render.MAX_TIMEOUT


async def test_render_health(gotenberg):
    gotenberg(httpx.Response(200, json={"status": "up", "details": {"chromium": {"status": "up"}}}))
    assert (await dd.render_health())["ok"] is True
    gotenberg(httpx.ConnectError("refused"))
    down = await dd.render_health()
    assert down["ok"] is False and down["status"] == "unreachable"


# ─── Fonts (D3) ──────────────────────────────────────────────────────────────


def test_the_font_lock_matches_the_files():
    lock = fonts.lock()
    listed = {"fonts.css", "fonts.lock.json"}
    for info in lock["families"].values():
        entries = [info["license"]] + [f["ttf"] for f in info["faces"]] + [w for f in info["faces"] for w in f["woff2"]]
        for e in entries:
            listed.add(e["file"])
            assert hashlib.sha256((fonts.ASSETS / e["file"]).read_bytes()).hexdigest() == e["sha256"], e["file"]
            assert e["source"].startswith("https://"), e["file"]
    on_disk = {str(p.relative_to(fonts.ASSETS)) for p in fonts.ASSETS.rglob("*") if p.is_file()}
    assert on_disk == listed
    used = {face for d in ALL for face in d.fonts}
    locked = {(fam, f["style"], f["weight"]) for fam, info in lock["families"].items() for f in info["faces"]}
    # Exactly the faces the designs use, and the layout-v2 editor's five v1 fonts (doc_fonts.EDITOR_QUERIES).
    from app.core.layout_v2.fonts import V1_FAMILIES

    assert used <= locked
    assert {fam for fam, _s, _w in locked - used} <= set(V1_FAMILIES.values())
    assert {fam for fam, _s, _w in locked} >= set(V1_FAMILIES.values())


async def test_the_static_font_route_is_public_and_immutable(client):
    woff2 = next(iter(sorted(f for f in fonts.served_files() if f.endswith(".woff2"))))
    for path, kind in ((woff2, "font/woff2"), ("fonts.css", "text/css")):
        r = await client.get(f"/static/doc-fonts/{path}", headers={"Origin": "null"})
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith(kind)
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert r.headers["access-control-allow-origin"] == "*"
        assert r.headers["cross-origin-resource-policy"] == "cross-origin"
    assert r.text.startswith("/* Generated")
    for bad in ("../app/config.py", "ttf/inter-400.ttf", "woff2/nope.woff2", "%2e%2e/doth.py"):
        assert (await client.get(f"/static/doc-fonts/{bad}")).status_code == 404, bad
    r = await client.get("/static/doc-designs/badge/06-thermal-mono.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["access-control-allow-origin"] == "*"
    assert (await client.get("/static/doc-designs/badge/99-nope.png")).status_code == 404


# ─── The render service's deployment ─────────────────────────────────────────


def test_the_render_service_is_internal_and_loads_nothing_but_the_page():
    import yaml

    repo = Path(__file__).resolve().parents[2]
    for compose in (repo / "docker-compose.prod.yml", repo / "ops/server/compose.yml"):
        doc = yaml.safe_load(compose.read_text())
        svc = doc["services"]["render"]
        assert "ports" not in svc and "env_file" not in svc, compose
        nets = svc["networks"]
        (net,) = nets if isinstance(nets, list) else list(nets)
        assert doc["networks"][net]["internal"] is True, compose
        flags = svc["command"]
        assert [f for f in flags if f.startswith("--chromium-allow-list=")] == [
            "--chromium-allow-list=^file:///tmp/", "--chromium-allow-list=^data:"]
        assert "--chromium-deny-public-ips=true" in flags and "--chromium-deny-private-ips=true" in flags
        timeout = int(re.search(r"--api-timeout=(\d+)s", " ".join(flags)).group(1))
        assert timeout > render.MAX_TIMEOUT
        for app in ("backend", "worker"):
            app_nets = doc["services"][app]["networks"]
            assert net in (app_nets if isinstance(app_nets, list) else list(app_nets)), (compose, app)
    dockerfile = (repo / "backend/render/Dockerfile").read_text()
    assert re.search(r"^FROM gotenberg/gotenberg:8\.\d+\.\d+-chromium@sha256:[0-9a-f]{64}$", dockerfile, re.M)
