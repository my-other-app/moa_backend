"""The page itself: one HTML file with its CSS and a little JS inline, in
one of four templates, light or dark, in the club's colour and font.

Every string from the database goes through `esc` (or, inside the JSON-LD
script, `ld_json`). Images are on the media CDN when it is on -- permanent
addresses, each size in the img's srcset -- and otherwise go through the
site's own image route, because the bucket's URLs are signed and expire; the
page must outlive them. The JS is small on purpose: the menu on a phone, a
countdown on the Arena template, the year in the footer.

The inline script and the inline stylesheet are the same bytes on every
club's page -- one stylesheet per template and look, eight in all -- so the
Content-Security-Policy can name them by hash (`SITE_CSP`) instead of
allowing any inline script with 'unsafe-inline'. The club's colour and font
are the only parts that differ; they are CSS custom properties on the
<html> element's style attribute, from a validated hex colour and a fixed
list of fonts.
"""
import base64
import hashlib
import html
import json
import re
from datetime import datetime
from urllib.parse import quote

from app.api.sites.models import FONTS, TEMPLATES, THEMES
from app.core.storage import media



def esc(v) -> str:
    return html.escape(str(v or ""), quote=True)


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex(v: str | None, fallback: str) -> str:
    """A colour the page may print: six hex digits, or the fallback. It goes
    into the <html> style attribute, so nothing else may get through."""
    v = (v or "").strip()
    return v if _HEX.match(v) else fallback


def _mix(hex_: str, alpha: float) -> str:
    h = hex_.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _dark_enough(hex_: str) -> bool:
    h = hex_.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) < 150


def image_url(site_url: str, img, variation: str) -> str | None:
    """One of the bucket's images at one size: its address on the media CDN
    when that is on, else the page's own image route; None when the club has none."""
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


def _day(dt: datetime) -> tuple[str, str]:
    return dt.strftime("%b").upper(), str(dt.day)


def _when(dt: datetime) -> str:
    return dt.strftime("%a %d %b · %I:%M %p").replace(" 0", " ").lstrip("0")


# ─── The CSS ─────────────────────────────────────────────────────────────


DEFAULT_BRAND = "#17a58f"


def _brand_vars(brand: str, font_family: str) -> str:
    """The club's own colour and font, as the custom properties the
    stylesheet reads. Set on <html>, so the stylesheet itself is the same
    for every club and can be named in the CSP by its hash."""
    on_brand = "#ffffff" if _dark_enough(brand) else "#0b1f1a"
    return (
        f"--brand:{brand};--brand-soft:{_mix(brand, 0.14)};--on-brand:{on_brand};"
        f"--font:'{font_family}',system-ui,-apple-system,Segoe UI,sans-serif"
    )


def _css(template: str, theme: str) -> str:
    """The stylesheet for one template and look; nothing in it varies by club."""
    dark = theme == "dark"
    bg = "#0b0e0d" if dark else "#f7f8f6"
    surface = "#151918" if dark else "#ffffff"
    sunken = "#1b201e" if dark else "#eef0ec"
    line = "#2a302d" if dark else "#e3e6e1"
    ink = "#eef1ee" if dark else "#151918"
    ink2 = "#c5cbc7" if dark else "#4b524e"
    ink3 = "#8f9793" if dark else "#7b827e"
    radius = {"launchpad": "18px", "chapter": "6px", "campus": "14px", "arena": "10px"}[template]
    # The defaults behind the club's own colour and font (set on <html>), for
    # a browser that refuses the style attribute.
    defaults = _brand_vars(DEFAULT_BRAND, FONTS["dm_sans"][0])
    base = f"""
:root{{--bg:{bg};--surface:{surface};--sunken:{sunken};--line:{line};--ink:{ink};--ink2:{ink2};--ink3:{ink3};{defaults};--r:{radius}}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}
body{{margin:0;font-family:var(--font);color:var(--ink);background:var(--bg);line-height:1.5;-webkit-font-smoothing:antialiased}}
a{{color:inherit;text-decoration:none}}img{{display:block;max-width:100%}}
.wrap{{width:min(1120px,calc(100% - 40px));margin:0 auto}}
.nav{{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}}
.nav .wrap{{display:flex;align-items:center;gap:14px;height:64px}}
.brand{{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;letter-spacing:-.01em}}
.brand img,.brand .mark{{width:34px;height:34px;border-radius:10px;object-fit:cover;background:var(--brand);color:var(--on-brand);display:grid;place-items:center;font-size:13px;font-weight:700}}
.links{{display:flex;gap:4px;margin-left:auto;align-items:center}}
.links a{{padding:8px 12px;border-radius:999px;font-size:14px;color:var(--ink2)}}.links a:hover{{background:var(--sunken);color:var(--ink)}}
.menu{{display:none;margin-left:auto;background:none;border:1px solid var(--line);border-radius:10px;padding:8px 10px;color:var(--ink);font:inherit}}
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:44px;padding:0 20px;border-radius:999px;font-weight:600;font-size:15px;border:1px solid transparent;cursor:pointer;font-family:inherit}}
.btn-primary{{background:var(--brand);color:var(--on-brand)}}.btn-primary:hover{{filter:brightness(1.06)}}
.btn-ghost{{border-color:var(--line);background:var(--surface);color:var(--ink)}}.btn-ghost:hover{{background:var(--sunken)}}
.btn-sm{{height:36px;padding:0 14px;font-size:13.5px}}
section{{padding:56px 0}}
h1{{font-size:clamp(36px,6vw,64px);line-height:1.02;letter-spacing:-.03em;margin:0 0 16px;font-weight:700}}
h2{{font-size:28px;letter-spacing:-.02em;margin:0 0 18px;font-weight:700}}
.sub{{font-size:18px;color:var(--ink2);max-width:640px;margin:0 0 28px}}
.pill{{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;background:var(--sunken);color:var(--ink2);font-size:13px;margin-bottom:18px}}
.pill i{{width:8px;height:8px;border-radius:50%;background:var(--brand)}}
.grid{{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px;display:flex;flex-direction:column;gap:10px}}
.date{{display:inline-grid;place-items:center;width:56px;height:56px;border-radius:12px;background:var(--brand-soft);color:var(--brand);font-weight:700;line-height:1}}
.date small{{font-size:10px;letter-spacing:.08em}}.date b{{font-size:22px}}
.card h3{{margin:4px 0 0;font-size:17px;letter-spacing:-.01em}}.card p{{margin:0;color:var(--ink3);font-size:13.5px}}
.card .btn{{margin-top:auto;align-self:flex-start}}
.poster{{position:relative;display:block;aspect-ratio:4/5;overflow:hidden;border-radius:calc(var(--r) - 6px);background:var(--sunken)}}
.poster img{{position:absolute;inset:0;width:100%;height:100%;max-width:none}}.poster .bg{{object-fit:cover;filter:blur(20px) saturate(1.1) brightness(.92);transform:scale(1.15)}}.poster .fg{{object-fit:contain}}
.row{{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
.more{{color:var(--brand);font-weight:600;font-size:14px}}
.nums{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:20px}}
.num{{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px}}
.num b{{display:block;font-size:30px;letter-spacing:-.02em;line-height:1}}.num span{{color:var(--ink3);font-size:13px}}
.about p{{font-size:16px;color:var(--ink2);max-width:720px;white-space:pre-line}}
.people{{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(180px,1fr))}}
.person{{text-align:center;padding:18px 12px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r)}}
.person img,.person .mark{{width:72px;height:72px;border-radius:50%;object-fit:cover;margin:0 auto 10px;background:var(--brand-soft);color:var(--brand);display:grid;place-items:center;font-weight:700;font-size:20px}}
.person b{{display:block;font-size:15px}}.person span{{color:var(--ink3);font-size:13px}}
.person .so{{display:flex;justify-content:center;gap:8px;margin-top:8px}}.person .so a{{font-size:12px;color:var(--brand)}}
.logos{{display:flex;flex-wrap:wrap;gap:12px}}
.logo{{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--surface);border:1px solid var(--line);border-radius:999px;font-size:14px}}
.logo img,.logo .mark{{width:28px;height:28px;border-radius:8px;object-fit:cover;background:var(--sunken);display:grid;place-items:center;font-size:11px;font-weight:700}}
.logo span{{color:var(--ink3);font-size:12px}}
/* A logo whole on white: a square one fills its tile, any other shape gets room around it (_JS adds .pad). */
.brand img,.logo img{{object-fit:contain;background:#fff}}.brand img.pad,.logo img.pad{{padding:3px}}
.band{{background:var(--brand);color:var(--on-brand);border-radius:var(--r);padding:32px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}}
.band h2{{margin:0 0 4px}}.band p{{margin:0;opacity:.85}}
.band .btn{{background:var(--on-brand);color:var(--brand)}}
footer{{border-top:1px solid var(--line);padding:28px 0;color:var(--ink3);font-size:13px}}
footer .wrap{{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center}}footer a{{color:var(--ink2)}}footer .made{{margin-left:auto}}
.empty{{color:var(--ink3);font-size:14px}}
@media (max-width:720px){{.links{{display:none;position:absolute;left:0;right:0;top:64px;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 20px 16px;flex-direction:column;align-items:stretch}}.links.open{{display:flex}}.menu{{display:block}}section{{padding:40px 0}}}}
"""
    if template == "launchpad":
        base += """
.hero{text-align:center;padding:84px 0 64px}.hero .sub{margin-left:auto;margin-right:auto}.hero .cta{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
"""
    elif template == "chapter":
        base += """
.hero{padding:72px 0 48px;border-bottom:1px solid var(--line)}.hero h1{max-width:760px}
.grid{grid-template-columns:1fr}.card{flex-direction:row;align-items:center;gap:18px}.card .btn{margin-top:0;margin-left:auto}.card h3{margin:0}
h2{font-size:22px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);font-weight:600}
"""
    elif template == "campus":
        base += """
.hero{width:min(1120px,calc(100% - 40px));margin:20px auto 0;border-radius:var(--r);overflow:hidden;background:var(--surface);border:1px solid var(--line)}
.hero .cover{display:block;width:100%;max-width:none;aspect-ratio:3/1;object-fit:cover;background:linear-gradient(135deg,var(--brand),color-mix(in srgb,var(--brand) 55%,#0b1413))}
.hero .wrap{width:auto;padding:28px clamp(20px,4vw,40px) 36px}
.gallery{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}.gallery .poster{border-radius:var(--r)}
"""
    elif template == "arena":
        base += """
.hero{padding:72px 0 56px;background:radial-gradient(900px 400px at 10% 0%,var(--brand-soft),transparent 60%)}
.count{display:flex;gap:10px;margin:0 0 24px;flex-wrap:wrap}.count div{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:12px 16px;min-width:84px;text-align:center}
.count b{display:block;font-size:30px;letter-spacing:-.02em;line-height:1;font-variant-numeric:tabular-nums}.count span{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3)}
h1{text-transform:uppercase;letter-spacing:-.01em}
"""
    return base


_JS = """
(function(){var m=document.querySelector('.menu'),l=document.querySelector('.links');if(m&&l){m.addEventListener('click',function(){l.classList.toggle('open')})}
var y=document.getElementById('y');if(y){y.textContent=new Date().getFullYear()}
// A logo that is not square (a wordmark) is shown whole with room around it; a square one fills its tile.
document.querySelectorAll('.brand img,.logo img').forEach(function(i){function f(){if(i.naturalWidth&&Math.abs(i.naturalWidth/i.naturalHeight-1)>0.03){i.classList.add('pad')}}if(i.complete){f()}else{i.addEventListener('load',f)}});
document.querySelectorAll('[data-when]').forEach(function(el){var d=new Date(el.getAttribute('data-when'));if(!isNaN(d)){el.textContent=d.toLocaleString(undefined,{weekday:'short',day:'numeric',month:'short',hour:'numeric',minute:'2-digit'})}});
var c=document.querySelector('[data-countdown]');if(c){var t=new Date(c.getAttribute('data-countdown')).getTime();function tick(){var s=Math.max(0,Math.floor((t-Date.now())/1000));var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),mi=Math.floor(s%3600/60),se=s%60;c.querySelector('[data-d]').textContent=d;c.querySelector('[data-h]').textContent=('0'+h).slice(-2);c.querySelector('[data-m]').textContent=('0'+mi).slice(-2);c.querySelector('[data-s]').textContent=('0'+se).slice(-2)}tick();setInterval(tick,1000)}
// A visit, counted for the club's analytics -- only where the page is the whole tab (its own address).
// Framed (the events site, the dashboard's preview), the page around it counts or does not.
try{var v=document.body.getAttribute('data-visit');if(v&&window.top===window){var q=new URLSearchParams(location.search);fetch(v,{method:'POST',mode:'cors',keepalive:true,headers:{'content-type':'text/plain'},body:JSON.stringify({ref:q.get('ref'),via:q.get('via'),referrer:document.referrer||null})}).catch(function(){})}}catch(e){}
})();
"""


# ─── The page ────────────────────────────────────────────────────────────


def _href(url) -> str:
    """A link the page may carry: http(s) or mail, else nowhere. The data
    is built that way (service._social_href); this holds whatever it is
    handed."""
    u = str(url or "").strip()
    return esc(u) if u.lower().startswith(("https://", "http://", "mailto:")) else "#"


def _mark(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    return esc("".join(p[0] for p in parts[:2]).upper() or "C")


# How wide each kind of picture is drawn, for the browser to pick a size from
# the srcset before it has laid the page out (the stylesheet above).
SIZES = {
    "brand": "34px",
    "person": "72px",
    "partner": "28px",
    "poster": "(max-width: 720px) calc(100vw - 40px), 380px",
    "gallery": "(max-width: 720px) 50vw, 280px",
}


def _srcset(srcset: str | None, kind: str) -> str:
    """The srcset and sizes attributes of an img tag, leading space and all; nothing without a srcset."""
    if not srcset:
        return ""
    return f' srcset="{esc(srcset)}" sizes="{esc(SIZES[kind])}"'


def _img(url: str | None, name: str, cls: str = "", *, srcset: str | None = None, kind: str = "brand") -> str:
    """An image tag, or the initials in a mark when the club gave none."""
    if url:
        return f'<img src="{esc(url)}"{_srcset(srcset, kind)} alt="" loading="lazy"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>'
    return f'<span class="mark">{_mark(name)}</span>'


def _poster(e: dict, kind: str) -> str:
    """An event's poster in its 4:5 frame, never cropped: whole, over a
    blurred copy of itself (the same file, so one download). A 4:5 poster
    covers its copy completely."""
    src = esc(e["poster"])
    srcset = _srcset(e.get("poster_srcset"), kind)
    alt = esc(e["name"]) if kind == "gallery" else ""
    return (
        f'<span class="poster"><img class="bg" src="{src}"{srcset} alt="" aria-hidden="true" loading="lazy">'
        f'<img class="fg" src="{src}"{srcset} alt="{alt}" loading="lazy"></span>'
    )


def _event_card(e: dict, template: str, register: str) -> str:
    mon, day = _day(e["starts_at"])
    meta = " · ".join(x for x in (e.get("where"), e.get("fee")) if x)
    poster = _poster(e, "poster") if e.get("poster") and template == "campus" else ""
    when = esc(_when(e["starts_at"]))
    meta_html = f" · {esc(meta)}" if meta else ""
    label = "Register" if e.get("upcoming") else "See the event"
    return (
        f'<article class="card">{poster}<span class="date"><small>{mon}</small><b>{day}</b></span>'
        f'<h3>{esc(e["name"])}</h3><p><span data-when="{esc(e["starts_at"].isoformat())}">{when}</span>{meta_html}</p>'
        f'<a class="btn btn-primary btn-sm" href="{esc(register)}" target="_top">{label}</a></article>'
    )


def render(data: dict) -> str:
    """`data` is what `service.site_data` gathers: the club, its choices, the events, the team, the partners, the numbers."""
    s = data["site"]
    club = data["club"]
    template = s["template"]
    on = s["sections"]
    font_family, font_query = FONTS.get(s["font"], FONTS["dm_sans"])
    brand = _hex(club.get("brand_primary"), DEFAULT_BRAND)
    css = _css(template, s["theme"] if s["theme"] in THEMES else "light")
    name = club["name"]
    headline = s["headline"] or data["default_headline"]
    subline = s["subline"] or data["default_subline"] or ""
    upcoming = data["upcoming"]
    past = data["past"]
    nxt = upcoming[0] if upcoming else None
    logo = club.get("logo_url")
    cover = club.get("cover_url")
    site_url = data["site_url"]
    follow_url = data.get("follow_url")

    nav_links = []
    if on.get("upcoming", True):
        nav_links.append('<a href="#events">Events</a>')
    if on.get("about", True):
        nav_links.append('<a href="#about">About</a>')
    if on.get("team", True) and data["team"]:
        nav_links.append('<a href="#team">Team</a>')
    if on.get("partners", True) and data["partners"]:
        nav_links.append('<a href="#partners">Partners</a>')
    if on.get("contact", True):
        nav_links.append('<a href="#contact">Contact</a>')
    follow_btn = f'<a class="btn btn-primary btn-sm" href="{esc(follow_url)}" target="_top">Follow</a>' if follow_url else ""
    brand_img = _img(logo, name, srcset=club.get("logo_srcset"), kind="brand")
    parts = [
        f'<nav class="nav"><div class="wrap"><a class="brand" href="#top">{brand_img}<span>{esc(name)}</span></a>'
        f'<button class="menu" type="button" aria-label="Menu">☰</button><div class="links">{"".join(nav_links)}{follow_btn}</div></div></nav>'
    ]

    if on.get("hero", True):
        pill = f'<span class="pill"><i></i>Next up · {esc(nxt["name"])} · {esc(_when(nxt["starts_at"]))}</span>' if nxt else ""
        see = '<a class="btn btn-primary" href="#events">See upcoming events</a>' if on.get("upcoming", True) else ""
        join = f'<a class="btn btn-ghost" href="{esc(follow_url)}" target="_top">Join the club</a>' if follow_url else ""
        cta = f'<div class="cta">{see}{join}</div>'
        text = f'{pill}<h1>{esc(headline)}</h1><p class="sub">{esc(subline)}</p>'
        if template == "campus":
            # The banner, 3:1 as everywhere, above the words (not behind them: on a phone a
            # hero tall enough for the words would show a third of a 3:1 banner).
            img = f'<img class="cover" src="{esc(cover)}" alt="">' if cover else '<div class="cover"></div>'
            parts.append(f'<header class="hero" id="top">{img}<div class="wrap">{text}{cta}</div></header>')
        elif template == "arena" and nxt:
            count = (
                f'<div class="count" data-countdown="{esc(nxt["starts_at"].isoformat())}"><div><b data-d>0</b><span>days</span></div>'
                '<div><b data-h>00</b><span>hours</span></div><div><b data-m>00</b><span>min</span></div><div><b data-s>00</b><span>sec</span></div></div>'
            )
            parts.append(f'<header class="hero" id="top"><div class="wrap">{text}{count}{cta}</div></header>')
        else:
            parts.append(f'<header class="hero" id="top"><div class="wrap">{text}{cta}</div></header>')

    if on.get("upcoming", True):
        cards = "".join(_event_card(e, template, e["url"]) for e in upcoming) if upcoming else '<p class="empty">Nothing scheduled yet — follow the club to hear first.</p>'
        more = f'<a class="more" href="{esc(data["events_url"])}" target="_top">View all →</a>' if data.get("events_url") else ""
        parts.append(f'<section id="events"><div class="wrap"><div class="row"><h2>Upcoming events</h2>{more}</div><div class="grid">{cards}</div></div></section>')

    if on.get("about", True):
        nums = data["numbers"]
        home = f'<div class="num"><b>{esc(club["location_name"])}</b><span>home</span></div>' if club.get("location_name") else ""
        parts.append(
            f'<section id="about" class="about"><div class="wrap"><h2>About us</h2><p>{esc(club.get("about") or subline)}</p>'
            f'<div class="nums"><div class="num"><b>{nums["followers"]:,}</b><span>followers</span></div><div class="num"><b>{nums["events"]:,}</b><span>events held</span></div>{home}</div></div></section>'
        )

    if on.get("past", True) and past:
        pics = "".join(f'<a href="{esc(e["url"])}" title="{esc(e["name"])}" target="_top">{_poster(e, "gallery")}</a>' for e in past if e.get("poster")) if template == "campus" else ""
        body = f'<div class="gallery">{pics}</div>' if pics else '<div class="grid">' + "".join(_event_card(e, template, e["url"]) for e in past) + "</div>"
        parts.append(f'<section id="past"><div class="wrap"><h2>Past events</h2>{body}</div></section>')

    if on.get("team", True) and data["team"]:
        people = []
        for m in data["team"]:
            so = "".join(f'<a href="{_href(u)}" target="_blank" rel="noreferrer">{esc(k)}</a>' for k, u in m["links"])
            so_html = f'<div class="so">{so}</div>' if so else ""
            people.append(f'<div class="person">{_img(m.get("photo"), m["name"], srcset=m.get("photo_srcset"), kind="person")}<b>{esc(m["name"])}</b><span>{esc(m.get("role") or "")}</span>{so_html}</div>')
        parts.append(f'<section id="team"><div class="wrap"><h2>The team</h2><div class="people">{"".join(people)}</div></div></section>')

    if on.get("partners", True) and data["partners"]:
        logos = []
        for p in data["partners"]:
            role = f'<span>{esc(p["role"])}</span>' if p.get("role") else ""
            logos.append(f'<div class="logo">{_img(p.get("logo"), p["name"], srcset=p.get("logo_srcset"), kind="partner")}<div><div>{esc(p["name"])}</div>{role}</div></div>')
        parts.append(f'<section id="partners"><div class="wrap"><h2>Partners</h2><div class="logos">{"".join(logos)}</div></div></section>')

    if on.get("follow", True) and follow_url:
        parts.append(f'<section><div class="wrap"><div class="band"><div><h2>Follow {esc(name)}</h2><p>Hear first about every event, in the app.</p></div><a class="btn" href="{esc(follow_url)}" target="_top">Follow in the app</a></div></div></section>')

    if on.get("contact", True):
        links = "".join(f'<a href="{_href(u)}" target="_blank" rel="noreferrer">{esc(k)}</a>' for k, u in data["socials"])
        contact = " · ".join(esc(x) for x in (club.get("contact_email"), club.get("location_name")) if x)
        contact_html = f"<span>{contact}</span>" if contact else ""
        parts.append(f'<footer id="contact"><div class="wrap"><span>© <span id="y"></span> {esc(name)}</span>{contact_html}{links}<span class="made">Made with MyOtherApp</span></div></footer>')

    title = f"{name} · {headline}"
    desc = subline or f"{name} on MyOtherApp."
    og = f'<meta property="og:image" content="{esc(cover or logo)}">' if (cover or logo) else ""
    icon = f'<link rel="icon" href="{esc(logo)}">' if logo else ""
    ld = ld_json({"@context": "https://schema.org", "@type": "Organization", "name": name, "url": site_url, "description": desc})
    fonts_href = f"https://fonts.googleapis.com/css2?family={font_query}&display=swap"
    return (
        f'<!doctype html><html lang="en" style="{esc(_brand_vars(brand, font_family))}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title><meta name="description" content="{esc(desc)}"><meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}">{og}'
        f'<meta property="og:url" content="{esc(site_url)}"><meta property="og:type" content="website">'
        f'<link rel="canonical" href="{esc(site_url)}">{icon}'
        f'<link rel="preconnect" href="https://fonts.googleapis.com"><link href="{esc(fonts_href)}" rel="stylesheet">'
        f"<style>{css}</style></head><body data-visit=\"{esc(data.get('visit_url') or '')}\">{''.join(parts)}<script>{_JS}</script>"
        f'<script type="application/ld+json">{ld}</script></body></html>'
    )


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


# ─── The Content-Security-Policy ─────────────────────────────────────────
#
# The pages' inline script and stylesheets, by hash, for the CSP the
# club-sites Worker (infra/cloudflare/club-sites) and the API send. A change
# to `_JS` or `_css` changes a hash here: tests/test_sites_security.py
# recomputes them and fails until these -- and the Worker -- are updated.


def csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def inline_scripts() -> list[str]:
    return [_JS]


def inline_styles() -> list[str]:
    return [_css(t, th) for t in TEMPLATES for th in THEMES]


SITE_SCRIPT_HASHES: tuple = (
    "'sha256-G46dCTnrg774fseYkRyhOzn441jv+kKKGYkyWqTag+E='",
)
# One per template and look, in TEMPLATES x THEMES order.
SITE_STYLE_HASHES: tuple = (
    "'sha256-gUhBwHY5yVy/YkaZOf+qR+TesRdEqKuRyjCj099JqPc='",
    "'sha256-Q4Suj/LMDoGebqcJZphncuCsGVpmaowf/AKUC6BMSy0='",
    "'sha256-0P8agz3Mmyy9p+GwX9DyOInqA9kAQrGMbSQpEsXxP0k='",
    "'sha256-SyrvDSgdFesZyzF/3hIFuk2e3kX0SDOCi3I5Jd2VX3I='",
    "'sha256-s7qUVSv8/Oo435nvM2FSKd2snYgDfLkYgla/Rt57Zm8='",
    "'sha256-fsFW9rmnG695OknJ8Kg+KPg6W2bLsWCiERGCVEZeR6A='",
    "'sha256-xYtE49+16ppwE0WtKLFM0dlodBBmklI9kmOfB8I6BtI='",
    "'sha256-/y3LedMRrr6bdcvDR7Be2n6k6SAGgsw9WcEsy/t23X0='",
)


def site_csp(api_origin: str = "https://api.myotherapp.in", *, frame_ancestors: str = "'none'") -> str:
    """The policy for a club's page: its own inline script and stylesheets
    by hash, Google Fonts, images from anywhere on https (the image route
    redirects to the bucket), the visit beacon to the API."""
    parts = [
        "default-src 'none'",
        f"script-src {' '.join(SITE_SCRIPT_HASHES)}",
        f"style-src {' '.join(SITE_STYLE_HASHES)} https://fonts.googleapis.com",
        f"style-src-elem {' '.join(SITE_STYLE_HASHES)} https://fonts.googleapis.com",
        # The club's colour and font, as custom properties on <html>.
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
