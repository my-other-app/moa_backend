"""A guest's photo for their poster (CONTRACT.md §6.2-6.5, §7).

Cleaned once, as it arrives, by the pipeline every printed picture goes
through (app/core/storage/doc_art.py): turned upright, converted to sRGB, at
most LONGEST px on its longest side, and stored with no metadata at all (a
phone's EXIF, its GPS position included, is left behind). Kept in the
private bucket under guest_posters/{event}/{registration}/ -- never a public
or CDN folder, never a presigned link: the API hands it to its owner, and
the renderer reads it into the page as a data: URI.

Framing is {zoom, x, y}: zoom 1 covers the frame exactly (object-fit:
cover), x and y the point of the photo (fractions of its width and height)
at the frame's centre. `crop` cuts exactly that window and sizes it to the
frame, so the page only ever shows a picture already of the frame's shape.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import uuid
from typing import Optional

from app.core.storage import doc_art, objects
from app.core.storage.bucket import prefix

MAX_BYTES = 12 * 1024 * 1024
#: The longest side kept: a story's frame is at most 1080 px wide.
LONGEST = 2000
DEFAULT_FRAMING = {"zoom": 1.0, "x": 0.5, "y": 0.45}
ZOOM_MIN, ZOOM_MAX = 1.0, 5.0
#: A frame's picture at most this many times its canvas pixels.
SHARPEN = 1.5


def folder(event_id: int, registration_id) -> str:
    return prefix("guest_posters", str(int(event_id)), str(registration_id))


def is_ours(key: Optional[str], event_id: int, registration_id) -> bool:
    return bool(key) and key.startswith(folder(event_id, registration_id)) and ".." not in key.split("/")


def clean(raw: bytes) -> doc_art.PrintArt:
    """The upload made safe and small (413/422 from the pipeline when it is not a picture)."""
    return doc_art.normalise(raw, max_bytes=MAX_BYTES, longest=LONGEST)


def store(event_id: int, registration_id, art: doc_art.PrintArt) -> tuple[str, str]:
    """Kept privately; (key, sha256 of the bytes). Blocking."""
    key = f"{folder(event_id, registration_id)}photo-{uuid.uuid4().hex}.{art.ext()}"
    objects.put_bytes(key, art.data, art.content_type)
    return key, hashlib.sha256(art.data).hexdigest()


def read(key: Optional[str]) -> Optional[bytes]:
    """The stored bytes, or None. Not through the renderer's shared image
    cache: a photo is one guest's. Blocking."""
    if not key:
        return None
    return objects.fetch(key, quiet_if_missing=True)


def delete(keys) -> None:
    """Gone from the bucket now; a key already gone is fine. Blocking."""
    from app.config import settings

    keys = [k for k in keys if k]
    if not keys:
        return
    client = objects.client()
    for i in range(0, len(keys), 1000):
        client.delete_objects(Bucket=settings.S3_BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})


def content_type(key: str) -> str:
    return "image/png" if key.endswith(".png") else "image/jpeg"


# ─── Framing ────────────────────────────────────────────────────────────────


def checked_framing(body: dict) -> dict:
    """{zoom, x, y} within range, or ValueError naming the field."""
    out = {}
    for name, lo, hi in (("zoom", ZOOM_MIN, ZOOM_MAX), ("x", 0.0, 1.0), ("y", 0.0, 1.0)):
        value = body.get(name, DEFAULT_FRAMING[name])
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(name)
        if not math.isfinite(value) or value < lo or value > hi:
            raise ValueError(name)
        out[name] = round(value, 4)
    return out


def framing_for(saved: Optional[dict], fmt: str) -> dict:
    saved = saved if isinstance(saved, dict) else {}
    mine = saved.get(fmt) if isinstance(saved.get(fmt), dict) else saved.get("*")
    if not isinstance(mine, dict):
        return dict(DEFAULT_FRAMING)
    try:
        return checked_framing(mine)
    except ValueError:
        return dict(DEFAULT_FRAMING)


def window(photo_w: int, photo_h: int, frame_w: float, frame_h: float, framing: dict) -> tuple[float, float, float, float]:
    """The part of the photo the frame shows: (left, top, width, height) in photo px."""
    s = max(frame_w / photo_w, frame_h / photo_h) * float(framing["zoom"])
    ww, wh = min(photo_w, frame_w / s), min(photo_h, frame_h / s)
    left = min(max(float(framing["x"]) * photo_w - ww / 2, 0.0), photo_w - ww)
    top = min(max(float(framing["y"]) * photo_h - wh / 2, 0.0), photo_h - wh)
    return left, top, ww, wh


def crop(raw: bytes, frame_w: float, frame_h: float, framing: dict) -> Optional[bytes]:
    """The frame's picture: the window cut and sized to the frame (at most
    SHARPEN times its canvas pixels, never larger than the window). A JPEG,
    or a PNG when the photo has transparency (a cut-out). Blocking."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            img = opened.convert("RGBA") if opened.mode in ("RGBA", "LA", "P") else opened.convert("RGB")
    except Exception:  # noqa: BLE001 - not readable now: the initials tile
        return None
    w, h = img.size
    if w < 2 or h < 2 or frame_w <= 0 or frame_h <= 0:
        return None
    left, top, ww, wh = window(w, h, frame_w, frame_h, framing)
    scale = min(SHARPEN, max(ww / frame_w, 1e-6))
    out_w, out_h = max(1, round(frame_w * scale)), max(1, round(frame_h * scale))
    cut = img.crop((round(left), round(top), round(left + ww), round(top + wh))).resize((out_w, out_h), Image.LANCZOS)
    buf = io.BytesIO()
    if cut.mode == "RGBA" and cut.getextrema()[3][0] < 255:
        cut.save(buf, format="PNG", optimize=False, compress_level=6)
    else:
        cut.convert("RGB").save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


def data_uri(data: bytes) -> str:
    kind = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(data).decode("ascii")


def frame_of(layout: Optional[dict]) -> Optional[dict]:
    """The design's photo frame: its largest visible guest_photo layer."""
    best = None
    for layer in (layout or {}).get("layers") or []:
        if not isinstance(layer, dict) or layer.get("kind") != "image" or layer.get("source") != "guest_photo" or layer.get("hidden"):
            continue
        area = float(layer.get("width") or 0) * float(layer.get("height") or 0)
        if best is None or area > best[0]:
            best = (area, layer)
    if best is None:
        return None
    layer = best[1]
    radius = layer.get("radius", 0)
    if isinstance(radius, (int, float)):
        radius = [radius] * 4
    return {"x": layer.get("x", 0), "y": layer.get("y", 0), "width": layer.get("width"), "height": layer.get("height"),
            "radius": list(radius or [0, 0, 0, 0]), "rotation": layer.get("rotation", 0) or 0}
