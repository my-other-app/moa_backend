"""One adapter per platform, behind one interface (base.py)."""
from __future__ import annotations


def for_platform(platform: str):
    """The adapter module for `platform`."""
    from app.api.social.adapters import linkedin, meta, x

    return {
        "facebook": meta.FACEBOOK,
        "instagram": meta.INSTAGRAM,
        "linkedin": linkedin.MEMBER,
        "linkedin_page": linkedin.PAGE,
        "x": x.X,
    }[platform]
