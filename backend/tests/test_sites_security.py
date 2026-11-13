"""A club's site cannot be made to run a script, and its CSP names exactly
the inline code it carries.

The JSON-LD block was `json.dumps` written raw inside <script>: a club named
`</script><script>...` closed the element and ran its own code on the page,
which the Worker's `script-src 'unsafe-inline'` let through.

Every template in the checkout is rendered here (the catalogue's and the
tests' fixture): whatever a template's markup, the platform's guarantees hold
-- one script, every stylesheet named by hash, no club text as markup.
"""
import copy
import json
import re
from html.parser import HTMLParser

import pytest

from app.api.sites import registry
from app.api.sites import render as site_render
from app.api.sites.service import _social_href, safe_href
from tests.conftest import auth_header

EVIL = '</script><script>alert(document.cookie)</script><!--'

SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)
STYLE = re.compile(r"<style[^>]*>(?P<body>.*?)</style>", re.S)


def _tags(html: str) -> list:
    """Every element the browser would build, with its attributes."""
    found = []

    class Collect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.append((tag, attrs))

    Collect().feed(html)
    return found


def _data(template="fixture", *, name="Circuit Society", headline=None, team=(), sponsors=(), accent=None) -> dict:
    data = copy.deepcopy(registry.get("fixture").sample)
    data["site"] = {**data["site"], "template": template, "headline": headline, "layout": {"accent": accent}}
    data["club"]["name"] = name
    data["headline"] = headline or name
    data["page"] = {"title": name, "description": "Sub", "url": "https://circuit.myotherapp.in"}
    data["team"] = list(team)
    data["sponsors"] = list(sponsors)
    data["visit_url"] = "https://api.myotherapp.in/api/v1/sites/circuit/visit"
    return data


def _page(template="fixture", **kw) -> str:
    return site_render.render(_data(template, **kw))


# First in the module: a test that opens the database before the rest
# (the suite's loop is made by the first test that needs it).
async def test_the_page_is_served_with_the_policy(client, session, make_club, mint_token):
    owner, club = await make_club(name=EVIL, email="evil-site@example.com")
    await session.commit()
    h = auth_header(await mint_token(owner))
    assert (await client.post("/api/v1/clubs/site/publish", headers=h)).status_code == 200

    r = await client.get(f"/api/v1/sites/{club.slug}")

    assert r.status_code == 200
    assert r.headers["content-security-policy"] == site_render.site_csp(frame_ancestors="")
    ld = [m.group("body") for m in SCRIPT.finditer(r.text) if "ld+json" in m.group("attrs")]
    assert json.loads(ld[0])["name"] == EVIL and "</script" not in ld[0]


async def test_a_club_name_cannot_close_the_json_ld_script():
    html = _page(name=EVIL)

    blocks = [m for m in SCRIPT.finditer(html)]
    ld = [m.group("body") for m in blocks if "ld+json" in m.group("attrs")]
    assert len(ld) == 1 and "</script" not in ld[0] and "<" not in ld[0] and ">" not in ld[0]
    # Still the same data to anything reading it.
    assert json.loads(ld[0])["name"] == EVIL
    # Only our two scripts on the page, whatever the club typed.
    assert len(blocks) == 2


async def test_nothing_the_club_types_becomes_markup():
    html = _page(
        name='"><img src=x onerror=alert(1)>', headline="<b>bold</b>",
        team=[{"name": "<i>x</i>", "role": '" onmouseover="alert(1)', "initials": "X", "photo": None, "links": [{"kind": "x", "label": "X", "href": safe_href(_social_href("x", 'javascript:alert(1)"'))}]}],
        sponsors=[{"name": "<svg onload=alert(1)>", "role": None, "initials": "S", "logo": None}],
    )

    assert "<img src=x" not in html and "<b>bold" not in html and "<svg" not in html and "<i>x" not in html
    tags = _tags(html)
    assert not [a for _, attrs in tags for a, _ in attrs if a.startswith("on")]  # no event handler anywhere
    assert not [v for _, attrs in tags for a, v in attrs if a == "href" and not (v or "").lower().startswith(("https://", "mailto:", "tel:", "#"))]


def test_only_https_mail_and_phone_addresses_reach_a_page():
    assert safe_href("https://circuit.org/x") == "https://circuit.org/x"
    assert safe_href("http://circuit.org") == "https://circuit.org"
    assert safe_href("mailto:a@b.c") == "mailto:a@b.c" and safe_href("tel:+919800000000") == "tel:+919800000000"
    for bad in ("javascript:alert(1)", " JavaScript:alert(1)", "data:text/html,x", "//evil.com", "circuit.org", "", None, "https://"):
        assert safe_href(bad) is None, bad


@pytest.mark.parametrize("template", list(registry.packages()))
async def test_every_inline_script_and_style_is_named_in_the_csp(template):
    """Recomputed from the pages themselves, every template, with and
    without the club's accent: a template whose page carries a stylesheet
    or script the policy does not name fails here."""
    csp = site_render.site_csp()
    for accent in (None, "#3355ff"):
        html = _page(template, accent=accent)

        scripts = [m.group("body") for m in SCRIPT.finditer(html) if "ld+json" not in m.group("attrs")]
        styles = [m.group("body") for m in STYLE.finditer(html)]
        assert scripts == [site_render.SITE_SCRIPT] and styles
        assert {site_render.csp_hash(s) for s in styles} <= set(site_render.style_hashes())
        assert all(site_render.csp_hash(s) in csp for s in styles + scripts)
        # The accent is a custom property on <html>, never in a stylesheet.
        on_html = re.search(r"<html\b[^>]*>", html).group(0)
        assert ("--accent:#3355ff" in on_html) is (accent is not None)


async def test_the_policy():
    csp = site_render.site_csp()
    parts = dict(p.split(" ", 1) for p in csp.split("; "))
    assert parts["script-src"] == site_render.csp_hash(site_render.SITE_SCRIPT)
    assert "unsafe-inline" not in parts["script-src"] and "unsafe-inline" not in parts["style-src-elem"]
    assert parts["style-src-elem"].startswith("'sha256-") and parts["style-src-attr"] == "'unsafe-inline'"
    assert parts["frame-ancestors"] == "'none'" and parts["form-action"] == "'none'" and parts["default-src"] == "'none'"
    # Every template's stylesheet, once.
    hashes = site_render.style_hashes()
    assert len(hashes) == len(set(hashes)) and {site_render.csp_hash(str(p.css)) for p in registry.packages().values()} <= set(hashes)


async def test_a_colour_that_is_not_one_never_reaches_the_style_attribute():
    html = _page(accent='#1"><x')

    assert re.search(r"<html\b[^>]*>", html).group(0) == '<html lang="en-IN">' and "<x" not in html
