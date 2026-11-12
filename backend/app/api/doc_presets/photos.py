"""A guest's photo for a badge (D5).

In order, the first there is:

1. the registration's answer to the form's photo question (an `image`
   field) -- read only when it is a file in this event's own folder of our
   bucket, never an address anywhere else;
2. the person's own profile photo in the app (UserProfiles.profile_pic, its
   800 px variant).

With neither, the photo is empty and the design draws the guest's initials
in the photo's frame (app.core.doc_designs: `data-slot="photo"`), so a
photo design never prints an empty frame.

A photo is cut square around the upper middle (where a face is) and set at
600 px or more (at most 800), as the designer's notes ask, and goes into the
document as a `data:` URI: the render service fetches nothing.
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

from app.api.doc_presets.values import Subject

logger = logging.getLogger(__name__)

MIN_SIDE = 600
MAX_SIDE = 800
#: A photo larger than this is not read (a camera original is a few MB).
MAX_BYTES = 15 * 1024 * 1024


def _event_key(answer: Optional[str], event_id) -> Optional[str]:
    """The bucket key a photo answer names, if it is one of this event's files."""
    if not answer or event_id is None:
        return None
    from app.config import settings
    from app.core.storage.bucket import key_from_url

    text = answer.strip()
    key = key_from_url(text) if "://" in text else text
    if not key:
        return None
    base = (settings.S3_BASE_PATH or "").strip("/")
    rest = key[len(base):].lstrip("/") if base and key.startswith(base) else key.lstrip("/")
    return key if rest.startswith(f"events/{event_id}/") else None


def _read(key: Optional[str]) -> Optional[bytes]:
    if not key:
        return None
    from app.core.storage import objects

    try:
        data = objects.get_bytes(key)
    except Exception:  # noqa: BLE001 - a missing photo is the initials, not a failed card
        logger.warning("a badge photo could not be read", exc_info=True)
        return None
    return data if data and len(data) <= MAX_BYTES else None


def square(raw: bytes) -> Optional[bytes]:
    """The picture cut square around its upper middle, MIN_SIDE..MAX_SIDE px,
    as a JPEG; None when it is not a picture."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
        w, h = img.size
        side = min(w, h)
        if side < 16:
            return None
        left = (w - side) // 2
        # A portrait's face sits above its middle.
        top = int((h - side) * 0.3)
        img = img.crop((left, top, left + side, top + side))
        target = max(MIN_SIDE, min(MAX_SIDE, side))
        if target != side:
            img = img.resize((target, target), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88, optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - not a picture we can read
        return None


def photo_bytes(subject: Subject, event_id) -> Optional[bytes]:
    """The first photo the chain finds, squared; None when there is none.
    Blocking (a bucket read and Pillow): called while the document is drawn."""
    for key in (_event_key(subject.photo_answer, event_id), subject.profile_photo_key):
        raw = _read(key)
        if raw:
            done = square(raw)
            if done:
                return done
    return None


def photo_uri(subject: Subject, event_id) -> str:
    """The photo as a data URI, or "" for the design's initials tile."""
    data = photo_bytes(subject, event_id)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii") if data else ""
