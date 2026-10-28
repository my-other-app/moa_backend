"""The picture formats an upload may arrive in, and what each is kept as.

Every path that takes a picture (the image columns, fields.prepare_image;
print art, doc_art; email pictures, images.store_image; product photos,
merch.service.product_photo; the AI draft's poster) reads its format here.

- **Kept as they came:** JPEG, PNG, WebP and GIF. Every browser, the app and
  email clients show them.
- **Converted on the way in:** HEIC/HEIF (an iPhone's photos, picked on a
  Mac or in Chrome), AVIF (many web downloads), BMP and TIFF (scanners, old
  exports). Not every client can show them, so each is decoded once, here,
  and stored as a JPEG, or as a PNG when some of it is see-through. The
  checks are the same as for any upload: bytes counted before anything
  decodes them, pixels from the header before the decoder runs, the
  camera's orientation applied.
- **Refused, saying what to do:** SVG (the server never draws an SVG
  someone sent: a drawing can name files and addresses for the renderer to
  fetch; the dashboard turns one into a PNG in the browser before sending
  it), PDF, and anything else.

HEIC is read by pillow-heif (libheif). AVIF, BMP and TIFF are Pillow's own.
Without pillow-heif installed (an image built before it was added to
requirements.txt) everything else still works, and a HEIC is refused as a
file that cannot be read.
"""
from __future__ import annotations

import io
import logging
from typing import Iterable, Optional

from PIL import Image

from app.response import CustomHTTPException

logger = logging.getLogger(__name__)

try:
    import pillow_heif

    # Only the main picture: a HEIC's thumbnails, depth maps and auxiliary
    # images are never used, so they are not read.
    pillow_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)
    HEIF_READABLE = True
except ImportError:  # pragma: no cover - only on an image built without it
    HEIF_READABLE = False
    logger.warning("pillow-heif is not installed: HEIC/HEIF uploads will be refused")

#: Pillow's format name -> the extension a stored file of it ends in.
KEPT = {"JPEG": "jpeg", "MPO": "jpeg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}
#: Read, then stored as a JPEG (or a PNG when see-through).
CONVERTED = frozenset({"HEIF", "AVIF", "BMP", "DIB", "TIFF"})

#: What works, in the words people know the formats by.
WORKS = "JPG, PNG, WebP, GIF, HEIC, AVIF, BMP or TIFF"

_HEIF_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1")


def sniff(raw: bytes) -> Optional[str]:
    """What a file that does not open as a picture looks like it is, from
    its first bytes: "svg", "pdf", "heif" or None."""
    head = raw[:2048]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "heif"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith(b"<") and b"<svg" in text:
        return "svg"
    return None


def not_readable(raw: bytes = b"") -> CustomHTTPException:
    """The 422 for bytes that do not open as a picture, saying what to do."""
    kind = sniff(raw)
    if kind == "svg":
        message = "That's an SVG drawing, which can't be uploaded as it is. Export it as a PNG and upload that."
    elif kind == "pdf":
        message = "That's a PDF, not a picture. Export the page as a PNG or JPG and upload that."
    elif kind == "heif":
        message = f"We couldn't read that HEIC photo. Upload it as a {WORKS} picture."
    else:
        message = f"That file isn't a picture we can read. Upload a {WORKS} picture."
    return CustomHTTPException(422, message=message, error_code="IMAGE_NOT_READABLE")


def refused(fmt: str, works: Optional[Iterable[str]] = None) -> CustomHTTPException:
    """The 422 for a picture we can read but do not take here."""
    names = _say(works) if works is not None else WORKS
    shown = {"MPO": "JPG", "JPEG": "JPG", "HEIF": "HEIC", "DIB": "BMP"}.get(fmt, fmt) or "That"
    return CustomHTTPException(
        422,
        message=f"{shown} pictures can't be used here. Upload a {names} picture.",
        error_code="IMAGE_FORMAT",
    )


def _say(exts: Iterable[str]) -> str:
    """["jpg", "png", "webp"] -> "JPG, PNG or WebP"."""
    words = {"jpg": "JPG", "jpeg": "JPG", "png": "PNG", "webp": "WebP", "gif": "GIF"}
    names = list(dict.fromkeys(words.get(e.lower(), e.upper()) for e in exts))
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} or {names[-1]}"


def may_be_picture(content_type: Optional[str]) -> bool:
    """Whether a multipart part's declared type leaves it worth reading.

    An image/* part is; so is one with no type, or application/octet-stream,
    which is how a browser sends a file whose type it does not know (a HEIC
    in Chrome on Windows, a phone's gallery photo). The bytes decide either
    way: every path that stores a picture opens it and refuses what is not one.
    """
    kind = (content_type or "").split(";")[0].strip().lower()
    return not kind or kind.startswith("image/") or kind == "application/octet-stream"


# ─── Converting ──────────────────────────────────────────────────────────


def has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info


def in_srgb(image: Image.Image) -> Image.Image:
    """The pixels in sRGB, when the picture names another colour space (a
    phone's Display P3, an Adobe RGB export). A grey one is left: grey is
    grey. sRGB is what a picture without a profile is read as, so the stored
    file needs none."""
    icc = image.info.get("icc_profile")
    if not icc or image.mode not in ("RGB", "RGBA", "CMYK"):
        return image
    try:
        from PIL import ImageCms

        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if image.mode != "CMYK" and "srgb" in (ImageCms.getProfileDescription(source) or "").lower():
            return image
        alpha = image.getchannel("A") if image.mode == "RGBA" else None
        out = ImageCms.profileToProfile(
            image.convert("RGB") if alpha is not None else image, source, ImageCms.createProfile("sRGB"),
            renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB",
        )
        if alpha is not None:
            out.putalpha(alpha)
        return out
    except Exception:  # noqa: BLE001 - a profile that cannot be read: the pixels as they are
        return image


def plain_pixels(image: Image.Image) -> Image.Image:
    """RGB, RGBA or L: what is encoded. Alpha that is opaque everywhere is dropped."""
    if has_alpha(image):
        image = image.convert("RGBA")
        low, _ = image.getchannel("A").getextrema()
        if low == 255:
            image = image.convert("RGB")
    elif image.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16-bit grey: scaled to 8 bits, not clipped to white.
        image = image.convert("I").point(lambda v: v * (1 / 257)).convert("L")
    elif image.mode == "1":
        image = image.convert("L")
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def kept_as(image: Image.Image) -> tuple[Image.Image, str]:
    """A decoded (and upright) picture of a CONVERTED format as it is
    stored: in sRGB, without metadata, and "png" when some of it is
    see-through, else "jpeg"."""
    image = plain_pixels(in_srgb(image))
    image.info = {}
    return image, ("png" if image.mode == "RGBA" else "jpeg")
