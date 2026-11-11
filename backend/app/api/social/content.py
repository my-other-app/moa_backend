"""What a post says: the event's facts, the fill-in words, a draft per
platform, hashtag ideas, and the checks a preview shows.

Fill-in words (`{event_name}` `{date}` `{time}` `{venue}` `{fee}`
`{seats_left}` `{speakers}` `{link}`) stay in the saved text and are filled
at the moment of posting, so a reminder says the seats left then. Dates and
times are India time.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select

from app.api.social import platforms as P

IST = timezone(timedelta(hours=5, minutes=30))
FILL_INS = ("event_name", "date", "time", "venue", "fee", "seats_left", "speakers", "link")
_WORD = re.compile(r"\{([a-z_]+)\}")
_HASHTAG = re.compile(r"(?<![\w&])#(\w+)")
_URL = re.compile(r"https?://\S+")


@dataclass
class Facts:
    event_name: str
    date: str
    time: str
    venue: str
    fee: str
    seats_left: str
    speakers: str
    about: str = ""
    club_name: str = ""
    hashtags: list[str] = field(default_factory=list)

    def words(self, link: str = "") -> dict[str, str]:
        return {
            "event_name": self.event_name, "date": self.date, "time": self.time, "venue": self.venue, "fee": self.fee,
            "seats_left": self.seats_left, "speakers": self.speakers, "link": link,
        }


def _local(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(IST)


def date_text(dt: Optional[datetime]) -> str:
    when = _local(dt)
    return f"{when:%a}, {when.day} {when:%b} {when.year}" if when else ""


def time_text(dt: Optional[datetime]) -> str:
    when = _local(dt)
    return f"{when:%I:%M %p}".lstrip("0") if when else ""


def fee_text(event) -> str:
    if not event.has_fee or not event.reg_fee:
        return "Free"
    amount = Decimal(str(event.reg_fee))
    return f"₹{int(amount)}" if amount == amount.to_integral() else f"₹{amount:.2f}"


def venue_text(event) -> str:
    if event.is_online:
        return "Online"
    return (event.location_name or "").strip() or "Venue to be announced"


def _names(names: list[str]) -> str:
    names = [n.strip() for n in names if n and n.strip()]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def tag_of(words: Optional[str], *, longest: int = 28) -> Optional[str]:
    """A hashtag from words: letters and digits only, each word capitalised
    ("GDG Trivandrum" -> "GDGTrivandrum")."""
    if not words:
        return None
    plain = unicodedata.normalize("NFKD", words)
    parts = re.findall(r"[A-Za-z0-9]+", plain)
    tag = "".join(p if p.isupper() else p[:1].upper() + p[1:] for p in parts)
    return tag if 2 < len(tag) <= longest and not tag.isdigit() else None


def excerpt(text: Optional[str], limit: int) -> str:
    """The start of the event's description, cut at a sentence or a word."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop > limit * 0.5:
        return cut[: stop + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"


async def facts(session, event, club=None) -> Facts:
    """The event's facts now, as the fill-in words say them."""
    from app.api.clubs.models import Clubs
    from app.api.events.models import EventCategories, EventInterestsLink, EventSpeakers
    from app.api.interests.models import Interests
    from app.api.orgs.models import Organizations
    from app.api.ticketing import places

    club = club or await session.get(Clubs, event.club_id)
    speakers = list(await session.scalars(
        select(EventSpeakers.name).where(EventSpeakers.event_id == event.id, EventSpeakers.is_deleted == False)  # noqa: E712
        .order_by(EventSpeakers.display_order, EventSpeakers.id)
    ))
    try:
        left = (await places.summary(session, event)).left
    except Exception:  # noqa: BLE001 - a draft never fails for a count
        left = None
    tags: list[str] = []
    for words in [getattr(club, "name", None)]:
        if (t := tag_of(words)) and t not in tags:
            tags.append(t)
    if club is not None and club.org_id:
        org = await session.scalar(select(Organizations.name).where(Organizations.id == club.org_id))
        if (t := tag_of(org)) and t not in tags:
            tags.append(t)
    if club is not None and club.location_name:
        if (t := tag_of(club.location_name.split(",")[0])) and t not in tags:
            tags.append(t)
    category = await session.scalar(select(EventCategories.name).where(EventCategories.id == event.category_id))
    interests = list(await session.scalars(
        select(Interests.name).join(EventInterestsLink, EventInterestsLink.interest_id == Interests.id)
        .where(EventInterestsLink.event_id == event.id).limit(6)
    ))
    for words in [category, *interests]:
        if (t := tag_of(words)) and t not in tags and t.lower() != "general":
            tags.append(t)
    return Facts(
        event_name=event.name or "", date=date_text(event.event_datetime), time=time_text(event.event_datetime),
        venue=venue_text(event), fee=fee_text(event), seats_left=str(left) if left is not None else "many",
        speakers=_names(speakers), about=event.about or "", club_name=getattr(club, "name", "") or "", hashtags=tags[:10],
    )


def render(text: str, words: dict[str, str]) -> str:
    """The text with its fill-in words filled; a word we do not know stays as it is."""
    return _WORD.sub(lambda m: words[m.group(1)] if m.group(1) in words else m.group(0), text or "")


def unknown_words(text: str) -> list[str]:
    return sorted({w for w in _WORD.findall(text or "") if w not in FILL_INS})


def hashtags_in(text: str) -> list[str]:
    return _HASHTAG.findall(text or "")


def _tags(f: Facts, n: int) -> str:
    return " ".join(f"#{t}" for t in f.hashtags[:n])


def draft(platform: str, f: Facts) -> str:
    """A first draft for the platform, in its own tone and length, with fill-in words."""
    p = P.get(platform)
    speakers = "\n🎤 {speakers}" if f.speakers else ""
    if platform == "instagram":
        about = excerpt(f.about, 300)
        body = (
            "{event_name} ✨\n\n📅 {date} · {time}\n📍 {venue}\n🎟️ {fee}" + speakers
            + (f"\n\n{about}" if about else "")
            + "\n\nRegister: link in bio 🔗"
        )
        tags = _tags(f, p.hashtags)
        return body + (f"\n\n{tags}" if tags else "")
    if platform == "facebook":
        about = excerpt(f.about, 600)
        return (
            "{event_name}\n\n📅 {date}, {time}\n📍 {venue}\n🎟️ {fee}" + speakers
            + (f"\n\n{about}" if about else "") + "\n\nRegister: {link}"
        )
    if platform in ("linkedin", "linkedin_page"):
        about = excerpt(f.about, 700)
        opener = f"{f.club_name} presents {{event_name}}." if f.club_name else "{event_name}"
        tags = _tags(f, p.hashtags)
        return (
            opener + (f"\n\n{about}" if about else "") + "\n\n📅 {date}, {time}\n📍 {venue}"
            + ("\n🎤 Speakers: {speakers}" if f.speakers else "") + "\n🎟️ {fee}\n\nRegister here: {link}"
            + (f"\n\n{tags}" if tags else "")
        )
    # X: short, one link, one hashtag; trimmed to fit with the facts filled.
    tag = f" #{f.hashtags[0]}" if f.hashtags else ""
    for text in (
        "{event_name} · {date}, {time} · {venue}. Register: {link}" + tag,
        "{event_name} · {date}, {time}. Register: {link}" + tag,
        "{event_name} · {date}, {time}. Register: {link}",
    ):
        if weighted_length(render(text, f.words("https://l.example/12345678"))) <= p.text_limit:
            return text
    return "{event_name}. Register: {link}"


def instagram_note(text: str) -> str:
    """Instagram's captions do not open links: "link in bio" instead of {link}."""
    return text.replace("{link}", "link in bio")


# ─── Counting and checking ───────────────────────────────────────────────


def _wide(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1100 and (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                            or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60
                            or 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF)


def weighted_length(text: str, platform: str = "x") -> int:
    """Characters as the platform counts them: on X a link is 23 and a wide
    character (CJK, an emoji) two; elsewhere each character is one."""
    if platform != "x":
        return len(text or "")
    links = _URL.findall(text or "")
    rest = _URL.sub("", text or "")
    return P.X_LINK_LENGTH * len(links) + sum(2 if _wide(ch) else 1 for ch in rest if not unicodedata.combining(ch))


def check(platform: str, raw: str, rendered: str, *, picture_size: Optional[tuple[int, int]] = None,
          has_picture: bool = False) -> tuple[list[dict], dict]:
    """The preview's warnings and counts for one platform's text and picture."""
    p = P.get(platform)
    warnings: list[dict] = []
    length = weighted_length(rendered, platform)
    tags = hashtags_in(rendered)
    if length > p.text_limit:
        code = "x_over_280" if platform == "x" else f"{'ig' if platform == 'instagram' else platform}_over_{p.text_limit}"
        warnings.append({"code": code, "message": f"{p.label} allows {p.text_limit:,} characters; this is {length:,}."})
    if platform == "instagram":
        if len(tags) > P.INSTAGRAM_MAX_HASHTAGS:
            warnings.append({"code": "ig_hashtags_over_30", "message": f"Instagram allows 30 hashtags; this has {len(tags)}."})
        if "{link}" in (raw or "") or _URL.search(rendered or ""):
            warnings.append({"code": "ig_link_not_clickable", "message": "Links in Instagram captions can't be tapped: say \"link in bio\" instead."})
        if not has_picture:
            warnings.append({"code": "image_missing", "message": "Instagram needs a picture with every post."})
    if picture_size and p.aspect:
        ratio = picture_size[0] / picture_size[1]
        low, high = p.aspect
        if ratio < low - 0.001 or ratio > high + 0.001:
            warnings.append({"code": "image_shape", "message": f"{p.label} needs a picture from 4:5 to 1.91:1; pick its own size."})
    unknown = unknown_words(raw)
    if unknown:
        warnings.append({"code": "unknown_fill_in", "message": "Not a fill-in word: " + ", ".join("{" + w + "}" for w in unknown)})
    return warnings, {"characters": length, "limit": p.text_limit, "hashtags": len(tags)}


# ─── An event's details, then and now ────────────────────────────────────


def snapshot(event) -> dict:
    """What a post may say about the event, to tell later what changed."""
    return {
        "event_datetime": event.event_datetime.isoformat() if event.event_datetime else None,
        "duration": float(event.duration or 0),
        "location_name": event.location_name, "is_online": bool(event.is_online),
        "has_fee": bool(event.has_fee), "reg_fee": str(event.reg_fee) if event.reg_fee is not None else None,
        "reg_enddate": event.reg_enddate.isoformat() if event.reg_enddate else None,
    }


def literal_mentions(before: dict) -> dict[str, list[str]]:
    """How a text written by hand may mention each detail as it was: the
    date, the time, the venue, the fee -- the strings to look for."""
    out: dict[str, list[str]] = {"date": [], "time": [], "venue": [], "fee": []}
    raw = before.get("event_datetime")
    if raw:
        when = _local(datetime.fromisoformat(raw))
        d = when.day
        out["date"] = [f"{d} {when:%b}", f"{when:%b} {d}", f"{d} {when:%B}", f"{when:%B} {d}", f"{when:%d/%m}", f"{d}/{when.month}"]
        out["time"] = [f"{when:%I:%M %p}".lstrip("0"), f"{when:%I %p}".lstrip("0"), f"{when:%I:%M%p}".lstrip("0"),
                       f"{when:%I%p}".lstrip("0"), f"{when:%H:%M}"]
    venue = (before.get("location_name") or "").strip()
    if len(venue) >= 4 and not before.get("is_online"):
        out["venue"] = [venue]
    fee = before.get("reg_fee")
    if before.get("has_fee") and fee:
        amount = Decimal(fee)
        n = str(int(amount)) if amount == amount.to_integral() else f"{amount:.2f}"
        out["fee"] = [f"₹{n}", f"₹ {n}", f"Rs {n}", f"Rs. {n}", f"INR {n}"]
    return out


def mentions(text: str, needles: list[str]) -> bool:
    low = (text or "").lower()
    return any(n and n.lower() in low for n in needles)
