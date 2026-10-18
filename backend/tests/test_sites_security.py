"""A club's site cannot be made to run a script, and its CSP names exactly
the inline code it carries.

The JSON-LD block was `json.dumps` written raw inside <script>: a club named
`</script><script>...` closed the element and ran its own code on the page,
which the Worker's `script-src 'unsafe-inline'` let through.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from app.api.sites import render as site_render
from app.api.sites.models import TEMPLATES, THEMES
from tests.conftest import auth_header

NOW = datetime.now(timezone.utc)
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


def _page(template="launchpad", theme="light", **kw) -> str:
    return site_render.render(_data(template, theme, **kw))


def _data(template="launchpad", theme="light", *, name="Circuit Society", headline=None, team=(), partners=(), upcoming=None) -> dict:
    when = NOW + timedelta(days=3)
    return ({
        "site": {"template": template, "theme": theme, "font": "inter", "headline": headline, "subline": None,
                 "sections": {k: True for k in ("hero", "upcoming", "about", "past", "team", "partners", "follow", "contact")}},
        "club": {"name": name, "about": "About", "tagline": None, "location_name": "Pune", "contact_email": "a@b.c",
                 "brand_primary": "#3355ff", "logo_url": None, "cover_url": None},
        "default_headline": name, "default_subline": "Sub",
        "upcoming": upcoming if upcoming is not None else [{"id": 1, "name": "Hack", "starts_at": when, "where": "Lab", "fee": "Free", "poster": None, "url": "https://myotherapp.in/events/hack", "upcoming": True}],
        "past": [], "team": list(team), "partners": list(partners), "socials": [],
        "numbers": {"followers": 3, "events": 1}, "site_url": "https://circuit.myotherapp.in",
        "follow_url": "https://myotherapp.in/clubs/circuit?follow=1", "events_url": "https://myotherapp.in/clubs/circuit#events",
        "visit_url": "https://api.myotherapp.in/api/v1/sites/circuit/visit",
    })


# First in the module: a test that opens the database before the rest
# (the suite's loop is made by the first test that needs it).
async def test_the_page_is_served_with_the_policy(client, session, make_club, mint_token):
    owner, club = await make_club(name=EVIL, email="evil-site@example.com")
    await session.commit()
    h = auth_header(await mint_token(owner))
    assert (await client.post("/api/v1/clubs/site/publish", headers=h)).status_code == 200

    r = await client.get(f"/api/v1/sites/{club.slug}")

    assert r.status_code == 200
    assert "script-src 'sha256-" in r.headers["content-security-policy"]
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
        team=[{"name": "<i>x</i>", "role": '" onmouseover="alert(1)', "photo": None, "links": [("instagram", 'javascript:alert(1)"')]}],
        partners=[{"name": "<svg onload=alert(1)>", "role": None, "logo": None}],
    )

    assert "<img src=x" not in html and "<b>bold" not in html and "<svg" not in html and "<i>x" not in html
    tags = _tags(html)
    assert {t for t, _ in tags} <= {"html", "head", "meta", "title", "link", "style", "body", "nav", "div", "a", "span", "button", "header", "h1", "h2", "h3", "p", "i", "section", "article", "small", "b", "footer", "script", "img"}
    assert not [a for _, attrs in tags for a, _ in attrs if a.startswith("on")]  # no event handler anywhere
    assert not [v for _, attrs in tags for a, v in attrs if a == "href" and (v or "").lower().startswith("javascript:")]


async def test_every_inline_script_and_style_is_named_in_the_csp():
    """Recomputed from the pages themselves, every template in both looks: a
    change to the page's script or stylesheet fails here until
    SITE_*_HASHES -- and the Worker's policy -- are updated (see site_csp)."""
    for template in TEMPLATES:
        for theme in THEMES:
            html = _page(template, theme)

            scripts = [m.group("body") for m in SCRIPT.finditer(html) if "ld+json" not in m.group("attrs")]
            styles = [m.group("body") for m in STYLE.finditer(html)]
            assert scripts and styles
            assert {site_render.csp_hash(s) for s in scripts} <= set(site_render.SITE_SCRIPT_HASHES)
            assert {site_render.csp_hash(s) for s in styles} <= set(site_render.SITE_STYLE_HASHES)
            # The only style attribute is the club's colour and font on <html>.
            assert re.findall(r'\sstyle="', html) == [' style="']
            assert re.search(r'<html lang="en" style="--brand:#3355ff;', html)


async def test_the_hashes_are_the_code_as_it_stands():
    assert site_render.SITE_SCRIPT_HASHES == tuple(site_render.csp_hash(s) for s in site_render.inline_scripts())
    assert site_render.SITE_STYLE_HASHES == tuple(site_render.csp_hash(s) for s in site_render.inline_styles())
    csp = site_render.site_csp()
    assert "unsafe-inline" not in csp.replace("style-src-attr 'unsafe-inline'", "")
    assert "script-src 'sha256-" in csp and "frame-ancestors 'none'" in csp


async def test_a_colour_that_is_not_one_never_reaches_the_style_attribute():
    html = site_render.render({**_data(), "club": {**_data()["club"], "brand_primary": '#1"><x'}})

    assert '<html lang="en" style="--brand:#17a58f;' in html and "<x" not in html
