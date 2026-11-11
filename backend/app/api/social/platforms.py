"""The five platforms: what each needs, allows and costs, and whether a club
may use it now (configured, past review, on its plan)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.api.social import crypto
from app.config import settings


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    #: The plan flag automatic posting to it needs (subscriptions.models).
    plan_feature: str
    #: The most characters a post may have.
    text_limit: int
    #: The poster size its drafts use (models.SIZES).
    default_size: str
    #: A post cannot go without a picture.
    needs_image: bool
    #: A link in the text opens (Instagram's captions do not).
    link_clickable: bool
    #: Hashtags its draft carries at most.
    hashtags: int
    #: The shapes (width / height) a picture may have, or None for any.
    aspect: Optional[tuple[float, float]] = None


PLATFORMS: dict[str, Platform] = {
    "facebook": Platform("facebook", "Facebook Page", "social_autopost", 63206, "facebook", False, True, 2),
    # Instagram: 2,200 characters, 30 hashtags, JPEG at a public address, 4:5 to 1.91:1.
    "instagram": Platform("instagram", "Instagram", "social_autopost", 2200, "ig_portrait", True, False, 8, (0.8, 1.91)),
    "linkedin": Platform("linkedin", "LinkedIn", "social_autopost", 3000, "linkedin", False, True, 3),
    "linkedin_page": Platform("linkedin_page", "LinkedIn Page", "social_autopost", 3000, "linkedin", False, True, 3),
    "x": Platform("x", "X", "social_x", 280, "x", False, True, 1),
}

#: Instagram's limit: posts by the API per account in a rolling 24 hours (a carousel counts as one).
INSTAGRAM_DAILY_LIMIT = 100
#: X counts every link as this many characters (t.co).
X_LINK_LENGTH = 23
#: Instagram's hashtags at most.
INSTAGRAM_MAX_HASHTAGS = 30


def get(key: str) -> Platform:
    return PLATFORMS[key]


def _set(name: str) -> bool:
    return bool((getattr(settings, name, "") or "").strip())


def configured(key: str) -> bool:
    """The platform's app is set up on this server (its id and secret saved),
    and tokens can be kept (SOCIAL_KEYS_SECRET)."""
    if not crypto.available():
        return False
    if key in ("facebook", "instagram"):
        return _set("META_APP_ID") and _set("META_APP_SECRET")
    if key in ("linkedin", "linkedin_page"):
        return _set("LINKEDIN_CLIENT_ID") and _set("LINKEDIN_CLIENT_SECRET")
    if key == "x":
        return _set("X_CLIENT_ID") and _set("X_CLIENT_SECRET")
    return False


def unavailable_reason(key: str) -> Optional[str]:
    """Why no club can use it on this server: `not_configured`, or
    `review_pending` for LinkedIn Pages until LinkedIn's review passes."""
    if not configured(key):
        return "not_configured"
    if key == "linkedin_page" and not settings.LINKEDIN_PAGES_ENABLED:
        return "review_pending"
    return None


async def plan_allows(session, club_id: int, key: str, *, actor=None) -> bool:
    from app.api.subscriptions import gates

    return await gates.allows(session, club_id, PLATFORMS[key].plan_feature, actor=actor)


async def require_plan(session, club_id: int, key: str, *, actor=None) -> None:
    from app.api.subscriptions import gates

    await gates.require_feature(session, club_id, PLATFORMS[key].plan_feature, actor=actor)


async def listing(session, club_id: int, *, actor=None) -> list[dict]:
    """GET /platforms: each platform, whether the club can connect and post
    to it now, and if not why."""
    out = []
    for key, p in PLATFORMS.items():
        reason = unavailable_reason(key)
        if reason is None and not await plan_allows(session, club_id, key, actor=actor):
            reason = "plan"
        out.append({"platform": key, "label": p.label, "available": reason is None, "reason": reason, "plan_feature": p.plan_feature})
    return out
