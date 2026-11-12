"""Every club-site template package under app/api/sites/templates/<key>/.

Loops over whatever packages exist (a folder with a template.json), so a new
package is tested the moment it lands. Each one is rendered by the reference
renderer (scripts/site_templates/render.py: plain Jinja2 with the platform's
rules) with its own sample.json, with real pictures, with a club that has
nothing but a name, with very long names, and in other section orders. The
page must come out as finished, well-formed HTML: no template syntax left,
tags balanced, unique ids, every <img> with width/height/alt, every link
https/mailto/tel/#, no "None", no empty sections, and under the size budget.

Pixel parity with the designer's files is checked by
scripts/site_templates/parity.py (it needs a browser), not here.
"""
from __future__ import annotations

import json
import re
import struct
from html.parser import HTMLParser

import pytest

from scripts.site_templates import render as R
from scripts.site_templates import variants as V

KEYS = R.packages()
CANONICAL = {"nav", "hero", "profile", "about", "expertise", "stats", "upcoming", "past", "photos", "speakers", "team", "sponsors", "join", "footer"}
SLOT = re.compile(r"^(hero\.cover|hero\.image|about\.image|photos\.\d+|join\.image|event\.fallback)$")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
IMG_URLS = ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg", "https://cdn.example.com/c.jpg"]


class Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.ids: list[str] = []
        self.imgs: list[dict] = []
        self.hrefs: list[str] = []
        self.text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "img":
            self.imgs.append(a)
        if tag == "a":
            self.hrefs.append(a.get("href"))
        if tag in ("style", "script"):
            self._skip += 1
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            self.errors.append(f"</{tag}> on a void element")
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1] if self.stack else '-'}>")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        if tag in ("style", "script"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


def check_page(html: str) -> Checker:
    body = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    for token in ("{{", "{%", "{#", "%}"):
        assert token not in body, f"template syntax left in the page: {token}"
    c = Checker()
    c.feed(html)
    c.close()
    assert not c.errors, c.errors[:5]
    assert c.stack == [], f"unclosed: {c.stack}"
    dup = {i for i in c.ids if c.ids.count(i) > 1}
    assert not dup, f"duplicate ids: {dup}"
    for img in c.imgs:
        assert img.get("width") and img.get("height"), f"img without width/height: {img}"
        assert "alt" in img, f"img without alt: {img}"
        assert img.get("src", "").startswith(("https://", "file://", "data:")), img
        if img.get("fetchpriority") != "high":
            assert img.get("loading") == "lazy", f"img not lazy: {img}"
    for href in c.hrefs:
        assert href and href.startswith(("https://", "mailto:", "tel:", "#")), f"bad link: {href!r}"
    text = " ".join(" ".join(c.text).split())
    assert not re.search(r"\bNone\b|\bUndefined\b|\bnan\b", text), re.search(r".{0,40}\b(None|Undefined|nan)\b.{0,40}", text).group(0)
    return c


def png_size(path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", head[16:24])


@pytest.mark.parametrize("key", KEYS)
def test_package_files_and_meta(key):
    d = R.TEMPLATES_DIR / key
    m = R.meta(key)
    assert m["key"] == key
    for f in ("name", "blurb", "fonts", "layout", "fixed_top", "fixed_bottom", "sections", "slots", "accent"):
        assert f in m, f"template.json lacks {f}"
    assert m["layout"] in ("single", "two_column")
    if m["layout"] == "two_column":
        assert m.get("fixed_side"), "a two-column template names its fixed side column"
    fixed = set(m["fixed_top"]) | set(m.get("fixed_side") or []) | set(m["fixed_bottom"])
    assert "nav" in m["fixed_top"] and "footer" in m["fixed_bottom"]
    movable = [s["key"] for s in m["sections"]]
    assert len(movable) == len(set(movable)) and not (set(movable) & fixed)
    assert set(movable) | fixed <= CANONICAL, set(movable) | fixed - CANONICAL
    for s in m["sections"]:
        assert s["label"] and s["default_title"] and len(s["default_title"]) <= 60 and isinstance(s["hideable"], bool)
        assert (d / "sections" / f"{s['key']}.html.j2").is_file()
    for k in fixed:
        assert (d / "sections" / f"{k}.html.j2").is_file(), f"no sections/{k}.html.j2"
    seen = set()
    for s in m["slots"]:
        assert SLOT.match(s["key"]) and s["key"] not in seen, s
        seen.add(s["key"])
        assert s["label"] and len(s["size"]) == 2 and s["section"] in (set(movable) | fixed)
    assert HEX.match(m["accent"]["default"]) and m["accent"]["vars"] == ["--accent"]
    css = R.stylesheet(key)
    assert "--accent" in css and "</style" not in css.lower()
    for f in ("page.html.j2", "style.css", "sample.json"):
        assert (d / f).is_file()
    assert png_size(d / "thumb.png") == (600, 400)
    assert png_size(d / "thumb-mobile.png") == (300, 600)


@pytest.mark.parametrize("key", KEYS)
def test_sample_renders_every_section(key):
    sample = R.sample(key)
    html = R.render(key, sample)
    check_page(html)
    shown = [k for k, _ in R.render_sections(key, sample)]
    assert shown == [s["key"] for s in R.meta(key)["sections"]], "the designer's sample fills every section"
    # The stylesheet goes in byte for byte (its CSP hash is taken from the file).
    assert f"<style>{R.stylesheet(key)}</style>" in html


@pytest.mark.parametrize("key", KEYS)
def test_real_pictures(key):
    data = V.with_images(R.sample(key), IMG_URLS)
    html = R.render(key, data)
    c = check_page(html)
    assert c.imgs, "the pictures are <img> elements"
    assert "placeholder" not in json.dumps(data)
    assert "repeating-linear-gradient" not in html or "ph-img" in html
    eager = [i for i in c.imgs if i.get("fetchpriority") == "high"]
    assert len(eager) <= 2, "only the hero's picture loads eagerly"
    for i in c.imgs:
        if i.get("srcset"):
            assert i.get("sizes"), f"srcset without sizes: {i}"
    assert len(html.encode()) <= 150_000, f"{len(html.encode())} bytes, over the 150 KB budget"


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("name", ["Chess", V.LONG])
def test_club_with_nothing_but_a_name(key, name):
    data = V.sparse(key, name)
    html = R.render(key, data)
    check_page(html)
    assert R.render_sections(key, data) == [], "a section with no data renders nothing"
    assert name in html


@pytest.mark.parametrize("key", KEYS)
def test_long_texts(key):
    check_page(R.render(key, V.long_names(key)))


@pytest.mark.parametrize("key", KEYS)
def test_any_order_and_hiding(key):
    data = V.with_images(R.sample(key), IMG_URLS)
    movable = [s["key"] for s in R.meta(key)["sections"]]
    for order in (list(reversed(movable)), movable[1:] + movable[:1]):
        shown = [k for k, _ in R.render_sections(key, data, order=order)]
        assert shown == order
        check_page(R.render(key, data, order=order))
    hidden = movable[::2]
    shown = [k for k, _ in R.render_sections(key, data, hidden=hidden)]
    assert shown == [k for k in movable if k not in hidden]
    check_page(R.render(key, data, hidden=hidden))


@pytest.mark.parametrize("key", KEYS)
def test_titles_accent_and_escaping(key):
    sample = R.sample(key)
    defaults = {s["key"]: s["default_title"] for s in R.meta(key)["sections"]}
    plain = dict(R.render_sections(key, sample))
    data = json.loads(json.dumps(sample))
    data["site"]["title_overrides"] = {k: f"Custom {k} title" for k in defaults}
    data["site"]["accent"] = "#123456"
    renamed = dict(R.render_sections(key, data))
    for k, t in defaults.items():
        if t in plain[k]:
            assert f"Custom {k} title" in renamed[k], f"{k}: the club's title replaces {t!r}"
    html = R.render(key, data)
    assert "--accent: #123456" in html or "--accent:#123456" in html
    evil = json.loads(json.dumps(sample))
    evil["club"]["name"] = '<script>alert(1)</script>"'
    evil["club"]["short_name"] = None
    html = R.render(key, evil)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
