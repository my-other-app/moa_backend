"""Render inputs other than the designer's sample, shared by the parity
harness (screenshots) and tests/test_site_templates.py (checks):

* `with_images(sample, urls)` -- the sample with every placeholder swapped for
  a real picture (url, srcset, alt, w, h), the club's logo set, counts as
  plain numbers: what a full club's page looks like.
* `sparse(name)` -- a club with nothing but a name: no events, no photos, no
  team, no sponsors, no stats. Every section must vanish, not leave a box.
* `long_names()` -- one of everything with very long texts.
"""
from __future__ import annotations

import copy
from itertools import cycle

EMPTY_IMAGES = {"hero.cover": None, "hero.image": None, "about.image": None, "photos": [], "join.image": None, "event.fallback": None}


def _strip_brackets(v):
    if isinstance(v, str) and v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].replace(",", "")
        if inner.isdigit():
            return int(inner)
    return v


def with_images(sample: dict, urls: list[str], *, srcset: bool = True) -> dict:
    """`urls`: picture addresses to hand out in turn (https for the tests,
    file:// for screenshots)."""
    data = copy.deepcopy(sample)
    data["sample"] = False
    pool = cycle(urls)

    def swap(node, alt=""):
        if isinstance(node, dict):
            if "placeholder" in node and "url" not in node:
                u = next(pool)
                return {"url": u, "srcset": f"{u} 960w, {u} 1440w" if srcset else None, "alt": node.get("alt") or node.get("placeholder") or alt, "w": 1440, "h": 960}
            return {k: swap(v, alt=node.get("name") or alt) for k, v in node.items()}
        if isinstance(node, list):
            return [swap(v, alt) for v in node]
        return node

    data = swap(data)
    data["club"]["logo_url"] = next(pool)
    data["club"]["logo_srcset"] = None
    data["stats"] = {k: _strip_brackets(v) for k, v in (data.get("stats") or {}).items()}
    for e in data.get("past", []):
        e.pop("facts", None)  # the line comes from `going` / `speakers_count`, as in B's data
        e["speakers_count"] = 3
    return data


def _site(template: str) -> dict:
    return {"template": template, "title_overrides": {}, "accent": None, "headline": None, "subline": None}


# The render input as B's site_data() builds it (contract, Changes, B 7).


def sparse(template: str, name: str = "Chess") -> dict:
    return {
        "site": _site(template),
        "page": {"title": name, "description": None, "url": "https://x.myotherapp.in"},
        "headline": name, "subline": None, "year": 2026,
        "club": {
            "name": name, "initials": name[:1].upper(), "tagline": None, "about": None, "about_short": None, "location_name": None,
            "contact_email": None, "slug": name.lower(), "logo": None, "logo_url": None, "logo_srcset": None, "cover": None,
            "founded_year": None, "topics": [], "brand_colour": None,
        },
        "images": copy.deepcopy(EMPTY_IMAGES),
        "stats": {"followers": 0, "events_held": 0, "upcoming": 0, "members": None},
        "upcoming": [], "past": [], "speakers": [], "team": [], "sponsors": [], "socials": [],
        "links": {"follow_url": None, "events_url": None, "join_url": None, "contact_mailto": None, "site_url": "https://x.myotherapp.in"},
    }


LONG = "The Extraordinarily Long-Named Society of Weekend Mechanical Keyboard Builders, Tinkerers and Friends"


def long_names(template: str) -> dict:
    data = sparse(template, LONG)
    data["club"].update(
        tagline="A tagline that goes on for quite a while to see whether it wraps without pushing anything off the side of a phone",
        about="Supercalifragilisticexpialidocious-and-then-some-unbreakable-words-to-test-wrapping " * 3 + "\n\nA second paragraph.",
        location_name="Thiruvananthapuram, Kerala, India", contact_email="a.very.long.address.for.contact@extraordinarily-long-domain-name.example.com",
        founded_year=2019, topics=["Mechanical keyboards and the people who love them", "Soldering"],
    )
    ev = {
        "id": 1, "name": "An event with a name long enough to wrap onto three or four lines on a phone screen, at least", "starts_at": "2027-01-16T10:00:00+05:30",
        "day": "16", "month": "Jan", "year": "2027", "weekday": "Sat", "time": "10:00 AM", "end_time": "6:00 PM",
        "where": "A venue with a long name, Some Street, Thiruvananthapuram", "is_online": False, "kind": "Workshop",
        "blurb": "A long summary " * 6, "seats_left": 12, "going": 1488, "fee": "₹1,500", "poster": None, "poster_is_fallback": False,
        "url": "https://myotherapp.in/events/x", "speakers_count": 14,
    }
    data["upcoming"] = [ev, dict(ev, id=2), dict(ev, id=3)]
    data["past"] = [dict(ev, id=4)]
    data["speakers"] = [{"name": "Speaker With A Rather Long Double-Barrelled Name", "title": "Principal engineer and head of everything", "org": "A Company With A Long Name Pvt Ltd", "photo": None, "url": None}]
    data["team"] = [{"name": "Team Member With A Long Name", "role": "Head of partnerships, outreach and community", "initials": "TM", "photo": None, "links": []}]
    data["sponsors"] = [{"name": "Sponsor With A Long Name Private Limited", "role": "Title sponsor", "initials": "SW", "logo": None}, {"name": "Partner With A Long Name", "role": "Venue partner", "initials": "PW", "logo": None}]
    data["socials"] = [{"kind": "linkedin", "label": "LinkedIn", "href": "https://linkedin.com/company/x"}, {"kind": "instagram", "label": "Instagram", "href": "https://instagram.com/x"},
                       {"kind": "website", "label": "Website", "href": "https://www.extraordinarily-long-domain-name.example.com/about"}]
    data["stats"] = {"followers": 1234567, "events_held": 48, "upcoming": 3, "members": None}
    data["links"].update(follow_url="https://myotherapp.in/c/x", events_url="https://myotherapp.in/c/x/events", join_url="https://myotherapp.in/c/x/join", contact_mailto="mailto:a@b.example")
    return data
