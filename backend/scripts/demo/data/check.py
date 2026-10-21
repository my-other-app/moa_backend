"""Check the demo content in this folder against the seeder's contract.

    python backend/scripts/demo/data/check.py

Standard library only. Reads people.json and every clubs/*.json next to this
file, prints each problem it finds and exits 1 if there were any (0, with a
one-line summary, when the content is clean). If `jsonschema` happens to be
installed, schema.json is checked as well; it is never required.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

TREE = {
    "development": [
        "web-development", "app-development", "software-engineering", "coding",
        "open-source", "cloud-devops", "cybersecurity", "data-science",
    ],
    "deep-tech": [
        "artificial-intelligence", "robotics", "iot-hardware", "blockchain",
        "vr-ar", "quantum-computing", "space-exploration",
    ],
    "design": ["ui-ux", "graphic-design", "product-design", "animation"],
    "arts": [
        "drawing", "music", "singing", "dance", "theatre", "photography",
        "film", "writing", "fashion",
    ],
    "business": [
        "entrepreneurship", "product-management", "marketing", "finance", "analytics",
    ],
    "gaming-media": ["gaming", "e-sports", "content-creation", "memes-internet-culture"],
    "community": [
        "volunteering", "sustainability", "public-speaking", "debate-mun", "quizzing",
    ],
    "sports-fitness": ["sports", "fitness", "outdoors-adventure"],
}
TOPICS = set(TREE)
TOPIC_OF = {i: t for t, interests in TREE.items() for i in interests}
INTERESTS = set(TOPIC_OF)

FONTS = {"bebas", "dm-serif", "inter"}
MOTIFS = {"waves", "grid", "circles", "stripes", "blobs", "triangles", "dots"}
KINDS = {"tee", "hoodie", "cap", "tote", "sticker", "mug", "lanyard", "notebook"}
SIZES = {"XS", "S", "M", "L", "XL", "XXL"}
FORMATS = {"in_person", "online"}
CATEGORIES = {
    "Workshop", "Meetup", "Hackathon", "Talk", "Competition", "Festival",
    "Exhibition", "Tournament",
}
QUESTION_TYPES = {"text", "select", "radio", "checkbox", "number", "url", "date"}
CHOICE_TYPES = {"select", "radio", "checkbox"}
THREAD_KINDS = {"post", "poll", "announcement"}

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KEY = re.compile(r"^[a-z][a-z0-9_]*$")
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PHONE = re.compile(r"^\+91 90000 0\d{4}$")
HANDLE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
USERNAME = re.compile(r"^[a-z0-9._]{3,30}$")
EMAIL_ANY = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_ANY = re.compile(r"\+91[\d \-]{6,}")
URL_ANY = re.compile(r"https?://|www\.", re.I)

PAST_DAYS = (-120, -1)
UPCOMING_DAYS = (1, 60)
THREAD_DAYS = (1, 45)
POOL_SIZE = 150


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def add(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def text(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def strings(node, path="$"):
    """Every string in a JSON value, with where it sits."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from strings(v, f"{path}[{i}]")


def need(r: Report, where: str, obj: dict, keys: list[str]) -> bool:
    if not isinstance(obj, dict):
        r.add(where, "expected an object")
        return False
    missing = [k for k in keys if k not in obj]
    for k in missing:
        r.add(where, f"missing `{k}`")
    return not missing


def paragraphs(s: str) -> int:
    return len([p for p in s.split("\n\n") if p.strip()])


def check_people(r: Report, people: dict) -> set[str]:
    w = "people.json"
    if not need(r, w, people, ["main_user", "attendee_names", "colleges", "comment_snippets"]):
        return set()
    colleges = people["colleges"]
    if not (isinstance(colleges, list) and 8 <= len(colleges) <= 12):
        r.add(w, "colleges: want 8-12 names")
        colleges = colleges if isinstance(colleges, list) else []
    if len(set(colleges)) != len(colleges):
        r.add(w, "colleges: duplicates")
    names = people["attendee_names"]
    if not isinstance(names, list) or len(names) != POOL_SIZE:
        r.add(w, f"attendee_names: want exactly {POOL_SIZE}, have {len(names) if isinstance(names, list) else '?'}")
    else:
        dupes = [n for n, c in Counter(names).items() if c > 1]
        if dupes:
            r.add(w, f"attendee_names: duplicates {dupes}")
        for n in names:
            if not (text(n) and len(n.split()) >= 2):
                r.add(w, f"attendee_names: {n!r} wants a first and last name")
    snippets = people["comment_snippets"]
    if not isinstance(snippets, list) or len(snippets) != 60:
        r.add(w, "comment_snippets: want exactly 60")
    elif len(set(snippets)) != len(snippets):
        r.add(w, "comment_snippets: duplicates")
    mu = people["main_user"]
    if need(r, f"{w} main_user", mu, ["name", "username", "college", "interests", "bio"]):
        if not USERNAME.match(str(mu["username"])):
            r.add(w, f"main_user.username {mu['username']!r} is not a plain handle")
        if mu["college"] not in colleges:
            r.add(w, "main_user.college is not one of colleges")
        ints = mu["interests"]
        if not (isinstance(ints, list) and len(ints) == 5 and len(set(ints)) == 5):
            r.add(w, "main_user.interests: want 5 distinct slugs")
        for s in ints or []:
            if s not in INTERESTS:
                r.add(w, f"main_user.interests: unknown slug {s!r}")
        if not text(mu["bio"]):
            r.add(w, "main_user.bio is empty")
    return set(colleges)


def check_merch(r: Report, w: str, merch) -> set[str]:
    keys: set[str] = set()
    if not isinstance(merch, list) or len(merch) > 4:
        r.add(w, "merch: want a list of at most 4 items")
        return keys
    for i, m in enumerate(merch):
        mw = f"{w} merch[{i}]"
        if not need(r, mw, m, ["key", "name", "description", "price", "kind", "color", "sizes", "stock"]):
            continue
        if not KEY.match(str(m["key"])):
            r.add(mw, f"key {m['key']!r} is not a plain key")
        if m["key"] in keys:
            r.add(mw, f"key {m['key']!r} repeats")
        keys.add(m["key"])
        if m["kind"] not in KINDS:
            r.add(mw, f"kind {m['kind']!r} is not one of {sorted(KINDS)}")
        if not (is_int(m["price"]) and 0 < m["price"] <= 5000):
            r.add(mw, "price wants whole rupees between 1 and 5000")
        if not HEX.match(str(m["color"])):
            r.add(mw, "color is not #rrggbb")
        sizes, stock = m["sizes"], m["stock"]
        if sizes is not None and stock is not None:
            r.add(mw, "sizes and stock are mutually exclusive")
        if sizes is None and stock is None:
            r.add(mw, "give either sizes or stock")
        if sizes is not None:
            if not isinstance(sizes, dict) or not sizes:
                r.add(mw, "sizes wants an object of size -> count")
            else:
                for s, n in sizes.items():
                    if s not in SIZES or not (is_int(n) and n >= 0):
                        r.add(mw, f"sizes: bad entry {s}={n}")
            if m["kind"] not in {"tee", "hoodie"}:
                r.add(mw, f"only tees and hoodies come in sizes, not a {m['kind']}")
        if stock is not None and not (is_int(stock) and stock >= 0):
            r.add(mw, "stock wants a whole number")
    return keys


def check_event(r: Report, w: str, e: dict, merch_keys: set[str], seen_events: dict, club_slug: str) -> str | None:
    """Checks one event; returns 'past' or 'upcoming' (None if undated)."""
    keys = [
        "slug", "name", "when", "format", "venue", "category", "interests", "fee",
        "sold_out", "capacity", "registrations", "about", "guidelines", "speakers",
        "agenda", "questions", "merch", "swag", "poster",
    ]
    if not need(r, w, e, keys):
        return None
    slug = e["slug"]
    if not SLUG.match(str(slug)):
        r.add(w, f"slug {slug!r} is not kebab-case")
    if slug in seen_events:
        r.add(w, f"event slug {slug!r} also used by {seen_events[slug]}")
    seen_events[slug] = club_slug
    w = f"{w} ({slug})"

    when = e["when"]
    phase = None
    if need(r, f"{w} when", when, ["days_from_now", "time", "duration_hours"]):
        d = when["days_from_now"]
        if not is_int(d) or d == 0:
            r.add(w, "when.days_from_now wants a non-zero whole number")
        else:
            phase = "past" if d < 0 else "upcoming"
            lo, hi = PAST_DAYS if d < 0 else UPCOMING_DAYS
            if not lo <= d <= hi:
                r.add(w, f"when.days_from_now {d} is outside {lo}..{hi}")
        if not HHMM.match(str(when["time"])):
            r.add(w, "when.time is not HH:MM")
        if not (is_num(when["duration_hours"]) and 0 < when["duration_hours"] <= 72):
            r.add(w, "when.duration_hours wants hours between 0 and 72")

    if e["format"] not in FORMATS:
        r.add(w, f"format {e['format']!r} is not one of {sorted(FORMATS)}")
    venue = e["venue"]
    if e["format"] == "in_person":
        if not (isinstance(venue, dict) and text(venue.get("name")) and text(venue.get("address"))):
            r.add(w, "an in-person event wants a venue with a name and an address")
        elif not (is_num(venue.get("lat")) and 8.0 <= venue["lat"] <= 12.9 and is_num(venue.get("lng")) and 74.8 <= venue["lng"] <= 77.5):
            r.add(w, "an in-person venue wants its own lat and lng, in Kerala")
    elif venue is not None:
        r.add(w, "an online event has no venue (null)")
    if e["category"] not in CATEGORIES:
        r.add(w, f"category {e['category']!r} is not one of {sorted(CATEGORIES)}")
    ints = e["interests"]
    if not (isinstance(ints, list) and ints):
        r.add(w, "interests wants at least one slug")
    for s in ints or []:
        if s not in INTERESTS:
            r.add(w, f"interests: unknown slug {s!r}")

    fee, cap, regs, sold = e["fee"], e["capacity"], e["registrations"], e["sold_out"]
    if not (is_int(fee) and fee >= 0):
        r.add(w, "fee wants whole rupees, 0 for free")
    if not isinstance(sold, bool):
        r.add(w, "sold_out wants true or false")
    if not (is_int(cap) and cap > 0):
        r.add(w, "capacity wants a positive whole number")
    if not (is_int(regs) and 20 <= regs <= POOL_SIZE):
        r.add(w, f"registrations wants 20..{POOL_SIZE} (the people pool)")
    if is_int(cap) and is_int(regs) and regs > cap:
        r.add(w, f"registrations {regs} exceed capacity {cap}")
    if sold is True and regs != cap:
        r.add(w, "a sold-out event has registrations equal to capacity")
    if phase == "upcoming" and is_int(fee) and fee > 0:
        if sold is not True or regs != cap:
            r.add(w, "an UPCOMING paid event must be sold out (sold_out true, registrations == capacity)")

    after_only = ["attended_ratio", "certificates", "feedback", "live"]
    if phase == "upcoming":
        for k in after_only:
            if e.get(k) is not None:
                r.add(w, f"{k} is for past events only; leave it null on an upcoming one")
    elif phase == "past":
        ar = e.get("attended_ratio")
        if not (is_num(ar) and 0.3 <= ar <= 1):
            r.add(w, "a past event wants attended_ratio between 0.3 and 1")
        if not isinstance(e.get("certificates"), bool):
            r.add(w, "a past event wants certificates true or false")
        fb = e.get("feedback")
        if fb is not None and need(r, f"{w} feedback", fb, ["responses", "avg_rating", "comments"]):
            attended = int(regs * ar) if is_int(regs) and is_num(ar) else None
            if not (is_int(fb["responses"]) and fb["responses"] > 0):
                r.add(w, "feedback.responses wants a positive whole number")
            elif attended is not None and fb["responses"] > attended:
                r.add(w, f"feedback.responses {fb['responses']} exceed attendees ({attended})")
            if not (is_num(fb["avg_rating"]) and 1 <= fb["avg_rating"] <= 5):
                r.add(w, "feedback.avg_rating wants 1..5")
            c = fb["comments"]
            if not (isinstance(c, list) and 4 <= len(c) <= 6 and all(text(x) for x in c)):
                r.add(w, "feedback.comments wants 4-6 comments")
        live = e.get("live")
        if live is not None and need(r, f"{w} live", live, ["poll", "qna"]):
            poll = live["poll"]
            if poll is not None:
                if not (isinstance(poll, dict) and text(poll.get("question"))):
                    r.add(w, "live.poll wants a question")
                elif not (isinstance(poll.get("options"), list) and 2 <= len(poll["options"]) <= 4):
                    r.add(w, "live.poll wants 2-4 options")
            if not (isinstance(live["qna"], list) and all(text(q) for q in live["qna"])):
                r.add(w, "live.qna wants a list of questions")

    if not (text(e["about"]) and 2 <= paragraphs(e["about"]) <= 3):
        r.add(w, "about wants 2-3 paragraphs")
    if not text(e["guidelines"]):
        r.add(w, "guidelines is empty")
    sp = e["speakers"]
    if not (isinstance(sp, list) and sp):
        r.add(w, "speakers wants at least one person")
    for i, s in enumerate(sp or []):
        if need(r, f"{w} speakers[{i}]", s, ["name", "designation", "bio"]):
            if not all(text(s[k]) for k in ("name", "designation", "bio")):
                r.add(w, f"speakers[{i}] has an empty field")
    ag = e["agenda"]
    if not (isinstance(ag, list) and len(ag) >= 2):
        r.add(w, "agenda wants at least two items")
    last = None
    for i, a in enumerate(ag or []):
        if need(r, f"{w} agenda[{i}]", a, ["at", "title", "by"]):
            if not HHMM.match(str(a["at"])):
                r.add(w, f"agenda[{i}].at is not HH:MM")
            elif last is not None and a["at"] < last:
                r.add(w, f"agenda[{i}].at {a['at']} comes before the item above it (keep a multi-day agenda on day one)")
            else:
                last = a["at"]
            if a["by"] is not None and not text(a["by"]):
                r.add(w, f"agenda[{i}].by is empty")
    if ag and isinstance(when, dict) and HHMM.match(str(when.get("time"))) and ag[0].get("at", "") < when["time"]:
        r.add(w, "the agenda starts before the event does")
    qs = e["questions"]
    if not (isinstance(qs, list) and 1 <= len(qs) <= 4):
        r.add(w, "questions wants 1-4 questions")
    qkeys = set()
    for i, q in enumerate(qs or []):
        qw = f"{w} questions[{i}]"
        if not need(r, qw, q, ["key", "label", "type", "required"]):
            continue
        if not KEY.match(str(q["key"])) or q["key"] in qkeys:
            r.add(qw, f"key {q['key']!r} is not a plain, unique key")
        qkeys.add(q["key"])
        if q["type"] not in QUESTION_TYPES:
            r.add(qw, f"type {q['type']!r} is not one of {sorted(QUESTION_TYPES)}")
        opts = q.get("options")
        if q["type"] in CHOICE_TYPES:
            if not (isinstance(opts, list) and len(opts) >= 2 and all(text(o) for o in opts)):
                r.add(qw, "a choice question wants at least two options")
        elif opts is not None:
            r.add(qw, f"options only belong on select/radio/checkbox, not {q['type']}")
        if not isinstance(q["required"], bool):
            r.add(qw, "required wants true or false")
    if merch_keys and not e["merch"]:
        r.add(w, "merch wants at least one of the club's merch keys")
    for k in e["merch"] or []:
        if k not in merch_keys:
            r.add(w, f"merch key {k!r} is not in the club's merch")
    if not e["swag"]:
        r.add(w, "swag wants at least one item")
    for i, s in enumerate(e["swag"] or []):
        if not (isinstance(s, dict) and text(s.get("name")) and "sponsor" in s):
            r.add(w, f"swag[{i}] wants a name and a sponsor (or null)")
    if not (isinstance(e["poster"], dict) and text(e["poster"].get("headline")) and text(e["poster"].get("sub"))):
        r.add(w, "poster wants a headline and a sub-line")
    return phase


def check_club(r: Report, path: Path, club: dict, colleges: set[str], seen_events: dict) -> dict:
    w = f"clubs/{path.name}"
    keys = [
        "slug", "name", "tagline", "about", "org", "location", "contact", "socials",
        "topics", "interests", "brand", "team", "partners", "followers", "merch",
        "events", "threads",
    ]
    stats = {"events": 0, "past": 0, "upcoming": 0, "threads": 0, "merch": 0}
    if not need(r, w, club, keys):
        return stats
    slug = club["slug"]
    if not SLUG.match(str(slug)):
        r.add(w, f"slug {slug!r} is not kebab-case")
    if path.stem != slug:
        r.add(w, f"file name should be {slug}.json")
    if not text(club["name"]):
        r.add(w, "name is empty")
    if not (text(club["tagline"]) and len(club["tagline"]) <= 80):
        r.add(w, f"tagline wants 1-80 characters (has {len(club['tagline'] or '')})")
    if not (text(club["about"]) and 2 <= paragraphs(club["about"]) <= 4):
        r.add(w, "about wants 2-4 paragraphs")
    org = club["org"]
    if org is not None and need(r, f"{w} org", org, ["name", "city"]):
        if org["name"] not in colleges:
            r.add(w, f"org {org['name']!r} is not in people.json colleges")
    loc = club["location"]
    if need(r, f"{w} location", loc, ["name", "lat", "lng"]):
        if not (is_num(loc["lat"]) and 8.0 <= loc["lat"] <= 12.9 and is_num(loc["lng"]) and 74.8 <= loc["lng"] <= 77.5):
            r.add(w, "location is not in Kerala")
    contact = club["contact"]
    if need(r, f"{w} contact", contact, ["email", "phone"]):
        if contact["email"] != f"hello@{slug}.demo.myotherapp.in":
            r.add(w, f"contact.email must be hello@{slug}.demo.myotherapp.in")
        if not PHONE.match(str(contact["phone"])):
            r.add(w, "contact.phone must look like +91 90000 0xxxx")
    socials = club["socials"]
    if need(r, f"{w} socials", socials, ["instagram", "linkedin", "x"]):
        for k, v in socials.items():
            if v is not None and (not HANDLE.match(str(v)) or URL_ANY.search(str(v)) or ".com" in str(v)):
                r.add(w, f"socials.{k} {v!r} must be a bare handle, never a URL")
            if k == "x" and v is not None and len(v) > 15:
                r.add(w, "socials.x handles are at most 15 characters")
    topics, ints = club["topics"], club["interests"]
    for t in topics or []:
        if t not in TOPICS:
            r.add(w, f"topics: unknown slug {t!r}")
    if not topics:
        r.add(w, "topics wants at least one slug")
    for s in ints or []:
        if s not in INTERESTS:
            r.add(w, f"interests: unknown slug {s!r}")
        elif TOPIC_OF[s] not in (topics or []):
            r.add(w, f"interest {s!r} sits under topic {TOPIC_OF[s]!r}, which the club does not list")
    if not ints:
        r.add(w, "interests wants at least one slug")
    brand = club["brand"]
    if need(r, f"{w} brand", brand, ["primary", "accent", "ink", "paper", "display_font", "motif"]):
        for k in ("primary", "accent", "ink", "paper"):
            if not HEX.match(str(brand[k])):
                r.add(w, f"brand.{k} is not #rrggbb")
        if brand["display_font"] not in FONTS:
            r.add(w, f"brand.display_font is not one of {sorted(FONTS)}")
        if brand["motif"] not in MOTIFS:
            r.add(w, f"brand.motif is not one of {sorted(MOTIFS)}")
    team = club["team"]
    if not (isinstance(team, list) and 4 <= len(team) <= 8):
        r.add(w, "team wants 4-8 people")
    for i, p in enumerate(team or []):
        if need(r, f"{w} team[{i}]", p, ["name", "role", "bio"]) and not all(text(p[k]) for k in ("name", "role", "bio")):
            r.add(w, f"team[{i}] has an empty field")
    partners = club["partners"]
    if not (isinstance(partners, list) and 2 <= len(partners) <= 4):
        r.add(w, "partners wants 2-4 partners")
    for i, p in enumerate(partners or []):
        if need(r, f"{w} partners[{i}]", p, ["name", "role", "since"]):
            if not (is_int(p["since"]) and 2015 <= p["since"] <= 2026):
                r.add(w, f"partners[{i}].since wants a year 2015-2026")
    if not (is_int(club["followers"]) and club["followers"] > 0):
        r.add(w, "followers wants a positive whole number")

    merch_keys = check_merch(r, w, club["merch"])
    stats["merch"] = len(club["merch"] or [])

    events = club["events"] or []
    for i, e in enumerate(events):
        phase = check_event(r, f"{w} events[{i}]", e, merch_keys, seen_events, slug)
        if phase:
            stats[phase] += 1
    stats["events"] = len(events)
    if not 2 <= stats["past"] <= 3:
        r.add(w, f"want 2-3 past events, have {stats['past']}")
    if not 2 <= stats["upcoming"] <= 3:
        r.add(w, f"want 2-3 upcoming events, have {stats['upcoming']}")

    threads = club["threads"] or []
    stats["threads"] = len(threads)
    if not 5 <= len(threads) <= 8:
        r.add(w, "threads wants 5-8 threads")
    for i, t in enumerate(threads):
        tw = f"{w} threads[{i}]"
        if not need(r, tw, t, ["days_ago", "kind", "text", "likes", "comments"]):
            continue
        lo, hi = THREAD_DAYS
        if not (is_int(t["days_ago"]) and lo <= t["days_ago"] <= hi):
            r.add(tw, f"days_ago wants {lo}..{hi}")
        if t["kind"] not in THREAD_KINDS:
            r.add(tw, f"kind {t['kind']!r} is not one of {sorted(THREAD_KINDS)}")
        opts = t.get("options")
        if t["kind"] == "poll":
            if not (isinstance(opts, list) and 2 <= len(opts) <= 4 and all(text(o) for o in opts)):
                r.add(tw, "a poll wants 2-4 options")
        elif opts is not None:
            r.add(tw, "only a poll has options")
        if not text(t["text"]):
            r.add(tw, "text is empty")
        if not (is_int(t["likes"]) and t["likes"] >= 0):
            r.add(tw, "likes wants a whole number")
        c = t["comments"]
        if not (isinstance(c, list) and 1 <= len(c) <= 4 and all(isinstance(x, dict) and text(x.get("text")) for x in c)):
            r.add(tw, "comments wants 1-4 {text} objects")
    return stats


def check_text(r: Report, where: str, doc, slugs: set[str]) -> None:
    """Rules that hold for every string in a file."""
    for path, s in strings(doc):
        if "lorem" in s.lower():
            r.add(where, f"{path} has lorem ipsum")
        if URL_ANY.search(s):
            r.add(where, f"{path} has a URL")
        for m in EMAIL_ANY.findall(s):
            ok = re.fullmatch(r"hello@([a-z0-9-]+)\.demo\.myotherapp\.in", m)
            if not ok or ok.group(1) not in slugs:
                r.add(where, f"{path} has an email off the demo domain: {m}")
        for m in PHONE_ANY.findall(s):
            if not PHONE.match(m.strip()):
                r.add(where, f"{path} has a phone that is not +91 90000 0xxxx: {m.strip()}")


def sentences(s: str):
    for part in re.split(r"(?<=[.!?])\s+|\n+", s):
        part = part.strip()
        if len(part.split()) >= 8:
            yield re.sub(r"\W+", " ", part.lower()).strip()


def check_repeats(r: Report, docs: dict[str, object]) -> None:
    """A sentence of eight words or more is written once across the clubs."""
    seen: dict[str, set[str]] = defaultdict(set)
    for name, doc in docs.items():
        for _, s in strings(doc):
            for sent in sentences(s):
                seen[sent].add(name)
    for sent, files in seen.items():
        if len(files) > 1:
            r.add("clubs", f"sentence repeated in {sorted(files)}: {sent[:70]}...")


def check_schema(r: Report, docs: dict[str, object], people) -> None:
    try:
        import jsonschema  # optional
    except ImportError:
        return
    schema = json.loads((HERE / "schema.json").read_text())

    def run(name: str, ref: str, doc) -> None:
        sub = {"$ref": f"#/$defs/{ref}", "$defs": schema["$defs"]}
        for err in jsonschema.Draft202012Validator(sub).iter_errors(doc):
            msg = err.message if len(err.message) <= 100 else err.message[:97] + "..."
            r.add(name, f"schema at {err.json_path}: {msg}")

    for name, doc in docs.items():
        run(name, "club", doc)
    run("people.json", "people", people)


def main() -> int:
    r = Report()
    try:
        people = json.loads((HERE / "people.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"people.json: {exc}")
        return 1
    colleges = check_people(r, people)

    # A name starting with "_" is the seed's own test club (_example.json), not content.
    paths = sorted(p for p in (HERE / "clubs").glob("*.json") if not p.name.startswith("_"))
    if len(paths) != 8:
        r.add("clubs", f"want 8 club files, have {len(paths)}")
    docs: dict[str, object] = {}
    for p in paths:
        try:
            docs[f"clubs/{p.name}"] = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            r.add(f"clubs/{p.name}", f"not JSON: {exc}")
    slugs = Counter(d.get("slug") for d in docs.values() if isinstance(d, dict))
    for s, n in slugs.items():
        if n > 1:
            r.add("clubs", f"club slug {s!r} used {n} times")

    seen_events: dict[str, str] = {}
    totals = []
    for name, doc in docs.items():
        stats = check_club(r, Path(name), doc, colleges, seen_events)
        totals.append((doc.get("slug"), stats))
        check_text(r, name, doc, set(slugs))
    check_text(r, "people.json", people, set(slugs))
    check_repeats(r, docs)
    check_schema(r, docs, people)

    for p in r.problems:
        print(p)
    if r.problems:
        print(f"\n{len(r.problems)} problem(s)")
        return 1
    for slug, s in totals:
        print(f"{slug:28} events {s['events']} ({s['past']} past, {s['upcoming']} upcoming)  threads {s['threads']}  merch {s['merch']}")
    print(f"ok: {len(docs)} clubs, {len(seen_events)} events, people.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
