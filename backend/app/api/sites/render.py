"""The page itself: one HTML file, made from the club's data in one of the
designer's templates (app/api/sites/registry.py), with the sections in the
club's order, under its titles, in the template's colours or the club's.

How a page is made (`render`):

1. Each movable section the club shows is rendered on its own, in the
   club's order; one with nothing to show renders nothing and is left out
   (and out of the nav).
2. The template's frame (`page.html.j2`) is rendered around them.
3. The platform's parts go in, the same whatever the template: the head's
   description, Open Graph, canonical address, icon and JSON-LD; the
   preconnects to Google Fonts; the club's accent colour on <html> when it
   chose one; and the one shared script (the visit beacon) before </body>.

Jinja2 escapes every value (autoescape), every address in the data is
https, mailto or tel (service.py makes it so), and colours are #rrggbb
(layout.hex_colour): nothing a club types becomes markup. The JSON-LD goes
through `ld_json`.

The Content-Security-Policy names the inline code by hash (`site_csp`):
each template's stylesheet, and the one script. The stylesheets are the same
bytes on every club's page, so a template's hash never changes with a club;
everything a club chooses is in the markup or in the style attribute on
<html> (`style-src-attr 'unsafe-inline'`, which the designs' inline styles
need anyway).
"""
import base64
import hashlib
import html as html_lib
import json
import logging
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from markupsafe import Markup

from app.api.sites import registry
from app.api.sites.layout import hex_colour, normalise
from app.api.sites.registry import Package
from app.core.storage import media

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def esc(v) -> str:
    return html_lib.escape(str(v or ""), quote=True)


def image_url(site_url: str, img, variation: str) -> str | None:
    """One of the bucket's images at one size: its address on the media CDN
    when that is on, else the page's own image route; None when there is none."""
    if img is None:
        return None
    paths = getattr(img, "variations", None) or {}
    key = paths.get(variation) or paths.get("original")
    if not key:
        return None
    return media.cdn_url(key) or f"{site_url}/i?k={quote(key, safe='')}"


def image_srcset(img) -> str | None:
    """Every size of the image, for the img's srcset: only on the media CDN
    (media.srcset), where each size is a cached file rather than a redirect
    through the API."""
    return media.srcset(img)


def ld_json(value) -> str:
    """JSON for inside a <script> element. `json.dumps` leaves `<`, `>` and
    `&` as they are, so a club named `</script><script>...` closed the
    element and ran its own script on the club's page. As \\u escapes they
    are the same JSON to any parser, and never markup."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# ─── The one script ──────────────────────────────────────────────────────
#
# A visit, counted for the club's analytics -- only where the page is the
# whole tab (its own address). Framed (the events site, the dashboard's
# preview), the page around it counts or does not. The address comes from
# the head (<meta name="moa:visit">), so the script is the same bytes on
# every page and the CSP names it by hash.

SITE_SCRIPT = (
    "(function(){try{var m=document.querySelector('meta[name=\"moa:visit\"]'),v=m&&m.getAttribute('content');"
    "if(v&&window.top===window){var q=new URLSearchParams(location.search);"
    "fetch(v,{method:'POST',mode:'cors',keepalive:true,headers:{'content-type':'text/plain'},"
    "body:JSON.stringify({ref:q.get('ref'),via:q.get('via'),referrer:document.referrer||null})}).catch(function(){})}}catch(e){}})();"
)


# ─── Making the page ─────────────────────────────────────────────────────


def titles_for(pkg: Package, overrides: dict | None) -> dict:
    """Every movable section's title: the club's, else the template's."""
    overrides = overrides or {}
    return {s.key: overrides.get(s.key) or s.default_title for s in pkg.sections}


def assemble(pkg: Package, data: dict, layout: dict) -> str:
    """The template's page for `data`, the sections in `layout`'s order
    minus the hidden ones, each under its title -- without the platform's
    parts (`finish`). What the parity harness compares with the designer's file."""
    titles = titles_for(pkg, layout.get("titles"))
    ctx = {**data, "css": pkg.css, "titles": titles, "year": data.get("year") or datetime.now(IST).year}
    hidden = set(layout.get("hidden") or [])
    parts, nav = [], []
    for key in layout["order"]:
        if key in hidden:
            continue
        s = pkg.section(key)
        if s is None:
            continue
        # `index`: its 1-based place among the movable sections on the page
        # (Night numbers its headings "01 / about"); one that renders empty
        # takes no number.
        section = {"key": s.key, "title": titles[s.key], "label": s.label, "anchor": s.anchor, "index": len(parts) + 1}
        out = pkg.env.get_template(f"sections/{key}.html.j2").render({**ctx, "section": section}).strip()
        if out:
            parts.append(out)
            nav.append({"key": s.key, "label": s.label, "title": titles[s.key], "href": f"#{s.anchor}"})
    return pkg.env.get_template("page.html.j2").render({**ctx, "sections_html": Markup("\n".join(parts)), "nav": nav})


_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.I)
_TITLE = re.compile(r"<title\b[^>]*>.*?</title\s*>", re.I | re.S)
_HTML_OPEN = re.compile(r"<html\b([^>]*)>", re.I)
_LANG_ATTR = re.compile(r'\slang="[^"]*"', re.I)
LANG = "en-IN"
_STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*(["'])(.*?)\1""", re.I | re.S)

PRECONNECT = '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'


def _with_lang(attrs: str) -> str:
    """The <html> element's attributes with lang="en-IN", like the rest of
    myotherapp.in (the designs say "en")."""
    return _LANG_ATTR.sub(f' lang="{LANG}"', attrs, count=1) if _LANG_ATTR.search(attrs) else f' lang="{LANG}"{attrs}'


def _head(data: dict) -> str:
    """The platform's part of <head>: what search engines, link previews and the visit beacon read."""
    page = data.get("page") or {}
    club = data.get("club") or {}
    url = page.get("url") or ""
    title = page.get("title") or club.get("name") or ""
    desc = page.get("description") or ""
    cover = (data.get("images") or {}).get("hero.cover") or club.get("cover")
    logo = club.get("logo")
    preview = (cover or logo or {}).get("url")
    socials = [s["href"] for s in data.get("socials") or [] if s.get("href", "").startswith("https://")]
    ld = {"@context": "https://schema.org", "@type": "Organization", "name": club.get("name"), "url": url, "description": desc}
    if logo and logo.get("url"):
        ld["logo"] = logo["url"]
    if socials:
        ld["sameAs"] = socials
    # A wide picture (the cover) previews as a large card; the logo alone as a small one.
    card = "summary_large_image" if (cover or {}).get("url") else "summary"
    name = club.get("name") or title
    parts = [
        f'<meta name="description" content="{esc(desc)}">',
        f'<meta property="og:title" content="{esc(title)}">',
        f'<meta property="og:description" content="{esc(desc)}">',
        f'<meta property="og:image" content="{esc(preview)}">' if preview else "",
        f'<meta property="og:image:alt" content="{esc(name)}">' if preview else "",
        f'<meta property="og:url" content="{esc(url)}">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{esc(name)}">',
        '<meta property="og:locale" content="en_IN">',
        f'<meta name="twitter:card" content="{card}">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        f'<meta name="twitter:description" content="{esc(desc)}">',
        f'<meta name="twitter:image" content="{esc(preview)}">' if preview else "",
        f'<link rel="canonical" href="{esc(url)}">' if url else "",
        f'<link rel="icon" href="{esc(logo["url"])}">' if logo and logo.get("url") else "",
        f'<meta name="moa:visit" content="{esc(data.get("visit_url"))}">' if data.get("visit_url") else "",
        f'<script type="application/ld+json">{ld_json(ld)}</script>',
    ]
    return "".join(parts)


def finish(page: str, pkg: Package, data: dict, accent: str | None) -> str:
    """The platform's parts put into the template's page (see the module docstring)."""
    title = esc((data.get("page") or {}).get("title") or (data.get("club") or {}).get("name") or "")
    head = _head(data)
    if _TITLE.search(page):
        page = _TITLE.sub(lambda m: f"<title>{title}</title>", page, count=1)
    else:
        head = f"<title>{title}</title>" + head
    m = _HEAD_OPEN.search(page)
    if m:
        page = page[:m.end()] + PRECONNECT + page[m.end():]
    page = _HTML_OPEN.sub(lambda m: f"<html{_with_lang(m.group(1))}>", page, count=1)
    at = page.lower().find("</head>")
    if at >= 0:
        page = page[:at] + head + page[at:]
    colour = hex_colour(accent)
    if colour:
        decl = ";".join(f"{v}:{colour}" for v in pkg.accent_vars)

        def with_accent(m: re.Match) -> str:
            attrs = m.group(1)
            s = _STYLE_ATTR.search(attrs)
            if s:
                attrs = attrs[:s.start()] + f' style="{decl};{s.group(2)}"' + attrs[s.end():]
            else:
                attrs += f' style="{decl}"'
            return f"<html{attrs}>"

        page = _HTML_OPEN.sub(with_accent, page, count=1)
    script = f"<script>{SITE_SCRIPT}</script>"
    at = page.lower().rfind("</body>")
    page = page[:at] + script + page[at:] if at >= 0 else page + script
    return page


def render(data: dict) -> str:
    """`data` is what `service.site_data` gathers (the render input in
    feature-register/site-templates-contract.md, "Changes"); `data["site"]`
    names the template and carries the club's layout."""
    site = data.get("site") or {}
    pkg = registry.resolve(site.get("template"))
    layout = normalise(site.get("layout") or site, pkg)  # a sample.json may put the layout's fields on `site` itself
    return finish(assemble(pkg, data, layout), pkg, data, layout["accent"])


# ─── The Content-Security-Policy ─────────────────────────────────────────
#
# Every template's stylesheet and the one script, by hash, for the CSP the
# API sends with a club's page (and the club-sites Worker passes on). A
# template's hash is computed when the packages are read: a new template or
# a changed stylesheet is in the policy on the next start, with nothing to
# copy by hand. Rendering each package's sample once also finds any other
# <style> element a template carries (one in a section), so it is named too.


def csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


_STYLE_EL = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)
_SCRIPT_EL = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_hashes: tuple[dict, tuple[str, ...]] | None = None


def inline_styles(html: str) -> list[str]:
    return _STYLE_EL.findall(html)


def inline_scripts(html: str) -> list[str]:
    """The page's executable inline scripts (not its JSON-LD, which runs nothing)."""
    return [body for attrs, body in _SCRIPT_EL.findall(html) if "ld+json" not in attrs.lower()]


def _package_styles(pkg: Package) -> list[str]:
    found = [str(pkg.css)]
    if pkg.sample:
        try:
            site = pkg.sample.get("site") or {}
            found += inline_styles(assemble(pkg, pkg.sample, normalise(site.get("layout") or site, pkg)))
        except Exception:  # noqa: BLE001 -- a sample that does not render only costs its extra hashes
            logger.exception("site template %s: its sample.json did not render", pkg.key)
    return found


def style_hashes() -> tuple[str, ...]:
    """Every template's inline stylesheets, by hash, in catalogue order."""
    global _hashes
    ps = registry.packages()
    if _hashes is None or _hashes[0] is not ps:
        out = dict.fromkeys(csp_hash(s) for pkg in ps.values() for s in _package_styles(pkg))
        _hashes = (ps, tuple(out))
    return _hashes[1]


def script_hashes() -> tuple[str, ...]:
    return (csp_hash(SITE_SCRIPT),)


def site_csp(api_origin: str = "https://api.myotherapp.in", *, frame_ancestors: str = "'none'") -> str:
    """The policy for a club's page: its script and stylesheets by hash,
    Google Fonts, images from anywhere on https (the image route redirects
    to the bucket), the visit beacon to the API.

    Styles: `style-src-elem` names the stylesheets by hash and
    `style-src-attr` lets the designs' style attributes through. A browser
    older than those two directives reads `style-src` instead, which is
    'unsafe-inline' so the page still has its look there; every browser
    since 2022 holds the <style> elements to their hashes."""
    styles = " ".join(style_hashes())
    parts = [
        "default-src 'none'",
        f"script-src {' '.join(script_hashes())}",
        "style-src 'unsafe-inline' https://fonts.googleapis.com",
        f"style-src-elem {styles} https://fonts.googleapis.com",
        "style-src-attr 'unsafe-inline'",
        "font-src https://fonts.gstatic.com",
        "img-src https: data:",
        f"connect-src {api_origin}",
        "base-uri 'none'",
        "form-action 'none'",
    ]
    if frame_ancestors:
        parts.append(f"frame-ancestors {frame_ancestors}")
    return "; ".join(parts)
