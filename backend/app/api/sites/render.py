"""The page itself: one HTML file with its CSS and a little JS inline, in
one of four templates, light or dark, in the club's colour and font.

Every string from the database goes through `esc`. Images go through the
site's own image route, because the bucket's URLs are signed and expire;
the page must outlive them. The JS is small on purpose: the menu on a
phone, a countdown on the Arena template, the year in the footer.
"""
import html
import json
from datetime import datetime
from urllib.parse import quote

from app.api.sites.models import FONTS



def esc(v) -> str:
    return html.escape(str(v or ""), quote=True)


def _hex(v: str | None, fallback: str) -> str:
    v = (v or "").strip()
    return v if len(v) == 7 and v.startswith("#") else fallback


def _mix(hex_: str, alpha: float) -> str:
    h = hex_.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _dark_enough(hex_: str) -> bool:
    h = hex_.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) < 150


def image_url(site_url: str, img, variation: str) -> str | None:
    """The page's own image route for one of the bucket's images; None when the club has none."""
    if img is None:
        return None
    paths = getattr(img, "variations", None) or {}
    key = paths.get(variation) or paths.get("original")
    if not key:
        return None
    return f"{site_url}/i?k={quote(key, safe='')}"


def _day(dt: datetime) -> tuple[str, str]:
    return dt.strftime("%b").upper(), str(dt.day)


def _when(dt: datetime) -> str:
    return dt.strftime("%a %d %b · %I:%M %p").replace(" 0", " ").lstrip("0")


# ─── The CSS ─────────────────────────────────────────────────────────────


def _css(template: str, theme: str, brand: str, font_family: str) -> str:
    dark = theme == "dark"
    bg = "#0b0e0d" if dark else "#f7f8f6"
    surface = "#151918" if dark else "#ffffff"
    sunken = "#1b201e" if dark else "#eef0ec"
    line = "#2a302d" if dark else "#e3e6e1"
    ink = "#eef1ee" if dark else "#151918"
    ink2 = "#c5cbc7" if dark else "#4b524e"
    ink3 = "#8f9793" if dark else "#7b827e"
    on_brand = "#ffffff" if _dark_enough(brand) else "#0b1f1a"
    radius = {"launchpad": "18px", "chapter": "6px", "campus": "14px", "arena": "10px"}[template]
    base = f"""
:root{{--bg:{bg};--surface:{surface};--sunken:{sunken};--line:{line};--ink:{ink};--ink2:{ink2};--ink3:{ink3};--brand:{brand};--brand-soft:{_mix(brand, 0.14)};--on-brand:{on_brand};--r:{radius};--font:'{font_family}',system-ui,-apple-system,Segoe UI,sans-serif}}
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
.poster{{aspect-ratio:16/9;object-fit:cover;border-radius:calc(var(--r) - 6px);background:var(--sunken)}}
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
.band{{background:var(--brand);color:var(--on-brand);border-radius:var(--r);padding:32px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}}
.band h2{{margin:0 0 4px}}.band p{{margin:0;opacity:.85}}
.band .btn{{background:var(--on-brand);color:var(--brand)}}
footer{{border-top:1px solid var(--line);padding:28px 0;color:var(--ink3);font-size:13px}}
footer .wrap{{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center}}footer a{{color:var(--ink2)}}
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
.hero{position:relative;min-height:440px;display:flex;align-items:flex-end;padding:0 0 40px;border-radius:var(--r);overflow:hidden;margin-top:20px;color:#fff}
.hero .cover{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.hero .shade{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.05),rgba(0,0,0,.72))}
.hero .wrap{position:relative}.hero .sub{color:rgba(255,255,255,.85)}.hero .pill{background:rgba(255,255,255,.15);color:#fff}
.gallery{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}.gallery img{aspect-ratio:1;object-fit:cover;border-radius:var(--r)}
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
document.querySelectorAll('[data-when]').forEach(function(el){var d=new Date(el.getAttribute('data-when'));if(!isNaN(d)){el.textContent=d.toLocaleString(undefined,{weekday:'short',day:'numeric',month:'short',hour:'numeric',minute:'2-digit'})}});
var c=document.querySelector('[data-countdown]');if(c){var t=new Date(c.getAttribute('data-countdown')).getTime();function tick(){var s=Math.max(0,Math.floor((t-Date.now())/1000));var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),mi=Math.floor(s%3600/60),se=s%60;c.querySelector('[data-d]').textContent=d;c.querySelector('[data-h]').textContent=('0'+h).slice(-2);c.querySelector('[data-m]').textContent=('0'+mi).slice(-2);c.querySelector('[data-s]').textContent=('0'+se).slice(-2)}tick();setInterval(tick,1000)}
})();
"""


# ─── The page ────────────────────────────────────────────────────────────


def _mark(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    return esc("".join(p[0] for p in parts[:2]).upper() or "C")


def _img(url: str | None, name: str, cls: str = "") -> str:
    """An image tag, or the initials in a mark when the club gave none."""
    if url:
        return f'<img src="{esc(url)}" alt="" loading="lazy"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>'
    return f'<span class="mark">{_mark(name)}</span>'


def _event_card(e: dict, template: str, register: str) -> str:
    mon, day = _day(e["starts_at"])
    meta = " · ".join(x for x in (e.get("where"), e.get("fee")) if x)
    poster = f'<img class="poster" src="{esc(e["poster"])}" alt="" loading="lazy">' if e.get("poster") and template == "campus" else ""
    when = esc(_when(e["starts_at"]))
    meta_html = f" · {esc(meta)}" if meta else ""
    label = "Register" if e.get("upcoming") else "See the event"
    return (
        f'<article class="card">{poster}<span class="date"><small>{mon}</small><b>{day}</b></span>'
        f'<h3>{esc(e["name"])}</h3><p><span data-when="{esc(e["starts_at"].isoformat())}">{when}</span>{meta_html}</p>'
        f'<a class="btn btn-primary btn-sm" href="{esc(register)}">{label}</a></article>'
    )


def render(data: dict) -> str:
    """`data` is what `service.site_data` gathers: the club, its choices, the events, the team, the partners, the numbers."""
    s = data["site"]
    club = data["club"]
    template = s["template"]
    on = s["sections"]
    font_family, font_query = FONTS.get(s["font"], FONTS["dm_sans"])
    brand = _hex(club.get("brand_primary"), "#17a58f")
    css = _css(template, s["theme"], brand, font_family)
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
    follow_btn = f'<a class="btn btn-primary btn-sm" href="{esc(follow_url)}">Follow</a>' if follow_url else ""
    brand_img = _img(logo, name)
    parts = [
        f'<nav class="nav"><div class="wrap"><a class="brand" href="#top">{brand_img}<span>{esc(name)}</span></a>'
        f'<button class="menu" type="button" aria-label="Menu">☰</button><div class="links">{"".join(nav_links)}{follow_btn}</div></div></nav>'
    ]

    if on.get("hero", True):
        pill = f'<span class="pill"><i></i>Next up · {esc(nxt["name"])} · {esc(_when(nxt["starts_at"]))}</span>' if nxt else ""
        see = '<a class="btn btn-primary" href="#events">See upcoming events</a>' if on.get("upcoming", True) else ""
        join = f'<a class="btn btn-ghost" href="{esc(follow_url)}">Join the club</a>' if follow_url else ""
        cta = f'<div class="cta">{see}{join}</div>'
        text = f'{pill}<h1>{esc(headline)}</h1><p class="sub">{esc(subline)}</p>'
        if template == "campus":
            img = f'<img class="cover" src="{esc(cover)}" alt="">' if cover else ""
            parts.append(f'<header class="hero" id="top">{img}<div class="shade"></div><div class="wrap">{text}{cta}</div></header>')
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
        more = f'<a class="more" href="{esc(data["events_url"])}">View all →</a>' if data.get("events_url") else ""
        parts.append(f'<section id="events"><div class="wrap"><div class="row"><h2>Upcoming events</h2>{more}</div><div class="grid">{cards}</div></div></section>')

    if on.get("about", True):
        nums = data["numbers"]
        home = f'<div class="num"><b>{esc(club["location_name"])}</b><span>home</span></div>' if club.get("location_name") else ""
        parts.append(
            f'<section id="about" class="about"><div class="wrap"><h2>About us</h2><p>{esc(club.get("about") or subline)}</p>'
            f'<div class="nums"><div class="num"><b>{nums["followers"]:,}</b><span>followers</span></div><div class="num"><b>{nums["events"]:,}</b><span>events held</span></div>{home}</div></div></section>'
        )

    if on.get("past", True) and past:
        pics = "".join(f'<a href="{esc(e["url"])}" title="{esc(e["name"])}"><img src="{esc(e["poster"])}" alt="{esc(e["name"])}" loading="lazy"></a>' for e in past if e.get("poster")) if template == "campus" else ""
        body = f'<div class="gallery">{pics}</div>' if pics else '<div class="grid">' + "".join(_event_card(e, template, e["url"]) for e in past) + "</div>"
        parts.append(f'<section id="past"><div class="wrap"><h2>Past events</h2>{body}</div></section>')

    if on.get("team", True) and data["team"]:
        people = []
        for m in data["team"]:
            so = "".join(f'<a href="{esc(u)}" target="_blank" rel="noreferrer">{esc(k)}</a>' for k, u in m["links"])
            so_html = f'<div class="so">{so}</div>' if so else ""
            people.append(f'<div class="person">{_img(m.get("photo"), m["name"])}<b>{esc(m["name"])}</b><span>{esc(m.get("role") or "")}</span>{so_html}</div>')
        parts.append(f'<section id="team"><div class="wrap"><h2>The team</h2><div class="people">{"".join(people)}</div></div></section>')

    if on.get("partners", True) and data["partners"]:
        logos = []
        for p in data["partners"]:
            role = f'<span>{esc(p["role"])}</span>' if p.get("role") else ""
            logos.append(f'<div class="logo">{_img(p.get("logo"), p["name"])}<div><div>{esc(p["name"])}</div>{role}</div></div>')
        parts.append(f'<section id="partners"><div class="wrap"><h2>Partners</h2><div class="logos">{"".join(logos)}</div></div></section>')

    if on.get("follow", True) and follow_url:
        parts.append(f'<section><div class="wrap"><div class="band"><div><h2>Follow {esc(name)}</h2><p>Hear first about every event, in the app.</p></div><a class="btn" href="{esc(follow_url)}">Follow in the app</a></div></div></section>')

    if on.get("contact", True):
        links = "".join(f'<a href="{esc(u)}" target="_blank" rel="noreferrer">{esc(k)}</a>' for k, u in data["socials"])
        contact = " · ".join(esc(x) for x in (club.get("contact_email"), club.get("location_name")) if x)
        contact_html = f"<span>{contact}</span>" if contact else ""
        parts.append(f'<footer id="contact"><div class="wrap"><span>© <span id="y"></span> {esc(name)}</span>{contact_html}{links}<span style="margin-left:auto">Made with MyOtherApp</span></div></footer>')

    title = f"{name} · {headline}"
    desc = subline or f"{name} on MyOtherApp."
    og = f'<meta property="og:image" content="{esc(cover or logo)}">' if (cover or logo) else ""
    icon = f'<link rel="icon" href="{esc(logo)}">' if logo else ""
    ld = json.dumps({"@context": "https://schema.org", "@type": "Organization", "name": name, "url": site_url, "description": desc})
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title><meta name="description" content="{esc(desc)}"><meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}">{og}'
        f'<link rel="canonical" href="{esc(site_url)}">{icon}'
        f'<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family={font_query}&display=swap" rel="stylesheet">'
        f"<style>{css}</style></head><body>{''.join(parts)}<script>{_JS}</script>"
        f'<script type="application/ld+json">{ld}</script></body></html>'
    )
